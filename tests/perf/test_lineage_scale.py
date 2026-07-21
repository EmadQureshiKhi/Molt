"""Both lineage closures terminate inside 5 seconds on a 100000-edge graph.

**Validates: Requirements 11.7**

Why the bound exists. The erasure sweep selects derived content by lineage, so a
closure that does not return is an erasure that does not complete, and the graph
a deployment accumulates grows without an upper bound. The requirement fixes the
scale the two traversals must stay usable at, and this module is the only place
that scale is actually built.

**The corpus is placed by direct statements rather than through the guarded
insert.** The guarded insert carries a recursive reachability query over the
child's descendants, so placing a hundred thousand edges through it would run a
hundred thousand reachability checks and the measurement would be dominated by
the guard. The guard is the subject of the acyclicity property, which drives
every edge through it; what is measured here is the traversal. Every edge this
module plans runs from a later position in a topological order to an earlier one,
so the graph is acyclic by construction and no edge placed here is one the guard
would have refused. The fixture's statements are whole module-level literals with
every value bound, and rows are placed in bulk over bound arrays rather than one
round trip at a time, so building the corpus costs tens of statements rather than
a hundred and eighty thousand.

**The shape is adversarial for a traversal rather than convenient for one.** A
star would satisfy the bound while proving nothing: one recursive step reaches
every node, so the walk never recurses. This graph is built from two component
shapes instead, and both are long:

- A **diamond chain** is a run of diamonds joined end to end. Each diamond leaves
  its source by two disjoint paths that meet again at its sink, so every sink is
  reachable by two paths and the deduplication has something to deduplicate at
  every level. One component spans twice as many levels as it holds diamonds.
- A **plain chain** is a run of single links, which is the deepest shape per edge
  a graph admits and the one a naive traversal degrades on.

Both components span the same number of levels, so the recursion descends that
far whichever component a walk started in.

**Both traversals are seeded from every component at once**, from the sources for
the descendant direction and from the sinks for the ancestor direction. That is
the largest question the corpus can be asked: the closure spans every node the
graph holds except the seeds themselves, so every one of the hundred thousand
edges is walked. Seeding from one component instead would walk a few hundred
edges of a hundred-thousand-edge table and would report a time that says nothing
about the scale the requirement names.

**Statistics are refreshed before the measurement.** A plan built on absent
statistics is not the plan a deployed cluster would produce, and the whole point
of the per-direction indexes is that each recursive step is a lookup join rather
than a scan. The plan is printed beside the timing so the number is interpretable
rather than merely asserted.

Each measurement also asserts what came back: the closure size, that nothing
repeated, and for the ancestor direction that every node carries the kind of the
table holding it. Without those, a traversal that returned early would post an
excellent time.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.lineage import (
    SELECT_ANCESTORS_STATEMENT,
    SELECT_DESCENDANTS_STATEMENT,
    ancestors_of,
    descendants_of,
)
from molt.store.migrate import apply_migrations

# A bound measured against a cluster: the corpus is placed by real statements and
# both traversals are served by the cluster's own plan. The performance marker says
# what is measured and the instance marker states what that measurement needs, so
# with no instance reachable this module skips at collection naming what was
# missing, while the benchmarks that need nothing beyond this process still run.
pytestmark = [pytest.mark.perf, pytest.mark.instance]

# The scale the requirement states and the bound it states for it.
EDGE_TARGET: Final[int] = 100000
TRAVERSAL_BOUND_SECONDS: Final[float] = 5.0

# How the corpus reaches that edge count. A diamond contributes four edges and
# two levels; a plain link contributes one edge and one level. Both component
# shapes therefore span the same number of levels, and the two counts multiply out
# to exactly the stated edge target.
DIAMONDS_PER_COMPONENT: Final[int] = 125
DIAMOND_COMPONENTS: Final[int] = 120
CHAIN_LENGTH: Final[int] = 2 * DIAMONDS_PER_COMPONENT
CHAIN_COMPONENTS: Final[int] = 160

# How many rows one statement places. Bulk placement is what keeps the corpus
# affordable; the batch is large enough that the whole graph is a few dozen
# statements and small enough that no single statement carries the whole array.
BATCH_ROWS: Final[int] = 5000

# The fixture's own statements. This module owns no Client insert and no Artifact
# write, so every row its graph is built from is placed here, in bulk, with every
# value bound and no identifier interpolated.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACTS_BULK: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, expires_at) "
    "SELECT unnest(%s::UUID[]), %s, %s, %s, %s, %s, %s"
)
INSERT_EDGES_BULK: Final[str] = (
    "INSERT INTO lineage_edge (child_id, parent_id, parent_kind, derivation_method) "
    "SELECT unnest(%s::UUID[]), unnest(%s::UUID[]), %s, %s"
)
COUNT_EDGES: Final[str] = "SELECT count(*) FROM lineage_edge"
COUNT_NODES: Final[str] = "SELECT count(*) FROM derived_artifact"
ANALYSE_EDGES: Final[str] = "ANALYZE lineage_edge"
ANALYSE_NODES: Final[str] = "ANALYZE derived_artifact"

# The kind every node carries, the method every edge records, and the body every
# node holds. None of them is what this module measures, so none is varied.
NODE_KIND: Final[str] = "summary"
METHOD: Final[str] = "distil"
NODE_BODY: Final[str] = "a derived body"

# A digest of the fixed width the schema declares. The column carries no
# uniqueness constraint, so one value serves every row and the corpus costs no
# per-row hashing.
NODE_DIGEST: Final[str] = "0" * 64

# An instant with an offset, derived from the epoch rather than written as a
# literal, so a run embeds nothing about when it happened.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# What the plan says when a recursive step sought into an index, and what it says
# when it read a whole table instead. The plan is lowercased before comparison.
LOOKUP_JOIN: Final[str] = "lookup join"
FULL_SCAN: Final[str] = "full scan"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# The planned graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphPlan:
    """A graph described by position, before any identifier exists.

    Every edge names a child position greater than its parent position, so a walk
    in the parent-to-child direction strictly increases the position it stands at
    and the graph is acyclic by construction.

    Attributes:
        node_count: How many nodes the graph holds.
        edges: Every edge, as a child position and a parent position.
        sources: The position of each component's source, which is the seed group
            the descendant direction is measured from.
        sinks: The position of each component's final sink, which is the seed
            group the ancestor direction is measured from.
        levels: How many levels a component spans, which is how deep the recursion
            descends before it stops finding new nodes.
    """

    node_count: int
    edges: tuple[tuple[int, int], ...]
    sources: tuple[int, ...]
    sinks: tuple[int, ...]
    levels: int


def plan_graph() -> GraphPlan:
    """Build the planned graph: diamond chains and plain chains, end to end.

    The components share no node, so each one's closure is its own, and seeding
    from every source at once is what makes the measured walk span the whole
    corpus rather than one component of it.
    """
    edges: list[tuple[int, int]] = []
    sources: list[int] = []
    sinks: list[int] = []
    position = 0

    for _ in range(DIAMOND_COMPONENTS):
        source = position
        sources.append(source)
        current = position
        position += 1
        for _ in range(DIAMONDS_PER_COMPONENT):
            left, right, sink = position, position + 1, position + 2
            position += 3
            edges.extend(
                ((left, current), (right, current), (sink, left), (sink, right)),
            )
            current = sink
        sinks.append(current)

    for _ in range(CHAIN_COMPONENTS):
        source = position
        sources.append(source)
        position += 1
        current = source
        for _ in range(CHAIN_LENGTH):
            child = position
            position += 1
            edges.append((child, current))
            current = child
        sinks.append(current)

    return GraphPlan(
        node_count=position,
        edges=tuple(edges),
        sources=tuple(sources),
        sinks=tuple(sinks),
        levels=2 * DIAMONDS_PER_COMPONENT,
    )


PLAN: Final[GraphPlan] = plan_graph()

# How large each closure is. Every node except the seeds is reachable from the
# sources, and every node except the sinks is an ancestor of the sinks, so both
# answers are the node count less one node per component.
CLOSURE_SIZE: Final[int] = PLAN.node_count - len(PLAN.sources)


# ---------------------------------------------------------------------------
# The cluster the graph is placed on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Graph:
    """A schema holding every migration, a store over it, and the placed corpus."""

    store: MemoryStore
    connection: DriverConnection
    nodes: tuple[UUID, ...]
    sources: tuple[UUID, ...]
    sinks: tuple[UUID, ...]

    def scalar(self, statement: str) -> int:
        """Read one count on the fixture's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement)
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def plan_of(self, statement: str, seeds: Sequence[UUID]) -> str:
        """The plan the cluster produces for one traversal, lowercased."""
        with self.connection.cursor() as cursor:
            cursor.execute("EXPLAIN " + statement, (list(seeds),))
            rows = cursor.fetchall()
        return "\n".join(" ".join(str(column) for column in row) for row in rows).lower()


def _send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def _place_nodes(connection: DriverConnection, nodes: Sequence[UUID], client_id: UUID) -> None:
    """Place one Derived_Artifact per node, in batches of bound arrays."""
    for start in range(0, len(nodes), BATCH_ROWS):
        batch = list(nodes[start : start + BATCH_ROWS])
        _send(
            connection,
            INSERT_ARTIFACTS_BULK,
            (batch, NODE_KIND, client_id, NODE_BODY, NODE_DIGEST, METHOD, EXPIRY),
        )


def _place_edges(
    connection: DriverConnection,
    nodes: Sequence[UUID],
    edges: Sequence[tuple[int, int]],
) -> None:
    """Place every planned edge, in batches of two bound arrays."""
    for start in range(0, len(edges), BATCH_ROWS):
        batch = edges[start : start + BATCH_ROWS]
        _send(
            connection,
            INSERT_EDGES_BULK,
            (
                [nodes[child] for child, _ in batch],
                [nodes[parent] for _, parent in batch],
                ArtifactKind.DERIVED_ARTIFACT.value,
                METHOD,
            ),
        )


@pytest.fixture(scope="module")
def graph(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Graph]:
    """Apply every migration, place the corpus, and refresh the statistics.

    Module scope pays the corpus cost once for both directions. The schema is
    created and dropped by the shared fixture this one builds on, so the corpus
    leaves nothing behind after the module finishes.
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
    _send(
        fresh_schema,
        INSERT_CLIENT,
        (client_id, f"tenant-{client_id.hex[:8]}", "Tenant", "eu"),
    )

    nodes = tuple(uuid4() for _ in range(PLAN.node_count))
    started = time.perf_counter()
    _place_nodes(fresh_schema, nodes, client_id)
    _place_edges(fresh_schema, nodes, PLAN.edges)
    placed = time.perf_counter() - started

    with fresh_schema.cursor() as cursor:
        cursor.execute(ANALYSE_NODES)
        cursor.execute(ANALYSE_EDGES)

    print(
        f"corpus: {PLAN.node_count} nodes and {len(PLAN.edges)} edges across "
        f"{len(PLAN.sources)} components of {PLAN.levels} levels, placed in "
        f"{placed:.1f} s"
    )

    with MemoryStore(connect_with=connect_with) as store:
        yield Graph(
            store=store,
            connection=fresh_schema,
            nodes=nodes,
            sources=tuple(nodes[position] for position in PLAN.sources),
            sinks=tuple(nodes[position] for position in PLAN.sinks),
        )


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _report(label: str, seconds: float, reached: int, plan: str) -> str:
    """One line naming the direction, its timing, its answer, and its plan."""
    served = LOOKUP_JOIN in plan
    return (
        f"{label}: {seconds:.3f} s for {reached} nodes over {len(PLAN.edges)} edges "
        f"and {PLAN.levels} levels, bound {TRAVERSAL_BOUND_SECONDS:.0f} s, "
        f"recursive step index-served: {served}\n{plan}"
    )


def test_the_corpus_holds_the_stated_scale(graph: Graph) -> None:
    """The graph really holds a hundred thousand edges before anything is timed."""
    assert len(PLAN.edges) == EDGE_TARGET, (
        f"the plan describes {len(PLAN.edges)} edges rather than {EDGE_TARGET}"
    )
    assert graph.scalar(COUNT_EDGES) == EDGE_TARGET, (
        "the placed edge count disagrees with the plan, so the measurement below "
        "would not be made at the stated scale"
    )
    assert graph.scalar(COUNT_NODES) == PLAN.node_count
    # A star would make both traversals trivial: one recursive step would reach
    # every node. No node here has more than two children, so reaching the far end
    # of a component takes as many steps as the component has levels.
    children: dict[int, int] = {}
    parents: dict[int, int] = {}
    for child, parent in PLAN.edges:
        children[parent] = children.get(parent, 0) + 1
        parents[child] = parents.get(child, 0) + 1
    assert max(children.values()) == 2, "the graph fans out further than a diamond does"
    assert max(parents.values()) == 2, "no node in this graph has more than two parents"
    assert PLAN.levels == CHAIN_LENGTH == 2 * DIAMONDS_PER_COMPONENT


def test_descendant_closure_within_bound(graph: Graph) -> None:
    """The parent-to-child closure over the whole corpus stays inside the bound."""
    started = time.perf_counter()
    reached = descendants_of(graph.store, graph.sources)
    elapsed = time.perf_counter() - started

    plan = graph.plan_of(SELECT_DESCENDANTS_STATEMENT, graph.sources)
    summary = _report("descendant closure", elapsed, len(reached), plan)
    print(summary)

    assert len(set(reached)) == len(reached), "the descendant closure repeated a node"
    assert len(reached) == CLOSURE_SIZE, (
        f"the descendant closure returned {len(reached)} of {CLOSURE_SIZE} nodes, "
        "so it came back truncated and the timing describes less work than the "
        "bound is stated over"
    )
    assert FULL_SCAN not in plan, f"a recursive step read a whole table: {plan}"
    assert elapsed <= TRAVERSAL_BOUND_SECONDS, summary


def test_ancestor_closure_within_bound(graph: Graph) -> None:
    """The child-to-parent closure over the whole corpus stays inside the bound."""
    started = time.perf_counter()
    reached = ancestors_of(graph.store, graph.sinks)
    elapsed = time.perf_counter() - started

    plan = graph.plan_of(SELECT_ANCESTORS_STATEMENT, graph.sinks)
    summary = _report("ancestor closure", elapsed, len(reached), plan)
    print(summary)

    identifiers = tuple(node.id for node in reached)
    assert len(set(identifiers)) == len(identifiers), "the ancestor closure repeated a node"
    assert len(identifiers) == CLOSURE_SIZE, (
        f"the ancestor closure returned {len(identifiers)} of {CLOSURE_SIZE} nodes, "
        "so it came back truncated and the timing describes less work than the "
        "bound is stated over"
    )
    assert all(node.kind is ArtifactKind.DERIVED_ARTIFACT for node in reached), (
        "every node of this graph is a derived artifact, and an ancestor carries "
        "the kind of the table that holds it"
    )
    assert FULL_SCAN not in plan, f"a recursive step read a whole table: {plan}"
    assert elapsed <= TRAVERSAL_BOUND_SECONDS, summary
