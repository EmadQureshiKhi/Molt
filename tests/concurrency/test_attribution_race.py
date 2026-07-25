"""Two supersessions of one pair collide, and the loser supersedes the winner.

**Validates: Requirements 43.3, 36.2**

A supersession is a decision read followed by two ordered writes inside one
SERIALIZABLE transaction. The decision read runs on the write's own cursor, which
is what makes a concurrent supersession of the same pair a conflict rather than a
race: the other transaction writes into this one's read set, the cluster aborts
one of the two, and the shipped retry re-runs the body from the beginning so the
decision read happens again against the state that won.

Two claims follow, and this module asserts both.

**Exactly one unsuperseded version survives.** The partial unique index admits one
version carrying no successor per Artifact and Client pair, so two supersessions
that both committed a current version would be refused by the database. What is
asserted here is the whole outcome rather than the absence of that refusal: three
versions for the pair, one of them current, and two Events, because both writers
committed a supersession.

**The loser retries against the new current version.** This is the sharper half
and the one an implementation can get wrong while still leaving one current
version behind. A loser that simply failed would leave two versions and one
Event. A loser that superseded the version it first read would have closed a
version already closed, which matches no row and is reported as such rather than
written. Neither happens: the loser's committed supersession names the winner's
version as the version it closed, its own version begins exactly where the
winner's ended, and its confidence is the greater of its own submission and the
winner's, which the closing statement can only have learned by reading the
winner's row. The version it first read is carried back separately, so the
difference between what it read and what it superseded is visible rather than
inferred.

**The collision is arranged rather than hoped for.** A barrier before the
transaction begins would only start the writers together; whether their read sets
ever overlapped would be luck. Each writer instead takes the module's own decision
read as the first statement of its transaction and then waits at a gate, so
neither writer writes anything until both have that row in their read set. The
gate admits each writer once, so a retried body passes straight through it and no
attempt after the first waits for a writer that has already committed. The
injected sleeper counts the retry waits, so the abort the arrangement forces is
observed rather than assumed.

**An exhausted retry is a different finding from a retry.** A conflict followed by
a successful retry is the correct behaviour under contention. A writer that
conflicted on every attempt the policy permits committed nothing, and that is
reported as the named failure it is rather than as a smaller version count.

The submissions the two writers carry differ from each other in method and in
confidence, and both differ from the version already stored. Two identical
submissions would each be a restatement of the current version, supersede nothing,
and leave the pair with the version it started with, so a race between them would
assert nothing at all.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.errors import SerializationExhaustedError
from molt.models.artifact import ArtifactKind
from molt.models.binding import BindingMethod
from molt.store import Connection, Cursor, MemoryStore
from molt.store.attribution import (
    AttributionOutcome,
    AttributionSubmission,
    AttributionWrite,
    SupersessionContext,
    current_pair_version,
    record_attribution,
    write_attribution,
)
from molt.store.migrate import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

# How many writers supersede the same pair at once. Two is the whole claim: a
# third would add attempts without sharpening either assertion, and the outcome
# the requirement states is about the pair of statements rather than about depth of
# contention.
WRITERS: Final[int] = 2

# How many versions the pair holds once the race has settled, and how many Events
# that history recorded: the first write supersedes nothing, and each of the two
# writers supersedes exactly one version.
EXPECTED_VERSIONS: Final[int] = 1 + WRITERS
EXPECTED_EVENTS: Final[int] = WRITERS

# How long a writer waits at the gate for the other writer to reach it. Generous
# against a slow instance, and bounded so a writer that will never be joined
# reports a broken gate instead of holding the suite open.
GATE_TIMEOUT_SECONDS: Final[float] = 30.0

# The three submissions. The stored version is written first; the two writers then
# supersede it concurrently. Each writer's method differs from the other's and from
# the stored one's, and the higher of the two confidences exceeds both the other
# writer's and the stored one's, so whichever writer wins the loser still has
# something to supersede and the loser's own confidence records what it closed.
STORED_METHOD: Final[BindingMethod] = BindingMethod.MARKER
STORED_CONFIDENCE: Final[float] = 0.3
WRITER_METHODS: Final[tuple[BindingMethod, ...]] = (
    BindingMethod.SCOPE,
    BindingMethod.INHERITED,
)
WRITER_CONFIDENCES: Final[tuple[float, ...]] = (0.6, 0.4)
SETTLED_CONFIDENCE: Final[float] = max(WRITER_CONFIDENCES)

# The migration set is the whole one: the partial unique index, the total closure
# check, and the supersession Event category arrive with the attribution migration,
# and the self-referencing reference that would refuse the ordered pair of
# statements is dropped by the protection migration.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
SELECT_VERSIONS: Final[str] = (
    "SELECT id, method, confidence, valid_from, valid_to, superseded_by "
    "FROM client_binding WHERE artifact_id = %s AND client_id = %s "
    "ORDER BY valid_from ASC, valid_to ASC NULLS LAST, id ASC"
)
COUNT_CURRENT: Final[str] = (
    "SELECT count(*) FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NULL"
)
SELECT_EVENT_PAYLOADS: Final[str] = (
    "SELECT payload FROM ledger WHERE session_id = %s AND category = %s ORDER BY seq ASC"
)

# The Event category a supersession appends, bound as a value rather than written
# into the statement text.
SUPERSEDED_CATEGORY: Final[str] = "attribution_superseded"

# The command and the machine every Event of this module records.
AGENT_CLI: Final[str] = "molt"
MACHINE_ID: Final[str] = "concurrency-machine"

# A detection instant with an offset, derived from the epoch rather than written as
# a literal. Every validity instant asserted below is read back from a stored row.
DETECTED_AT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


class BackoffCounter:
    """Counts the waits the store's retry schedule performs, then performs them.

    Counting through the injected sleeper is what makes the abort observable without
    reading a log: the wrapper waits once per retry and nowhere else, so a count
    above nought means the cluster really did abort one of the two writers. The wait
    itself is still the schedule's own, so nothing about the shipped backoff is
    altered by measuring it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waits = 0

    def wait(self, seconds: float) -> None:
        """Record one retry wait, then wait it."""
        with self._lock:
            self._waits += 1
        time.sleep(seconds)

    def take(self) -> int:
        """Return the waits counted since the last reading, and start again."""
        with self._lock:
            counted = self._waits
            self._waits = 0
            return counted


class FirstReadGate:
    """Holds each writer inside its own transaction until every writer has read.

    The gate is what turns the collision from a hope into a fact. A writer arrives
    after its decision read and before any write, so when the gate releases, every
    writer holds the same current version in the read set of a transaction that has
    written nothing yet, and whichever writes second must be aborted.

    Each writer is admitted once. A retried body therefore passes straight through
    rather than waiting for a writer that has already committed, which is what keeps
    the retry the shipped one instead of a second synchronised round.
    """

    def __init__(self, parties: int, *, timeout: float) -> None:
        self._barrier = threading.Barrier(parties)
        self._timeout = timeout
        self._lock = threading.Lock()
        self._admitted: set[int] = set()

    def arrive(self, worker: int) -> bool:
        """Wait for the other writers if this writer has not waited yet.

        Returns whether this call waited, so a test can assert that every writer was
        held rather than that the gate happened to be open.
        """
        with self._lock:
            if worker in self._admitted:
                return False
            self._admitted.add(worker)
        self._barrier.wait(timeout=self._timeout)
        return True


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the retry counter."""

    store: MemoryStore
    connection: DriverConnection
    backoff: BackoffCounter

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


@dataclass(frozen=True, slots=True)
class StoredVersion:
    """One stored version, read back column by column."""

    id: UUID
    method: str
    confidence: float
    valid_from: datetime
    valid_to: datetime | None
    superseded_by: UUID | None

    @property
    def current(self) -> bool:
        """Whether this row is the live claim for its pair."""
        return self.superseded_by is None


@dataclass(frozen=True, slots=True)
class WriterOutcome:
    """What one writer read, how often it ran, and what it committed.

    Attributes:
        worker: Which writer this was.
        first_seen: The version its decision read reported on its first attempt,
            which is the version it would have superseded had nothing intervened.
        attempts: How many times its transaction body ran.
        held: Whether the gate held it, so the collision is a fact rather than a
            hope.
        write: What its transaction committed, or None when it committed nothing.
        failure: What stopped it, or None when it completed.
    """

    worker: int
    first_seen: UUID | None
    attempts: int
    held: bool
    write: AttributionWrite | None
    failure: Exception | None


@dataclass(frozen=True, slots=True)
class Race:
    """One settled race: the version that was there first, and the two writers."""

    artifact_id: UUID
    client_id: UUID
    session_id: UUID
    initial: AttributionWrite
    outcomes: tuple[WriterOutcome, ...]
    waits: int
    stored: tuple[StoredVersion, ...]
    payloads: tuple[dict[str, object], ...]

    @property
    def current(self) -> tuple[StoredVersion, ...]:
        """Every stored version of the pair carrying no successor."""
        return tuple(version for version in self.stored if version.current)


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


def stored_versions(
    connection: DriverConnection,
    artifact_id: UUID,
    client_id: UUID,
) -> tuple[StoredVersion, ...]:
    """Every stored version of one pair, oldest first, read column by column."""
    return tuple(
        StoredVersion(
            id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
            method=str(row[1]),
            confidence=float(str(row[2])),
            valid_from=_moment(row[3]),
            valid_to=None if row[4] is None else _moment(row[4]),
            superseded_by=None if row[5] is None else UUID(str(row[5])),
        )
        for row in rows_of(connection, SELECT_VERSIONS, (artifact_id, client_id))
    )


def submission(
    artifact_id: UUID,
    client_id: UUID,
    *,
    method: BindingMethod,
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


# The label the shipped one-transaction form of this write runs under, so a log
# record and the note an exhausted retry attaches read the same here as there.
WRITE_LABEL: Final[str] = "attribution_write"


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store bound to that schema.

    The pool, the retry policy, and the backoff schedule are all the shipped ones.
    The claim is about what the shipped supersession does under contention, so
    nothing about it is substituted; the sleeper is wrapped rather than replaced, so
    the waits are counted while still being waited.
    """
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

    backoff = BackoffCounter()
    with MemoryStore(connect_with=connect_with, sleep=backoff.wait) as store:
        yield Cluster(store=store, connection=fresh_schema, backoff=backoff)


def _payloads_of(
    connection: DriverConnection,
    session_id: UUID,
) -> tuple[dict[str, object], ...]:
    """The payload of each supersession Event of one Session, in sequence order."""
    payloads: list[dict[str, object]] = []
    for row in rows_of(connection, SELECT_EVENT_PAYLOADS, (session_id, SUPERSEDED_CATEGORY)):
        payload = row[0]
        assert isinstance(payload, dict), "a Ledger payload column holds an object"
        payloads.append(payload)
    return tuple(payloads)


def run_race(cluster: Cluster) -> Race:
    """Write one version, then have both writers supersede it at once.

    Each writer takes the module's decision read as the first statement of its own
    transaction and then waits at the gate, so both hold the same current version in
    a read set before either writes. A writer's failure is carried back rather than
    raised in its own thread, so the assertions see both writers' outcomes instead
    of only the first to fail.
    """
    client_id = cluster.tenant()
    session_id = cluster.session(client_id)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    initial = write_attribution(
        cluster.store,
        submission(artifact_id, client_id, method=STORED_METHOD, confidence=STORED_CONFIDENCE),
        context=context,
    )
    assert initial.outcome is AttributionOutcome.INSERTED, (
        "the version the two writers race to supersede was not written"
    )

    cluster.backoff.take()
    gate = FirstReadGate(WRITERS, timeout=GATE_TIMEOUT_SECONDS)
    outcomes: list[WriterOutcome] = []
    guard = threading.Lock()

    def work(worker: int) -> None:
        reads: list[UUID | None] = []
        held: list[bool] = []
        committed: AttributionWrite | None = None
        failure: Exception | None = None
        offered = submission(
            artifact_id,
            client_id,
            method=WRITER_METHODS[worker],
            confidence=WRITER_CONFIDENCES[worker],
        )

        def body(cursor: Cursor) -> AttributionWrite:
            prior = current_pair_version(cursor, artifact_id, client_id)
            reads.append(None if prior is None else prior.id)
            held.append(gate.arrive(worker))
            return record_attribution(cursor, offered, context=context)

        try:
            committed = cluster.store.in_serializable(body, label=WRITE_LABEL)
        except Exception as error:  # carried back rather than raised in this thread
            failure = error
        with guard:
            outcomes.append(
                WriterOutcome(
                    worker=worker,
                    first_seen=reads[0] if reads else None,
                    attempts=len(reads),
                    held=any(held),
                    write=committed,
                    failure=failure,
                )
            )

    threads = [
        threading.Thread(target=work, args=(worker,), name=f"superseder-{worker}")
        for worker in range(WRITERS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    waits = cluster.backoff.take()
    return Race(
        artifact_id=artifact_id,
        client_id=client_id,
        session_id=session_id,
        initial=initial,
        outcomes=tuple(sorted(outcomes, key=lambda outcome: outcome.worker)),
        waits=waits,
        stored=stored_versions(cluster.connection, artifact_id, client_id),
        payloads=_payloads_of(cluster.connection, session_id),
    )


def failure_report(outcomes: tuple[WriterOutcome, ...]) -> str:
    """Describe every writer that failed, so a stopped writer is named."""
    return "; ".join(
        f"writer {outcome.worker} failed on attempt {outcome.attempts} with "
        f"{type(outcome.failure).__name__}: {outcome.failure}"
        for outcome in outcomes
        if outcome.failure is not None
    )


def exhaustion_report(outcomes: tuple[WriterOutcome, ...]) -> str:
    """Name the writers whose retry budget ran out, which is its own diagnosis."""
    return "; ".join(
        f"writer {outcome.worker} conflicted on all {outcome.attempts} attempt(s) and "
        "committed nothing"
        for outcome in outcomes
        if isinstance(outcome.failure, SerializationExhaustedError)
    )


def committed_write(outcome: WriterOutcome) -> AttributionWrite:
    """The write one writer committed, refusing an outcome that committed none."""
    assert outcome.write is not None, f"writer {outcome.worker} committed nothing"
    return outcome.write


@pytest.fixture(scope="module")
def race(cluster: Cluster) -> Race:
    """One settled race, run once and asserted from two directions."""
    return run_race(cluster)


# ---------------------------------------------------------------------------
# The claims
# ---------------------------------------------------------------------------


def test_two_concurrent_supersessions_leave_one_unsuperseded_version(
    cluster: Cluster,
    race: Race,
) -> None:
    """Both writers commit a supersession, and the pair still holds one live claim."""
    # An exhausted retry is not a retry: the writer committed nothing, and every
    # count below would then be counting the wrong thing.
    assert not exhaustion_report(race.outcomes), exhaustion_report(race.outcomes)
    assert all(outcome.failure is None for outcome in race.outcomes), failure_report(race.outcomes)

    # The collision is a fact rather than a hope: both writers were held inside a
    # transaction that had read the current version and written nothing.
    assert all(outcome.held for outcome in race.outcomes), (
        "a writer passed the gate without waiting, so the two transactions were not "
        "both open across the same read and the outcome below says nothing"
    )
    assert race.waits >= 1, (
        "the retry schedule never waited, so the cluster aborted neither writer even "
        "though both read the same current version before either wrote"
    )

    # Both writers really superseded rather than one of them finding nothing to do.
    assert [committed_write(outcome).outcome for outcome in race.outcomes] == [
        AttributionOutcome.SUPERSEDED
    ] * WRITERS

    # The claim: one unsuperseded version, and a history that grew by one row per
    # writer rather than losing one of them.
    assert len(race.stored) == EXPECTED_VERSIONS
    assert len(race.current) == 1
    rows = rows_of(cluster.connection, COUNT_CURRENT, (race.artifact_id, race.client_id))
    assert int(str(rows[0][0])) == 1, "the pair holds more than one current version"

    # The surviving claim carries the greatest confidence submitted for the pair, so
    # neither writer lowered what the other had established.
    assert race.current[0].confidence == pytest.approx(SETTLED_CONFIDENCE)

    # And no supersession was silent: one Event per supersession, none for the first
    # write.
    assert len(race.payloads) == EXPECTED_EVENTS


def test_the_loser_supersedes_the_version_that_won(race: Race) -> None:
    """The retried writer closes the winner's version rather than the one it read.

    The winner is the writer whose committed supersession closed the version that
    was already stored; the loser is the other one. What makes this the sharp claim
    is that the loser read that same stored version too, on its first attempt, and
    committed a supersession of a different version: the one the winner wrote.
    """
    stored_first = race.initial.version_id
    winners = [
        outcome
        for outcome in race.outcomes
        if committed_write(outcome).superseded_id == stored_first
    ]
    assert len(winners) == 1, (
        "the version that was already stored was closed by neither writer or by both, "
        "so the two supersessions did not order themselves"
    )
    winner = committed_write(winners[0])
    losers = [outcome for outcome in race.outcomes if outcome.worker != winners[0].worker]
    loser_outcome = losers[0]
    loser = committed_write(loser_outcome)

    # Both writers began from the same reading, so the difference in what they
    # superseded is a consequence of the conflict rather than of what they saw.
    assert all(outcome.first_seen == stored_first for outcome in race.outcomes)

    # The loser did not fail, and it did not close the version it first read. It ran
    # its body again and closed the version that won.
    assert loser_outcome.attempts > 1, (
        "the losing writer committed without re-running its body, so its decision "
        "read never saw the version that won"
    )
    assert loser.superseded_id == winner.version_id
    assert loser.version_id != winner.version_id

    # The history is one line of three: the stored version, the winner's, the
    # loser's, each closure naming the next and only the last one live.
    assert [version.id for version in race.stored] == [
        stored_first,
        winner.version_id,
        loser.version_id,
    ]
    assert race.stored[0].superseded_by == winner.version_id
    assert race.stored[1].superseded_by == loser.version_id
    assert race.stored[2].current
    assert race.stored[1].valid_to == race.stored[2].valid_from, (
        "the loser's version does not begin where the winner's ended, so it closed "
        "something other than the version that won"
    )

    # The loser's confidence is the greater of its own submission and the winner's,
    # which the closing statement can only have returned by reading the winner's row.
    assert loser.confidence == pytest.approx(
        max(WRITER_CONFIDENCES[loser_outcome.worker], winner.confidence)
    )
    assert loser.confidence >= winner.confidence

    # The two Events record the two supersessions in the order they committed, so the
    # episodic record carries the same chain the versions do.
    assert [payload["superseded_version_id"] for payload in race.payloads] == [
        str(stored_first),
        str(winner.version_id),
    ]
    assert [payload["superseding_version_id"] for payload in race.payloads] == [
        str(winner.version_id),
        str(loser.version_id),
    ]
