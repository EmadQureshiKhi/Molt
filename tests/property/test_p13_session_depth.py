"""Property 13: a stored nesting depth follows the parent row, not the caller.

**Validates: Requirements 9.3, 9.4, 1.4**

This property needs the cluster, and that is the whole point. Half of the depth
invariant is a check constraint and half is an expression inside the inserting
statement, and neither half exists anywhere in Python: a forest built in memory
would be evidence about a reimplementation rather than about the stored graph a
reviewer queries. The module is therefore marked so it gates on a reachable
instance and is deselected from the credential-free workflow, like every other
suite that needs one.

Five decisions shape what is asserted.

**The depth every Session presents is drawn, and about half the drawn values
disagree with the parent.** That is the load-bearing claim of the module under
test: a caller does not decide where a Session sits in the tree, the parent row
does. A property that only ever presented the correct depth would pass against an
implementation that bound the presented value straight into the column, so the
generator draws a presented depth over a range wider than the tree can reach and
lets it be too deep, too shallow, or right. A Session naming no parent is the one
node whose presented depth is always zero, because the model refuses a root at any
other depth before a statement is ever sent, and that half of the invariant is a
check constraint on the table besides.

**The reference depth is walked in Python from the drawn tree, not read back from
a second query.** Asserting that the store agrees with itself would assert
nothing, so the expected depth of every node is computed from the parent links the
generator drew and the stored value is compared against that. The parent-plus-one
relation is then asserted a second way, over stored rows alone, so the invariant is
shown to hold of the graph as stored and not only of the model.

**Both Session creation paths the store exposes are exercised.** Neither the
capture layer nor the Collector is built yet, so neither of those components can
be driven from here; what exists, and what both of them will write through, are
the two Session entry points of the session module, and a drawn Session takes one
or the other. The first is the plain metadata write, which is the shape a reported
Session record takes, carrying its own parent identifier and its own claimed
depth. The second is the spawning write, which places the spawning Event and the
child Session it spawned inside one transaction in the one order the cluster
admits, because the reference between them is checked per statement. A root
Session is written through the first path only: a spawning Event names the Session
it was observed in, so a Session with no parent has no such Event to be paired
with.

**Each example owns a tenant, so the forest is read back in one query.** A tenant
of its own is what makes the single tenancy-scoped listing total: it returns that
example's whole forest and nothing another example wrote, so thirty Session writes
cost one read rather than thirty.

The example budget is 100 with no per-example deadline, matching the two other
database-backed properties. Per-example cost is one tenant insert, up to thirty
real Session writes each in a transaction of its own, an Event append for every
Session drawn onto the spawning path, and one listing; that swings by more than an
order of magnitude with the drawn Session count, so a deadline would fail a
thirty-Session example for being large rather than for being wrong. A hundred
examples finish well inside a minute against a local instance. Where a budget had
to give, it was the budget; no assertion was.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.event import EmbeddingState, Event, EventCategory, JsonObject
from molt.models.session import Session, SessionOutcome
from molt.store import Connection, Cursor, MemoryStore
from molt.store.chain import LedgerAppend, append_in_transaction
from molt.store.migrate import apply_migrations, discover_migrations
from molt.store.sessions import insert_spawned_session, sessions_of_client, upsert_session

pytestmark = pytest.mark.integration

# How many examples the property runs, and the bounds of a drawn spawn tree. The
# reasoning behind the budget is in the module docstring.
MAX_EXAMPLES: Final[int] = 100
MIN_SESSIONS: Final[int] = 1
MAX_SESSIONS: Final[int] = 30

# The deepest a drawn tree nests. A node already at this depth is ineligible to be
# anybody's parent, so the cap is a property of the generator rather than something
# the assertions have to tolerate.
MAX_DEPTH: Final[int] = 6

# The widest depth a caller may claim. It sits above the cap so that a node at any
# reachable depth has admissible values both above and below its own to be drawn,
# which is what makes an overstatement and an understatement reachable everywhere
# in the tree.
PRESENTED_CEILING: Final[int] = MAX_DEPTH + 3

# The migration generation this module applies: the tenant table, the Session table
# with its two depth constraints, and the ledger a spawning Event lands in.
CORE_MIGRATION_VERSION: Final[int] = 1

# The fixture's own statement. The module under test owns no tenant insert, so it
# is written here with every value bound and no identifier interpolated.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)

# The instant the first Session of a forest starts at and how far apart two
# Sessions sit. The reading is derived from the epoch rather than written as a
# literal, so no example embeds a calendar value.
BASE_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
STEP: Final[timedelta] = timedelta(seconds=1)

# The retention interval an appended row expires after.
RETENTION: Final[timedelta] = timedelta(days=90)

# The fields every written Session and every appended Event share, because none of
# them is what this property is about.
AGENT_CLI: Final[str] = "agent"
MACHINE_ID: Final[str] = "machine"
TEAM_ID: Final[str] = "team"
WORKSPACE_PATH: Final[str] = "/workspace"
ATTRIBUTION: Final[JsonObject] = {"principal": "operator"}

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


class CreationPath(StrEnum):
    """Which of the two Session entry points of the store writes a node.

    The names say what each one does rather than which component calls it, because
    neither calling component is built yet. The metadata write is the shape a
    reported Session record takes, carrying its own lineage claim; the spawning
    write is the Event-then-Session transaction a spawned Session requires.
    """

    METADATA_WRITE = "metadata_write"
    SPAWNING_WRITE = "spawning_write"


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpawnNode:
    """One Session of a drawn tree.

    Attributes:
        parent_index: The position of this node's parent in the drawn sequence,
            always below this node's own position, or None for a root. Drawing it
            as an earlier position is what makes the write order admissible
            without a sort: a parent row exists by the time its child is written.
        presented_depth: The depth this node's record claims. It is drawn rather
            than derived, and it disagrees with the parent about half the time.
        path: Which entry point writes this node.
    """

    parent_index: int | None
    presented_depth: int
    path: CreationPath


@dataclass(frozen=True, slots=True)
class SpawnTree:
    """A forest of 1 to 30 Sessions, in the order they are written."""

    nodes: tuple[SpawnNode, ...]

    @property
    def size(self) -> int:
        """How many Sessions this tree holds."""
        return len(self.nodes)

    @property
    def spawning_writes(self) -> int:
        """How many of its Sessions take the spawning path."""
        return sum(1 for node in self.nodes if node.path is CreationPath.SPAWNING_WRITE)


def reference_depths(nodes: tuple[SpawnNode, ...]) -> tuple[int, ...]:
    """Walk a drawn tree and return the depth every node ought to hold.

    This is the independent model the stored values are compared against: a root
    sits at zero and every other node sits one below the parent it names. Every
    parent is at an earlier position than its child, so one forward pass suffices
    and no node is read before it has been computed.
    """
    depths: list[int] = []
    for node in nodes:
        depths.append(0 if node.parent_index is None else depths[node.parent_index] + 1)
    return tuple(depths)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def parent_choices(depths: list[int]) -> st.SearchStrategy[int | None]:
    """Draw the parent of the next node from the nodes eligible to be one.

    A node already at the cap is ineligible, so no drawn tree nests past it. Three
    branches are offered rather than one: no parent at all, any eligible node, and
    the deepest eligible node. The third is what makes the cap reachable with
    thirty nodes at all, because a parent drawn uniformly is usually a shallow one
    and a tree built that way rarely gets far down.
    """
    eligible = [index for index, depth in enumerate(depths) if depth < MAX_DEPTH]
    if not eligible:
        return st.none()
    deepest = max(eligible, key=lambda index: depths[index])
    return st.one_of(st.none(), st.sampled_from(eligible), st.just(deepest))


def presented_depths(true_depth: int) -> st.SearchStrategy[int]:
    """Draw the depth a record claims: either the true one or any other.

    The disagreeing branch samples from every other admissible value rather than
    applying an offset, so an understatement and an overstatement are equally
    reachable and the drawn value bears no relation to the true one that an
    implementation could accidentally exploit.
    """
    if true_depth == 0:
        # The model refuses a root at any depth other than zero, so there is no
        # disagreeing value to draw for one. That half of the invariant is a check
        # constraint on the table as well.
        return st.just(0)
    disagreeing = [value for value in range(PRESENTED_CEILING + 1) if value != true_depth]
    return st.one_of(st.just(true_depth), st.sampled_from(disagreeing))


def creation_paths(is_root: bool) -> st.SearchStrategy[CreationPath]:
    """Draw the entry point a node is written through.

    A root takes the metadata write alone. The spawning write pairs a Session with
    the Event that spawned it, and an Event names the Session it was observed in,
    so a Session with no parent has no Event of another Session to be paired with.
    """
    if is_root:
        return st.just(CreationPath.METADATA_WRITE)
    return st.sampled_from(CreationPath)


@st.composite
def spawn_trees(draw: st.DrawFn) -> SpawnTree:
    """Draw 1 to 30 Sessions forming a forest nesting no deeper than six levels.

    The size is drawn first and each node's parent is drawn from the nodes already
    placed, so every tree is admissible by construction: a parent exists before its
    child is written, no cycle is expressible, and the cap is held by the
    eligibility filter rather than by discarding examples.
    """
    size = draw(st.integers(min_value=MIN_SESSIONS, max_value=MAX_SESSIONS))
    nodes: list[SpawnNode] = []
    depths: list[int] = []
    for _ in range(size):
        parent_index = draw(parent_choices(depths))
        true_depth = 0 if parent_index is None else depths[parent_index] + 1
        nodes.append(
            SpawnNode(
                parent_index=parent_index,
                presented_depth=draw(presented_depths(true_depth)),
                path=draw(creation_paths(parent_index is None)),
            )
        )
        depths.append(true_depth)
    return SpawnTree(nodes=tuple(nodes))


# ---------------------------------------------------------------------------
# The cluster the forest is stored on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding the core migration and a store bound to it."""

    store: MemoryStore
    connection: DriverConnection


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

    Module scope is what keeps the schema cost paid once: examples are isolated
    from each other by a tenant of their own rather than by a schema of their own.
    """
    directory = tmp_path_factory.mktemp("molt_p13_core")
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

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


def new_client(cluster: Cluster) -> UUID:
    """Place one tenant row, so each example owns a forest of its own."""
    client_id = uuid4()
    send(
        cluster.connection,
        INSERT_CLIENT,
        (client_id, f"tenant-{client_id.hex[:12]}", "Tenant", "eu"),
    )
    return client_id


# ---------------------------------------------------------------------------
# Writing a drawn tree
# ---------------------------------------------------------------------------


def build_record(
    client_id: UUID,
    *,
    session_id: UUID,
    parent_session_id: UUID | None,
    presented_depth: int,
    started_at: datetime,
) -> Session:
    """A Session record claiming the drawn depth, with its counters at rest."""
    return Session(
        id=session_id,
        client_id=client_id,
        agent_cli=AGENT_CLI,
        machine_id=MACHINE_ID,
        team_id=TEAM_ID,
        attribution=dict(ATTRIBUTION),
        workspace_path=WORKSPACE_PATH,
        started_at=started_at,
        ended_at=None,
        outcome=SessionOutcome.IN_PROGRESS,
        parent_session_id=parent_session_id,
        spawning_event_id=None,
        depth=presented_depth,
        tool_call_count=0,
        model_request_count=0,
        error_count=0,
        token_count=0,
        cost_usd=Decimal(0),
        halted=False,
        halted_at=None,
        halt_reason=None,
        halt_rule_id=None,
    )


def spawning_request(client_id: UUID, parent_session_id: UUID, moment: datetime) -> LedgerAppend:
    """The Event a spawning write records before the child Session it spawned.

    The Event belongs to the parent Session, because that is where a subagent spawn
    is observed, and the parent row exists by the time this is appended.
    """
    return LedgerAppend(
        event=Event(
            id=uuid4(),
            session_id=parent_session_id,
            client_id=client_id,
            category=EventCategory.TOOL_CALL,
            occurred_at=moment,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            parent_event_id=None,
            payload={"tool": "spawn_subagent"},
            redacted=False,
            text_body="a subagent spawn",
        ),
        expires_at=moment + RETENTION,
        embedding_state=EmbeddingState.PENDING,
    )


def spawning_appender(request: LedgerAppend) -> Callable[[Cursor], UUID]:
    """Bind one append request into the callable the spawning write invokes first."""

    def append_spawning_event(cursor: Cursor) -> UUID:
        return append_in_transaction(cursor, request).event_id

    return append_spawning_event


@dataclass(frozen=True, slots=True)
class WrittenNode:
    """One written Session: its identifier, the depth reported, and its Event."""

    session_id: UUID
    reported_depth: int
    spawning_event_id: UUID | None


def write_tree(cluster: Cluster, client_id: UUID, tree: SpawnTree) -> tuple[WrittenNode, ...]:
    """Write a drawn forest through both entry points and report what came back.

    Nodes are written in the order they were drawn, which is an admissible order
    because every parent sits at an earlier position than its child.
    """
    written: list[WrittenNode] = []
    for index, node in enumerate(tree.nodes):
        parent = None if node.parent_index is None else written[node.parent_index].session_id
        moment = BASE_INSTANT + index * STEP
        record = build_record(
            client_id,
            session_id=uuid4(),
            parent_session_id=parent,
            presented_depth=node.presented_depth,
            started_at=moment,
        )
        spawning_event_id: UUID | None = None
        if node.path is CreationPath.METADATA_WRITE:
            reported = upsert_session(cluster.store, record)
        else:
            assert parent is not None, "the spawning path is drawn for a child alone"
            request = spawning_request(client_id, parent, moment)
            spawning_event_id = request.event.id
            reported = insert_spawned_session(
                cluster.store, record, append_spawning_event=spawning_appender(request)
            )
        written.append(
            WrittenNode(
                session_id=record.id,
                reported_depth=reported,
                spawning_event_id=spawning_event_id,
            )
        )
    return tuple(written)


def size_band(size: int) -> str:
    """Which part of the size range an example drew, for the coverage record."""
    if size == MIN_SESSIONS:
        return "1"
    if size <= 8:
        return "2-8"
    if size <= 20:
        return "9-20"
    return f"21-{MAX_SESSIONS}"


def spawning_band(count: int) -> str:
    """How much of an example took the spawning path, for the coverage record."""
    if count == 0:
        return "none"
    if count <= 4:
        return "1-4"
    return "5+"


# Feature: molt, Property 13: For any Session spawn tree, every Session's nesting depth
# equals its parent's nesting depth plus 1 and every root Session has depth 0, whether
# the capture layer or the Collector created that Session.
#
# The design states that last clause in the passive voice. It is restated in the active
# voice here for one reason only: the metadata-hygiene gate refuses the passive phrasing
# as an attribution phrase. Nothing of the claim moved.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(tree=spawn_trees())
def test_every_stored_depth_follows_the_parent_row_whatever_the_caller_claimed(
    cluster: Cluster, tree: SpawnTree
) -> None:
    expected = reference_depths(tree.nodes)
    misstated = sum(
        1 for index, node in enumerate(tree.nodes) if node.presented_depth != expected[index]
    )
    event(f"sessions={size_band(tree.size)}")
    event(f"deepest={max(expected)}")
    event(f"spawning writes={spawning_band(tree.spawning_writes)}")
    event(f"misstated depths={spawning_band(misstated)}")

    client_id = new_client(cluster)
    written = write_tree(cluster, client_id, tree)

    # The depth each write reported is the modelled depth, for both entry points.
    for index, node in enumerate(tree.nodes):
        assert written[index].reported_depth == expected[index], (
            f"the {node.path} of node {index} reported depth "
            f"{written[index].reported_depth} where the tree places it at {expected[index]}"
        )

    # One tenancy-scoped read returns the whole forest, because the tenant is this
    # example's own and the drawn size is inside the bound.
    stored = {
        found.id: found
        for found in sessions_of_client(cluster.store, client_id, limit=MAX_SESSIONS)
    }
    assert len(stored) == tree.size, "every drawn Session landed exactly once"

    for index, node in enumerate(tree.nodes):
        row = stored[written[index].session_id]
        parent = None if node.parent_index is None else written[node.parent_index].session_id

        # The lineage the row holds is the lineage the tree drew, without which the
        # depth assertions below would be about some other graph.
        assert row.parent_session_id == parent
        assert row.spawning_event_id == written[index].spawning_event_id

        # Requirements 9.3 and 9.4, against the independently walked model.
        assert row.depth == expected[index], (
            f"node {index} is stored at depth {row.depth} where the tree places it "
            f"at {expected[index]}"
        )
        assert row.depth <= MAX_DEPTH

        if parent is None:
            assert row.depth == 0, "a Session naming no parent sits at depth zero"
            continue

        # The same invariant again over stored rows alone: the parent-plus-one
        # relation holds of the graph as stored, not only of the model.
        assert row.depth == stored[parent].depth + 1, (
            f"node {index} is stored at depth {row.depth} and its stored parent at "
            f"{stored[parent].depth}"
        )

        # Requirement 1.4: the stored depth is the parent's plus one whatever the
        # record claimed, so a claim that disagreed was not what got stored.
        if node.presented_depth != expected[index]:
            assert row.depth != node.presented_depth, (
                f"node {index} claimed depth {node.presented_depth} and the stored "
                "depth followed the claim rather than the parent row"
            )
