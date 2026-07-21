"""Concurrent Session counter increments lose nothing, and lock nothing unrelated.

**Validates: Requirements 15.6, 15.7, 33.1, 36.3**

Two claims are demonstrated here, and both need a cluster deciding real
concurrency, so the module gates on a reachable instance and is deselected from
the credential-free workflow.

**No increment is lost.** The shipped increment names each counter column on both
sides of its own assignment, so the addition happens in the cluster and no value
passes through application memory between a read and a write. What makes that
observable is arranging writers to overlap and then counting: every writer reaches
its first increment at the same moment, and the final total must equal the number
of increments issued, exactly. The equality is the assertion; a total that merely
looked plausible would be the same number a lossy implementation produces on a
lucky run.

**The totals returned prove serialisation rather than suggest it.** Each increment
returns the totals its own transaction produced, so a run of N increments that lost
nothing hands back each of the values 1 through N exactly once. Two writers whose
increments collapsed into one would return the same total twice, and that shows up
as a missing value in the sequence rather than as a smaller final figure alone. The
per-writer readings are therefore checked as a set as well as in aggregate.

**Nothing unrelated is serialised.** The second test increments counters on
Sessions that share nothing but a tenant, and counts the retry waits the store
performs through an injected sleeper. Distinct Sessions touch disjoint rows, so the
cluster has nothing to conflict on and the wait count stays at zero; a design that
reached for a shared lock or a serialising sentinel row would show up as waits here
even though every assertion about totals would still hold. Twenty writers on twenty
Sessions with twenty distinct machine identifiers is also the concurrency figure the
scale envelope names.

The counter-loss claim is what the companion comparison module contrasts: the same
logical update written as an application-level read-modify-write, asserting the loss
that this module asserts the absence of.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations, discover_migrations
from molt.store.sessions import CounterDelta, SessionCounters, bump_session_counters
from molt.store.sessions import session_of_client as read_session

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

# The contended run: how many writers increment one Session at once, and how many
# increments each of them issues. Modest on purpose. The claim is an exact equality
# rather than a throughput figure, so a heavier run would cost the shared instance
# more without making the equality any sharper.
CONTENDED_WRITERS: Final[int] = 8
INCREMENTS_PER_WRITER: Final[int] = 10

# The unrelated run: one Session per writer, each writer carrying a machine
# identifier of its own. Twenty is the concurrent-writer figure the scale envelope
# states, and it is also what the shipped pool admits, so a writer here waits on
# the cluster rather than on a connection.
UNRELATED_WRITERS: Final[int] = 20
UNRELATED_INCREMENTS: Final[int] = 4

# What one increment moves. Three of the five counters move, and they are of three
# different column types, so a lost increment cannot hide in an integer count while
# the exact decimal total still adds up.
TOKENS_PER_INCREMENT: Final[int] = 7
COST_PER_INCREMENT: Final[Decimal] = Decimal("0.000500")
ONE_INCREMENT: Final[CounterDelta] = CounterDelta(
    tool_calls=1,
    tokens=TOKENS_PER_INCREMENT,
    cost_usd=COST_PER_INCREMENT,
)

# The migration generation this module applies: the tenant table and the Session
# table with its five counter columns.
CORE_MIGRATION_VERSION: Final[int] = 1

# The fixture's own statements. The module under test owns neither insert, so both
# are written here with every value bound and no identifier interpolated.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)

# The fields every Session row here shares, because none of them is what these
# tests are about. The unrelated run overrides the machine identifier per writer.
AGENT_CLI: Final[str] = "agent"
MACHINE_ID: Final[str] = "machine"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# The cluster the writers contend on
# ---------------------------------------------------------------------------


class BackoffCounter:
    """Counts the waits the store's retry schedule performs, then performs them.

    Counting through the injected sleeper is what makes contention observable
    without reading a log: the wrapper waits once per retry and nowhere else, so a
    count above zero means the cluster really did abort somebody. The wait itself is
    still the schedule's own, so nothing about the shipped backoff is altered by
    measuring it.
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


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding the core migration, a store over it, and one tenant."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID
    backoff: BackoffCounter


@dataclass(frozen=True, slots=True)
class WriterOutcome:
    """What one writer produced: the totals it was handed, or what stopped it."""

    worker: int
    session_id: UUID
    seen: tuple[SessionCounters, ...]
    failure: Exception | None


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


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
    """Apply the core migration, then build a store bound to that schema.

    The pool, the retry policy, and the backoff schedule are all the shipped ones.
    These tests are about what the shipped increment does under contention, so
    nothing about it is substituted; the sleeper is wrapped rather than replaced, so
    the waits are counted while still being waited.
    """
    directory = tmp_path_factory.mktemp("molt_counter_core")
    stage_core_migration(directory)
    apply_migrations(fresh_schema, directory=directory)

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

    client_id = uuid4()
    send(fresh_schema, INSERT_CLIENT, (client_id, f"tenant-{client_id.hex[:8]}", "Tenant", "eu"))

    backoff = BackoffCounter()
    with MemoryStore(connect_with=connect_with, sleep=backoff.wait) as store:
        yield Cluster(
            store=store,
            connection=fresh_schema,
            client_id=client_id,
            backoff=backoff,
        )


def new_session(cluster: Cluster, machine_id: str = MACHINE_ID) -> UUID:
    """Place one Session row, so each test owns counters of its own."""
    session_id = uuid4()
    send(cluster.connection, INSERT_SESSION, (session_id, cluster.client_id, AGENT_CLI, machine_id))
    return session_id


def run_writers(
    cluster: Cluster, targets: dict[int, UUID], increments: int
) -> tuple[WriterOutcome, ...]:
    """Run every writer at once, each incrementing the Session it was handed.

    The barrier is what makes the overlap a fact rather than a hope: no writer
    issues its first increment until every writer has arrived, so the writers
    collide at the start instead of trickling past each other. A writer's failure is
    carried back rather than raised in its own thread, so the assertions see every
    writer's outcome instead of only the first one to fail.
    """
    release = threading.Barrier(len(targets))
    outcomes: list[WriterOutcome] = []
    guard = threading.Lock()

    def work(worker: int) -> None:
        session_id = targets[worker]
        seen: list[SessionCounters] = []
        failure: Exception | None = None
        try:
            release.wait()
            for _ in range(increments):
                counters = bump_session_counters(
                    cluster.store,
                    session_id,
                    ONE_INCREMENT,
                    client_id=cluster.client_id,
                )
                assert counters is not None, "the increment matched no Session row"
                seen.append(counters)
        except Exception as error:
            failure = error
        with guard:
            outcomes.append(
                WriterOutcome(
                    worker=worker,
                    session_id=session_id,
                    seen=tuple(seen),
                    failure=failure,
                )
            )

    threads = [
        threading.Thread(target=work, args=(worker,), name=f"counter-{worker}")
        for worker in sorted(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return tuple(sorted(outcomes, key=lambda outcome: outcome.worker))


def failure_report(outcomes: tuple[WriterOutcome, ...]) -> str:
    """Describe every writer that failed, so a stopped writer is named."""
    return "; ".join(
        f"writer {outcome.worker} failed after {len(outcome.seen)} increment(s) with "
        f"{type(outcome.failure).__name__}: {outcome.failure}"
        for outcome in outcomes
        if outcome.failure is not None
    )


def stored_counters(cluster: Cluster, session_id: UUID) -> SessionCounters:
    """Read the counters the cluster holds for one Session."""
    session = read_session(cluster.store, session_id, cluster.client_id)
    assert session is not None, "the Session the writers incremented could not be read"
    return SessionCounters(
        tool_call_count=session.tool_call_count,
        model_request_count=session.model_request_count,
        error_count=session.error_count,
        token_count=session.token_count,
        cost_usd=session.cost_usd,
    )


# ---------------------------------------------------------------------------
# The claims
# ---------------------------------------------------------------------------


def test_concurrent_increments_to_one_session_lose_nothing(cluster: Cluster) -> None:
    cluster.backoff.take()
    session_id = new_session(cluster)
    targets = dict.fromkeys(range(CONTENDED_WRITERS), session_id)
    issued = CONTENDED_WRITERS * INCREMENTS_PER_WRITER

    outcomes = run_writers(cluster, targets, INCREMENTS_PER_WRITER)

    # A writer that stopped would make every count below count the wrong thing, so
    # it is reported as itself rather than as a smaller total.
    assert all(outcome.failure is None for outcome in outcomes), failure_report(outcomes)
    assert sum(len(outcome.seen) for outcome in outcomes) == issued

    # The exact equality: every increment issued is present in the stored total,
    # for the integer counter, the wider integer counter, and the exact decimal.
    final = stored_counters(cluster, session_id)
    assert final.tool_call_count == issued
    assert final.token_count == issued * TOKENS_PER_INCREMENT
    assert final.cost_usd == COST_PER_INCREMENT * issued

    # The counters no increment moved stayed where they were, so the statement moved
    # what it was asked to and nothing else.
    assert final.model_request_count == 0
    assert final.error_count == 0

    # Each increment was handed the totals its own transaction produced, so a run
    # that lost nothing hands back each value from one to the total exactly once.
    # Two increments that collapsed into one would repeat a value here.
    returned = [counters.tool_call_count for outcome in outcomes for counters in outcome.seen]
    assert sorted(returned) == list(range(1, issued + 1)), (
        "two increments returned one total, so an increment was overwritten rather than added"
    )

    # The wider counter and the decimal move in step with the tool-call counter in
    # every reading, which is what says the five columns were assigned in one
    # statement rather than in separate visits to the row.
    for outcome in outcomes:
        for counters in outcome.seen:
            assert counters.token_count == counters.tool_call_count * TOKENS_PER_INCREMENT
            assert counters.cost_usd == COST_PER_INCREMENT * counters.tool_call_count


def test_increments_to_unrelated_sessions_serialise_on_nothing(cluster: Cluster) -> None:
    cluster.backoff.take()
    targets = {
        worker: new_session(cluster, machine_id=f"machine-{worker:02d}")
        for worker in range(UNRELATED_WRITERS)
    }
    assert len(set(targets.values())) == UNRELATED_WRITERS

    outcomes = run_writers(cluster, targets, UNRELATED_INCREMENTS)
    waits = cluster.backoff.take()

    assert all(outcome.failure is None for outcome in outcomes), failure_report(outcomes)

    # Twenty writers on twenty Sessions conflicted on nothing, so the retry schedule
    # never ran. A lock or a sentinel row shared across unrelated Sessions would be
    # visible here as waits, while every total below would still be correct.
    assert waits == 0, (
        f"unrelated Sessions cost {waits} retry wait(s), so something serialised "
        "writers that share no row"
    )

    # Each Session holds its own increments and no other writer's.
    for outcome in outcomes:
        final = stored_counters(cluster, outcome.session_id)
        assert final.tool_call_count == UNRELATED_INCREMENTS
        assert final.token_count == UNRELATED_INCREMENTS * TOKENS_PER_INCREMENT
        assert final.cost_usd == COST_PER_INCREMENT * UNRELATED_INCREMENTS
        assert [counters.tool_call_count for counters in outcome.seen] == list(
            range(1, UNRELATED_INCREMENTS + 1)
        )
