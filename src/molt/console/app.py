"""The console application: one object, built from the route table.

The same object is served by the local development server behind `molt serve` and
by the deployed function through the Lambda adapter, and nothing in a handler knows
which. That is the point of building it here and adapting it there.

**Every route is constructed from `ROUTE_TABLE`.** There is no second place a route
can appear, so the table's stated posture is the deployed posture and a property
test over the table is a property test over the application. A route the table
declares whose handler no view module has claimed answers `501` *after* the
authentication check, so an unfinished view is never an open one.

**Authentication is middleware, not a decorator.** A per-handler decorator is
opt-in and therefore forgettable; the middleware here refuses first and dispatches
second, so a handler that forgot to ask is unreachable without a session. The
middleware consults the table by route name: a request whose path matches no route
is a 404 that discloses nothing else, a request to a non-public route without a
valid session is a 401, and a mutation route without the session's own CSRF token
is a 403.

**Two routes are public and neither reads a memory row.** `GET /health` reports
liveness, cluster reachability, the capability record summary, the configured
database role, and the mode flag; the reachability probe is a read keyed on the nil
identifier, which can match no row whatever the cluster holds, so the report is
proof that the cluster answered and carries nothing it holds. `GET /spec` serves the
tracked Interface_Specification document verbatim from the documentation directory
and opens no transaction at all.

**Error handling states the status and nothing more.** A refusal carries a fixed
document, an unexpected failure carries the status and a correlated log record, and
neither carries a message the cluster composed or a value a row holds.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Final, cast
from urllib.parse import parse_qsl
from uuid import UUID

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Match, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from molt.console import auth
from molt.console.demo import DemonstrationMiddleware
from molt.console.deps import COMPONENT, STATIC_MOUNT, Console
from molt.console.routing import (
    HANDLERS,
    ROUTE_TABLE,
    Endpoint,
    RouteSpec,
    route_named,
)
from molt.store.capability import PROBED_CAPABILITIES, CapabilityRecord
from molt.store.sessions import session_of_client
from molt.telemetry import Severity, log

__all__ = [
    "CONSOLE_STATE_KEY",
    "LOGIN_PATH",
    "PROBE_IDENTIFIER",
    "SESSION_STATE_KEY",
    "SPECIFICATION_MEDIA_TYPE",
    "build_app",
    "build_routes",
    "console_of",
    "form_values",
    "session_of",
]

# Where the console object and the verified session are put for a handler to find.
CONSOLE_STATE_KEY: Final[str] = "molt_console"
SESSION_STATE_KEY: Final[str] = "molt_session"

# Where an unauthenticated browser is sent, and what the specification is served as.
LOGIN_PATH: Final[str] = "/login"
SPECIFICATION_MEDIA_TYPE: Final[str] = "application/json"

# The one form encoding the console's own forms are submitted in.
_MEDIA_FORM: Final[str] = "application/x-www-form-urlencoded"

# The identifier the reachability probe reads with. It names no Session and no
# Client, so the probe proves the cluster answered and returns no row whatever the
# cluster holds.
PROBE_IDENTIFIER: Final[UUID] = UUID(int=0)

# The vocabulary of the health body, as fixed strings so a test asserts a name.
_LIVE: Final[str] = "ok"
_DEGRADED: Final[str] = "degraded"
_REACHABLE: Final[str] = "reachable"
_UNREACHABLE: Final[str] = "unreachable"

# One body per refusal kind. An absent session and an expired one produce the same
# answer, as do an absent CSRF token and a wrong one.
_UNAUTHENTICATED: Final[dict[str, str]] = {"error": "authentication required"}
_FORBIDDEN: Final[dict[str, str]] = {"error": "forbidden"}
_NOT_FOUND: Final[dict[str, str]] = {"error": "no such route"}
_NOT_IMPLEMENTED: Final[dict[str, str]] = {"error": "not implemented"}
_UNAVAILABLE: Final[dict[str, str]] = {"error": "unavailable"}
_FAILED: Final[dict[str, str]] = {"error": "the request could not be completed"}

_OK: Final[int] = 200
_SEE_OTHER: Final[int] = 303
_UNAUTHORISED: Final[int] = 401
_FORBIDDEN_STATUS: Final[int] = 403
_NOT_FOUND_STATUS: Final[int] = 404
_NOT_IMPLEMENTED_STATUS: Final[int] = 501

# The package whose import attaches every view module's handlers. Resolved by name at
# the point of use rather than imported here, because every view module imports this
# module's request helpers and an import at this level would close that cycle.
_VIEW_PACKAGE: Final[str] = "molt.console.routes"
_UNAVAILABLE_STATUS: Final[int] = 503


def console_of(request: Request) -> Console:
    """The console object this request is served by."""
    return cast(Console, getattr(request.app.state, CONSOLE_STATE_KEY))


def session_of(request: Request) -> auth.Session | None:
    """The verified session of this request, or None on a public route."""
    return cast("auth.Session | None", request.scope.get(SESSION_STATE_KEY))


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Refuse first, dispatch second, by route name from the table.

    The route is matched by name rather than by method or by path prefix, so a new
    route carries the posture the table gives it and no default of this middleware's
    own. A route the table does not name cannot be reached: the application is built
    from the table, so there is no such route to reach.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Apply the table's posture to one request before its handler runs."""
        matched = _matched(request)
        if matched is _ASSET:
            return await call_next(request)
        if matched is None:
            return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
        spec = cast(RouteSpec, matched)
        request.scope["route_name"] = spec.name
        if spec.public:
            return await call_next(request)

        console = console_of(request)
        session = auth.verify_cookie(
            request.cookies.get(auth.COOKIE_NAME),
            console.session_key.reveal(),
            now=console.now(),
        )
        if session is None:
            return _unauthenticated(request, spec)
        request.scope[SESSION_STATE_KEY] = session
        if spec.mutation and not await _csrf_accepted(request, session):
            log(
                Severity.WARNING,
                COMPONENT,
                "a mutation route was reached without the session's own CSRF token",
                route=spec.name,
            )
            return JSONResponse(dict(_FORBIDDEN), status_code=_FORBIDDEN_STATUS)
        return await call_next(request)


# The answer `_matched` gives for a request to the static asset mount, which carries
# no route posture because it serves the stylesheet and no memory content.
_ASSET: Final[object] = object()


def _matched(request: Request) -> RouteSpec | object | None:
    """Which declared route this request is for, decided before the router runs.

    The middleware stack runs ahead of routing, so the match is made here against
    the same route objects the router will use. A path that matches a route but not
    its method carries that route's posture too, so a wrong-method request to an
    authenticated path is refused for want of a session rather than answered with a
    method refusal that would confirm the path exists.
    """
    partial: RouteSpec | None = None
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match is Match.NONE:
            continue
        if not isinstance(route, Route):
            return _ASSET
        spec = route_named(str(route.name))
        if match is Match.FULL:
            return spec
        partial = spec
    return partial


def _unauthenticated(request: Request, spec: RouteSpec) -> Response:
    """The one answer an unauthenticated caller gets, in the form it can read.

    A browser navigating to a page is redirected to the login form and an
    interchange caller is refused with 401. Both are refusals: the redirect carries
    no content of the route it was sent from, and the status of the interchange
    answer is the same whether the cookie was absent, forged, or expired.
    """
    log(
        Severity.INFO,
        COMPONENT,
        "a route requiring a session was reached without one",
        route=spec.name,
    )
    if spec.method == "GET" and _wants_html(request):
        return RedirectResponse(LOGIN_PATH, status_code=_SEE_OTHER)
    return JSONResponse(dict(_UNAUTHENTICATED), status_code=_UNAUTHORISED)


def _wants_html(request: Request) -> bool:
    """Whether the caller asked for a document rather than for an interchange body."""
    return "text/html" in request.headers.get("accept", "")


async def form_values(request: Request) -> Mapping[str, str]:
    """The submitted form as a mapping, parsed with the standard library.

    The form encoding is read here rather than through the framework's own parser,
    because that parser needs a multipart package this deployment does not carry and
    every console form is url-encoded. A body that is not a url-encoded form yields
    an empty mapping, which every caller treats as a refusal rather than as consent.
    """
    if _MEDIA_FORM not in request.headers.get("content-type", ""):
        return {}
    raw = await request.body()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    return dict(parse_qsl(text, keep_blank_values=True))


async def _csrf_accepted(request: Request, session: auth.Session) -> bool:
    """Whether the mutation carries this session's own CSRF token.

    The token is read from the form body, with the header form accepted as well so
    a progressive-enhancement submission need not re-encode a form.
    """
    submitted = request.headers.get("x-csrf-token")
    if submitted is None:
        submitted = (await form_values(request)).get(auth.CSRF_FIELD)
    return auth.csrf_accepted(session, submitted)


def _adapted(endpoint: Endpoint) -> Callable[[Request], Awaitable[Response]]:
    """Give a claimed handler the one signature the router is built against."""

    async def serve(request: Request) -> Response:
        return cast(Response, await endpoint(request))

    return serve


def _placeholder(spec: RouteSpec) -> Callable[[Request], Awaitable[Response]]:
    """The answer a declared route with no claimed handler gives, after the check."""

    async def serve(request: Request) -> Response:  # noqa: ARG001
        return JSONResponse(
            dict(_NOT_IMPLEMENTED) | {"route": spec.name},
            status_code=_NOT_IMPLEMENTED_STATUS,
        )

    return serve


def build_routes() -> list[BaseRoute]:
    """Construct one Starlette route per table entry, and nothing else.

    The route's name is the table's name, so the middleware, the demonstration
    denylist, and the Interface_Specification all key on one identifier.

    The view package is imported here, inside the call, and that placement is the whole
    of why every page of this console once answered *not implemented*. Importing the
    package is what attaches the handlers: each view module claims its declared routes
    by name at import time, and the package's import list is the single place that says
    which modules exist. Nothing imported it. Every route therefore resolved to the
    placeholder below, in every deployment and locally alike, while eighteen written
    handlers sat unreferenced.

    It cannot be a module-level import, which is presumably how it came to be missing:
    every view module imports the request helpers from this module, so importing the
    package from this module's body closes a cycle and fails at start-up. Inside the
    call there is no cycle, because this module is fully initialised before anything
    calls it.

    It is resolved by name for the reason the store resolves its driver by name: the
    import exists for its effect rather than for anything it returns, and a bound name
    nothing reads is a line a reader is entitled to delete. What the name is right is
    asserted by a gate that builds the routes through this call and refuses any route
    the placeholder would answer, so a wrong module name fails there rather than
    silently restoring the defect.
    """
    importlib.import_module(_VIEW_PACKAGE)
    routes: list[BaseRoute] = []
    for spec in ROUTE_TABLE:
        claimed = HANDLERS.get(spec.name)
        endpoint = _placeholder(spec) if claimed is None else _adapted(claimed)
        routes.append(
            Route(
                spec.path,
                _tagged(spec, endpoint),
                methods=[spec.method],
                name=spec.name,
            )
        )
    return routes


def _tagged(
    spec: RouteSpec, endpoint: Callable[[Request], Awaitable[Response]]
) -> Callable[[Request], Awaitable[Response]]:
    """Bind the route name into the scope, so the middleware can read it by name."""

    async def serve(request: Request) -> Response:
        request.scope["route_name"] = spec.name
        return await endpoint(request)

    return serve


# ---------------------------------------------------------------------------
# The two public routes
# ---------------------------------------------------------------------------


async def health(request: Request) -> Response:
    """Report liveness, reachability, the capability summary, and the mode flag.

    No memory content appears: the reachability probe is keyed on the nil
    identifier, the capability summary is read from what the store already holds,
    and no count of anything stored is reported (Requirements 25.10, 31.5).
    """
    console = console_of(request)
    reachable = _reachable(console)
    record = _known_capabilities(console)
    document: dict[str, object] = {
        "status": _LIVE if reachable else _DEGRADED,
        "component": COMPONENT,
        "database": _REACHABLE if reachable else _UNREACHABLE,
        "database_role": console.store.role,
        "demo_mode": console.demo_mode,
        "capabilities": {
            name: {"probed": record.probed(name), "available": record.available(name)}
            for name in PROBED_CAPABILITIES
        },
        "unprobed": list(record.unprobed),
        "routes": len(ROUTE_TABLE),
    }
    return JSONResponse(document, status_code=_OK)


def _reachable(console: Console) -> bool:
    """Whether the cluster answered a read that can match no row."""
    try:
        session_of_client(console.store, PROBE_IDENTIFIER, PROBE_IDENTIFIER)
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "the cluster did not answer the reachability probe",
            error_type=type(error).__name__,
        )
        return False
    return True


def _known_capabilities(console: Console) -> CapabilityRecord:
    """The capability record the store holds, without requiring a read privilege."""
    try:
        return console.store.known_capabilities()
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "the capability record could not be read",
            error_type=type(error).__name__,
        )
        return CapabilityRecord()


async def specification(request: Request) -> Response:
    """Serve the tracked Interface_Specification document verbatim.

    It reads no table and opens no transaction: it describes shapes rather than
    content, which is why it is public (Requirement 51.4). An absent document is a
    503 naming no path, because the path is a deployment fact rather than a caller's
    concern.
    """
    console = console_of(request)
    path = console.settings.interface_spec_path
    body = _read_document(path)
    if body is None:
        return JSONResponse(dict(_UNAVAILABLE), status_code=_UNAVAILABLE_STATUS)
    return Response(body, status_code=_OK, media_type=SPECIFICATION_MEDIA_TYPE)


def _read_document(path: Path) -> bytes | None:
    """The document's bytes, or None when it cannot be read at all."""
    try:
        return path.read_bytes()
    except OSError as error:
        log(
            Severity.ERROR,
            COMPONENT,
            "the interface specification document could not be read",
            error_type=type(error).__name__,
        )
        return None


async def login_form(request: Request) -> Response:
    """The credential form: one labelled password input and one submit control."""
    console = console_of(request)
    templates = _templates(request)
    if templates is None:
        return PlainTextResponse(
            "the console templates are not available", status_code=_UNAVAILABLE_STATUS
        )
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "title": "Sign in",
            "demo_mode": console.demo_mode,
            "credential_field": auth.CREDENTIAL_FIELD,
            "authenticated": False,
            "csrf_token": "",
        },
    )


async def login_submit(request: Request) -> Response:
    """Verify the presented credential and issue the session cookie on a match.

    The comparison is against the stored hash and is constant time. A refusal is one
    answer for an absent value and for a wrong one, and it carries no hint about
    which.
    """
    console = console_of(request)
    presented = (await form_values(request)).get(auth.CREDENTIAL_FIELD)
    accepted = presented is not None and auth.verify_credential(
        presented, console.credential.reveal()
    )
    if not accepted:
        log(Severity.WARNING, COMPONENT, "a console credential was refused")
        return JSONResponse(dict(_UNAUTHENTICATED), status_code=_UNAUTHORISED)
    session, cookie = auth.issue(
        console.session_key.reveal(),
        now=console.now(),
        lifetime=console.settings.session_lifetime,
    )
    answer = RedirectResponse("/", status_code=_SEE_OTHER)
    answer.set_cookie(
        auth.COOKIE_NAME,
        cookie,
        max_age=int((session.expires_at - session.issued_at).total_seconds()),
        path=auth.COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    log(Severity.INFO, COMPONENT, "a console session was issued", subject=session.subject)
    return answer


async def logout(request: Request) -> Response:  # noqa: ARG001
    """Revoke the session cookie. Requires the session and its CSRF token."""
    answer = RedirectResponse(LOGIN_PATH, status_code=_SEE_OTHER)
    answer.delete_cookie(
        auth.COOKIE_NAME,
        path=auth.COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return answer


def _templates(request: Request) -> Jinja2Templates | None:
    """The template environment, or None when the asset directory is absent."""
    return cast("Jinja2Templates | None", getattr(request.app.state, "molt_templates", None))


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def _not_found(request: Request, exc: Exception) -> Response:  # noqa: ARG001
    """A path the table declares no route for, answered without disclosing more."""
    return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)


async def _failure(request: Request, exc: Exception) -> Response:
    """An unexpected failure: one fixed body, and the cause in the log record."""
    log(
        Severity.ERROR,
        COMPONENT,
        "a console request failed",
        route=request.scope.get("route_name"),
        error_type=type(exc).__name__,
    )
    return JSONResponse(dict(_FAILED), status_code=500)


def build_app(console: Console) -> Starlette:
    """Build the one application object, from the route table and this console.

    The same object is what `molt serve` runs locally and what the Lambda adapter
    invokes in the deployed configuration.
    """
    routes = build_routes()
    static = console.settings.static_directory
    if static.is_dir():
        routes.append(Mount(STATIC_MOUNT, app=StaticFiles(directory=static), name="static"))

    app = Starlette(
        routes=routes,
        exception_handlers={404: _not_found, 500: _failure, Exception: _failure},
    )
    setattr(app.state, CONSOLE_STATE_KEY, console)
    templates = console.settings.template_directory
    app.state.molt_templates = Jinja2Templates(directory=templates) if templates.is_dir() else None
    app.add_middleware(AuthenticationMiddleware)
    # Added last, so it is outermost: demonstration mode refuses a blocked route
    # before authentication is consulted and before any handler is chosen.
    app.add_middleware(DemonstrationMiddleware, console=console)
    log(
        Severity.INFO,
        COMPONENT,
        "built the console application from its route table",
        routes=len(ROUTE_TABLE),
        claimed_handlers=len(HANDLERS),
        demo_mode=console.demo_mode,
    )
    return app


# The four routes this task implements claim their table entries here rather than in
# a view module, because they are the skeleton's own routes: two are public and
# unauthenticated by design and two are the way a session is obtained and revoked.
HANDLERS.setdefault("health", health)
HANDLERS.setdefault("specification", specification)
HANDLERS.setdefault("login_form", login_form)
HANDLERS.setdefault("login_submit", login_submit)
HANDLERS.setdefault("logout", logout)
