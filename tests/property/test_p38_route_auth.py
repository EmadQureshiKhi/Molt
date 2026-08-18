"""Property 38: the served route set's posture, in three contexts at once.

**Validates: Requirements 25.9, 25.10, 25.12, 30.5, 51.4**

The route set is enumerated from the ASGI application object rather than from a list
written here, so a route added later is crossed with every context and with the
Interface_Specification without this module being edited. `build_app` constructs its
routes only from `ROUTE_TABLE`, so what the enumeration walks is the deployed route
set and not a copy of it.

Nothing here reaches a cluster. The console's reachability probe is a read keyed on
the nil identifier, so a stand-in store answering no rows is enough for the health
route to report that a store answered, and every other clause is a claim about a
refusal that is decided before any handler reads anything.

**This property is deliberately not the whole obligation restated.** Two suites
already hold the parts a single reading establishes: `tests/security/
test_route_authentication.py` asserts that the console's public set is exactly the
named exemptions and that no public route mutates, and `tests/spec/
test_specification_parses.py` asserts route and specification agreement in both
directions. What only a crossing can establish is asserted here.

**The three contexts.** A route is served with no cookie at all, with a valid
operator session and that session's own CSRF token, and with demonstration mode
enabled. The same route is therefore judged three ways, and each way has its own
obligation: outside the public allowlist an anonymous caller is refused with 401 or
403; a session reaches the route, so the refusal was for want of a session rather
than an unconditional one; and in demonstration mode the table's blocked set is
refused and nothing else is.

**A public route is asserted to be session-independent rather than never refused.**
The credential exchange is public and refuses a caller presenting a wrong
credential, so *public* cannot mean *answers 200*. What it means is that the answer
does not turn on holding a session, which is checked by serving the route both ways
and comparing.

**Demonstration containment is asserted on the deliberate asymmetry.** `BLOCKED` is
refused; `HIDDEN` is not. A hidden route is left out of the demonstration's
navigation and stays reachable, because the demonstration shows what the erasure
console is with its controls disabled rather than hiding it. So a hidden route
answering the demonstration refusal is a failure here, as is a blocked route
answering anything else. Two further clauses come with it: a blocked route no view
module has claimed is refused rather than answering the placeholder's 501, so the
declaration is what is enforced; and a route name the table classifies at all is
refused in demonstration mode, which is why an unclassified name is drawn per
example rather than named once.

**The memory-content clause is asserted at any depth, and asymmetrically between the
two public bodies, because the two bodies are different kinds of document.** The
health body is data, so a content key anywhere in it is a leak and the whole of
`CONTENT_KEYS` is searched for at every nesting depth. The specification body
*describes* shapes, so a declared field name is its subject matter: `credential` and
`excerpt` are field names it is obliged to name, and `content` is the interchange
keyword the document is written in. The clause asserted for it is therefore that no
content key appears outside a declaration position — a schema property map, a schema
collection, or the media-type container — which still fails the moment the document
carries `payload` as data. That the document names no content key as a *value* is
asserted in `tests/spec/test_served_specification.py` and is not restated here.
"""

# Feature: molt, Property 38: For any route in the Web_Console route table, a request
# without a valid session returns 401 or 403 unless the route is in the public
# allowlist, the health route body and the Interface_Specification route body each
# contain no key from the memory-content key set, every route the application
# declares appears in the Interface_Specification, and with demonstration mode
# enabled every route classified as a mutation returns 403. The property is asserted
# against the ASGI application object, which is the same object the deployed function
# serves, so it holds for the deployed console without needing a running server.

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, cast

from hypothesis import event, given, settings
from hypothesis import strategies as st
from starlette.applications import Starlette
from starlette.routing import Route

from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import build_app
from molt.console.demo import (
    REFUSAL_FIELD,
    REFUSAL_REASON,
    REFUSAL_STATUS,
    Verdict,
    navigable,
    verdict_for,
)
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.console.routing import DemoDisposition, RouteSpec, route_named
from molt.models.event import JsonValue
from molt.store.capability import CapabilityRecord
from molt.telemetry import CONTENT_KEYS

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOCUMENT_PATH: Final[Path] = REPOSITORY_ROOT / "docs" / "interface.json"

CREDENTIAL: Final[str] = "a-route-posture-property-credential"
SESSION_KEY: Final[str] = "a-route-posture-property-session-key"
# A fixed instant derived from a count of seconds, so this module carries no reading
# of a calendar and none of a clock.
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

# The statuses that are a refusal of a caller for want of a session or of a token.
REFUSAL_STATUSES: Final[frozenset[int]] = frozenset({401, REFUSAL_STATUS})

# The status the placeholder gives a declared route no view module has claimed. A
# blocked route answering it in demonstration mode would mean the gate ran after
# dispatch rather than before it.
NOT_IMPLEMENTED_STATUS: Final[int] = 501

# What the CSRF refusal says. It is distinguished from the demonstration refusal by
# body rather than by status, because both are 403 and only one of them is the mode's
# own.
CSRF_MARKER: Final[str] = "forbidden"

# The two public routes whose bodies the memory-content clause is about.
HEALTH_PATH: Final[str] = "/health"
SPECIFICATION_PATH: Final[str] = "/spec"

# Where a content key in the Interface_Specification is a declared field name rather
# than carried data: the keys of a schema property map and of the schema collection.
DECLARATION_PARENTS: Final[frozenset[str]] = frozenset({"properties", "schemas"})

# The one interchange keyword that collides with the content-key set. It holds the
# media-type map of a request or a response body, so it names a shape.
STRUCTURAL_KEYS: Final[frozenset[str]] = frozenset({"content"})

# The path parameters a declared route may carry, filled in so the router matches.
PATH_PARAMETERS: Final[tuple[str, ...]] = (
    "session_id",
    "artifact_id",
    "run_id",
    "approval_id",
)
FILLED_IDENTIFIER: Final[str] = "00000000-0000-0000-0000-000000000000"

# What the specification states a route's authentication requirement is, per posture.
NO_REQUIREMENT: Final[str] = "none"
SESSION_REQUIREMENT: Final[str] = "session"


class Context(StrEnum):
    """The three ways one route is served, which is the crossing this property adds."""

    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    DEMONSTRATION = "demonstration"


# ---------------------------------------------------------------------------
# The application object, over a stand-in store that reaches no cluster
# ---------------------------------------------------------------------------


class EmptyCursor:
    """A cursor answering every statement with no rows at all.

    A read behaves like a read: a store whose `read` ignored its body would hand a
    view a `None` where it expected rows, and the resulting failure would be the
    stand-in's rather than the posture's.
    """

    def execute(self, statement: str, parameters: object = ()) -> None:
        """Accept a statement and record nothing."""
        del statement, parameters

    def fetchone(self) -> tuple[object, ...] | None:
        """No first row."""
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        """No rows."""
        return []

    def close(self) -> None:
        """Release this cursor."""


class StubStore:
    """The calls the health route and a view make of a store, answering nothing."""

    role = "reader"

    def read(self, body: Callable[[Any], object]) -> object:
        """Run a read body against a cursor that answers no rows."""
        return body(EmptyCursor())

    def known_capabilities(self) -> CapabilityRecord:
        """The capability record, empty, so the health route reports it as unprobed."""
        return CapabilityRecord()

    def capabilities(self, *, refresh: bool = False) -> CapabilityRecord:
        """The capability record a view asks for, without probing anything."""
        del refresh
        return CapabilityRecord()


def console_for(*, demo_mode: bool) -> Console:
    """A console over the stand-in store, with credentials of this module's own."""
    settings = ConsoleSettings(
        host="127.0.0.1",
        port=8080,
        demo_mode=demo_mode,
        interface_spec_path=DOCUMENT_PATH,
        template_directory=REPOSITORY_ROOT / "web" / "templates",
        static_directory=REPOSITORY_ROOT / "web" / "static",
    )
    return Console(
        settings=settings,
        store=cast(Any, StubStore()),
        credential=Credential(
            auth.credential_record(CREDENTIAL, iterations=2),
            source_name="test",
            source=CredentialSource.ENVIRONMENT,
        ),
        session_key=Credential(
            SESSION_KEY, source_name="test", source=CredentialSource.ENVIRONMENT
        ),
        clock=lambda: NOW,
    )


@lru_cache(maxsize=2)
def application(demo_mode: bool) -> Starlette:
    """The ASGI object the deployed function serves, built once per mode.

    Built once rather than per example because it holds no per-request state: the
    store is a stand-in, the clock is fixed, and the demonstration principal is
    minted per process in the deployed configuration too.
    """
    return build_app(console_for(demo_mode=demo_mode))


def served_routes() -> tuple[RouteSpec, ...]:
    """Every named route the application object carries, as the table declares it.

    Read from `app.routes` rather than from the table, so this is an enumeration of
    the served set. The static asset mount carries no route posture and is not one.
    """
    found = tuple(
        route_named(str(route.name))
        for route in application(False).routes
        if isinstance(route, Route)
    )
    assert found, "the application object served no named route, so nothing was crossed"
    return found


SERVED_ROUTES: Final[tuple[RouteSpec, ...]] = served_routes()
DECLARED_NAMES: Final[frozenset[str]] = frozenset(spec.name for spec in SERVED_ROUTES)


# ---------------------------------------------------------------------------
# The parsed Interface_Specification, which the route set is crossed with
# ---------------------------------------------------------------------------

# The methods an operation may be keyed by, so a key that is not a method is not
# mistaken for one.
METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
)
CONSOLE_SURFACE: Final[str] = "console"


def specification_document() -> Mapping[str, Any]:
    """The tracked document, parsed once."""
    parsed = json.loads(DOCUMENT_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "the document's root is an object"
    return cast(Mapping[str, Any], parsed)


def described_console_operations() -> Mapping[str, tuple[str, Mapping[str, Any]]]:
    """Every console operation the document describes, keyed by the route it names."""
    found: dict[str, tuple[str, Mapping[str, Any]]] = {}
    paths = cast(Mapping[str, Any], specification_document()["paths"])
    for item in paths.values():
        for method, operation in cast(Mapping[str, Any], item).items():
            if method.lower() not in METHODS:
                continue
            described = cast(Mapping[str, Any], operation)
            if described.get("x-molt-surface") != CONSOLE_SURFACE:
                continue
            name = described.get("x-molt-route-name")
            assert isinstance(name, str) and name, "every console operation names its route"
            found[name] = (method.lower(), described)
    return found


DESCRIBED: Final[Mapping[str, tuple[str, Mapping[str, Any]]]] = described_console_operations()


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """One route, one context, and the operation the specification describes for it.

    Attributes:
        route: The declared route, taken from the served application object.
        context: Which of the three ways this route is served in this example.
        described: The specification operation naming this route, or None when the
            document describes none, which is itself the finding.
        unclassified: A route name the table classifies nowhere, drawn per example so
            the demonstration gate's answer to an unclassified name is a property
            rather than one hard-coded case.
    """

    route: RouteSpec
    context: Context
    described: tuple[str, Mapping[str, Any]] | None
    unclassified: str


def _unclassified_names() -> st.SearchStrategy[str]:
    """Names the route table declares nothing under, however they are spelled."""
    return st.text(min_size=1, max_size=32).filter(lambda name: name not in DECLARED_NAMES)


def route_requests() -> st.SearchStrategy[RouteRequest]:
    """The served route set crossed with the three contexts and with the document."""
    return st.builds(
        lambda route, context, unclassified: RouteRequest(
            route=route,
            context=context,
            described=DESCRIBED.get(route.name),
            unclassified=unclassified,
        ),
        route=st.sampled_from(SERVED_ROUTES),
        context=st.sampled_from(tuple(Context)),
        unclassified=_unclassified_names(),
    )


# ---------------------------------------------------------------------------
# Serving one request through the deployed path
# ---------------------------------------------------------------------------


def concrete_path(spec: RouteSpec) -> str:
    """The route's path with its parameters filled in, so the router matches it."""
    path = spec.path
    for parameter in PATH_PARAMETERS:
        path = path.replace("{" + parameter + "}", FILLED_IDENTIFIER)
    return path


def function_event(
    method: str,
    path: str,
    *,
    cookies: tuple[str, ...] = (),
    headers: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """One function invocation event, asking for an interchange body.

    The accept header matters: a browser navigating to an authenticated page is
    redirected to the login form, and this property is about the interchange answer,
    which is the status the design states.
    """
    carried = {"accept": "application/json"}
    if headers is not None:
        carried.update(headers)
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "headers": carried,
        "cookies": list(cookies),
        "requestContext": {"http": {"method": method, "path": path}},
        "body": "",
        "isBase64Encoded": False,
    }


def operator_session() -> tuple[str, str]:
    """A valid operator session cookie and the CSRF token it carries."""
    session, cookie = auth.issue(SESSION_KEY, now=NOW)
    return (f"{auth.COOKIE_NAME}={cookie}", session.csrf_token)


def serve(method: str, path: str, *, context: Context) -> LambdaResponse:
    """Serve one request against the application object, in one context."""
    app = application(context is Context.DEMONSTRATION)
    if context is Context.AUTHENTICATED:
        cookie, token = operator_session()
        return invoke(
            cast(Any, app),
            function_event(method, path, cookies=(cookie,), headers={"x-csrf-token": token}),
        )
    return invoke(cast(Any, app), function_event(method, path))


def body_text(answer: LambdaResponse) -> str:
    """The answer's body as text, whichever way the adapter carried it.

    A refusal and a rendered page arrive as text because both declare a textual
    content type. A redirect declares none and therefore arrives encoded, so the
    encoding is undone here rather than asserted away: the redirect is one of the
    answers this property judges.
    """
    raw = cast(str, answer["body"])
    if answer.get("isBase64Encoded") is True:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    return raw


def status_of(answer: LambdaResponse) -> int:
    """The answer's status."""
    return int(cast(int, answer["statusCode"]))


def is_demonstration_refusal(status: int, body: str) -> bool:
    """Whether one answer is the mode's own refusal, judged on the field that says so.

    Not a phrase found anywhere in the body. The mode banner names the mode on every
    page it renders, and a disabled control states the mode as its own explanation, so
    a page the mode served perfectly well contains the same words the refusal does.
    Reading the refusal's own field instead asks the answer what it is rather than what
    it mentions, which is the difference between the two.
    """
    if status != REFUSAL_STATUS:
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get(REFUSAL_FIELD) == REFUSAL_REASON


# ---------------------------------------------------------------------------
# The memory-content clause
# ---------------------------------------------------------------------------


def content_keys_in(value: JsonValue) -> set[str]:
    """Every content-bearing field name a document carries, at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for name, nested in value.items():
            if name.lower() in CONTENT_KEYS:
                found.add(name)
            found |= content_keys_in(nested)
    elif isinstance(value, list):
        for item in value:
            found |= content_keys_in(item)
    return found


def carried_content_keys(value: JsonValue, *, parent: str | None = None) -> set[str]:
    """Content keys a schema document carries as data rather than as a declaration.

    A declared field name is the specification's subject matter, so the keys of a
    schema property map and of the schema collection are declarations, as is the
    media-type container the interchange format spells with a colliding word.
    Everything else is data, and a content key there would be a row in a document
    that is meant to describe shapes.
    """
    found: set[str] = set()
    if isinstance(value, dict):
        for name, nested in value.items():
            declared = parent in DECLARATION_PARENTS or name.lower() in STRUCTURAL_KEYS
            if name.lower() in CONTENT_KEYS and not declared:
                found.add(name)
            found |= carried_content_keys(nested, parent=name)
    elif isinstance(value, list):
        for item in value:
            found |= carried_content_keys(item, parent=parent)
    return found


def parsed_body(answer: LambdaResponse) -> JsonValue:
    """The answer's body parsed as a document."""
    return cast(JsonValue, json.loads(body_text(answer)))


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(case=route_requests())
def test_the_served_route_set_keeps_its_posture_in_every_context(case: RouteRequest) -> None:
    route = case.route
    context = case.context
    event(f"context={context.value}")
    event(f"access={route.access.value}")
    event(f"disposition={route.demo.value}")
    event(f"mutation={route.mutation}")

    # Requirements 25.9 and 51.4: the document describes the route the application
    # serves, and it states the same posture the table gives it. Asserted here
    # against the served route rather than against the table, so a route the
    # application carries and the document does not describe is the finding.
    assert case.described is not None, (
        f"the application serves the route {route.name!r} and the Interface_"
        "Specification describes no such route"
    )
    described_method, operation = case.described
    assert described_method == route.method.lower(), route.name
    expected = NO_REQUIREMENT if route.public else SESSION_REQUIREMENT
    stated = operation["x-molt-authentication"]
    assert stated == expected, (
        f"the document states the authentication requirement {stated!r} for "
        f"{route.name!r} where the served table declares {expected!r}"
    )

    answer = serve(route.method, concrete_path(route), context=context)
    status = status_of(answer)
    body = body_text(answer)
    demonstration_refusal = is_demonstration_refusal(status, body)
    event(f"status={status}")

    if context is Context.UNAUTHENTICATED:
        _assert_anonymous_posture(route, status, demonstration_refusal)
    elif context is Context.AUTHENTICATED:
        # A route outside the allowlist refused the anonymous caller for want of a
        # session, so with one it must not refuse. A public route is excluded because
        # the credential exchange is public and refuses a wrong credential; what
        # *it* owes is session-independence, asserted in the anonymous context.
        if not route.public:
            assert status not in REFUSAL_STATUSES, (
                f"{route.name} refused a caller carrying a valid session and that "
                f"session's own CSRF token with {status}, so the refusal it gives an "
                "anonymous caller is not for want of a session"
            )
        assert not demonstration_refusal, (
            f"{route.name} answered the demonstration refusal outside demonstration mode"
        )
    else:
        _assert_demonstration_posture(route, status, body, demonstration_refusal)

    # Requirement 25.12: a name the table classifies nowhere is refused in
    # demonstration mode rather than allowed, so forgetting to classify a route
    # leaves a visibly refused route rather than a silently open one.
    assert verdict_for(case.unclassified, demo_mode=True) is Verdict.REFUSE, case.unclassified
    assert verdict_for(case.unclassified, demo_mode=False) is Verdict.ALLOW, case.unclassified
    assert not navigable(case.unclassified, demo_mode=True), case.unclassified

    _assert_public_bodies_carry_no_memory_content(context)


def _assert_anonymous_posture(route: RouteSpec, status: int, demonstration_refusal: bool) -> None:
    """The posture an anonymous caller meets outside demonstration mode.

    Requirement 30.5: outside the public allowlist the answer is 401 or 403. A public
    route is not asserted to answer 200, because the credential exchange is public
    and refuses a wrong credential; what is asserted of it is that its answer does
    not turn on holding a session, which is checked by serving it both ways.
    """
    assert not demonstration_refusal, (
        f"{route.name} answered the demonstration refusal outside demonstration mode"
    )
    if not route.public:
        assert status in REFUSAL_STATUSES, (
            f"{route.name} answered {status} to a caller carrying no session, and it is "
            "not in the public allowlist"
        )
        return
    with_session = status_of(
        serve(route.method, concrete_path(route), context=Context.AUTHENTICATED)
    )
    assert status == with_session, (
        f"the public route {route.name} answered {status} without a session and "
        f"{with_session} with one, so its answer turns on a session it declares it "
        "does not require"
    )


def _assert_demonstration_posture(
    route: RouteSpec, status: int, body: str, demonstration_refusal: bool
) -> None:
    """The containment demonstration mode obliges, on the asymmetry it is declared with.

    Requirement 25.12: the blocked set is refused with 403 and nothing else is
    refused by the mode. `HIDDEN` is a rendering decision rather than a refusal, so a
    hidden route stays reachable and is merely left out of the navigation.
    """
    blocked = route.demo is DemoDisposition.BLOCKED
    assert demonstration_refusal is blocked, (
        f"{route.name} is classified {route.demo.value} and the demonstration refusal "
        f"was {'given' if demonstration_refusal else 'not given'}, answering {status}"
    )
    if blocked:
        assert status == REFUSAL_STATUS, (
            f"the blocked route {route.name} answered {status} rather than {REFUSAL_STATUS}"
        )
        # The gate decides before dispatch, so the placeholder an unclaimed route
        # would answer with is unreachable and a view written later inherits the
        # refusal it was declared with.
        assert status != NOT_IMPLEMENTED_STATUS, route.name
        return
    if route.demo is DemoDisposition.HIDDEN:
        assert not navigable(route.name, demo_mode=True), route.name
        assert verdict_for(route.name, demo_mode=True) is Verdict.ALLOW, route.name
    if route.public:
        # The mode changes what the health body reports and what the navigation
        # offers. It changes no public route's reachability, and the credential
        # exchange refuses a caller presenting nothing in either configuration.
        unmoded = status_of(
            serve(route.method, concrete_path(route), context=Context.UNAUTHENTICATED)
        )
        assert status == unmoded, (
            f"the public route {route.name} answered {status} in demonstration mode and "
            f"{unmoded} outside it"
        )
        return
    if not route.mutation:
        assert status not in REFUSAL_STATUSES, (
            f"{route.name} answered {status} in demonstration mode although the table "
            "classifies it as reachable, so the mode refused more than its denylist"
        )
        return
    # A mutation route the table does not block still requires the session's own CSRF
    # token, and this request carries none. The refusal it gives is the token's rather
    # than the mode's, which is what the body distinguishes.
    if status == REFUSAL_STATUS:
        assert CSRF_MARKER in body, route.name


def _assert_public_bodies_carry_no_memory_content(context: Context) -> None:
    """Requirements 25.10 and 51.4, in whichever context this example is serving.

    Both bodies are checked in every context, because demonstration mode changes what
    the health route reports and a mode flag is no reason for a content key to appear.
    """
    health = serve("GET", HEALTH_PATH, context=context)
    assert status_of(health) == 200, status_of(health)
    carried = content_keys_in(parsed_body(health))
    assert carried == set(), f"the health body carries the content key(s) {sorted(carried)}"

    described = serve("GET", SPECIFICATION_PATH, context=context)
    assert status_of(described) == 200, status_of(described)
    outside = carried_content_keys(parsed_body(described))
    assert outside == set(), (
        "the Interface_Specification body carries the content key(s) "
        f"{sorted(outside)} outside a declaration position"
    )


def test_the_enumeration_reaches_every_route_the_application_serves() -> None:
    """The crossing is over the whole served set, so the property is not vacuous.

    A guard rather than a claim of its own: if the enumeration walked a subset, every
    assertion above would hold of a route set nobody deploys.
    """
    served = {str(route.name) for route in application(False).routes if isinstance(route, Route)}
    assert served == DECLARED_NAMES
    assert len(SERVED_ROUTES) == len(served)
    for name in served:
        assert name in DESCRIBED, name


def test_the_contexts_are_the_three_the_property_crosses() -> None:
    """Three contexts and no fourth, so a context added later is added deliberately."""
    assert tuple(Context) == (
        Context.AUTHENTICATED,
        Context.UNAUTHENTICATED,
        Context.DEMONSTRATION,
    )
    assert sorted(REFUSAL_STATUSES) == [401, REFUSAL_STATUS]
