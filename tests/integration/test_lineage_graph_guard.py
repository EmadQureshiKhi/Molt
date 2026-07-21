"""The lineage guard and the two closure traversals against a live instance.

The unit module asserts the shape of the statements. This one asserts the four
things only a cluster can answer, because each rests on the schema and on the
recursive evaluation rather than on the module.

A valid edge is accepted and carries the derivation method that produced the
child. A self edge and an edge that would close a longer cycle are each refused,
and refused with the cycle error rather than with a constraint failure leaking
through. An edge naming a parent no Artifact holds is refused with the
missing-parent error, which is the case no foreign key can cover because the
parent column is polymorphic across three kinds. And both traversals are compared
against an independent closure computed in Python over a graph holding diamonds
and long chains, so agreement is asserted rather than assumed.

The plans are read back as well. The time bound of Requirement 11.7 rests on one
index per direction, so a plan that scanned the edge table instead of seeking into
it would meet every assertion above and still miss the bound. Asserting the index
by name is what makes that failure visible here rather than in a performance run.

Every migration is applied, because the cascading reference on the child column
arrives with the second generation and the refusal for an absent child is read
off the constraint that generation installs.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.errors import LineageCycleError, MissingParentError, StoreError
from molt.models.artifact import ArtifactKind
from molt.store import Connection, Cursor, MemoryStore
from molt.store.lineage import (
    SELECT_ANCESTORS_STATEMENT,
    SELECT_DESCENDANTS_STATEMENT,
    LineageNode,
    ParentRef,
    ancestors_of,
    descendants_of,
    insert_edges,
    insert_lineage_edge,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes the fixtures make, parameterised in full. This module owns no
# Client insert, no Ledger append, and no Artifact write, so the rows its graphs
# are built from are placed here rather than through the module under test.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACT_ROW: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
INSERT_LEDGER_ROW: Final[str] = (
    "INSERT INTO ledger (id, session_id, client_id, seq, category, occurred_at, "
    "recorded_at, agent_cli, machine_id, payload, content_digest, prev_chain_digest, "
    "chain_digest, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s, %s, %s, %s)"
)
INSERT_SESSION_ROW: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, started_at) "
    "VALUES (%s, %s, %s, %s, %s)"
)
SELECT_EDGE: Final[str] = (
    "SELECT parent_kind, derivation_method FROM lineage_edge WHERE child_id = %s AND parent_id = %s"
)
COUNT_EDGES: Final[str] = "SELECT count(*) FROM lineage_edge WHERE child_id = %s"

# The index each traversal direction is served by. A plan naming neither is a
# scan, which is the failure this module exists to catch early.
DESCENDANT_INDEX: Final[str] = "lineage_by_parent"
ANCESTOR_INDEX: Final[str] = "lineage_by_child"

# What a plan says when it read the whole table instead of seeking into it. The
# plan is lowercased before the comparison, so this is the lowercase spelling.
FULL_SCAN: Final[str] = "full scan"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# The derivation method every edge in this module records, which is what the
# acceptance assertion reads back.
METHOD: Final[str] = "distil"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Graph:
    """A schema holding every migration, a store over it, and one tenant."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID

    def artifact(self) -> UUID:
        """Place one Derived_Artifact directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_ARTIFACT_ROW,
            (
                identifier,
                "summary",
                self.client_id,
                "a body",
                digest_of(f"artifact-{identifier}"),
                METHOD,
                EXPIRY,
            ),
        )
        return identifier

    def session(self) -> UUID:
        """Place one Session directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_SESSION_ROW,
            (identifier, self.client_id, "agent", "machine", MOMENT),
        )
        return identifier

    def event(self, session_id: UUID, seq: int) -> UUID:
        """Place one Ledger row directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_LEDGER_ROW,
            (
                identifier,
                session_id,
                self.client_id,
                seq,
                "tool_call",
                MOMENT,
                MOMENT,
                "agent",
                "machine",
                '{"tool":"read"}',
                digest_of(f"content-{identifier}"),
                digest_of(f"previous-{seq}"),
                digest_of(f"chain-{identifier}"),
                EXPIRY,
            ),
        )
        return identifier


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def parent(identifier: UUID, kind: ArtifactKind = ArtifactKind.DERIVED_ARTIFACT) -> ParentRef:
    """A parent reference of one kind, carrying this module's derivation method."""
    return ParentRef(parent_id=identifier, parent_kind=kind, derivation_method=METHOD)


@pytest.fixture(scope="module")
def graph(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Graph]:
    """Apply every migration, then build a store bound to that schema."""
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
# Acceptance
# ---------------------------------------------------------------------------


def test_a_valid_edge_is_accepted_and_records_its_derivation_method(graph: Graph) -> None:
    """The edge lands, and the method that produced the child lands with it."""
    child = graph.artifact()
    ancestor = graph.artifact()

    edge_id = insert_lineage_edge(graph.store, child, parent(ancestor))

    assert isinstance(edge_id, UUID)
    with graph.connection.cursor() as cursor:
        cursor.execute(SELECT_EDGE, (child, ancestor))
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == ArtifactKind.DERIVED_ARTIFACT.value
    assert row[1] == METHOD


def test_a_parent_of_every_admitted_kind_is_accepted(graph: Graph) -> None:
    """The join spans the three kinds the reference view holds, and it accepts each."""
    child = graph.artifact()
    session_id = graph.session()
    event_id = graph.event(session_id, seq=1)
    ancestor = graph.artifact()

    def body(cursor: Cursor) -> tuple[UUID, ...]:
        return insert_edges(
            cursor,
            child,
            (
                parent(event_id, ArtifactKind.EVENT),
                parent(session_id, ArtifactKind.SESSION),
                parent(ancestor),
            ),
        )

    written = graph.store.in_serializable(body)

    assert len(written) == 3
    reached = {node.id: node.kind for node in ancestors_of(graph.store, [child])}
    assert reached == {
        event_id: ArtifactKind.EVENT,
        session_id: ArtifactKind.SESSION,
        ancestor: ArtifactKind.DERIVED_ARTIFACT,
    }


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_self_edge_is_refused_with_the_cycle_error(graph: Graph) -> None:
    """The shortest cycle is refused by the same statement that refuses the long ones."""
    child = graph.artifact()

    with pytest.raises(LineageCycleError):
        insert_lineage_edge(graph.store, child, parent(child))

    assert count_edges(graph, child) == 0


def test_a_cycle_closing_edge_is_refused_with_the_cycle_error(graph: Graph) -> None:
    """A parent already reachable from the child in the child direction is refused."""
    first = graph.artifact()
    second = graph.artifact()
    third = graph.artifact()
    insert_lineage_edge(graph.store, second, parent(first))
    insert_lineage_edge(graph.store, third, parent(second))

    with pytest.raises(LineageCycleError):
        insert_lineage_edge(graph.store, first, parent(third))

    assert count_edges(graph, first) == 0
    assert set(descendants_of(graph.store, [first])) == {second, third}


def test_an_edge_naming_an_absent_parent_is_refused(graph: Graph) -> None:
    """No reference can cover the polymorphic parent column, so the join does."""
    child = graph.artifact()

    with pytest.raises(MissingParentError):
        insert_lineage_edge(graph.store, child, parent(uuid4()))

    assert count_edges(graph, child) == 0


def test_an_edge_naming_a_parent_of_the_wrong_kind_is_refused(graph: Graph) -> None:
    """The join matches on kind as well as identity, so a mislabelled parent is absent."""
    child = graph.artifact()
    session_id = graph.session()

    with pytest.raises(MissingParentError):
        insert_lineage_edge(graph.store, child, parent(session_id, ArtifactKind.EVENT))

    assert count_edges(graph, child) == 0


def test_an_edge_naming_an_absent_child_is_refused(graph: Graph) -> None:
    """The child column carries a reference, and an absent child reaches it."""
    ancestor = graph.artifact()

    with pytest.raises(MissingParentError):
        insert_lineage_edge(graph.store, uuid4(), parent(ancestor))


def test_a_repeated_edge_is_refused_by_the_unique_pair(graph: Graph) -> None:
    """The pair of child and parent is held unique, and a restatement says so."""
    child = graph.artifact()
    ancestor = graph.artifact()
    insert_lineage_edge(graph.store, child, parent(ancestor))

    with pytest.raises(StoreError, match="already recorded"):
        insert_lineage_edge(graph.store, child, parent(ancestor))

    assert count_edges(graph, child) == 1


# ---------------------------------------------------------------------------
# The closures, against an independent traversal
# ---------------------------------------------------------------------------


def test_both_traversals_agree_with_a_reference_traversal(graph: Graph) -> None:
    """A graph of diamonds and long chains, walked twice by two implementations."""
    nodes = [graph.artifact() for _ in range(12)]
    # A diamond over the first four, a second diamond sharing its sink, and a
    # chain of five hanging off that sink, so the walk meets both a node reached
    # by several paths and a path no single step reaches the end of.
    pairs: list[tuple[int, int]] = [
        (1, 0),
        (2, 0),
        (3, 1),
        (3, 2),
        (4, 0),
        (5, 4),
        (5, 1),
        (6, 3),
        (6, 5),
        (7, 6),
        (8, 7),
        (9, 8),
        (10, 9),
        (11, 10),
        (11, 3),
    ]
    for child_index, parent_index in pairs:
        insert_lineage_edge(graph.store, nodes[child_index], parent(nodes[parent_index]))

    children: dict[UUID, set[UUID]] = {node: set() for node in nodes}
    parents: dict[UUID, set[UUID]] = {node: set() for node in nodes}
    for child_index, parent_index in pairs:
        children[nodes[parent_index]].add(nodes[child_index])
        parents[nodes[child_index]].add(nodes[parent_index])

    for node in nodes:
        assert set(descendants_of(graph.store, [node])) == closure_of([node], children)
        assert {found.id for found in ancestors_of(graph.store, [node])} == closure_of(
            [node], parents
        )

    seeds = [nodes[3], nodes[5], nodes[11]]
    assert set(descendants_of(graph.store, seeds)) == closure_of(seeds, children)
    assert {found.id for found in ancestors_of(graph.store, seeds)} == closure_of(seeds, parents)


def test_a_traversal_from_no_seed_reaches_nothing(graph: Graph) -> None:
    """An empty seed set is answered without a statement, in both directions."""
    assert descendants_of(graph.store, []) == ()
    assert ancestors_of(graph.store, []) == ()


def test_every_ancestor_carries_the_kind_that_holds_it(graph: Graph) -> None:
    """A parent is polymorphic, so a caller reading one learns which table holds it."""
    child = graph.artifact()
    middle = graph.artifact()
    session_id = graph.session()
    insert_lineage_edge(graph.store, child, parent(middle))
    insert_lineage_edge(graph.store, middle, parent(session_id, ArtifactKind.SESSION))

    reached = ancestors_of(graph.store, [child])

    assert LineageNode(id=middle, kind=ArtifactKind.DERIVED_ARTIFACT) in reached
    assert LineageNode(id=session_id, kind=ArtifactKind.SESSION) in reached


# ---------------------------------------------------------------------------
# The plans the time bound rests on
# ---------------------------------------------------------------------------


def test_each_traversal_is_served_by_its_own_index(graph: Graph) -> None:
    """One index per direction, seeking into a bounded span rather than scanning.

    The primary index appears in each plan as well, because a secondary index on
    one column stores the primary key and nothing else, so reading the far end of
    an edge is a join back into the primary index. That join is a batched lookup
    keyed by the rows the seek produced, not a read of the table, which is why
    what is asserted here is the bounded span on the named index and the absence
    of a full scan rather than the absence of the primary index.
    """
    root = graph.artifact()
    child = graph.artifact()
    insert_lineage_edge(graph.store, child, parent(root))

    descendant_plan = plan_of(graph, SELECT_DESCENDANTS_STATEMENT, [root])
    ancestor_plan = plan_of(graph, SELECT_ANCESTORS_STATEMENT, [child])

    assert DESCENDANT_INDEX in descendant_plan, descendant_plan
    assert ANCESTOR_INDEX in ancestor_plan, ancestor_plan
    assert FULL_SCAN not in descendant_plan, descendant_plan
    assert FULL_SCAN not in ancestor_plan, ancestor_plan
    assert "spans: [/" in descendant_plan, descendant_plan
    assert "spans: [/" in ancestor_plan, ancestor_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def closure_of(seeds: list[UUID], adjacency: dict[UUID, set[UUID]]) -> set[UUID]:
    """The reachable set from the seeds, excluding a seed no path returns to.

    An independent implementation on purpose: a breadth-first walk over an
    adjacency mapping, sharing nothing with the recursive statements it is
    compared against.
    """
    reached: set[UUID] = set()
    queue: deque[UUID] = deque()
    for seed in seeds:
        queue.extend(adjacency.get(seed, set()))
    while queue:
        node = queue.popleft()
        if node in reached:
            continue
        reached.add(node)
        queue.extend(adjacency.get(node, set()))
    return reached


def count_edges(graph: Graph, child_id: UUID) -> int:
    """How many edges the graph holds for one child."""
    with graph.connection.cursor() as cursor:
        cursor.execute(COUNT_EDGES, (child_id,))
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def plan_of(graph: Graph, statement: str, seeds: list[UUID]) -> str:
    """The plan the cluster reports for one traversal, as one lowercase block.

    The prefix is a literal and the statement is the module's own literal; the
    seed array stays a bound parameter, so nothing a caller supplied reaches
    statement text even here.
    """
    with graph.connection.cursor() as cursor:
        cursor.execute("EXPLAIN " + statement, (seeds,))
        rows = cursor.fetchall()
    return "\n".join(" ".join(str(column) for column in row) for row in rows).lower()
