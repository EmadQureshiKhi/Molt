"""The ledger hash chain against a live instance.

The unit module of the same concern asserts the shape of the statement and drives
the verifier over rows a test constructed. This one asserts the four things only a
cluster can answer, because each rests on the cluster's own behaviour rather than
on the module's text.

Sequence numbers are derived by the statement and come out contiguous and unique.
Nothing outside the append knows the next number, so a chain of appends is the only
evidence that the derivation from the tip read really does produce one line.

The digest the cluster computed and the digest an independent Python recomputation
computes are the same digest. Verification never re-reads the cluster's own answer,
so an intact report means two implementations of one rule agreed on every row, and
the reported row count and terminal digest are what a checkpoint commits to.

A single altered stored field is caught at the row that carries it. The alteration
is made by a direct update outside the module under test, which is exactly the
retrospective edit the chain exists to make visible.

A Session holding no Event answers the terminal-tip query rather than failing it,
because the Checkpoint_Signer asks every Session in its window and some of them are
empty.

Only the first migration is applied, staged into a directory of its own, so the
schema under test is exactly what that generation declares and no later grant
restricts the direct update the tamper case makes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt.models.event import EmbeddingState, Event, EventCategory, JsonObject
from molt.store import Connection, MemoryStore
from molt.store.chain import (
    GENESIS_PREDECESSOR,
    MISMATCH_CONTENT,
    AppendedRow,
    LedgerAppend,
    append,
    append_batch,
    chain_tip,
    verify_chain,
)
from molt.store.migrate import apply_migrations, discover_migrations

pytestmark = pytest.mark.integration

# The only migration this module needs: the tenant table, the Session table, and
# the ledger with its two uniqueness constraints.
CORE_MIGRATION_VERSION: Final[int] = 1

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The direct writes the fixtures and the tamper case make. The module under test
# owns no tenant insert and no Session insert, and it owns no update at all, so
# each is written here and every value is bound.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
ALTER_STORED_PAYLOAD: Final[str] = "UPDATE ledger SET payload = %s::JSONB WHERE id = %s"
SELECT_STORED_SEQUENCES: Final[str] = "SELECT seq FROM ledger WHERE session_id = %s ORDER BY seq"

# How far apart two Events of a chain sit, so no two rows share an instant.
STEP_SECONDS: Final[float] = 1.5

# The retention interval an appended row expires after.
RETENTION: Final[timedelta] = timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


class Clock(Protocol):
    """The two calls a test makes on the injected clock.

    The shape is declared structurally rather than imported, because the shared
    fixtures reach a test as a plugin rather than as an importable module path.
    """

    def now(self) -> datetime:
        """The current wall reading, timezone aware."""

    def advance(self, seconds: float) -> None:
        """Move the reading forward by a non-negative number of seconds."""


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding the core migration, a store over it, and one tenant."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID


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
    """Apply the core migration, then build a store bound to that schema."""
    directory = tmp_path_factory.mktemp("molt_chain_core")
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

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema, client_id=client_id)


def new_session(cluster: Cluster) -> UUID:
    """Place one Session row, so each example owns a chain of its own."""
    session_id = uuid4()
    send(cluster.connection, INSERT_SESSION, (session_id, cluster.client_id, "agent", "machine"))
    return session_id


def build_request(
    cluster: Cluster,
    session_id: UUID,
    *,
    occurred_at: datetime,
    payload: JsonObject | None = None,
) -> LedgerAppend:
    """One append request, with a payload holding unordered and non-ASCII keys.

    The payload matters: the cluster hashes the canonical text as bytes and the
    verifier hashes the same text in Python, so content outside the ASCII range is
    what shows the two encodings agree.
    """
    return LedgerAppend(
        event=Event(
            id=uuid4(),
            session_id=session_id,
            client_id=cluster.client_id,
            category=EventCategory.TOOL_CALL,
            occurred_at=occurred_at,
            agent_cli="agent",
            machine_id="machine",
            parent_event_id=None,
            payload=payload
            if payload is not None
            else {"tool": "read", "\u00e9tape": 2, "path": "/workspace/fichier"},
            redacted=False,
            text_body="a tool call",
        ),
        expires_at=occurred_at + RETENTION,
        embedding_state=EmbeddingState.PENDING,
    )


def append_chain(
    cluster: Cluster, session_id: UUID, clock: Clock, length: int
) -> list[AppendedRow]:
    """Append a chain one statement at a time, advancing the clock between rows."""
    written: list[AppendedRow] = []
    for _ in range(length):
        written.append(
            append(cluster.store, build_request(cluster, session_id, occurred_at=clock.now()))
        )
        clock.advance(STEP_SECONDS)
    return written


# ---------------------------------------------------------------------------
# Sequence numbers the statement derived
# ---------------------------------------------------------------------------


def test_appends_derive_contiguous_unique_sequence_numbers(
    cluster: Cluster, time_source: Clock
) -> None:
    """Each append reads the tip and produces the number after it, exactly once."""
    session_id = new_session(cluster)

    written = append_chain(cluster, session_id, time_source, 4)

    assert [row.seq for row in written] == [1, 2, 3, 4]
    assert len({row.seq for row in written}) == 4
    assert written[0].prev_chain_digest == GENESIS_PREDECESSOR
    for earlier, later in pairwise(written):
        assert later.prev_chain_digest == earlier.chain_digest, "each row names one predecessor"

    with cluster.connection.cursor() as cursor:
        cursor.execute(SELECT_STORED_SEQUENCES, (session_id,))
        stored = [int(row[0]) for row in cursor.fetchall()]
    assert stored == [1, 2, 3, 4]


def test_a_batch_continues_the_chain_it_was_appended_to(
    cluster: Cluster, time_source: Clock
) -> None:
    """A batch is a loop of the same statement, so it continues rather than restarts."""
    session_id = new_session(cluster)
    first = append_chain(cluster, session_id, time_source, 1)

    requests = []
    for _ in range(3):
        requests.append(build_request(cluster, session_id, occurred_at=time_source.now()))
        time_source.advance(STEP_SECONDS)
    batched = append_batch(cluster.store, requests)

    assert [row.seq for row in batched] == [2, 3, 4]
    assert batched[0].prev_chain_digest == first[-1].chain_digest


# ---------------------------------------------------------------------------
# Verification of an intact chain
# ---------------------------------------------------------------------------


def test_an_intact_chain_reports_the_row_count_and_the_terminal_digest(
    cluster: Cluster, time_source: Clock
) -> None:
    """The cluster's digests and an independent recomputation agree on every row."""
    session_id = new_session(cluster)
    written = append_chain(cluster, session_id, time_source, 5)

    report = verify_chain(cluster.store, session_id)

    assert report.ok, f"the chain disagreed at sequence {report.first_mismatch_seq}"
    assert report.rows == 5
    assert report.first_mismatch_seq is None
    assert report.terminal_digest == written[-1].chain_digest

    tip = chain_tip(cluster.store, session_id)
    assert tip.seq == 5
    assert tip.chain_digest == report.terminal_digest
    assert not tip.empty


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def test_an_altered_stored_field_is_detected_at_its_sequence_number(
    cluster: Cluster, time_source: Clock
) -> None:
    """A retrospective edit is visible at the row it was made in."""
    session_id = new_session(cluster)
    written = append_chain(cluster, session_id, time_source, 4)
    assert verify_chain(cluster.store, session_id).ok, "the chain held before the edit"

    edited = written[2]
    send(
        cluster.connection,
        ALTER_STORED_PAYLOAD,
        (json.dumps({"tool": "write", "path": "/workspace/autre"}), edited.event_id),
    )

    report = verify_chain(cluster.store, session_id)

    assert not report.ok
    assert report.first_mismatch_seq == edited.seq
    assert report.mismatch == MISMATCH_CONTENT
    assert report.rows == edited.seq - 1, "the report says how far the chain held"
    assert report.terminal_digest == written[1].chain_digest


# ---------------------------------------------------------------------------
# The terminal tip of an empty Session
# ---------------------------------------------------------------------------


def test_the_tip_of_a_session_with_no_events_answers_the_genesis_state(
    cluster: Cluster,
) -> None:
    """A checkpoint asks every Session in its window, and some of them are empty."""
    session_id = new_session(cluster)

    tip = chain_tip(cluster.store, session_id)

    assert tip.session_id == session_id
    assert tip.empty
    assert tip.seq == 0
    assert tip.next_seq == 1
    assert tip.chain_digest == GENESIS_PREDECESSOR

    report = verify_chain(cluster.store, session_id)
    assert report.ok
    assert report.rows == 0
    assert report.terminal_digest == GENESIS_PREDECESSOR
