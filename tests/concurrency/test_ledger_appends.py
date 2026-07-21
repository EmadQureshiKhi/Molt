"""Concurrent appends to distinct Sessions conflict on nothing at all.

**Validates: Requirements 15.7, 33.1, 36.3**

The companion property over one Session asserts what happens when writers contend:
they read the same tip, the cluster aborts one of them, and the shipped retry
re-reads and produces the following number. This module asserts the other half of
that story, which is easy to assume and worth measuring — writers on Sessions that
share nothing contend on nothing.

**The claim is measured, not assumed.** The append reads the tip of its own Session
and inserts into that Session, so twenty writers on twenty Sessions touch twenty
disjoint sets of rows and the cluster has no overlap to abort anybody over. What
turns that reasoning into an observation is the injected sleeper: the retry schedule
waits once per retry and nowhere else, so counting the waits counts the conflicts.
The assertion is that the count is nought. A lock shared across unrelated Sessions,
a serialising sentinel row, or a tip read wider than one Session would each show up
here as waits, while every assertion about the shape of the chains would still pass.

**Concurrency is arranged and then confirmed.** Every writer reaches its first
append at the same moment, released together. Confirming that they really overlapped
matters as much as arranging it, because zero conflicts is also what a run of
writers that never met would report: each writer records when it started and
finished, and the assertion is that there was an instant at which all twenty were in
flight. Without that reading, a pool that admitted one connection at a time would
look like an absence of conflict rather than an absence of concurrency.

**Twenty writers with twenty machine identifiers is the stated figure.** The scale
envelope names concurrent writes from at least twenty distinct machine identifiers
with no lock shared across unrelated Sessions, so the run is sized to that figure
rather than to a round number, and each writer carries a machine identifier of its
own.

**Each chain is checked as a chain.** Every Session's rows are read back in sequence
order, and each Session must hold a line of its own: numbered from one with no gap,
opening at the genesis predecessor, and surviving the independent digest
recomputation. A writer whose appends had somehow reached another Session's chain
would show up as a sequence number missing from one line and duplicated in another.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.models.event import EmbeddingState, Event, EventCategory
from molt.store import Connection, MemoryStore
from molt.store.chain import (
    GENESIS_PREDECESSOR,
    AppendedRow,
    LedgerAppend,
    append,
    chain_rows,
    verify_chain,
)
from molt.store.migrate import apply_migrations, discover_migrations

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

# How many writers run at once, each owning a Session and a machine identifier of
# its own, and how many Events each of them appends. The writer count is the
# concurrent-writer figure the scale envelope states, and it is also what the
# shipped pool admits, so a writer waits on the cluster rather than on a connection.
WRITERS: Final[int] = 20
EVENTS_PER_WRITER: Final[int] = 3

# The migration generation this module applies: the tenant table, the Session table,
# and the ledger with its two uniqueness constraints.
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

# The instant the first Event carries and how far apart two Events sit. The reading
# is derived from the epoch rather than written as a literal, so no row embeds a
# calendar value, and every Event takes a slot of its own.
BASE_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
STEP: Final[timedelta] = timedelta(seconds=1)

# The retention interval an appended row expires after.
RETENTION: Final[timedelta] = timedelta(days=90)

# The agent every writer reports itself as. The machine identifier is the field that
# differs per writer, because that is the one the scale figure counts.
AGENT_CLI: Final[str] = "agent"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


class BackoffCounter:
    """Counts the waits the store's retry schedule performs, then performs them.

    Counting through the injected sleeper is what makes the absence of contention an
    observation: the wrapper waits once per retry and nowhere else, so a count of
    nought means the cluster aborted nobody. The wait itself is still the schedule's
    own, so nothing about the shipped backoff is altered by measuring it.
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
    """What one writer produced, when it ran, and the failure that stopped it.

    Attributes:
        worker: Which writer this was.
        session_id: The Session it appended to, which no other writer touched.
        written: The rows the appends returned, in the order they were appended.
        started: The monotonic reading taken after the shared release.
        finished: The monotonic reading taken after the writer's last append.
        failure: What stopped the writer, or nothing when it completed.
    """

    worker: int
    session_id: UUID
    written: tuple[AppendedRow, ...]
    started: float
    finished: float
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

    The pool and the retry policy are the shipped ones, and the sleeper is the
    shipped schedule's wait with a counter around it, so what is measured is what
    ships.
    """
    directory = tmp_path_factory.mktemp("molt_appends_core")
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


def new_session(cluster: Cluster, machine_id: str) -> UUID:
    """Place one Session row for one writer, carrying that writer's machine."""
    session_id = uuid4()
    send(cluster.connection, INSERT_SESSION, (session_id, cluster.client_id, AGENT_CLI, machine_id))
    return session_id


def build_request(cluster: Cluster, session_id: UUID, machine_id: str, slot: int) -> LedgerAppend:
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
            machine_id=machine_id,
            parent_event_id=None,
            payload={"tool": "read", "slot": slot, "\u00e9tape": "one of many"},
            redacted=False,
            text_body="a tool call",
        ),
        expires_at=moment + RETENTION,
        embedding_state=EmbeddingState.PENDING,
    )


def run_writers(
    cluster: Cluster, sessions: dict[int, tuple[UUID, str]]
) -> tuple[WriterOutcome, ...]:
    """Append to every Session at once, one writer per Session.

    The barrier is what makes the overlap a fact rather than a hope: no writer issues
    its first append until every writer has arrived. Each writer's own start and
    finish readings are carried back, so the assertions can confirm the writers were
    in flight together rather than merely started together.
    """
    release = threading.Barrier(len(sessions))
    outcomes: list[WriterOutcome] = []
    guard = threading.Lock()

    def work(worker: int) -> None:
        session_id, machine_id = sessions[worker]
        written: list[AppendedRow] = []
        failure: Exception | None = None
        release.wait()
        started = time.monotonic()
        try:
            for index in range(EVENTS_PER_WRITER):
                written.append(
                    append(cluster.store, build_request(cluster, session_id, machine_id, index))
                )
        except Exception as error:
            failure = error
        finished = time.monotonic()
        with guard:
            outcomes.append(
                WriterOutcome(
                    worker=worker,
                    session_id=session_id,
                    written=tuple(written),
                    started=started,
                    finished=finished,
                    failure=failure,
                )
            )

    threads = [
        threading.Thread(target=work, args=(worker,), name=f"appender-{worker}")
        for worker in sorted(sessions)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return tuple(sorted(outcomes, key=lambda outcome: outcome.worker))


def failure_report(outcomes: tuple[WriterOutcome, ...]) -> str:
    """Describe every writer that failed, so a stopped writer is named."""
    return "; ".join(
        f"writer {outcome.worker} failed after {len(outcome.written)} append(s) with "
        f"{type(outcome.failure).__name__}: {outcome.failure}"
        for outcome in outcomes
        if outcome.failure is not None
    )


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


def test_concurrent_appends_to_distinct_sessions_never_conflict(cluster: Cluster) -> None:
    cluster.backoff.take()
    sessions = {
        worker: (new_session(cluster, f"machine-{worker:02d}"), f"machine-{worker:02d}")
        for worker in range(WRITERS)
    }
    assert len({session_id for session_id, _ in sessions.values()}) == WRITERS
    assert len({machine_id for _, machine_id in sessions.values()}) == WRITERS

    outcomes = run_writers(cluster, sessions)
    waits = cluster.backoff.take()

    assert all(outcome.failure is None for outcome in outcomes), failure_report(outcomes)

    # The claim: nothing conflicted. The retry schedule waits once per conflict and
    # nowhere else, so nought waits is nought aborts across twenty writers and twenty
    # machine identifiers.
    assert waits == 0, (
        f"appends to {WRITERS} distinct Sessions cost {waits} retry wait(s), so "
        "writers that share no row were serialised against each other"
    )

    # And they really were concurrent: there was an instant at which every writer had
    # started and none had finished, so the absence of conflict above is not the
    # absence of overlap.
    latest_start = max(outcome.started for outcome in outcomes)
    earliest_finish = min(outcome.finished for outcome in outcomes)
    assert latest_start < earliest_finish, (
        "no instant held every writer at once, so the run did not overlap and the "
        "conflict count says nothing"
    )

    # Each Session holds a line of its own: numbered from one with no gap, opening at
    # the genesis predecessor, and agreeing with what its writer was handed.
    for outcome in outcomes:
        assert [row.seq for row in outcome.written] == list(range(1, EVENTS_PER_WRITER + 1))

        stored = chain_rows(cluster.store, outcome.session_id)
        assert [row.seq for row in stored] == list(range(1, EVENTS_PER_WRITER + 1))
        assert stored[0].prev_chain_digest == GENESIS_PREDECESSOR
        assert [row.chain_digest for row in stored] == [row.chain_digest for row in outcome.written]

        report = verify_chain(cluster.store, outcome.session_id)
        assert report.ok, (
            f"the chain of writer {outcome.worker} disagreed at sequence "
            f"{report.first_mismatch_seq}"
        )
        assert report.rows == EVENTS_PER_WRITER

    # No row of one Session reached another: the chain digests across all the writers
    # are distinct, and every Session's rows add up to the whole run.
    digests = [row.chain_digest for outcome in outcomes for row in outcome.written]
    assert len(set(digests)) == WRITERS * EVENTS_PER_WRITER
