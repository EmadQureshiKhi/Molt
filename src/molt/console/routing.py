"""The console route table, held as data so its security posture is inspectable.

The console's function endpoint declares no request signing, because the
distribution in front of it cannot sign as a cloud principal. Authentication is
therefore entirely the application's own, and the property that matters is a
property of the whole route set rather than of any one handler: *every route that
is not explicitly public requires a valid session*.

A property of a whole set can only be checked if the set is a value. So the table
lives here as an immutable tuple of `RouteSpec` records, each naming its method,
its path, whether it is public, whether it mutates, and how demonstration mode
treats it. The application is built *from* this table rather than beside it, which
is what makes the table's answer the deployed answer: a route reachable in the
application but absent here cannot exist, because `build_routes` is the only thing
that constructs Starlette routes.

Four invariants are checked at import rather than left to review:

1. Only names in `PUBLIC_ROUTE_NAMES` may declare public access. A new route is
   authenticated by default, and making one public is an edit to an allowlist that
   a reviewer will see.
2. A public route may not mutate. A mutation reachable without a session is the
   failure mode the whole posture exists to prevent.
3. Route names are unique, because the demonstration denylist, the handler
   registry, and the CSRF classification are all keyed by name.
4. A route that declares `mutation=True` declares `BLOCKED`. A demonstration is
   obliged to expose no mutation route at all, so the disposition a mutating route
   may carry is the refusing one and there is no second reading of the table: the
   denylist and the mutation set are the same set by construction rather than by
   two derivations that could drift.

Handlers are attached by name through `HANDLERS`. That indirection is what lets
the view tasks add a handler module without touching the table, and lets the table
declare a route before its view exists: an unclaimed route answers
`501 Not Implemented` *after* the authentication check, so an unfinished view is
never an unauthenticated one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from molt.errors import MoltError

__all__ = [
    "HANDLERS",
    "MUTATION_ROUTE_NAMES",
    "PUBLIC_ROUTE_NAMES",
    "ROUTE_TABLE",
    "Access",
    "DemoDisposition",
    "Endpoint",
    "RouteSpec",
    "RouteTableError",
    "authenticated_routes",
    "mutation_routes",
    "public_routes",
    "register",
    "registered",
    "route_named",
]


class RouteTableError(MoltError):
    """The route table declares something the posture forbids."""


class Access(StrEnum):
    """What a request must carry to reach a route.

    Two values and no third. There is no *optional* session: a route either
    answers an anonymous caller or refuses one, and a route that would answer
    differently depending on what it was given is a route whose posture cannot be
    stated as a property.
    """

    PUBLIC = "public"
    SESSION = "session"


class DemoDisposition(StrEnum):
    """How read-only demonstration mode treats a route.

    Classification is by route name rather than by method, so a route added later
    is blocked until it is classified here (Requirement 25.12). `BLOCKED` is the
    denylist the demonstration middleware rejects on, and every route declaring
    `mutation=True` is required to declare it: a demonstration exposes no mutation
    route, so a mutating route carrying `READ_ONLY` or `HIDDEN` is a table the
    import-time check refuses rather than a route the mode dispatches.

    The other three are rendering decisions. `VISIBLE` and `READ_ONLY` are offered
    in the navigation and reachable; `HIDDEN` is reachable and not linked, which is
    what keeps `GET /erase` observable with its controls disabled (Requirement 25.5).
    """

    VISIBLE = "visible"
    READ_ONLY = "read-only"
    HIDDEN = "hidden"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """One route as a value: its shape, its posture, and its demonstration standing."""

    name: str
    method: str
    path: str
    access: Access
    demo: DemoDisposition
    mutation: bool
    summary: str

    @property
    def authenticated(self) -> bool:
        """Whether a valid session cookie is required to reach this route."""
        return self.access is Access.SESSION

    @property
    def public(self) -> bool:
        """Whether this route answers a caller carrying no session."""
        return self.access is Access.PUBLIC


# The only routes that answer an anonymous caller. Each one is public for a stated
# reason and none of them reads a memory row: the health route reports liveness,
# reachability, and platform facts; the specification route serves a tracked
# document describing shapes; and the login pair is how a session is obtained at
# all, so requiring one would leave no way in.
PUBLIC_ROUTE_NAMES: Final[frozenset[str]] = frozenset(
    {"health", "specification", "login_form", "login_submit"}
)

_DECLARED_ROUTES: Final[tuple[RouteSpec, ...]] = (
    RouteSpec(
        name="health",
        method="GET",
        path="/health",
        access=Access.PUBLIC,
        demo=DemoDisposition.VISIBLE,
        mutation=False,
        summary="liveness, database reachability, capability record, and the mode flag",
    ),
    RouteSpec(
        name="specification",
        method="GET",
        path="/spec",
        access=Access.PUBLIC,
        demo=DemoDisposition.VISIBLE,
        mutation=False,
        summary="the tracked Interface_Specification document, served verbatim",
    ),
    RouteSpec(
        name="login_form",
        method="GET",
        path="/login",
        access=Access.PUBLIC,
        demo=DemoDisposition.HIDDEN,
        mutation=False,
        summary="the operator credential form",
    ),
    RouteSpec(
        name="login_submit",
        method="POST",
        path="/login",
        access=Access.PUBLIC,
        demo=DemoDisposition.HIDDEN,
        mutation=False,
        summary="verify the operator credential and issue the session cookie",
    ),
    RouteSpec(
        name="logout",
        method="POST",
        path="/logout",
        access=Access.SESSION,
        demo=DemoDisposition.BLOCKED,
        mutation=True,
        summary="revoke the session cookie",
    ),
    RouteSpec(
        name="fleet",
        method="GET",
        path="/",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="fleet overview of live Sessions",
    ),
    RouteSpec(
        name="session_detail",
        method="GET",
        path="/sessions/{session_id}",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="the Event stream of one Session with chain verification status",
    ),
    RouteSpec(
        name="lineage",
        method="GET",
        path="/lineage",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="the Lineage_Graph view, filterable by Client",
    ),
    RouteSpec(
        name="lineage_artifact",
        method="GET",
        path="/lineage/{artifact_id}",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="the ancestor and descendant subgraph of one Artifact",
    ),
    RouteSpec(
        name="residue",
        method="GET",
        path="/residue",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="semantic residue search with cosine distances",
    ),
    RouteSpec(
        name="sensitivity",
        method="GET",
        path="/sensitivity",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="the Threshold_Grid view of the Sensitivity_Analyzer report",
    ),
    RouteSpec(
        name="procedures",
        method="GET",
        path="/procedures",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="Learned_Procedure standing with the change history per procedure",
    ),
    RouteSpec(
        name="tiers",
        method="GET",
        path="/tiers",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="the Memory_Tier view with live per-tier counts",
    ),
    RouteSpec(
        name="erase_console",
        method="GET",
        path="/erase",
        access=Access.SESSION,
        demo=DemoDisposition.HIDDEN,
        mutation=False,
        summary="the erasure console: Client picker, thresholds, dry-run toggle",
    ),
    RouteSpec(
        name="erase_start",
        method="POST",
        path="/erase",
        access=Access.SESSION,
        demo=DemoDisposition.BLOCKED,
        mutation=True,
        summary="record an Erasure_Run and return its identifier",
    ),
    RouteSpec(
        name="erase_stream",
        method="GET",
        path="/erase/{run_id}/stream",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="phase progress streamed from the durable phase and disposition rows",
    ),
    RouteSpec(
        name="erase_detail",
        method="GET",
        path="/erase/{run_id}",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="run detail with per-Artifact Dispositions",
    ),
    RouteSpec(
        name="erase_redaction",
        method="GET",
        path="/erase/{run_id}/redactions/{artifact_id}",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="the before and after comparison of one Blended_Artifact body",
    ),
    RouteSpec(
        name="certificate",
        method="GET",
        path="/certificates/{run_id}",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="the certificate display",
    ),
    RouteSpec(
        name="certificate_verify",
        method="POST",
        path="/certificates/{run_id}/verify",
        access=Access.SESSION,
        demo=DemoDisposition.BLOCKED,
        mutation=True,
        summary="trigger a live verification and display the outcome",
    ),
    RouteSpec(
        name="retention",
        method="GET",
        path="/retention",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="per-Client Jurisdiction, interval, and expiring and expired counts",
    ),
    RouteSpec(
        name="approvals",
        method="GET",
        path="/approvals",
        access=Access.SESSION,
        demo=DemoDisposition.READ_ONLY,
        mutation=False,
        summary="the Approval_Queue list",
    ),
    RouteSpec(
        name="approval_resolve",
        method="POST",
        path="/approvals/{approval_id}",
        access=Access.SESSION,
        demo=DemoDisposition.BLOCKED,
        mutation=True,
        summary="resolve one queued approval",
    ),
)


def _validated(table: tuple[RouteSpec, ...]) -> tuple[RouteSpec, ...]:
    """Check the three invariants at import, so a bad table never builds an app."""
    seen: set[str] = set()
    for spec in table:
        if spec.name in seen:
            raise RouteTableError(f"the route name {spec.name!r} is declared more than once")
        seen.add(spec.name)
        if spec.public and spec.name not in PUBLIC_ROUTE_NAMES:
            raise RouteTableError(
                f"the route {spec.name!r} declares public access without being named in "
                "the public allowlist"
            )
        if spec.public and spec.mutation:
            raise RouteTableError(
                f"the route {spec.name!r} is public and mutating, which no route may be"
            )
        if spec.mutation and spec.demo is not DemoDisposition.BLOCKED:
            raise RouteTableError(
                f"the route {spec.name!r} mutates and declares the demonstration "
                f"disposition {spec.demo.value!r}, where a mutating route may only "
                "declare blocked: a demonstration exposes no mutation route"
            )
    absent = PUBLIC_ROUTE_NAMES - seen
    if absent:
        raise RouteTableError(
            "the public allowlist names routes the table does not declare: "
            + ", ".join(sorted(absent))
        )
    return table


ROUTE_TABLE: Final[tuple[RouteSpec, ...]] = _validated(_DECLARED_ROUTES)

# The demonstration denylist, derived from the table rather than restated, so the
# middleware and the table cannot disagree.
#
# It is derived from `mutation` rather than from the disposition, because *mutating* is
# the property the obligation is about: a demonstration exposes no route that would
# change stored memory (Requirement 25.12). Deriving it from the disposition instead
# made the denylist a second, editable opinion about which routes mutate, and the two
# had already diverged. The fourth import-time invariant keeps the two readings equal
# — every mutating route declares blocked — so this set is also exactly the blocked
# set, and a route can no longer slip out of it by carrying another disposition.
#
# The CSRF classification is a different consumer with a different question, and it
# reads `RouteSpec.mutation` directly through `mutation_routes` and the authentication
# middleware. Both now key on the same declaration, so widening the denylist narrowed
# nothing: a route refused in a demonstration still requires the session's own token
# in every other configuration.
MUTATION_ROUTE_NAMES: Final[frozenset[str]] = frozenset(
    spec.name for spec in ROUTE_TABLE if spec.mutation
)


def route_named(name: str) -> RouteSpec:
    """The one route carrying this name, refusing a name the table does not declare."""
    for spec in ROUTE_TABLE:
        if spec.name == name:
            return spec
    raise RouteTableError(f"the route table declares no route named {name!r}")


def public_routes() -> tuple[RouteSpec, ...]:
    """Every route that answers a caller carrying no session."""
    return tuple(spec for spec in ROUTE_TABLE if spec.public)


def authenticated_routes() -> tuple[RouteSpec, ...]:
    """Every route that refuses a caller carrying no valid session."""
    return tuple(spec for spec in ROUTE_TABLE if spec.authenticated)


def mutation_routes() -> tuple[RouteSpec, ...]:
    """Every route that mutates, which is the set requiring a CSRF token."""
    return tuple(spec for spec in ROUTE_TABLE if spec.mutation)


# What a handler is, and where handlers are found by name. The registry is a plain
# mapping so a view module registers by import and a test drives the table with
# stand-ins of its own.
Endpoint = Callable[..., Awaitable[object]]
HANDLERS: Final[dict[str, Endpoint]] = {}


def register(name: str) -> Callable[[Endpoint], Endpoint]:
    """Claim one declared route for a handler, refusing an undeclared name.

    The name is checked against the table at decoration time, so a view module
    naming a route the table does not declare fails at import rather than serving
    a route with no stated posture.
    """
    spec = route_named(name)

    def claim(endpoint: Endpoint) -> Endpoint:
        HANDLERS[spec.name] = endpoint
        return endpoint

    return claim


def registered() -> Mapping[str, Endpoint]:
    """The handlers claimed so far, as a read-only view for inspection."""
    return dict(HANDLERS)
