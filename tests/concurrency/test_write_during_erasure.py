"""A binding write against an in-flight erasure: refused by name, or ordered against it.

**Validates: Requirements 15.3, 36.4**

A Client_Binding write is the one write an erasure run cannot tolerate arriving
underneath it: an Artifact bound to the erased Client after the sweep has selected
its set, but before the certificate is assembled, would sit in a window the
certificate accounts for nothing in. The guard is one statement at the front of the
write's own transaction, reading `erasure_run` for that Client with the in-flight
status, and this module asserts the two halves of what that buys.

**A run already on the record refuses the write by name.** The run row is committed
before the write is attempted, so nothing races: the guard read finds it, the
refusal is raised before any write statement is sent, and the Artifact's binding is
absent afterwards rather than present and unaccounted for. The refusal is measured,
because the acceptance criterion attaches a counter to it, and it is the domain
failure rather than a serialization abort, because an operator reading it needs to
learn that a run was already running and not that two transactions collided. The
same write with no run in flight commits, which is what keeps the refusal a
statement about erasure rather than about the write path being broken.

**A run starting underneath the write leaves only admissible outcomes.** The second
test drives the genuine race: one connection performs the binding write through the
guard and waits, inside its transaction, after the guard read and before the insert;
another connection inserts the in-flight run row while it waits. Because the guard
read joined the writing transaction's read set, the run's insert writes into that
read set and SERIALIZABLE has to order the two. Three outcomes are therefore
correct, and the cluster may reach any of them:

* the writing transaction is aborted as a serialization failure, and nothing of the
  binding is committed;
* the wrapper retries it, the retry's guard read now finds the run, and the write is
  refused with the domain failure and nothing committed;
* the writing transaction is ordered first, in which case the binding committed
  before the run's sweep could have selected anything, and the sweep finds it and
  records a Disposition for it.

Which of the three a given run reaches depends on which transaction the cluster
chooses to abort, so asserting any single one would be asserting a timing
coincidence. What is asserted is the disjunction, that exactly one branch holds,
and that the branch which holds is consistent in the database: refused or aborted
means no binding row at all, and committed means a binding row that the run's sweep
turns into a Disposition. The branch reached is reported in the failure message of
the exclusivity assertion, so a run that keeps reaching one branch is legible from
the output rather than invisible.

The third branch is the one both arrangements here reach in practice, and that is
worth stating rather than hiding: a guard read that saw no run, followed by a run
that commits afterwards, is a serializable order, so the cluster has no reason to
abort either party even when the run's insert commits while the writing transaction
is still open. The disjunction is therefore asserted twice under two different pinned
orderings, and never a single branch, because whether the refusing branches are
reached is the cluster's choice.

**The interleaving is arranged rather than hoped for.** A barrier sits inside the
writing transaction between the guard read and the insert, so the run's insert
cannot land before the read it must conflict with, and no test here sleeps to
arrange an ordering. The barrier admits each side once, so a retried body passes
straight through it rather than waiting for a party that has already finished.

**The run row is placed by statements of this module.** Requesting an erasure,
starting a run, and sweeping a Client's bindings into Dispositions belong to the
erasure lifecycle rather than to the guard. What the guard needs from a run is that
one exists with the in-flight status, and what the third branch needs is that a
sweep would have found the binding, so both are placed directly and parameterised
in full.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.errors import ErasureInFlightError, SerializationExhaustedError
from molt.store import Connection, Cursor, MemoryStore
from molt.store.fencing import (
    ACTIVE_RUN_QUERY,
    ERASURE_IN_FLIGHT_METRIC,
    RUNNING_STATUS,
    ActiveRun,
    guarded_binding_write,
    select_active_run,
)
from molt.store.migrate import apply_migrations
from molt.store.retry import is_serialization_failure
from molt.telemetry import Telemetry, configure, current, reset

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

# The bound parameter form of a search path change, so a schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows a scenario is built from. This module owns no Client insert, no erasure
# request, and no run start, so each is placed here rather than through the module
# under test, and the status the run carries is bound rather than defaulted so the
# in-flight state is stated by the test that wants it.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_REQUEST: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification) VALUES (%s, %s, %s, %s)"
)
INSERT_RUN: Final[str] = (
    "INSERT INTO erasure_run (id, request_id, client_id, requester, status, phase, t_before) "
    "VALUES (%s, %s, %s, %s, %s, %s, now())"
)

# The binding write every guarded body here performs, and the read the assertions
# about persistence are made from.
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, confidence) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
COUNT_BINDINGS: Final[str] = (
    "SELECT count(*) FROM client_binding WHERE client_id = %s AND artifact_id = %s"
)

# The sweep, as the third branch of the disjunction needs it: the bindings a run
# would select for the erased Client, and the Disposition each selected Artifact
# earns. Only the two statements are this module's; the real sweep belongs to the
# erasure engine.
SELECT_BOUND_ARTIFACTS: Final[str] = (
    "SELECT artifact_id, artifact_kind FROM client_binding "
    "WHERE client_id = %s ORDER BY artifact_id"
)
INSERT_DISPOSITION: Final[str] = (
    "INSERT INTO disposition (run_id, artifact_id, artifact_kind, disposition, reason, "
    "selection_reason) VALUES (%s, %s, %s, %s, %s, %s)"
)
SELECT_DISPOSED_ARTIFACTS: Final[str] = (
    "SELECT artifact_id FROM disposition WHERE run_id = %s ORDER BY artifact_id"
)

# The values the placed rows carry. None is what an assertion turns on, so each is
# fixed here rather than varied per example.
JURISDICTION: Final[str] = "eu"
REQUESTER: Final[str] = "operator"
JUSTIFICATION: Final[str] = "a governed request"
ARTIFACT_KIND: Final[str] = "derived_artifact"
BINDING_METHOD: Final[str] = "marker"
BINDING_CONFIDENCE: Final[float] = 0.75
SWEEP_PHASE: Final[str] = "sweep"
COMPLETED_STATUS: Final[str] = "completed"
DISPOSITION_KIND: Final[str] = "surgical_redaction"
DISPOSITION_REASON: Final[str] = "bound to the erased Client"
SELECTION_REASON: Final[str] = "client_binding"

# How long either side of the race waits for the other to reach the barrier.
# Generous against a slow instance, and bounded so a side that will never be joined
# reports a broken barrier instead of holding the suite open.
GATE_TIMEOUT_SECONDS: Final[float] = 30.0

# How many parties the race barrier holds: the writing transaction and the run that
# starts underneath it.
RACERS: Final[int] = 2

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


class ArriveOnce:
    """Holds each side of the race until both have arrived, admitting each once.

    The barrier is what turns the collision from a hope into a fact: the writing
    transaction arrives after its guard read and before its insert, so the run's
    insert cannot land ahead of the read it has to conflict with.

    Each side is admitted once. A retried transaction body therefore passes straight
    through rather than waiting for a side that has already finished, which is what
    keeps the retry the shipped one instead of a second synchronised round.
    """

    def __init__(self, parties: int, *, timeout: float) -> None:
        self._barrier = threading.Barrier(parties)
        self._timeout = timeout
        self._lock = threading.Lock()
        self._admitted: set[str] = set()

    def arrive(self, side: str) -> bool:
        """Wait for the other side if this side has not waited yet.

        Returns whether this call waited, so a test can assert that both sides were
        held rather than that the barrier happened to be open.
        """
        with self._lock:
            if side in self._admitted:
                return False
            self._admitted.add(side)
        self._barrier.wait(timeout=self._timeout)
        return True


@dataclass(frozen=True, slots=True)
class Scenario:
    """One tenant, one erasure request, and the run identifier a test will use."""

    client_id: UUID
    request_id: UUID
    run_id: UUID


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and a connection factory."""

    store: MemoryStore
    connection: DriverConnection
    open_connection: Callable[[], DriverConnection]

    def scenario(self) -> Scenario:
        """Place a tenant and an erasure request, and name the run to come.

        Each scenario carries its own tenant, so the guard read of one example never
        matches a run another example started.
        """
        client_id = uuid4()
        request_id = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (client_id, f"tenant-{client_id.hex[:12]}", "Tenant", JURISDICTION),
        )
        send(self.connection, INSERT_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION))
        return Scenario(client_id=client_id, request_id=request_id, run_id=uuid4())

    def start_run(
        self,
        scenario: Scenario,
        *,
        status: str = RUNNING_STATUS,
        connection: DriverConnection | None = None,
    ) -> None:
        """Record one run for the tenant, with the status the caller states."""
        target = self.connection if connection is None else connection
        send(
            target,
            INSERT_RUN,
            (
                scenario.run_id,
                scenario.request_id,
                scenario.client_id,
                REQUESTER,
                status,
                SWEEP_PHASE,
            ),
        )

    def bindings(self, client_id: UUID, artifact_id: UUID) -> int:
        """How many binding rows the tenant now holds for one Artifact."""
        with self.connection.cursor() as cursor:
            cursor.execute(COUNT_BINDINGS, (client_id, artifact_id))
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def sweep(self, scenario: Scenario) -> tuple[UUID, ...]:
        """Record a Disposition for every Artifact bound to the erased tenant.

        This stands in for the run's own sweep, which is what the third branch of
        the disjunction is about: a binding that committed before the run started is
        a binding the sweep selects, so the Artifact appears in the run's evidence
        rather than in a gap.
        """
        with self.connection.cursor() as cursor:
            cursor.execute(SELECT_BOUND_ARTIFACTS, (scenario.client_id,))
            selected = [(as_uuid(row[0]), str(row[1])) for row in cursor.fetchall()]
        for artifact_id, artifact_kind in selected:
            send(
                self.connection,
                INSERT_DISPOSITION,
                (
                    scenario.run_id,
                    artifact_id,
                    artifact_kind,
                    DISPOSITION_KIND,
                    DISPOSITION_REASON,
                    SELECTION_REASON,
                ),
            )
        return self.disposed(scenario.run_id)

    def disposed(self, run_id: UUID) -> tuple[UUID, ...]:
        """The Artifacts one run recorded a Disposition for."""
        with self.connection.cursor() as cursor:
            cursor.execute(SELECT_DISPOSED_ARTIFACTS, (run_id,))
            return tuple(as_uuid(row[0]) for row in cursor.fetchall())

    def run_exists(self, scenario: Scenario) -> bool:
        """Whether the in-flight run row is on the record, read outside any write."""
        with self.connection.cursor() as cursor:
            cursor.execute(ACTIVE_RUN_QUERY, (scenario.client_id, RUNNING_STATUS))
            return cursor.fetchone() is not None


def as_uuid(value: object) -> UUID:
    """Narrow a stored identifier column, refusing anything else."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on a caller's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def binding_body(
    client_id: UUID,
    artifact_id: UUID,
    *,
    before_write: Callable[[], None] | None = None,
) -> Callable[[Cursor], UUID]:
    """A body writing one binding, optionally letting a contender in first."""

    def body(cursor: Cursor) -> UUID:
        if before_write is not None:
            before_write()
        cursor.execute(
            INSERT_BINDING,
            (uuid4(), artifact_id, ARTIFACT_KIND, client_id, BINDING_METHOD, BINDING_CONFIDENCE),
        )
        return artifact_id

    return body


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store bound to that schema.

    Every migration is applied because the partial index the guard read seeks on and
    the restricting references the evidence tables carry both arrive after the
    tables that hold them. The pool, the isolation level, and the retry policy are
    all the shipped ones, since the claim is about what the shipped write path does
    under contention.
    """
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    def open_connection() -> DriverConnection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        return opened

    def connect_with() -> Connection:
        connection: Connection = cast(Connection, open_connection())
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(
            store=store,
            connection=fresh_schema,
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
    """The process-wide telemetry instance the guard emitted through."""
    return current()


def refusals_counted() -> float:
    """How many in-flight refusals the process-wide instance has counted."""
    return instance().counters().get((ERASURE_IN_FLIGHT_METRIC, ()), 0.0)


# ---------------------------------------------------------------------------
# The guard read refuses a write against a run already on the record
# ---------------------------------------------------------------------------


def test_a_running_erasure_refuses_the_binding_write(
    cluster: Cluster,
    telemetry_sink: io.StringIO,
) -> None:
    """The named domain failure, no binding row, and the refusal counted once."""
    scenario = cluster.scenario()
    cluster.start_run(scenario)
    artifact_id = uuid4()
    before = refusals_counted()

    with pytest.raises(ErasureInFlightError) as raised:
        guarded_binding_write(
            cluster.store,
            scenario.client_id,
            binding_body(scenario.client_id, artifact_id),
        )

    # The refusal names the run the write would have slipped past, so an operator
    # learns which run refused it rather than only that something did.
    notes = "\n".join(raised.value.__notes__)
    assert str(scenario.run_id) in notes
    assert SWEEP_PHASE in notes

    # Nothing was persisted: the guard raises ahead of the insert, so the abandoned
    # transaction had nothing to discard.
    assert cluster.bindings(scenario.client_id, artifact_id) == 0

    # And the refusal was measured, undimensioned, exactly once.
    assert refusals_counted() == before + 1.0
    assert (ERASURE_IN_FLIGHT_METRIC, ()) in instance().counters()
    assert str(scenario.run_id) in telemetry_sink.getvalue()


def test_the_guard_read_reports_the_run_it_refuses_for(cluster: Cluster) -> None:
    """The reading the guard turns on is the run row the erasure lifecycle wrote."""
    scenario = cluster.scenario()
    cluster.start_run(scenario)

    def body(cursor: Cursor) -> ActiveRun | None:
        return select_active_run(cursor, scenario.client_id)

    active = cluster.store.read(body)

    assert active is not None
    assert active.run_id == scenario.run_id
    assert active.phase == SWEEP_PHASE


def test_no_run_in_flight_admits_the_binding_write(cluster: Cluster) -> None:
    """With nothing in flight the same write commits, so the refusal is about erasure."""
    scenario = cluster.scenario()
    artifact_id = uuid4()

    written = guarded_binding_write(
        cluster.store,
        scenario.client_id,
        binding_body(scenario.client_id, artifact_id),
    )

    assert written == artifact_id
    assert cluster.bindings(scenario.client_id, artifact_id) == 1


def test_a_completed_run_admits_the_binding_write(cluster: Cluster) -> None:
    """A run that is over refuses nothing: the guard turns on the in-flight status."""
    scenario = cluster.scenario()
    cluster.start_run(scenario, status=COMPLETED_STATUS)
    artifact_id = uuid4()

    guarded_binding_write(
        cluster.store,
        scenario.client_id,
        binding_body(scenario.client_id, artifact_id),
    )

    assert cluster.bindings(scenario.client_id, artifact_id) == 1


# ---------------------------------------------------------------------------
# A run starting underneath the write
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RaceOutcome:
    """One settled race: what each side did, and what the database holds after.

    Attributes:
        artifact_id: The Artifact whose binding was raced.
        attempts: How many times the guarded body ran, so a retry is visible.
        held: Whether both sides were held at the barrier, which is what makes the
            collision a fact rather than a hope.
        refused: The domain refusal the write ended with, or None.
        aborted: The serialization failure the write ended with, or None.
        committed: Whether the binding write returned rather than failing.
        run_started: Whether the run's own insert committed on its first attempt.
        run_conflict: Whether the run's insert was the transaction the cluster
            aborted.
    """

    artifact_id: UUID
    attempts: int
    held: bool
    refused: ErasureInFlightError | None
    aborted: BaseException | None
    committed: bool
    run_started: bool
    run_conflict: bool


def race_a_starting_run(cluster: Cluster, scenario: Scenario) -> RaceOutcome:
    """Write a binding through the guard while the run starts underneath it.

    The writing transaction arrives at the barrier after its guard read and before
    its insert, so the run's insert writes into a read set that is already held. The
    failure each side reaches is carried back rather than raised in its own thread,
    so the assertions see both sides instead of only the first to fail.
    """
    gate = ArriveOnce(RACERS, timeout=GATE_TIMEOUT_SECONDS)
    artifact_id = uuid4()
    attempts: list[int] = []
    held: list[bool] = []
    refused: list[ErasureInFlightError] = []
    aborted: list[BaseException] = []
    committed: list[UUID] = []
    run_started: list[bool] = []
    run_conflict: list[bool] = []

    def write() -> None:
        def before_write() -> None:
            attempts.append(len(attempts) + 1)
            held.append(gate.arrive("writer"))

        try:
            committed.append(
                guarded_binding_write(
                    cluster.store,
                    scenario.client_id,
                    binding_body(scenario.client_id, artifact_id, before_write=before_write),
                )
            )
        except ErasureInFlightError as refusal:
            refused.append(refusal)
        except Exception as error:  # carried back rather than raised in this thread
            aborted.append(error)

    def start() -> None:
        connection = cluster.open_connection()
        try:
            gate.arrive("runner")
            cluster.start_run(scenario, connection=connection)
            run_started.append(True)
        except Exception as error:
            run_conflict.append(is_serialization_failure(error))
        finally:
            connection.close()

    threads = [
        threading.Thread(target=write, name="binding-writer"),
        threading.Thread(target=start, name="run-starter"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return RaceOutcome(
        artifact_id=artifact_id,
        attempts=len(attempts),
        held=any(held),
        refused=refused[0] if refused else None,
        aborted=aborted[0] if aborted else None,
        committed=bool(committed),
        run_started=bool(run_started),
        run_conflict=bool(run_conflict) and run_conflict[0],
    )


def interleave_a_starting_run(cluster: Cluster, scenario: Scenario) -> RaceOutcome:
    """Commit the run's insert inside the open guarded transaction, before its write.

    This is the same collision as the threaded race with the ordering pinned rather
    than left to the schedule: the guard read has already happened on the writing
    transaction's cursor, and the run's insert commits on a connection of its own
    while that transaction is still open and has written nothing. The writing
    transaction can therefore not be ordered before the run, which is what drives the
    two refusing branches the threaded form reaches only sometimes.

    The insert is sent once, so the retried body does not start a second run.
    """
    artifact_id = uuid4()
    attempts: list[int] = []
    refused: list[ErasureInFlightError] = []
    aborted: list[BaseException] = []
    committed: list[UUID] = []
    started: list[bool] = []
    connection = cluster.open_connection()

    def before_write() -> None:
        attempts.append(len(attempts) + 1)
        if not started:
            cluster.start_run(scenario, connection=connection)
            started.append(True)

    try:
        try:
            committed.append(
                guarded_binding_write(
                    cluster.store,
                    scenario.client_id,
                    binding_body(scenario.client_id, artifact_id, before_write=before_write),
                )
            )
        except ErasureInFlightError as refusal:
            refused.append(refusal)
        except Exception as error:  # carried back so the assertions see the branch
            aborted.append(error)
    finally:
        connection.close()

    return RaceOutcome(
        artifact_id=artifact_id,
        attempts=len(attempts),
        held=True,
        refused=refused[0] if refused else None,
        aborted=aborted[0] if aborted else None,
        committed=bool(committed),
        run_started=bool(started),
        run_conflict=False,
    )


def branch_of(outcome: RaceOutcome) -> str:
    """Name the branch of the disjunction this race reached."""
    if outcome.refused is not None:
        return "refused with the domain failure"
    if outcome.aborted is not None:
        return f"aborted with {type(outcome.aborted).__name__}: {outcome.aborted}"
    return "committed before the run started"


def assert_only_admissible(cluster: Cluster, scenario: Scenario, outcome: RaceOutcome) -> None:
    """Assert the disjunction, and check the branch reached against what is stored.

    The three branches are all serializable orderings of the two transactions, so
    which one is reached is the cluster's choice rather than a test's. Exactly one of
    them holds, and whichever holds is checked against what the database holds
    afterwards: a write that did not land left no binding row at all, and a write
    that landed is selected by the run's sweep and appears in its Dispositions.
    """
    # The collision is a fact: the writing transaction was held after its guard read
    # and before its insert, so the run's insert could not precede that read.
    assert outcome.held, (
        "the writing transaction passed the barrier without waiting, so the run's "
        "insert did not necessarily land inside its read set and the outcome below "
        "says nothing"
    )

    # Exactly one branch, and the branch is named in the message so a run that keeps
    # reaching one of them is legible from the output.
    reached = [outcome.refused is not None, outcome.aborted is not None, outcome.committed]
    assert sum(reached) == 1, (
        f"the race reached {sum(reached)} outcomes at once, which is none of the "
        f"three the guard admits; it {branch_of(outcome)}"
    )

    # A write that ended in the domain refusal was refused by a re-read that found
    # the run, which is only possible after the wrapper retried it.
    if outcome.refused is not None:
        assert outcome.attempts > 1, (
            "the write was refused on its first attempt, so it never raced the run "
            "at all and this is the already-recorded case rather than the race"
        )
        assert str(scenario.run_id) in "\n".join(outcome.refused.__notes__)

    # Neither failing branch persisted anything, whichever of the two it was.
    if not outcome.committed:
        assert cluster.bindings(scenario.client_id, outcome.artifact_id) == 0, (
            f"the write {branch_of(outcome)} and yet its binding is stored, which is "
            "the unaccounted-for Artifact the guard exists to prevent"
        )
        if outcome.aborted is not None:
            assert is_serialization_failure(outcome.aborted) or isinstance(
                outcome.aborted, SerializationExhaustedError
            ), f"the write failed for a reason the guard does not admit: {outcome.aborted}"
        return

    # The committing branch: the binding is stored, and it was ordered before the
    # run, so the run's sweep selects it and the Artifact appears in the evidence
    # rather than in a window the certificate cannot account for.
    assert cluster.bindings(scenario.client_id, outcome.artifact_id) == 1
    if not outcome.run_started:
        # The cluster aborted the run's insert rather than the write. The run is the
        # one that retries, exactly as the wrapper would retry it, and the sweep it
        # then performs is the sweep the binding has to appear in.
        assert outcome.run_conflict, (
            "the run's insert failed for a reason other than the conflict it was "
            "supposed to have with the guard read"
        )
        cluster.start_run(scenario)
    assert cluster.run_exists(scenario)
    assert outcome.artifact_id in cluster.sweep(scenario)


def test_a_run_starting_underneath_the_write_leaves_only_admissible_outcomes(
    cluster: Cluster,
) -> None:
    """Two threads, one barrier: whichever way the cluster orders them is admissible."""
    scenario = cluster.scenario()

    assert_only_admissible(cluster, scenario, race_a_starting_run(cluster, scenario))


def test_a_run_committed_inside_the_open_transaction_leaves_only_admissible_outcomes(
    cluster: Cluster,
) -> None:
    """The same disjunction with the run's commit pinned inside the open transaction.

    Wall order is not serialization order, and this is where that distinction earns
    its keep. The run's insert commits after the guard read and before the writing
    transaction commits, and the writing transaction may still commit: its guard read
    took a timestamp the run's insert is ordered after, and the binding insert
    touches nothing the run's insert touched, so the cluster can order the write first
    without pushing its timestamp and has no reason to abort either party. That
    outcome is the third branch, not a hole: the run's own sweep runs after its insert
    committed, so it observes the binding and disposes of it.

    So this form is not a way to force the refusing branches; it is the arrangement
    under which a check taken outside the write's transaction would be indistinguishable
    from one taken inside it, and the disjunction is asserted here too rather than any
    single branch.
    """
    scenario = cluster.scenario()

    assert_only_admissible(cluster, scenario, interleave_a_starting_run(cluster, scenario))
