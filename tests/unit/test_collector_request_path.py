"""The Collector request path: routing, the bearer gate, the body bound, and health.

Every assertion here is about a decision the Collector makes before a row is
written, which is why the suite needs no cluster. The store it is given refuses
every connection, so a test can assert both that a refused request never reached
for one and that an unreachable cluster is answered as unreachability rather than
as a fault.

The persistence path itself — the Session created inside the Event transaction,
the chain appends, the halt fields read back — is asserted against a real instance
by the Collector integration suite, because those are promises about a
transaction and a transaction is not something a double can keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt.capture.hook import EVENTS_PATH as HOOK_EVENTS_PATH
from molt.capture.hook import RECALL_PATH as HOOK_RECALL_PATH
from molt.capture.hook import SESSION_PATH_TEMPLATE, batch_body, read_envelope
from molt.capture.signing import AUTHORIZATION_HEADER, BEARER_SCHEME
from molt.capture.spool import RECORD_SEPARATOR as SPOOL_RECORD_SEPARATOR
from molt.collector.handler import (
    DEGRADED_STATUS,
    LIVE_STATUS,
    REACHABLE,
    UNREACHABLE,
    WRITE_FAILURE_METRIC,
    Collector,
    Invocation,
    RecallSearch,
    rendered,
    retention_interval,
)
from molt.collector.routes import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_RESERVED_CONCURRENCY,
    EVENTS_PATH,
    HEALTH_PATH,
    RECALL_PATH,
    RECORD_SEPARATOR,
    SESSIONS_PREFIX,
    SIGNED_KINDS,
    HaltReport,
    Headers,
    RecallAnswer,
    RecallQuery,
    RejectionReason,
    Response,
    RouteKind,
    envelope,
    exceeds_bound,
    match_path,
    max_body_bytes,
    read_records,
    reserved_concurrency,
)
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.errors import IngressRejectedError, StoreError
from molt.models.event import Event, EventCategory, JsonObject, JsonValue, serialise_event
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.store import Connection, MemoryStore
from molt.telemetry import CONTENT_KEYS, current, reset

BEARER_VALUE: Final[str] = "a-collector-bearer-value"
OTHER_VALUE: Final[str] = "a-value-that-is-not-the-expected-one"
MACHINE: Final[str] = "machine-under-test"
TOOL: Final[str] = "claude_code"
SMALL_BOUND: Final[int] = 512
TIMEOUT_MS: Final[int] = 1000
OK: Final[int] = 200
BAD_REQUEST: Final[int] = 400
UNAUTHORISED: Final[int] = 401
NOT_FOUND: Final[int] = 404
METHOD_NOT_ALLOWED: Final[int] = 405
TOO_LARGE: Final[int] = 413
UNAVAILABLE: Final[int] = 503
SESSION_UNDER_TEST: Final[UUID] = UUID("11111111-1111-4111-8111-111111111111")


class ManualClock(Protocol):
    """The one reading this suite takes from the injected time source."""

    def now(self) -> datetime:
        """The current wall reading."""


# ---------------------------------------------------------------------------
# Doubles and builders
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefusedConnections:
    """A connection factory that refuses, counting how often it was asked.

    Counting is what lets a test assert that a request refused on its bound or on
    its bearer value never reached for a connection at all.
    """

    attempts: int = 0

    def open(self) -> Connection:
        """Record the attempt and refuse it."""
        self.attempts += 1
        raise StoreError("this suite reaches no cluster")


def build_configuration(*, maximum: int = SMALL_BOUND) -> Configuration:
    """A configuration view over an explicit environment and no file."""
    return Configuration(
        environ={
            "MOLT_COLLECTOR_MAX_BODY_BYTES": str(maximum),
            "MOLT_DB_STATEMENT_TIMEOUT_MS": str(TIMEOUT_MS),
        }
    )


def build_collector(
    *,
    maximum: int = SMALL_BOUND,
    factory: RefusedConnections | None = None,
    verified: bool = True,
    recall: RecallSearch | None = None,
) -> Collector:
    """A Collector over a refusing store, with the signature seam driven explicitly.

    The signature verifier is injected rather than loaded, because what the
    signature does with a request is the next task's subject and what this suite
    asserts is only that the seam decides whether the request proceeds.
    """
    connections = RefusedConnections() if factory is None else factory
    store = MemoryStore(connect_with=connections.open, statement_timeout_ms=TIMEOUT_MS)

    def accept(headers: object, body: bytes) -> None:
        assert isinstance(body, bytes)
        assert headers is not None

    def refuse(headers: object, body: bytes) -> None:
        assert headers is not None
        raise IngressRejectedError(f"the injected verifier refused {len(body)} byte(s)")

    return Collector(
        configuration=build_configuration(maximum=maximum),
        store=store,
        bearer=Credential(
            BEARER_VALUE,
            source_name="MOLT_COLLECTOR_TOKEN",
            source=CredentialSource.ENVIRONMENT,
        ),
        ingress=accept if verified else refuse,
        recall=recall,
    )


def authorised(value: str = BEARER_VALUE) -> dict[str, str]:
    """The one header an authenticated request presents."""
    return {AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {value}"}


def invocation(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> Invocation:
    """One request as the transport would deliver it, carried as text."""
    return Invocation(
        method=method,
        path=path,
        headers=Headers(headers or {}),
        body_text=body.decode("utf-8"),
        base64_encoded=False,
    )


def build_event(clock: ManualClock, *, session_id: UUID = SESSION_UNDER_TEST) -> Event:
    """One well-formed Event of the shape the capture side transmits."""
    return Event(
        id=uuid4(),
        session_id=session_id,
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=clock.now(),
        agent_cli=TOOL,
        machine_id=MACHINE,
        parent_event_id=None,
        payload={"command": "ls"},
        redacted=False,
        text_body=None,
    )


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


# ---------------------------------------------------------------------------
# The route table
# ---------------------------------------------------------------------------


def test_the_route_paths_are_the_ones_the_capture_side_addresses() -> None:
    """The two sides spell the paths independently and must spell them the same."""
    assert EVENTS_PATH == HOOK_EVENTS_PATH
    assert RECALL_PATH == HOOK_RECALL_PATH
    assert SESSION_PATH_TEMPLATE.format(session_id=SESSION_UNDER_TEST).startswith(SESSIONS_PREFIX)
    assert RECORD_SEPARATOR == SPOOL_RECORD_SEPARATOR


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        (EVENTS_PATH, RouteKind.EVENTS),
        (RECALL_PATH, RouteKind.RECALL),
        (HEALTH_PATH, RouteKind.HEALTH),
        (f"{HEALTH_PATH}/", RouteKind.HEALTH),
        (f"{SESSIONS_PREFIX}{SESSION_UNDER_TEST}", RouteKind.SESSION),
    ],
)
def test_each_served_path_matches_its_own_route(path: str, kind: RouteKind) -> None:
    """A path the design tabulates resolves to the route the table names."""
    route = match_path(path)
    assert route is not None
    assert route.kind is kind


@pytest.mark.parametrize(
    "path",
    ["/", "/events/extra", "/sessions/", f"{SESSIONS_PREFIX}not-an-identifier", "/metrics"],
)
def test_a_path_naming_no_resource_matches_no_route(path: str) -> None:
    """A path outside the table matches nothing rather than the nearest thing."""
    assert match_path(path) is None


def test_the_two_ingest_routes_are_the_signed_ones() -> None:
    """Recall stays bearer-only so an interactive caller reaches it unsigned."""
    assert frozenset({RouteKind.EVENTS, RouteKind.SESSION}) == SIGNED_KINDS


# ---------------------------------------------------------------------------
# The bearer gate
# ---------------------------------------------------------------------------


def test_an_absent_and_a_mismatched_bearer_value_are_one_answer() -> None:
    """Both refusals carry the same status and the same body, as the comparison does."""
    collector = build_collector()
    absent = collector.serve(invocation("POST", EVENTS_PATH))
    mismatched = collector.serve(invocation("POST", EVENTS_PATH, headers=authorised(OTHER_VALUE)))
    assert absent.status == UNAUTHORISED
    assert mismatched.status == UNAUTHORISED
    assert absent.document == mismatched.document


def test_an_unauthenticated_caller_learns_nothing_about_the_route_table() -> None:
    """An unknown path is refused before it is matched, so 404 needs the bearer value."""
    collector = build_collector()
    unauthenticated = collector.serve(invocation("GET", "/metrics"))
    authenticated = collector.serve(invocation("GET", "/metrics", headers=authorised()))
    assert unauthenticated.status == UNAUTHORISED
    assert authenticated.status == NOT_FOUND


def test_a_known_path_addressed_with_the_wrong_method_is_refused() -> None:
    """One route answers one method, and the refusal names the method rather than the path."""
    collector = build_collector()
    answer = collector.serve(invocation("GET", EVENTS_PATH, headers=authorised()))
    assert answer.status == METHOD_NOT_ALLOWED


def test_an_authenticated_empty_batch_reaches_no_connection() -> None:
    """A batch carrying no record opens no transaction and reports both counts as zero."""
    factory = RefusedConnections()
    collector = build_collector(factory=factory)
    answer = collector.serve(invocation("POST", EVENTS_PATH, headers=authorised()))
    assert answer.status == OK
    assert answer.document["accepted"] == 0
    assert answer.document["rejected"] == 0
    assert factory.attempts == 0


# ---------------------------------------------------------------------------
# The request bound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("length", "over"),
    [
        (SMALL_BOUND // 2, False),
        (SMALL_BOUND - 1, False),
        (SMALL_BOUND, False),
        (SMALL_BOUND + 1, True),
        (SMALL_BOUND * 8, True),
    ],
)
def test_the_bound_admits_the_maximum_and_refuses_one_byte_more(length: int, over: bool) -> None:
    """The boundary is inclusive on the accepted side, at the byte."""
    assert (
        exceeds_bound(
            Headers({}),
            "x" * length,
            base64_encoded=False,
            maximum=SMALL_BOUND,
        )
        is over
    )


def test_an_oversized_body_is_refused_before_any_connection_is_asked_for(
    time_source: ManualClock,
) -> None:
    """An oversized batch persists nothing, including nothing from a well-formed prefix."""
    factory = RefusedConnections()
    collector = build_collector(factory=factory)
    prefix = batch_body([build_event(time_source) for _ in range(4)])
    body = prefix + b"x" * (SMALL_BOUND + 1)
    answer = collector.serve(invocation("POST", EVENTS_PATH, headers=authorised(), body=body))
    assert answer.status == TOO_LARGE
    assert factory.attempts == 0


def test_a_body_declaring_an_oversized_length_is_refused_on_its_own_statement() -> None:
    """The declared length is read before the body is decoded or looked at."""
    factory = RefusedConnections()
    collector = build_collector(factory=factory)
    headers = authorised() | {"Content-Length": str(SMALL_BOUND + 1)}
    answer = collector.serve(invocation("POST", EVENTS_PATH, headers=headers, body=b"{}"))
    assert answer.status == TOO_LARGE
    assert factory.attempts == 0


def test_the_deployment_bounds_are_read_from_the_configuration_surface() -> None:
    """The body bound and the concurrency ceiling are the surface's own defaults."""
    surface = Configuration(environ={})
    assert max_body_bytes(surface) == DEFAULT_MAX_BODY_BYTES
    assert reserved_concurrency(surface) == DEFAULT_RESERVED_CONCURRENCY
    assert DEFAULT_MAX_BODY_BYTES == 5 * 1024 * 1024
    assert DEFAULT_RESERVED_CONCURRENCY == 10
    assert retention_interval(surface).total_seconds() > 0.0


# ---------------------------------------------------------------------------
# Partial batches
# ---------------------------------------------------------------------------


def test_a_partly_malformed_batch_keeps_every_well_formed_record(
    time_source: ManualClock,
) -> None:
    """Each rejection reason is told apart, and a blank line is a separator."""
    well_formed = serialise_event(build_event(time_source)).encode("utf-8")
    truncated = b'{"id": "not closed'
    wrong_type = b'{"id": 7, "session_id": 7, "client_id": 7, "category": 7}'
    unreadable = b'{"agent_cli": "\xff\xfe"}'
    body = RECORD_SEPARATOR.join(
        [well_formed, truncated, b"", b"   ", wrong_type, unreadable, well_formed, b""]
    )

    batch = read_records(body)

    assert len(batch.events) == 2
    assert batch.records == 5
    assert batch.rejections.count(RejectionReason.UNPARSED) == 1
    assert batch.rejections.count(RejectionReason.INVALID) == 1
    assert batch.rejections.count(RejectionReason.UNREADABLE) == 1
    assert len(batch.events) + len(batch.rejections) == batch.records


def test_a_long_record_inside_a_bounded_request_is_judged_by_its_shape(
    time_source: ManualClock,
) -> None:
    """There is no per-record bound: a long line that parses is a well-formed record."""
    event = build_event(time_source)
    padded = Event(
        id=event.id,
        session_id=event.session_id,
        client_id=event.client_id,
        category=event.category,
        occurred_at=event.occurred_at,
        agent_cli=event.agent_cli,
        machine_id=event.machine_id,
        parent_event_id=None,
        payload={"command": "x" * 4096},
        redacted=False,
        text_body=None,
    )
    batch = read_records(batch_body([padded]))
    assert len(batch.events) == 1
    assert batch.rejections == ()


# ---------------------------------------------------------------------------
# The response envelope
# ---------------------------------------------------------------------------


def test_the_envelope_round_trips_through_the_capture_side_reader() -> None:
    """The Collector's envelope is the one the hook's own reader parses."""
    approval: JsonObject = {
        "rule_id": str(SESSION_UNDER_TEST),
        "rule_name": "a rule under test",
        "categories": ["shell_command"],
        "patterns": ["/etc/"],
    }
    document = envelope(
        accepted=3,
        rejected=1,
        halt=HaltReport(halted=True, halt_reason="a stated reason", pending_approvals=(approval,)),
    )
    rendered_body = rendered_document(document)

    read = read_envelope(rendered_body)

    assert read is not None
    assert read.accepted == 3
    assert read.rejected == 1
    assert read.halted is True
    assert read.halt_reason == "a stated reason"
    assert len(read.pending_approvals) == 1
    assert read.pending_approvals[0].rule_name == "a rule under test"
    assert read.pending_approvals[0].categories == ("shell_command",)


def rendered_document(document: JsonObject) -> bytes:
    """The bytes a response body carries for a document."""
    body = rendered(Response(OK, document))["body"]
    assert isinstance(body, str)
    return body.encode("utf-8")


def test_every_ingest_response_carries_the_halt_fields() -> None:
    """The five envelope fields are present whether anything was read or not."""
    collector = build_collector()
    answer = collector.serve(invocation("POST", EVENTS_PATH, headers=authorised()))
    for name in ("accepted", "rejected", "halted", "halt_reason", "pending_approvals"):
        assert name in answer.document


# ---------------------------------------------------------------------------
# Unreachability
# ---------------------------------------------------------------------------


def test_an_unreachable_cluster_answers_503_and_counts_the_write_failure(
    time_source: ManualClock,
) -> None:
    """The caller learns to try again, and the failure is measured under its own name."""
    reset()
    collector = build_collector(maximum=DEFAULT_MAX_BODY_BYTES)
    body = batch_body([build_event(time_source)])

    answer = collector.serve(invocation("POST", EVENTS_PATH, headers=authorised(), body=body))

    assert answer.status == UNAVAILABLE
    assert current().counters().get((WRITE_FAILURE_METRIC, ()), 0.0) == 1.0


# ---------------------------------------------------------------------------
# The signature seam
# ---------------------------------------------------------------------------


def test_a_request_the_verifier_refuses_reaches_no_connection(time_source: ManualClock) -> None:
    """The check runs before the transaction, so a well-formed prefix persists nothing."""
    factory = RefusedConnections()
    collector = build_collector(factory=factory, verified=False, maximum=DEFAULT_MAX_BODY_BYTES)
    body = batch_body([build_event(time_source) for _ in range(3)])

    answer = collector.serve(invocation("POST", EVENTS_PATH, headers=authorised(), body=body))

    assert answer.status == UNAUTHORISED
    assert factory.attempts == 0


def test_the_recall_route_is_answered_with_the_bearer_value_alone() -> None:
    """No signature is presented and none is required (Requirement 47.12)."""
    seen: list[RecallQuery] = []

    def search(query: RecallQuery) -> RecallAnswer:
        seen.append(query)
        return RecallAnswer(
            results=({"artifact_id": str(uuid4()), "distance": 0.5},),
            halt=HaltReport(halted=True, halt_reason="a stated reason"),
        )

    collector = build_collector(verified=False, recall=search)
    body = (
        f'{{"query_text": "how did this fail", "k": 5, "session_id": "{SESSION_UNDER_TEST}"}}'
    ).encode()

    answer = collector.serve(invocation("POST", RECALL_PATH, headers=authorised(), body=body))

    assert answer.status == OK
    assert answer.document["halted"] is True
    assert len(seen) == 1
    assert seen[0].limit == 5


def test_an_unreadable_recall_body_is_refused_rather_than_answered() -> None:
    """A query the route cannot read is a request fault rather than an empty answer."""
    collector = build_collector()
    answer = collector.serve(
        invocation("POST", RECALL_PATH, headers=authorised(), body=b"not a document")
    )
    assert answer.status == BAD_REQUEST


def test_session_metadata_describing_another_session_is_refused() -> None:
    """The path is authoritative, so a document naming another Session is refused."""
    collector = build_collector()
    body = (
        f'{{"id": "22222222-2222-4222-8222-222222222222", "client_id": "{UNASSIGNED_CLIENT_ID}"}}'
    ).encode()
    answer = collector.serve(
        invocation(
            "PUT",
            f"{SESSIONS_PREFIX}{SESSION_UNDER_TEST}",
            headers=authorised(),
            body=body,
        )
    )
    assert answer.status == BAD_REQUEST


# ---------------------------------------------------------------------------
# The health route
# ---------------------------------------------------------------------------


def test_the_health_route_needs_no_bearer_value_and_reports_unreachability() -> None:
    """Answering is the liveness report; the cluster's silence is reported as such."""
    collector = build_collector()
    answer = collector.serve(invocation("GET", HEALTH_PATH))
    assert answer.status == OK
    assert answer.document["status"] == DEGRADED_STATUS
    assert answer.document["database"] == UNREACHABLE
    assert answer.document["component"] == "collector"


def test_the_health_body_carries_no_memory_content() -> None:
    """No content-bearing field name appears anywhere in the health document."""
    collector = build_collector()
    answer = collector.serve(invocation("GET", HEALTH_PATH))
    assert content_keys_in(answer.document) == set()
    assert LIVE_STATUS != DEGRADED_STATUS
    assert REACHABLE != UNREACHABLE


def test_the_health_route_answers_one_method() -> None:
    """A health path addressed with another method is refused without the bearer value."""
    collector = build_collector()
    answer = collector.serve(invocation("POST", HEALTH_PATH))
    assert answer.status == METHOD_NOT_ALLOWED
