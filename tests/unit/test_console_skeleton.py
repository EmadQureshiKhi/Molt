"""The console skeleton: the route table's posture, authentication, and the adapter.

Credential-free and database-free. The store is a stand-in whose reachability probe
and capability read are the two calls the health route makes, and the application is
driven through the Lambda adapter, which exercises the deployed path and the route
table in one pass.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pytest
from starlette.routing import Route

from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import build_app, build_routes
from molt.console.deps import Console, ConsoleSettings
from molt.console.handler import CHECKPOINT_ENTRY_POINT, ENTRY_POINT_KEY, UnknownEntryPointError
from molt.console.handler import handler as function_handler
from molt.console.lambda_adapter import LambdaResponse, invoke, scope_of
from molt.console.routing import (
    MUTATION_ROUTE_NAMES,
    PUBLIC_ROUTE_NAMES,
    ROUTE_TABLE,
    Access,
    DemoDisposition,
    RouteSpec,
    RouteTableError,
    authenticated_routes,
    mutation_routes,
    route_named,
)
from molt.store.capability import CapabilityRecord

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
# A fixed instant, derived from a count of seconds rather than written as a
# calendar reading, so the module carries no date literal.
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)


class StubStore:
    """The two things the health route asks of a store, and nothing else."""

    role = "reader"

    def __init__(self, *, reachable: bool = True) -> None:
        self._reachable = reachable
        self.probes = 0

    def read(self, body: object) -> object:  # noqa: ARG002
        self.probes += 1
        if not self._reachable:
            raise RuntimeError("the cluster did not answer")
        return None

    def known_capabilities(self) -> CapabilityRecord:
        return CapabilityRecord()


def _console(
    *, demo_mode: bool = False, reachable: bool = True, spec: Path | None = None
) -> Console:
    root = Path(__file__).resolve().parents[2]
    settings = ConsoleSettings(
        host="127.0.0.1",
        port=8080,
        demo_mode=demo_mode,
        interface_spec_path=root / "docs" / "interface.json" if spec is None else spec,
        template_directory=root / "web" / "templates",
        static_directory=root / "web" / "static",
    )
    return Console(
        settings=settings,
        store=cast(Any, StubStore(reachable=reachable)),
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
    body: str = "",
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
        "body": body,
        "isBase64Encoded": False,
    }


def _serve(method: str, path: str, **kwargs: object) -> LambdaResponse:
    app = build_app(_console())
    return invoke(cast(Any, app), _event(method, path, **cast(Any, kwargs)))


def _cookie() -> str:
    _, value = auth.issue(SESSION_KEY, now=NOW)
    return f"{auth.COOKIE_NAME}={value}"


# -- the route table -------------------------------------------------------


def test_every_route_outside_the_allowlist_requires_a_session() -> None:
    for spec in ROUTE_TABLE:
        if spec.name in PUBLIC_ROUTE_NAMES:
            assert spec.access is Access.PUBLIC
        else:
            assert spec.authenticated, spec.name


def test_no_public_route_mutates() -> None:
    assert not [spec for spec in ROUTE_TABLE if spec.public and spec.mutation]


def test_the_table_and_the_built_routes_name_the_same_set() -> None:
    built = {route.name for route in build_routes() if isinstance(route, Route)}
    assert built == {spec.name for spec in ROUTE_TABLE}


def test_every_route_name_is_unique_and_resolvable() -> None:
    names = [spec.name for spec in ROUTE_TABLE]
    assert len(names) == len(set(names))
    for name in names:
        assert route_named(name).name == name


def test_an_undeclared_route_name_is_refused() -> None:
    with pytest.raises(RouteTableError):
        route_named("a-route-nothing-declares")


def test_the_demonstration_denylist_is_the_blocked_set() -> None:
    assert {
        spec.name for spec in ROUTE_TABLE if spec.demo is DemoDisposition.BLOCKED
    } == MUTATION_ROUTE_NAMES
    assert {spec.name for spec in mutation_routes()} >= MUTATION_ROUTE_NAMES


def test_the_authenticated_set_is_the_complement_of_the_allowlist() -> None:
    assert {spec.name for spec in authenticated_routes()} == {
        spec.name for spec in ROUTE_TABLE
    } - PUBLIC_ROUTE_NAMES


# -- authentication --------------------------------------------------------


def test_a_credential_verifies_against_its_record_and_a_wrong_one_does_not() -> None:
    record = auth.credential_record(CREDENTIAL, iterations=2)
    assert auth.verify_credential(CREDENTIAL, record)
    assert not auth.verify_credential(CREDENTIAL + "x", record)


def test_an_unreadable_credential_record_lets_nobody_in() -> None:
    with pytest.raises(auth.CredentialRecordError):
        auth.verify_credential(CREDENTIAL, "not-a-record")


def test_an_issued_cookie_verifies_and_carries_a_csrf_token() -> None:
    session, cookie = auth.issue(SESSION_KEY, now=NOW)
    read = auth.verify_cookie(cookie, SESSION_KEY, now=NOW)
    assert read is not None
    assert read.csrf_token == session.csrf_token
    assert auth.csrf_accepted(read, session.csrf_token)
    assert not auth.csrf_accepted(read, "another-token")


def test_a_cookie_signed_with_another_key_or_edited_names_no_session() -> None:
    _, cookie = auth.issue(SESSION_KEY, now=NOW)
    assert auth.verify_cookie(cookie, "a-different-key", now=NOW) is None
    payload, _, signature = cookie.partition(".")
    assert auth.verify_cookie(f"{payload}x.{signature}", SESSION_KEY, now=NOW) is None
    assert auth.verify_cookie(None, SESSION_KEY, now=NOW) is None


def test_the_expiry_is_absolute() -> None:
    _, cookie = auth.issue(SESSION_KEY, now=NOW, lifetime=timedelta(minutes=5))
    assert auth.verify_cookie(cookie, SESSION_KEY, now=NOW + timedelta(minutes=4)) is not None
    assert auth.verify_cookie(cookie, SESSION_KEY, now=NOW + timedelta(minutes=6)) is None


# -- the served posture ----------------------------------------------------


def test_the_health_route_answers_without_a_session_and_names_no_memory_content() -> None:
    answer = _serve("GET", "/health")
    assert answer["statusCode"] == 200
    body = cast(str, answer["body"])
    assert '"status":"ok"' in body
    assert '"demo_mode":false' in body
    assert "capabilities" in body
    for forbidden in ("event", "artifact", "text_body", "payload", "client_id"):
        assert forbidden not in body


def test_every_authenticated_route_refuses_a_caller_carrying_no_cookie() -> None:
    app = build_app(_console())
    for spec in authenticated_routes():
        answer = invoke(cast(Any, app), _event(spec.method, _concrete(spec)))
        assert answer["statusCode"] == 401, spec.name


def _concrete(spec: RouteSpec) -> str:
    """The route's path with its parameters filled in, so the router matches it."""
    path = spec.path
    for parameter in ("session_id", "artifact_id", "run_id", "approval_id"):
        path = path.replace("{" + parameter + "}", "00000000-0000-0000-0000-000000000000")
    return path


def _unclaimed(*, mutation: bool) -> RouteSpec | None:
    """A declared route no view module has claimed, or None once every view is written.

    Derived rather than named, because the whole subject of the two cases below is a
    route whose handler does not exist yet, and naming one would make the case expire
    the moment that view was written. Once every declared route is claimed the
    placeholder has nothing left to answer for and the case reports that instead.
    """
    from molt.console.routing import HANDLERS

    for spec in ROUTE_TABLE:
        if spec.name not in HANDLERS and spec.mutation is mutation:
            return spec
    return None


def test_a_session_reaches_a_declared_route_whose_view_is_not_yet_written() -> None:
    spec = _unclaimed(mutation=False)
    if spec is None:
        pytest.skip("every declared read route has a claimed view, so nothing answers 501")
    answer = _serve(spec.method, _concrete(spec), cookies=(_cookie(),))
    assert answer["statusCode"] == 501, spec.name


def test_a_mutation_route_with_a_session_and_no_csrf_token_is_refused() -> None:
    answer = _serve("POST", "/erase", cookies=(_cookie(),))
    assert answer["statusCode"] == 403


def test_a_mutation_route_with_the_session_token_reaches_its_handler() -> None:
    """The session's own token gets past the CSRF check, whatever the handler answers.

    Asserted against an unclaimed mutation route, so what is observed is the middleware
    admitting the request rather than a particular view's reply: the placeholder's 501
    is proof the request reached dispatch.
    """
    spec = _unclaimed(mutation=True)
    if spec is None:
        pytest.skip("every declared mutation route has a claimed view, so nothing answers 501")
    session, cookie = auth.issue(SESSION_KEY, now=NOW)
    answer = _serve(
        spec.method,
        _concrete(spec),
        cookies=(f"{auth.COOKIE_NAME}={cookie}",),
        headers={"x-csrf-token": session.csrf_token},
    )
    assert answer["statusCode"] == 501, spec.name


def test_the_login_route_issues_a_hardened_cookie_and_refuses_a_wrong_credential() -> None:
    form = {"content-type": "application/x-www-form-urlencoded"}
    refused = _serve("POST", "/login", body=f"{auth.CREDENTIAL_FIELD}=wrong", headers=form)
    assert refused["statusCode"] == 401
    assert not cast(list[str], refused["cookies"])

    accepted = _serve("POST", "/login", body=f"{auth.CREDENTIAL_FIELD}={CREDENTIAL}", headers=form)
    assert accepted["statusCode"] == 303
    cookies = cast(list[str], accepted["cookies"])
    assert len(cookies) == 1
    rendered = cookies[0]
    assert rendered.startswith(f"{auth.COOKIE_NAME}=")
    assert "HttpOnly" in rendered
    assert "Secure" in rendered
    assert "SameSite=strict" in rendered.replace("SameSite=Strict", "SameSite=strict")


def test_an_undeclared_path_is_a_refusal_naming_nothing_else() -> None:
    answer = _serve("GET", "/a-path-the-table-declares-no-route-for")
    assert answer["statusCode"] == 404


def test_the_specification_route_is_public_and_reports_absence_rather_than_a_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "absent.json"
    app = build_app(_console(spec=missing))
    answer = invoke(cast(Any, app), _event("GET", "/spec"))
    assert answer["statusCode"] == 503
    assert str(missing) not in cast(str, answer["body"])

    present = tmp_path / "interface.json"
    present.write_text('{"openapi": "3.1.0"}', encoding="utf-8")
    served = invoke(cast(Any, build_app(_console(spec=present))), _event("GET", "/spec"))
    assert served["statusCode"] == 200
    assert cast(str, served["body"]) == '{"openapi": "3.1.0"}'


def test_an_unreachable_cluster_is_degraded_rather_than_a_failure() -> None:
    app = build_app(_console(reachable=False))
    answer = invoke(cast(Any, app), _event("GET", "/health"))
    assert answer["statusCode"] == 200
    assert '"status":"degraded"' in cast(str, answer["body"])


# -- the adapter -----------------------------------------------------------


def test_the_scope_carries_the_method_path_query_and_rejoined_cookies() -> None:
    event = _event("post", "/erase", cookies=("a=1", "b=2"))
    event["rawQueryString"] = "client=one"
    scope = scope_of(event)
    assert scope["method"] == "POST"
    assert scope["path"] == "/erase"
    assert scope["query_string"] == b"client=one"
    headers = cast(list[tuple[bytes, bytes]], scope["headers"])
    assert (b"cookie", b"a=1; b=2") in headers


def test_the_adapter_returns_a_base64_body_for_a_non_text_content_type() -> None:
    async def app(
        scope: Mapping[str, object],
        receive: object,
        send: Callable[[Mapping[str, object]], Awaitable[None]],
    ) -> None:
        assert scope["type"] == "http"
        assert receive is not None
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/octet-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b"\x00\x01", "more_body": False})

    answer = invoke(cast(Any, app), _event("GET", "/"))
    assert answer["isBase64Encoded"] is True
    assert answer["body"] == "AAE="


# -- the function entry points --------------------------------------------


def test_the_scheduled_entry_point_is_dispatched_by_name() -> None:
    answer = function_handler({ENTRY_POINT_KEY: CHECKPOINT_ENTRY_POINT})
    assert answer["entry_point"] == CHECKPOINT_ENTRY_POINT


def test_an_unknown_entry_point_is_refused_by_name() -> None:
    with pytest.raises(UnknownEntryPointError):
        function_handler({ENTRY_POINT_KEY: "something-this-function-does-not-host"})
