"""The tool registry: four read-only tools and the dispatch that admits only them.

The registry is a module-level tuple built once at import. Dispatch resolves a
requested name against that tuple and against nothing else, so a mutation tool is
not refused by a check that could be edited away — it has no entry to be reached
through. The absence is structural in a second way as well: `ToolEffect` declares
one member, so there is no effect value a mutating entry could carry, and the
server connects with the reader role, so a statement that tried to write would be
refused by the cluster's own privilege check.

**Tenancy is resolved once, at startup, and is not an argument.** No schema below
declares a client-set parameter, so a caller has no field to widen its reach with,
and an argument naming one is a key dispatch never reads. The permitted set the
backend carries is applied inside SQL, as a semi-join over unsuperseded
Attribution_Versions, exactly as the Recall_Engine applies it: the recall tool
reaches that filter by calling the engine, and the two lineage tools carry the
same term in their own statements because a closure filtered after it is returned
is a closure that crossed the wire holding rows the caller may not see.

**The bound is a SQL limit rather than a trim.** Every tool passes the configured
maximum into the statement that produces its rows. The residue tool has two
statement bounds rather than one — the query-Artifact limit and the neighbour
bound — and their product is what the pass can report, so the neighbour bound is
divided down until the product fits the maximum. Nothing is discarded after the
cluster answered.

Every statement here is a whole module-level literal with bound parameters, and no
identifier and no domain value reaches statement text. The one term the two
closures share with the rest of the system, the attribution layer's reading of a
current claim, is checked into them at import rather than concatenated in, so there
is one definition of *unsuperseded* behind all of them and still nothing composed
around a name.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from molt.config.resolve import Configuration
from molt.erase.residue import ResidueFinding, ResiduePolicy, residue_report
from molt.errors import MoltError, StoreError
from molt.models.artifact import ArtifactKind
from molt.models.event import JsonObject, JsonValue
from molt.recall import RecallEngine
from molt.store import Cursor, MemoryStore
from molt.store.attribution import CURRENT_VERSION_PREDICATE

__all__ = [
    "ANCESTORS_TOOL",
    "COMPONENT",
    "DEFAULT_MAX_RESULTS",
    "DESCENDANTS_TOOL",
    "PERMITTED_CLIENT_IDS_STATEMENT",
    "RECALL_TOOL",
    "REGISTRY",
    "RESIDUE_TOOL",
    "SELECT_PERMITTED_ANCESTORS_STATEMENT",
    "SELECT_PERMITTED_DESCENDANTS_STATEMENT",
    "TOOL_NAMES",
    "ArgumentKind",
    "ArgumentSpec",
    "McpSettings",
    "ResultShape",
    "Tool",
    "ToolBackend",
    "ToolEffect",
    "ToolResult",
    "UnknownToolError",
    "cluster_reachable",
    "dispatch",
    "permitted_client_ids",
    "tool_named",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "mcpserver"

# The configuration keys this server reads. Nothing below is defaulted in code
# beyond what the surface itself declares, so an operator changes one place.
TRANSPORT_KEY: Final[str] = "MOLT_MCP_TRANSPORT"
BIND_KEY: Final[str] = "MOLT_MCP_BIND"
PERMITTED_CLIENTS_KEY: Final[str] = "MOLT_MCP_PERMITTED_CLIENTS"
MAX_RESULTS_KEY: Final[str] = "MOLT_MCP_MAX_RESULTS"

# The bound the surface declares when an operator names none. Stated here so a
# reader of this module can see the figure the requirement names, and read from
# the surface rather than from this constant at every use.
DEFAULT_MAX_RESULTS: Final[int] = 50

# The two transports a configured server may speak.
STDIO_TRANSPORT: Final[str] = "stdio"
HTTP_TRANSPORT: Final[str] = "http"

# The names the four tools are reached by. They are constants because the
# specification document, the registry, and the dispatch all name the same four.
RECALL_TOOL: Final[str] = "molt.recall"
ANCESTORS_TOOL: Final[str] = "molt.lineage_ancestors"
DESCENDANTS_TOOL: Final[str] = "molt.lineage_descendants"
RESIDUE_TOOL: Final[str] = "molt.residue_candidates"

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The permitted Client set, resolved from configured slugs at startup. The slugs
# travel as one bound array, so a slug holding a quote character is data.
PERMITTED_CLIENT_IDS_STATEMENT: Final[str] = (
    "SELECT id FROM client WHERE slug = ANY (%s::STRING[]) ORDER BY slug"
)

# Whether the cluster answers at all, which is what the health route reports.
REACHABILITY_STATEMENT: Final[str] = "SELECT 1"

# The two closures, each carrying the tenancy admission and the bound inside the
# statement. The recursive terms are the Lineage_Graph's own, served by the same
# per-direction indexes; what is added is the semi-join over unsuperseded
# Attribution_Versions and the limit, neither of which the store's unbounded
# closure carries because an erasure sweep must not have either.
SELECT_PERMITTED_ANCESTORS_STATEMENT: Final[str] = (
    "WITH RECURSIVE seed AS ("
    "SELECT unnest(%s::UUID[]) AS node"
    "), "
    "ancestors AS ("
    "SELECT e.parent_id AS node, e.parent_kind AS kind FROM lineage_edge AS e "
    "JOIN seed AS s ON e.child_id = s.node "
    "UNION "
    "SELECT e.parent_id, e.parent_kind FROM lineage_edge AS e "
    "JOIN ancestors AS a ON e.child_id = a.node"
    ") "
    "SELECT a.node, a.kind FROM ancestors AS a "
    "WHERE EXISTS ("
    "SELECT 1 FROM client_binding AS b "
    "WHERE b.artifact_id = a.node AND b.client_id = ANY (%s::UUID[]) "
    "AND b.superseded_by IS NULL"
    ") "
    "ORDER BY a.node LIMIT %s"
)

SELECT_PERMITTED_DESCENDANTS_STATEMENT: Final[str] = (
    "WITH RECURSIVE seed AS ("
    "SELECT unnest(%s::UUID[]) AS node"
    "), "
    "descendants AS ("
    "SELECT e.child_id AS node FROM lineage_edge AS e "
    "JOIN seed AS s ON e.parent_id = s.node "
    "UNION "
    "SELECT e.child_id FROM lineage_edge AS e "
    "JOIN descendants AS d ON e.parent_id = d.node"
    ") "
    "SELECT d.node FROM descendants AS d "
    "WHERE EXISTS ("
    "SELECT 1 FROM client_binding AS b "
    "WHERE b.artifact_id = d.node AND b.client_id = ANY (%s::UUID[]) "
    "AND b.superseded_by IS NULL"
    ") "
    "ORDER BY d.node LIMIT %s"
)


def _validate_statements() -> None:
    """Refuse at import a closure whose tenancy admission has drifted.

    The admission each closure carries must be the attribution layer's own term for
    a current claim rather than a second spelling of it, so what the tool server
    admits and what recall admits cannot come to mean different things. The check
    runs here rather than in a suite, because a drifted statement would otherwise
    stay runnable on a machine nobody had run the suite on.

    The predicate is checked into the statement rather than composed into it: a
    statement assembled around a name is a statement a value could one day be
    assembled into, and every statement of this module is a whole literal for that
    reason.
    """
    for name, text in (
        ("the ancestor closure", SELECT_PERMITTED_ANCESTORS_STATEMENT),
        ("the descendant closure", SELECT_PERMITTED_DESCENDANTS_STATEMENT),
    ):
        if CURRENT_VERSION_PREDICATE not in text:
            raise StoreError(f"{name} must admit rows through the current-attribution predicate")


_validate_statements()


class UnknownToolError(MoltError):
    """A requested name is not one the registry carries, so nothing was called."""


class ToolEffect(StrEnum):
    """What a tool may do to stored state, which has one value and no other.

    There is deliberately no mutating member. A registry entry declares its effect
    from this enumeration, so a mutation tool cannot be described here, let alone
    dispatched.
    """

    READ_ONLY = "read_only"


class ArgumentKind(StrEnum):
    """The value shapes a tool argument may have.

    Every shape is a scalar or a list of scalars. None of them is a client set,
    and none of them can carry one, which is the point.
    """

    TEXT = "text"
    IDENTIFIER = "identifier"
    IDENTIFIER_LIST = "identifier_list"
    COUNT = "count"


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """One argument a tool declares: its name, its shape, and whether it is owed."""

    name: str
    kind: ArgumentKind
    required: bool
    description: str


@dataclass(frozen=True, slots=True)
class ResultShape:
    """The row shape a tool returns, named by its fields in the order reported."""

    row: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What one invocation produced: its rows, and a note when the cluster was lost.

    The note exists because an unreachable cluster returns an empty result with an
    explanation rather than tearing down the session: a tool call is advisory, and
    a client that lost its transport has lost more than one answer.
    """

    rows: tuple[JsonObject, ...]
    note: str | None = None

    @property
    def count(self) -> int:
        """How many rows the invocation returned, which is what recording names."""
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class McpSettings:
    """The server's configured surface, resolved once at startup.

    The permitted set is held here as the configured slugs; the identifiers they
    resolve to are read from the cluster once, by `permitted_client_ids`. Both
    halves come from configuration and neither is reachable from a tool argument.
    """

    transport: str
    bind_host: str
    bind_port: int
    permitted_client_slugs: tuple[str, ...]
    max_results: int

    def __post_init__(self) -> None:
        if self.transport not in (STDIO_TRANSPORT, HTTP_TRANSPORT):
            raise ValueError("the configured transport must be stdio or http")
        if self.max_results < 1:
            raise ValueError("the maximum result count must admit at least one row")
        if not self.permitted_client_slugs:
            raise ValueError("the server must be configured with at least one permitted client")

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> McpSettings:
        """Resolve every value from the configuration surface and from nowhere else."""
        host, port = _split_bind(configuration.text(BIND_KEY))
        return cls(
            transport=configuration.text(TRANSPORT_KEY),
            bind_host=host,
            bind_port=port,
            permitted_client_slugs=configuration.text_list(PERMITTED_CLIENTS_KEY),
            max_results=configuration.integer(MAX_RESULTS_KEY),
        )


@dataclass(frozen=True, slots=True)
class ToolBackend:
    """Everything the four tools reach storage and the providers through.

    Assembled once by the server, so a tool receives its tenancy and its bound
    rather than deriving either from what it was asked.
    """

    store: MemoryStore
    engine: RecallEngine
    policy: ResiduePolicy
    permitted_clients: tuple[UUID, ...]
    max_results: int

    def bound_for(self, arguments: Mapping[str, JsonValue]) -> int:
        """The row bound one invocation runs under, never above the configured one.

        A caller asking for more than the maximum is answered at the maximum
        rather than refused, and a caller asking for a number that is not a usable
        count is answered at the maximum as well.
        """
        asked = arguments.get(_LIMIT_ARGUMENT)
        if isinstance(asked, bool) or not isinstance(asked, int) or asked < 1:
            return self.max_results
        return min(asked, self.max_results)


@dataclass(frozen=True, slots=True)
class Tool:
    """One registry entry: its name, its argument schema, its result, its effect."""

    name: str
    summary: str
    arguments: tuple[ArgumentSpec, ...]
    result: ResultShape
    effect: ToolEffect
    handler: Callable[[ToolBackend, Mapping[str, JsonValue]], ToolResult]

    def schema(self) -> JsonObject:
        """The argument schema as a client reads it, carrying no client-set field."""
        properties: dict[str, JsonValue] = {
            argument.name: {
                "kind": argument.kind.value,
                "required": argument.required,
                "description": argument.description,
            }
            for argument in self.arguments
        }
        return {
            "name": self.name,
            "summary": self.summary,
            "effect": self.effect.value,
            "arguments": properties,
            "required": [argument.name for argument in self.arguments if argument.required],
            "result": {"row": self.result.row, "fields": list(self.result.fields)},
        }


# The one optional argument every tool shares, kept as a constant so the bound is
# read from one key rather than from a name spelled per tool.
_LIMIT_ARGUMENT: Final[str] = "limit"
_QUERY_ARGUMENT: Final[str] = "query_text"
_ARTIFACT_IDS_ARGUMENT: Final[str] = "artifact_ids"
_RUN_ARGUMENT: Final[str] = "run_id"

_LIMIT_SPEC: Final[ArgumentSpec] = ArgumentSpec(
    name=_LIMIT_ARGUMENT,
    kind=ArgumentKind.COUNT,
    required=False,
    description="How many rows to return, held at or below the configured maximum.",
)

# The note an invocation carries back when the cluster did not answer.
CLUSTER_LOST_NOTE: Final[str] = (
    "the cluster did not answer, so this call returned no row and the session stays open"
)


# ---------------------------------------------------------------------------
# The four tools
# ---------------------------------------------------------------------------


def _recall(backend: ToolBackend, arguments: Mapping[str, JsonValue]) -> ToolResult:
    """Semantic recall over fleet memory, answered by the Recall_Engine.

    The engine is handed the configured permitted set and no Session identifier.
    Both halves matter: the set is the server's, and a Session named by a caller
    would have added its own Client to the permitted union, which is precisely the
    widening a tool argument must not be able to do.
    """
    query = arguments.get(_QUERY_ARGUMENT)
    if not isinstance(query, str) or not query.strip():
        return ToolResult(rows=())
    results = backend.engine.recall(
        query,
        backend.bound_for(arguments),
        permitted=backend.permitted_clients,
    )
    return ToolResult(rows=tuple(result.as_document() for result in results))


def _ancestors(backend: ToolBackend, arguments: Mapping[str, JsonValue]) -> ToolResult:
    """Every Artifact the named Artifacts were derived from that the caller may see."""
    return _closure(backend, arguments, SELECT_PERMITTED_ANCESTORS_STATEMENT, _ancestor_row)


def _descendants(backend: ToolBackend, arguments: Mapping[str, JsonValue]) -> ToolResult:
    """Every Artifact derived from the named Artifacts that the caller may see."""
    return _closure(backend, arguments, SELECT_PERMITTED_DESCENDANTS_STATEMENT, _descendant_row)


def _residue(backend: ToolBackend, arguments: Mapping[str, JsonValue]) -> ToolResult:
    """Residue candidates for one erasure run, through the Residue_Detector's read path.

    `residue_report` is the read-only exposure of the same walk the sweep uses, so
    a band reported here and a band recorded there cannot come to mean different
    things, and this call records nothing and adjudicates nothing.
    """
    run_id = _identifier(arguments.get(_RUN_ARGUMENT))
    if run_id is None:
        return ToolResult(rows=())
    bound = backend.bound_for(arguments)
    try:
        report = residue_report(
            backend.store,
            run_id,
            _bounded_policy(backend.policy, bound),
            permitted_clients=backend.permitted_clients,
        )
    except (StoreError, OSError):
        return ToolResult(rows=(), note=CLUSTER_LOST_NOTE)
    return ToolResult(rows=tuple(_finding_row(finding) for finding in report.findings))


def _bounded_policy(policy: ResiduePolicy, bound: int) -> ResiduePolicy:
    """The pass's policy with both statement bounds fitted under one row bound.

    The pass reports at most one candidate per query Artifact per neighbour, so
    the product of the two limits is what it can produce. Dividing the neighbour
    bound down by the query limit keeps that product inside the configured
    maximum with both halves still applied by the cluster, which is what makes the
    bound a limit rather than a trim.
    """
    queries = min(policy.query_limit, bound)
    return ResiduePolicy(
        auto_include_threshold=policy.auto_include_threshold,
        review_threshold=policy.review_threshold,
        query_limit=queries,
        top_k=max(1, bound // queries),
        excerpt_characters=policy.excerpt_characters,
    )


def _closure(
    backend: ToolBackend,
    arguments: Mapping[str, JsonValue],
    statement: str,
    row_of: Callable[[Sequence[object]], JsonObject],
) -> ToolResult:
    """Run one bounded, tenancy-filtered closure and render its rows."""
    seeds = _identifiers(arguments.get(_ARTIFACT_IDS_ARGUMENT))
    if not seeds:
        return ToolResult(rows=())
    parameters = (list(seeds), list(backend.permitted_clients), backend.bound_for(arguments))

    def body(opened: Cursor) -> tuple[JsonObject, ...]:
        opened.execute(statement, parameters)
        return tuple(row_of(row) for row in opened.fetchall())

    try:
        return ToolResult(rows=backend.store.read(body))
    except (StoreError, OSError):
        return ToolResult(rows=(), note=CLUSTER_LOST_NOTE)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


REGISTRY: Final[tuple[Tool, ...]] = (
    Tool(
        name=RECALL_TOOL,
        summary="Recall prior attempts and their outcomes similar to a described action.",
        arguments=(
            ArgumentSpec(
                name=_QUERY_ARGUMENT,
                kind=ArgumentKind.TEXT,
                required=True,
                description="A natural-language description of the intended action.",
            ),
            _LIMIT_SPEC,
        ),
        result=ResultShape(
            row="recalled",
            fields=(
                "artifact_id",
                "artifact_kind",
                "distance",
                "session_id",
                "machine_id",
                "occurred_at",
                "outcome",
                "kind",
                "excerpt",
                "confidence",
            ),
        ),
        effect=ToolEffect.READ_ONLY,
        handler=_recall,
    ),
    Tool(
        name=ANCESTORS_TOOL,
        summary="Retrieve the lineage ancestors of the named Artifacts.",
        arguments=(
            ArgumentSpec(
                name=_ARTIFACT_IDS_ARGUMENT,
                kind=ArgumentKind.IDENTIFIER_LIST,
                required=True,
                description="The Artifacts whose ancestors are asked for.",
            ),
            _LIMIT_SPEC,
        ),
        result=ResultShape(row="lineage_node", fields=("artifact_id", "artifact_kind")),
        effect=ToolEffect.READ_ONLY,
        handler=_ancestors,
    ),
    Tool(
        name=DESCENDANTS_TOOL,
        summary="Retrieve the lineage descendants of the named Artifacts.",
        arguments=(
            ArgumentSpec(
                name=_ARTIFACT_IDS_ARGUMENT,
                kind=ArgumentKind.IDENTIFIER_LIST,
                required=True,
                description="The Artifacts whose descendants are asked for.",
            ),
            _LIMIT_SPEC,
        ),
        result=ResultShape(row="lineage_node", fields=("artifact_id",)),
        effect=ToolEffect.READ_ONLY,
        handler=_descendants,
    ),
    Tool(
        name=RESIDUE_TOOL,
        summary="Search residue candidates for one erasure run, recording nothing.",
        arguments=(
            ArgumentSpec(
                name=_RUN_ARGUMENT,
                kind=ArgumentKind.IDENTIFIER,
                required=True,
                description="The erasure run whose candidate set seeds the search.",
            ),
            _LIMIT_SPEC,
        ),
        result=ResultShape(
            row="residue_candidate",
            fields=(
                "artifact_id",
                "artifact_kind",
                "query_artifact_id",
                "cosine_distance",
                "band",
                "included",
                "decision_reason",
            ),
        ),
        effect=ToolEffect.READ_ONLY,
        handler=_residue,
    ),
)

# The dispatchable names, as one tuple built from the registry. Dispatch consults
# this and the registry alone, so a name absent from the tuple is unreachable.
TOOL_NAMES: Final[tuple[str, ...]] = tuple(tool.name for tool in REGISTRY)

_BY_NAME: Final[Mapping[str, Tool]] = {tool.name: tool for tool in REGISTRY}


def tool_named(name: str) -> Tool | None:
    """The registry entry one name reaches, or None when it reaches none."""
    return _BY_NAME.get(name)


def dispatch(backend: ToolBackend, name: str, arguments: Mapping[str, JsonValue]) -> ToolResult:
    """Call the tool one name reaches, refusing every name the registry lacks.

    Arguments a schema does not declare are not read. That is how an extra key
    naming a client set is ignored: nothing looks for it.
    """
    tool = tool_named(name)
    if tool is None:
        raise UnknownToolError(f"no tool named {name!r} is exposed, so nothing was called")
    return tool.handler(backend, arguments)


# ---------------------------------------------------------------------------
# Startup reads and value decoding
# ---------------------------------------------------------------------------


def permitted_client_ids(store: MemoryStore, slugs: Sequence[str]) -> tuple[UUID, ...]:
    """Resolve the configured Client slugs to identifiers, once, at startup.

    A slug naming no Client resolves to nothing rather than to an error: an
    operator's list may name a tenant not yet placed, and a server that refused to
    start over that would be a server an absent tenant can take down.
    """
    if not slugs:
        return ()

    def body(opened: Cursor) -> tuple[UUID, ...]:
        opened.execute(PERMITTED_CLIENT_IDS_STATEMENT, (list(slugs),))
        return tuple(_as_uuid(row[0]) for row in opened.fetchall())

    return store.read(body)


def cluster_reachable(store: MemoryStore) -> bool:
    """Whether the cluster answers one trivial read, which the health route reports."""

    def body(opened: Cursor) -> bool:
        opened.execute(REACHABILITY_STATEMENT, ())
        return opened.fetchone() is not None

    try:
        return store.read(body)
    except (StoreError, OSError):
        return False


def _ancestor_row(row: Sequence[object]) -> JsonObject:
    """One ancestor as the result shape carries it."""
    return {
        "artifact_id": str(_as_uuid(row[0])),
        "artifact_kind": ArtifactKind(_as_str(row[1])).value,
    }


def _descendant_row(row: Sequence[object]) -> JsonObject:
    """One descendant as the result shape carries it."""
    return {"artifact_id": str(_as_uuid(row[0]))}


def _finding_row(finding: ResidueFinding) -> JsonObject:
    """One residue candidate as the result shape carries it."""
    return {
        "artifact_id": str(finding.artifact_id),
        "artifact_kind": ArtifactKind(finding.artifact_kind).value,
        "query_artifact_id": str(finding.query_artifact_id),
        "cosine_distance": finding.cosine_distance,
        "band": str(finding.band),
        "included": finding.included,
        "decision_reason": finding.decision_reason,
    }


def _identifier(value: JsonValue | None) -> UUID | None:
    """One identifier an argument named, or None when it named none well-formed."""
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _identifiers(value: JsonValue | None) -> tuple[UUID, ...]:
    """The identifiers a list argument named, with the malformed ones dropped.

    An identifier no Artifact holds is left in: the closure answers nothing for
    it, which is the right answer and one the cluster gives.
    """
    if isinstance(value, str):
        found = _identifier(value)
        return () if found is None else (found,)
    if not isinstance(value, list):
        return ()
    decoded = [_identifier(item) for item in value]
    return tuple(dict.fromkeys(item for item in decoded if item is not None))


def _split_bind(bind: str) -> tuple[str, int]:
    """The host and port a configured bind address names."""
    host, separator, port = bind.rpartition(":")
    if not separator or not port.isdigit():
        raise ValueError("the configured bind address must name a host and a port")
    return host, int(port)


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise StoreError(f"a selected column holds {type(value).__name__} where an identifier was read")


def _as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise StoreError(f"a selected column holds {type(value).__name__} where text was read")
