"""The residue view and the erasure console: read-only search, durable progress.

Credential-free and cluster-free. The store is a stand-in that records every statement
it is given and answers each from a scripted table, so two claims are assertable
without a database: the residue view issues no statement that writes, and the stream
reads its progress from rows rather than from anything the start route left in memory.

The application is driven through the Lambda adapter, which is the deployed path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeVar, cast
from uuid import UUID, uuid4

from starlette.applications import Starlette

from molt.config.resolve import load_configuration
from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import build_app
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.console.routes import erasure, residue
from molt.console.routes.erasure_common import CONFIGURATION_STATE_KEY
from molt.console.routing import DemoDisposition, route_named
from molt.erase.residue import ResiduePolicy
from molt.store import Cursor
from molt.store.capability import CapabilityRecord

T = TypeVar("T")

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

CLIENT_ID: Final[UUID] = UUID(int=7)
CLIENT_SLUG: Final[str] = "a-tenant"
RUN_ID: Final[UUID] = UUID(int=11)

# The statement openers that would change stored state. The residue view must issue
# none of them, which is the claim the read-only test makes.
_WRITING: Final[tuple[str, ...]] = ("INSERT", "UPDATE", "DELETE", "UPSERT", "TRUNCATE")


class RecordingCursor:
    """A cursor that records every statement and answers from a scripted table."""

    def __init__(self, log: list[str], answers: Mapping[str, Sequence[tuple[object, ...]]]) -> None:
        self._log = log
        self._answers = answers
        self._rows: list[tuple[object, ...]] = []

    def execute(self, statement: str, parameters: object = ()) -> None:
        del parameters
        self._log.append(statement)
        self._rows = list(self._answers.get(_key_of(statement), ()))

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


def _key_of(statement: str) -> str:
    """Which scripted answer a statement asks for, keyed by a distinctive fragment."""
    for fragment in (
        "FROM client ORDER BY slug",
        "FROM erasure_run WHERE id",
        "FROM erasure_candidate WHERE run_id",
        "FROM residue_candidate WHERE run_id",
        "FROM disposition WHERE run_id",
    ):
        if fragment in statement:
            return fragment
    return "other"


class RecordingStore:
    """The store surface these two views use: `read`, and nothing that writes."""

    role = "eraser"

    def __init__(self, answers: Mapping[str, Sequence[tuple[object, ...]]] | None = None) -> None:
        self.statements: list[str] = []
        self._answers = {} if answers is None else answers

    def read(self, body: Callable[[Cursor], T]) -> T:
        return body(cast(Cursor, RecordingCursor(self.statements, self._answers)))

    def in_serializable(self, body: Callable[[Cursor], T], *, label: str = "") -> T:
        del label
        self.statements.append("TRANSACTION")
        return body(cast(Cursor, RecordingCursor(self.statements, self._answers)))

    def known_capabilities(self) -> CapabilityRecord:
        return CapabilityRecord()

    def capabilities(self, *, refresh: bool = False) -> CapabilityRecord:
        del refresh
        return CapabilityRecord()


def _console(store: RecordingStore, *, demo_mode: bool = False) -> Console:
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
        store=cast(Any, store),
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


class RecordingLauncher:
    """A launcher that records the plan it was handed and performs no run."""

    def __init__(self) -> None:
        self.plans: list[erasure.RunPlan] = []

    def __call__(self, plan: erasure.RunPlan) -> None:
        self.plans.append(plan)


def _app(
    store: RecordingStore,
    *,
    demo_mode: bool = False,
    launcher: RecordingLauncher | None = None,
) -> Starlette:
    app = build_app(_console(store, demo_mode=demo_mode))
    setattr(app.state, CONFIGURATION_STATE_KEY, load_configuration(environ={}))
    if launcher is not None:
        setattr(app.state, erasure.LAUNCHER_STATE_KEY, launcher)
    return app


def _event(
    method: str,
    path: str,
    *,
    query: str = "",
    body: str = "",
    headers: Mapping[str, str] | None = None,
    cookies: tuple[str, ...] = (),
) -> dict[str, object]:
    carried = {"accept": "application/json"}
    if headers is not None:
        carried.update(headers)
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": query,
        "headers": carried,
        "cookies": list(cookies),
        "requestContext": {"http": {"method": method, "path": path}},
        "body": body,
        "isBase64Encoded": False,
    }


def _session() -> tuple[str, str]:
    """One issued session as its cookie and its CSRF token."""
    session, cookie = auth.issue(SESSION_KEY, now=NOW)
    return (f"{auth.COOKIE_NAME}={cookie}", session.csrf_token)


def _fleet_answers() -> dict[str, Sequence[tuple[object, ...]]]:
    return {"FROM client ORDER BY slug": ((CLIENT_ID, CLIENT_SLUG, "A Tenant"),)}


def _run_answers(status: str, phase: str) -> dict[str, Sequence[tuple[object, ...]]]:
    answers = _fleet_answers()
    answers["FROM erasure_run WHERE id"] = (
        (RUN_ID, CLIENT_ID, "an-operator", False, status, phase, None, 0.2, 0.45),
    )
    answers["FROM erasure_candidate WHERE run_id"] = ((4,),)
    answers["FROM residue_candidate WHERE run_id"] = ((3, 2, 1),)
    answers["FROM disposition WHERE run_id"] = (("hard_delete", 2), ("retained", 1))
    return answers


# -- the routes are claimed, not added ------------------------------------


def test_the_three_declared_routes_are_claimed_by_these_view_modules() -> None:
    from molt.console.routing import HANDLERS

    for name in ("residue", "erase_console", "erase_start", "erase_stream"):
        assert name in HANDLERS, name
    assert route_named("erase_start").demo is DemoDisposition.BLOCKED
    assert route_named("erase_start").mutation
    assert not route_named("residue").mutation
    assert not route_named("erase_stream").mutation


# -- the residue view mutates nothing -------------------------------------


def test_the_residue_view_issues_no_statement_that_writes() -> None:
    store = RecordingStore(_fleet_answers())
    cookie, _ = _session()
    answer = invoke(
        _app(store),
        _event(
            "GET",
            "/residue",
            query=f"client={CLIENT_SLUG}",
            headers={"accept": "text/html"},
            cookies=(cookie,),
        ),
    )
    assert answer["statusCode"] == 200
    assert store.statements
    for statement in store.statements:
        assert not statement.lstrip().upper().startswith(_WRITING), statement
    assert "TRANSACTION" not in store.statements


def test_the_residue_view_renders_its_form_with_labelled_native_controls() -> None:
    store = RecordingStore(_fleet_answers())
    cookie, _ = _session()
    answer = invoke(
        _app(store),
        _event("GET", "/residue", headers={"accept": "text/html"}, cookies=(cookie,)),
    )
    body = cast(str, answer["body"])
    assert '<select id="residue-client"' in body
    assert 'for="residue-client"' in body
    assert 'type="number"' in body
    assert "<caption>" not in body or "Residue candidates" in body


def test_the_residue_view_refuses_a_slug_naming_no_client_without_searching() -> None:
    store = RecordingStore(_fleet_answers())
    view = residue.residue_view(
        cast(Any, store),
        _policy(),
        {residue.CLIENT_FIELD: "a-slug-nothing-names"},
    )
    assert not view.searched
    assert view.refusal is not None


def test_the_residue_view_refuses_thresholds_describing_no_band() -> None:
    store = RecordingStore(_fleet_answers())
    view = residue.residue_view(
        cast(Any, store),
        _policy(),
        {
            residue.CLIENT_FIELD: CLIENT_SLUG,
            residue.AUTO_INCLUDE_FIELD: "0.9",
            residue.REVIEW_FIELD: "0.1",
        },
    )
    assert not view.searched
    assert view.refusal is not None


def _policy() -> ResiduePolicy:
    return ResiduePolicy.from_configuration(load_configuration(environ={}))


# -- starting a run ------------------------------------------------------


def _start(
    store: RecordingStore,
    *,
    demo_mode: bool = False,
    launcher: RecordingLauncher | None = None,
    form: str = "",
    token: str | None = None,
    cookie: str | None = None,
) -> LambdaResponse:
    held_cookie, held_token = _session()
    headers = {"content-type": "application/x-www-form-urlencoded"}
    resolved = held_token if token is None else token
    if resolved:
        headers["x-csrf-token"] = resolved
    return invoke(
        _app(store, demo_mode=demo_mode, launcher=launcher),
        _event(
            "POST",
            "/erase",
            body=form,
            headers=headers,
            cookies=(held_cookie if cookie is None else cookie,),
        ),
    )


def test_starting_a_run_records_the_attempt_and_returns_its_identifier() -> None:
    store = RecordingStore(_fleet_answers())
    launcher = RecordingLauncher()
    answer = _start(
        store,
        launcher=launcher,
        form=(
            f"client={CLIENT_SLUG}&requester=an-operator&justification=a-request"
            "&dry_run=on&auto_include_threshold=0.3&review_threshold=0.5"
        ),
    )
    assert answer["statusCode"] == 202
    assert len(launcher.plans) == 1
    plan = launcher.plans[0]
    assert plan.request.client_id == CLIENT_ID
    assert plan.request.dry_run is True
    assert plan.auto_include_threshold == 0.3
    assert plan.review_threshold == 0.5
    body = cast(str, answer["body"])
    assert str(plan.attempt) in body
    assert f"/erase/{plan.attempt}/stream" in body
    # The identifier answered is the attempt's idempotency key in its other form, so
    # the stream reads the run the engine claims under that key.
    assert plan.request.idempotency_key == plan.attempt.hex


def test_the_dry_run_toggle_is_absent_consent_rather_than_a_default() -> None:
    store = RecordingStore(_fleet_answers())
    launcher = RecordingLauncher()
    _start(
        store,
        launcher=launcher,
        form=f"client={CLIENT_SLUG}&requester=an-operator&justification=a-request",
    )
    assert launcher.plans[0].request.dry_run is False


def test_starting_a_run_is_refused_in_read_only_demonstration_mode() -> None:
    store = RecordingStore(_fleet_answers())
    launcher = RecordingLauncher()
    answer = _start(
        store,
        demo_mode=True,
        launcher=launcher,
        form=f"client={CLIENT_SLUG}&requester=an-operator&justification=a-request",
    )
    assert answer["statusCode"] == 403
    assert not launcher.plans


def test_starting_a_run_is_refused_without_the_sessions_own_csrf_token() -> None:
    store = RecordingStore(_fleet_answers())
    launcher = RecordingLauncher()
    answer = _start(
        store,
        launcher=launcher,
        token="",
        form=f"client={CLIENT_SLUG}&requester=an-operator&justification=a-request",
    )
    assert answer["statusCode"] == 403
    assert not launcher.plans


def test_an_incomplete_submission_is_refused_and_starts_nothing() -> None:
    store = RecordingStore(_fleet_answers())
    launcher = RecordingLauncher()
    answer = _start(store, launcher=launcher, form=f"client={CLIENT_SLUG}")
    assert answer["statusCode"] == 400
    assert not launcher.plans


# -- reading progress from the durable rows ------------------------------


def test_progress_is_read_from_the_run_row_and_the_committed_evidence() -> None:
    store = RecordingStore(_run_answers("running", "disposition"))
    progress = erasure.progress_of(cast(Any, store), RUN_ID)
    assert progress.recorded
    assert progress.run_id == RUN_ID
    assert progress.phase == "disposition"
    assert progress.candidates == 4
    assert progress.residue_candidates == 3
    assert progress.residue_included == 2
    assert progress.dispositions == {"hard_delete": 2, "retained": 1}
    assert not progress.terminal
    for statement in store.statements:
        assert not statement.lstrip().upper().startswith(_WRITING), statement


def test_an_attempt_with_no_run_row_yet_streams_the_queued_state() -> None:
    store = RecordingStore(_fleet_answers())
    progress = erasure.progress_of(cast(Any, store), uuid4())
    assert not progress.recorded
    assert progress.phase is None
    rendered = "".join(erasure.stream_body(progress))
    assert f"event: {erasure.QUEUED_EVENT}" in rendered
    assert f"event: {erasure.OUTCOME_EVENT}" not in rendered


def test_a_terminal_run_ends_the_stream_with_an_explicit_outcome_event() -> None:
    store = RecordingStore(_run_answers("completed", "done"))
    progress = erasure.progress_of(cast(Any, store), RUN_ID)
    assert progress.terminal
    rendered = "".join(erasure.stream_body(progress))
    assert f"event: {erasure.PHASE_EVENT}" in rendered
    assert f"event: {erasure.OUTCOME_EVENT}" in rendered
    assert "completed" in rendered
    assert f"retry: {erasure.RECONNECT_MILLISECONDS}" in rendered


def test_the_stream_route_serves_events_and_names_no_memory_content() -> None:
    store = RecordingStore(_run_answers("running", "residue"))
    cookie, _ = _session()
    answer = invoke(
        _app(store),
        _event("GET", f"/erase/{RUN_ID}/stream", cookies=(cookie,)),
    )
    assert answer["statusCode"] == 200
    headers = cast(Mapping[str, str], answer["headers"])
    assert erasure.EVENT_STREAM_MEDIA_TYPE in headers["content-type"]
    body = cast(str, answer["body"])
    assert '"phase":"residue"' in body.replace(" ", "")
    for forbidden in ("text_body", 'body":', "payload", "vector"):
        assert forbidden not in body


def test_the_stream_route_refuses_an_identifier_that_is_not_one() -> None:
    store = RecordingStore(_run_answers("running", "sweep"))
    cookie, _ = _session()
    answer = invoke(
        _app(store),
        _event("GET", "/erase/not-an-identifier/stream", cookies=(cookie,)),
    )
    assert answer["statusCode"] == 400


# -- the console page ----------------------------------------------------


def test_the_console_renders_native_labelled_controls_and_a_live_region() -> None:
    store = RecordingStore(_fleet_answers())
    cookie, _ = _session()
    answer = invoke(
        _app(store),
        _event("GET", "/erase", headers={"accept": "text/html"}, cookies=(cookie,)),
    )
    assert answer["statusCode"] == 200
    body = cast(str, answer["body"])
    assert "<select" in body
    assert 'id="erase-client"' in body
    assert 'for="erase-client"' in body
    assert 'type="number"' in body
    assert 'type="checkbox"' in body
    assert 'aria-live="polite"' in body
    assert "<button" in body
    assert 'name="csrf_token"' in body


def test_the_console_renders_blocked_controls_disabled_with_an_explanation() -> None:
    store = RecordingStore(_fleet_answers())
    cookie, _ = _session()
    answer = invoke(
        _app(store, demo_mode=True),
        _event("GET", "/erase", headers={"accept": "text/html"}, cookies=(cookie,)),
    )
    body = cast(str, answer["body"])
    assert "disabled" in body
    assert 'aria-describedby="erase-blocked"' in body
    assert "demonstration mode" in body
