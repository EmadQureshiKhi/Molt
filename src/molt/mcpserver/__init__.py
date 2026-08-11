"""The read-only tool server that exposes memory to any compatible client agent.

The server is one object holding what resolves once per process: the configured
transport and bound, the permitted Client set, the reader-role store, and the
seams recording reaches the Collector through. A call then names a tool and its
arguments and supplies nothing else.

**Read-only is a privilege fact rather than a promise.** The store is built from
the configuration surface, whose role key names the reader role, and construction
refuses a store authenticated as anything else. So a statement that tried to write
would be refused by the cluster, and the registry carries no entry that could try:
the two guarantees are independent, and both hold.

**Tenancy is resolved at startup and never from an argument.** The configured
slugs are read from `[mcp]` and turned into identifiers by one query at
construction. No tool schema declares a client-set field, so a caller has nothing
to widen with, and an extra key naming one is a key nothing reads.

**Every invocation is recorded, and recording holds no write privilege.** The
Event naming the tool, its redacted arguments, and its returned row count is
handed to the Collector through the capture ingress rather than appended here, so
the record travels the signed path and obeys the capture redaction. Recording
cannot fail an invocation: a tool call that failed for want of its own bookkeeping
would be worse than one whose bookkeeping is short.

**Argument values are digested rather than carried.** A tool argument may hold a
query text, and a query text is memory content. The recorded payload names the
argument keys and a digest of each value, so the Event states what was asked
without restating it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol
from uuid import UUID, uuid4

from molt.attest.verifier import READER_ROLE_NAMES
from molt.config.resolve import Configuration, load_configuration
from molt.erase.residue import ResiduePolicy
from molt.errors import MoltError
from molt.lifecycle import current_termination
from molt.mcpserver.tools import (
    COMPONENT,
    DEFAULT_MAX_RESULTS,
    REGISTRY,
    TOOL_NAMES,
    McpSettings,
    Tool,
    ToolBackend,
    ToolEffect,
    ToolResult,
    UnknownToolError,
    cluster_reachable,
    dispatch,
    permitted_client_ids,
    tool_named,
)
from molt.models.event import Event, EventCategory, JsonObject, JsonValue
from molt.recall import QueryEmbedder, RecallEngine
from molt.store import MemoryStore
from molt.telemetry import Severity, log, metric

__all__ = [
    "AGENT_CLI",
    "COMPONENT",
    "DEFAULT_MACHINE_ID",
    "DEFAULT_MAX_RESULTS",
    "HTTP_AUTHENTICATION_POSTURE",
    "REGISTRY",
    "TOOL_INVOCATION_METRIC",
    "TOOL_NAMES",
    "EventSink",
    "HealthReport",
    "McpServer",
    "McpSettings",
    "ReaderRoleRequiredError",
    "Tool",
    "ToolBackend",
    "ToolEffect",
    "ToolResult",
    "UnknownToolError",
    "dispatch",
    "tool_named",
]

# The measurement every invocation emits, with the tool as its one dimension.
TOOL_INVOCATION_METRIC: Final[str] = "mcp.tool_invocations"

# How the recorded Event names this component as its producer, and the machine
# identifier used when the surface names none. A host name is not derived: this
# process may run as one task of many on shared infrastructure, and a stable
# component name is what a reader of the Ledger can act on.
AGENT_CLI: Final[str] = "molt-mcp"
DEFAULT_MACHINE_ID: Final[str] = "mcp-server"

# The configuration key the machine identifier is read from.
MACHINE_ID_KEY: Final[str] = "MOLT_MACHINE_ID"

# What the HTTP transport requires of a caller, stated rather than assumed.
#
# It requires nothing. The configuration surface declares no credential for this
# transport, and this module invents none, so the only control on the HTTP
# transport is the network: the service template gives its task no ingress
# listener, and the process transport is what a local client uses. Exposing this
# transport to a reachable network would expose fleet memory to whoever reaches
# it.
HTTP_AUTHENTICATION_POSTURE: Final[str] = "unauthenticated; network isolation is the only control"

# The payload keys the recording Event carries.
_PAYLOAD_TOOL: Final[str] = "tool"
_PAYLOAD_ARGUMENTS: Final[str] = "arguments"
_PAYLOAD_RESULTS: Final[str] = "result_count"
_PAYLOAD_NOTE: Final[str] = "note"

# How an argument value is named without being carried.
_DIGEST_PREFIX: Final[str] = "sha256:"
_DIGEST_CHARACTERS: Final[int] = 32


class ReaderRoleRequiredError(MoltError):
    """The configured database role is not the read-only one, so no server was built."""


class EventSink(Protocol):
    """Where a recording Event goes, which is the Collector and not the cluster.

    Narrow on purpose and reached as a seam: this server holds no write privilege,
    so the Event is placed with the Collector over the signed ingress path, and
    stating that as a protocol keeps this module drivable by a stub and keeps the
    dependency pointing one way.
    """

    def emit(self, events: Sequence[Event]) -> None:
        """Accept the batch, in the order it was produced."""
        ...


@dataclass(frozen=True, slots=True)
class HealthReport:
    """What the health route reports, which is status and no memory content.

    Every field is a count, a name, or a flag. No Artifact, no excerpt, and no
    argument value appears, because a health route is the one route on this server
    that answers without a permitted set having been consulted.
    """

    status: str
    database_reachable: bool
    tools: tuple[str, ...]
    permitted_client_count: int
    transport: str
    max_results: int
    authentication: str

    def as_document(self) -> JsonObject:
        """The report as the route renders it."""
        return {
            "status": self.status,
            "database_reachable": self.database_reachable,
            "tools": list(self.tools),
            "permitted_client_count": self.permitted_client_count,
            "transport": self.transport,
            "max_results": self.max_results,
            "authentication": self.authentication,
        }


class _CollectorSink:
    """The default sink, which places the batch with the Collector.

    The transmitter is imported inside the call rather than at module scope
    because it resolves configuration and opens a connection of its own, and a
    server built for a test that stubs the sink should pay neither cost.
    """

    def emit(self, events: Sequence[Event]) -> None:
        """Place the batch with the Collector over the signed ingress path."""
        from molt.capture.hook import emit as place

        place(list(events))


class McpServer:
    """The tool server: one backend, one recording seam, and the health reading."""

    __slots__ = (
        "_backend",
        "_clock",
        "_machine_id",
        "_session_id",
        "_settings",
        "_sink",
        "_store",
    )

    def __init__(
        self,
        store: MemoryStore,
        settings: McpSettings,
        *,
        engine: RecallEngine,
        policy: ResiduePolicy,
        permitted_clients: Sequence[UUID],
        sink: EventSink | None = None,
        machine_id: str = DEFAULT_MACHINE_ID,
        session_id: UUID | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._sink = _CollectorSink() if sink is None else sink
        self._machine_id = machine_id or DEFAULT_MACHINE_ID
        self._session_id = uuid4() if session_id is None else session_id
        self._clock = _now if clock is None else clock
        self._backend = ToolBackend(
            store=store,
            engine=engine,
            policy=policy,
            permitted_clients=tuple(dict.fromkeys(permitted_clients)),
            max_results=settings.max_results,
        )

    @classmethod
    def from_configuration(
        cls,
        store: MemoryStore,
        embedder: QueryEmbedder,
        configuration: Configuration | None = None,
        *,
        sink: EventSink | None = None,
        permitted_clients: Sequence[UUID] | None = None,
    ) -> McpServer:
        """Build the server, resolving the permitted set and the bound at startup.

        The store's role is checked first. A store authenticated as anything but
        the reader role is refused here rather than trusted to only read, because
        the read-only guarantee this server makes is the privilege and not the
        registry alone.
        """
        resolved = load_configuration() if configuration is None else configuration
        if store.role not in READER_ROLE_NAMES:
            raise ReaderRoleRequiredError(
                "the tool server connects with the read-only database role, and the "
                f"configured role is {store.role!r}, so no server was built"
            )
        settings = McpSettings.from_configuration(resolved)
        clients = (
            permitted_client_ids(store, settings.permitted_client_slugs)
            if permitted_clients is None
            else tuple(permitted_clients)
        )
        log(
            Severity.INFO,
            COMPONENT,
            "resolved the permitted client set from configuration",
            configured_slugs=len(settings.permitted_client_slugs),
            resolved_clients=len(clients),
            transport=settings.transport,
            max_results=settings.max_results,
        )
        return cls(
            store,
            settings,
            engine=RecallEngine.from_configuration(store, embedder, resolved),
            policy=ResiduePolicy.from_configuration(resolved),
            permitted_clients=clients,
            sink=sink,
            machine_id=resolved.text(MACHINE_ID_KEY),
        )

    # -- what a client sees ----------------------------------------------

    @property
    def settings(self) -> McpSettings:
        """The configured surface this server resolved at startup."""
        return self._settings

    @property
    def permitted_clients(self) -> tuple[UUID, ...]:
        """The Clients this server may answer for, resolved from configuration."""
        return self._backend.permitted_clients

    @property
    def session_id(self) -> UUID:
        """The Session the recording Events of this process belong to."""
        return self._session_id

    def tools(self) -> tuple[JsonObject, ...]:
        """Every exposed tool's schema, in registry order."""
        return tuple(tool.schema() for tool in REGISTRY)

    def invoke(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolResult:
        """Call one tool, record the invocation, and measure it.

        A name the registry lacks raises before anything is recorded: nothing was
        invoked, so there is no invocation to record.
        """
        # An invocation admitted while draining is allowed to finish. Declining it
        # is the transport's decision, made from the health reading, because a tool
        # call is a read and a client agent proceeding uninformed is worse than a
        # slightly late answer.
        with current_termination().in_flight_work():
            result = dispatch(self._backend, name, arguments)
        metric(TOOL_INVOCATION_METRIC, tool=name)
        self._record(name, arguments, result)
        return result

    def health(self) -> HealthReport:
        """The health reading, naming status and no memory content.

        A draining instance reports the degraded status even while the cluster is
        reachable, so a load balancer takes it out of rotation before the transport
        stops answering rather than after.
        """
        reachable = cluster_reachable(self._store)
        draining = current_termination().stopping
        return HealthReport(
            status="ok" if reachable and not draining else "degraded",
            database_reachable=reachable,
            tools=TOOL_NAMES,
            permitted_client_count=len(self.permitted_clients),
            transport=self._settings.transport,
            max_results=self._settings.max_results,
            authentication=HTTP_AUTHENTICATION_POSTURE,
        )

    # -- recording -------------------------------------------------------

    def _record(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        result: ToolResult,
    ) -> None:
        """Hand the Collector one Event naming the tool, the arguments, and the count.

        Nothing here can fail the invocation. A sink that would not accept the
        batch is measured and logged, and the answer the caller already holds
        stands.
        """
        clients = self.permitted_clients
        if not clients:
            return
        payload: JsonObject = {
            _PAYLOAD_TOOL: name,
            _PAYLOAD_ARGUMENTS: _digested(arguments),
            _PAYLOAD_RESULTS: result.count,
        }
        if result.note is not None:
            payload[_PAYLOAD_NOTE] = result.note
        record = Event(
            id=uuid4(),
            session_id=self._session_id,
            client_id=clients[0],
            category=EventCategory.TOOL_CALL,
            occurred_at=self._clock(),
            agent_cli=AGENT_CLI,
            machine_id=self._machine_id,
            parent_event_id=None,
            payload=payload,
            redacted=True,
            text_body=None,
        )
        try:
            self._sink.emit((record,))
        except Exception as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "the tool invocation went unrecorded",
                tool=name,
                error_type=type(error).__name__,
            )


def _digested(arguments: Mapping[str, JsonValue]) -> JsonObject:
    """The arguments as the Event carries them: keys named, values digested.

    Every value becomes a truncated digest of its canonical rendering, including
    the values of keys no schema declares, so an extra key attempting to name a
    client set is recorded as having been present without its content being
    restated.
    """
    return {key: _digest_of(value) for key, value in sorted(arguments.items())}


def _digest_of(value: JsonValue) -> str:
    """One argument value's digest, which is what stands in for the value."""
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return f"{_DIGEST_PREFIX}{digest[:_DIGEST_CHARACTERS]}"


def _now() -> datetime:
    """The instant a recording Event carries, with an offset so it has one reading."""
    return datetime.now(UTC)
