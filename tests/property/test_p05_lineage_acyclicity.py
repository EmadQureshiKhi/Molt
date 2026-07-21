"""Property 5: every accepted edge leaves the graph acyclic, and each refusal says why.

**Validates: Requirements 11.3, 11.4**

This property needs the cluster, and the reason is the whole point of the guard.
The reachability check that refuses a cycle and the join that refuses an absent
parent both travel inside the inserting statement, so what they refuse is decided
by the cluster's evaluation of that statement rather than by anything a
reimplementation could stand in for. The module is therefore marked so it gates on
a reachable instance and is deselected from the credential-free workflow.

Five decisions shape what is asserted.

**Every proposed edge falls in one of four classes, and each class is asserted
against its own outcome rather than against "some refusal".** A valid edge is
accepted and returns the identifier of the row it wrote. A self edge is refused,
and it is refused as a cycle, because an Artifact deriving from itself is the
shortest cycle a graph admits. A reversed edge is refused as a cycle exactly when
the reverse path has already landed, and is otherwise a perfectly good edge whose
forward counterpart is refused later instead: that ordering dependence is why the
outcome is predicted from a model of what has been accepted so far rather than
from the class alone. An edge naming an identifier no Artifact holds is refused as
a missing parent.

**The two refusal causes are told apart by type and by message.** A verifier that
answered "refused" for both would satisfy a weaker claim and would leave an
operator unable to tell a graph shape problem from a dangling reference. A
cycle-closing edge must raise the cycle failure, an edge whose parent no Artifact
holds must raise the missing-parent failure naming the parent, and an edge whose
child no Derived_Artifact holds must raise the missing-parent failure naming the
child, which is the reference the schema does carry. The two failures are siblings
rather than one deriving from the other, so each assertion excludes the other.

**The invariant is checked with a different algorithm from the one that predicts
the outcomes.** The prediction walks descendants of the proposed child to see
whether the proposed parent is already down there. The invariant is checked by
peeling: nodes with no remaining parent are removed until nothing more can be, and
anything left over sits on a cycle. Two algorithms agreeing is worth something;
one algorithm agreeing with itself is not. The peel runs after every accepted
insertion, so an example reports the first insertion that broke the invariant
rather than only that the end state was broken, and it runs once more over the
edges read back out of the cluster.

**The atomicity claim the batch insert makes is asserted directly.** Every edge of
one Derived_Artifact goes in the transaction that writes the Artifact, so a refusal
part way through must leave none of that Artifact's edges behind. An example builds
a batch of valid parents with one refused parent placed at a drawn position, asserts
the refusal, and asserts the Artifact holds no edge at all. Then it sends the same
batch without the refused parent to the same Artifact and asserts every edge lands,
which is what makes the previous assertion mean something: the prefix that was
abandoned would otherwise have written rows.

The example budget is 100 with no per-example deadline, which is the floor every
property in this plan runs at. What was tuned instead is the length of a sequence.
Every proposal here goes through the guarded insert in a transaction of its own,
because that insert is the subject rather than the setting, and a transaction is
several round trips on a shared instance. Graphs are drawn at two to fourteen nodes
carrying a planted chain and up to twenty further edges, and the density is drawn
as a number rather than as a list size, so most sequences run from nine to thirty
proposals rather than collapsing to the two or three a list size would concentrate
on. That is what makes the ordering claim reachable: a reversed edge is only
interesting when the sequence is long enough for it to land on either side of the
edge it reverses. Node count is not what this property varies over, because a cycle
through three nodes and a cycle through five hundred are refused by the same
reachability set and the closure property is where size earns its keep. The whole
file including the migrations stays inside a minute. Where a budget had to give, it
was the budget; no assertion was.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.errors import LineageCycleError, MissingParentError, StoreError
from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.lineage import ParentRef, insert_lineage_edge, insert_lineage_edges
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# How many examples the property runs, and the bounds of a drawn sequence. The
# reasoning behind these numbers is in the module docstring.
MAX_EXAMPLES: Final[int] = 100
MIN_NODES: Final[int] = 2
MAX_NODES: Final[int] = 14
MAX_EXTRA_EDGES: Final[int] = 20
MAX_SELF_EDGES: Final[int] = 3
MAX_ABSENT_EDGES: Final[int] = 4
MAX_BATCH_PARENTS: Final[int] = 4

# The position standing for an identifier no Artifact holds. A proposal carrying
# it is given a freshly minted identifier at insertion time, so nothing the graph
# holds can collide with it.
ABSENT: Final[int] = -1

# The fixture's own statements. This module owns no tenant insert and no Artifact
# write, so each row its graphs are built from is placed here with every value
# bound and no identifier interpolated. Edges are the one thing placed through the
# module under test, because the guard on that insert is the subject here.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACT_ROW: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
SELECT_EDGES_OF: Final[str] = (
    "SELECT child_id, parent_id FROM lineage_edge WHERE child_id IN (SELECT unnest(%s::UUID[]))"
)
COUNT_EDGES_OF: Final[str] = "SELECT count(*) FROM lineage_edge WHERE child_id = %s"

# The kind every node carries and the derivation method every edge records.
NODE_KIND: Final[str] = "summary"
METHOD: Final[str] = "distil"
NODE_BODY: Final[str] = "a derived body"

# The message fragment each refusal is identified by, alongside its type. The
# first names the polymorphic parent no reference can cover, the second names the
# child column that does carry one, and the third is the graph shape.
ABSENT_PARENT_FRAGMENT: Final[str] = "no Artifact holds"
ABSENT_CHILD_FRAGMENT: Final[str] = "no Derived_Artifact"
CYCLE_FRAGMENT: Final[str] = "cyclic"
REPEATED_FRAGMENT: Final[str] = "already recorded"

# An instant with an offset, derived from the epoch rather than written as a
# literal, so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


class EdgeClass(StrEnum):
    """Which of the four kinds of proposal an entry of a sequence is."""

    VALID = "valid"
    REVERSED = "reversed"
    SELF = "self"
    ABSENT = "absent"


class Outcome(StrEnum):
    """What the guarded insert must do with a proposal.

    The two missing members are one failure type carrying two messages, because
    the parent side is enforced by a join over the polymorphic reference view and
    the child side by the reference the schema declares, and an operator reading
    either one needs to know which end was dangling.
    """

    ACCEPTED = "accepted"
    CYCLE = "cycle"
    MISSING_PARENT = "missing parent"
    MISSING_CHILD = "missing child"
    REPEATED = "repeated"


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Proposal:
    """One edge to propose, as positions in a topological order.

    A position of `ABSENT` stands for an identifier no Artifact holds. The class
    is carried for the coverage record and for the failure message; it is never
    what the expected outcome is read off, because a reversed edge proposed before
    its forward counterpart is a valid insertion and the same edge proposed after
    it is a cycle.
    """

    child: int
    parent: int
    edge_class: EdgeClass


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """A batch of edges for one Artifact, with one refused parent placed in it."""

    parents: tuple[int, ...]
    refusal: EdgeClass
    slot: int


@dataclass(frozen=True, slots=True)
class InsertionPlan:
    """A graph to build one proposal at a time, and one batch to abandon."""

    node_count: int
    proposals: tuple[Proposal, ...]
    batch: BatchPlan


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


@st.composite
def edge_insertion_sequences(draw: st.DrawFn) -> InsertionPlan:
    """Draw a directed acyclic graph and a shuffled sequence proposing its edges.

    The planned edges all run from a later position to an earlier one, so the
    planned graph is acyclic however the pairs fell, and a chain of consecutive
    positions is planted so that some reversed edge closes a cycle several hops
    long rather than only the two-hop kind. Into the shuffle go a drawn subset of
    those edges reversed, a drawn number of self edges, and a drawn number of
    edges naming an identifier no Artifact holds, on the parent side, the child
    side, or both. The order is a drawn permutation of the lot, which is what
    decides whether a reversed edge arrives before or after the edge it reverses.
    """
    count = draw(st.integers(min_value=MIN_NODES, max_value=MAX_NODES))
    positions = st.integers(min_value=0, max_value=count - 1)

    chain_length = draw(st.integers(min_value=1, max_value=count))
    planned: set[tuple[int, int]] = {
        (position, position - 1) for position in range(1, chain_length)
    }

    # The density is drawn as a number and then as a list of exactly that length,
    # rather than as a list size, because a list size concentrates near its lower
    # end and a sequence long enough for a reversed edge to arrive on either side
    # of the edge it reverses is the interesting part of the range.
    density = draw(st.integers(min_value=0, max_value=MAX_EXTRA_EDGES))
    pairs = st.tuples(positions, positions)
    for first, second in draw(st.lists(pairs, min_size=density, max_size=density)):
        if first != second:
            planned.add((max(first, second), min(first, second)))

    proposals: list[Proposal] = []
    for child, parent in sorted(planned):
        proposals.append(Proposal(child=child, parent=parent, edge_class=EdgeClass.VALID))
        if draw(st.booleans()):
            proposals.append(Proposal(child=parent, parent=child, edge_class=EdgeClass.REVERSED))

    for _ in range(draw(st.integers(min_value=0, max_value=MAX_SELF_EDGES))):
        position = draw(positions)
        proposals.append(Proposal(child=position, parent=position, edge_class=EdgeClass.SELF))

    for _ in range(draw(st.integers(min_value=0, max_value=MAX_ABSENT_EDGES))):
        side = draw(st.sampled_from(("parent", "child", "both")))
        position = draw(positions)
        proposals.append(
            Proposal(
                child=ABSENT if side in {"child", "both"} else position,
                parent=ABSENT if side in {"parent", "both"} else position,
                edge_class=EdgeClass.ABSENT,
            )
        )

    order = draw(st.permutations(range(len(proposals))))
    return InsertionPlan(
        node_count=count,
        proposals=tuple(proposals[index] for index in order),
        batch=draw(batch_plans(count)),
    )


@st.composite
def batch_plans(draw: st.DrawFn, node_count: int) -> BatchPlan:
    """Draw a batch of valid parents with one refused parent placed among them.

    The refusal is either a self edge or an absent identifier, so the abandoned
    batch is abandoned for each of the two causes across the run rather than for
    only one of them. The batch is no longer than the graph has nodes, because the
    parents must be distinct and a request for more distinct positions than exist
    would spend the example's budget on rejected draws.
    """
    parents = draw(
        st.lists(
            st.integers(min_value=0, max_value=node_count - 1),
            min_size=1,
            max_size=min(MAX_BATCH_PARENTS, node_count),
            unique=True,
        )
    )
    return BatchPlan(
        parents=tuple(parents),
        refusal=draw(st.sampled_from((EdgeClass.SELF, EdgeClass.ABSENT))),
        slot=draw(st.integers(min_value=0, max_value=len(parents))),
    )


# ---------------------------------------------------------------------------
# The model that predicts an outcome, and the peel that checks the invariant
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Model:
    """The edges accepted so far, and what the next proposal must therefore do.

    Prediction walks descendants of the proposed child, which is the same question
    the guard asks and the reason the two can be compared at all. The invariant is
    checked by `cyclic_nodes` instead, which shares no code with this walk.
    """

    known: frozenset[UUID]
    edges: set[tuple[UUID, UUID]]

    def descendants(self, node: UUID) -> set[UUID]:
        """Every node reachable from one node in the parent-to-child direction."""
        reached: set[UUID] = set()
        pending = [child for child, parent in self.edges if parent == node]
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            pending.extend(child for child, parent in self.edges if parent == current)
        return reached

    def expected(self, child: UUID, parent: UUID) -> Outcome:
        """What the guarded insert must do with one proposed edge.

        The order of these questions is the order the statement decides them in.
        An absent parent produces no row from the existence join, so the guard is
        never reached and the refusal is the missing parent whatever else was
        wrong; an absent child passes the join and reaches the reference the child
        column carries instead.
        """
        if parent not in self.known:
            return Outcome.MISSING_PARENT
        if child not in self.known:
            return Outcome.MISSING_CHILD
        if child == parent:
            return Outcome.CYCLE
        if (child, parent) in self.edges:
            return Outcome.REPEATED
        if parent in self.descendants(child):
            return Outcome.CYCLE
        return Outcome.ACCEPTED

    def accept(self, child: UUID, parent: UUID) -> None:
        """Record one edge the cluster accepted."""
        self.edges.add((child, parent))


def cyclic_nodes(edges: Sequence[tuple[UUID, UUID]]) -> frozenset[UUID]:
    """Every node that sits on a cycle, found by peeling rather than by walking.

    Nodes with no remaining parent are removed until nothing more can be. A
    directed acyclic graph peels away entirely; whatever is left over is reachable
    from itself, a node naming itself as its own parent included.
    """
    parents: dict[UUID, set[UUID]] = {}
    remaining: set[UUID] = set()
    for child, parent in edges:
        parents.setdefault(child, set()).add(parent)
        remaining |= {child, parent}

    peeled = True
    while peeled:
        peeled = False
        for node in sorted(remaining):
            if not parents.get(node, set()) & remaining:
                remaining.discard(node)
                peeled = True
    return frozenset(remaining)


# ---------------------------------------------------------------------------
# The cluster the sequences are applied to
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Graph:
    """A schema holding every migration, a store over it, and one tenant."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID

    def place_nodes(self, count: int) -> tuple[UUID, ...]:
        """Place one Derived_Artifact per node and return their identifiers."""
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

    def stored_edges(self, nodes: Sequence[UUID]) -> set[tuple[UUID, UUID]]:
        """Every edge the cluster holds whose child is one of these nodes."""
        with self.connection.cursor() as cursor:
            cursor.execute(SELECT_EDGES_OF, (list(nodes),))
            rows = cursor.fetchall()
        return {(as_uuid(row[0]), as_uuid(row[1])) for row in rows}

    def edge_count(self, child_id: UUID) -> int:
        """How many edges the cluster holds for one child."""
        with self.connection.cursor() as cursor:
            cursor.execute(COUNT_EDGES_OF, (child_id,))
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])


def as_uuid(value: object) -> UUID:
    """Read one identifier column, whichever representation the driver returned."""
    return value if isinstance(value, UUID) else UUID(str(value))


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
    """Send one statement once per row, as a batch, with every value bound."""
    if not rows:
        return
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)


def parent_ref(identifier: UUID) -> ParentRef:
    """A parent reference of the derived kind, carrying this module's method."""
    return ParentRef(
        parent_id=identifier,
        parent_kind=ArtifactKind.DERIVED_ARTIFACT,
        derivation_method=METHOD,
    )


@pytest.fixture(scope="module")
def graph(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Graph]:
    """Apply every migration, then build a store bound to that schema.

    Every migration rather than the first generation alone, because the reference
    on the child column is re-created with a cascading delete by the second
    generation and the refusal for an absent child is read off whichever of the
    two names the constraint carries.
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
# One proposal
# ---------------------------------------------------------------------------


def identifier_for(position: int, nodes: Sequence[UUID]) -> UUID:
    """The identifier a position names, minting one no Artifact holds for `ABSENT`."""
    return uuid4() if position == ABSENT else nodes[position]


def apply_proposal(
    graph: Graph,
    model: Model,
    child: UUID,
    parent: UUID,
    edge_class: EdgeClass,
    described: str,
) -> None:
    """Propose one edge and assert the outcome the model says it must have.

    The class is recorded alongside the outcome as well as separately, because the
    interesting part of the claim is which class produced which outcome: a reversed
    edge refused as a cycle is the ordering-dependent case, and a valid edge refused
    as a cycle is the same case seen from the other side, and neither is
    distinguishable from a self edge in a record of outcomes alone.
    """
    expected = model.expected(child, parent)
    event(f"outcome={expected}")
    event(f"{edge_class} edge -> {expected}")

    if expected is Outcome.ACCEPTED:
        written = insert_lineage_edge(graph.store, child, parent_ref(parent))
        assert isinstance(written, UUID), f"the accepted {described} returned no edge identifier"
        model.accept(child, parent)
        assert not cyclic_nodes(sorted(model.edges)), (
            f"the accepted {described} left the graph cyclic"
        )
        return

    if expected is Outcome.CYCLE:
        with pytest.raises(LineageCycleError, match=CYCLE_FRAGMENT):
            insert_lineage_edge(graph.store, child, parent_ref(parent))
        return

    if expected is Outcome.MISSING_PARENT:
        with pytest.raises(MissingParentError, match=ABSENT_PARENT_FRAGMENT):
            insert_lineage_edge(graph.store, child, parent_ref(parent))
        return

    if expected is Outcome.MISSING_CHILD:
        with pytest.raises(MissingParentError, match=ABSENT_CHILD_FRAGMENT):
            insert_lineage_edge(graph.store, child, parent_ref(parent))
        return

    # A restatement of an edge already recorded. No sequence this generator draws
    # reaches it, because a proposal is drawn once and a refused proposal writes
    # nothing, but the model answers it and so the assertion states it rather than
    # leaving the branch to fall through to a neighbouring one.
    with pytest.raises(StoreError, match=REPEATED_FRAGMENT):
        insert_lineage_edge(graph.store, child, parent_ref(parent))


def apply_batch(graph: Graph, nodes: Sequence[UUID], plan: BatchPlan) -> None:
    """Assert that a batch refused part way through leaves no edge of its Artifact.

    The Artifact is minted here rather than taken from the graph, so it starts with
    no edge and nothing it names can close a cycle: every valid parent in the batch
    would land, which is what the second half of this asserts by sending the same
    batch without the refused entry to the same Artifact.
    """
    child = graph.place_nodes(1)[0]
    valid = [nodes[position] for position in plan.parents]
    refused = child if plan.refusal is EdgeClass.SELF else uuid4()
    mixed = [*valid[: plan.slot], refused, *valid[plan.slot :]]
    event(f"batch refusal={plan.refusal} at {plan.slot} of {len(valid)}")

    failure: type[Exception] = (
        LineageCycleError if plan.refusal is EdgeClass.SELF else MissingParentError
    )
    with pytest.raises(failure):
        insert_lineage_edges(graph.store, child, [parent_ref(entry) for entry in mixed])

    assert graph.edge_count(child) == 0, (
        "one refused edge of an artifact leaves none of that artifact's edges behind"
    )

    written = insert_lineage_edges(graph.store, child, [parent_ref(entry) for entry in valid])
    assert len(written) == len(valid)
    assert graph.edge_count(child) == len(valid), (
        "the same batch without the refused entry lands in full, which is what "
        "makes the abandoned prefix above an abandonment rather than a no-op"
    )


def length_band(length: int) -> str:
    """Which part of the length range a sequence fell in, for the coverage record."""
    if length <= 8:
        return "1-8"
    if length <= 20:
        return "9-20"
    return "21+"


# Feature: molt, Property 5: For any sequence of Lineage_Edge insertions, including
# insertions that would close a cycle and insertions naming absent parents, every
# accepted insertion leaves the Lineage_Graph acyclic, every cycle-closing
# insertion is rejected, and every insertion naming an absent parent is rejected.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(plan=edge_insertion_sequences())
def test_every_accepted_edge_leaves_the_graph_acyclic_and_each_refusal_says_why(
    graph: Graph, plan: InsertionPlan
) -> None:
    event(f"proposals={length_band(len(plan.proposals))}")
    nodes = graph.place_nodes(plan.node_count)
    model = Model(known=frozenset(nodes), edges=set())

    for index, proposal in enumerate(plan.proposals):
        child = identifier_for(proposal.child, nodes)
        parent = identifier_for(proposal.parent, nodes)
        apply_proposal(
            graph,
            model,
            child,
            parent,
            proposal.edge_class,
            f"{proposal.edge_class} edge at position {index} of the sequence",
        )

    # The cluster holds exactly the edges the model accepted: nothing a refusal
    # wrote, and nothing an acceptance failed to write.
    stored = graph.stored_edges(nodes)
    assert stored == model.edges

    # And the graph the cluster holds is acyclic, by the peel rather than by the
    # walk the outcomes above were predicted with.
    assert not cyclic_nodes(sorted(stored)), "the stored graph holds a cycle"

    apply_batch(graph, nodes, plan.batch)
