"""Session writes and tenancy-scoped reads against a live instance.

The unit module of the same name asserts the shape of the statements. This one
asserts the three things only a cluster can answer, because each rests on the
schema rather than on the module.

Depth is read from the parent row. A caller presenting a depth of its own gets the
derived value stored regardless of what it presented, in both directions: a caller
claiming to be deeper than its parent and a caller claiming to be shallower are
both corrected by the same expression.

A Session naming a parent that does not exist is refused by the reference on the
parent column, and refused is the requirement. This is the case a naive derivation
gets wrong by inserting nothing at all and reporting success, so the assertion
below checks both that the write failed and that no row landed.

A transaction writing a spawning Event together with the child Session it spawned
has exactly one admissible order. The reference from a Session to the Ledger is
checked per statement and is declared deferrable nowhere, so the Event goes first.
Both halves are asserted: the correct order commits, and the reverse order is
refused by the cluster rather than tolerated.

Only the first migration generation is applied, staged into a directory of its
own, so the schema under test is exactly what generations 001 through 007 declare.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.errors import MissingParentError
from molt.models.session import Session, SessionOutcome
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations, discover_migrations
from molt.store.sessions import (
    CounterDelta,
    SessionCounters,
    artifacts_of_client,
    bump_session_counters,
    child_sessions,
    end_session,
    events_of_session,
    insert_spawned_session,
    session_of_client,
    sessions_of_client,
    upsert_session,
)

pytestmark = pytest.mark.integration

# The last version of the first migration generation, which is all this module
# needs: the session table, the ledger, and the derived artifact table.
FIRST_GENERATION_LAST_VERSION: Final[int] = 7

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes the fixtures make, parameterised in full. The module under test
# owns no Client insert, no Ledger append, and no Artifact write, so the rows
# those tests need are placed here rather than through it.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_LEDGER_ROW: Final[str] = (
    "INSERT INTO ledger (id, session_id, client_id, seq, category, occurred_at, "
    "recorded_at, agent_cli, machine_id, payload, content_digest, prev_chain_digest, "
    "chain_digest, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s, %s, %s, %s)"
)
INSERT_ARTIFACT_ROW: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
COUNT_SESSION: Final[str] = "SELECT count(*) FROM session WHERE id = %s"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding the first generation, a store over it, and two tenants."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID
    other_client_id: UUID


def stage_first_generation(destination: Path) -> None:
    """Copy the first-generation migration files into a directory of their own."""
    for migration in discover_migrations():
        if migration.version <= FIRST_GENERATION_LAST_VERSION:
            destination.joinpath(migration.path.name).write_bytes(migration.path.read_bytes())


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Cluster]:
    """Apply the first generation, then build a store bound to that schema."""
    directory = tmp_path_factory.mktemp("molt_sessions_generation")
    stage_first_generation(directory)
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
    other_client_id = uuid4()
    send(fresh_schema, INSERT_CLIENT, (client_id, f"tenant-{client_id.hex[:8]}", "Tenant", "eu"))
    send(
        fresh_schema,
        INSERT_CLIENT,
        (other_client_id, f"tenant-{other_client_id.hex[:8]}", "Other tenant", "eu"),
    )

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(
            store=store,
            connection=fresh_schema,
            client_id=client_id,
            other_client_id=other_client_id,
        )


def build_session(
    client_id: UUID,
    *,
    session_id: UUID | None = None,
    parent_session_id: UUID | None = None,
    depth: int = 0,
    counters: SessionCounters | None = None,
) -> Session:
    """A Session record for one tenant, with the outcome open."""
    held = counters or SessionCounters(0, 0, 0, 0, Decimal(0))
    return Session(
        id=session_id or uuid4(),
        client_id=client_id,
        agent_cli="agent",
        machine_id="machine",
        team_id="team",
        attribution={"principal": "operator", "role": "engineer"},
        workspace_path="/workspace",
        started_at=MOMENT,
        ended_at=None,
        outcome=SessionOutcome.IN_PROGRESS,
        parent_session_id=parent_session_id,
        spawning_event_id=None,
        depth=depth,
        tool_call_count=held.tool_call_count,
        model_request_count=held.model_request_count,
        error_count=held.error_count,
        token_count=held.token_count,
        cost_usd=held.cost_usd,
        halted=False,
        halted_at=None,
        halt_reason=None,
        halt_rule_id=None,
    )


def append_ledger_row(cluster: Cluster, session_id: UUID, seq: int) -> UUID:
    """Place one Ledger row directly, returning its identifier."""
    event_id = uuid4()
    send(
        cluster.connection,
        INSERT_LEDGER_ROW,
        (
            event_id,
            session_id,
            cluster.client_id,
            seq,
            "tool_call",
            MOMENT,
            MOMENT,
            "agent",
            "machine",
            '{"tool":"read"}',
            digest_of(f"content-{event_id}"),
            digest_of(f"previous-{seq}"),
            digest_of(f"chain-{event_id}"),
            EXPIRY,
        ),
    )
    return event_id


# ---------------------------------------------------------------------------
# Depth derivation
# ---------------------------------------------------------------------------


def test_depth_is_derived_from_the_parent_row(cluster: Cluster) -> None:
    """A presented depth decides nothing; the parent's depth plus one does."""
    root = build_session(cluster.client_id)
    assert upsert_session(cluster.store, root) == 0

    overstating = build_session(cluster.client_id, parent_session_id=root.id, depth=99)
    assert upsert_session(cluster.store, overstating) == 1

    understating = build_session(cluster.client_id, parent_session_id=overstating.id, depth=0)
    assert upsert_session(cluster.store, understating) == 2

    stored = session_of_client(cluster.store, understating.id, cluster.client_id)
    assert stored is not None
    assert stored.depth == 2
    assert stored.parent_session_id == overstating.id
    assert stored.attribution == {"principal": "operator", "role": "engineer"}
    assert stored.started_at == MOMENT


def test_a_session_naming_an_absent_parent_is_refused(cluster: Cluster) -> None:
    """The reference refuses the write, and no row lands."""
    absent = uuid4()
    orphan = build_session(cluster.client_id, parent_session_id=absent, depth=1)

    with pytest.raises(MissingParentError):
        upsert_session(cluster.store, orphan)

    with cluster.connection.cursor() as cursor:
        cursor.execute(COUNT_SESSION, (orphan.id,))
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == 0, "a refused insert leaves no row behind"


def test_restating_a_session_closes_it_without_moving_lineage(cluster: Cluster) -> None:
    """A second write with the same identifier changes nothing it may not change."""
    root = build_session(cluster.client_id)
    upsert_session(cluster.store, root)
    child = build_session(cluster.client_id, parent_session_id=root.id, depth=1)
    upsert_session(cluster.store, child)

    end_session(
        cluster.store,
        child.id,
        cluster.client_id,
        outcome=SessionOutcome.SUCCEEDED,
        ended_at=MOMENT + timedelta(minutes=5),
    )
    bump_session_counters(
        cluster.store, child.id, CounterDelta(tool_calls=2), client_id=cluster.client_id
    )

    restated = build_session(
        cluster.client_id, session_id=child.id, parent_session_id=root.id, depth=1
    )
    assert upsert_session(cluster.store, restated) == 1

    stored = session_of_client(cluster.store, child.id, cluster.client_id)
    assert stored is not None
    assert stored.outcome is SessionOutcome.SUCCEEDED, "a closed Session is not reopened"
    assert stored.ended_at == MOMENT + timedelta(minutes=5), "an end timestamp is not cleared"
    assert stored.tool_call_count == 2, "a restatement does not roll a counter back"
    assert stored.depth == 1


# ---------------------------------------------------------------------------
# The spawning Event and the order it forces
# ---------------------------------------------------------------------------


def test_the_spawning_event_and_the_child_session_commit_in_that_order(
    cluster: Cluster,
) -> None:
    """Event first, Session second, in one transaction that commits."""
    parent = build_session(cluster.client_id)
    upsert_session(cluster.store, parent)
    spawning_event_id = append_ledger_row(cluster, parent.id, seq=1)
    child = build_session(cluster.client_id, parent_session_id=parent.id, depth=7)

    def append_spawning_event(cursor: object) -> UUID:
        assert hasattr(cursor, "execute")
        return spawning_event_id

    depth = insert_spawned_session(
        cluster.store, child, append_spawning_event=append_spawning_event
    )

    assert depth == 1
    stored = session_of_client(cluster.store, child.id, cluster.client_id)
    assert stored is not None
    assert stored.spawning_event_id == spawning_event_id
    assert stored.depth == 1


def test_a_session_naming_an_unwritten_spawning_event_is_refused(cluster: Cluster) -> None:
    """The reverse order is refused by the cluster, which is why the order exists."""
    child = build_session(cluster.client_id)

    with pytest.raises(MissingParentError, match="spawning Event"):
        insert_spawned_session(
            cluster.store,
            child,
            append_spawning_event=lambda _: uuid4(),
        )

    with cluster.connection.cursor() as cursor:
        cursor.execute(COUNT_SESSION, (child.id,))
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == 0


# ---------------------------------------------------------------------------
# Counters and the terminal write
# ---------------------------------------------------------------------------


def test_counters_accumulate_by_increment(cluster: Cluster) -> None:
    """Each increment adds to what is stored rather than replacing it."""
    session = build_session(cluster.client_id)
    upsert_session(cluster.store, session)

    first = bump_session_counters(
        cluster.store,
        session.id,
        CounterDelta(tool_calls=2, tokens=100, cost_usd=Decimal("0.25")),
        client_id=cluster.client_id,
    )
    second = bump_session_counters(
        cluster.store,
        session.id,
        CounterDelta(tool_calls=3, model_requests=1, errors=1, cost_usd=Decimal("0.75")),
        client_id=cluster.client_id,
    )

    assert first is not None
    assert first.tool_call_count == 2
    assert second is not None
    assert second.tool_call_count == 5
    assert second.model_request_count == 1
    assert second.error_count == 1
    assert second.token_count == 100
    assert second.cost_usd == Decimal("1.000000")


def test_an_increment_for_another_tenant_moves_nothing(cluster: Cluster) -> None:
    """A Session identifier is not authority over the row it names."""
    session = build_session(cluster.client_id)
    upsert_session(cluster.store, session)

    assert (
        bump_session_counters(
            cluster.store,
            session.id,
            CounterDelta(errors=5),
            client_id=cluster.other_client_id,
        )
        is None
    )

    stored = session_of_client(cluster.store, session.id, cluster.client_id)
    assert stored is not None
    assert stored.error_count == 0


def test_the_terminal_write_raises_counters_and_never_lowers_them(cluster: Cluster) -> None:
    """A closing total may raise a counter and cannot undo an increment."""
    session = build_session(cluster.client_id)
    upsert_session(cluster.store, session)
    bump_session_counters(
        cluster.store,
        session.id,
        CounterDelta(tool_calls=4, tokens=500, cost_usd=Decimal("2.00")),
        client_id=cluster.client_id,
    )

    lower = end_session(
        cluster.store,
        session.id,
        cluster.client_id,
        outcome=SessionOutcome.FAILED,
        ended_at=MOMENT + timedelta(minutes=1),
        counters=SessionCounters(1, 0, 0, 10, Decimal("0.10")),
    )
    assert lower is not None
    assert lower.tool_call_count == 4
    assert lower.token_count == 500
    assert lower.cost_usd == Decimal("2.000000")

    higher = end_session(
        cluster.store,
        session.id,
        cluster.client_id,
        outcome=SessionOutcome.SUCCEEDED,
        ended_at=MOMENT + timedelta(minutes=2),
        counters=SessionCounters(9, 2, 1, 900, Decimal("3.00")),
    )
    assert higher is not None
    assert higher.tool_call_count == 9
    assert higher.token_count == 900

    stored = session_of_client(cluster.store, session.id, cluster.client_id)
    assert stored is not None
    assert stored.outcome is SessionOutcome.SUCCEEDED
    assert stored.ended_at == MOMENT + timedelta(minutes=2)


def test_a_terminal_write_for_another_tenant_matches_nothing(cluster: Cluster) -> None:
    """The terminal path is scoped by tenant like every other statement here."""
    session = build_session(cluster.client_id)
    upsert_session(cluster.store, session)

    assert (
        end_session(
            cluster.store,
            session.id,
            cluster.other_client_id,
            outcome=SessionOutcome.ABANDONED,
            ended_at=MOMENT,
        )
        is None
    )

    stored = session_of_client(cluster.store, session.id, cluster.client_id)
    assert stored is not None
    assert stored.outcome is SessionOutcome.IN_PROGRESS


# ---------------------------------------------------------------------------
# Tenancy scoping of the reads
# ---------------------------------------------------------------------------


def test_a_session_read_by_the_wrong_tenant_finds_nothing(cluster: Cluster) -> None:
    """The scope is the predicate, so the wrong tenant sees no row at all."""
    session = build_session(cluster.client_id)
    upsert_session(cluster.store, session)

    assert session_of_client(cluster.store, session.id, cluster.other_client_id) is None
    assert session_of_client(cluster.store, session.id, cluster.client_id) is not None


def test_session_listing_and_child_listing_are_scoped(cluster: Cluster) -> None:
    """Each listing returns the asking tenant's rows and no other tenant's."""
    parent = build_session(cluster.client_id)
    upsert_session(cluster.store, parent)
    child = build_session(cluster.client_id, parent_session_id=parent.id, depth=1)
    upsert_session(cluster.store, child)
    stranger = build_session(cluster.other_client_id)
    upsert_session(cluster.store, stranger)

    mine = {found.id for found in sessions_of_client(cluster.store, cluster.client_id, limit=500)}
    assert parent.id in mine
    assert child.id in mine
    assert stranger.id not in mine

    children = child_sessions(cluster.store, parent.id, cluster.client_id)
    assert [found.id for found in children] == [child.id]
    assert child_sessions(cluster.store, parent.id, cluster.other_client_id) == ()


def test_the_event_read_is_scoped_by_client(cluster: Cluster) -> None:
    """A Session's stream is readable by the tenant that owns it and no other."""
    session = build_session(cluster.client_id)
    upsert_session(cluster.store, session)
    first = append_ledger_row(cluster, session.id, seq=1)
    second = append_ledger_row(cluster, session.id, seq=2)

    found = events_of_session(cluster.store, session.id, cluster.client_id)
    assert [event.id for event in found] == [first, second]
    assert [event.seq for event in found] == [1, 2]
    assert all(len(event.chain_digest) == 64 for event in found)

    assert events_of_session(cluster.store, session.id, cluster.other_client_id) == ()


def test_the_artifact_read_is_scoped_by_client(cluster: Cluster) -> None:
    """A Derived_Artifact is readable by the tenant that owns it and no other."""
    mine = uuid4()
    theirs = uuid4()
    send(
        cluster.connection,
        INSERT_ARTIFACT_ROW,
        (mine, "summary", cluster.client_id, "a summary", digest_of("mine"), "distil", EXPIRY),
    )
    send(
        cluster.connection,
        INSERT_ARTIFACT_ROW,
        (
            theirs,
            "summary",
            cluster.other_client_id,
            "a summary",
            digest_of("theirs"),
            "distil",
            EXPIRY,
        ),
    )

    found = {artifact.id for artifact in artifacts_of_client(cluster.store, cluster.client_id)}
    assert mine in found
    assert theirs not in found
