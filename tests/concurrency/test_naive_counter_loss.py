"""The same counter update written naively loses increments, and is shown losing them.

**Validates: Requirements 15.6, 36.5**

This is the negative control that makes the lossless claim mean something. The
companion module asserts that the shipped increment loses nothing under contention;
on its own that could be read as a property of the workload rather than of the
statement. So the same logical update is written here the way an application
ordinarily writes it — read the value, add one in Python, write the sum back — and
the loss is asserted rather than hoped for.

**Nothing in the source is weakened to produce the loss.** The naive read and the
naive blind write are statements of this module. They are two separate
autocommitted statements, which is exactly what makes them lossy: the value the
write sends was decided outside any transaction that still holds the read, so the
cluster has nothing to conflict on and accepts the stale sum. The shipped increment
is left untouched and is exercised here too, for the contrast.

**The loss is deterministic, and the arrangement that makes it so is stated
plainly.** A lost update needs the reads of two writers to precede the writes of
both, and waiting for that to happen by luck would make this test a coin toss. So
the first test places a barrier between the read and the write: every writer reads,
every writer arrives, and only then does any writer write. Every writer therefore
reads the same starting value and writes the same sum, so the final total is that
starting value plus one no matter how the writes are ordered, and the number of
increments lost is exactly the number of writers less one. The barrier does not
invent an interleaving the cluster would refuse; it selects one of the
interleavings the naive path permits and would otherwise reach only sometimes.

**The unsynchronised form is measured too, and it is measured as a loss.** The
third test removes the barrier and lets writers loop, pausing briefly between the
read and the write the way real application code does while it works. That test
asserts a loss as well, so it fails if the naive path ever stops being lossy; it is
not written to tolerate either outcome. Its determinism is weaker than the staged
form's — it rests on the pause being long enough that concurrent writers overlap
rather than on an ordering guarantee — which is why the staged form is the one
carrying the exact figure.

**The comparison runs the same staging through the shipped statement.** Same writer
count, same barrier, same instant of release, and the shipped increment loses
nothing, because the addition happens inside the cluster where the row is held
rather than in application memory where a stale value can sit. The two figures
side by side are the comparison the verification requirement asks for.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations, discover_migrations
from molt.store.sessions import CounterDelta, bump_session_counters

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

# The staged run: how many writers read the same value before any of them writes.
# Six is enough for the arithmetic to be unmistakable and small enough to be cheap.
STAGED_WRITERS: Final[int] = 6

# The unsynchronised run: how many writers loop, how many read-modify-writes each
# performs, and how long each of them spends deciding the sum. The pause stands in
# for the work an application does between reading a counter and writing it back,
# and it is what makes the writers overlap rather than queue.
LOOPING_WRITERS: Final[int] = 6
LOOPS_PER_WRITER: Final[int] = 8
DECIDE_PAUSE_SECONDS: Final[float] = 0.005

# What one increment moves in either implementation. The naive path can only move
# one counter without becoming a different comparison, so the tool-call counter is
# the one both paths move.
ONE_INCREMENT: Final[CounterDelta] = CounterDelta(tool_calls=1)

# The migration generation this module applies: the tenant table and the Session
# table with its counter columns.
CORE_MIGRATION_VERSION: Final[int] = 1

# The fixture's own statements, including the two that make up the naive path. Every
# value is bound and no identifier is interpolated into any of them.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
READ_COUNTER: Final[str] = "SELECT tool_call_count FROM session WHERE id = %s AND client_id = %s"

# The naive write: a total decided in application memory, sent as a whole value
# rather than as an addition. This is the statement the loss comes from.
WRITE_COUNTER: Final[str] = (
    "UPDATE session SET tool_call_count = %s WHERE id = %s AND client_id = %s"
)

AGENT_CLI: Final[str] = "agent"
MACHINE_ID: Final[str] = "machine"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding the core migration, a store over it, and one tenant.

    The connection factory is exposed as well as the store, because the naive path
    is deliberately not a store operation: it is two autocommitted statements on a
    connection of the writer's own, which is how application code that lost an
    increment was written.
    """

    store: MemoryStore
    connection: DriverConnection
    connect: Callable[[], DriverConnection]
    client_id: UUID


@dataclass(frozen=True, slots=True)
class NaiveOutcome:
    """What one naive writer read, what it wrote back, and what stopped it."""

    worker: int
    read: tuple[int, ...]
    wrote: tuple[int, ...]
    failure: Exception | None


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on a connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def read_counter(connection: DriverConnection, session_id: UUID, client_id: UUID) -> int:
    """Read the tool-call counter in a statement of its own, holding nothing."""
    with connection.cursor() as cursor:
        cursor.execute(READ_COUNTER, (session_id, client_id))
        row = cursor.fetchone()
    assert row is not None, "the naive read matched no Session row"
    return int(row[0])


def stage_core_migration(destination: Path) -> None:
    """Copy the core migration file into a directory of its own."""
    for migration in discover_migrations():
        if migration.version == CORE_MIGRATION_VERSION:
            destination.joinpath(migration.path.name).write_bytes(migration.path.read_bytes())


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Cluster]:
    """Apply the core migration, then build a store and a connection factory over it."""
    directory = tmp_path_factory.mktemp("molt_naive_core")
    stage_core_migration(directory)
    apply_migrations(fresh_schema, directory=directory)

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
        connection: Connection = open_connection()
        return connection

    client_id = uuid4()
    send(fresh_schema, INSERT_CLIENT, (client_id, f"tenant-{client_id.hex[:8]}", "Tenant", "eu"))

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(
            store=store,
            connection=fresh_schema,
            connect=open_connection,
            client_id=client_id,
        )


def new_session(cluster: Cluster) -> UUID:
    """Place one Session row, so each test owns a counter of its own."""
    session_id = uuid4()
    send(cluster.connection, INSERT_SESSION, (session_id, cluster.client_id, AGENT_CLI, MACHINE_ID))
    return session_id


def final_counter(cluster: Cluster, session_id: UUID) -> int:
    """Read the counter the writers left behind, on the fixture's own connection."""
    return read_counter(cluster.connection, session_id, cluster.client_id)


def run_workers(worker: Callable[[int], None], count: int) -> None:
    """Run one function once per worker, at the same time, and wait for all of them."""
    threads = [
        threading.Thread(target=worker, args=(index,), name=f"naive-{index}")
        for index in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def run_staged_naive(cluster: Cluster, session_id: UUID) -> tuple[NaiveOutcome, ...]:
    """Every writer reads, every writer arrives, and only then does anybody write.

    The barrier sits between the read and the write, so the interleaving is chosen
    rather than awaited: this is the ordering under which a read-modify-write is
    known to lose, and running it deliberately is what makes the loss below an exact
    figure instead of a probability.
    """
    written = threading.Barrier(STAGED_WRITERS)
    outcomes: list[NaiveOutcome] = []
    guard = threading.Lock()

    def work(worker: int) -> None:
        failure: Exception | None = None
        seen: list[int] = []
        sent: list[int] = []
        connection = cluster.connect()
        try:
            current = read_counter(connection, session_id, cluster.client_id)
            seen.append(current)
            written.wait()
            send(connection, WRITE_COUNTER, (current + 1, session_id, cluster.client_id))
            sent.append(current + 1)
        except Exception as error:
            failure = error
        finally:
            connection.close()
        with guard:
            outcomes.append(
                NaiveOutcome(
                    worker=worker,
                    read=tuple(seen),
                    wrote=tuple(sent),
                    failure=failure,
                )
            )

    run_workers(work, STAGED_WRITERS)
    return tuple(sorted(outcomes, key=lambda outcome: outcome.worker))


def run_looping_naive(cluster: Cluster, session_id: UUID) -> tuple[NaiveOutcome, ...]:
    """Writers loop through read, decide, write, with no ordering imposed at all.

    The only arrangement here is the shared release and the pause between the read
    and the write, which is where application code ordinarily spends its time. The
    writers overlap because the pause is longer than the round trip either statement
    costs, so each writer's write lands after somebody else has read the value it
    is about to overwrite.
    """
    release = threading.Barrier(LOOPING_WRITERS)
    outcomes: list[NaiveOutcome] = []
    guard = threading.Lock()

    def work(worker: int) -> None:
        failure: Exception | None = None
        seen: list[int] = []
        sent: list[int] = []
        connection = cluster.connect()
        try:
            release.wait()
            for _ in range(LOOPS_PER_WRITER):
                current = read_counter(connection, session_id, cluster.client_id)
                seen.append(current)
                time.sleep(DECIDE_PAUSE_SECONDS)
                send(connection, WRITE_COUNTER, (current + 1, session_id, cluster.client_id))
                sent.append(current + 1)
        except Exception as error:
            failure = error
        finally:
            connection.close()
        with guard:
            outcomes.append(
                NaiveOutcome(
                    worker=worker,
                    read=tuple(seen),
                    wrote=tuple(sent),
                    failure=failure,
                )
            )

    run_workers(work, LOOPING_WRITERS)
    return tuple(sorted(outcomes, key=lambda outcome: outcome.worker))


def run_staged_shipped(cluster: Cluster, session_id: UUID) -> tuple[Exception | None, ...]:
    """The same staging, through the shipped single-statement increment.

    Same writer count and the same barrier position: every writer arrives, and only
    then does any writer send its increment. Nothing here reads the counter first,
    which is the whole difference being measured.
    """
    release = threading.Barrier(STAGED_WRITERS)
    failures: list[Exception | None] = []
    guard = threading.Lock()

    def work(_worker: int) -> None:
        failure: Exception | None = None
        try:
            release.wait()
            counters = bump_session_counters(
                cluster.store,
                session_id,
                ONE_INCREMENT,
                client_id=cluster.client_id,
            )
            assert counters is not None, "the increment matched no Session row"
        except Exception as error:
            failure = error
        with guard:
            failures.append(failure)

    run_workers(work, STAGED_WRITERS)
    return tuple(failures)


def failure_report(outcomes: tuple[NaiveOutcome, ...]) -> str:
    """Describe every writer that failed, so a stopped writer is named."""
    return "; ".join(
        f"writer {outcome.worker} failed after {len(outcome.wrote)} write(s) with "
        f"{type(outcome.failure).__name__}: {outcome.failure}"
        for outcome in outcomes
        if outcome.failure is not None
    )


# ---------------------------------------------------------------------------
# The claims
# ---------------------------------------------------------------------------


def test_staged_read_modify_write_loses_every_increment_but_one(cluster: Cluster) -> None:
    session_id = new_session(cluster)
    assert final_counter(cluster, session_id) == 0

    outcomes = run_staged_naive(cluster, session_id)

    # Neither statement of the naive path can conflict: the read holds nothing and
    # the write is a whole value rather than an addition, so no writer is aborted
    # and every writer believes it succeeded.
    assert all(outcome.failure is None for outcome in outcomes), failure_report(outcomes)
    assert all(len(outcome.wrote) == 1 for outcome in outcomes)

    # Every writer read the same starting value, because the barrier held the writes
    # until all the reads were done.
    assert [outcome.read for outcome in outcomes] == [(0,)] * STAGED_WRITERS
    assert [outcome.wrote for outcome in outcomes] == [(1,)] * STAGED_WRITERS

    # So the counter holds one increment while six were issued, and the five that
    # were lost were lost silently.
    final = final_counter(cluster, session_id)
    assert final == 1
    lost = STAGED_WRITERS - final
    assert lost == STAGED_WRITERS - 1, (
        f"{STAGED_WRITERS} naive increments left the counter at {final}, so {lost} were lost"
    )


def test_staged_single_statement_increment_loses_nothing(cluster: Cluster) -> None:
    session_id = new_session(cluster)
    assert final_counter(cluster, session_id) == 0

    failures = run_staged_shipped(cluster, session_id)

    assert all(failure is None for failure in failures), "; ".join(
        f"{type(failure).__name__}: {failure}" for failure in failures if failure
    )

    # The comparison: the same writers, released at the same instant, against the
    # same row. The addition happened where the row is held, so every increment is
    # present and the loss the staged naive path shows is nought here.
    final = final_counter(cluster, session_id)
    assert final == STAGED_WRITERS, (
        f"{STAGED_WRITERS} single-statement increments left the counter at {final}"
    )


def test_looping_read_modify_write_loses_increments(cluster: Cluster) -> None:
    session_id = new_session(cluster)
    assert final_counter(cluster, session_id) == 0
    issued = LOOPING_WRITERS * LOOPS_PER_WRITER

    outcomes = run_looping_naive(cluster, session_id)

    assert all(outcome.failure is None for outcome in outcomes), failure_report(outcomes)
    assert sum(len(outcome.wrote) for outcome in outcomes) == issued

    # Loss, asserted rather than tolerated: the counter is strictly below the number
    # of increments issued. Two writers read one value here as well, but by racing
    # rather than by arrangement, which is why this test asserts that some were lost
    # and the staged test is the one that says how many.
    final = final_counter(cluster, session_id)
    assert 0 < final < issued, (
        f"{issued} naive increments left the counter at {final}; a naive "
        "read-modify-write under this much overlap is expected to lose increments, "
        "and losing none would mean the writers did not overlap"
    )

    # Several writers were handed the same value to add one to, which is the
    # mechanism of the loss rather than a restatement of it.
    read_values = [value for outcome in outcomes for value in outcome.read]
    assert len(set(read_values)) < len(read_values), (
        "no two naive reads returned one value, so the writers did not overlap"
    )
