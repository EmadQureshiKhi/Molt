"""The retention view and the approval queue pair, driven credential-free and cluster-free.

The store is a stand-in answering each statement these two view modules send from rows
held in memory, keyed by statement text, and recording what each statement was bound
with. That is what makes the tenancy claims assertable without a cluster: the queue read
carries the permitted Client identifiers as bound values, a report line describing a
Client the roster does not hold is never rendered, and a resolution naming an entry
outside the roster writes nothing.

The application is driven through the Lambda adapter, which is the deployed path, so the
route table, both middlewares, and the templates are all exercised as they are served.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TypeVar, cast
from uuid import UUID

from molt.config.secrets import Credential, CredentialSource
from molt.console import auth, demo
from molt.console.app import build_app
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.console.routes import approvals as approvals_view
from molt.console.routes import retention as retention_view
from molt.console.routes import tenancy
from molt.console.routing import HANDLERS, ROUTE_TABLE, DemoDisposition, route_named
from molt.policy.apply import (
    QUEUE_ENTRY_QUERY,
    QUEUE_LIST_QUERY,
    RESOLVE_APPROVAL_STATEMENT,
    ApprovalDecision,
    ApprovalStatus,
    queued_approval,
    queued_approvals,
)
from molt.retention import REPORT_STATEMENT, ClientRetentionReport, expiry_for
from molt.store import Cursor
from molt.store.capability import CapabilityRecord

T = TypeVar("T")

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

PERMITTED_CLIENT: Final[UUID] = UUID(int=11)
OTHER_CLIENT: Final[UUID] = UUID(int=22)
PERMITTED_SLUG: Final[str] = "permitted-tenant"
OTHER_SLUG: Final[str] = "another-tenant"

# A demonstration serves the seeded tenants and nothing else, so a case that renders a
# view under the mode rosters a slug taken from the seeded set itself rather than a
# literal of its own, which would drift the moment the corpus changed.
SEEDED_SLUG: Final[str] = min(demo.SEEDED_CLIENT_SLUGS)

# A slug provably outside that set: it is longer than every member, being every member
# joined together and then suffixed, so no member can equal it whatever the corpus holds.
UNSEEDED_SLUG: Final[str] = "-".join(sorted(demo.SEEDED_CLIENT_SLUGS)) + "-unseeded"

PENDING_ENTRY: Final[UUID] = UUID(int=31)
RESOLVED_ENTRY: Final[UUID] = UUID(int=32)
FOREIGN_ENTRY: Final[UUID] = UUID(int=33)
RULE_ID: Final[UUID] = UUID(int=41)
SESSION_ID: Final[UUID] = UUID(int=51)
EVENT_ID: Final[UUID] = UUID(int=61)

RULE_NAME: Final[str] = "a-rule-asking-for-approval"
JURISDICTION: Final[str] = "a-jurisdiction"
INTERVAL: Final[timedelta] = timedelta(days=90)

_ROSTER_ROW: Final[tuple[object, ...]] = (
    PERMITTED_CLIENT,
    PERMITTED_SLUG,
    "Permitted Tenant",
)


def _roster_of(slug: str) -> tuple[tuple[object, ...], ...]:
    """A one-row roster naming a slug, held by the Client the queue rows name.

    The slug is what the demonstration narrowing decides on, so a case parameterises it
    here rather than the fixture's default changing for every case in the file.
    """
    return ((PERMITTED_CLIENT, slug, "A Rostered Tenant"),)


# One report line per Client, including one for a Client the roster does not hold, so
# the page's scoping is asserted against a report that offered more than the roster did.
_REPORT_ROWS: Final[tuple[tuple[object, ...], ...]] = (
    (PERMITTED_CLIENT, PERMITTED_SLUG, JURISDICTION, INTERVAL, 4, 2),
    (OTHER_CLIENT, OTHER_SLUG, "another-jurisdiction", INTERVAL, 9, 8),
)

_PENDING_ROW: Final[tuple[object, ...]] = (
    PENDING_ENTRY,
    RULE_ID,
    RULE_NAME,
    "require_approval",
    SESSION_ID,
    PERMITTED_CLIENT,
    EVENT_ID,
    ApprovalStatus.PENDING.value,
    NOW,
    None,
    None,
    None,
)

_RESOLVED_ROW: Final[tuple[object, ...]] = (
    RESOLVED_ENTRY,
    RULE_ID,
    RULE_NAME,
    "require_approval",
    SESSION_ID,
    PERMITTED_CLIENT,
    None,
    ApprovalStatus.RESOLVED.value,
    NOW,
    auth.OPERATOR_SUBJECT,
    ApprovalDecision.DENIED.value,
    NOW,
)

_RESOLUTION_ROW: Final[tuple[object, ...]] = (
    PENDING_ENTRY,
    RULE_ID,
    SESSION_ID,
    EVENT_ID,
    ApprovalStatus.RESOLVED.value,
    auth.OPERATOR_SUBJECT,
    ApprovalDecision.APPROVED.value,
    NOW,
)


class RecordingCursor:
    """A cursor answering each statement these views send from held rows."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self._rows: list[tuple[object, ...]] = []

    def execute(self, statement: str, parameters: Sequence[object] | None = None) -> None:
        """Record the statement with what it was bound with, and hold its answer."""
        bound = () if parameters is None else tuple(parameters)
        self._store.sent.append((statement, bound))
        self._rows = list(self._store.answer(statement, bound))

    def fetchone(self) -> tuple[object, ...] | None:
        """The single row a statement of one row answers with."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row a listing statement answers with."""
        return list(self._rows)

    def close(self) -> None:
        """Close the cursor, which this stand-in holds nothing for."""


class FakeStore:
    """The store surface these views read and write through, answering by statement text."""

    role = "eraser"

    def __init__(
        self,
        *,
        roster: Sequence[tuple[object, ...]] | None = None,
        report_rows: Sequence[tuple[object, ...]] | None = None,
        queue: Sequence[tuple[object, ...]] | None = None,
        resolution: Sequence[tuple[object, ...]] | None = None,
        failing: bool = False,
    ) -> None:
        self.sent: list[tuple[str, tuple[object, ...]]] = []
        self.labels: list[str] = []
        self._roster = [_ROSTER_ROW] if roster is None else list(roster)
        self._report = list(_REPORT_ROWS) if report_rows is None else list(report_rows)
        self._queue = [_PENDING_ROW, _RESOLVED_ROW] if queue is None else list(queue)
        self._resolution = [_RESOLUTION_ROW] if resolution is None else list(resolution)
        self._failing = failing

    def read(self, body: Callable[[Cursor], T]) -> T:
        """Run a read body on a cursor of this stand-in's own."""
        return body(cast(Cursor, RecordingCursor(self)))

    def in_serializable(self, body: Callable[[Cursor], T], *, label: str = "") -> T:
        """Run a transaction body, recording the label the caller framed it under."""
        self.labels.append(label)
        return body(cast(Cursor, RecordingCursor(self)))

    def known_capabilities(self) -> CapabilityRecord:
        """The capability record the health route reads and these views do not."""
        return CapabilityRecord()

    def answer(self, statement: str, bound: tuple[object, ...]) -> Sequence[tuple[object, ...]]:
        """The rows one statement is answered with, refusing a statement not declared."""
        if statement == tenancy.CLIENT_ROSTER_STATEMENT:
            return list(self._roster)
        if statement == REPORT_STATEMENT:
            if self._failing:
                raise RuntimeError("the cluster did not answer the retention report")
            return list(self._report)
        if statement == QUEUE_LIST_QUERY:
            if self._failing:
                raise RuntimeError("the cluster did not answer the queue read")
            return [row for row in self._queue if row[5] in tuple(cast(list[object], bound[0]))]
        if statement == QUEUE_ENTRY_QUERY:
            return [
                row
                for row in self._queue
                if row[0] == bound[0] and row[5] in tuple(cast(list[object], bound[1]))
            ]
        if statement == RESOLVE_APPROVAL_STATEMENT:
            return list(self._resolution)
        raise AssertionError(f"the views sent an unexpected statement: {statement}")


def _console(store: FakeStore, *, demo_mode: bool = False) -> Console:
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


def _serve(
    method: str,
    path: str,
    *,
    store: FakeStore,
    query: str = "",
    body: str = "",
    token: str | None = None,
    demo_mode: bool = False,
) -> LambdaResponse:
    """Serve one request through the deployed path, carrying a valid session."""
    session, cookie = auth.issue(SESSION_KEY, now=NOW)
    headers = {"accept": "text/html", "content-type": "application/x-www-form-urlencoded"}
    carried = session.csrf_token if token is None else token
    if carried:
        headers["x-csrf-token"] = carried
    event: Mapping[str, object] = {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": query,
        "headers": headers,
        "cookies": [f"{auth.COOKIE_NAME}={cookie}"],
        "requestContext": {"http": {"method": method, "path": path}},
        "body": body,
        "isBase64Encoded": False,
    }
    app = build_app(_console(store, demo_mode=demo_mode))
    return invoke(cast(Any, app), dict(event))


def _html(answer: LambdaResponse) -> str:
    return cast(str, answer["body"])


def _statements(store: FakeStore) -> list[str]:
    return [statement for statement, _ in store.sent]


# -- the three routes are claimed -----------------------------------------


def test_the_three_declared_routes_are_claimed_by_these_views() -> None:
    assert HANDLERS["retention"] is retention_view.retention_view
    assert HANDLERS["approvals"] is approvals_view.approvals_view
    assert HANDLERS["approval_resolve"] is approvals_view.approval_resolve


def test_no_declared_route_is_left_without_a_handler() -> None:
    unclaimed = [spec.name for spec in ROUTE_TABLE if spec.name not in HANDLERS]
    assert unclaimed == []


def test_the_resolution_route_keeps_the_posture_the_table_declares() -> None:
    spec = route_named("approval_resolve")
    assert spec.mutation
    assert spec.demo is DemoDisposition.BLOCKED
    assert not route_named("retention").mutation
    assert not route_named("approvals").mutation


# -- the retention view ---------------------------------------------------


def test_the_retention_view_renders_each_permitted_client_s_regime_and_two_counts() -> None:
    store = FakeStore()
    answer = _serve("GET", "/retention", store=store)
    assert answer["statusCode"] == 200
    body = _html(answer)
    assert PERMITTED_SLUG in body
    assert JURISDICTION in body
    assert "90 day(s)" in body
    assert ">4<" in body
    assert ">2<" in body
    assert "<caption>" in body
    assert 'scope="col"' in body


def test_a_report_line_for_a_client_outside_the_roster_is_not_rendered() -> None:
    store = FakeStore()
    body = _html(_serve("GET", "/retention", store=store))
    assert OTHER_SLUG not in body
    assert "another-jurisdiction" not in body


def test_a_client_filter_naming_no_roster_row_reports_nothing_and_takes_no_report() -> None:
    store = FakeStore()
    answer = _serve("GET", "/retention", store=store, query="client=a-slug-nobody-holds")
    assert answer["statusCode"] == 200
    assert PERMITTED_SLUG not in _html(answer)
    assert REPORT_STATEMENT not in _statements(store)


def test_the_expiry_shown_is_the_retention_component_s_own_arithmetic() -> None:
    rows = retention_view.retention_rows(
        (
            ClientRetentionReport(
                client_id=PERMITTED_CLIENT,
                slug=PERMITTED_SLUG,
                jurisdiction=JURISDICTION,
                interval=INTERVAL,
                expiring_soon=4,
                already_expired=2,
            ),
        ),
        (tenancy.ClientChoice(id=PERMITTED_CLIENT, slug=PERMITTED_SLUG, display_name="A"),),
        None,
        NOW,
    )
    assert len(rows) == 1
    assert rows[0].expiry_of_a_write_now == expiry_for(NOW, INTERVAL)
    assert rows[0].interval == INTERVAL


def test_a_permitted_client_the_report_described_not_at_all_renders_that_absence() -> None:
    rows = retention_view.retention_rows(
        (),
        (tenancy.ClientChoice(id=PERMITTED_CLIENT, slug=PERMITTED_SLUG, display_name="A"),),
        None,
        NOW,
    )
    assert len(rows) == 1
    assert rows[0].line is None
    assert rows[0].expiring_soon is None
    assert rows[0].expiry_of_a_write_now is None


def test_a_report_the_cluster_did_not_answer_renders_a_notice_rather_than_a_failure() -> None:
    answer = _serve("GET", "/retention", store=FakeStore(failing=True))
    assert answer["statusCode"] == 200
    assert "could not be taken" in _html(answer)


# -- the approval queue list ----------------------------------------------


def test_the_queue_list_renders_each_entry_with_its_rule_and_its_standing() -> None:
    store = FakeStore()
    answer = _serve("GET", "/approvals", store=store)
    assert answer["statusCode"] == 200
    body = _html(answer)
    assert str(PENDING_ENTRY) in body
    assert str(RESOLVED_ENTRY) in body
    assert RULE_NAME in body
    assert ApprovalStatus.PENDING.value in body
    assert ApprovalDecision.DENIED.value in body
    assert f'action="/approvals/{PENDING_ENTRY}"' in body


def test_the_queue_read_is_bound_to_the_permitted_client_identifiers() -> None:
    store = FakeStore()
    _serve("GET", "/approvals", store=store)
    bound = [values for statement, values in store.sent if statement == QUEUE_LIST_QUERY]
    assert bound
    assert bound[0][0] == [PERMITTED_CLIENT]


def test_an_entry_of_another_tenant_is_not_listed() -> None:
    foreign = (
        FOREIGN_ENTRY,
        RULE_ID,
        "a-rule-of-another-tenant",
        "require_approval",
        SESSION_ID,
        OTHER_CLIENT,
        None,
        ApprovalStatus.PENDING.value,
        NOW,
        None,
        None,
        None,
    )
    store = FakeStore(queue=[_PENDING_ROW, foreign])
    body = _html(_serve("GET", "/approvals", store=store))
    assert str(PENDING_ENTRY) in body
    assert str(FOREIGN_ENTRY) not in body


def test_an_empty_permitted_set_reads_no_queue_at_all() -> None:
    store = FakeStore(roster=[])
    assert queued_approvals(cast(Any, store), ()) == ()
    assert queued_approval(cast(Any, store), PENDING_ENTRY, ()) is None
    assert not store.sent


def test_a_queue_the_cluster_did_not_answer_renders_a_notice() -> None:
    answer = _serve("GET", "/approvals", store=FakeStore(failing=True))
    assert answer["statusCode"] == 200
    assert "could not be read" in _html(answer)


def test_the_queue_list_disables_its_controls_in_demonstration_mode() -> None:
    plain = _html(_serve("GET", "/approvals", store=FakeStore()))
    demonstration = _html(
        _serve(
            "GET",
            "/approvals",
            store=FakeStore(roster=_roster_of(SEEDED_SLUG)),
            demo_mode=True,
        )
    )
    assert "disabled" not in plain
    assert "disabled" in demonstration
    assert str(PENDING_ENTRY) in demonstration
    assert 'aria-describedby="approvals-blocked"' in demonstration


def test_a_demonstration_renders_no_row_for_a_tenant_the_seed_does_not_name() -> None:
    assert UNSEEDED_SLUG not in demo.SEEDED_CLIENT_SLUGS
    store = FakeStore(roster=_roster_of(UNSEEDED_SLUG))
    answer = _serve("GET", "/approvals", store=store, demo_mode=True)
    assert answer["statusCode"] == 200
    body = _html(answer)
    assert UNSEEDED_SLUG not in body
    assert str(PENDING_ENTRY) not in body
    scoped = [
        values
        for statement, values in store.sent
        if statement == QUEUE_LIST_QUERY and PERMITTED_CLIENT in cast(list[object], values[0])
    ]
    assert scoped == []


# -- resolving one entry --------------------------------------------------


def test_resolving_an_entry_writes_the_schema_s_own_vocabulary_and_returns_to_the_queue() -> None:
    store = FakeStore()
    answer = _serve(
        "POST",
        f"/approvals/{PENDING_ENTRY}",
        store=store,
        body=f"decision={ApprovalDecision.APPROVED.value}",
    )
    assert answer["statusCode"] == 303
    assert cast(Mapping[str, str], answer["headers"])["location"] == approvals_view.APPROVALS_PATH
    resolving = [
        values for statement, values in store.sent if statement == RESOLVE_APPROVAL_STATEMENT
    ]
    assert len(resolving) == 1
    assert resolving[0][0] == ApprovalStatus.RESOLVED.value
    assert resolving[0][1] == auth.OPERATOR_SUBJECT
    assert resolving[0][2] == ApprovalDecision.APPROVED.value
    assert resolving[0][4] == PENDING_ENTRY
    assert resolving[0][5] == ApprovalStatus.PENDING.value


def test_a_decision_outside_the_schema_s_vocabulary_writes_nothing() -> None:
    store = FakeStore()
    answer = _serve("POST", f"/approvals/{PENDING_ENTRY}", store=store, body="decision=maybe")
    assert answer["statusCode"] == 400
    assert RESOLVE_APPROVAL_STATEMENT not in _statements(store)
    body = _html(answer)
    assert ApprovalDecision.APPROVED.value in body
    assert ApprovalDecision.DENIED.value in body


def test_an_entry_no_permitted_client_holds_is_answered_as_absent() -> None:
    store = FakeStore()
    answer = _serve(
        "POST",
        f"/approvals/{FOREIGN_ENTRY}",
        store=store,
        body=f"decision={ApprovalDecision.DENIED.value}",
    )
    assert answer["statusCode"] == 404
    assert RESOLVE_APPROVAL_STATEMENT not in _statements(store)


def test_an_identifier_that_is_not_one_reads_nothing() -> None:
    store = FakeStore()
    answer = _serve("POST", "/approvals/not-an-identifier", store=store, body="decision=approved")
    assert answer["statusCode"] == 404
    assert not store.sent


def test_a_resolution_without_the_session_s_own_token_is_refused_before_the_handler() -> None:
    store = FakeStore()
    answer = _serve(
        "POST",
        f"/approvals/{PENDING_ENTRY}",
        store=store,
        body="decision=approved",
        token="",
    )
    assert answer["statusCode"] == 403
    assert not store.sent


def test_a_resolution_is_refused_in_read_only_demonstration_mode() -> None:
    store = FakeStore()
    answer = _serve(
        "POST",
        f"/approvals/{PENDING_ENTRY}",
        store=store,
        body="decision=approved",
        demo_mode=True,
    )
    assert answer["statusCode"] == 403
    assert not store.sent


def test_an_entry_already_resolved_keeps_the_first_decision() -> None:
    store = FakeStore(resolution=[])
    answer = _serve(
        "POST",
        f"/approvals/{RESOLVED_ENTRY}",
        store=store,
        body=f"decision={ApprovalDecision.APPROVED.value}",
    )
    assert answer["statusCode"] == 303
    resolving = [
        values for statement, values in store.sent if statement == RESOLVE_APPROVAL_STATEMENT
    ]
    assert len(resolving) == 1
    assert resolving[0][5] == ApprovalStatus.PENDING.value
