"""Property 7: concurrent appends to one Session leave one line, never a tree.

**Validates: Requirements 8.1, 8.5, 15.1**

This property needs the cluster and needs real concurrency, and neither is
incidental. The whole guarantee is the isolation level plus two uniqueness
constraints: two appends to one Session read the same tip, the second commit
conflicts on that read and aborts, and the retry re-reads the new tip and produces
the following number. Nothing about that is observable without a cluster deciding
the conflicts, so the module is marked to gate on a reachable instance and is
deselected from the credential-free workflow.

Four decisions shape what is asserted.

**Contention is arranged rather than hoped for.** Every writer reaches its first
append at the same moment, released together, and only then follows the delays the
schedule drew. Without that the writers would trickle past each other and the
example would assert a property about serial appends wearing concurrent clothes.
The drawn submission order decides which writer is started first and the drawn
delays decide how each one paces itself afterwards, so the interleaving differs
from example to example while the collision at the start is guaranteed.

**A retry is expected and an exhaustion is not.** A conflict is the cluster doing
its job, so the store's own bounded, jittered retry is left exactly as it ships and
the waits it performs are counted through the injected sleeper, which is how an
example records whether it actually contended. What no example tolerates is a
writer that ran out of attempts or failed for any other reason: that is reported
as a failure naming the writer, because a lost append would make every count below
meaningless.

**The line is asserted by walking it, not by trusting the ordering.** Reading the
rows in sequence order and checking that consecutive digests agree would assume the
answer. Instead the rows are indexed by the predecessor each one claims and the
chain is walked from the genesis value onwards, so a fork shows up as two rows
claiming one predecessor, an orphan shows up as a row the walk never reaches, and a
break shows up as a walk that stops early. The uniqueness constraints make each of
those impossible; the walk is what demonstrates it rather than restating it.

**The digests are verified too.** Sequence numbers alone would not show that each
row's digest was computed inside the transaction that inserted it, so the chain the
concurrent writers built is put through the independent recomputation as well, and
the returned digests each writer received are compared against what is stored.

The example budget is 100 with no per-example deadline. Every Event is a real
insert on a shared instance and a conflict costs a real backoff wait, so
per-example cost swings with both the drawn writer count and the luck of the
interleaving; a deadline would fail a heavily contended example for contending.
A hundred examples of up to eight writers appending up to twenty Events each
finishes inside a minute locally, and the recorded contention shows nine examples
in ten reaching at least one retry, so the budget buys real collisions rather than
merely many runs. Where a budget had to give, it was the budget; no assertion was.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.errors import SerializationExhaustedError
from molt.models.event import EmbeddingState, Event, EventCategory
from molt.store import Connection, MemoryStore
from molt.store.chain import (
    GENESIS_PREDECESSOR,
    AppendedRow,
    ChainRow,
    LedgerAppend,
    append,
    chain_rows,
    verify_chain,
)
from molt.store.migrate import apply_migrations, discover_migrations

# Marked serial because the subject is the contention this module creates itself:
# concurrent appends to one Session against a bounded retry budget. Load from unrelated
# suites spends that budget on other transactions, so the module reports exhaustion where
# the property it asserts holds. It is run in a pass of its own.
pytestmark = [pytest.mark.integration, pytest.mark.concurrency, pytest.mark.serial]

# How many examples the property runs, and the bounds of a drawn schedule. The
# reasoning behind the budget is in the module docstring.
MAX_EXAMPLES: Final[int] = 100
MIN_WRITERS: Final[int] = 2
MAX_WRITERS: Final[int] = 8
MIN_EVENTS: Final[int] = 1
MAX_EVENTS: Final[int] = 20

# The longest pause a schedule may place before a writer's first append or between
# two of its appends. Small on purpose: a long pause would separate the writers
# instead of interleaving them, and the point of the pauses is to vary the order
# in which they collide rather than to keep them apart.
MAX_DELAY_MS: Final[int] = 5
MILLISECONDS_IN_SECOND: Final[float] = 1000.0

# The migration generation this module applies: the tenant table, the Session
# table, and the ledger with its two uniqueness constraints, which are what make
# the invariant below structural rather than probabilistic.
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

# The instant the first Event carries and how far apart two Events sit. The
# reading is derived from the epoch rather than written as a literal, and each
# Event of a schedule takes a slot of its own, so no two rows share an instant and
# no schedule embeds a calendar value.
BASE_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
STEP: Final[timedelta] = timedelta(seconds=1)

# The retention interval an appended row expires after.
RETENTION: Final[timedelta] = timedelta(days=90)

# The fields every appended Event shares, because none of them is what this
# property is about.
AGENT_CLI: Final[str] = "agent"
MACHINE_ID: Final[str] = "machine"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriterPlan:
    """One writer task: how many Events it appends, and how it paces itself.

    Attributes:
        start_delay_ms: How long the writer waits after the shared release before
            its first append.
        step_delays_ms: How long it waits after each append, one value per Event,
            so the number of Events is the length of this sequence.
    """

    start_delay_ms: int
    step_delays_ms: tuple[int, ...]

    @property
    def events(self) -> int:
        """How many Events this writer appends."""
        return len(self.step_delays_ms)


@dataclass(frozen=True, slots=True)
class Schedule:
    """A set of writer tasks contending for one Session, and their start order."""

    writers: tuple[WriterPlan, ...]
    submission_order: tuple[int, ...]

    @property
    def events(self) -> int:
        """How many Events the whole schedule appends."""
        return sum(writer.events for writer in self.writers)


@dataclass(frozen=True, slots=True)
class WriterOutcome:
    """What one writer task produced, or the failure that stopped it."""

    worker: int
    written: tuple[AppendedRow, ...]
    failure: Exception | None


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def delays() -> st.SearchStrategy[int]:
    """Draw one small pause, in milliseconds, including no pause at all."""
    return st.integers(min_value=0, max_value=MAX_DELAY_MS)


@st.composite
def writer_plans(draw: st.DrawFn) -> WriterPlan:
    """Draw one writer appending 1 to 20 Events with a pause around each."""
    events = draw(st.integers(min_value=MIN_EVENTS, max_value=MAX_EVENTS))
    steps = draw(st.lists(delays(), min_size=events, max_size=events))
    return WriterPlan(start_delay_ms=draw(delays()), step_delays_ms=tuple(steps))


@st.composite
def concurrent_schedules(draw: st.DrawFn) -> Schedule:
    """Draw 2 to 8 writer tasks for one Session, in a drawn submission order."""
    count = draw(st.integers(min_value=MIN_WRITERS, max_value=MAX_WRITERS))
    writers = draw(st.lists(writer_plans(), min_size=count, max_size=count))
    order = draw(st.permutations(range(count)))
    return Schedule(writers=tuple(writers), submission_order=tuple(order))


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

    The pool is the shipped one, which admits more connections than a schedule has
    writers, so a writer waits on the cluster rather than on the pool. The retry
    policy is the shipped one too: this property is about what the shipped
    behaviour produces under contention.
    """
    directory = tmp_path_factory.mktemp("molt_p07_core")
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


def new_session(cluster: Cluster) -> UUID:
    """Place one Session row, so each example owns a chain of its own."""
    session_id = uuid4()
    send(cluster.connection, INSERT_SESSION, (session_id, cluster.client_id, AGENT_CLI, MACHINE_ID))
    return session_id


def build_request(cluster: Cluster, session_id: UUID, slot: int) -> LedgerAppend:
    """One append request occupying a slot of its own on the timeline."""
    moment = BASE_INSTANT + slot * STEP
    return LedgerAppend(
        event=Event(
            id=uuid4(),
            session_id=session_id,
            client_id=cluster.client_id,
            category=EventCategory.TOOL_CALL,
            occurred_at=moment,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            parent_event_id=None,
            payload={"tool": "read", "slot": slot, "\u00e9tape": "one of many"},
            redacted=False,
            text_body="a tool call",
        ),
        expires_at=moment + RETENTION,
        embedding_state=EmbeddingState.PENDING,
    )


def run_schedule(
    cluster: Cluster, session_id: UUID, schedule: Schedule
) -> tuple[WriterOutcome, ...]:
    """Run every writer of a schedule at once, and report what each produced.

    A writer's failure is carried back rather than raised in its own thread, so the
    assertions see every writer's outcome instead of only the first one to fail.
    """
    release = threading.Barrier(len(schedule.writers))
    outcomes: list[WriterOutcome] = []
    guard = threading.Lock()

    def work(worker: int) -> None:
        plan = schedule.writers[worker]
        written: list[AppendedRow] = []
        failure: Exception | None = None
        try:
            release.wait()
            time.sleep(plan.start_delay_ms / MILLISECONDS_IN_SECOND)
            for index, pause in enumerate(plan.step_delays_ms):
                slot = worker * MAX_EVENTS + index
                written.append(append(cluster.store, build_request(cluster, session_id, slot)))
                time.sleep(pause / MILLISECONDS_IN_SECOND)
        except Exception as error:
            # Carried back rather than raised here, so the assertions see every
            # writer's outcome instead of only the first thread to fail.
            failure = error
        with guard:
            outcomes.append(WriterOutcome(worker=worker, written=tuple(written), failure=failure))

    threads = [
        threading.Thread(target=work, args=(worker,), name=f"writer-{worker}")
        for worker in schedule.submission_order
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return tuple(sorted(outcomes, key=lambda outcome: outcome.worker))


def failure_report(outcomes: Sequence[WriterOutcome]) -> str:
    """Describe every writer that failed, naming an exhaustion for what it is."""
    lines: list[str] = []
    for outcome in outcomes:
        if outcome.failure is None:
            continue
        if isinstance(outcome.failure, SerializationExhaustedError):
            lines.append(
                f"writer {outcome.worker} ran out of retries after "
                f"{len(outcome.written)} append(s); a retried conflict is expected "
                "under contention and an exhausted one is not"
            )
        else:
            lines.append(
                f"writer {outcome.worker} failed after {len(outcome.written)} "
                f"append(s) with {type(outcome.failure).__name__}: {outcome.failure}"
            )
    return "; ".join(lines)


def walk_from_genesis(rows: Sequence[ChainRow]) -> tuple[ChainRow, ...]:
    """Follow the chain from the genesis predecessor, one row per predecessor.

    Rows are indexed by the predecessor each one claims, so following the walk
    reaches a row only if exactly one row claimed the digest before it. A fork
    would have collapsed two rows into one index entry and is refused before the
    walk starts; an orphan is a row the walk never reaches.
    """
    by_predecessor: dict[str, ChainRow] = {}
    for row in rows:
        assert row.prev_chain_digest not in by_predecessor, (
            f"rows {by_predecessor[row.prev_chain_digest].seq} and {row.seq} both "
            "claim one predecessor, so the chain forked"
        )
        by_predecessor[row.prev_chain_digest] = row

    walked: list[ChainRow] = []
    digest = GENESIS_PREDECESSOR
    while digest in by_predecessor:
        current = by_predecessor.pop(digest)
        walked.append(current)
        digest = current.chain_digest
    return tuple(walked)


def contention_band(waits: int) -> str:
    """How much the writers actually contended, for the coverage record."""
    if waits == 0:
        return "none"
    if waits <= 4:
        return "1-4"
    if waits <= 16:
        return "5-16"
    return "17+"


# Feature: molt, Property 7: For any interleaving of concurrent Event inserts into
# one Session, the resulting sequence numbers are unique and contiguous from 1,
# every row's predecessor digest equals exactly one other row's chain digest or the
# genesis value, and no two rows in the Session share a predecessor.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(schedule=concurrent_schedules())
def test_concurrent_appends_to_one_session_produce_one_unbroken_line(
    cluster: Cluster, schedule: Schedule
) -> None:
    total = schedule.events
    event(f"writers={len(schedule.writers)}")
    cluster.backoff.take()
    session_id = new_session(cluster)

    outcomes = run_schedule(cluster, session_id, schedule)
    event(f"contention={contention_band(cluster.backoff.take())}")

    # A conflict retried is the cluster doing its job; a writer that stopped is a
    # lost append, and every count below would then be counting the wrong thing.
    assert all(outcome.failure is None for outcome in outcomes), failure_report(outcomes)

    # Every writer's own appends came back, and between them they hold each
    # sequence number from one to the total exactly once.
    returned = [row for outcome in outcomes for row in outcome.written]
    assert len(returned) == total
    assert sorted(row.seq for row in returned) == list(range(1, total + 1))

    stored = chain_rows(cluster.store, session_id)

    # Contiguous from one, with no gap, and unique, with no duplicate.
    assert len(stored) == total
    assert [row.seq for row in stored] == list(range(1, total + 1))
    assert len({row.seq for row in stored}) == total

    # Exactly one predecessor per row: the genesis value opens the chain, no two
    # rows claim one predecessor, and every other predecessor is some row's chain
    # digest, so there is no fork and no orphan.
    assert stored[0].prev_chain_digest == GENESIS_PREDECESSOR
    predecessors = [row.prev_chain_digest for row in stored]
    assert len(set(predecessors)) == total, "two rows claimed one predecessor"
    produced = {row.chain_digest for row in stored}
    assert len(produced) == total, "two rows produced one chain digest"
    named = set(predecessors) - {GENESIS_PREDECESSOR}
    assert named <= produced, "a row named a predecessor no row in the Session produced"

    walked = walk_from_genesis(stored)
    assert [row.seq for row in walked] == list(range(1, total + 1)), (
        "the walk from the genesis predecessor did not reach every row in order"
    )

    # The digests each writer received are the digests that are stored, and the
    # whole concurrent chain survives the independent recomputation.
    by_sequence = {row.seq: row for row in returned}
    for row in stored:
        assert by_sequence[row.seq].chain_digest == row.chain_digest
        assert by_sequence[row.seq].prev_chain_digest == row.prev_chain_digest

    report = verify_chain(cluster.store, session_id)
    assert report.ok, f"the concurrent chain disagreed at sequence {report.first_mismatch_seq}"
    assert report.rows == total
    assert report.terminal_digest == stored[-1].chain_digest
