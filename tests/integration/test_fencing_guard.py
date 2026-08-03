"""The erasure fence against a live instance: admission, refusal, and one transaction.

The unit module asserts the shape of the statements and the order they are sent
in. This module asserts the three things only a cluster can answer.

A write presenting the current generation lands, and the generation it presented
is stored on the evidence, which is what makes the ownership claim on a
certificate checkable against the lease history rather than taken on trust.

A write presenting a superseded generation is refused, the refusal names both
generations, and the evidence table holds nothing from it. The refusal is measured
too, because the acceptance criterion attaches a counter to it.

And the generation read really sits inside the write's transaction. That claim is
the reason the module exists, so it is asserted twice over, at two different
depths. The statements the store sent are read back, so the read is seen to arrive
after the isolation level was set and before the write and the commit, on one
connection inside one explicit transaction. Then the harder version: a takeover
commits on a second connection while the guarded transaction is open, after that
transaction has read the generation and before it commits. Under SERIALIZABLE the
read joined that transaction's read set, so the takeover's write to the row it
matched makes the guarded transaction conflict; the wrapper retries, the retry
re-reads a bumped generation, and the write is refused with nothing persisted. A
guard read taken in an earlier transaction would pass every other assertion here
and commit that row.

The lease rows are placed by parameterised statements of this module rather than
granted through a lease manager, because granting, refusing, renewing, and taking
over an Erasure_Lease belong to the lease lifecycle rather than to the fence. What
this module needs from a lease is that one exists and that its generation can be
superseded, and both are placed directly.

Every migration is applied, because the fencing generation columns on the
evidence tables and the update guard confining which columns of a lease may move
both arrive after the table that holds them.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.errors import LeaseNotHeld, StaleFencingGeneration, StaleFencingGenerationError
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, Cursor, MemoryStore
from molt.store.fencing import (
    CURRENT_GENERATION_QUERY,
    STALE_GENERATION_METRIC,
    CurrentGeneration,
    fenced_certificate,
    fenced_disposition,
    fenced_run_completion,
    select_current_generation,
)
from molt.store.migrate import apply_migrations
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    SERIALIZABLE_STATEMENT,
)
from molt.telemetry import Telemetry, configure, current, reset

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes the fixtures make, parameterised in full. This module owns no
# Client insert, no run insert, and no lease grant, so the rows its scenarios are
# built from are placed here rather than through the module under test.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_REQUEST: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification) VALUES (%s, %s, %s, %s)"
)
INSERT_RUN: Final[str] = (
    "INSERT INTO erasure_run (id, request_id, client_id, requester, t_before) "
    "VALUES (%s, %s, %s, %s, now())"
)
# The interval and the reading are the statement's own literals, so a lease is
# placed with an expiry the cluster computes rather than one a local clock
# supplies. Expiry is not what the fence turns on, but the schema requires the
# window to run forwards.
INSERT_LEASE: Final[str] = (
    "INSERT INTO erasure_lease (id, client_id, owner, generation, idempotency_key, "
    "acquired_at, expires_at) VALUES (%s, %s, %s, %s, %s, now(), now() + INTERVAL '1 hour')"
)
CLOSE_LEASE: Final[str] = (
    "UPDATE erasure_lease SET superseded_at = now(), superseded_by = %s WHERE id = %s"
)

# The evidence write every guarded body here performs, and the two reads the
# assertions are made from. This statement belongs to this module, as the real
# Disposition write belongs to the module that will own it.
INSERT_DISPOSITION: Final[str] = (
    "INSERT INTO disposition (run_id, artifact_id, artifact_kind, disposition, reason, "
    "selection_reason, fencing_generation) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
COUNT_DISPOSITIONS: Final[str] = "SELECT count(*) FROM disposition WHERE run_id = %s"
SELECT_DISPOSITION_GENERATION: Final[str] = (
    "SELECT fencing_generation FROM disposition WHERE run_id = %s AND artifact_id = %s"
)
# The two instants the serialization order of a contended write is read from. Each
# is the cluster's own reading, taken by the transaction that wrote the row, so
# comparing them compares the order the cluster committed the two transactions in.
SELECT_DISPOSITION_INSTANT: Final[str] = (
    "SELECT decided_at FROM disposition WHERE run_id = %s AND artifact_id = %s"
)
SELECT_LEASE_INSTANT: Final[str] = "SELECT acquired_at FROM erasure_lease WHERE id = %s"

# The values the placed rows carry. None of them is what any assertion turns on,
# so each is fixed here rather than varied per example.
JURISDICTION: Final[str] = "eu"
REQUESTER: Final[str] = "operator"
JUSTIFICATION: Final[str] = "a governed request"
ARTIFACT_KIND: Final[str] = "derived_artifact"
DISPOSITION_KIND: Final[str] = "hard_delete"
DISPOSITION_REASON: Final[str] = "selected by the sweep"
SELECTION_REASON: Final[str] = "session_scope"

# The owners of the two generations in every contention scenario.
FIRST_OWNER: Final[str] = "worker-first"
SECOND_OWNER: Final[str] = "worker-second"

# The generation a first grant records, and the one a takeover records after it.
FIRST_GRANT: Final[int] = 1
AFTER_TAKEOVER: Final[int] = 2

# A connection and a cursor are typed loosely because the driver is reached
# through a fixture rather than imported, which keeps this module collectable with
# no driver installed.
DriverConnection = Any
DriverCursor = Any


# ---------------------------------------------------------------------------
# Recording what the store sent
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Recorder:
    """Every statement the store sent, in order, across every connection it used."""

    statements: list[str] = field(default_factory=list)

    @property
    def issued(self) -> list[str]:
        """What the module sent, with the connection surface's own statements removed.

        Establishing the statement timeout, setting the search path, and resetting
        a returned connection belong to the pool rather than to the module under
        test. The reset statement and the wrapper's abandon share their text, so a
        claim about an abandoned transaction is made against the whole list rather
        than against this one.
        """
        return [
            query
            for query in self.statements
            if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT, SEARCH_PATH_STATEMENT)
        ]


class RecordingCursor:
    """A cursor that records each statement and otherwise delegates in full."""

    def __init__(self, inner: DriverCursor, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then send it exactly as it was given."""
        self._recorder.statements.append(query)
        sent: object = self._inner.execute(query, params)
        return sent

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the next row the last statement produced."""
        row: tuple[object, ...] | None = self._inner.fetchone()
        return row

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every remaining row the last statement produced."""
        rows: list[tuple[object, ...]] = self._inner.fetchall()
        return rows

    def close(self) -> None:
        """Release this cursor."""
        self._inner.close()


class RecordingConnection:
    """A connection handing out recording cursors over one shared recorder."""

    def __init__(self, inner: DriverConnection, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder

    @property
    def closed(self) -> bool:
        """Whether the underlying connection can no longer be used."""
        state: bool = bool(self._inner.closed)
        return state

    def cursor(self) -> RecordingCursor:
        """Open a recording cursor over the underlying connection."""
        return RecordingCursor(self._inner.cursor(), self._recorder)

    def close(self) -> None:
        """Close the underlying connection."""
        self._inner.close()


# ---------------------------------------------------------------------------
# The scenario a test runs against
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scenario:
    """One tenant, one run to write evidence for, and one lease over it."""

    client_id: UUID
    run_id: UUID
    lease_id: UUID
    generation: int


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the recorder."""

    store: MemoryStore
    connection: DriverConnection
    recorder: Recorder
    open_connection: Callable[[], DriverConnection]

    def scenario(self, *, leased: bool = True) -> Scenario:
        """Place a tenant, a request, a run, and a first lease, and report them.

        Each scenario carries its own tenant, so the partial uniqueness constraint
        admitting one current lease per tenant never brings two examples into
        contact. A scenario placed unleased holds every row an evidence write needs
        and no lease at all, which is the state a run that never acquired one, or
        released the one it had, is in.
        """
        client_id = uuid4()
        request_id = uuid4()
        run_id = uuid4()
        lease_id = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (client_id, f"tenant-{client_id.hex[:12]}", "Tenant", JURISDICTION),
        )
        send(self.connection, INSERT_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION))
        send(self.connection, INSERT_RUN, (run_id, request_id, client_id, REQUESTER))
        if leased:
            send(
                self.connection,
                INSERT_LEASE,
                (lease_id, client_id, FIRST_OWNER, FIRST_GRANT, f"key-{lease_id.hex[:12]}"),
            )
        return Scenario(
            client_id=client_id,
            run_id=run_id,
            lease_id=lease_id,
            generation=FIRST_GRANT,
        )

    def take_over(self, scenario: Scenario, *, connection: DriverConnection | None = None) -> UUID:
        """Supersede the current lease and grant its successor, in that order.

        The two statements and their order are the lease protocol's, restated here
        because the fence needs a bumped generation to exist and not because this
        module owns the protocol.
        """
        successor = uuid4()
        target = self.connection if connection is None else connection
        send(target, CLOSE_LEASE, (successor, scenario.lease_id))
        send(
            target,
            INSERT_LEASE,
            (
                successor,
                scenario.client_id,
                SECOND_OWNER,
                AFTER_TAKEOVER,
                f"key-{successor.hex[:12]}",
            ),
        )
        return successor

    def dispositions(self, run_id: UUID) -> int:
        """How many disposition rows this run now holds."""
        with self.connection.cursor() as cursor:
            cursor.execute(COUNT_DISPOSITIONS, (run_id,))
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def recorded_generation(self, run_id: UUID, artifact_id: UUID) -> int | None:
        """The generation stored on one disposition, or None where it holds none."""
        with self.connection.cursor() as cursor:
            cursor.execute(SELECT_DISPOSITION_GENERATION, (run_id, artifact_id))
            row = cursor.fetchone()
        assert row is not None
        return None if row[0] is None else int(row[0])

    def decided_at(self, run_id: UUID, artifact_id: UUID) -> datetime:
        """The instant the transaction that wrote one disposition committed at."""
        return self._instant(SELECT_DISPOSITION_INSTANT, (run_id, artifact_id))

    def acquired_at(self, lease_id: UUID) -> datetime:
        """The instant the transaction that granted one lease committed at."""
        return self._instant(SELECT_LEASE_INSTANT, (lease_id,))

    def _instant(self, statement: str, params: tuple[object, ...]) -> datetime:
        """One timestamp column of one row, as the cluster recorded it."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            row = cursor.fetchone()
        assert row is not None
        moment = row[0]
        assert isinstance(moment, datetime)
        return moment


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on a caller's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def disposition_body(
    run_id: UUID,
    artifact_id: UUID,
    generation: int,
    *,
    before_write: Callable[[], None] | None = None,
) -> Callable[[Cursor], UUID]:
    """A body writing one disposition, optionally letting a contender in first."""

    def body(cursor: Cursor) -> UUID:
        if before_write is not None:
            before_write()
        cursor.execute(
            INSERT_DISPOSITION,
            (
                run_id,
                artifact_id,
                ARTIFACT_KIND,
                DISPOSITION_KIND,
                DISPOSITION_REASON,
                SELECTION_REASON,
                generation,
            ),
        )
        return artifact_id

    return body


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store bound to that schema."""
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    recorder = Recorder()

    def open_connection() -> DriverConnection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        return opened

    def connect_with() -> Connection:
        connection: Connection = RecordingConnection(open_connection(), recorder)
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(
            store=store,
            connection=fresh_schema,
            recorder=recorder,
            open_connection=open_connection,
        )


@pytest.fixture
def telemetry_sink() -> Iterator[io.StringIO]:
    """Install a process-wide telemetry instance writing to a sink for one test."""
    sink = io.StringIO()
    configure(Configuration(environ={"MOLT_LOG_LEVEL": "debug"}, file_values={}), stream=sink)
    try:
        yield sink
    finally:
        reset()


def instance() -> Telemetry:
    """The process-wide telemetry instance the module emitted through."""
    return current()


def refusals_counted() -> float:
    """How many supersession refusals the process-wide instance has counted."""
    return instance().counters().get((STALE_GENERATION_METRIC, ()), 0.0)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_a_matching_generation_admits_the_write(cluster: Cluster) -> None:
    """The current owner's evidence lands, carrying the generation it was written under."""
    scenario = cluster.scenario()
    artifact_id = uuid4()

    written = fenced_disposition(
        cluster.store,
        scenario.client_id,
        scenario.generation,
        disposition_body(scenario.run_id, artifact_id, scenario.generation),
    )

    assert written == artifact_id
    assert cluster.dispositions(scenario.run_id) == 1
    assert cluster.recorded_generation(scenario.run_id, artifact_id) == scenario.generation


def test_the_generation_read_is_answered_from_the_lease_history(cluster: Cluster) -> None:
    """The reading is the row the lease protocol wrote, ownership columns included."""
    scenario = cluster.scenario()

    def body(cursor: Cursor) -> CurrentGeneration | None:
        return select_current_generation(cursor, scenario.client_id)

    held = cluster.store.read(body)

    assert held == CurrentGeneration(
        lease_id=scenario.lease_id,
        owner=FIRST_OWNER,
        generation=scenario.generation,
    )


# ---------------------------------------------------------------------------
# The read and the write are one transaction
# ---------------------------------------------------------------------------


def test_the_generation_read_sits_inside_the_writes_transaction(cluster: Cluster) -> None:
    """One connection, one explicit transaction, the read ahead of the write and the commit."""
    scenario = cluster.scenario()
    cluster.recorder.statements.clear()

    fenced_disposition(
        cluster.store,
        scenario.client_id,
        scenario.generation,
        disposition_body(scenario.run_id, uuid4(), scenario.generation),
    )

    issued = cluster.recorder.issued
    assert issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        INSERT_DISPOSITION,
        COMMIT_STATEMENT,
    ]


def test_a_takeover_during_the_transaction_cannot_get_underneath_the_write(
    cluster: Cluster,
) -> None:
    """No committed evidence is ordered after the takeover that superseded its generation.

    The takeover commits on its own connection after the guarded transaction has
    read the generation and before that transaction commits. That is exactly the
    interleaving a check taken in an earlier transaction would admit a stale row
    in, because there the read happened at an instant the write no longer belongs
    to.

    Two outcomes are correct here and the cluster may choose either, since both
    are serializable orderings of the two transactions. Either the guarded write is
    ordered after the takeover, in which case the read that joined its read set is
    invalidated, the wrapper retries, the retry re-reads a bumped generation, and
    the write is refused with nothing persisted. Or the guarded write is ordered
    before the takeover, in which case it committed while its generation was still
    the current one, and its evidence is the then-current owner's. What cannot
    happen, and what this asserts, is a committed row whose transaction is ordered
    after the takeover: the instant the disposition records is compared against the
    instant the successor lease records, and evidence committed under the
    superseded generation after the successor was granted would show up there.
    """
    scenario = cluster.scenario()
    contender = cluster.open_connection()
    artifact_id = uuid4()
    taken_over: list[UUID] = []
    refusals: list[StaleFencingGenerationError] = []

    def contend() -> None:
        if not taken_over:
            taken_over.append(cluster.take_over(scenario, connection=contender))

    try:
        try:
            fenced_disposition(
                cluster.store,
                scenario.client_id,
                scenario.generation,
                disposition_body(
                    scenario.run_id,
                    artifact_id,
                    scenario.generation,
                    before_write=contend,
                ),
            )
        except StaleFencingGeneration as refused:
            refusals.append(refused)
    finally:
        contender.close()

    assert len(taken_over) == 1, "the takeover committed once, inside the guarded transaction"
    if refusals:
        assert refusals[0].presented == scenario.generation
        assert refusals[0].current == AFTER_TAKEOVER
        assert cluster.dispositions(scenario.run_id) == 0
        return
    assert cluster.dispositions(scenario.run_id) == 1
    assert cluster.decided_at(scenario.run_id, artifact_id) < cluster.acquired_at(taken_over[0]), (
        "a committed write under the old generation is ordered before the takeover"
    )


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_a_stale_generation_is_refused_and_nothing_is_written(
    cluster: Cluster,
    telemetry_sink: io.StringIO,
) -> None:
    """A superseded owner learns who holds the run, and its evidence never lands."""
    scenario = cluster.scenario()
    cluster.take_over(scenario)

    with pytest.raises(StaleFencingGeneration) as raised:
        fenced_disposition(
            cluster.store,
            scenario.client_id,
            scenario.generation,
            disposition_body(scenario.run_id, uuid4(), scenario.generation),
        )

    assert raised.value.presented == scenario.generation
    assert raised.value.current == AFTER_TAKEOVER
    assert SECOND_OWNER in "\n".join(raised.value.__notes__)
    assert cluster.dispositions(scenario.run_id) == 0
    assert refusals_counted() == 1.0
    assert SECOND_OWNER in telemetry_sink.getvalue()


@pytest.mark.parametrize("form", [fenced_disposition, fenced_run_completion, fenced_certificate])
def test_no_named_form_admits_a_superseded_owner(
    cluster: Cluster,
    form: Callable[[MemoryStore, UUID, int, Callable[[Cursor], UUID]], UUID],
) -> None:
    """A stale owner can neither record evidence, declare a run finished, nor sign for it."""
    scenario = cluster.scenario()
    cluster.take_over(scenario)

    with pytest.raises(StaleFencingGeneration):
        form(
            cluster.store,
            scenario.client_id,
            scenario.generation,
            disposition_body(scenario.run_id, uuid4(), scenario.generation),
        )

    assert cluster.dispositions(scenario.run_id) == 0


def test_a_write_for_a_client_holding_no_lease_is_refused(cluster: Cluster) -> None:
    """A run that holds no lease writes nothing, and learns that rather than a generation."""
    scenario = cluster.scenario(leased=False)

    with pytest.raises(LeaseNotHeld):
        fenced_disposition(
            cluster.store,
            scenario.client_id,
            scenario.generation,
            disposition_body(scenario.run_id, uuid4(), scenario.generation),
        )

    assert cluster.dispositions(scenario.run_id) == 0


def test_a_later_owner_writes_under_its_own_generation(cluster: Cluster) -> None:
    """The successor is admitted by the generation the takeover recorded for it."""
    scenario = cluster.scenario()
    cluster.take_over(scenario)
    artifact_id = uuid4()

    fenced_disposition(
        cluster.store,
        scenario.client_id,
        AFTER_TAKEOVER,
        disposition_body(scenario.run_id, artifact_id, AFTER_TAKEOVER),
    )

    assert cluster.dispositions(scenario.run_id) == 1
    assert cluster.recorded_generation(scenario.run_id, artifact_id) == AFTER_TAKEOVER
