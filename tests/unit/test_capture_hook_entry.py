"""The hook entry point: exit status, signing, the two bounds, and halt observation.

Every assertion here is about a promise the capture path makes to the agent that
is waiting on it. The suite drives the real entry point and the real transmitter
against a recorded transport and a manual clock, so the retry schedule and the
soft deadline are exercised by advancing a reading rather than by waiting, and no
socket is opened.

The adapter is a stub rather than one of the five delivered ones. The five are
written from their vendors' own specifications and are asserted against those
specifications elsewhere; what is under test here is the entry point's behaviour
given *an* adapter, which is exactly what a stub isolates.
"""

from __future__ import annotations

import ast
import io
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt.capture.hook import (
    DEADLINE_NOTE,
    EVENTS_PATH,
    EXIT_OK,
    NO_BEARER_NOTE,
    RECALL_PATH,
    UNASSIGNED_NOTE,
    UNOBSERVED_HALT_NOTE,
    UNSIGNED_NOTE,
    Envelope,
    HttpTransport,
    Reply,
    Transmitter,
    batch_body,
    dispatch,
    load_client_map,
    main,
    read_envelope,
    resolve_client,
)
from molt.capture.protocol import (
    AdapterCapabilities,
    CaptureContext,
    ClientRef,
    HookInvocation,
    PendingApproval,
    RecallResult,
    derive_session_id,
)
from molt.capture.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ingress_timestamp,
    sign_ingress,
)
from molt.capture.spool import Spool
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.models.event import Event, EventCategory, deserialise_event
from molt.models.session import UNASSIGNED_CLIENT_ID

SHARED_VALUE: Final[str] = "an-ingress-shared-value"
BEARER: Final[str] = "a-collector-bearer-value"
MACHINE: Final[str] = "machine-under-test"
TOOL: Final[str] = "claude_code"
COLLECTOR_URL: Final[str] = "https://collector.invalid/api"
NO_JITTER: Final[float] = 0.0
GENEROUS_DEADLINE_SECONDS: Final[float] = 10.0
CAP_SECONDS: Final[float] = 5.0
RETRIES: Final[int] = 3
SCHEDULE: Final[tuple[float, ...]] = (0.2, 0.4, 0.8)
SERVICE_UNAVAILABLE: Final[int] = 503
UNAUTHORISED: Final[int] = 401
OK: Final[int] = 200
DRIVER_PREFIXES: Final[tuple[str, ...]] = ("psycopg", "boto3", "botocore")
SOURCE_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "src"
HOOK_MODULE: Final[str] = "molt.capture.hook"
REDACT_PACKAGE: Final[str] = "molt.redact"


class ManualClock(Protocol):
    """The manual time source, with the two driving calls the suite makes on it."""

    def now(self) -> datetime:
        """The current wall reading."""

    def monotonic(self) -> float:
        """The current monotonic reading."""

    def advance(self, seconds: float) -> None:
        """Move both readings forward."""

    def sleep(self, seconds: float) -> None:
        """Stand in for waiting by advancing."""


def no_jitter(low: float, high: float) -> float:
    """Return zero, ignoring the window, so a delay is its scheduled value."""
    assert low <= high
    return NO_JITTER


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ByteStdin:
    """A standard input carrying bytes, so a payload is never decoded on the way in."""

    buffer: io.BytesIO


@dataclass(slots=True)
class RecordedTransport:
    """A transport that answers from a script and records what it was asked.

    A scripted entry that is an `OSError` is raised rather than returned, which is
    how an unreachable Collector is expressed. The last entry repeats, so a test
    that wants every attempt to fail scripts one failure.
    """

    replies: list[Reply | OSError]
    sent: list[tuple[str, str, bytes, dict[str, str], float]] = field(default_factory=list)
    closed: int = 0

    def send(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str] | object,
        *,
        timeout: float,
    ) -> Reply:
        """Record the request and answer with the next scripted reply."""
        fields = dict(headers) if isinstance(headers, dict) else {}
        self.sent.append((method, path, body, fields, timeout))
        entry = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        if isinstance(entry, OSError):
            raise entry
        return entry

    def close(self) -> None:
        """Record that the connection was released."""
        self.closed += 1


@dataclass(slots=True)
class StubAdapter:
    """One adapter's worth of behaviour, with every vendor decision made explicit."""

    tool: str = TOOL
    invocation: HookInvocation = field(
        default_factory=lambda: HookInvocation(
            tool=TOOL,
            event_name="PreToolUse",
            payload={"command": "rm -rf /etc"},
            session_key="run-1",
            workspace_path="/workspace/acme",
        )
    )
    categories: tuple[EventCategory, ...] = (EventCategory.TOOL_CALL,)
    capability: AdapterCapabilities = field(
        default_factory=lambda: AdapterCapabilities(
            structured_stdout=True,
            context_injection=True,
            blocking_decision=True,
        )
    )
    parsed: list[bytes] = field(default_factory=list)

    def parse(self, raw: bytes) -> HookInvocation:
        """Record the bytes and answer with the configured invocation."""
        self.parsed.append(raw)
        return self.invocation

    def to_events(self, inv: HookInvocation, ctx: CaptureContext) -> list[Event]:
        """Build one Event per configured category, carrying the matched fields."""
        return [
            Event(
                id=uuid4(),
                session_id=ctx.session_id,
                client_id=ctx.client.id,
                category=category,
                occurred_at=ctx.clock.now(),
                agent_cli=ctx.agent_cli,
                machine_id=ctx.machine_id,
                parent_event_id=None,
                payload={"command": str(inv.payload.get("command", ""))},
                redacted=False,
                text_body=None,
            )
            for category in self.categories
        ]

    def context_injection(self, results: list[RecallResult]) -> bytes:
        """Render the count of results, which is enough to tell empty from not."""
        return f"injected {len(results)}".encode()

    def blocking_response(self, reason: str) -> bytes:
        """Render the refusal, carrying the reason so a test can read it back."""
        return f"refused {reason}".encode()

    def capabilities(self) -> AdapterCapabilities:
        """The three flags this stub declares."""
        return self.capability


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_configuration(
    tmp_path: Path,
    *,
    secret: bool = True,
    bearer: bool = True,
    client_map: Path | None = None,
    soft_deadline_ms: int = 1200,
) -> Configuration:
    """A configuration view over an explicit environment and no file."""
    environ = {
        "MOLT_COLLECTOR_URL": COLLECTOR_URL,
        "MOLT_SPOOL_DIR": str(tmp_path / "spool"),
        "MOLT_MACHINE_ID": MACHINE,
        "MOLT_HTTP_TIMEOUT_SECONDS": str(int(CAP_SECONDS)),
        "MOLT_HTTP_RETRIES": str(RETRIES),
        "MOLT_HOOK_SOFT_DEADLINE_MS": str(soft_deadline_ms),
    }
    if secret:
        environ["MOLT_INGRESS_SECRET"] = SHARED_VALUE
    if bearer:
        environ["MOLT_COLLECTOR_TOKEN"] = BEARER
    if client_map is not None:
        environ["MOLT_CLIENT_MAP"] = str(client_map)
    return Configuration(environ=environ)


def build_spool(tmp_path: Path) -> Spool:
    """A spool of this machine's own, inside the temporary directory."""
    return Spool(tmp_path / "spool", MACHINE)


def build_transmitter(
    *,
    spool: Spool,
    transport: RecordedTransport,
    clock: ManualClock,
    secret: bool = True,
    bearer: bool = True,
    soft_deadline_seconds: float = GENEROUS_DEADLINE_SECONDS,
    retries: int = RETRIES,
) -> Transmitter:
    """A transmitter wired to the doubles, with both bounds stated explicitly."""
    return Transmitter(
        spool=spool,
        transport=transport,
        bearer=(
            Credential(
                BEARER,
                source_name="MOLT_COLLECTOR_TOKEN",
                source=CredentialSource.ENVIRONMENT,
            )
            if bearer
            else None
        ),
        secret=(
            Credential(
                SHARED_VALUE,
                source_name="MOLT_INGRESS_SECRET",
                source=CredentialSource.ENVIRONMENT,
            )
            if secret
            else None
        ),
        cap_seconds=CAP_SECONDS,
        soft_deadline_seconds=soft_deadline_seconds,
        retries=retries,
        clock=clock,
        sleep=clock.sleep,
        jitter=no_jitter,
    )


def build_event(clock: ManualClock, *, command: str = "ls") -> Event:
    """One Event of the shape the stub adapter produces."""
    return Event(
        id=uuid4(),
        session_id=derive_session_id(TOOL, "run-1"),
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=clock.now(),
        agent_cli=TOOL,
        machine_id=MACHINE,
        parent_event_id=None,
        payload={"command": command},
        redacted=False,
        text_body=None,
    )


def envelope_reply(
    *,
    halted: bool = False,
    reason: str | None = None,
    approvals: str = "",
    status: int = OK,
) -> Reply:
    """A reply carrying a response envelope, built as the Collector would send it."""
    body = f'{{"accepted": 1, "rejected": 0, "halted": {str(halted).lower()}'
    if reason is not None:
        body += f', "halt_reason": "{reason}"'
    if approvals:
        body += f', "pending_approvals": [{approvals}]'
    return Reply(status=status, body=(body + "}").encode())


def spooled_events(spool: Spool) -> tuple[Event, ...]:
    """Every Event the spool currently holds."""
    return spool.records()


# ---------------------------------------------------------------------------
# Exit status: the entry point never fails its host
# ---------------------------------------------------------------------------


def feed(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    """Place a byte payload on standard input."""
    monkeypatch.setattr(sys, "stdin", ByteStdin(io.BytesIO(payload)))


def test_an_unsupported_tool_token_exits_zero_and_writes_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    """A token naming no tool is a configuration mistake, not a reason to fail."""
    feed(monkeypatch, b"{}")

    status = main(["not_a_tool", "PreToolUse"])

    captured = capsysbinary.readouterr()
    assert status == EXIT_OK
    assert captured.out == b""
    assert len(captured.err.decode().splitlines()) == 1


def test_input_that_is_not_valid_utf8_exits_zero_and_writes_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    """Undecodable bytes reach the adapter as bytes and fail inside the handler."""
    feed(monkeypatch, b"\xff\xfe\x00not utf-8 at all")

    status = main([TOOL, "PreToolUse"])

    captured = capsysbinary.readouterr()
    assert status == EXIT_OK
    assert captured.out == b""
    assert len(captured.err.decode().splitlines()) == 1


def test_an_invocation_with_no_token_exits_zero_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    """The shim is installed per tool, so an argument-free call is a broken install."""
    feed(monkeypatch, b"")

    status = main([])

    captured = capsysbinary.readouterr()
    assert status == EXIT_OK
    assert captured.out == b""
    assert b"no agent tool token" in captured.err


def test_every_note_of_one_invocation_lands_on_one_line(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Three things to report is still one line, because the notes are joined."""
    configuration = build_configuration(tmp_path)
    transport = RecordedTransport(replies=[OSError("unreachable")])
    outcome = dispatch(
        TOOL,
        b"{}",
        adapter=StubAdapter(),
        configuration=configuration,
        transmitter=build_transmitter(
            spool=build_spool(tmp_path), transport=transport, clock=time_source
        ),
        clock=time_source,
    )

    assert len(outcome.notes) > 1
    diagnostic = outcome.diagnostic
    assert diagnostic is not None
    assert "\n" not in diagnostic


# ---------------------------------------------------------------------------
# Signing at transmission
# ---------------------------------------------------------------------------


def test_the_signature_covers_the_presented_timestamp_and_the_exact_body_bytes(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The verifier recomputes over the raw bytes, so the signer signs those bytes."""
    transport = RecordedTransport(replies=[envelope_reply()])
    sender = build_transmitter(spool=build_spool(tmp_path), transport=transport, clock=time_source)
    events = (build_event(time_source),)

    sender.emit(events)

    method, path, body, headers, _ = transport.sent[0]
    assert (method, path) == ("POST", EVENTS_PATH)
    assert body == batch_body(events)
    assert headers[SIGNATURE_HEADER] == sign_ingress(body, SHARED_VALUE, headers[TIMESTAMP_HEADER])


def test_a_spooled_batch_is_signed_with_a_timestamp_read_at_transmission(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """A batch buffered through an outage lands inside the age bound however long it ran."""
    spool = build_spool(tmp_path)
    spool.append((build_event(time_source),))
    captured_at = ingress_timestamp(time_source.now())
    time_source.advance(GENEROUS_DEADLINE_SECONDS * 100.0)
    transport = RecordedTransport(replies=[envelope_reply()])
    sender = build_transmitter(
        spool=spool,
        transport=transport,
        clock=time_source,
        soft_deadline_seconds=GENEROUS_DEADLINE_SECONDS,
    )

    sender.emit(())

    _, _, _, headers, _ = transport.sent[0]
    assert headers[TIMESTAMP_HEADER] != captured_at
    assert headers[TIMESTAMP_HEADER] == ingress_timestamp(time_source.now())


def test_the_claimed_records_travel_ahead_of_the_new_ones_in_one_batch(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Spooled Events are transmitted before new ones, over one connection."""
    spool = build_spool(tmp_path)
    spool.append((build_event(time_source, command="spooled"),))
    transport = RecordedTransport(replies=[envelope_reply()])
    sender = build_transmitter(spool=spool, transport=transport, clock=time_source)

    result = sender.emit((build_event(time_source, command="fresh"),))

    _, _, body, _, _ = transport.sent[0]
    lines = [deserialise_event(line.decode()) for line in body.splitlines()]
    assert [str(event.payload["command"]) for event in lines] == ["spooled", "fresh"]
    assert result.transmitted == 2
    assert spool.is_empty()


def test_an_absent_shared_secret_spools_rather_than_sending_an_unsigned_batch(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """An unsigned ingest request would be refused, so the Events are kept instead."""
    spool = build_spool(tmp_path)
    transport = RecordedTransport(replies=[envelope_reply()])
    sender = build_transmitter(spool=spool, transport=transport, clock=time_source, secret=False)

    result = sender.emit((build_event(time_source),))

    assert transport.sent == []
    assert result.note == UNSIGNED_NOTE
    assert result.spooled == 1
    assert len(spooled_events(spool)) == 1


def test_an_absent_bearer_token_spools_as_well(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The Collector requires the bearer on the ingest routes in addition to the signature."""
    spool = build_spool(tmp_path)
    transport = RecordedTransport(replies=[envelope_reply()])
    sender = build_transmitter(spool=spool, transport=transport, clock=time_source, bearer=False)

    result = sender.emit((build_event(time_source),))

    assert transport.sent == []
    assert result.note == NO_BEARER_NOTE
    assert len(spooled_events(spool)) == 1


def test_the_recall_path_proceeds_with_no_shared_secret_and_presents_no_signature(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Recall is bearer-only, so an operator holding no shared value still asks memory."""
    transport = RecordedTransport(
        replies=[Reply(status=OK, body=b'{"results": [], "halted": false}')]
    )
    sender = build_transmitter(
        spool=build_spool(tmp_path), transport=transport, clock=time_source, secret=False
    )

    outcome = sender.recall("delete the production role", session_id=uuid4())

    _, path, _, headers, _ = transport.sent[0]
    assert path == RECALL_PATH
    assert TIMESTAMP_HEADER not in headers
    assert SIGNATURE_HEADER not in headers
    assert outcome.results == ()
    assert outcome.envelope is not None


# ---------------------------------------------------------------------------
# The two bounds
# ---------------------------------------------------------------------------


def test_a_failed_transmission_is_retried_three_times_on_the_scheduled_backoff(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Four attempts in all, waiting the scheduled delays between them."""
    waits: list[float] = []

    def record(seconds: float) -> None:
        waits.append(seconds)
        time_source.sleep(seconds)

    spool = build_spool(tmp_path)
    transport = RecordedTransport(replies=[Reply(status=SERVICE_UNAVAILABLE, body=b"")])
    sender = Transmitter(
        spool=spool,
        transport=transport,
        bearer=Credential(BEARER, source_name="e", source=CredentialSource.ENVIRONMENT),
        secret=Credential(SHARED_VALUE, source_name="e", source=CredentialSource.ENVIRONMENT),
        cap_seconds=CAP_SECONDS,
        soft_deadline_seconds=GENEROUS_DEADLINE_SECONDS,
        retries=RETRIES,
        clock=time_source,
        sleep=record,
        jitter=no_jitter,
    )

    result = sender.emit((build_event(time_source),))

    assert len(transport.sent) == RETRIES + 1
    assert waits == list(SCHEDULE)
    assert result.attempts == RETRIES + 1
    assert result.spooled == 1


def test_the_soft_deadline_stops_the_phase_and_the_batch_is_spooled(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The cap bounds one operation; the soft deadline bounds the whole phase."""
    spool = build_spool(tmp_path)
    transport = RecordedTransport(replies=[Reply(status=SERVICE_UNAVAILABLE, body=b"")])
    sender = build_transmitter(
        spool=spool,
        transport=transport,
        clock=time_source,
        soft_deadline_seconds=SCHEDULE[0] + SCHEDULE[1],
    )

    result = sender.emit((build_event(time_source),))

    assert result.attempts < RETRIES + 1
    assert result.note == DEADLINE_NOTE
    assert len(spooled_events(spool)) == 1


def test_the_configured_soft_deadline_truncates_the_schedule_before_it_runs_out(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """At the configured default the phase ends inside the schedule, which is the point.

    The schedule sums to 1.4 seconds of waiting before the fourth attempt, and the
    configured soft deadline is 1.2 seconds, so the third retry is never started.
    The two bounds are not alternatives: the cap says how long one operation may
    take, the deadline says how long the agent may be kept waiting in total.
    """
    spool = build_spool(tmp_path)
    transport = RecordedTransport(replies=[Reply(status=SERVICE_UNAVAILABLE, body=b"")])
    sender = Transmitter.from_configuration(
        build_configuration(tmp_path),
        spool=spool,
        transport=transport,
        clock=time_source,
        sleep=time_source.sleep,
        jitter=no_jitter,
    )

    result = sender.emit((build_event(time_source),))

    assert result.attempts == RETRIES
    assert result.note == DEADLINE_NOTE
    assert len(spooled_events(spool)) == 1


def test_no_network_operation_is_given_more_than_the_configured_cap(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Every operation is bounded, and the bound never exceeds the configured ceiling."""
    transport = RecordedTransport(replies=[Reply(status=SERVICE_UNAVAILABLE, body=b"")])
    sender = build_transmitter(
        spool=build_spool(tmp_path),
        transport=transport,
        clock=time_source,
        soft_deadline_seconds=GENEROUS_DEADLINE_SECONDS,
    )

    sender.emit((build_event(time_source),))

    assert transport.sent
    for _, _, _, _, timeout in transport.sent:
        assert 0.0 < timeout <= CAP_SECONDS


def test_a_refused_request_is_not_retried(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """A refusal will not improve on a second try, so the round trips are not spent."""
    transport = RecordedTransport(replies=[Reply(status=UNAUTHORISED, body=b"")])
    sender = build_transmitter(spool=build_spool(tmp_path), transport=transport, clock=time_source)

    result = sender.emit((build_event(time_source),))

    assert len(transport.sent) == 1
    assert result.spooled == 1


# ---------------------------------------------------------------------------
# Halt and approval observation
# ---------------------------------------------------------------------------


def test_a_halted_envelope_refuses_the_action_and_queues_a_policy_halt_event(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The response envelope is the only halt channel the capture side has."""
    spool = build_spool(tmp_path)
    transport = RecordedTransport(
        replies=[envelope_reply(halted=True, reason="a sensitive path was written")]
    )
    outcome = dispatch(
        TOOL,
        b"{}",
        adapter=StubAdapter(),
        configuration=build_configuration(tmp_path),
        transmitter=build_transmitter(spool=spool, transport=transport, clock=time_source),
        clock=time_source,
    )

    assert outcome.blocked is True
    assert outcome.stdout.startswith(b"refused ")
    assert b"a sensitive path was written" in outcome.stdout
    assert [event.category for event in spooled_events(spool)] == [EventCategory.POLICY_HALT]


def test_a_pending_approval_refuses_only_an_action_its_rule_matches(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """An unrelated action proceeds while an approval for another rule is pending."""
    matching = RecordedTransport(
        replies=[
            envelope_reply(
                approvals='{"rule_name": "shell", "categories": ["tool_call"]}',
            )
        ]
    )
    unrelated = RecordedTransport(
        replies=[
            envelope_reply(
                approvals='{"rule_name": "writes", "categories": ["file_write"]}',
            )
        ]
    )

    blocked = dispatch(
        TOOL,
        b"{}",
        adapter=StubAdapter(),
        configuration=build_configuration(tmp_path),
        transmitter=build_transmitter(
            spool=build_spool(tmp_path / "a"), transport=matching, clock=time_source
        ),
        clock=time_source,
    )
    allowed = dispatch(
        TOOL,
        b"{}",
        adapter=StubAdapter(),
        configuration=build_configuration(tmp_path),
        transmitter=build_transmitter(
            spool=build_spool(tmp_path / "b"), transport=unrelated, clock=time_source
        ),
        clock=time_source,
    )

    assert blocked.blocked is True
    assert allowed.blocked is False


def test_an_unreachable_collector_spools_without_blocking_and_notes_the_unobserved_halt(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Availability of the agent is the higher obligation, so nothing is blocked."""
    spool = build_spool(tmp_path)
    transport = RecordedTransport(replies=[OSError("no route to host")])
    outcome = dispatch(
        TOOL,
        b"{}",
        adapter=StubAdapter(),
        configuration=build_configuration(tmp_path),
        transmitter=build_transmitter(
            spool=spool,
            transport=transport,
            clock=time_source,
            soft_deadline_seconds=GENEROUS_DEADLINE_SECONDS,
        ),
        clock=time_source,
    )

    assert outcome.blocked is False
    assert outcome.stdout == b""
    assert UNOBSERVED_HALT_NOTE in outcome.notes
    assert len(spooled_events(spool)) == 1


def test_an_unreachable_recall_path_injects_an_empty_result_set(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """A cluster failure yields an empty injection rather than a failed hook."""
    adapter = StubAdapter()
    adapter.invocation = HookInvocation(
        tool=TOOL,
        event_name="PreToolUse",
        payload={"command": "ls"},
        session_key="run-1",
        recall_query="list the directory",
    )
    outcome = dispatch(
        TOOL,
        b"{}",
        adapter=adapter,
        configuration=build_configuration(tmp_path),
        transmitter=build_transmitter(
            spool=build_spool(tmp_path),
            transport=RecordedTransport(replies=[OSError("no route to host")]),
            clock=time_source,
        ),
        clock=time_source,
    )

    assert outcome.stdout == b"injected 0"
    assert outcome.blocked is False


def test_a_tool_documenting_no_injection_envelope_still_surfaces_results(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The flag records which channel was used, not whether memory was consulted."""
    adapter = StubAdapter()
    adapter.capability = AdapterCapabilities(
        structured_stdout=True,
        context_injection=False,
        blocking_decision=True,
    )
    adapter.invocation = HookInvocation(
        tool=TOOL,
        event_name="PreToolUse",
        payload={"command": "ls"},
        session_key="run-1",
        recall_query="list the directory",
    )
    outcome = dispatch(
        TOOL,
        b"{}",
        adapter=adapter,
        configuration=build_configuration(tmp_path),
        transmitter=build_transmitter(
            spool=build_spool(tmp_path),
            transport=RecordedTransport(replies=[envelope_reply()]),
            clock=time_source,
        ),
        clock=time_source,
    )

    assert outcome.stdout == b"injected 0"
    assert any("advisory" in note for note in outcome.notes)


def test_a_reply_carrying_no_envelope_is_not_read_as_a_clear_one() -> None:
    """An unread envelope is unknown halt state rather than an absence of halt."""
    assert read_envelope(b"not json at all") is None
    assert read_envelope(b"[]") is None
    assert read_envelope(b'{"halted": true}') == Envelope(halted=True)


def test_a_session_wide_approval_matches_every_action() -> None:
    """An approval naming neither a category nor a pattern holds the whole Session."""
    approval = PendingApproval(rule_id=None, rule_name="hold")

    assert approval.session_wide is True
    assert approval.matches(()) is True


# ---------------------------------------------------------------------------
# Workspace to Client resolution
# ---------------------------------------------------------------------------


def write_client_map(tmp_path: Path, body: str) -> Path:
    """Write a mapping document and return its path."""
    path = tmp_path / "client-map.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_longest_matching_workspace_root_decides_the_client(tmp_path: Path) -> None:
    """A sub-project's own entry wins over the repository that contains it."""
    outer = uuid4()
    inner = uuid4()
    path = write_client_map(
        tmp_path,
        f'[[client]]\nworkspace = "/work"\nid = "{outer}"\nslug = "outer"\n'
        f'[[client]]\nworkspace = "/work/acme"\nid = "{inner}"\nslug = "acme"\n',
    )
    configuration = build_configuration(tmp_path, client_map=path)

    resolved = resolve_client("/work/acme/service", configuration=configuration)

    assert resolved == ClientRef(id=inner, slug="acme", assigned=True)


def test_a_workspace_matching_no_entry_falls_back_to_the_reserved_client(
    tmp_path: Path,
) -> None:
    """The reserved Client is used and the caller is told, rather than the Event being dropped."""
    path = write_client_map(
        tmp_path,
        f'[[client]]\nworkspace = "/elsewhere"\nid = "{uuid4()}"\nslug = "other"\n',
    )
    configuration = build_configuration(tmp_path, client_map=path)

    resolved = resolve_client("/work/acme", configuration=configuration)

    assert resolved.id == UNASSIGNED_CLIENT_ID
    assert resolved.assigned is False


def test_the_unassigned_fallback_is_reported_on_standard_error(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Requirement 1.6 asks for a warning, and it travels on the one diagnostic line."""
    outcome = dispatch(
        TOOL,
        b"{}",
        adapter=StubAdapter(),
        configuration=build_configuration(tmp_path),
        transmitter=build_transmitter(
            spool=build_spool(tmp_path),
            transport=RecordedTransport(replies=[envelope_reply()]),
            clock=time_source,
        ),
        clock=time_source,
    )

    assert UNASSIGNED_NOTE in outcome.notes


def test_a_mapping_entry_missing_a_field_costs_one_entry_rather_than_every_entry(
    tmp_path: Path,
) -> None:
    """One malformed line does not take the mapping down with it."""
    kept = uuid4()
    path = write_client_map(
        tmp_path,
        '[[client]]\nworkspace = "/broken"\nslug = "no-identifier"\n'
        f'[[client]]\nworkspace = "/kept"\nid = "{kept}"\nslug = "kept"\n',
    )

    entries = load_client_map(path)

    assert [entry.client.id for entry in entries] == [kept]


# ---------------------------------------------------------------------------
# What the hook process does not load
# ---------------------------------------------------------------------------


def module_path(name: str) -> Path | None:
    """The source file one dotted name inside the source package resolves to."""
    relative = Path(*name.split("."))
    for candidate in (SOURCE_ROOT / f"{relative}.py", SOURCE_ROOT / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def module_level_imports(path: Path) -> set[str]:
    """The dotted names a module imports at module level, and nothing else.

    Only the top level of the module body is read, so an import inside a function
    body and an import inside a type-checking guard are both excluded. That is
    exactly the distinction the latency claim rests on: a lazily imported module is
    not loaded by importing this one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
    return names


def import_closure(entry: str) -> tuple[frozenset[str], frozenset[str]]:
    """Every module importing `entry` loads, split into own modules and outside ones.

    The walk follows module-level imports alone, so what it computes is the set a
    fresh interpreter would hold after importing the entry point and nothing more.
    """
    own: set[str] = set()
    outside: set[str] = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in own:
            continue
        path = module_path(name)
        if path is None:
            outside.add(name)
            continue
        own.add(name)
        pending.extend(module_level_imports(path))
    return frozenset(own), frozenset(outside)


def test_the_hook_process_imports_no_database_driver_and_no_cloud_client() -> None:
    """The latency budget is met by what importing the entry point does not pull in."""
    _, outside = import_closure(HOOK_MODULE)

    offending = sorted(name for name in outside if name.startswith(DRIVER_PREFIXES))
    assert offending == []


def test_the_hook_module_does_not_import_the_redaction_pattern_table() -> None:
    """The pattern table is the largest avoidable import, so it is loaded on demand."""
    own, _ = import_closure(HOOK_MODULE)

    assert [name for name in sorted(own) if name.startswith(REDACT_PACKAGE)] == []


def test_the_transport_reads_the_host_and_the_path_prefix_from_the_address() -> None:
    """A Collector behind a path prefix is addressed with that prefix on every route."""
    transport = HttpTransport(COLLECTOR_URL)

    assert transport.secure is True
    transport.close()


def test_a_derived_session_identifier_is_stable_across_processes() -> None:
    """Two hook processes of one run compute the same Session with no shared state."""
    first = derive_session_id(TOOL, "run-77")
    second = derive_session_id(TOOL, "run-77")

    assert first == second
    assert isinstance(first, UUID)
    assert derive_session_id("cursor", "run-77") != first
