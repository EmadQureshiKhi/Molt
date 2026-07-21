"""Property 4: both closures equal an independent traversal written in Python.

**Validates: Requirements 11.5, 11.6, 11.7, 16.5**

This property needs the cluster. The two closures are recursive statements the
cluster evaluates, and the whole claim is that its evaluation of them returns the
transitive closure and nothing else, so a reimplementation walked here would be
evidence about the reimplementation. The module is marked so it gates on a
reachable instance and is deselected from the credential-free workflow, exactly
like every other suite that needs one.

Five decisions shape what is asserted.

**The reference traversal shares nothing with the statements it judges.** Each
direction is compared against a frontier expansion over an adjacency mapping the
generator's own edge list is loaded into, level by level, in Python. It is never
compared against the store's other direction: two statements that had made the
same mistake in mirror image would agree with each other and disagree with the
truth, and a comparison between them would report nothing. The reference also
answers the depth it needed, which is what an example records to show a long
chain was really walked rather than merely generated.

**Diamonds are placed deliberately, and the answer is asserted as a sequence
rather than as a set.** A diamond is four nodes where two disjoint paths leave one
node and meet again, so its sink is reachable from its source by two paths. A
traversal combining its recursive terms with `UNION ALL` would return such a node
once per path, and comparing sets alone would hide that: every returned sequence
is therefore asserted to hold no repeat before its content is compared. The
generator plants whole diamonds by construction rather than hoping the random
edges form one, and the drawn extra edges plant many more.

**A closure is asserted to arrive whole.** The module takes no row bound on
either traversal on purpose, because a closure truncated at some number of rows is
not a closure and the erasure sweep that selects derived content by lineage would
then leave rows behind. So the count is asserted against the reference count and
not only the membership, and the seed groups include every source of the graph at
once, whose descendant closure is the largest answer the example can ask for.

**The corpus is placed by the fixture, not by the module under test.** Every
edge the generator plans runs from a later position in a topological order to an
earlier one, so the planned graph is acyclic by construction and no proposed edge
is one the guard would have refused. Placing them through the guarded insert would
cost one transaction per edge and would put an example's cost in the insert rather
than in the traversal this property is about; the guard itself is the subject of
the acyclicity property, which drives every edge through it. The fixture's own
statements are whole module-level literals with every value bound, the same
discipline the source is held to.

**Nodes are Derived_Artifacts throughout.** A child is always one, which the
schema enforces with a reference, so a graph whose interior nodes are both child
and parent has no other choice. That the kind travels back with every ancestor is
asserted too, since the ancestor traversal carries it for a caller that must know
which table holds a polymorphic parent.

The example budget is 100 with no per-example deadline, which is the floor every
property in this plan runs at. What was tuned instead is the size distribution.
Node counts are drawn from three bands with the branch weighting five to three to
two, so the cheap band is favoured while the upper band still runs to the five
hundred nodes the property is stated over. A flat draw over one to five hundred
would have spent nearly every example placing hundreds of rows to ask the same ten
or twelve questions, and on a shared instance that is minutes of insert for no
additional coverage. Weighted this way a run places its graphs and asks its
questions in something between ten seconds and a minute depending on how many
upper-band graphs it happens to draw, the recorded bands show that graphs above
two hundred and fifty nodes and closures above two hundred and fifty nodes are
both reached, and the whole file including the migrations stays inside two
minutes. Where a budget had to give, it was the budget; no assertion was.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.lineage import ancestors_of, descendants_of
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# How many examples the property runs. The reasoning behind this number and
# behind the size distribution below is in the module docstring.
MAX_EXAMPLES: Final[int] = 100

# The node-count bands and how many branches each gets. Weighting a choice by
# repeating a branch is what keeps most examples cheap while the upper band still
# reaches the five hundred nodes the property is stated over: five branches in ten
# draw from the first band, three from the second, and two from the third.
NODE_BANDS: Final[tuple[tuple[int, int], ...]] = ((1, 24), (25, 120), (121, 500))
BAND_SHARES: Final[tuple[int, ...]] = (5, 3, 2)

# How many whole diamonds a plan may plant, how many nodes one takes, and the
# smallest graph one is planted in. The floor is above the four corners a diamond
# occupies so that four distinct positions are drawn without the draw being
# rejected repeatedly for landing on a position already taken; below it the drawn
# density edges are what produce the several-path shapes.
MAX_DIAMONDS: Final[int] = 4
DIAMOND_NODES: Final[int] = 4
DIAMOND_MIN_NODES: Final[int] = 8

# How many nodes a drawn seed group may name.
MAX_PROBE_SEEDS: Final[int] = 8

# The fixture's own statements. This module owns no tenant insert and no Artifact
# write, so each row its graphs are built from is placed here with every value
# bound and no identifier interpolated.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACT_ROW: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
INSERT_EDGE_ROW: Final[str] = (
    "INSERT INTO lineage_edge (child_id, parent_id, parent_kind, derivation_method) "
    "VALUES (%s, %s, %s, %s)"
)

# The kind every node of every graph carries, and the derivation method every
# edge records. Neither is what this property is about, so neither is drawn.
NODE_KIND: Final[str] = "summary"
METHOD: Final[str] = "distil"
NODE_BODY: Final[str] = "a derived body"

# An instant with an offset, derived from the epoch rather than written as a
# literal, so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any

# The adjacency shape both reference walks read.
Adjacency = Mapping[UUID, frozenset[UUID]]


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DagPlan:
    """A directed acyclic graph, described by position in a topological order.

    Every edge is a pair of positions whose child position is the greater of the
    two, which is what makes the plan acyclic by construction: a walk in the
    parent-to-child direction strictly increases the position it stands at, so it
    cannot return to where it started.

    Attributes:
        node_count: How many nodes the graph holds.
        chain_length: How many nodes the planted chain spans, from position zero
            upwards through consecutive positions, so the ancestor walk from its
            far end has exactly that many levels less one to descend.
        edges: Every edge, as a child position and a parent position.
        diamond_sinks: The sink position of each planted diamond, each of which is
            reachable from that diamond's source by two disjoint paths.
        probes: A drawn seed group, as positions.
    """

    node_count: int
    chain_length: int
    edges: tuple[tuple[int, int], ...]
    diamond_sinks: tuple[int, ...]
    probes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Reference:
    """What the independent traversal found: the reachable set and its depth."""

    reached: frozenset[UUID]
    depth: int


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def node_counts() -> st.SearchStrategy[int]:
    """Draw a node count from the weighted bands the module docstring explains."""
    branches: list[st.SearchStrategy[int]] = []
    for (low, high), share in zip(NODE_BANDS, BAND_SHARES, strict=True):
        branches.extend([st.integers(min_value=low, max_value=high)] * share)
    return st.one_of(*branches)


@st.composite
def dags(draw: st.DrawFn) -> DagPlan:
    """Draw a directed acyclic graph holding a long chain and whole diamonds.

    Three sources of edges combine. A chain of consecutive positions gives the
    recursion a depth to descend that no single step reaches the end of. A drawn
    number of diamonds gives the deduplication something to deduplicate, since
    each one's sink is reachable from its source by two paths. A drawn list of
    position pairs gives the density, each pair oriented so the greater position
    is the child, which keeps the graph acyclic however the pair fell.
    """
    count = draw(node_counts())
    chain_length = draw(st.integers(min_value=1, max_value=count))

    edges: set[tuple[int, int]] = {(position, position - 1) for position in range(1, chain_length)}

    sinks: list[int] = []
    if count >= DIAMOND_MIN_NODES:
        for _ in range(draw(st.integers(min_value=0, max_value=MAX_DIAMONDS))):
            corners = sorted(
                draw(
                    st.sets(
                        st.integers(min_value=0, max_value=count - 1),
                        min_size=DIAMOND_NODES,
                        max_size=DIAMOND_NODES,
                    )
                )
            )
            source, left, right, sink = corners
            edges |= {(left, source), (right, source), (sink, left), (sink, right)}
            sinks.append(sink)

    positions = st.integers(min_value=0, max_value=count - 1)
    for first, second in draw(st.lists(st.tuples(positions, positions), max_size=count)):
        if first != second:
            edges.add((max(first, second), min(first, second)))

    probes = draw(
        st.lists(
            positions,
            min_size=1,
            max_size=min(MAX_PROBE_SEEDS, count),
            unique=True,
        )
    )
    return DagPlan(
        node_count=count,
        chain_length=chain_length,
        edges=tuple(sorted(edges)),
        diamond_sinks=tuple(sorted(set(sinks))),
        probes=tuple(probes),
    )


# ---------------------------------------------------------------------------
# The independent traversal
# ---------------------------------------------------------------------------


def successors(seeds: Sequence[UUID], adjacency: Adjacency) -> set[UUID]:
    """Every node one step from any seed."""
    reached: set[UUID] = set()
    for seed in seeds:
        reached |= adjacency.get(seed, frozenset())
    return reached


def reference_closure(seeds: Sequence[UUID], adjacency: Adjacency) -> Reference:
    """The transitive closure of the seeds, and how many levels it took.

    A frontier expansion, one level at a time, sharing nothing with the recursive
    statements it is compared against: no seed is in the answer unless some path
    leads back to it, which is what the statements return as well since their seed
    rows are not part of their result.
    """
    reached: set[UUID] = set()
    frontier = successors(seeds, adjacency)
    depth = 0
    while frontier:
        reached |= frontier
        depth += 1
        frontier = successors(sorted(frontier), adjacency) - reached
    return Reference(reached=frozenset(reached), depth=depth)


def adjacency_of(
    nodes: Sequence[UUID], edges: Sequence[tuple[int, int]]
) -> tuple[Adjacency, Adjacency]:
    """Load the planned edges into one mapping per direction."""
    children: dict[UUID, set[UUID]] = {node: set() for node in nodes}
    parents: dict[UUID, set[UUID]] = {node: set() for node in nodes}
    for child, parent in edges:
        children[nodes[parent]].add(nodes[child])
        parents[nodes[child]].add(nodes[parent])
    return (
        {node: frozenset(reached) for node, reached in children.items()},
        {node: frozenset(reached) for node, reached in parents.items()},
    )


# ---------------------------------------------------------------------------
# The cluster the graphs are placed on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Graph:
    """A schema holding every migration, a store over it, and one tenant."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID

    def place_nodes(self, count: int) -> tuple[UUID, ...]:
        """Place one Derived_Artifact per node, returned in topological order.

        Each identifier is minted rather than derived from its position, so the
        order the rows are stored in carries no trace of the topological order and
        a traversal cannot appear to work by reading rows in the order they were
        written.
        """
        nodes = tuple(uuid4() for _ in range(count))
        send_many(
            self.connection,
            INSERT_ARTIFACT_ROW,
            [
                (
                    node,
                    NODE_KIND,
                    self.client_id,
                    NODE_BODY,
                    digest_of(f"node-{node}"),
                    METHOD,
                    EXPIRY,
                )
                for node in nodes
            ],
        )
        return nodes

    def place_edges(self, nodes: Sequence[UUID], edges: Sequence[tuple[int, int]]) -> None:
        """Place every planned edge as one batch of the fixture's own statement."""
        send_many(
            self.connection,
            INSERT_EDGE_ROW,
            [
                (nodes[child], nodes[parent], ArtifactKind.DERIVED_ARTIFACT.value, METHOD)
                for child, parent in edges
            ],
        )


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes in width."""
    return hashlib.sha256(label.encode()).hexdigest()


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def send_many(
    connection: DriverConnection,
    statement: str,
    rows: Sequence[tuple[object, ...]],
) -> None:
    """Send one statement once per row, as a batch, with every value bound.

    A batch is what keeps an example's cost in the traversal rather than in the
    placement: five hundred nodes and their edges are placed in two batches rather
    than in a thousand round trips.
    """
    if not rows:
        return
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)


@pytest.fixture(scope="module")
def graph(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Graph]:
    """Apply every migration, then build a store bound to that schema.

    Module scope is what keeps the schema cost paid once. Examples are isolated
    from each other by minting nodes of their own rather than by a schema of their
    own, so each example's graph is a component nothing else reaches, and the
    accumulated table the later examples traverse is larger than the graph they
    ask about, which is the interesting way round.
    """
    apply_migrations(fresh_schema)

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
        yield Graph(store=store, connection=fresh_schema, client_id=client_id)


# ---------------------------------------------------------------------------
# The seed groups an example asks about
# ---------------------------------------------------------------------------


def probe_groups(
    plan: DagPlan,
    nodes: Sequence[UUID],
    children: Adjacency,
    parents: Adjacency,
) -> tuple[tuple[str, tuple[UUID, ...]], ...]:
    """The seed groups both directions are asked about, each named for the record.

    The chain ends drive the depth, the diamond sinks drive the deduplication, the
    sources and the sinks drive the largest answer the graph can produce in each
    direction, and the drawn group drives everything the first four did not think
    of.
    """
    groups: list[tuple[str, tuple[UUID, ...]]] = [
        ("chain root", (nodes[0],)),
        ("chain end", (nodes[plan.chain_length - 1],)),
        ("drawn", tuple(nodes[position] for position in plan.probes)),
        ("sources", tuple(node for node in nodes if not parents[node])),
        ("sinks", tuple(node for node in nodes if not children[node])),
    ]
    if plan.diamond_sinks:
        groups.append(("diamond sinks", tuple(nodes[position] for position in plan.diamond_sinks)))
    return tuple(groups)


def size_band(size: int) -> str:
    """Which part of the size range a count fell in, for the coverage record."""
    if size == 0:
        return "0"
    if size <= 8:
        return "1-8"
    if size <= 64:
        return "9-64"
    if size <= 256:
        return "65-256"
    return "257+"


# Feature: molt, Property 4: For any directed acyclic Lineage_Graph of up to 500
# nodes, the result of the recursive descendant query equals the transitive
# closure computed by an independent reference traversal implemented in Python,
# and the ancestor query result equals the reverse closure.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(plan=dags())
def test_both_closures_equal_an_independent_reference_traversal(
    graph: Graph, plan: DagPlan
) -> None:
    event(f"nodes={size_band(plan.node_count)}")
    event(f"edges={size_band(len(plan.edges))}")
    event(f"diamonds={len(plan.diamond_sinks)}")

    nodes = graph.place_nodes(plan.node_count)
    graph.place_edges(nodes, plan.edges)
    children, parents = adjacency_of(nodes, plan.edges)

    for label, seeds in probe_groups(plan, nodes, children, parents):
        # The parent-to-child direction, against the reference walk over the
        # planned edges rather than against the other direction's answer.
        downwards = reference_closure(seeds, children)
        found = descendants_of(graph.store, seeds)
        assert len(set(found)) == len(found), (
            f"the descendant closure from the {label} repeated a node, so a node "
            "reachable by several paths was walked once per path"
        )
        assert set(found) == downwards.reached, f"the descendant closure from the {label} disagreed"
        assert len(found) == len(downwards.reached), (
            f"the descendant closure from the {label} returned {len(found)} of "
            f"{len(downwards.reached)} node(s), so it came back truncated"
        )

        # The child-to-parent direction, against the reverse walk over the same
        # planned edges, and every ancestor carrying the kind that holds it.
        upwards = reference_closure(seeds, parents)
        reached = ancestors_of(graph.store, seeds)
        ancestor_ids = tuple(node.id for node in reached)
        assert all(node.kind is ArtifactKind.DERIVED_ARTIFACT for node in reached), (
            "every node of this graph is a derived artifact, and an ancestor "
            "carries the kind of the table that holds it"
        )
        assert len(set(ancestor_ids)) == len(ancestor_ids), (
            f"the ancestor closure from the {label} repeated a node"
        )
        assert set(ancestor_ids) == upwards.reached, (
            f"the ancestor closure from the {label} disagreed"
        )
        assert len(ancestor_ids) == len(upwards.reached), (
            f"the ancestor closure from the {label} returned {len(ancestor_ids)} of "
            f"{len(upwards.reached)} node(s), so it came back truncated"
        )

        if label == "chain root":
            event(f"descendant depth={size_band(downwards.depth)}")
            event(f"descendant closure={size_band(len(downwards.reached))}")
        if label == "chain end":
            event(f"ancestor depth={size_band(upwards.depth)}")
        if label == "sources":
            event(f"widest descendant closure={size_band(len(downwards.reached))}")
