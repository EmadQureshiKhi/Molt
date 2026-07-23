"""Property 15: a child carries every Client its parents carry, and raises none of them.

**Validates: Requirements 12.3**

This property drives a real cluster, and the reason is the shape of the claim. What
a child inherits is read by one statement in the child's own transaction: the
current-attribution form over the parents, aggregated by `max(confidence)` per
Client. What a pair then holds is decided by the supersession path, whose retained
maximum is `greatest` evaluated by the cluster against the confidence the closing
statement returned, and whose single live claim per pair is a partial unique index.
None of those three exists in this process. A lineage graph walked against a
dictionary of stubbed parent rows would be evidence about the stub, and the stub is
exactly the part the property is about, so the module is marked to gate on a
reachable instance and is deselected from the credential-free workflow. It carries
`integration` alone and not `instance` beside it: the two name one prerequisite,
and the suite marker already implies it, whereas `instance` is for a test whose
suite marker does not.

The claim has two halves and an exception clause, and all three are asserted.

**The Client set only grows.** For every edge, and again for the union over all of
a node's parents, and again for the union over its whole ancestry, the child's live
Client set contains every Client the parents hold. The ancestry form is the one that
makes a chain worth generating: it is what Requirement 12.3 amounts to at depth,
and it holds because a parent's own current bindings already carry what that parent
inherited, so a detector reading direct parents alone closes transitively.

**A confidence does not rise across an inheritance edge unless other evidence
supplies the higher value.** Inheritance takes the parent's own confidence, so a
child never holds less for a Client than a parent does; the interesting direction is
the other one, and every rise is required to name its source. Three sources can
legitimately supply one: the child's own scope claim at certainty, a marker match on
the child's text just below it, and another parent holding a stronger claim on the
same Client. Each rise is classified against the generated graph and a rise no
source accounts for fails the example.

**The whole live map is compared against a map computed from the drawn graph.** The
three kinds, the collapse that resolves a collision between them, and the write
path's own rule are modelled here from the drawn nodes alone, so nothing below
compares the store against itself. The precedence that breaks a tie between two
kinds at equal confidence is restated in this module rather than imported from the
one under test, because importing it would make the tie assertion circular.

Four decisions shape what is generated.

**Parents are drawn from the nodes already placed, with the deepest one offered as a
shape of its own and weighted above the others.** A parent drawn uniformly is
usually a shallow one, and a graph built that way barely nests. The three shapes are
no parent at all, the deepest node plus up to two others, and an arbitrary parent
set, so an example holds roots, a chain that really descends, and nodes with several
parents at once; offered evenly the deepest chain a six-node graph admits turns up in
a fraction of a percent of examples, which is why descending is the common case.
Depth needs no cap: a node's parents are always earlier nodes, so the node count
bounds the depth,
and the traversal cost is bounded with it. The detector itself reads direct parents
in one statement over a bounded array however deep the ancestry goes, and the model
here walks the graph once in index order, taking each node's expectation from
parents already computed, so neither side ever recurses.

**Some claims are planted at a drawn confidence rather than left at the two the
detector fixes.** Detection emits only certainty and the marker value, so a graph
built from detections alone would carry two distinct confidences and the
non-increase half would be nearly vacuous. One residue claim at a drawn confidence
is therefore written for some nodes, after that node's own detection has landed and
before any child of it exists, so inherited values spread across the whole interval
while every node's own detection still acts on a pair holding nothing.

**Collisions between the three kinds are arranged rather than hoped for.** The owner
of a node is drawn from the same small set of Clients its markers and its parents
come from, so the owning Client is frequently a marker match and frequently bound to
a parent as well, and the coverage record reports how often each pairing occurred.

**One twin node per example re-runs one node's evidence in a permuted emission
order.** The same owner, the same text, and the same parents reversed, with the
collapsed claims submitted back to front. Its live map must be identical to the
original's, which is the claim that neither the order evidence arrives in nor the
order claims are written in reaches stored state. The parents of the chosen node are
settled before it was first written, so the twin reads exactly the state the
original read.

Marker text is built from a token derived from each Client's own identifier, and the
schema is shared across the examples of this module, so a marker configured by one
example cannot occur in another example's text and no example asserts about another
example's tenants.

The example budget is 100 with no per-example deadline. Per-example cost is up to
five tenant inserts, one Session insert, up to six detections and one twin, each a
transaction of its own carrying up to two reads and one attribution write per
Client, plus one planted claim per seeded node and one readback per node. A deadline
would fail a six-node example for being large rather than for being wrong, which is
why there is none.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.artifact import ArtifactKind, ArtifactRef
from molt.models.binding import BindingMethod
from molt.store import Connection, Cursor, MemoryStore
from molt.store.attribution import (
    AttributionSubmission,
    SupersessionContext,
    record_attribution,
    write_attribution,
)
from molt.store.binding_detector import (
    MARKER_CONFIDENCE,
    SCOPE_CONFIDENCE,
    DetectionRequest,
    bindings_for,
    record_bindings,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# How many examples the property runs, how many Clients a graph spreads over, how
# many nodes it holds, and how many parents one node may name. The node count is
# also the depth bound, because every parent is an earlier node.
MAX_EXAMPLES: Final[int] = 100
MIN_CLIENTS: Final[int] = 2
MAX_CLIENTS: Final[int] = 5
MIN_NODES: Final[int] = 2
MAX_NODES: Final[int] = 6
MAX_FAN_IN: Final[int] = 3

# The ends of the interval a planted confidence is drawn from.
CONFIDENCE_FLOOR: Final[float] = 0.0
CONFIDENCE_CEILING: Final[float] = 1.0

# How directly each kind claims an Artifact, most direct first. Restated here
# rather than imported from the module under test, because a tie between two kinds
# at equal confidence is resolved by this order and reading it off the
# implementation would make the assertion about it circular.
PRECEDENCE: Final[tuple[BindingMethod, ...]] = (
    BindingMethod.SCOPE,
    BindingMethod.MARKER,
    BindingMethod.INHERITED,
    BindingMethod.RESIDUE,
)

# The words a generated text is padded with, so every planted marker sits between
# token boundaries and a match is the Client's own name occurring in the text.
TEXT_PREFIX: Final[str] = "the run"
TEXT_SUFFIX: Final[str] = "finished"

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The writes and the read this module makes for itself. The module under test owns
# no tenant insert and no Session insert, and the live claims are read back by a
# statement of this module's own so that no assertion about stored state rests on
# the statement text the module under test reads bindings through.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction, content_markers) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
SELECT_LIVE_CLAIMS: Final[str] = (
    "SELECT client_id, method, confidence FROM client_binding "
    "WHERE artifact_id = %s AND superseded_by IS NULL ORDER BY client_id"
)

# The command and the machine every Event of this module records.
AGENT_CLI: Final[str] = "molt"
MACHINE_ID: Final[str] = "property-machine"

# The detection instant every submission carries and how long an appended row is
# retained for. The reading is derived from the epoch rather than written out, so no
# example embeds a calendar value, and it is a detection reading rather than a
# validity reading: every validity instant is the cluster's own.
DETECTED_AT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claim:
    """What one Artifact and Client pair asserts: how it was concluded, and how strongly."""

    method: BindingMethod
    confidence: float


@dataclass(frozen=True, slots=True)
class PlantedClaim:
    """One residue claim written for a node after its own detection has landed.

    Detection concludes at two fixed confidences, so a graph built from detections
    alone carries two distinct values and says little about a confidence rising or
    falling along an edge. A planted claim at a drawn value is what spreads the
    inherited confidences across the interval.
    """

    client_index: int
    confidence: float


class Attachment(StrEnum):
    """How one node joins the nodes already placed."""

    ROOT = "root"
    DESCEND = "descend"
    SPREAD = "spread"


# How often each attachment shape is offered to a node, by how many times it appears
# here. The weighting is deliberate: offered evenly, the deepest chain a six-node
# graph admits is reached in a fraction of a percent of examples, because every one
# of the five later nodes has to descend for it, and the deep end is the whole point
# of a derivation chain. Descending is therefore the common case and a root the
# uncommon one, while spreading stays frequent enough to keep several parents on one
# node ordinary.
ATTACHMENT_OFFERING: Final[tuple[Attachment, ...]] = (
    Attachment.ROOT,
    Attachment.DESCEND,
    Attachment.DESCEND,
    Attachment.DESCEND,
    Attachment.DESCEND,
    Attachment.SPREAD,
    Attachment.SPREAD,
)


@dataclass(frozen=True, slots=True)
class NodePlan:
    """One Artifact of a drawn lineage graph, and the three sources of evidence for it.

    Attributes:
        owner_index: The Client owning the Session the Artifact was produced under,
            which is its scope claim.
        parent_indices: The direct parents, always earlier nodes, so a graph is
            acyclic by construction and its depth is bounded by its node count.
        marker_indices: The Clients whose configured marker is planted in the text.
            Empty when the node carries no text, and always a subset of the Clients
            that configured a marker at all.
        carries_text: Whether the Artifact has text for the marker pass to read.
        planted: One residue claim written after this node's own detection, or None.
    """

    owner_index: int
    parent_indices: tuple[int, ...]
    marker_indices: tuple[int, ...]
    carries_text: bool
    planted: PlantedClaim | None


@dataclass(frozen=True, slots=True)
class DerivationChain:
    """A drawn lineage graph over a small set of Clients.

    Attributes:
        client_count: How many Clients the graph's evidence is spread over.
        marker_flags: Which of those Clients configured a content marker.
        nodes: The Artifacts, in the order they are written.
    """

    client_count: int
    marker_flags: tuple[bool, ...]
    nodes: tuple[NodePlan, ...]

    @property
    def size(self) -> int:
        """How many Artifacts this graph holds."""
        return len(self.nodes)

    @property
    def depths(self) -> tuple[int, ...]:
        """The longest path in edges from a root to each node, computed in one pass."""
        found: list[int] = []
        for node in self.nodes:
            found.append(
                0 if not node.parent_indices else 1 + max(found[at] for at in node.parent_indices)
            )
        return tuple(found)

    @property
    def ancestries(self) -> tuple[frozenset[int], ...]:
        """Every ancestor of each node, accumulated forward rather than by traversal.

        A node's ancestry is its parents together with their own ancestries, and
        every parent sits at an earlier position, so one forward pass suffices and
        no node is read before it has been computed. That is also why nothing here
        walks the graph: the closure is carried along as the nodes are visited.
        """
        found: list[frozenset[int]] = []
        for node in self.nodes:
            reached: set[int] = set()
            for at in node.parent_indices:
                reached.add(at)
                reached |= found[at]
            found.append(frozenset(reached))
        return tuple(found)


# ---------------------------------------------------------------------------
# The model the stored claims are compared against
# ---------------------------------------------------------------------------


def outranks(candidate: Claim, held: Claim) -> bool:
    """Whether one claim about a Client is the stronger of two.

    Confidence decides first and the kind decides a tie, so the result depends on
    neither the order the kinds were produced in nor the order they were written in.
    """
    if candidate.confidence != held.confidence:
        return candidate.confidence > held.confidence
    return PRECEDENCE.index(candidate.method) < PRECEDENCE.index(held.method)


def collapsed(detections: Sequence[tuple[int, Claim]]) -> dict[int, Claim]:
    """One claim per Client, the strongest, from every detection a node produces."""
    strongest: dict[int, Claim] = {}
    for client_index, claim in detections:
        held = strongest.get(client_index)
        if held is None or outranks(claim, held):
            strongest[client_index] = claim
    return strongest


def after_submission(held: Claim | None, submitted: Claim) -> Claim:
    """What a pair holds after one submission, by the rule the write path applies.

    Three branches. A pair holding nothing takes the submission as it stands. A
    submission carrying the same method and no greater confidence says nothing new
    and leaves the claim alone. Anything else supersedes, and the successor carries
    the submitted method with the greater of the two confidences, because the
    comparison the cluster evaluates is against the confidence it just closed.
    """
    if held is None:
        return submitted
    if submitted.method is held.method and submitted.confidence <= held.confidence:
        return held
    return Claim(
        method=submitted.method,
        confidence=max(submitted.confidence, held.confidence),
    )


def inherited_from(parents: Sequence[Mapping[int, Claim]]) -> dict[int, float]:
    """The confidence each Client's inherited claim carries, greatest across the parents."""
    greatest: dict[int, float] = {}
    for held in parents:
        for client_index, claim in held.items():
            standing = greatest.get(client_index, CONFIDENCE_FLOOR)
            greatest[client_index] = max(standing, claim.confidence)
    return greatest


@dataclass(frozen=True, slots=True)
class ExpectedNode:
    """What one node ought to hold, before and after its planted claim.

    Attributes:
        detected: The live claim per Client the three kinds alone produce, which is
            what every clause of the property is stated over.
        settled: The live claim per Client after the planted residue claim, which is
            what this node's children inherit.
    """

    detected: Mapping[int, Claim]
    settled: Mapping[int, Claim]


def reference_nodes(chain: DerivationChain) -> tuple[ExpectedNode, ...]:
    """Walk a drawn graph and return what every node ought to hold.

    This is the independent model every assertion is compared against, computed from
    the drawn nodes alone. Each node's detections are the scope claim at certainty,
    one inherited claim per Client bound to any parent at the greatest confidence
    those parents hold, and one marker claim per Client whose marker the text
    carries; the collapse resolves a Client claimed by more than one kind.
    """
    expected: list[ExpectedNode] = []
    for node in chain.nodes:
        parents = [expected[at].settled for at in node.parent_indices]
        detections: list[tuple[int, Claim]] = [
            (node.owner_index, Claim(method=BindingMethod.SCOPE, confidence=SCOPE_CONFIDENCE))
        ]
        detections.extend(
            (client_index, Claim(method=BindingMethod.INHERITED, confidence=confidence))
            for client_index, confidence in inherited_from(parents).items()
        )
        detections.extend(
            (client_index, Claim(method=BindingMethod.MARKER, confidence=MARKER_CONFIDENCE))
            for client_index in node.marker_indices
        )
        detected = collapsed(detections)
        settled = dict(detected)
        if node.planted is not None:
            settled[node.planted.client_index] = after_submission(
                detected.get(node.planted.client_index),
                Claim(method=BindingMethod.RESIDUE, confidence=node.planted.confidence),
            )
        expected.append(ExpectedNode(detected=detected, settled=settled))
    return tuple(expected)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def confidences() -> st.SearchStrategy[float]:
    """Draw one confidence from the closed unit interval, both ends included.

    The ends are a branch of their own rather than left to the real generator to
    reach: a parent claiming a Client at the floor and a parent claiming one at
    certainty are the two edges the non-increase clause is stated at.
    """
    return st.one_of(
        st.sampled_from((CONFIDENCE_FLOOR, CONFIDENCE_CEILING)),
        st.floats(
            min_value=CONFIDENCE_FLOOR,
            max_value=CONFIDENCE_CEILING,
            allow_nan=False,
            allow_infinity=False,
        ),
    )


def parent_choices(
    attachment: Attachment,
    placed: int,
    depths: Sequence[int],
) -> st.SearchStrategy[tuple[int, ...]]:
    """Draw the parents of the next node, in the shape its attachment names.

    A root names none. A descending node names the deepest node placed so far
    together with up to two others, which is what makes a graph really nest, because
    a parent drawn uniformly is usually a shallow one. A spreading node names an
    arbitrary parent set, which is what puts two and three parents on one node so a
    Client can be claimed by several parents at different confidences at once.

    Every parent is an earlier node, so a drawn graph is acyclic by construction and
    its depth cannot exceed its node count, which is what bounds the traversal at
    both ends: the detector reads direct parents in one statement whatever the
    ancestry, and the model above walks the nodes once in order.
    """
    if placed == 0 or attachment is Attachment.ROOT:
        return st.just(())
    if attachment is Attachment.DESCEND:
        deepest = max(range(placed), key=lambda index: depths[index])
        return st.lists(
            st.integers(min_value=0, max_value=placed - 1),
            max_size=MAX_FAN_IN - 1,
            unique=True,
        ).map(lambda rest: tuple(dict.fromkeys((deepest, *rest))))
    return st.lists(
        st.integers(min_value=0, max_value=placed - 1),
        min_size=1,
        max_size=MAX_FAN_IN,
        unique=True,
    ).map(tuple)


def planted_markers(
    marked: Sequence[int], carries_text: bool
) -> st.SearchStrategy[tuple[int, ...]]:
    """Draw which Clients' markers the node's text carries.

    A node with no text evidences nothing about markers, and a Client that
    configured none can never be matched, so both cases resolve to the empty set
    rather than being drawn and discarded.
    """
    if not carries_text or not marked:
        return st.just(())
    return st.lists(st.sampled_from(marked), max_size=len(marked), unique=True).map(
        lambda drawn: tuple(sorted(drawn))
    )


def planted_claims(client_count: int) -> st.SearchStrategy[PlantedClaim | None]:
    """Draw the residue claim written for a node after its own detection, or none."""
    return st.one_of(
        st.none(),
        st.builds(
            PlantedClaim,
            st.integers(min_value=0, max_value=client_count - 1),
            confidences(),
        ),
    )


@st.composite
def derivation_chains(draw: st.DrawFn) -> DerivationChain:
    """Draw a lineage graph of 2 to 6 Artifacts over 2 to 5 Clients.

    The Clients are drawn first, because every later choice is relative to them: the
    owner of a node, the markers its text carries, and the Client a planted claim
    names all come from the same small set, which is what makes a Client claimed by
    two and three kinds at once an ordinary member of a drawn graph rather than a
    coincidence.

    The size is drawn as a number before the nodes are drawn, rather than left to a
    list's own sizing, because a list generator concentrates near its lower bound and
    the deep graphs are the interesting end: a six-node graph is where a chain, a
    node with several parents, and a Client inherited from more than one of them all
    appear at once.
    """
    client_count = draw(st.integers(min_value=MIN_CLIENTS, max_value=MAX_CLIENTS))
    marker_flags = tuple(
        draw(st.lists(st.booleans(), min_size=client_count, max_size=client_count))
    )
    marked = [index for index, configured in enumerate(marker_flags) if configured]

    size = draw(st.integers(min_value=MIN_NODES, max_value=MAX_NODES))
    nodes: list[NodePlan] = []
    depths: list[int] = []
    for placed in range(size):
        attachment = draw(st.sampled_from(ATTACHMENT_OFFERING))
        parent_indices = draw(parent_choices(attachment, placed, depths))
        carries_text = draw(st.booleans())
        nodes.append(
            NodePlan(
                owner_index=draw(st.integers(min_value=0, max_value=client_count - 1)),
                parent_indices=parent_indices,
                marker_indices=draw(planted_markers(marked, carries_text)),
                carries_text=carries_text,
                planted=draw(planted_claims(client_count)),
            )
        )
        depths.append(0 if not parent_indices else 1 + max(depths[at] for at in parent_indices))
    return DerivationChain(
        client_count=client_count,
        marker_flags=marker_flags,
        nodes=tuple(nodes),
    )


# ---------------------------------------------------------------------------
# The cluster the graph is stored on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tenant:
    """One Client of an example, with the marker it configured or none.

    The marker is derived from the Client's own identifier rather than chosen. The
    schema is shared across the examples of this module and the marker read returns
    every Client in it that configured one, so a marker that repeated across
    examples would let one example's text bind another example's tenant, and the
    assertions would then be about tenants the example never drew.
    """

    client_id: UUID
    marker: str | None


@dataclass(frozen=True, slots=True)
class StoredClaim:
    """One live claim, read back column by column."""

    method: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the reads of this module."""

    store: MemoryStore
    connection: DriverConnection

    def tenant(self, *, marked: bool) -> Tenant:
        """Place one Client, configuring a marker of its own when asked for one."""
        identifier = uuid4()
        marker = f"m{identifier.hex[:12]}" if marked else None
        self.send(
            INSERT_CLIENT,
            (
                identifier,
                f"tenant-{identifier.hex[:10]}",
                "Tenant",
                "eu",
                [] if marker is None else [marker],
            ),
        )
        return Tenant(client_id=identifier, marker=marker)

    def session(self, client_id: UUID) -> UUID:
        """Place one Session, which every Event of a supersession belongs to."""
        identifier = uuid4()
        self.send(INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
        return identifier

    def context(self, session_id: UUID) -> SupersessionContext:
        """The Session context a supersession Event is recorded within."""
        return SupersessionContext(
            session_id=session_id,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=DETECTED_AT + RETENTION,
        )

    def send(self, statement: str, params: tuple[object, ...]) -> None:
        """Send one parameterised statement on the fixture's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)

    def live(self, artifact_id: UUID) -> dict[UUID, StoredClaim]:
        """The live claim per Client for one Artifact, read on the fixture's connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(SELECT_LIVE_CLAIMS, (artifact_id,))
            rows = list(cursor.fetchall())
        return {
            _as_uuid(row[0]): StoredClaim(method=str(row[1]), confidence=float(str(row[2])))
            for row in rows
        }


def _as_uuid(value: object) -> UUID:
    """Narrow a stored identifier, refusing anything else."""
    return value if isinstance(value, UUID) else UUID(str(value))


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store whose connections see that schema.

    Every migration is applied because the partial unique index, the supersession
    Event category, and the marker column all arrive with later migrations, and the
    self-referencing constraint that would refuse the ordered pair of statements a
    supersession sends is dropped by the protection migration.

    Module scope is what keeps the schema cost paid once: examples are isolated from
    each other by tenants and Artifacts of their own rather than by a schema of their
    own.
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

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


# ---------------------------------------------------------------------------
# Sending a drawn graph
# ---------------------------------------------------------------------------


def reference(artifact_id: UUID, owner: UUID) -> ArtifactRef:
    """A reference to one Artifact of this module."""
    return ArtifactRef(id=artifact_id, kind=ArtifactKind.DERIVED_ARTIFACT, client_id=owner)


def text_of(node: NodePlan, tenants: Sequence[Tenant]) -> str | None:
    """The Artifact text a drawn node carries, or None when it carries none.

    Every planted marker is surrounded by whitespace, so both edges of an occurrence
    sit at a token boundary and the match is the Client's own name occurring in the
    text rather than a fragment inside a longer word. A node carrying text but no
    planted marker is the case where the marker read runs and matches nothing.
    """
    if not node.carries_text:
        return None
    planted = [tenants[index].marker for index in node.marker_indices]
    return " ".join((TEXT_PREFIX, *(marker for marker in planted if marker), TEXT_SUFFIX))


def request_for(
    chain: DerivationChain,
    index: int,
    artifact_id: UUID,
    tenants: Sequence[Tenant],
    placed: Sequence[UUID],
    *,
    reverse_parents: bool = False,
) -> DetectionRequest:
    """The detection request one drawn node is written through.

    Reversing the parents is one half of the permuted emission order: the parent
    identifiers reach the inheritance read as one bound array, and what a child
    inherits must not depend on the order they sit in it.
    """
    node = chain.nodes[index]
    owner = tenants[node.owner_index].client_id
    parents = [
        reference(placed[at], tenants[chain.nodes[at].owner_index].client_id)
        for at in node.parent_indices
    ]
    ordered = tuple(reversed(parents)) if reverse_parents else tuple(parents)
    return DetectionRequest(
        artifact=reference(artifact_id, owner),
        scope_client_id=owner,
        text=text_of(node, tenants),
        parents=ordered,
    )


def plant(
    cluster: Cluster,
    claim: PlantedClaim,
    artifact_id: UUID,
    tenants: Sequence[Tenant],
    context: SupersessionContext,
) -> None:
    """Write one residue claim for a node through the shipped attribution path."""
    write_attribution(
        cluster.store,
        AttributionSubmission(
            artifact_id=artifact_id,
            artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=tenants[claim.client_index].client_id,
            method=BindingMethod.RESIDUE,
            confidence=claim.confidence,
            detected_at=DETECTED_AT,
        ),
        context=context,
    )


def write_reversed(
    cluster: Cluster,
    request: DetectionRequest,
    context: SupersessionContext,
) -> None:
    """Detect one Artifact's bindings and submit the collapsed claims back to front.

    The detector collapses its three kinds into one submission per Client before any
    of them is written, so the order those submissions are sent in must not reach
    stored state. Composing the two steps here is what lets that order be permuted
    from outside without touching the module under test.
    """

    def body(cursor: Cursor) -> None:
        detections = bindings_for(cursor, request)
        for detection in reversed(detections):
            record_attribution(
                cursor,
                AttributionSubmission(
                    artifact_id=request.artifact.id,
                    artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                    client_id=detection.client_id,
                    method=detection.method,
                    confidence=detection.confidence,
                    detected_at=DETECTED_AT,
                ),
                context=context,
            )

    cluster.store.in_serializable(body)


# ---------------------------------------------------------------------------
# The coverage record
# ---------------------------------------------------------------------------


def count_band(count: int) -> str:
    """How often a shape occurred, for the coverage record."""
    if count == 0:
        return "none"
    if count == 1:
        return "1"
    if count <= 3:
        return "2-3"
    return "4+"


def kind_tally(chain: DerivationChain, expected: Sequence[ExpectedNode]) -> dict[str, int]:
    """How many pairs each collision between the three kinds occurred for.

    A pair claimed by more than one kind is where the collapse decides what is
    stored, so the record names which pairings an example really produced rather
    than which it could have.
    """
    tally: dict[str, int] = {}
    for index, node in enumerate(chain.nodes):
        parents = [expected[at].settled for at in node.parent_indices]
        inherited = set(inherited_from(parents))
        for client_index in expected[index].detected:
            reached = {
                "scope" if client_index == node.owner_index else "",
                "marker" if client_index in node.marker_indices else "",
                "inherited" if client_index in inherited else "",
            } - {""}
            if len(reached) > 1:
                name = "+".join(sorted(reached))
                tally[name] = tally.get(name, 0) + 1
    return tally


def excess_sources(
    chain: DerivationChain,
    index: int,
    parent_index: int,
    client_index: int,
    held: float,
    expected: Sequence[ExpectedNode],
) -> tuple[str, ...]:
    """The evidence that could supply a confidence higher than one parent's own.

    A rise across an inheritance edge is admissible only where something other than
    that parent supplies the value: the child's own scope claim at certainty, a
    marker match on the child's text just below it, or another parent holding a
    stronger claim on the same Client. Each is checked against the drawn graph, so a
    rise no source accounts for is a failure rather than a tolerated difference.
    """
    node = chain.nodes[index]
    found: list[str] = []
    if client_index == node.owner_index and held == SCOPE_CONFIDENCE:
        found.append("the child's own scope claim")
    if client_index in node.marker_indices and held == MARKER_CONFIDENCE:
        found.append("a marker match on the child")
    for other in node.parent_indices:
        if other == parent_index:
            continue
        claim = expected[other].settled.get(client_index)
        if claim is not None and claim.confidence >= held:
            found.append("another parent's stronger claim")
            break
    return tuple(found)


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 15: For any derivation chain, the Client set of a
# Derived_Artifact's Client_Bindings is a superset of the union of its parents'
# Client sets.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(chain=derivation_chains())
def test_a_child_carries_every_client_of_its_parents_and_raises_none_of_them(
    cluster: Cluster, chain: DerivationChain
) -> None:
    expected = reference_nodes(chain)
    depths = chain.depths
    ancestries = chain.ancestries
    event(f"nodes={chain.size}")
    event(f"clients={chain.client_count}")
    event(f"deepest node={max(depths)} edge(s) from a root")
    event(f"marked clients={count_band(sum(chain.marker_flags))}")
    event(f"planted claims={count_band(sum(1 for node in chain.nodes if node.planted))}")
    event(
        "multi-parent nodes="
        f"{count_band(sum(1 for node in chain.nodes if len(node.parent_indices) > 1))}"
    )
    event(f"roots={count_band(sum(1 for node in chain.nodes if not node.parent_indices))}")
    for name, count in sorted(kind_tally(chain, expected).items()):
        event(f"collision {name}={count_band(count)}")

    tenants = [cluster.tenant(marked=configured) for configured in chain.marker_flags]
    context = cluster.context(cluster.session(tenants[0].client_id))

    placed: list[UUID] = []
    for index, node in enumerate(chain.nodes):
        artifact_id = uuid4()
        record_bindings(
            cluster.store,
            request_for(chain, index, artifact_id, tenants, placed),
            context=context,
            detected_at=DETECTED_AT,
        )
        stored = cluster.live(artifact_id)

        # The whole live map, against the map the drawn graph makes. Everything
        # below is stated over the stored claims, and this is what licenses reading
        # the model where a stored value would be circular.
        model = {
            tenants[client_index].client_id: claim
            for client_index, claim in expected[index].detected.items()
        }
        assert set(stored) == set(model), (
            f"node {index} holds claims for {len(stored)} Client(s) where the drawn "
            f"graph makes it {len(model)}"
        )
        for client_id, claim in model.items():
            assert stored[client_id].method == claim.method.value, (
                f"node {index} records method {stored[client_id].method} for a Client "
                f"the drawn graph claims by {claim.method.value}"
            )
            assert stored[client_id].confidence == claim.confidence, (
                f"node {index} holds {stored[client_id].confidence} for a Client the "
                f"drawn graph puts at {claim.confidence}"
            )

        # Requirement 12.3, the first half: the Client set of a child contains the
        # union of its parents' Client sets, and by induction the union over its
        # whole ancestry, since a parent's own bindings already carry what it
        # inherited when it was written.
        parent_clients = {
            tenants[client_index].client_id
            for at in node.parent_indices
            for client_index in expected[at].settled
        }
        assert set(stored) >= parent_clients, (
            f"node {index} at depth {depths[index]} is missing "
            f"{len(parent_clients - set(stored))} Client(s) its parents hold, so the "
            "Client set did not grow along the edge"
        )
        ancestor_clients = {
            tenants[client_index].client_id
            for at in ancestries[index]
            for client_index in expected[at].settled
        }
        assert set(stored) >= ancestor_clients, (
            f"node {index} at depth {depths[index]} is missing a Client held by an "
            "ancestor, so inheritance did not close transitively over the chain"
        )

        # Requirement 12.3, the second half: a confidence does not rise across an
        # inheritance edge unless other evidence supplies the higher value, and it
        # never falls below the parent evidence it came from.
        for at in node.parent_indices:
            for client_index, parent_claim in expected[at].settled.items():
                client_id = tenants[client_index].client_id
                held = stored[client_id].confidence
                assert held >= parent_claim.confidence, (
                    f"node {index} holds {held} for a Client its parent claims at "
                    f"{parent_claim.confidence}, so inheritance weakened the parent "
                    "evidence rather than carrying it"
                )
                if held == parent_claim.confidence:
                    event("edge: the parent confidence is carried unchanged")
                    continue
                sources = excess_sources(chain, index, at, client_index, held, expected)
                assert sources, (
                    f"node {index} holds {held} for a Client its parent claims at "
                    f"{parent_claim.confidence}, and neither its scope claim, a marker "
                    "match, nor another parent supplies that value"
                )
                event(f"edge: raised by {sources[0]}")

        if node.planted is not None:
            plant(cluster, node.planted, artifact_id, tenants, context)
            settled = cluster.live(artifact_id)
            assert {
                client_id: (claim.method, claim.confidence) for client_id, claim in settled.items()
            } == {
                tenants[client_index].client_id: (claim.method.value, claim.confidence)
                for client_index, claim in expected[index].settled.items()
            }, f"the planted claim on node {index} did not leave the history it states"

        placed.append(artifact_id)

    # The same evidence in a permuted emission order leaves identical stored state:
    # the parents reversed in the bound array and the collapsed claims submitted back
    # to front. The node with the most claims is chosen because a permutation of one
    # claim asserts nothing, and its parents were settled before it was first
    # written, so the twin reads exactly the state the original read.
    twinned = max(range(chain.size), key=lambda index: (len(expected[index].detected), index))
    twin_id = uuid4()
    event(f"twin claims={len(expected[twinned].detected)}")
    write_reversed(
        cluster,
        request_for(chain, twinned, twin_id, tenants, placed, reverse_parents=True),
        context,
    )
    assert cluster.live(twin_id) == {
        tenants[client_index].client_id: StoredClaim(
            method=claim.method.value,
            confidence=claim.confidence,
        )
        for client_index, claim in expected[twinned].detected.items()
    }, (
        f"the twin of node {twinned} holds different claims from the original, so the "
        "order evidence arrives in or claims are written in reaches stored state"
    )
