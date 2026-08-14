"""The fleet, Session, and lineage views, driven credential-free and cluster-free.

The store is a stand-in that answers each statement this task's views send from rows held
in memory, keyed by the statement text, so the assertions are about what the views ask
for and what they render rather than about a driver. Tenancy is the property under test
in three places: the roster is what the reads are scoped by, a Client filter naming no
roster row renders nothing, and a Session or an Artifact outside the roster is answered
as absent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, TypeVar, cast
from uuid import UUID

from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import build_app
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.console.routes import fleet, lineage, sessions, tenancy
from molt.models.session import Session, SessionOutcome
from molt.store import Cursor
from molt.store.capability import CapabilityRecord
from molt.store.chain import GENESIS_PREDECESSOR
from molt.store.sessions import (
    SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT,
    SELECT_CHILD_SESSIONS_STATEMENT,
    SELECT_EVENTS_FOR_SESSION_STATEMENT,
    SELECT_SESSION_STATEMENT,
    SELECT_SESSIONS_FOR_CLIENT_STATEMENT,
)

T = TypeVar("T")

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

PERMITTED_CLIENT: Final[UUID] = UUID(int=11)
OTHER_CLIENT: Final[UUID] = UUID(int=22)
PERMITTED_SESSION: Final[UUID] = UUID(int=33)
FOREIGN_SESSION: Final[UUID] = UUID(int=44)
PERMITTED_ARTIFACT: Final[UUID] = UUID(int=55)
PARENT_ARTIFACT: Final[UUID] = UUID(int=66)
FOREIGN_ARTIFACT: Final[UUID] = UUID(int=77)

PERMITTED_SLUG: Final[str] = "permitted-tenant"


def _session(identifier: UUID, client_id: UUID) -> Session:
    return Session(
        id=identifier,
        client_id=client_id,
        agent_cli="claude_code",
        machine_id="a-machine",
        team_id=None,
        attribution={},
        workspace_path=None,
        started_at=NOW,
        ended_at=None,
        outcome=SessionOutcome.IN_PROGRESS,
        parent_session_id=None,
        spawning_event_id=None,
        depth=0,
        tool_call_count=3,
        model_request_count=2,
        error_count=1,
        token_count=99,
        cost_usd=Decimal("0.25"),
        halted=False,
        halted_at=None,
        halt_reason=None,
        halt_rule_id=None,
    )


def _session_row(record: Session) -> tuple[object, ...]:
    return (
        record.id,
        record.client_id,
        record.agent_cli,
        record.machine_id,
        record.team_id,
        {},
        record.workspace_path,
        record.started_at,
        record.ended_at,
        str(record.outcome),
        record.parent_session_id,
        record.spawning_event_id,
        record.depth,
        record.tool_call_count,
        record.model_request_count,
        record.error_count,
        record.token_count,
        record.cost_usd,
        record.halted,
        record.halted_at,
        record.halt_reason,
        record.halt_rule_id,
    )


_DIGEST: Final[str] = "0" * 64


class RecordingCursor:
    """A cursor answering each statement this task's views send from held rows."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self._rows: list[tuple[object, ...]] = []

    def execute(self, statement: str, parameters: Sequence[object] | None = None) -> None:
        bound = () if parameters is None else tuple(parameters)
        self._store.sent.append((statement, bound))
        self._rows = list(self._store.answer(statement, bound))

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class FakeStore:
    """The store surface the views read through, answering by statement text."""

    role = "reader"

    def __init__(self, *, roster: Sequence[tuple[object, ...]] | None = None) -> None:
        self.sent: list[tuple[str, tuple[object, ...]]] = []
        self._roster = (
            [(PERMITTED_CLIENT, PERMITTED_SLUG, "Permitted Tenant")] if roster is None else roster
        )

    def read(self, body: Callable[[Cursor], T]) -> T:
        return body(cast(Cursor, RecordingCursor(self)))

    def known_capabilities(self) -> CapabilityRecord:
        return CapabilityRecord()

    def answer(self, statement: str, bound: tuple[object, ...]) -> Sequence[tuple[object, ...]]:
        if statement == tenancy.CLIENT_ROSTER_STATEMENT:
            return list(self._roster)
        if statement == SELECT_SESSIONS_FOR_CLIENT_STATEMENT:
            return (
                [_session_row(_session(PERMITTED_SESSION, PERMITTED_CLIENT))]
                if bound[0] == PERMITTED_CLIENT
                else []
            )
        if statement == SELECT_SESSION_STATEMENT:
            if bound[0] == PERMITTED_SESSION and bound[1] == PERMITTED_CLIENT:
                return [_session_row(_session(PERMITTED_SESSION, PERMITTED_CLIENT))]
            return []
        if statement == SELECT_EVENTS_FOR_SESSION_STATEMENT:
            return [
                (
                    UUID(int=101),
                    PERMITTED_SESSION,
                    PERMITTED_CLIENT,
                    1,
                    "tool_call",
                    NOW,
                    NOW + timedelta(seconds=1),
                    None,
                    False,
                    _DIGEST,
                    _DIGEST,
                )
            ]
        if statement == SELECT_CHILD_SESSIONS_STATEMENT:
            return []
        if statement == SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT:
            if bound[0] != PERMITTED_CLIENT:
                return []
            return [
                (
                    PERMITTED_ARTIFACT,
                    "summary",
                    PERMITTED_CLIENT,
                    _DIGEST,
                    "model_summary",
                    1,
                    NOW,
                    "embedded",
                )
            ]
        if statement == lineage.SELECT_PERMITTED_ARTIFACT_STATEMENT:
            if bound[0] == PERMITTED_ARTIFACT:
                return [(PERMITTED_ARTIFACT, "summary", PERMITTED_CLIENT, _DIGEST, NOW)]
            return []
        if statement == lineage.SELECT_PERMITTED_NODES_STATEMENT:
            asked = cast(Sequence[object], bound[0])
            return [
                (identifier, "summary", PERMITTED_CLIENT, _DIGEST, NOW)
                for identifier in (PARENT_ARTIFACT, PERMITTED_ARTIFACT)
                if identifier in asked
            ]
        if statement == lineage.SELECT_PERMITTED_EDGES_STATEMENT:
            nodes = cast(Sequence[object], bound[0])
            if PARENT_ARTIFACT in nodes and PERMITTED_ARTIFACT in nodes:
                return [(PERMITTED_ARTIFACT, PARENT_ARTIFACT, "derived_artifact", "model_summary")]
            return []
        if "ancestors AS (" in statement:
            return [(PARENT_ARTIFACT, "derived_artifact")]
        if "descendants AS (" in statement:
            return []
        if "FROM ledger" in statement:
            return []
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


def _event(path: str, *, query: str = "") -> dict[str, object]:
    _, cookie = auth.issue(SESSION_KEY, now=NOW)
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": query,
        "headers": {"accept": "text/html"},
        "cookies": [f"{auth.COOKIE_NAME}={cookie}"],
        "requestContext": {"http": {"method": "GET", "path": path}},
        "body": "",
        "isBase64Encoded": False,
    }


def _serve(path: str, *, store: FakeStore, query: str = "") -> LambdaResponse:
    app = build_app(_console(store))
    return invoke(cast(Any, app), _event(path, query=query))


def _body(answer: LambdaResponse) -> str:
    return cast(str, answer["body"])


# -- the routes are claimed ------------------------------------------------


def test_the_four_declared_routes_are_claimed_by_these_views() -> None:
    from molt.console.routing import HANDLERS

    for name in ("fleet", "session_detail", "lineage", "lineage_artifact"):
        assert name in HANDLERS, name
    assert HANDLERS["fleet"] is fleet.fleet
    assert HANDLERS["session_detail"] is sessions.session_detail
    assert HANDLERS["lineage"] is lineage.lineage
    assert HANDLERS["lineage_artifact"] is lineage.lineage_artifact


# -- the fleet view --------------------------------------------------------


def test_the_fleet_view_reads_the_roster_and_scopes_every_session_read_by_it() -> None:
    store = FakeStore()
    answer = _serve("/", store=store)
    assert answer["statusCode"] == 200
    scoped = [
        bound
        for statement, bound in store.sent
        if statement == SELECT_SESSIONS_FOR_CLIENT_STATEMENT
    ]
    assert scoped
    for bound in scoped:
        assert bound[0] == PERMITTED_CLIENT
    rendered = _body(answer)
    assert str(PERMITTED_SESSION) in rendered
    assert "<caption>" in rendered
    assert 'scope="col"' in rendered


def test_a_client_filter_naming_no_roster_row_renders_no_session() -> None:
    store = FakeStore()
    answer = _serve("/", store=store, query="client=a-slug-the-roster-does-not-hold")
    assert answer["statusCode"] == 200
    assert str(PERMITTED_SESSION) not in _body(answer)
    assert not [
        bound
        for statement, bound in store.sent
        if statement == SELECT_SESSIONS_FOR_CLIENT_STATEMENT
    ]


def test_an_empty_roster_renders_no_tenant_content() -> None:
    store = FakeStore(roster=[])
    answer = _serve("/", store=store)
    assert answer["statusCode"] == 200
    assert str(PERMITTED_SESSION) not in _body(answer)


def test_the_fleet_row_labels_standing_as_a_word() -> None:
    row = fleet.FleetRow(
        client=tenancy.ClientChoice(id=PERMITTED_CLIENT, slug=PERMITTED_SLUG, display_name="A"),
        session=_session(PERMITTED_SESSION, PERMITTED_CLIENT),
    )
    assert row.standing == "running"
    assert row.activity == 5


# -- the Session view ------------------------------------------------------


def test_the_session_view_renders_the_stream_and_the_chain_status() -> None:
    store = FakeStore()
    answer = _serve(f"/sessions/{PERMITTED_SESSION}", store=store)
    assert answer["statusCode"] == 200
    rendered = _body(answer)
    assert "Chain verification" in rendered
    assert _DIGEST in rendered
    assert GENESIS_PREDECESSOR is not None
    scoped = [
        bound for statement, bound in store.sent if statement == SELECT_EVENTS_FOR_SESSION_STATEMENT
    ]
    assert scoped and scoped[0][1] == PERMITTED_CLIENT


def test_a_session_outside_the_roster_is_answered_as_absent() -> None:
    store = FakeStore()
    answer = _serve(f"/sessions/{FOREIGN_SESSION}", store=store)
    assert answer["statusCode"] == 404
    assert str(OTHER_CLIENT) not in _body(answer)


def test_a_malformed_session_identifier_reads_nothing() -> None:
    store = FakeStore()
    answer = _serve("/sessions/not-an-identifier", store=store)
    assert answer["statusCode"] == 404
    assert not store.sent


# -- the lineage views -----------------------------------------------------


def test_the_lineage_view_renders_an_svg_and_an_equivalent_edge_table() -> None:
    store = FakeStore()
    answer = _serve("/lineage", store=store)
    assert answer["statusCode"] == 200
    rendered = _body(answer)
    assert "<svg" in rendered
    assert "<title>" in rendered
    assert 'tabindex="0"' in rendered
    assert "Edge list" in rendered
    assert str(PARENT_ARTIFACT) in rendered


def test_every_lineage_read_is_bound_to_the_permitted_client_set() -> None:
    store = FakeStore()
    _serve("/lineage", store=store)
    for statement, bound in store.sent:
        if statement in (
            lineage.SELECT_PERMITTED_NODES_STATEMENT,
            lineage.SELECT_PERMITTED_ARTIFACT_STATEMENT,
        ):
            assert bound[1] == [PERMITTED_CLIENT]
            assert bound[2] == [PERMITTED_CLIENT]
        if "ancestors AS (" in statement or "descendants AS (" in statement:
            assert bound[1] == [PERMITTED_CLIENT]


def test_the_artifact_subgraph_answers_an_unpermitted_artifact_as_absent() -> None:
    store = FakeStore()
    answer = _serve(f"/lineage/{FOREIGN_ARTIFACT}", store=store)
    assert answer["statusCode"] == 404
    sent = [statement for statement, _ in store.sent]
    assert not [statement for statement in sent if "descendants AS (" in statement]


def test_the_artifact_subgraph_names_kind_bindings_and_creation_time() -> None:
    store = FakeStore()
    answer = _serve(f"/lineage/{PERMITTED_ARTIFACT}", store=store)
    assert answer["statusCode"] == 200
    rendered = _body(answer)
    assert "aria-label=" in rendered
    assert "bound to Permitted Tenant" in rendered
    assert NOW.isoformat() in rendered


def test_the_graph_layers_a_child_below_its_parent() -> None:
    rows = (
        lineage.ArtifactRow(
            id=PARENT_ARTIFACT,
            kind="summary",
            owner_client_id=PERMITTED_CLIENT,
            content_digest=_DIGEST,
            created_at=NOW,
        ),
        lineage.ArtifactRow(
            id=PERMITTED_ARTIFACT,
            kind="summary",
            owner_client_id=PERMITTED_CLIENT,
            content_digest=_DIGEST,
            created_at=NOW,
        ),
    )
    edges = (
        lineage.GraphEdge(
            child_id=PERMITTED_ARTIFACT,
            parent_id=PARENT_ARTIFACT,
            parent_kind="derived_artifact",
            derivation_method="model_summary",
        ),
    )
    labels: Mapping[UUID, str] = {PERMITTED_CLIENT: "Permitted Tenant"}
    graph = lineage.graph_of(rows, (), edges, labels)
    parent = graph.node_named(PARENT_ARTIFACT)
    child = graph.node_named(PERMITTED_ARTIFACT)
    assert parent is not None and child is not None
    assert child.layer == parent.layer + 1
    assert graph.depth == 2
    assert "derived_artifact" in child.accessible_name
