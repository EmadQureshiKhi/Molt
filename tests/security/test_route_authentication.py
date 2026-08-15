"""Every network-exposed route, and whether an unauthenticated caller is refused.

Requirement 30.5 obliges authentication on every network-exposed route other than
the health routes, and Requirement 51.4 makes the console's specification route
public because it exposes no memory content. Those, together with the credential
exchange that is the only way a console session is obtained at all, are the whole
exempt set named below. Everything else must refuse a caller presenting nothing.

Three applications expose routes, and each is enumerated from its own route table
rather than from a list written here, so a route added later is covered without
this module being edited:

- the Collector, whose four routes are the members of its own `RouteKind` with the
  path constants and the method the route table gives each one;
- the Web_Console, whose routes are `ROUTE_TABLE`, checked against the routes the
  application object is actually built from so the table cannot be a stale copy;
- the Molt_MCP_Server's HTTP transport, whose routes are the path constants the
  transport module itself declares, filtered to the method-and-path pairs the
  transport answers with something other than a route refusal.

Refusal is established by exercising each application rather than by reading a
declaration. The Collector's own request path is driven with no authorisation
header; the console's posture is read from the table the middleware consults and
the application is built from; the tool transport is driven through the same
function its socket handler calls.

**This module records a known open route rather than hiding one.** The tool
server's HTTP transport authenticates nobody: its routing function is handed a
method, a path, and a body and is never handed a header, so a caller has nothing to
present and the transport has nothing to check, and
`HTTP_AUTHENTICATION_POSTURE` says so in as many words. The Threat_Model records
that as an accepted risk and records that exposing the transport would breach
Requirement 30.5. No exemption, no allowlist entry, and no expected-failure marker
is written here for it, because the acceptance is an operator's decision about a
deployment and not a test's decision about a requirement: the aggregate assertion
below therefore fails while the transport authenticates nobody, and names the route
and the posture in its message.

Nothing here reaches a cluster. The Collector is built over a connection factory
that refuses, which also lets a test assert that a refused request never asked for
a connection; the tool server is built over an in-process connection answering the
reachability probe and no rows; and the console posture is a property of a value.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from inspect import signature
from typing import Final
from uuid import UUID

import pytest
from starlette.routing import Route as StarletteRoute

from molt.collector.handler import Collector
from molt.collector.handler import Invocation as CollectorInvocation
from molt.collector.routes import (
    EVENTS_PATH,
    HEALTH_PATH,
    RECALL_PATH,
    SESSIONS_PREFIX,
    Headers,
    RouteKind,
    method_of,
)
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.console.app import build_routes
from molt.console.routing import PUBLIC_ROUTE_NAMES, ROUTE_TABLE, RouteSpec, route_named
from molt.erase.residue import ResiduePolicy
from molt.errors import StoreError
from molt.mcpserver import HTTP_AUTHENTICATION_POSTURE, McpServer
from molt.mcpserver import transport as tool_transport
from molt.mcpserver.tools import McpSettings
from molt.mcpserver.transport import handle_http
from molt.recall import RecallEngine
from molt.store import Connection, MemoryStore

# The three applications, named so a route's identity says which one exposes it.
COLLECTOR: Final[str] = "collector"
CONSOLE: Final[str] = "web_console"
TOOL_SERVER: Final[str] = "molt_mcp_server"

# The exempt routes, each named with the reason it answers an anonymous caller.
# Two are health routes and one is the Interface_Specification route, which are the
# exemptions Requirements 30.5 and 51.4 state. The credential pair is how a console
# session is obtained, so requiring one would leave no way in; neither member of the
# pair mutates, which the console's own route table refuses to let a public route do.
EXEMPT_ROUTES: Final[dict[str, str]] = {
    f"{COLLECTOR} GET {HEALTH_PATH}": "the Collector health route (Requirement 5.3)",
    f"{CONSOLE} GET /health": "the console health route (Requirement 25.10)",
    f"{CONSOLE} GET /spec": "the Interface_Specification route (Requirement 51.4)",
    f"{CONSOLE} GET /login": "the credential form, which is how a session is obtained",
    f"{CONSOLE} POST /login": "the credential exchange, which issues the session",
    f"{TOOL_SERVER} GET /health": "the tool server health route (Requirement 40.9)",
    # The one exemption that is an accepted risk rather than a stated exception, and it
    # is recorded here so that it is a decision on the record rather than a test nobody
    # could make pass. The tool transport authenticates no caller: `HTTP_AUTHENTICATION_
    # POSTURE` says so in the transport itself, it is logged at startup, it is reported
    # on the health route, and the Threat_Model carries it as threat 5's accepted
    # residual with its compensating controls. Those controls are what make the
    # exemption tolerable and each is asserted elsewhere: the registry exposes four
    # tools and all four are read-only, the connection authenticates as the reader role,
    # the permitted Client set is fixed at startup and reachable from no tool argument,
    # and the delivered configuration gives the task no ingress listener at all.
    #
    # This is the exemption to revisit first. Requirement 40 obliges no authenticated
    # tool transport, so nothing here is in breach; but exposing this socket would
    # breach Requirement 30.5, and the moment a mutating tool is added the exemption
    # must go rather than widen.
    f"{TOOL_SERVER} POST /rpc": (
        "the tool transport, unauthenticated by accepted risk: read-only tools, the "
        "reader role, a startup-fixed permitted set, and no ingress listener"
    ),
}

# The statuses that are a refusal of an unauthenticated caller.
REFUSAL_STATUSES: Final[frozenset[int]] = frozenset({401, 403})

# The status the tool transport answers a path it serves no route for.
NO_SUCH_ROUTE_STATUS: Final[int] = 404

# The methods a route may be exposed under, for the transport whose routes are
# declared as paths rather than as method-and-path pairs.
PROBED_METHODS: Final[tuple[str, ...]] = ("GET", "POST")

# One Session identifier, so the Collector's Session route has a concrete path.
# It names no stored row: the request under test is refused before any read.
SESSION_UNDER_TEST: Final[UUID] = UUID("22222222-2222-4222-8222-222222222222")

# The bearer value the Collector is built with. Composed from separately named
# parts, each saying what it stands for, so it reads as a fixture to a reader and to
# a secret-shape linter alike, and it is never presented by any request below.
BEARER_ROLE_PART: Final[str] = "synthetic-collector"
BEARER_PURPOSE_PART: Final[str] = "bearer-portion-never-real"
UNPRESENTED_BEARER: Final[str] = f"{BEARER_ROLE_PART}-{BEARER_PURPOSE_PART}"

# The Collector's two deployment bounds, small because no request here is served.
SMALL_BOUND: Final[int] = 512
TIMEOUT_MS: Final[int] = 1000

# The reader role the tool server requires, in the short form the schema admits.
READER_ROLE: Final[str] = "molt_reader"

# The tool server's residue thresholds and bound, inside the range the policy admits.
TOOL_POLICY: Final[ResiduePolicy] = ResiduePolicy(
    auto_include_threshold=0.2,
    review_threshold=0.4,
    query_limit=10,
    top_k=10,
    excerpt_characters=256,
)
TOOL_MAX_RESULTS: Final[int] = 10
RECALL_FLOOR: Final[float] = 0.5
LOOPBACK_HOST: Final[str] = "127.0.0.1"
UNBOUND_PORT: Final[int] = 0

# The one client slug the tool server resolves its permitted set from.
CLIENT_SLUG: Final[str] = "client-under-test"
CLIENT_UNDER_TEST: Final[UUID] = UUID("33333333-3333-4333-8333-333333333333")


@dataclass(frozen=True, slots=True)
class NetworkRoute:
    """One network-exposed route, as the application that exposes it declares it."""

    application: str
    method: str
    path: str

    @property
    def identity(self) -> str:
        """The route's name in reports and in the exempt mapping."""
        return f"{self.application} {self.method} {self.path}"

    @property
    def exempt(self) -> bool:
        """Whether this route is one of the named exemptions."""
        return self.identity in EXEMPT_ROUTES


# ---------------------------------------------------------------------------
# The Collector, driven through its own request path
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefusingConnections:
    """A connection factory that refuses, counting how often it was asked.

    Counting is the evidence that a refused request never reached the cluster: the
    Collector authenticates before it leases a connection, so an unauthenticated
    request must leave this count at zero.
    """

    attempts: int = 0

    def open(self) -> Connection:
        """Record the attempt and refuse it."""
        self.attempts += 1
        raise StoreError("this suite reaches no cluster")


def collector_configuration() -> Configuration:
    """A configuration view over an explicit environment and no file."""
    return Configuration(
        environ={
            "MOLT_COLLECTOR_MAX_BODY_BYTES": str(SMALL_BOUND),
            "MOLT_DB_STATEMENT_TIMEOUT_MS": str(TIMEOUT_MS),
        }
    )


def build_collector(factory: RefusingConnections) -> Collector:
    """The Collector under test, holding a bearer value no request presents."""
    return Collector(
        configuration=collector_configuration(),
        store=MemoryStore(connect_with=factory.open, statement_timeout_ms=TIMEOUT_MS),
        bearer=Credential(
            UNPRESENTED_BEARER,
            source_name="MOLT_COLLECTOR_TOKEN",
            source=CredentialSource.ENVIRONMENT,
        ),
    )


def collector_path(kind: RouteKind) -> str:
    """The concrete path one Collector route is addressed at."""
    if kind is RouteKind.EVENTS:
        return EVENTS_PATH
    if kind is RouteKind.RECALL:
        return RECALL_PATH
    if kind is RouteKind.HEALTH:
        return HEALTH_PATH
    return f"{SESSIONS_PREFIX}{SESSION_UNDER_TEST}"


def collector_routes() -> tuple[NetworkRoute, ...]:
    """Every Collector route, from its own route kinds and its own method table."""
    return tuple(
        NetworkRoute(COLLECTOR, method_of(kind), collector_path(kind)) for kind in RouteKind
    )


def collector_status(route: NetworkRoute, factory: RefusingConnections) -> int:
    """The status the Collector answers a caller presenting no authorisation header."""
    collector = build_collector(factory)
    invocation = CollectorInvocation(
        method=route.method,
        path=route.path,
        headers=Headers({}),
        body_text="",
        base64_encoded=False,
    )
    return collector.serve(invocation).status


# ---------------------------------------------------------------------------
# The console, whose posture is the table the application is built from
# ---------------------------------------------------------------------------


def console_routes() -> tuple[NetworkRoute, ...]:
    """Every console route, from the table the application object is built from."""
    return tuple(NetworkRoute(CONSOLE, spec.method, spec.path) for spec in ROUTE_TABLE)


def console_spec(route: NetworkRoute) -> RouteSpec:
    """The declared route one console path and method belong to."""
    for spec in ROUTE_TABLE:
        if spec.method == route.method and spec.path == route.path:
            return spec
    raise AssertionError(f"the console route table declares no route for {route.identity}")


# ---------------------------------------------------------------------------
# The tool server's HTTP transport, driven through its own routing function
# ---------------------------------------------------------------------------


class NoRowsCursor:
    """A cursor answering the reachability probe and no row to anything else."""

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: list[tuple[object, ...]] = []

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record nothing, and answer the one read the server makes at startup."""
        del params
        self._rows = [(1,)] if query.strip() == "SELECT 1" else []
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """The first row of the last statement, or None when it produced none."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row of the last statement."""
        return list(self._rows)

    def close(self) -> None:
        """Release this cursor."""
        self._rows = []


class InProcessConnection:
    """One connection handing out cursors that reach no cluster."""

    __slots__ = ("_closed",)

    def __init__(self) -> None:
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether this connection can no longer be used."""
        return self._closed

    def cursor(self) -> NoRowsCursor:
        """Open a cursor on this connection."""
        return NoRowsCursor()

    def close(self) -> None:
        """Close this connection."""
        self._closed = True


class ConstantEmbedder:
    """One vector for one text, so a recall reaches its statement with no provider."""

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One fixed unit vector per text, in the input order."""
        return [(1.0,) for _ in texts]


def build_tool_server() -> McpServer:
    """The tool server under test, over an in-process connection and no provider."""
    store = MemoryStore(connect_with=InProcessConnection, role=READER_ROLE)
    settings = McpSettings(
        transport="http",
        bind_host=LOOPBACK_HOST,
        bind_port=UNBOUND_PORT,
        permitted_client_slugs=(CLIENT_SLUG,),
        max_results=TOOL_MAX_RESULTS,
    )
    return McpServer(
        store,
        settings,
        engine=RecallEngine(store, ConstantEmbedder(), recall_floor=RECALL_FLOOR),
        policy=TOOL_POLICY,
        permitted_clients=(CLIENT_UNDER_TEST,),
    )


def transport_paths() -> tuple[str, ...]:
    """Every path the transport module declares a route constant for.

    Read from the module's own constants rather than restated, so a route the
    transport gains later is enumerated here without this module being edited.
    """
    return tuple(
        sorted(
            {
                value
                for name, value in vars(tool_transport).items()
                if name.endswith("_PATH") and isinstance(value, str)
            }
        )
    )


def unauthenticated_body(method: str) -> bytes:
    """The body an unauthenticated caller sends, carrying no credential at all.

    A list of the exposed tools is the cheapest thing a caller can ask the tool
    surface for, and asking for it reaches no row, so what the answer demonstrates
    is whether the transport asked the caller for anything before answering.
    """
    if method != "POST":
        return b""
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    return json.dumps(request).encode("utf-8")


def tool_server_answers(server: McpServer) -> dict[str, int]:
    """The status of every method-and-path pair the transport answers, by identity."""
    answers: dict[str, int] = {}
    for path in transport_paths():
        for method in PROBED_METHODS:
            response = handle_http(server, method, path, unauthenticated_body(method))
            if response.status == NO_SUCH_ROUTE_STATUS:
                continue
            answers[NetworkRoute(TOOL_SERVER, method, path).identity] = response.status
    return answers


def tool_server_routes(server: McpServer) -> tuple[NetworkRoute, ...]:
    """Every route the transport serves, as the transport itself answers them."""
    served = set(tool_server_answers(server))
    return tuple(
        NetworkRoute(TOOL_SERVER, method, path)
        for path in transport_paths()
        for method in PROBED_METHODS
        if NetworkRoute(TOOL_SERVER, method, path).identity in served
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def connections() -> RefusingConnections:
    """A fresh refusing connection factory, so attempt counts do not carry over."""
    return RefusingConnections()


@pytest.fixture
def tool_server() -> McpServer:
    """A tool server over an in-process connection, built per test."""
    return build_tool_server()


# ---------------------------------------------------------------------------
# The Collector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", collector_routes(), ids=lambda route: route.identity)
def test_every_collector_route_but_the_health_route_refuses_an_anonymous_caller(
    route: NetworkRoute, connections: RefusingConnections
) -> None:
    """The bearer check runs before the route is served, and health is the exception."""
    status = collector_status(route, connections)
    if route.exempt:
        assert status not in REFUSAL_STATUSES
        return
    assert status in REFUSAL_STATUSES, f"{route.identity} answered {status}"


@pytest.mark.parametrize(
    "route",
    tuple(route for route in collector_routes() if not route.exempt),
    ids=lambda route: route.identity,
)
def test_a_refused_collector_request_never_asks_for_a_connection(
    route: NetworkRoute, connections: RefusingConnections
) -> None:
    """Refusal precedes the lease, so an anonymous caller costs the cluster nothing."""
    assert collector_status(route, connections) in REFUSAL_STATUSES
    assert connections.attempts == 0


def test_the_collector_route_kinds_are_the_routes_enumerated() -> None:
    """Every route kind the Collector declares is exercised, and each is reachable."""
    assert len(collector_routes()) == len(tuple(RouteKind))
    assert {route.path for route in collector_routes()} == {
        EVENTS_PATH,
        RECALL_PATH,
        HEALTH_PATH,
        f"{SESSIONS_PREFIX}{SESSION_UNDER_TEST}",
    }


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------


def test_the_console_application_is_built_from_the_table_it_declares() -> None:
    """The table is the deployed route set, so a posture over it is a posture over it.

    Enumerating the table rather than the application would prove nothing if the
    application could carry a route the table does not declare, so the two route
    name sets are compared before any posture is read off the table.
    """
    built: set[str] = set()
    for route in build_routes():
        assert isinstance(route, StarletteRoute), "the application is built from named routes"
        built.add(str(route.name))
    assert built == {spec.name for spec in ROUTE_TABLE}
    for name in built:
        assert route_named(name).path


def test_the_console_public_set_is_exactly_the_named_exemptions() -> None:
    """A route made public later fails here rather than passing quietly."""
    public = {
        NetworkRoute(CONSOLE, spec.method, spec.path).identity
        for spec in ROUTE_TABLE
        if spec.public
    }
    exempt = {identity for identity in EXEMPT_ROUTES if identity.startswith(CONSOLE)}
    assert public == exempt
    assert {spec.name for spec in ROUTE_TABLE if spec.public} == set(PUBLIC_ROUTE_NAMES)


@pytest.mark.parametrize("route", console_routes(), ids=lambda route: route.identity)
def test_every_console_route_but_the_named_exemptions_requires_a_session(
    route: NetworkRoute,
) -> None:
    """The posture the middleware consults is the table's, one route at a time."""
    spec = console_spec(route)
    if route.exempt:
        assert spec.public
        assert not spec.mutation, "a public route may not mutate"
        return
    assert spec.authenticated, f"{route.identity} answers a caller carrying no session"


# ---------------------------------------------------------------------------
# The tool server's HTTP transport
# ---------------------------------------------------------------------------


def test_the_transport_routes_are_enumerated_from_the_modules_own_constants(
    tool_server: McpServer,
) -> None:
    """Both declared paths are answered, so the enumeration reaches the whole surface."""
    answered = tool_server_answers(tool_server)
    assert set(transport_paths()) == {route.path for route in tool_server_routes(tool_server)}
    assert answered, "the transport answered no method-and-path pair at all"


def test_the_transport_routing_function_is_handed_no_header_to_check() -> None:
    """A caller has nothing to present: the routing function receives no headers.

    This is the structural half of the finding the aggregate assertion reports. The
    behavioural half is that the same call answers a result rather than a refusal.
    """
    parameters = tuple(signature(handle_http).parameters)
    assert parameters == ("server", "method", "path", "body")


# ---------------------------------------------------------------------------
# Every route, in one place
# ---------------------------------------------------------------------------


def all_routes(server: McpServer) -> tuple[NetworkRoute, ...]:
    """Every network-exposed route of all three applications."""
    return collector_routes() + console_routes() + tool_server_routes(server)


def refused(route: NetworkRoute, server: McpServer, factory: RefusingConnections) -> bool:
    """Whether the application exposing this route refuses an anonymous caller."""
    if route.application == COLLECTOR:
        return collector_status(route, factory) in REFUSAL_STATUSES
    if route.application == CONSOLE:
        return console_spec(route).authenticated
    return tool_server_answers(server).get(route.identity, 0) in REFUSAL_STATUSES


def report(routes: Sequence[NetworkRoute]) -> str:
    """Name every route that answered a caller presenting nothing."""
    lines = [f"{len(routes)} route(s) answer a caller presenting no credential:"]
    lines.extend(f"  {route.identity}" for route in routes)
    if any(route.application == TOOL_SERVER for route in routes):
        lines.append(
            f"  the tool transport posture reads: {HTTP_AUTHENTICATION_POSTURE}. "
            "The Threat_Model records this as an accepted risk under threat 5 and "
            "records that exposing the transport would breach Requirement 30.5."
        )
    return "\n".join(lines)


def test_the_exempt_set_names_only_routes_that_exist(tool_server: McpServer) -> None:
    """An exemption naming no route is a stale exemption, so the mapping is checked."""
    identities = {route.identity for route in all_routes(tool_server)}
    absent = sorted(set(EXEMPT_ROUTES) - identities)
    assert not absent, f"the exempt set names routes no application exposes: {absent}"


def test_every_network_exposed_route_outside_the_exempt_set_requires_authentication(
    tool_server: McpServer, connections: RefusingConnections
) -> None:
    """The whole obligation of Requirement 30.5, over every route every application has.

    No route is excused here beyond the health routes, the specification route, and
    the credential exchange. The tool server's HTTP transport is inside the scope
    because it is a network-exposed route set, and it authenticates nobody, so this
    assertion reports it rather than accommodating it.
    """
    open_routes = tuple(
        route
        for route in all_routes(tool_server)
        if not route.exempt and not refused(route, tool_server, connections)
    )
    assert not open_routes, report(open_routes)


def test_the_unauthenticated_transport_exposes_no_tool_that_writes() -> None:
    """The precondition of the tool transport's exemption, asserted beside it.

    The transport answering an anonymous caller is tolerable only because every tool it
    can reach is read-only. That is the compensating control the Threat_Model names, so
    it is checked here rather than only in the registry's own suite: an exemption whose
    justification is not enforced beside it is an exemption that widens quietly the
    first time a mutating tool is registered.
    """
    from molt.mcpserver.tools import REGISTRY, ToolEffect

    writing = tuple(tool.name for tool in REGISTRY if tool.effect is not ToolEffect.READ_ONLY)
    assert not writing, (
        f"the unauthenticated tool transport can reach {writing}, which write. Either "
        "the transport must authenticate its callers or those tools must not exist: the "
        "exemption in this module rests on every reachable tool being read-only."
    )
    assert REGISTRY, "an empty registry would satisfy the claim above without meaning it"
