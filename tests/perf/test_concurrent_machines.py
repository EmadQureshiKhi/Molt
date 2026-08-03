"""Twenty machines write Events at once, and no lock is shared across Sessions.

**Validates: Requirements 33.1**

Why the bound exists. Every developer machine running the agent is a separate
writer into one cluster, and each of them appends to a Session of its own. The
Hash_Chain makes an append sequence-ordered *within* a Session — the appending
statement reads the tip and derives the next digest from it — so two writers on one
Session genuinely conflict, and that conflict is by design. What would not be by
design is a conflict between writers on *unrelated* Sessions: that would make the
capture path's throughput fall as the fleet grew, for no correctness reason at all.
Requirement 33.1 states the concurrency the store must accept, and this module is
the only place that many writers run at once.

**What is asserted, and what would falsify it.** Twenty threads, each carrying its
own machine identifier and appending to its own Session, all succeed: every append
returns its row, every chain verifies by independent recomputation, every chain
holds exactly the rows its writer appended, and the Ledger holds twenty distinct
machine identifiers. A store that took a lock across unrelated Sessions would still
pass all of that, eventually, so the overlap is asserted too: the wall time of the
concurrent phase is compared against the total time the writers spent busy, and a
serialising lock would drive those two figures together. The margin is stated as a
constant below rather than left implicit.

**What is inside the measurement and what is outside it.** Inside: twenty concurrent
`append` calls per writer, each one a SERIALIZABLE transaction through the store's
own retry wrapper against a real cluster. Outside, and reported separately: the
schema and its migration, the Session rows, and the Events themselves, which are
built before any thread starts so no writer is timed while rendering a payload.

**No vector is placed.** An Event lands owing a vector rather than carrying one, and
this module writes rows in the state the ingest path writes them, so nothing here
pays vector index maintenance and the setup is a handful of statements.

**The corpus is small on purpose.** What is being established is that unrelated
writers do not block each other, and that shows up in the first few rows of each
chain as clearly as in the thousandth. A larger corpus would only put more load on
a shared instance.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.models.event import Event, EventCategory, JsonObject
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.store import DEFAULT_MAX_CONNECTIONS, Connection, MemoryStore
from molt.store.chain import AppendedRow, LedgerAppend, append, verify_chain
from molt.store.migrate import apply_migrations

# A bound measured against a cluster: every timed call opens a real transaction and
# commits real rows, so this module needs a reachable instance and skips at
# collection naming what was missing when none is.
pytestmark = [pytest.mark.perf, pytest.mark.instance]

# The concurrency the requirement states. Each writer is one machine identifier and
# one Session, and the count is held at what the connection pool admits so the
# figure describes concurrent writers rather than writers queueing for a connection.
WRITERS: Final[int] = 20
EVENTS_PER_WRITER: Final[int] = 5
TOTAL_EVENTS: Final[int] = WRITERS * EVENTS_PER_WRITER

# How much of the writers' combined busy time the concurrent phase must come in
# under. A store serialising unrelated Sessions would spend close to the whole of
# it; genuine overlap across twenty writers comes in far below. The margin is set
# well inside what overlap delivers, so the case reports a real regression rather
# than the scheduling noise of a shared instance.
OVERLAP_CEILING: Final[float] = 0.60

# The instant the generated records observe and the spacing between two records of
# one chain, derived from the epoch so a run embeds nothing about when it happened.
RECORD_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RECORD_STEP: Final[timedelta] = timedelta(milliseconds=250)
RETENTION: Final[timedelta] = timedelta(days=90)

AGENT_CLI: Final[str] = "a-coding-agent"
SESSION_OUTCOME: Final[str] = "succeeded"
MS_PER_SECOND: Final[float] = 1000.0

# The fixture's own statements. Every value is bound and no identifier is ever
# interpolated, the search path included.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, started_at, ended_at, outcome) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
COUNT_LEDGER: Final[str] = "SELECT count(*) FROM ledger"
COUNT_MACHINES: Final[str] = "SELECT count(DISTINCT machine_id) FROM ledger"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def machine_name(index: int) -> str:
    """The machine identifier one writer presents. Distinct per writer by construction."""
    return f"machine-{index:02d}-of-{WRITERS}"


def record_payload(index: int) -> JsonObject:
    """The payload one record carries: a command, its result, and its correlation."""
    return {
        "tool": "Bash",
        "command": "pytest tests/unit -q",
        "exit_code": 0,
        "duration_ms": 412,
        "tool_use_id": f"a-tool-use-identifier-{index}",
    }


def build_record(session_id: UUID, machine: str, index: int) -> LedgerAppend:
    """One well-formed Event of the shape the capture side transmits, as an append."""
    return LedgerAppend(
        event=Event(
            id=uuid4(),
            session_id=session_id,
            client_id=UNASSIGNED_CLIENT_ID,
            category=EventCategory.TOOL_RESULT,
            occurred_at=RECORD_INSTANT + RECORD_STEP * index,
            agent_cli=AGENT_CLI,
            machine_id=machine,
            parent_event_id=None,
            payload=record_payload(index),
            redacted=False,
            text_body="the suite passed and nothing was written",
        ),
        expires_at=RECORD_INSTANT + RETENTION,
    )


@dataclass(frozen=True, slots=True)
class Writer:
    """One machine: its identifier, its Session, and the Events it will append."""

    machine: str
    session_id: UUID
    records: tuple[LedgerAppend, ...]


@dataclass(frozen=True, slots=True)
class Fleet:
    """A store over a real cluster and the writers prepared against it."""

    store: MemoryStore
    connection: DriverConnection
    writers: tuple[Writer, ...]
    setup_seconds: float

    def scalar(self, statement: str) -> int:
        """Read one count on the fixture's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement)
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])


@dataclass(frozen=True, slots=True)
class Written:
    """What one writer produced and how long it was busy doing it."""

    machine: str
    session_id: UUID
    rows: tuple[AppendedRow, ...]
    busy_seconds: float


def drive(fleet: Fleet, writer: Writer) -> Written:
    """Append one writer's whole run, timing only the appends."""
    started = time.perf_counter()
    rows = tuple(append(fleet.store, record) for record in writer.records)
    return Written(
        machine=writer.machine,
        session_id=writer.session_id,
        rows=rows,
        busy_seconds=time.perf_counter() - started,
    )


@pytest.fixture(scope="module")
def fleet(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Fleet]:
    """Apply the migrations, place one Session per machine, and build every record."""
    started = time.perf_counter()
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    writers: list[Writer] = []
    for index in range(WRITERS):
        machine = machine_name(index)
        session_id = uuid4()
        with fresh_schema.cursor() as cursor:
            cursor.execute(
                INSERT_SESSION,
                (
                    session_id,
                    UNASSIGNED_CLIENT_ID,
                    AGENT_CLI,
                    machine,
                    RECORD_INSTANT,
                    RECORD_INSTANT,
                    SESSION_OUTCOME,
                ),
            )
        writers.append(
            Writer(
                machine=machine,
                session_id=session_id,
                records=tuple(
                    build_record(session_id, machine, offset) for offset in range(EVENTS_PER_WRITER)
                ),
            )
        )
    setup = time.perf_counter() - started

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    print(
        f"\nsetup, outside every measurement: the migrations, {WRITERS} Sessions, and "
        f"{TOTAL_EVENTS} records built in {setup:.2f}s"
    )

    with MemoryStore(connect_with=connect_with) as store:
        assert store.max_connections >= WRITERS, (
            f"the pool admits {store.max_connections} connections where {WRITERS} "
            "concurrent writers are being measured, so the figure would describe "
            "queueing for a connection rather than concurrency"
        )
        yield Fleet(
            store=store,
            connection=fresh_schema,
            writers=tuple(writers),
            setup_seconds=setup,
        )


def test_the_pool_admits_the_stated_concurrency(fleet: Fleet) -> None:
    """The store's own default is what makes twenty concurrent writers meaningful."""
    assert DEFAULT_MAX_CONNECTIONS >= WRITERS
    assert len(fleet.writers) == WRITERS
    assert len({writer.machine for writer in fleet.writers}) == WRITERS, (
        "the writers do not carry distinct machine identifiers"
    )
    assert len({writer.session_id for writer in fleet.writers}) == WRITERS, (
        "the writers do not write to unrelated Sessions"
    )


def test_twenty_machines_write_concurrently_without_a_shared_lock(fleet: Fleet) -> None:
    """Requirement 33.1: every concurrent writer succeeds, and they genuinely overlap."""
    before = fleet.scalar(COUNT_LEDGER)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        results = tuple(pool.map(lambda writer: drive(fleet, writer), fleet.writers))
    wall = time.perf_counter() - started

    busy = sum(result.busy_seconds for result in results)
    slowest = max(result.busy_seconds for result in results)
    print(
        f"{WRITERS} machines appending {EVENTS_PER_WRITER} Events each to unrelated "
        f"Sessions: {wall * MS_PER_SECOND:.1f} ms wall, {busy * MS_PER_SECOND:.1f} ms "
        f"combined busy across the writers, slowest writer "
        f"{slowest * MS_PER_SECOND:.1f} ms; overlap ratio {wall / busy:.3f} "
        f"(ceiling {OVERLAP_CEILING:.2f}); setup {fleet.setup_seconds:.2f}s, outside "
        "the measurement"
    )

    assert len(results) == WRITERS
    for result in results:
        assert len(result.rows) == EVENTS_PER_WRITER, (
            f"machine {result.machine} appended {len(result.rows)} of {EVENTS_PER_WRITER} Events"
        )
        report = verify_chain(fleet.store, result.session_id)
        assert report.ok, (
            f"the chain of machine {result.machine} disagreed at sequence "
            f"{report.first_mismatch_seq}"
        )
        assert report.rows == EVENTS_PER_WRITER, (
            f"the chain of machine {result.machine} holds {report.rows} rows where "
            f"{EVENTS_PER_WRITER} were appended to it"
        )

    assert fleet.scalar(COUNT_LEDGER) == before + TOTAL_EVENTS, (
        "the Ledger did not grow by the number of Events driven, so some concurrent "
        "append did not land"
    )
    assert fleet.scalar(COUNT_MACHINES) == WRITERS, (
        "the Ledger does not hold one row per distinct machine identifier"
    )
    assert wall < busy * OVERLAP_CEILING, (
        f"the concurrent phase took {wall * MS_PER_SECOND:.1f} ms against "
        f"{busy * MS_PER_SECOND:.1f} ms of combined writer time, so the writers were "
        "close to serialised and a lock may be shared across unrelated Sessions"
    )
