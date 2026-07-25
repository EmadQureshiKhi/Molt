"""The attribution history against a live instance.

The unit module of the same concern asserts the shape of the statements and the
order they go out in. This one asserts the six things only a cluster can answer,
because each rests on the cluster's own behaviour rather than on the module's text.

The closure, the successor, and the Ledger Event commit together. A supersession
whose Event the cluster refuses leaves the prior version current and writes no
successor, which is the claim that makes the history part of the episodic record
rather than merely accompanied by it.

The partial uniqueness admits a history while keeping one current version. Several
supersessions of one Artifact and Client pair accumulate rows without limit, and at
every point exactly one of them carries no superseding reference, so the two facts
the governance claim needs are the database's rather than the writing code's.

The greater-confidence rule really reads the closed version's own value. A
submission carrying a different method and a lower confidence produces a successor
holding the confidence that was closed, because `greatest` is evaluated by the
cluster over the value the closing statement returned.

The as-of query is half-open at the supersession instant. The instant a
supersession happened returns the successor and not its predecessor, which is what
makes an answer one version per Client rather than two.

The earliest-version query answers what the certificate records. The first
validity start for the pair and the method of that first version, read before any
disposition runs.

A removal is a closure. The Client stops appearing in the current attribution, no
row is deleted, the prior version is closed naming a terminal marker, and that
marker's validity interval is empty, so the history says the claim was withdrawn
rather than that it never existed.

Every migration is applied, because the partial unique index, the total closure
check, the ordered-interval check, and the supersession Event category all arrive
with the attribution migration, and the self-referencing constraint that would
refuse the ordered pair of statements is dropped by the protection migration.

**Validates: Requirements 12.7, 43.1, 43.3, 43.4, 43.5, 43.7, 43.8**
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.models.artifact import ArtifactKind
from molt.models.binding import BindingMethod
from molt.models.event import EventCategory
from molt.store import Connection, Cursor, MemoryStore
from molt.store.attribution import (
    AttributionOutcome,
    AttributionSubmission,
    SupersessionContext,
    attribution_as_of,
    current_attribution,
    first_attributions,
    record_attribution,
    remove_attribution,
    write_attribution,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The direct writes and reads this module makes. The module under test owns no
# tenant insert and no Session insert, and the history is read back column by
# column rather than through the surface that wrote it, so a claim about stored
# state does not rest on the same code that produced it.
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
COUNT_VERSIONS: Final[str] = (
    "SELECT count(*) FROM client_binding WHERE artifact_id = %s AND client_id = %s"
)
COUNT_CURRENT: Final[str] = (
    "SELECT count(*) FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NULL"
)
SELECT_EVENTS: Final[str] = (
    "SELECT id, category, payload, occurred_at FROM ledger WHERE session_id = %s ORDER BY seq ASC"
)

# The command and the machine every supersession Event of this module records.
AGENT_CLI: Final[str] = "molt"
MACHINE_ID: Final[str] = "integration-machine"

# The confidences the cases use, each distinct so no expectation is satisfied by
# another's value.
FIRST_CONFIDENCE: Final[float] = 0.5
STRONGER_CONFIDENCE: Final[float] = 0.8
STRONGEST_CONFIDENCE: Final[float] = 0.95
WEAKER_CONFIDENCE: Final[float] = 0.2

# An instant with an offset, fixed so no example depends on when it ran. It is a
# detection reading rather than a validity reading: the validity interval is the
# cluster's own, and every instant this module compares against is read back from
# a stored row.
DETECTED_AT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# How far before a stored validity start an as-of read is taken, so the answer is
# *nothing was attributed yet* rather than a boundary case.
BEFORE_SECONDS: Final[float] = 60.0

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


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


def scalar(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> object:
    """Read one column of one row on the fixture's own connection."""
    rows = rows_of(connection, statement, params)
    assert rows, "the statement should have produced one row"
    return rows[0][0]


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

    def versions(self, artifact_id: UUID, client_id: UUID) -> tuple[StoredVersion, ...]:
        """Every stored version of one pair, oldest first."""
        return tuple(
            StoredVersion(
                id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
                method=str(row[1]),
                confidence=float(str(row[2])),
                valid_from=_moment(row[3]),
                valid_to=None if row[4] is None else _moment(row[4]),
                superseded_by=None if row[5] is None else UUID(str(row[5])),
            )
            for row in rows_of(self.connection, SELECT_VERSIONS, (artifact_id, client_id))
        )

    def counts(self, artifact_id: UUID, client_id: UUID) -> tuple[int, int]:
        """How many versions the pair holds, and how many of them are current."""
        return (
            int(str(scalar(self.connection, COUNT_VERSIONS, (artifact_id, client_id)))),
            int(str(scalar(self.connection, COUNT_CURRENT, (artifact_id, client_id)))),
        )

    def events(self, session_id: UUID) -> list[tuple[object, ...]]:
        """Every Ledger row of one Session, in sequence order."""
        return rows_of(self.connection, SELECT_EVENTS, (session_id,))


def _moment(value: object) -> datetime:
    """Narrow a stored timestamp, refusing anything else."""
    assert isinstance(value, datetime), "a validity column holds an instant"
    return value


def submission(
    artifact_id: UUID,
    client_id: UUID,
    *,
    method: BindingMethod = BindingMethod.SCOPE,
    confidence: float = FIRST_CONFIDENCE,
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


# ---------------------------------------------------------------------------
# The first write and the supersession
# ---------------------------------------------------------------------------


def test_a_first_write_is_one_current_version_and_no_event(cluster: Cluster) -> None:
    """A first attribution supersedes nothing, so it records no supersession."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()

    written = write_attribution(
        cluster.store,
        submission(artifact_id, owner),
        context=cluster.context(session_id),
    )

    assert written.outcome is AttributionOutcome.INSERTED
    assert cluster.counts(artifact_id, owner) == (1, 1)
    assert cluster.events(session_id) == []
    stored = cluster.versions(artifact_id, owner)
    assert stored[0].valid_to is None
    assert stored[0].superseded_by is None
    assert current_attribution(cluster.store, artifact_id)[0].id == written.version_id


def test_a_supersession_closes_inserts_and_records_in_one_transaction(cluster: Cluster) -> None:
    """The predecessor names its successor, and one Event names them both.

    The closing statement wrote a superseding reference to a row that did not exist
    when it ran, which is the whole reason that column carries no reference. What
    makes the reference real is the transaction: both rows and the Event are read
    back here after one commit.
    """
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    first = write_attribution(cluster.store, submission(artifact_id, owner), context=context)
    second = write_attribution(
        cluster.store,
        submission(artifact_id, owner, confidence=STRONGER_CONFIDENCE),
        context=context,
    )

    assert second.outcome is AttributionOutcome.SUPERSEDED
    assert second.superseded_id == first.version_id
    stored = cluster.versions(artifact_id, owner)
    assert len(stored) == 2
    assert stored[0].id == first.version_id
    assert stored[0].superseded_by == second.version_id
    assert stored[0].valid_to is not None
    assert stored[1].id == second.version_id
    assert stored[1].current
    assert stored[1].valid_from == stored[0].valid_to, "the interval is contiguous at the closure"

    events = cluster.events(session_id)
    assert len(events) == 1
    assert events[0][1] == EventCategory.ATTRIBUTION_SUPERSEDED.value
    payload = events[0][2]
    assert isinstance(payload, dict)
    assert payload["artifact_id"] == str(artifact_id)
    assert payload["client_id"] == str(owner)
    assert payload["superseded_version_id"] == str(first.version_id)
    assert payload["superseding_version_id"] == str(second.version_id)
    assert second.event_id is not None
    assert events[0][0] == second.event_id


def test_a_refused_event_leaves_the_prior_version_current(cluster: Cluster) -> None:
    """The Event is part of the supersession, so its refusal undoes both writes.

    The Event names a Session no row holds, which reaches the reference on the
    Ledger's Session column. Both attribution statements have already succeeded
    when it arrives, so what the cluster discards is a closure and an insert that
    were about to become the history.
    """
    owner = cluster.tenant()
    artifact_id = uuid4()
    first = write_attribution(
        cluster.store,
        submission(artifact_id, owner),
        context=cluster.context(cluster.session(owner)),
    )

    with pytest.raises(Exception, match="foreign key"):
        write_attribution(
            cluster.store,
            submission(artifact_id, owner, confidence=STRONGER_CONFIDENCE),
            context=cluster.context(uuid4()),
        )

    assert cluster.counts(artifact_id, owner) == (1, 1)
    stored = cluster.versions(artifact_id, owner)
    assert stored[0].id == first.version_id
    assert stored[0].current
    assert stored[0].confidence == pytest.approx(FIRST_CONFIDENCE)


def test_a_binding_written_in_the_artifact_transaction_lands_with_it(cluster: Cluster) -> None:
    """The cursor form composes into a caller's transaction rather than framing one."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    def body(cursor: Cursor) -> tuple[UUID, UUID]:
        first = record_attribution(cursor, submission(artifact_id, owner), context=context)
        second = record_attribution(
            cursor,
            submission(artifact_id, owner, confidence=STRONGER_CONFIDENCE),
            context=context,
        )
        return first.version_id, second.version_id

    first_id, second_id = cluster.store.in_serializable(body)

    assert cluster.counts(artifact_id, owner) == (2, 1)
    stored = cluster.versions(artifact_id, owner)
    assert stored[0].id == first_id
    assert stored[0].superseded_by == second_id
    assert len(cluster.events(session_id)) == 1


# ---------------------------------------------------------------------------
# The partial uniqueness and the confidence rule
# ---------------------------------------------------------------------------


def test_a_history_accumulates_while_one_version_stays_current(cluster: Cluster) -> None:
    """Many rows for one pair, and exactly one of them current at every point."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    for confidence in (FIRST_CONFIDENCE, STRONGER_CONFIDENCE, STRONGEST_CONFIDENCE):
        write_attribution(
            cluster.store,
            submission(artifact_id, owner, confidence=confidence),
            context=context,
        )
        assert cluster.counts(artifact_id, owner)[1] == 1, "one current version at every point"

    stored = cluster.versions(artifact_id, owner)
    assert len(stored) == 3
    assert [version.current for version in stored] == [False, False, True]
    assert stored[-1].confidence == pytest.approx(STRONGEST_CONFIDENCE)
    assert len(cluster.events(session_id)) == 2, "one Event per supersession, none for the first"


def test_a_repeated_write_saying_nothing_new_leaves_the_history_alone(cluster: Cluster) -> None:
    """The current version already holds the maximum submitted confidence."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    first = write_attribution(
        cluster.store,
        submission(artifact_id, owner, confidence=STRONGER_CONFIDENCE),
        context=context,
    )
    repeated = write_attribution(
        cluster.store,
        submission(artifact_id, owner, confidence=STRONGER_CONFIDENCE),
        context=context,
    )

    assert repeated.outcome is AttributionOutcome.UNCHANGED
    assert repeated.version_id == first.version_id
    assert cluster.counts(artifact_id, owner) == (1, 1)
    assert cluster.events(session_id) == []


def test_a_successor_never_carries_less_confidence_than_it_replaced(cluster: Cluster) -> None:
    """`greatest` is evaluated against the confidence the closing statement returned."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    write_attribution(
        cluster.store,
        submission(artifact_id, owner, confidence=STRONGER_CONFIDENCE),
        context=context,
    )
    weaker = write_attribution(
        cluster.store,
        submission(
            artifact_id,
            owner,
            method=BindingMethod.INHERITED,
            confidence=WEAKER_CONFIDENCE,
        ),
        context=context,
    )

    assert weaker.outcome is AttributionOutcome.SUPERSEDED
    assert weaker.confidence == pytest.approx(STRONGER_CONFIDENCE)
    current = current_attribution(cluster.store, artifact_id)
    assert len(current) == 1
    assert current[0].method is BindingMethod.INHERITED, "the method is the submitted one"
    assert current[0].confidence == pytest.approx(STRONGER_CONFIDENCE)


# ---------------------------------------------------------------------------
# The two read forms and the certificate's read
# ---------------------------------------------------------------------------


def test_the_as_of_query_is_half_open_at_the_supersession_instant(cluster: Cluster) -> None:
    """One version per Client at every instant, including the instant of the change."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    first = write_attribution(cluster.store, submission(artifact_id, owner), context=context)
    second = write_attribution(
        cluster.store,
        submission(artifact_id, owner, confidence=STRONGER_CONFIDENCE),
        context=context,
    )
    stored = cluster.versions(artifact_id, owner)
    began = stored[0].valid_from
    changed = stored[1].valid_from

    before = attribution_as_of(
        cluster.store, artifact_id, began - timedelta(seconds=BEFORE_SECONDS)
    )
    at_start = attribution_as_of(cluster.store, artifact_id, began)
    at_change = attribution_as_of(cluster.store, artifact_id, changed)
    afterwards = attribution_as_of(
        cluster.store, artifact_id, changed + timedelta(seconds=BEFORE_SECONDS)
    )

    assert before == ()
    assert [version.id for version in at_start] == [first.version_id]
    assert at_start[0].valid_to == changed
    assert [version.id for version in at_change] == [second.version_id]
    assert [version.id for version in afterwards] == [second.version_id]
    assert afterwards[0].valid_to is None


def test_the_current_query_returns_one_version_per_client(cluster: Cluster) -> None:
    """An Artifact holding two tenants' data reports both, each once, ordered by Client."""
    first_owner = cluster.tenant()
    second_owner = cluster.tenant()
    session_id = cluster.session(first_owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    for owner in (first_owner, second_owner):
        write_attribution(cluster.store, submission(artifact_id, owner), context=context)
    write_attribution(
        cluster.store,
        submission(artifact_id, first_owner, confidence=STRONGEST_CONFIDENCE),
        context=context,
    )

    current = current_attribution(cluster.store, artifact_id)

    assert [version.client_id for version in current] == sorted(
        (first_owner, second_owner), key=str
    )
    assert len(current) == 2, "a closed version is absent from the current form"


def test_the_earliest_version_read_answers_the_certificate(cluster: Cluster) -> None:
    """The first validity start for the pair, and how that first version concluded."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    other_artifact_id = uuid4()
    context = cluster.context(session_id)

    write_attribution(
        cluster.store,
        submission(artifact_id, owner, method=BindingMethod.MARKER),
        context=context,
    )
    write_attribution(
        cluster.store,
        submission(
            artifact_id,
            owner,
            method=BindingMethod.INHERITED,
            confidence=STRONGEST_CONFIDENCE,
        ),
        context=context,
    )
    write_attribution(cluster.store, submission(other_artifact_id, owner), context=context)
    began = cluster.versions(artifact_id, owner)[0].valid_from

    earliest = first_attributions(cluster.store, owner, [artifact_id, other_artifact_id])

    by_artifact = {answer.artifact_id: answer for answer in earliest}
    assert set(by_artifact) == {artifact_id, other_artifact_id}
    assert by_artifact[artifact_id].first_attributed_at == began
    assert by_artifact[artifact_id].first_method is BindingMethod.MARKER, (
        "the earliest version's own method, not the current one's"
    )


# ---------------------------------------------------------------------------
# The removal
# ---------------------------------------------------------------------------


def test_a_removal_closes_the_current_version_rather_than_deleting_it(cluster: Cluster) -> None:
    """The Client leaves every operational read while the history keeps the record."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    first = write_attribution(cluster.store, submission(artifact_id, owner), context=context)
    withdrawn = remove_attribution(cluster.store, artifact_id, owner, context=context)

    assert withdrawn is not None
    assert withdrawn.outcome is AttributionOutcome.WITHDRAWN
    assert withdrawn.superseded_id == first.version_id
    assert cluster.counts(artifact_id, owner) == (2, 0), (
        "nothing was deleted and nothing is current"
    )
    assert current_attribution(cluster.store, artifact_id) == ()

    stored = cluster.versions(artifact_id, owner)
    closed, marker = stored
    assert closed.superseded_by == marker.id
    assert closed.valid_to == marker.valid_from
    assert marker.valid_to == marker.valid_from, "the marker's validity interval is empty"
    assert marker.superseded_by == closed.id, "the marker names the version it terminates"
    assert marker.method == closed.method
    assert attribution_as_of(cluster.store, artifact_id, marker.valid_from) == (), (
        "an empty interval contains no instant, so the marker is returned by neither form"
    )

    events = cluster.events(session_id)
    assert len(events) == 1
    payload = events[0][2]
    assert isinstance(payload, dict)
    assert payload["superseded_version_id"] == str(first.version_id)
    assert payload["superseding_version_id"] == str(marker.id)


def test_removing_a_binding_twice_writes_nothing_the_second_time(cluster: Cluster) -> None:
    """A repeated erasure of the same Artifact is idempotent rather than a failure."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    write_attribution(cluster.store, submission(artifact_id, owner), context=context)
    assert remove_attribution(cluster.store, artifact_id, owner, context=context) is not None

    assert remove_attribution(cluster.store, artifact_id, owner, context=context) is None
    assert cluster.counts(artifact_id, owner) == (2, 0)
    assert len(cluster.events(session_id)) == 1


def test_a_binding_removed_and_detected_again_starts_a_further_version(cluster: Cluster) -> None:
    """A withdrawal leaves the pair open to a first write again, and the history keeps both."""
    owner = cluster.tenant()
    session_id = cluster.session(owner)
    artifact_id = uuid4()
    context = cluster.context(session_id)

    write_attribution(cluster.store, submission(artifact_id, owner), context=context)
    remove_attribution(cluster.store, artifact_id, owner, context=context)
    revived = write_attribution(
        cluster.store,
        submission(artifact_id, owner, confidence=STRONGER_CONFIDENCE),
        context=context,
    )

    assert revived.outcome is AttributionOutcome.INSERTED
    assert cluster.counts(artifact_id, owner) == (3, 1)
    current = current_attribution(cluster.store, artifact_id)
    assert [version.id for version in current] == [revived.version_id]
    assert current[0].confidence == pytest.approx(STRONGER_CONFIDENCE), (
        "a withdrawal ends the chain, so the confidence rule starts from the new claim"
    )
