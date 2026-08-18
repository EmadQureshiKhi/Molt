"""The lineage views: the fleet graph filtered by Client, and one Artifact's subgraph.

Two routes and one graph builder. `GET /lineage` seeds the graph with the permitted
Clients' own Derived_Artifacts, and `GET /lineage/{artifact_id}` seeds it with one
Artifact and walks both directions from it.

**Every node in a rendered graph is admitted by a tenancy predicate.** The two closures
are the tool server's own statements, which carry the admission inside the statement: a
node appears only if a current, unsuperseded Client_Binding names a permitted Client.
The seed of the per-Artifact view is checked the same way before either closure runs, so
an Artifact that no permitted Client owns or is attributed to is answered exactly as one
that does not exist. The edge read is bounded on *both* endpoints by the node set those
admissions produced, so an edge cannot reach outside the graph it belongs to.

**Layers are longest-path from the roots.** A node sits one layer below its deepest
parent, so an edge always points forward and the reading order of the diagram matches
the derivation order. Nodes are emitted in layer order and each is focusable, so keyboard
traversal follows the same order the eye does.

**The diagram is never the only representation.** Each node carries a `<title>` and an
accessible name stating its kind, its Client bindings, and its creation time; each kind
carries its own text label and its own shape, so colour is never the only channel; and
the same graph is rendered below as an edge-list table, which is the representation that
works without vision.

Every statement here is a whole module-level literal with bound parameters, and the two
closures are imported rather than restated so the console and the tool server admit the
same rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from molt.console.app import console_of
from molt.console.deps import Console
from molt.console.routes.tenancy import (
    ClientChoice,
    client_roster,
    identifier_of,
    permitted_ids,
    render,
    selected_client,
)
from molt.console.routing import register
from molt.mcpserver.tools import (
    SELECT_PERMITTED_ANCESTORS_STATEMENT,
    SELECT_PERMITTED_DESCENDANTS_STATEMENT,
)
from molt.store import Cursor
from molt.store.sessions import artifacts_of_client

__all__ = [
    "ARTIFACT_TEMPLATE",
    "CLOSURE_LIMIT",
    "KIND_SHAPES",
    "LINEAGE_TEMPLATE",
    "SEEDS_PER_CLIENT",
    "SELECT_PERMITTED_ARTIFACT_STATEMENT",
    "SELECT_PERMITTED_EDGES_STATEMENT",
    "SELECT_PERMITTED_NODES_STATEMENT",
    "ArtifactRow",
    "GraphEdge",
    "GraphNode",
    "LineageGraph",
    "graph_of",
    "lineage",
    "lineage_artifact",
]

LINEAGE_TEMPLATE: Final[str] = "lineage.html"
ARTIFACT_TEMPLATE: Final[str] = "lineage_artifact.html"

# The bounds. Both are per request rather than per deployment, because a graph is a
# picture an operator reads and an unbounded one is neither readable nor cheap.
SEEDS_PER_CLIENT: Final[int] = 25
CLOSURE_LIMIT: Final[int] = 200

# One shape and one text label per kind, so a reader never depends on colour. The shape
# name is what the template selects its element by.
KIND_SHAPES: Final[Mapping[str, str]] = {
    "event": "rectangle",
    "session": "ellipse",
    "derived_artifact": "diamond",
    "embedding": "circle",
}
DEFAULT_SHAPE: Final[str] = "rectangle"

# Whether one Artifact may be shown at all: it is owned by a permitted Client, or a
# current Client_Binding names one. The predicate is the admission the closures carry,
# applied to the seed before either of them runs.
SELECT_PERMITTED_ARTIFACT_STATEMENT: Final[str] = (
    "SELECT d.id, d.kind, d.owner_client_id, d.content_digest, d.created_at "
    "FROM derived_artifact AS d "
    "WHERE d.id = %s AND (d.owner_client_id = ANY (%s::UUID[]) OR EXISTS ("
    "SELECT 1 FROM client_binding AS b "
    "WHERE b.artifact_id = d.id AND b.client_id = ANY (%s::UUID[]) "
    "AND b.superseded_by IS NULL"
    "))"
)

# The descriptive columns of the nodes a closure returned, admitted by the same
# predicate a second time. Reading them again under the predicate rather than trusting
# the closure's output keeps every row this view renders covered by one rule.
SELECT_PERMITTED_NODES_STATEMENT: Final[str] = (
    "SELECT d.id, d.kind, d.owner_client_id, d.content_digest, d.created_at "
    "FROM derived_artifact AS d "
    "WHERE d.id = ANY (%s::UUID[]) AND (d.owner_client_id = ANY (%s::UUID[]) OR EXISTS ("
    "SELECT 1 FROM client_binding AS b "
    "WHERE b.artifact_id = d.id AND b.client_id = ANY (%s::UUID[]) "
    "AND b.superseded_by IS NULL"
    ")) "
    "ORDER BY d.created_at, d.id"
)

# The edges of the graph, bounded on both endpoints by the admitted node set. An edge
# whose other end is outside that set is not part of the graph being rendered, so it is
# not read.
SELECT_PERMITTED_EDGES_STATEMENT: Final[str] = (
    "SELECT e.child_id, e.parent_id, e.parent_kind, e.derivation_method "
    "FROM lineage_edge AS e "
    "WHERE e.child_id = ANY (%s::UUID[]) AND e.parent_id = ANY (%s::UUID[]) "
    "ORDER BY e.child_id, e.parent_id"
)

_ARTIFACT_ROW_WIDTH: Final[int] = 5
_EDGE_ROW_WIDTH: Final[int] = 4
_DERIVED_KIND: Final[str] = "derived_artifact"
_NOT_FOUND: Final[dict[str, str]] = {"error": "no such Artifact"}
_NOT_FOUND_STATUS: Final[int] = 404


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One node of a rendered graph, with everything its accessible name states."""

    id: UUID
    kind: str
    layer: int
    order: int
    client_label: str
    created_at: datetime | None
    detail: str = ""

    @property
    def shape(self) -> str:
        """The shape this kind is drawn as, so kind is legible without colour."""
        return KIND_SHAPES.get(self.kind, DEFAULT_SHAPE)

    @property
    def short_id(self) -> str:
        """The leading field of the identifier, which is what the label shows."""
        return str(self.id).split("-")[0]

    @property
    def has_subgraph(self) -> bool:
        """Whether this node has a page of its own to open.

        Only a Derived_Artifact does. The subgraph route walks the derivation graph from
        an Artifact, and a closure reaches nodes that are not Artifacts at all — the Event
        that produced one, the Session it happened in — because omitting them would draw an
        edge to nothing. Those are nodes to read, not nodes to open.

        Stated here rather than in the template because the graph renders each node in
        three places, and a link condition written three times is a link condition that
        comes to disagree with itself. Every node was linked, so two thirds of the diagram
        offered a reader a page that answers *no such Artifact*.
        """
        return self.kind == _DERIVED_KIND

    @property
    def accessible_name(self) -> str:
        """Kind and Client bindings, as one sentence.

        The creation instant used to close this sentence and no longer does. It was the
        machine rendering the cluster returns — a full date, a time to the microsecond,
        and an offset — and read aloud by a screen reader it was a long string of digits
        in front of the two facts a reader of a lineage node actually needs, which are
        what the node is and whose it is. The graph's own shape carries the ordering the
        instant was standing in for: an edge points from a parent to the child it
        produced, and the layers are computed from those edges.
        """
        bindings = self.client_label or "no permitted Client binding recorded"
        named = self.kind if not self.detail else f"{self.kind} of kind {self.detail}"
        return f"{named} {self.short_id}, bound to {bindings}"


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One derivation edge, pointing from a parent Artifact to the child it produced."""

    child_id: UUID
    parent_id: UUID
    parent_kind: str
    derivation_method: str


@dataclass(frozen=True, slots=True)
class LineageGraph:
    """One graph as both representations read it: placed nodes and an edge list."""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

    @property
    def depth(self) -> int:
        """How many layers the diagram has, which sets its width."""
        return 0 if not self.nodes else max(node.layer for node in self.nodes) + 1

    @property
    def width(self) -> int:
        """How many nodes the fullest layer holds, which sets the diagram's height."""
        if not self.nodes:
            return 0
        return max(
            sum(1 for node in self.nodes if node.layer == layer) for layer in range(self.depth)
        )

    def node_named(self, identifier: UUID) -> GraphNode | None:
        """The node one identifier reaches, or None when the graph holds none."""
        for node in self.nodes:
            if node.id == identifier:
                return node
        return None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    """One admitted Derived_Artifact row, before it becomes a node."""

    id: UUID
    kind: str
    owner_client_id: UUID
    content_digest: str
    created_at: datetime


def _artifact_row_of(row: Sequence[object]) -> ArtifactRow:
    """Narrow one admitted Artifact row, refusing a width the statement did not select."""
    if len(row) != _ARTIFACT_ROW_WIDTH:
        raise ValueError(f"an Artifact row carries {len(row)} column(s) where 5 were selected")
    created = row[4]
    if not isinstance(created, datetime):
        raise ValueError("an Artifact creation timestamp was not returned as an instant")
    return ArtifactRow(
        id=_uuid_of(row[0]),
        kind=str(row[1]),
        owner_client_id=_uuid_of(row[2]),
        content_digest=str(row[3]),
        created_at=created,
    )


def _uuid_of(value: object) -> UUID:
    """One identifier column, whichever representation the driver returned it in."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _permitted_artifact(
    console: Console, artifact_id: UUID, permitted: Sequence[UUID]
) -> ArtifactRow | None:
    """The seed Artifact, or None when no permitted Client owns or is bound to it."""
    scope = list(permitted)
    if not scope:
        return None

    def body(opened: Cursor) -> ArtifactRow | None:
        opened.execute(SELECT_PERMITTED_ARTIFACT_STATEMENT, (artifact_id, scope, scope))
        row = opened.fetchone()
        return None if row is None else _artifact_row_of(row)

    return console.read_only_store().read(body)


def _permitted_nodes(
    console: Console, identifiers: Sequence[UUID], permitted: Sequence[UUID]
) -> tuple[ArtifactRow, ...]:
    """The admitted Derived_Artifact rows among a set of identifiers."""
    seeds = list(dict.fromkeys(identifiers))
    scope = list(permitted)
    if not seeds or not scope:
        return ()

    def body(opened: Cursor) -> tuple[ArtifactRow, ...]:
        opened.execute(SELECT_PERMITTED_NODES_STATEMENT, (seeds, scope, scope))
        return tuple(_artifact_row_of(row) for row in opened.fetchall())

    return console.read_only_store().read(body)


def _closure(
    console: Console, statement: str, seeds: Sequence[UUID], permitted: Sequence[UUID]
) -> tuple[tuple[UUID, str], ...]:
    """Run one admitted closure, returning each reached node with the kind it carries."""
    seeded = list(dict.fromkeys(seeds))
    scope = list(permitted)
    if not seeded or not scope:
        return ()

    def body(opened: Cursor) -> tuple[tuple[UUID, str], ...]:
        opened.execute(statement, (seeded, scope, CLOSURE_LIMIT))
        return tuple(
            (_uuid_of(row[0]), _DERIVED_KIND if len(row) < 2 else str(row[1]))
            for row in opened.fetchall()
        )

    return console.read_only_store().read(body)


def _permitted_edges(console: Console, identifiers: Sequence[UUID]) -> tuple[GraphEdge, ...]:
    """The edges whose both endpoints are in the admitted node set."""
    nodes = list(dict.fromkeys(identifiers))
    if not nodes:
        return ()

    def body(opened: Cursor) -> tuple[GraphEdge, ...]:
        opened.execute(SELECT_PERMITTED_EDGES_STATEMENT, (nodes, nodes))
        return tuple(_edge_of(row) for row in opened.fetchall())

    return console.read_only_store().read(body)


def _edge_of(row: Sequence[object]) -> GraphEdge:
    """Narrow one edge row, refusing a width the statement did not select."""
    if len(row) != _EDGE_ROW_WIDTH:
        raise ValueError(f"an edge row carries {len(row)} column(s) where 4 were selected")
    return GraphEdge(
        child_id=_uuid_of(row[0]),
        parent_id=_uuid_of(row[1]),
        parent_kind=str(row[2]),
        derivation_method=str(row[3]),
    )


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def _layers(nodes: Iterable[UUID], edges: Sequence[GraphEdge]) -> dict[UUID, int]:
    """Assign each node its longest path from a root, so every edge points forward.

    The walk is iterative and bounded by the node count, because the closures admit no
    cycle and a bound is cheaper than trusting that.
    """
    parents: dict[UUID, list[UUID]] = {}
    known = list(dict.fromkeys(nodes))
    for identifier in known:
        parents[identifier] = []
    for edge in edges:
        if edge.child_id in parents and edge.parent_id in parents:
            parents[edge.child_id].append(edge.parent_id)
    assigned: dict[UUID, int] = dict.fromkeys(known, 0)
    for _ in range(len(known)):
        moved = False
        for identifier in known:
            deepest = max((assigned[parent] + 1 for parent in parents[identifier]), default=0)
            if deepest > assigned[identifier]:
                assigned[identifier] = deepest
                moved = True
        if not moved:
            break
    return assigned


def graph_of(
    rows: Sequence[ArtifactRow],
    extra: Sequence[tuple[UUID, str]],
    edges: Sequence[GraphEdge],
    labels: Mapping[UUID, str],
) -> LineageGraph:
    """Assemble one graph from admitted rows, admitted closure nodes, and its edges.

    `rows` carry a creation time and an owning Client; `extra` are nodes a closure
    reached whose kind is not a Derived_Artifact, so no Artifact row describes them.
    Both become nodes, because omitting the second kind would draw an edge to nothing.
    """
    described = {row.id: row for row in rows}
    identifiers = list(described) + [
        identifier for identifier, _ in extra if identifier not in described
    ]
    kinds = dict(extra)
    placed = _layers(identifiers, edges)
    counts: dict[int, int] = {}
    nodes: list[GraphNode] = []
    for identifier in sorted(identifiers, key=lambda value: (placed[value], str(value))):
        layer = placed[identifier]
        order = counts.get(layer, 0)
        counts[layer] = order + 1
        row = described.get(identifier)
        nodes.append(
            GraphNode(
                id=identifier,
                kind=_DERIVED_KIND if row is not None else kinds.get(identifier, _DERIVED_KIND),
                layer=layer,
                order=order,
                client_label="" if row is None else labels.get(row.owner_client_id, ""),
                created_at=None if row is None else row.created_at,
                detail="" if row is None else row.kind,
            )
        )
    kept = {node.id for node in nodes}
    return LineageGraph(
        nodes=tuple(nodes),
        edges=tuple(edge for edge in edges if edge.child_id in kept and edge.parent_id in kept),
    )


def _labels(roster: Sequence[ClientChoice]) -> dict[UUID, str]:
    """The Client label each permitted identifier renders as."""
    return {choice.id: choice.label for choice in roster}


# ---------------------------------------------------------------------------
# The two views
# ---------------------------------------------------------------------------


@register("lineage")
async def lineage(request: Request) -> Response:
    """The fleet lineage graph, seeded with the permitted Clients' own Artifacts."""
    console = console_of(request)
    roster = client_roster(console)
    chosen, recognised = selected_client(request, roster)
    scope = permitted_ids(roster, chosen) if recognised else ()
    covered = (chosen,) if chosen is not None else roster
    seeds: list[UUID] = []
    if recognised:
        for choice in covered:
            seeds.extend(
                summary.id
                for summary in artifacts_of_client(
                    console.read_only_store(), choice.id, limit=SEEDS_PER_CLIENT
                )
            )
    reached = _closure(console, SELECT_PERMITTED_ANCESTORS_STATEMENT, seeds, scope)
    described = _permitted_nodes(console, seeds + [identifier for identifier, _ in reached], scope)
    identifiers = [row.id for row in described] + [identifier for identifier, _ in reached]
    graph = graph_of(described, reached, _permitted_edges(console, identifiers), _labels(roster))
    return render(
        request,
        LINEAGE_TEMPLATE,
        {
            "title": "Lineage",
            "roster": roster,
            "chosen": chosen,
            "filter_recognised": recognised,
            "graph": graph,
        },
    )


@register("lineage_artifact")
async def lineage_artifact(request: Request) -> Response:
    """The ancestor and descendant subgraph of one Artifact a permitted Client may see."""
    console = console_of(request)
    artifact_id = identifier_of(str(request.path_params.get("artifact_id", "")))
    if artifact_id is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    roster = client_roster(console)
    scope = permitted_ids(roster, None)
    seed = _permitted_artifact(console, artifact_id, scope)
    if seed is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    ancestors = _closure(console, SELECT_PERMITTED_ANCESTORS_STATEMENT, (artifact_id,), scope)
    descendants = _closure(console, SELECT_PERMITTED_DESCENDANTS_STATEMENT, (artifact_id,), scope)
    reached = ancestors + descendants
    described = _permitted_nodes(
        console, [artifact_id, *(identifier for identifier, _ in reached)], scope
    )
    identifiers = [row.id for row in described] + [identifier for identifier, _ in reached]
    graph = graph_of(described, reached, _permitted_edges(console, identifiers), _labels(roster))
    return render(
        request,
        ARTIFACT_TEMPLATE,
        {
            "title": "Artifact lineage",
            "graph": graph,
            "focus": graph.node_named(artifact_id),
            "digest": seed.content_digest,
            "ancestor_count": len(ancestors),
            "descendant_count": len(descendants),
        },
    )
