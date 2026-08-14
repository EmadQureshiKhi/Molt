"""Read-only demonstration mode: a gate ahead of routing, keyed on the route table.

Demonstration mode is a safety property rather than a feature, so it is arranged
the way the authentication posture is arranged: **refuse first, dispatch second.**
A per-handler check is opt-in and therefore forgettable, and a handler that forgot
to ask would be a mutation route reachable by an anonymous visitor to a public
demonstration. The gate here is middleware, it runs ahead of routing, and it decides
from the route's *declared disposition* in `ROUTE_TABLE`. A handler cannot opt out of
it and cannot forget it, because a refused request never reaches a handler at all.

Three consequences follow from deciding before dispatch:

1. A `BLOCKED` route is refused even when no view module has claimed it. The
   declaration is what is enforced, so the unclaimed-route placeholder that answers
   `501` is unreachable in demonstration mode and a view written later inherits the
   refusal it was declared with rather than acquiring one.
2. The refusal is `403 Forbidden` and not `404`. The route exists, the caller is
   recognised, and the request is refused on policy: that is what 403 states, and
   the demonstration is meant to *show* that the erasure console exists while
   refusing to run it. It is also the status Property 38 asserts for every mutation
   route in a demonstration context.
3. Classification is by route name. A route whose name the table does not classify
   is refused in demonstration mode rather than allowed, so the failure mode of
   forgetting to classify is a visibly refused route rather than a silently open
   one (Requirement 25.12).

**`BLOCKED` is refused; `HIDDEN` is not.** The design states demonstration mode
changes three things and nothing else, and the denylist it names is exactly the set
marked blocked. `HIDDEN` says a route is left out of the demonstration's navigation,
which is a rendering decision this module answers with `navigable`, not a refusal:
`GET /erase` is hidden from the navigation yet must stay reachable, because the
demonstration shows what the erasure console *is* with its controls disabled and an
accessible explanation, rather than hiding it (Requirement 25.5). Refusing a hidden
read route would remove the very thing the demonstration is for.

**The anonymous read-only principal.** A demonstration visitor carries no operator
credential, so the gate establishes one anonymous principal whose permitted Client
set is the seeded tenants and nothing else, and hands the request the session the
authentication middleware then verifies. The principal is minted once per process
and re-minted when its absolute expiry passes, so the CSRF token a rendered form
carries is the token the next request submits. Nothing about it widens the posture:
every mutation route on the denylist is already refused above it, and the two
mutation routes that are not on the denylist still require this session's own token.

**Replaying rather than running.** No route here replays anything. The streaming
view, the redaction comparison, and the certificate display are all declared
`READ_ONLY`, so a completed seeded run is observable through the same views in
demonstration mode with no mutation route involved: the run identifier a
demonstration is pointed at is a seeded one, which is what `DemoPrincipal.clients`
restricts reading to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match, Route
from starlette.types import ASGIApp

from molt.console import auth
from molt.console.deps import COMPONENT, Console
from molt.console.routing import (
    MUTATION_ROUTE_NAMES,
    DemoDisposition,
    RouteTableError,
    route_named,
)
from molt.seed.corpora import DOMAINS
from molt.telemetry import Severity, log

__all__ = [
    "BLOCKED_EXPLANATION",
    "DEMO_PRINCIPAL_KEY",
    "DEMO_STATE_KEY",
    "DEMO_SUBJECT",
    "REFUSAL_STATUS",
    "SEEDED_CLIENT_SLUGS",
    "DemoPrincipal",
    "DemonstrationMiddleware",
    "Verdict",
    "control_disabled",
    "demonstration_context",
    "navigable",
    "principal_of",
    "refused_route_names",
    "verdict_for",
]

# Where the mode flag and the anonymous principal are put for a view to find.
DEMO_STATE_KEY: Final[str] = "molt_demo_mode"
DEMO_PRINCIPAL_KEY: Final[str] = "molt_demo_principal"

# What the demonstration principal is named in the session payload and the logs. It
# is not the operator subject, so a record made under it is distinguishable from a
# record made by someone holding the credential.
DEMO_SUBJECT: Final[str] = "demonstration"

# The Clients a demonstration visitor may read: the seeded tenants, taken from the
# seed corpus itself rather than restated, so a demonstration can never read a
# Client that a real engagement created.
SEEDED_CLIENT_SLUGS: Final[frozenset[str]] = frozenset(domain.slug for domain in DOMAINS)

# The one refusal a blocked route gives, and the accessible explanation a disabled
# control carries. Both are fixed strings: neither names a row, a value, or a path.
REFUSAL_STATUS: Final[int] = 403
_REFUSED: Final[dict[str, str]] = {"error": "read-only demonstration mode"}
BLOCKED_EXPLANATION: Final[str] = (
    "Disabled in read-only demonstration mode, which refuses every route that would "
    "change stored memory."
)


class Verdict(StrEnum):
    """What the gate does with one request: two values and no third."""

    ALLOW = "allow"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class DemoPrincipal:
    """The anonymous read-only principal a demonstration request is served as."""

    subject: str = DEMO_SUBJECT
    read_only: bool = True
    clients: frozenset[str] = SEEDED_CLIENT_SLUGS

    def may_read(self, client_slug: str) -> bool:
        """Whether this principal may read a Client, which the seeded set decides."""
        return client_slug in self.clients


def refused_route_names() -> frozenset[str]:
    """The routes demonstration mode refuses: the table's own blocked set."""
    return MUTATION_ROUTE_NAMES


def verdict_for(route_name: str, *, demo_mode: bool) -> Verdict:
    """What demonstration mode does with a route, decided from its name alone.

    Outside demonstration mode every route is allowed, so this is the whole of the
    mode's effect on reachability. Inside it, a name on the blocked set is refused,
    and so is a name the table does not classify at all.
    """
    if not demo_mode:
        return Verdict.ALLOW
    if route_name in MUTATION_ROUTE_NAMES:
        return Verdict.REFUSE
    try:
        route_named(route_name)
    except RouteTableError:
        return Verdict.REFUSE
    return Verdict.ALLOW


def control_disabled(route_name: str, *, demo_mode: bool) -> bool:
    """Whether a control submitting to this route renders disabled rather than absent.

    A template pairs this with `BLOCKED_EXPLANATION`, so the demonstration shows the
    control it will not run and states why (Requirement 25.12).
    """
    return verdict_for(route_name, demo_mode=demo_mode) is Verdict.REFUSE


def navigable(route_name: str, *, demo_mode: bool) -> bool:
    """Whether this route is offered in the demonstration's navigation.

    `HIDDEN` is the only disposition this answers no to, and it is a rendering
    answer rather than a refusal: the route stays reachable, it is simply not linked.
    """
    if not demo_mode:
        return True
    try:
        spec = route_named(route_name)
    except RouteTableError:
        return False
    return spec.demo is not DemoDisposition.HIDDEN


def principal_of(request: Request) -> DemoPrincipal | None:
    """The demonstration principal this request is served as, or None outside the mode."""
    return cast("DemoPrincipal | None", request.scope.get(DEMO_PRINCIPAL_KEY))


def demonstration_context(request: Request) -> dict[str, object]:
    """The template context demonstration mode contributes, for a view to merge in.

    `demo_mode` is what the layout renders its banner from, and the explanation is
    what a disabled control states.
    """
    principal = principal_of(request)
    return {
        "demo_mode": principal is not None,
        "demo_explanation": BLOCKED_EXPLANATION,
        "demo_blocked_routes": sorted(refused_route_names()),
        "demo_clients": sorted(principal.clients) if principal is not None else [],
    }


class DemonstrationMiddleware(BaseHTTPMiddleware):
    """Refuse the blocked routes, then serve the rest as the anonymous principal.

    The console is passed in at construction rather than read from the application
    state, so the one input the gate has — whether this deployment is a demonstration
    — is explicit at the point the middleware is added to the stack.
    """

    def __init__(self, app: ASGIApp, *, console: Console) -> None:
        super().__init__(app)
        self._console = console
        self._principal = DemoPrincipal()
        self._minted: tuple[auth.Session, str] | None = None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Apply the mode to one request, before the router picks a handler for it."""
        if not self._console.demo_mode:
            return await call_next(request)
        request.scope[DEMO_STATE_KEY] = True
        refused = self._refused_name(request)
        if refused is not None:
            log(
                Severity.WARNING,
                COMPONENT,
                "a route the table classifies as a mutation was refused in "
                "read-only demonstration mode",
                route=refused,
            )
            return JSONResponse(dict(_REFUSED) | {"route": refused}, status_code=REFUSAL_STATUS)
        self._establish(request)
        return await call_next(request)

    def _refused_name(self, request: Request) -> str | None:
        """The name of the blocked route this request is for, if it is for one.

        A full match decides, and a partial match — a path this table declares under
        another method — decides only when nothing matched fully, so a request for a
        read route sharing a path with a blocked one is not refused for its
        neighbour's disposition. A wrong-method request to a blocked path still is.
        """
        full: list[str] = []
        partial: list[str] = []
        for route in request.app.routes:
            if not isinstance(route, Route):
                continue
            match, _ = route.matches(request.scope)
            if match is Match.NONE:
                continue
            (full if match is Match.FULL else partial).append(str(route.name))
        for name in full or partial:
            if verdict_for(name, demo_mode=True) is Verdict.REFUSE:
                return name
        return None

    def _establish(self, request: Request) -> None:
        """Put the anonymous principal on the request, and give it a session to carry.

        A visitor who already holds a valid operator session keeps it; only a request
        carrying no usable session is given the demonstration one, and it is carried
        as the same signed cookie the authentication middleware verifies, so there is
        no second way into an authenticated route.
        """
        request.scope[DEMO_PRINCIPAL_KEY] = self._principal
        console = self._console
        held = auth.verify_cookie(
            request.cookies.get(auth.COOKIE_NAME),
            console.session_key.reveal(),
            now=console.now(),
        )
        if held is not None:
            return
        _, cookie = self.principal_session()
        _carry(request, cookie)

    def principal_session(self) -> tuple[auth.Session, str]:
        """The demonstration session for this process, minted once and re-minted on expiry.

        One session per process rather than one per request, because a form rendered
        under one request is submitted under the next and a per-request CSRF token
        would refuse every submission. A new process mints a new one, which a visitor
        experiences exactly as an operator experiences an expired session.
        """
        console = self._console
        now = console.now()
        held = self._minted
        if held is not None and not held[0].expired_at(now):
            return held
        minted = auth.issue(
            console.session_key.reveal(),
            now=now,
            lifetime=console.settings.session_lifetime,
            subject=DEMO_SUBJECT,
        )
        self._minted = minted
        return minted


def _carry(request: Request, cookie: str) -> None:
    """Replace this request's cookie header with the demonstration session's own.

    The header list in the scope is what the authentication middleware reads its
    cookies from, and the scope is shared with the rest of the stack, so setting it
    here is what makes the principal established rather than merely announced.
    """
    headers = [
        (name, value)
        for name, value in cast("list[tuple[bytes, bytes]]", request.scope["headers"])
        if name.lower() != b"cookie"
    ]
    headers.append((b"cookie", f"{auth.COOKIE_NAME}={cookie}".encode()))
    request.scope["headers"] = headers
