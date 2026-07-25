"""The attribution history at the scale its read bound is stated over.

**Validates: Requirements 43.3, 43.4, 43.10, 36.2**

The sibling module of this concern asserts the behaviour of one supersession
against a live instance: the ordered pair of statements, the Event that commits
with them, the refusal that undoes both, the partial uniqueness, the confidence
rule, the two read forms over a history of two or three versions, and the
withdrawal. Nothing here repeats any of that. What this module adds is the two
claims that only appear at length, and one claim about the ordered pair that the
sibling asserts from one side only.

**The read bound is measured rather than assumed.** Requirement 43.10 states that
the as-of-attribution query answers for an Artifact carrying at least 100
Attribution_Versions inside 1 second, and nothing else in the suite builds that
history. This module builds exactly a hundred versions of one Artifact, spread
over four Clients so the answer is several rows rather than one, and times the
read at the instant whose index range is the widest the corpus admits: the
validity start of the newest version, which every one of the hundred versions
precedes or equals.

**The measurement is read beside the plan, so the number is interpretable.** The
design's claim is not that the read happens to be quick but that it is an index
range over one Artifact's versions with the Client, the method, the confidence,
and the successor stored in that index, so containment filtering and projection
need no row fetch. A timing alone would pass on a table small enough to sit in
memory whatever the plan did. The plan is therefore asserted as well: the read is
served by the covering index over one Artifact's versions, over a bounded span,
with no whole-table read and no fetch back to the table. Statistics are refreshed
before either is taken, because a plan built on absent statistics is not the plan
a deployed cluster produces.

**A history a hundred versions long is still one line per pair.** The sibling
module checks that exactly one version stays current across three writes. Over
twenty-five writes per pair the stronger structural claims become checkable: every
closed version is closed in both columns and no version is half-closed, the
superseding references of one pair form a single unbranched line from the oldest
version to the current one, and consecutive intervals meet exactly, so the pair's
validity intervals tile the whole span without gap or overlap. A supersession that
closed the wrong row, or that closed a row twice, would show up here as a fork or
a gap while every count still looked right.

**The ordered pair of statements is atomic from the insert's side too.** The
sibling module refuses the Ledger Event, which is the third statement, and asserts
that the closure and the successor are both discarded. The statement between them
is never refused there, so the case where the closure has committed nothing and
the successor is the statement that fails is untested. Here the successor is given
an identifier a stored version of another pair already holds, which the primary key
refuses after the closure has already run. The claim is that the pair still holds
its original version, current, at its original confidence: the closure reached no
committed state on its own.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.errors import AttributionImmutableError
from molt.models.artifact import ArtifactKind
from molt.models.binding import BindingMethod
from molt.store import Connection, MemoryStore
from molt.store.attribution import (
    ATTRIBUTION_AS_OF_QUERY,
    AttributionSubmission,
    SupersessionContext,
    attribution_as_of,
    write_attribution,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The scale the requirement names and the bound it names for it. The versions are
# spread over several Clients so the timed answer carries one row per Client
# rather than a single row, while the Artifact still carries the stated total.
CLIENTS: Final[int] = 4
VERSIONS_PER_CLIENT: Final[int] = 25
VERSION_TARGET: Final[int] = CLIENTS * VERSIONS_PER_CLIENT
AS_OF_BOUND_SECONDS: Final[float] = 1.0

# How many supersessions the corpus performs, which is every write after the first
# of each pair, and therefore how many Events it appends.
SUPERSESSIONS: Final[int] = CLIENTS * (VERSIONS_PER_CLIENT - 1)

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The fixture's own writes and reads. The module under test owns no tenant insert
# and no Session insert, and the history is read back column by column rather than
# through the surface that wrote it.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
SELECT_ARTIFACT_VERSIONS: Final[str] = (
    "SELECT id, client_id, method, confidence, valid_from, valid_to, superseded_by "
    "FROM client_binding WHERE artifact_id = %s "
    "ORDER BY client_id ASC, valid_from ASC, id ASC"
)
COUNT_ARTIFACT_VERSIONS: Final[str] = "SELECT count(*) FROM client_binding WHERE artifact_id = %s"
COUNT_SUPERSESSION_EVENTS: Final[str] = "SELECT count(*) FROM ledger WHERE category = %s"
ANALYSE_BINDINGS: Final[str] = "ANALYZE client_binding"

# What the plan says when the read was served by the covering index over one
# Artifact's versions, and the two shapes that would mean it was not: a whole-table
# read, or a fetch back to the table for the columns the index is meant to carry.
# The plan is lowercased before any of the three is looked for.
COVERING_INDEX: Final[str] = "client_binding@binding_as_of"
FULL_SCAN: Final[str] = "full scan"
INDEX_JOIN: Final[str] = "index join"

# The command and the machine every supersession Event of this module records.
AGENT_CLI: Final[str] = "molt"
MACHINE_ID: Final[str] = "integration-machine"

# The Event category a supersession appends, named as a value so the count above
# binds it rather than interpolating it.
SUPERSEDED_CATEGORY: Final[str] = "attribution_superseded"

# The detection method every version of the corpus carries. One method throughout
# is deliberate: each write supersedes on a strictly greater confidence alone, so
# no write of the corpus is a restatement and the history grows by one row per
# write.
CORPUS_METHOD: Final[BindingMethod] = BindingMethod.SCOPE

# An instant with an offset, derived from the epoch rather than written as a
# literal, so no example depends on when it ran. It is a detection reading; every
# validity instant this module compares against is read back from a stored row.
DETECTED_AT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# How far outside the corpus's own span the two boundary readings are taken.
OUTSIDE_SECONDS: Final[float] = 60.0

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


@dataclass(frozen=True, slots=True)
class StoredVersion:
    """One stored version of the corpus, read back column by column."""

    id: UUID
    client_id: UUID
    method: str
    confidence: float
    valid_from: datetime
    valid_to: datetime | None
    superseded_by: UUID | None

    @property
    def current(self) -> bool:
        """Whether this row is the live claim for its pair."""
        return self.superseded_by is None


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def rows_of(
    connection: DriverConnection,
    statement: str,
    params: tuple[object, ...],
) -> list[tuple[object, ...]]:
    """Read every row of one parameterised statement on the fixture's connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return list(cursor.fetchall())


def _moment(value: object) -> datetime:
    """Narrow a stored timestamp, refusing anything else."""
    assert isinstance(value, datetime), "a validity column holds an instant"
    return value


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and a tenant factory."""

    store: MemoryStore
    connection: DriverConnection

    def tenant(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:10]}", "Tenant", "eu"),
        )
        return identifier

    def session(self, client_id: UUID) -> UUID:
        """Place one Session for a tenant, which a supersession Event belongs to."""
        identifier = uuid4()
        send(self.connection, INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
        return identifier

    def context(self, session_id: UUID) -> SupersessionContext:
        """The Session context a supersession Event is recorded within."""
        return SupersessionContext(
            session_id=session_id,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=DETECTED_AT + RETENTION,
        )

    def versions(self, artifact_id: UUID) -> tuple[StoredVersion, ...]:
        """Every stored version of one Artifact, grouped by Client and oldest first."""
        return tuple(
            StoredVersion(
                id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
                client_id=row[1] if isinstance(row[1], UUID) else UUID(str(row[1])),
                method=str(row[2]),
                confidence=float(str(row[3])),
                valid_from=_moment(row[4]),
                valid_to=None if row[5] is None else _moment(row[5]),
                superseded_by=None if row[6] is None else UUID(str(row[6])),
            )
            for row in rows_of(self.connection, SELECT_ARTIFACT_VERSIONS, (artifact_id,))
        )

    def count(self, statement: str, params: tuple[object, ...]) -> int:
        """Read one count on the fixture's own connection."""
        rows = rows_of(self.connection, statement, params)
        assert rows, "a count statement produced no row"
        return int(str(rows[0][0]))

    def plan_of(self, artifact_id: UUID, at: datetime) -> str:
        """The plan the cluster produces for one as-of read, lowercased."""
        with self.connection.cursor() as cursor:
            cursor.execute("EXPLAIN " + ATTRIBUTION_AS_OF_QUERY, (artifact_id, at, at))
            rows = cursor.fetchall()
        return "\n".join(" ".join(str(column) for column in row) for row in rows).lower()


def submission(
    artifact_id: UUID,
    client_id: UUID,
    *,
    method: BindingMethod = CORPUS_METHOD,
    confidence: float,
) -> AttributionSubmission:
    """One detection result for a pair, as the Binding_Detector submits it."""
    return AttributionSubmission(
        artifact_id=artifact_id,
        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
        client_id=client_id,
        method=method,
        confidence=confidence,
        detected_at=DETECTED_AT,
    )


def corpus_confidence(position: int) -> float:
    """The confidence the version at one position of a pair's history carries.

    Strictly increasing across a pair's writes and inside the closed unit interval
    at both ends, so every write after the first supersedes on confidence alone and
    the greater-confidence rule never holds a value back.
    """
    return (position + 1) / (VERSIONS_PER_CLIENT + 1)


@dataclass(frozen=True, slots=True)
class Corpus:
    """The built history: one Artifact, several Clients, the stated version count."""

    cluster: Cluster
    artifact_id: UUID
    client_ids: tuple[UUID, ...]
    stored: tuple[StoredVersion, ...]

    def of_client(self, client_id: UUID) -> tuple[StoredVersion, ...]:
        """One pair's whole history, oldest first."""
        return tuple(version for version in self.stored if version.client_id == client_id)

    @property
    def widest_instant(self) -> datetime:
        """The instant whose index range covers every version the Artifact carries.

        The newest validity start in the corpus. Every version of the Artifact began
        at or before it, so the containment predicate's lower term excludes nothing
        and the read spans the whole history rather than a suffix of it.
        """
        return max(version.valid_from for version in self.stored)


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store whose connections see that schema."""
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


@pytest.fixture(scope="module")
def corpus(cluster: Cluster) -> Corpus:
    """Build one Artifact's hundred-version history through the shipped write path.

    Every version comes from the surface under test rather than from a direct
    statement, because what the timed read must scan is a history the supersession
    path produced: the closures, the successor inserts, and the Events all present,
    in the order and the transactions the requirement states. A hundred writes is
    affordable at this scale, and placing the rows directly would measure a read
    over a shape no write path can produce.

    Module scope pays that cost once, and statistics are refreshed afterwards so the
    plan the measurement is read beside is the plan a populated table produces.
    """
    artifact_id = uuid4()
    client_ids = tuple(cluster.tenant() for _ in range(CLIENTS))
    contexts = {client_id: cluster.context(cluster.session(client_id)) for client_id in client_ids}

    started = time.perf_counter()
    for position in range(VERSIONS_PER_CLIENT):
        for client_id in client_ids:
            write_attribution(
                cluster.store,
                submission(artifact_id, client_id, confidence=corpus_confidence(position)),
                context=contexts[client_id],
            )
    built = time.perf_counter() - started

    with cluster.connection.cursor() as opened:
        opened.execute(ANALYSE_BINDINGS)

    stored = cluster.versions(artifact_id)
    print(
        f"corpus: {len(stored)} attribution versions of one artifact across "
        f"{CLIENTS} clients, written in {built:.1f} s"
    )
    return Corpus(
        cluster=cluster,
        artifact_id=artifact_id,
        client_ids=client_ids,
        stored=stored,
    )


# ---------------------------------------------------------------------------
# The shape a long history holds
# ---------------------------------------------------------------------------


def test_the_corpus_holds_the_stated_scale(corpus: Corpus) -> None:
    """The Artifact really carries the stated version count before anything is timed."""
    cluster = corpus.cluster
    assert VERSION_TARGET >= 100, "the corpus is sized below the count the bound is stated over"
    assert len(corpus.stored) == VERSION_TARGET
    assert cluster.count(COUNT_ARTIFACT_VERSIONS, (corpus.artifact_id,)) == VERSION_TARGET

    # One Event per supersession and none for a first write, over the whole corpus.
    assert cluster.count(COUNT_SUPERSESSION_EVENTS, (SUPERSEDED_CATEGORY,)) == SUPERSESSIONS, (
        "the Event count disagrees with the number of supersessions the corpus performed"
    )

    # The versions are spread over the Clients as planned, so the timed answer below
    # carries one row per Client rather than a single row.
    per_client = {client_id: len(corpus.of_client(client_id)) for client_id in corpus.client_ids}
    assert per_client == dict.fromkeys(corpus.client_ids, VERSIONS_PER_CLIENT)
    current = [version for version in corpus.stored if version.current]
    assert len(current) == CLIENTS
    assert {version.client_id for version in current} == set(corpus.client_ids)


def test_every_pair_of_the_history_is_one_unbranched_line(corpus: Corpus) -> None:
    """Closure is total, the successors form a single line, and the intervals tile it.

    Three structural claims that only become checkable at length. A supersession
    that closed a version already closed, or that closed the wrong version of the
    pair, would leave a fork or a gap here while the count of rows and the count of
    current versions both still looked right.
    """
    for client_id in corpus.client_ids:
        history = corpus.of_client(client_id)
        closed, live = history[:-1], history[-1]

        # Closure is total: a closed version carries both a validity end and a
        # successor, and the live one carries neither. No half-closed version exists.
        assert all(version.valid_to is not None for version in closed)
        assert all(version.superseded_by is not None for version in closed)
        assert live.valid_to is None
        assert live.superseded_by is None

        # One unbranched line: each closed version names the next version of the pair
        # as its successor, so following the references from the oldest reaches the
        # live one and no version is named by two.
        assert [version.superseded_by for version in closed] == [
            version.id for version in history[1:]
        ], "the successor references of the pair do not form a single line"
        assert len({version.superseded_by for version in closed}) == len(closed), (
            "two versions of one pair name the same successor, so the line forks"
        )

        # The intervals tile the span: each closure's end is exactly the next
        # version's start, so no instant of the span falls in no version and none
        # falls in two.
        for earlier, later in pairwise(history):
            assert earlier.valid_to == later.valid_from
            assert earlier.valid_from <= earlier.valid_to

        # Each position carries the confidence the corpus submitted for it, so the
        # history records the sequence of detections rather than a repeated value.
        assert [version.confidence for version in history] == pytest.approx(
            [corpus_confidence(position) for position in range(VERSIONS_PER_CLIENT)]
        )


# ---------------------------------------------------------------------------
# The as-of read over the whole history
# ---------------------------------------------------------------------------


def _contained(stored: Sequence[StoredVersion], at: datetime) -> set[UUID]:
    """The versions whose half-open validity interval contains an instant.

    Computed from the rows read back column by column rather than by asking the
    query again, so the query's answer is checked against an independent reading of
    the same history rather than against itself.
    """
    return {
        version.id
        for version in stored
        if version.valid_from <= at and (version.valid_to is None or version.valid_to > at)
    }


def test_the_as_of_read_answers_one_version_per_client_at_every_instant(corpus: Corpus) -> None:
    """At each of the hundred supersession instants, the answer is one row per Client."""
    store = corpus.cluster.store
    instants = sorted({version.valid_from for version in corpus.stored})
    assert len(instants) == VERSION_TARGET, "two versions of the corpus began at one instant"

    for at in instants:
        answer = attribution_as_of(store, corpus.artifact_id, at)
        client_ids = [version.client_id for version in answer]
        assert len(set(client_ids)) == len(client_ids), (
            "one Client contributed two versions to an as-of answer, so an instant "
            "belongs to two intervals of one pair"
        )
        assert client_ids == sorted(client_ids, key=str), "the answer is ordered by Client"
        assert {version.id for version in answer} == _contained(corpus.stored, at)


def test_the_as_of_read_is_empty_before_the_history_and_current_after_it(corpus: Corpus) -> None:
    """Outside the corpus's span the two answers are nothing and the live claims."""
    store = corpus.cluster.store
    began = min(version.valid_from for version in corpus.stored)
    outside = timedelta(seconds=OUTSIDE_SECONDS)

    assert attribution_as_of(store, corpus.artifact_id, began - outside) == ()

    afterwards = attribution_as_of(store, corpus.artifact_id, corpus.widest_instant + outside)
    assert {version.id for version in afterwards} == {
        version.id for version in corpus.stored if version.current
    }
    assert all(version.valid_to is None for version in afterwards)


# ---------------------------------------------------------------------------
# The bound, and the plan it rests on
# ---------------------------------------------------------------------------


@pytest.mark.perf
def test_the_as_of_read_answers_within_the_bound(corpus: Corpus) -> None:
    """The widest read over a hundred versions answers inside the stated bound.

    The instant chosen is the newest validity start in the corpus, so the index
    range the read covers is every version the Artifact carries rather than a
    suffix of them. One warm read is taken first, because the pool builds a
    connection on its first use and the bound is stated over the query rather than
    over establishing a session.
    """
    store = corpus.cluster.store
    at = corpus.widest_instant

    attribution_as_of(store, corpus.artifact_id, at)
    started = time.perf_counter()
    answer = attribution_as_of(store, corpus.artifact_id, at)
    elapsed = time.perf_counter() - started

    plan = corpus.cluster.plan_of(corpus.artifact_id, at)
    summary = (
        f"as-of read: {elapsed:.4f} s for {len(answer)} row(s) over {VERSION_TARGET} "
        f"versions, bound {AS_OF_BOUND_SECONDS:.0f} s, "
        f"covering index: {COVERING_INDEX in plan}\n{plan}"
    )
    print(summary)

    # A read that came back truncated would post an excellent time, so what came
    # back is checked before the timing is.
    assert {version.id for version in answer} == _contained(corpus.stored, at), (
        "the timed read did not return the versions the history says contain that "
        "instant, so the timing describes the wrong work"
    )

    # The plan is the claim: an index range over one Artifact's versions, with the
    # projection stored in that index so no row is fetched.
    assert COVERING_INDEX in plan, f"the as-of read was not served by the covering index: {plan}"
    assert FULL_SCAN not in plan, f"the as-of read read a whole table: {plan}"
    assert str(corpus.artifact_id) in plan, (
        "the scanned span does not name the Artifact, so the range is not bounded to "
        f"one Artifact's versions: {plan}"
    )
    assert INDEX_JOIN not in plan, (
        f"the as-of read fetched rows back from the table, so the stored projection "
        f"is not carrying the read: {plan}"
    )

    assert elapsed <= AS_OF_BOUND_SECONDS, summary


# ---------------------------------------------------------------------------
# The ordered pair, from the successor's side
# ---------------------------------------------------------------------------


def test_a_refused_successor_leaves_the_closure_uncommitted(corpus: Corpus) -> None:
    """The closure reaches no committed state when the statement after it is refused.

    The successor is offered an identifier a stored version of another pair already
    holds, so the primary key refuses the insert once the closure has already run
    and returned. What the refusal must leave behind is the pair exactly as it was:
    one version, current, at the confidence it was written with.
    """
    cluster = corpus.cluster
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)
    taken = corpus.stored[0].id

    first = write_attribution(
        cluster.store,
        submission(artifact_id, owner, confidence=corpus_confidence(0)),
        context=context,
    )

    with pytest.raises(AttributionImmutableError):
        write_attribution(
            cluster.store,
            submission(artifact_id, owner, confidence=corpus_confidence(1)),
            context=context,
            version_id=taken,
        )

    stored = cluster.versions(artifact_id)
    assert len(stored) == 1, "the closure committed without its successor"
    assert stored[0].id == first.version_id
    assert stored[0].current
    assert stored[0].valid_to is None
    assert stored[0].confidence == pytest.approx(corpus_confidence(0))
    assert cluster.count(COUNT_SUPERSESSION_EVENTS, (SUPERSEDED_CATEGORY,)) == SUPERSESSIONS, (
        "the refused supersession appended an Event"
    )
