"""The recursive traversals against a live instance: what `UNION` is load-bearing for.

Two neighbouring suites already walk this graph. `test_lineage_graph_guard.py`
asserts the guard, the refusals, and the agreement of both traversals with an
independent breadth-first walk over a twelve-node graph holding diamonds. The
lineage closure property compares both directions against a reference traversal
over graphs of up to five hundred nodes. Both compare **sets** of reached nodes,
and a set comparison is exactly what cannot see the claim this module makes.

The recursive terms combine with `UNION` rather than `UNION ALL`. On a graph where
a node is reachable by many paths those two produce the same set and wildly
different row counts, so the difference is invisible to a set comparison and
visible only in what the cluster returned. It matters twice over: the erasure sweep
that selects derived content by lineage would do its work once per path rather than
once per node, and the time bound of Requirement 11.7 would be a bound on the
number of paths rather than on the size of the graph. The number of paths is
exponential in the depth of a lattice while the number of nodes is linear in it,
which is what makes the difference between the two a difference in kind.

So this module builds a lattice whose distinct path count is counted independently
in Python, and asserts that each traversal returns one row per reachable node
while the paths into the deepest node number in the thousands.

The second claim is that neither traversal is bounded by a row limit. Every other
read statement in the store carries one, because an unbounded read of a corpus is
not something a caller should be able to ask for; a closure is the exception,
because a closure truncated at some number of rows is not a closure and the sweep
that reads it would silently leave content behind. The graph here is deliberately
larger than the default bounds the store's other read statements carry, and those
bounds are imported rather than restated so the comparison follows them if they
move.

The edges are placed directly rather than through the guarded insert, because the
guard is what `test_lineage_graph_guard.py` asserts and the subject here is the
traversal. Both traversals are also read with their own module-level statements
through the store's query functions, so what is exercised is the statement the
store sends rather than one written here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.embeddings import DEFAULT_NEIGHBOUR_LIMIT, DEFAULT_PENDING_LIMIT
from molt.store.lineage import ancestors_of, descendants_of
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes the fixtures make, parameterised in full. This module owns no
# Client insert and no Artifact write, so the rows its graph is built from are
# placed here rather than through a module under test.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACTS_BULK: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, expires_at) "
    "SELECT unnest(%s::UUID[]), 'summary', %s, 'a body', unnest(%s::STRING[]), %s, %s"
)
INSERT_EDGES_BULK: Final[str] = (
    "INSERT INTO lineage_edge (child_id, parent_id, parent_kind, derivation_method) "
    "SELECT unnest(%s::UUID[]), unnest(%s::UUID[]), %s, %s"
)

# The derivation method every edge this module places records.
METHOD: Final[str] = "distil"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# How many layers of two nodes the lattice holds. Each layer doubles the number of
# distinct paths through it while adding two nodes, so the path count reaches
# thousands at a node count in the twenties.
LATTICE_LAYERS: Final[int] = 12
LATTICE_WIDTH: Final[int] = 2

# How many nodes hang off the lattice in a single chain. The chain is what pushes
# the closure past the row bounds the store's other reads carry, and it is a shape
# no single recursive step reaches the end of.
CHAIN_LENGTH: Final[int] = 96

# How many distinct paths the deepest node of the lattice must be reachable by for
# the deduplication claim to be worth making. The real count is computed rather
# than assumed; this is the floor it is checked against.
PATHS_FLOOR: Final[int] = 1000

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


@dataclass(frozen=True, slots=True)
class Lattice:
    """The graph this module walks, with the shape it was built to.

    Edges point child to parent, as the schema stores them, and the two adjacency
    mappings are held in both directions so the reference counting below reads in
    whichever direction it is asked about.
    """

    store: MemoryStore
    source: UUID
    sink: UUID
    deepest: UUID
    nodes: tuple[UUID, ...]
    children: dict[UUID, set[UUID]]
    parents: dict[UUID, set[UUID]]


def path_count(start: UUID, target: UUID, adjacency: dict[UUID, set[UUID]]) -> int:
    """How many distinct paths lead from one node to another, counted here.

    A depth-first count with memoisation over an acyclic graph, which is an
    independent computation: the traversals under test deduplicate by node and
    this counts by path, so the two numbers coming apart is the point.
    """
    memo: dict[UUID, int] = {}

    def onward(node: UUID) -> int:
        if node == target:
            return 1
        held = memo.get(node)
        if held is not None:
            return held
        total = sum(onward(step) for step in adjacency.get(node, set()))
        memo[node] = total
        return total

    return onward(start)


def reachable_from(seed: UUID, adjacency: dict[UUID, set[UUID]]) -> set[UUID]:
    """The set of nodes reachable from a seed, walked independently of the statement."""
    reached: set[UUID] = set()
    pending = list(adjacency.get(seed, set()))
    while pending:
        node = pending.pop()
        if node in reached:
            continue
        reached.add(node)
        pending.extend(adjacency.get(node, set()))
    return reached


@pytest.fixture(scope="module")
def lattice(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Lattice]:
    """Apply every migration, place the graph in two bulk statements, hand back both views.

    The lattice is a source, a stack of layers each of whose nodes carries every
    node of the layer above as a parent, and a sink under the last layer. A chain
    hangs off the sink. Every node is a Derived_Artifact, because a lineage child
    must be one and a graph whose interior nodes are both child and parent needs
    every node to be admissible as either.
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
    send(fresh_schema, INSERT_CLIENT, (client_id, f"tenant-{client_id.hex[:10]}", "Tenant", "eu"))

    source = uuid4()
    layers = [[uuid4() for _ in range(LATTICE_WIDTH)] for _ in range(LATTICE_LAYERS)]
    sink = uuid4()
    chain = [uuid4() for _ in range(CHAIN_LENGTH)]

    pairs: list[tuple[UUID, UUID]] = []
    for node in layers[0]:
        pairs.append((node, source))
    for above, below in pairwise(layers):
        for child in below:
            for parent_node in above:
                pairs.append((child, parent_node))
    for node in layers[-1]:
        pairs.append((sink, node))
    previous = sink
    for node in chain:
        pairs.append((node, previous))
        previous = node

    nodes = [source, *(node for layer in layers for node in layer), sink, *chain]
    send(
        fresh_schema,
        INSERT_ARTIFACTS_BULK,
        (
            nodes,
            client_id,
            [digest_of(f"artifact-{node}") for node in nodes],
            METHOD,
            EXPIRY,
        ),
    )
    send(
        fresh_schema,
        INSERT_EDGES_BULK,
        (
            [child for child, _ in pairs],
            [parent_node for _, parent_node in pairs],
            ArtifactKind.DERIVED_ARTIFACT.value,
            METHOD,
        ),
    )

    children: dict[UUID, set[UUID]] = {node: set() for node in nodes}
    parents: dict[UUID, set[UUID]] = {node: set() for node in nodes}
    for child, parent_node in pairs:
        children[parent_node].add(child)
        parents[child].add(parent_node)

    with MemoryStore(connect_with=connect_with) as store:
        yield Lattice(
            store=store,
            source=source,
            sink=sink,
            deepest=chain[-1],
            nodes=tuple(nodes),
            children=children,
            parents=parents,
        )


# ---------------------------------------------------------------------------
# Deduplication, where the path count and the node count come apart
# ---------------------------------------------------------------------------


def test_the_descendant_walk_returns_one_row_per_node_not_one_per_path(
    lattice: Lattice,
) -> None:
    """Thousands of paths reach the sink, and the sink comes back once.

    The path count is computed here by a walk that counts paths, and the row count
    is what the cluster returned. With the recursive terms combined by `UNION ALL`
    the second number would be the first; with `UNION` it is the number of nodes.
    """
    paths = path_count(lattice.source, lattice.sink, lattice.children)
    assert paths > PATHS_FLOOR, "the lattice should make the two counts differ in kind"

    reached = descendants_of(lattice.store, [lattice.source])

    assert len(reached) == len(set(reached))
    assert set(reached) == reachable_from(lattice.source, lattice.children)


def test_the_ancestor_walk_returns_one_row_per_node_not_one_per_path(
    lattice: Lattice,
) -> None:
    """The mirror claim, from the far end of the graph, over the same lattice.

    Every ancestor also carries the kind the edge recorded, so the deduplication
    here is over the pair rather than over the identifier alone, and a node
    reached as the parent of many edges still arrives once.
    """
    paths = path_count(lattice.deepest, lattice.source, lattice.parents)
    assert paths > PATHS_FLOOR, "the lattice should make the two counts differ in kind"

    reached = ancestors_of(lattice.store, [lattice.deepest])

    assert len({found.id for found in reached}) == len(reached)
    assert {found.id for found in reached} == reachable_from(lattice.deepest, lattice.parents)
    assert all(found.kind is ArtifactKind.DERIVED_ARTIFACT for found in reached)


# ---------------------------------------------------------------------------
# No row bound
# ---------------------------------------------------------------------------


def test_a_closure_larger_than_every_other_read_bound_comes_back_whole(
    lattice: Lattice,
) -> None:
    """A closure is not truncated, because a truncated closure is not a closure.

    Every other read the store performs carries a row bound. This one does not,
    and the graph is larger than the defaults those reads carry, so a bound
    inherited from anywhere would show as a short answer here rather than in an
    erasure sweep that quietly left content behind.
    """
    expected = reachable_from(lattice.source, lattice.children)
    assert len(expected) > DEFAULT_PENDING_LIMIT
    assert len(expected) > DEFAULT_NEIGHBOUR_LIMIT

    reached = descendants_of(lattice.store, [lattice.source])

    assert len(reached) == len(expected)
    assert set(reached) == expected


def test_a_walk_from_many_seeds_covers_each_seed_and_deduplicates_across_them(
    lattice: Lattice,
) -> None:
    """Overlapping seeds share most of their closure, and the shared part arrives once.

    The seeds are chosen so their closures overlap almost entirely: the source
    reaches everything the sink reaches, and the sink reaches everything the first
    chain node reaches. A traversal that walked each seed separately would return
    the shared tail once per seed.
    """
    first_chain_node = next(iter(lattice.children[lattice.sink]))
    seeds = [lattice.source, lattice.sink, first_chain_node]
    expected = set().union(*(reachable_from(seed, lattice.children) for seed in seeds))

    reached = descendants_of(lattice.store, seeds)

    assert len(reached) == len(set(reached))
    assert set(reached) == expected
