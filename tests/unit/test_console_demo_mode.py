"""Read-only demonstration mode: the gate, its status, and what it leaves reachable.

Credential-free and database-free, like the skeleton's own suite: the store is a
stand-in and the application is driven through the Lambda adapter, which is the path
the deployed function takes.

The assertions are deliberately about the *set* of routes rather than about a chosen
few, because the property demonstration mode has to hold is a property of the table:
every route the table marks blocked is refused, and every route it does not mark
blocked stays reachable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import build_app
from molt.console.demo import (
    BLOCKED_EXPLANATION,
    DEMO_SUBJECT,
    SEEDED_CLIENT_SLUGS,
    DemonstrationMiddleware,
    DemoPrincipal,
    Verdict,
    control_disabled,
    navigable,
    refused_route_names,
    verdict_for,
)
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.console.routing import (
    MUTATION_ROUTE_NAMES,
    ROUTE_TABLE,
    DemoDisposition,
    RouteSpec,
)
from molt.store.capability import CapabilityRecord

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)


class EmptyCursor:
    """A cursor that answers every statement with no rows at all.

    The gate under test decides before any handler runs, so what a view would have
    read is beside the point here. What matters is that a read behaves like a read: a
    store whose `read` ignored its body and answered `None` would hand every view a
    `None` where it expected rows, and the resulting failure would be the stand-in's
    rather than the gate's.
    """

    def execute(self, statement: str, parameters: object = ()) -> None:
        del statement, parameters

    def fetchone(self) -> tuple[object, ...] | None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def close(self) -> None:
        return None


class StubStore:
    """The calls a view or the health route makes of a store, answering nothing."""

    role = "reader"

    def read(self, body: Callable[[Any], object]) -> object:
        return body(EmptyCursor())

    def known_capabilities(self) -> CapabilityRecord:
        return CapabilityRecord()

    def capabilities(self, *, refresh: bool = False) -> CapabilityRecord:
        del refresh
        return CapabilityRecord()


def _console(*, demo_mode: bool) -> Console:
    root = Path(__file__).resolve().parents[2]
    settings = ConsoleSettings(
        host="127.0.0.1",
        port=8080,
        demo_mode=demo_mode,
        interface_spec_path=root / "docs" / "interface.json",
        template_directory=root / "web" / "templates",
        static_directory=root / "web" / "static",
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


def _event(
    method: str,
    path: str,
    *,
    cookies: tuple[str, ...] = (),
    headers: Mapping[str, str] | None = None,
) -> dict[str, object]:
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


def _concrete(spec: RouteSpec) -> str:
    path = spec.path
    for parameter in ("session_id", "artifact_id", "run_id", "approval_id"):
        path = path.replace("{" + parameter + "}", "00000000-0000-0000-0000-000000000000")
    return path


def _serve(spec: RouteSpec, *, demo_mode: bool, **kwargs: object) -> LambdaResponse:
    app = build_app(_console(demo_mode=demo_mode))
    return invoke(cast(Any, app), _event(spec.method, _concrete(spec), **cast(Any, kwargs)))


def _cookie() -> str:
    _, value = auth.issue(SESSION_KEY, now=NOW)
    return f"{auth.COOKIE_NAME}={value}"


def _named(name: str) -> RouteSpec:
    return next(spec for spec in ROUTE_TABLE if spec.name == name)


# -- the decision, as a function of the route name alone --------------------


def test_outside_demonstration_mode_the_gate_decides_nothing() -> None:
    for spec in ROUTE_TABLE:
        assert verdict_for(spec.name, demo_mode=False) is Verdict.ALLOW


def test_every_blocked_route_is_refused_and_no_other_route_is() -> None:
    for spec in ROUTE_TABLE:
        expected = Verdict.REFUSE if spec.demo is DemoDisposition.BLOCKED else Verdict.ALLOW
        assert verdict_for(spec.name, demo_mode=True) is expected, spec.name
    assert refused_route_names() == MUTATION_ROUTE_NAMES


def test_a_route_the_table_does_not_classify_is_refused_rather_than_allowed() -> None:
    assert verdict_for("a-route-added-without-a-classification", demo_mode=True) is Verdict.REFUSE
    assert verdict_for("a-route-added-without-a-classification", demo_mode=False) is Verdict.ALLOW


# -- the served posture ----------------------------------------------------


def test_every_blocked_route_answers_403_in_demonstration_mode() -> None:
    for spec in ROUTE_TABLE:
        if spec.demo is not DemoDisposition.BLOCKED:
            continue
        answer = _serve(spec, demo_mode=True, cookies=(_cookie(),))
        assert answer["statusCode"] == 403, spec.name
        assert "demonstration" in cast(str, answer["body"])


def test_a_blocked_route_is_refused_before_authentication_and_without_a_session() -> None:
    # The gate is outermost, so the refusal does not depend on holding a session:
    # the same 403 is given to a caller carrying nothing at all.
    answer = _serve(_named("erase_start"), demo_mode=True)
    assert answer["statusCode"] == 403


def test_a_blocked_route_with_no_written_handler_is_still_refused() -> None:
    # `erase_start` and `approval_resolve` have no claimed view yet, so outside
    # demonstration mode they answer 501 from the placeholder. That the demonstration
    # answer is 403 rather than 501 is the whole point: the declaration is enforced
    # before dispatch, so a view written later inherits the refusal.
    from molt.console.routing import HANDLERS

    spec = _named("approval_resolve")
    assert spec.name not in HANDLERS
    session, cookie = auth.issue(SESSION_KEY, now=NOW)
    permitted = _serve(
        spec,
        demo_mode=False,
        cookies=(f"{auth.COOKIE_NAME}={cookie}",),
        headers={"x-csrf-token": session.csrf_token},
    )
    assert permitted["statusCode"] == 501
    assert _serve(spec, demo_mode=True)["statusCode"] == 403


def test_a_read_route_sharing_a_path_with_a_blocked_one_is_not_refused() -> None:
    # GET /erase and POST /erase share a path. The gate decides on the full match, so
    # the erasure console stays reachable while starting a run does not.
    console = _serve(_named("erase_console"), demo_mode=True)
    assert console["statusCode"] != 403
    assert _serve(_named("erase_start"), demo_mode=True)["statusCode"] == 403


def test_a_wrong_method_request_to_a_blocked_path_is_refused_too() -> None:
    app = build_app(_console(demo_mode=True))
    path = "/approvals/00000000-0000-0000-0000-000000000000"
    answer = invoke(cast(Any, app), _event("PUT", path))
    assert answer["statusCode"] == 403


def test_the_read_only_routes_stay_reachable_as_the_anonymous_principal() -> None:
    # No cookie is presented. The gate establishes the demonstration principal, so
    # the authenticated read routes answer rather than refusing for want of a session.
    for spec in ROUTE_TABLE:
        if spec.demo is not DemoDisposition.READ_ONLY or spec.mutation:
            continue
        answer = _serve(spec, demo_mode=True)
        assert answer["statusCode"] not in (401, 403), spec.name


def test_the_same_routes_refuse_an_anonymous_caller_outside_demonstration_mode() -> None:
    for spec in ROUTE_TABLE:
        if spec.demo is not DemoDisposition.READ_ONLY or spec.mutation:
            continue
        assert _serve(spec, demo_mode=False)["statusCode"] == 401, spec.name


def test_the_health_route_reports_the_mode_and_the_gate_lets_it_through() -> None:
    answer = _serve(_named("health"), demo_mode=True)
    assert answer["statusCode"] == 200
    assert '"demo_mode":true' in cast(str, answer["body"])


def test_an_undeclared_path_is_still_a_404_rather_than_a_refusal() -> None:
    app = build_app(_console(demo_mode=True))
    answer = invoke(cast(Any, app), _event("GET", "/a-path-the-table-declares-no-route-for"))
    assert answer["statusCode"] == 404


def test_an_operator_session_presented_in_demonstration_mode_is_kept() -> None:
    # The gate does not replace a session a caller already holds, and it does not
    # widen what a held session reaches: the blocked routes are refused either way.
    # Asserted against a read route rather than an unclaimed one, so the case does not
    # expire when that route's view is written.
    answer = _serve(_named("tiers"), demo_mode=True, cookies=(_cookie(),))
    assert answer["statusCode"] not in (401, 403)
    assert _serve(_named("erase_start"), demo_mode=True, cookies=(_cookie(),))["statusCode"] == 403


# -- the principal ---------------------------------------------------------


def test_the_principal_is_anonymous_read_only_and_restricted_to_the_seeded_clients() -> None:
    principal = DemoPrincipal()
    assert principal.subject == DEMO_SUBJECT
    assert principal.subject != auth.OPERATOR_SUBJECT
    assert principal.read_only
    assert principal.clients == SEEDED_CLIENT_SLUGS
    assert principal.clients
    for slug in SEEDED_CLIENT_SLUGS:
        assert principal.may_read(slug)
    assert not principal.may_read("a-client-a-real-engagement-created")


def test_the_demonstration_session_is_stable_within_one_process() -> None:
    # A form rendered under one request is submitted under the next, so the CSRF
    # token the principal carries has to survive between them.
    gate = DemonstrationMiddleware(cast(Any, None), console=_console(demo_mode=True))
    first, _ = gate.principal_session()
    second, _ = gate.principal_session()
    assert first.csrf_token == second.csrf_token
    assert first.subject == DEMO_SUBJECT


# -- what the templates render ---------------------------------------------


def test_a_blocked_control_is_disabled_with_an_explanation_rather_than_hidden() -> None:
    for name in MUTATION_ROUTE_NAMES:
        assert control_disabled(name, demo_mode=True), name
        assert not control_disabled(name, demo_mode=False), name
    assert not control_disabled("fleet", demo_mode=True)
    assert "demonstration" in BLOCKED_EXPLANATION


def test_hidden_routes_are_left_out_of_the_navigation_and_not_refused() -> None:
    for spec in ROUTE_TABLE:
        if spec.demo is not DemoDisposition.HIDDEN:
            continue
        assert not navigable(spec.name, demo_mode=True), spec.name
        assert navigable(spec.name, demo_mode=False), spec.name
        assert verdict_for(spec.name, demo_mode=True) is Verdict.ALLOW, spec.name
    assert navigable("fleet", demo_mode=True)
