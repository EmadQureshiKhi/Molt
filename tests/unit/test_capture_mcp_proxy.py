"""The MCP proxy: byte-for-byte relay, linked Events, and the dropped-Event counter.

Every assertion here is about the one promise the component makes to both ends of a
conversation it sits inside: what came in is what goes out. The suite drives the real
forwarding loops, the real observation thread, a real spawned child over the stdio
transport, and a real socket listener over the HTTP transport, so the transparency
claim is exercised against the code that carries traffic rather than against a
description of it.

The observation side is driven without its thread wherever the assertion is about
what a frame produced: an observer that was never started processes its queue on the
calling thread when it is closed, which makes the Events a batch of frames produced
a deterministic value rather than a race. The thread is exercised where the assertion
is about the transports, which start it themselves.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import socket
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

import pytest

from molt.capture.mcp_proxy import (
    PROXY_STOPPED_REASON,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    UPSTREAM_CLOSED_REASON,
    Direction,
    EventSink,
    Framing,
    NullSink,
    Observer,
    build_http_server,
    forward_body,
    forward_frames,
    relay,
    request_key,
    run_stdio,
)
from molt.capture.protocol import UNASSIGNED_CLIENT, CaptureContext, Clock, derive_session_id
from molt.models.event import Event, EventCategory
from molt.redact import REDACTION_PLACEHOLDER, RedactionSettings

MACHINE: Final[str] = "machine-under-test"
AGENT: Final[str] = "claude_code"
SESSION_KEY: Final[str] = "a-proxy-session-key"
LOOPBACK: Final[str] = "127.0.0.1"
SOCKET_TIMEOUT_SECONDS: Final[float] = 5.0
OK: Final[int] = 200
BAD_GATEWAY: Final[int] = 502

# A child that reads its whole input, reports a digest of exactly what arrived, and
# then writes one line that is not a JSON document at all. The digest is what proves
# the agent-to-server direction was byte identical through a real process boundary.
CHILD_PROGRAM: Final[str] = (
    "import hashlib, sys\n"
    "raw = sys.stdin.buffer.read()\n"
    "out = sys.stdout.buffer\n"
    'out.write(b\'{"jsonrpc":"2.0","id":1,"result":{"digest":"\')\n'
    "out.write(hashlib.sha256(raw).hexdigest().encode())\n"
    "out.write(b'\"}}\\n')\n"
    "out.write(b'not a json document\\n')\n"
    "out.flush()\n"
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

# The instant every observed Event is placed at. Read from a fixed offset rather
# than from the host, so no run embeds a reading of the machine it ran on.
FIXED_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)


@dataclass(frozen=True, slots=True)
class FrozenClock:
    """A time source standing still, so a test states the instant rather than reads it."""

    def now(self) -> datetime:
        """The fixed instant every observed Event is placed at."""
        return FIXED_INSTANT

    def monotonic(self) -> float:
        """A fixed monotonic reading, which nothing under test subtracts."""
        return 0.0


@dataclass(slots=True)
class Recorder:
    """A byte target that keeps every byte written and counts the closes."""

    written: bytearray = field(default_factory=bytearray)
    closed: int = 0

    def write(self, data: bytes) -> int:
        """Keep the bytes and report them all written."""
        self.written += data
        return len(data)

    def flush(self) -> None:
        """Push nothing onward: everything is already kept."""

    def close(self) -> None:
        """Count the close, keeping what was written readable afterwards."""
        self.closed += 1

    @property
    def value(self) -> bytes:
        """Everything written so far."""
        return bytes(self.written)


@dataclass(slots=True)
class RecordingSink:
    """An Event destination that keeps what it was given, or refuses everything."""

    events: list[Event] = field(default_factory=list)
    closed: int = 0
    refusing: bool = False

    def emit(self, events: object) -> None:
        """Keep the batch, or refuse it the way an unreachable Collector would."""
        if self.refusing:
            raise OSError("the Event destination is unreachable")
        assert isinstance(events, (list, tuple))
        self.events.extend(events)

    def close(self) -> None:
        """Count the close."""
        self.closed += 1

    def categories(self) -> list[str]:
        """The category of every Event kept, in the order they were kept."""
        return [str(event.category) for event in self.events]

    def of(self, category: EventCategory) -> list[Event]:
        """Every kept Event of one category."""
        return [event for event in self.events if event.category is category]


@dataclass(slots=True)
class StubUpstream:
    """A socket listener answering one canned response per connection.

    A raw listener rather than a request handler, because what is under test is a
    byte sequence: recording the exact bytes that arrived is the assertion, and a
    framework that parsed them first would answer a different question.
    """

    response: bytes
    listener: socket.socket
    received: list[bytes] = field(default_factory=list)

    @classmethod
    def opened(cls, response: bytes) -> StubUpstream:
        """Bind a listener on an ephemeral loopback port."""
        listener = socket.create_server((LOOPBACK, 0))
        listener.settimeout(SOCKET_TIMEOUT_SECONDS)
        return cls(response=response, listener=listener)

    @property
    def address(self) -> str:
        """The address a proxy is pointed at."""
        host, port = self.listener.getsockname()[:2]
        return f"http://{host}:{port}"

    def serve_once(self) -> None:
        """Accept one connection, record the whole request, and answer it."""
        connection, _ = self.listener.accept()
        with connection:
            connection.settimeout(SOCKET_TIMEOUT_SECONDS)
            raw = _read_http_message(connection)
            self.received.append(raw)
            connection.sendall(self.response)

    def close(self) -> None:
        """Release the listener."""
        self.listener.close()


def _read_http_message(connection: socket.socket) -> bytes:
    """Read one HTTP message off a socket: its head, then its declared body."""
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        chunk = connection.recv(4096)
        if not chunk:
            return bytes(raw)
        raw += chunk
    head, _, body = bytes(raw).partition(b"\r\n\r\n")
    declared = 0
    for line in head.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            declared = int(value.strip())
    while len(body) < declared:
        chunk = connection.recv(4096)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def context(clock: Clock, *, disabled: bool = False) -> CaptureContext:
    """The identity every observed Event is built against."""
    return CaptureContext(
        session_id=derive_session_id("mcp_proxy", SESSION_KEY),
        client=UNASSIGNED_CLIENT,
        machine_id=MACHINE,
        agent_cli=AGENT,
        clock=clock,
        redaction=RedactionSettings(disabled=disabled),
    )


def observing(clock: Clock, sink: RecordingSink, **bounds: int) -> Observer:
    """An observer over a recording sink, with its thread left unstarted."""
    return Observer(ctx=context(clock), transport=TRANSPORT_STDIO, sink=sink, **bounds)


def frames(*payloads: bytes) -> bytes:
    """One newline-delimited stream carrying the given payloads."""
    return b"".join(payload + b"\n" for payload in payloads)


# ---------------------------------------------------------------------------
# The forwarding path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"{}",
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        b"not a json document at all",
        b"\x00\xff\xfe binary and not valid text",
        b"x" * 4096,
    ],
)
def test_relay_returns_the_same_object_for_every_frame(frame: bytes) -> None:
    """Forwarding is the identity function, so no input can be altered by it.

    Validates: Requirements 2.4
    """
    for direction in Direction:
        assert relay(frame, direction) is frame


def test_forward_frames_writes_exactly_what_it_read() -> None:
    """A mixed stream leaves the relay as the byte sequence it entered as.

    Validates: Requirements 2.4
    """
    sink = RecordingSink()
    clock_free = observing(FrozenClock(), sink)
    stream = (
        frames(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read"}}',
            b"not a json document",
            b"",
            b'[{"jsonrpc":"2.0","id":2,"method":"ping"},{"jsonrpc":"2.0","method":"notify"}]',
        )
        + b'{"jsonrpc":"2.0","id":3,"method":"unterminated"}'
    )
    target = Recorder()
    forward_frames(io.BytesIO(stream), target, Direction.TO_SERVER, clock_free)
    clock_free.close()
    assert target.value == stream


def test_forward_frames_preserves_a_carriage_return_terminator() -> None:
    """The terminator is forwarded as received rather than rewritten.

    Validates: Requirements 2.4
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    stream = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\r\n'
    target = Recorder()
    forward_frames(io.BytesIO(stream), target, Direction.TO_SERVER, watcher)
    watcher.close()
    assert target.value == stream
    assert sink.of(EventCategory.TOOL_CALL)[0].payload["method"] == "ping"


def test_forward_frames_relays_a_frame_above_the_observation_cap() -> None:
    """A frame larger than the copy the observer keeps is still relayed in full.

    Validates: Requirements 2.4, 2.5
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink, frame_cap=64)
    oversized = b'{"jsonrpc":"2.0","id":1,"method":"' + b"m" * 4096 + b'"}'
    stream = frames(oversized)
    target = Recorder()
    forward_frames(io.BytesIO(stream), target, Direction.TO_SERVER, watcher)
    watcher.close()
    assert target.value == stream
    assert sink.of(EventCategory.TOOL_CALL) == []
    assert watcher.dropped_events == 1


def test_forward_body_copies_a_length_delimited_body_through() -> None:
    """An entity body crosses the relay unchanged and is observed once.

    Validates: Requirements 2.4, 2.2
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    body = b'{"jsonrpc":"2.0","id":"a","method":"tools/call","params":{"path":"/tmp/x"}}'
    target = Recorder()
    forwarded = forward_body(
        io.BytesIO(body + b"trailing bytes nobody asked for"),
        target,
        Direction.TO_SERVER,
        watcher,
        remaining=len(body),
    )
    watcher.close()
    assert forwarded == len(body)
    assert target.value == body
    assert [event.payload["method"] for event in sink.of(EventCategory.TOOL_CALL)] == ["tools/call"]


def test_forward_body_observes_each_event_of_a_stream() -> None:
    """Event-stream framing is framing: the payloads inside it are the messages.

    Validates: Requirements 2.3, 2.4
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    body = (
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","id":7,"method":"ping"}\n'
        b"\n"
        b'data: {"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n'
        b"\n"
    )
    target = Recorder()
    forward_body(
        io.BytesIO(body),
        target,
        Direction.TO_AGENT,
        watcher,
        remaining=len(body),
        framing=Framing.EVENT_STREAM,
    )
    watcher.close()
    assert target.value == body
    call = sink.of(EventCategory.TOOL_CALL)[0]
    result = sink.of(EventCategory.TOOL_RESULT)[0]
    assert result.parent_event_id == call.id


# ---------------------------------------------------------------------------
# Linking by JSON-RPC identifier
# ---------------------------------------------------------------------------


def test_request_key_separates_the_identifier_types() -> None:
    """A textual identifier and a numeric one are different calls.

    Validates: Requirements 2.3
    """
    assert request_key("1") != request_key(1)
    assert request_key(1) == request_key(1.0)
    assert request_key(None) is None
    assert request_key(True) is None


def test_a_response_links_to_its_call_by_identifier() -> None:
    """The result Event names the call Event as its parent (Requirement 2.3).

    Validates: Requirements 2.2, 2.3
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    stream = frames(
        b'{"jsonrpc":"2.0","id":"a1","method":"tools/call","params":{"name":"read"}}',
        b'{"jsonrpc":"2.0","id":"a1","result":{"content":"a file"}}',
    )
    forward_frames(io.BytesIO(stream), Recorder(), Direction.TO_SERVER, watcher)
    watcher.close()
    call = sink.of(EventCategory.TOOL_CALL)[0]
    result = sink.of(EventCategory.TOOL_RESULT)[0]
    assert call.payload["method"] == "tools/call"
    assert call.payload["request_id"] == "a1"
    assert result.parent_event_id == call.id
    assert result.payload["linked"] is True


def test_a_textual_identifier_does_not_answer_a_numeric_one() -> None:
    """A response identified by text links to no call identified by a number.

    Validates: Requirements 2.3
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    stream = frames(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":"1","result":{}}',
    )
    forward_frames(io.BytesIO(stream), Recorder(), Direction.TO_SERVER, watcher)
    watcher.close()
    result = sink.of(EventCategory.TOOL_RESULT)[0]
    assert result.parent_event_id is None
    assert result.payload["linked"] is False


def test_a_notification_is_recorded_and_linked_to_by_nothing() -> None:
    """A message with no identifier is a call nothing can answer.

    Validates: Requirements 2.2
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    stream = frames(b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
    forward_frames(io.BytesIO(stream), Recorder(), Direction.TO_SERVER, watcher)
    watcher.close()
    call = sink.of(EventCategory.TOOL_CALL)[0]
    assert call.payload["notification"] is True
    assert call.payload["request_id"] is None


def test_a_batch_frame_produces_one_event_per_message() -> None:
    """A frame carrying several messages is observed as several Events.

    Validates: Requirements 2.2, 2.3
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    stream = frames(
        b'[{"jsonrpc":"2.0","id":1,"method":"ping"},{"jsonrpc":"2.0","method":"notifications/x"}]',
        b'[{"jsonrpc":"2.0","id":1,"result":{}}]',
    )
    forward_frames(io.BytesIO(stream), Recorder(), Direction.TO_SERVER, watcher)
    watcher.close()
    assert len(sink.of(EventCategory.TOOL_CALL)) == 2
    result = sink.of(EventCategory.TOOL_RESULT)[0]
    assert result.parent_event_id == sink.of(EventCategory.TOOL_CALL)[0].id


def test_the_identifier_map_is_bounded_by_its_maximum() -> None:
    """A session of unanswered calls costs a fixed amount of memory.

    Validates: Requirements 2.3
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink, pending_max=2)
    stream = frames(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":3,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":1,"result":{}}',
        b'{"jsonrpc":"2.0","id":3,"result":{}}',
    )
    forward_frames(io.BytesIO(stream), Recorder(), Direction.TO_SERVER, watcher)
    watcher.close()
    linked = {
        str(event.payload["request_id"]): event.payload["linked"]
        for event in sink.of(EventCategory.TOOL_RESULT)
    }
    assert linked == {"1": False, "3": True}


def test_a_parameter_naming_a_credential_is_redacted() -> None:
    """The parameters a call Event carries have passed the Redactor.

    Validates: Requirements 2.2
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    stream = frames(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"token":"abcd"}}')
    forward_frames(io.BytesIO(stream), Recorder(), Direction.TO_SERVER, watcher)
    watcher.close()
    call = sink.of(EventCategory.TOOL_CALL)[0]
    params = call.payload["params"]
    assert isinstance(params, dict)
    assert params["token"] == REDACTION_PLACEHOLDER
    assert call.redacted is True


# ---------------------------------------------------------------------------
# The dropped-Event counter
# ---------------------------------------------------------------------------


def test_a_body_that_is_not_json_is_counted_and_relayed() -> None:
    """A parse failure increments the counter and leaves the relay alone.

    Validates: Requirements 2.4, 2.5
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    stream = frames(b"not a json document", b"{oops", b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    target = Recorder()
    forward_frames(io.BytesIO(stream), target, Direction.TO_SERVER, watcher)
    watcher.close()
    assert target.value == stream
    assert watcher.dropped_events == 2
    assert len(sink.of(EventCategory.TOOL_CALL)) == 1


def test_a_json_document_that_is_no_message_is_counted() -> None:
    """A document that is neither a call nor a response produces no Event.

    Validates: Requirements 2.5
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    stream = frames(b"42", b'{"jsonrpc":"2.0"}', b'["not an object"]')
    forward_frames(io.BytesIO(stream), Recorder(), Direction.TO_SERVER, watcher)
    watcher.close()
    assert watcher.dropped_events == 3
    assert sink.categories() == [str(EventCategory.SESSION_END)]


def test_a_refused_destination_is_counted_and_the_relay_completes() -> None:
    """A transmission failure costs the Events and none of the traffic.

    Validates: Requirements 2.5
    """
    sink = RecordingSink(refusing=True)
    watcher = observing(FrozenClock(), sink)
    stream = frames(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":1,"result":{}}',
    )
    target = Recorder()
    forward_frames(io.BytesIO(stream), target, Direction.TO_SERVER, watcher)
    watcher.close()
    assert target.value == stream
    assert sink.events == []
    assert watcher.dropped_events == 3


def test_a_full_observation_queue_drops_rather_than_waits() -> None:
    """The relay's cost per frame does not depend on the observation side.

    Validates: Requirements 2.5
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink, queue_max=1)
    stream = frames(*[b'{"jsonrpc":"2.0","id":1,"method":"ping"}'] * 4)
    target = Recorder()
    forward_frames(io.BytesIO(stream), target, Direction.TO_SERVER, watcher)
    assert target.value == stream
    assert watcher.dropped_events >= 3


# ---------------------------------------------------------------------------
# The Session's end
# ---------------------------------------------------------------------------


def test_one_session_end_event_is_recorded_per_run() -> None:
    """A Session that was opened is ended exactly once, whatever ended it.

    Validates: Requirements 2.6
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    watcher.session_end(UPSTREAM_CLOSED_REASON)
    watcher.session_end("a second reason nobody should see")
    watcher.close()
    ends = sink.of(EventCategory.SESSION_END)
    assert len(ends) == 1
    assert ends[0].payload["reason"] == UPSTREAM_CLOSED_REASON
    assert sink.closed == 1


def test_a_run_that_nobody_ended_still_ends_its_session() -> None:
    """Closing the observer ends the Session when nothing else did.

    Validates: Requirements 2.6
    """
    sink = RecordingSink()
    watcher = observing(FrozenClock(), sink)
    watcher.close()
    ends = sink.of(EventCategory.SESSION_END)
    assert len(ends) == 1
    assert ends[0].payload["reason"] == PROXY_STOPPED_REASON


def test_the_null_destination_accepts_and_discards() -> None:
    """An unconfigured process relays and records nothing, without failing.

    Validates: Requirements 2.5
    """
    sink: EventSink = NullSink()
    watcher = Observer(ctx=context(FrozenClock()), transport=TRANSPORT_STDIO, sink=sink)
    stream = frames(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    target = Recorder()
    forward_frames(io.BytesIO(stream), target, Direction.TO_SERVER, watcher)
    watcher.close()
    assert target.value == stream
    assert watcher.dropped_events == 0


# ---------------------------------------------------------------------------
# The stdio transport
# ---------------------------------------------------------------------------


def test_run_stdio_relays_both_directions_byte_for_byte() -> None:
    """A real child receives exactly what the agent wrote, and answers through.

    The child reports a digest of every byte that reached it, so the assertion is
    over what crossed a process boundary rather than over what this process intended
    to send.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6
    """
    sink = RecordingSink()
    outgoing = frames(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read"}}',
        b"not a json document",
    )
    target = Recorder()
    watcher = Observer(ctx=context(FrozenClock()), transport=TRANSPORT_STDIO, sink=sink)
    status = run_stdio(
        [sys.executable, "-c", CHILD_PROGRAM],
        context(FrozenClock()),
        agent_in=io.BytesIO(outgoing),
        agent_out=target,
        observer=watcher,
    )
    watcher.close()

    assert status == 0
    assert target.closed == 1
    expected_digest = hashlib.sha256(outgoing).hexdigest()
    results = sink.of(EventCategory.TOOL_RESULT)
    assert len(results) == 1
    payload = results[0].payload
    assert isinstance(payload["result"], dict)
    assert payload["result"]["digest"] == expected_digest
    assert payload["transport"] == TRANSPORT_STDIO
    calls = sink.of(EventCategory.TOOL_CALL)
    assert [event.payload["method"] for event in calls] == ["tools/call"]
    assert results[0].parent_event_id == calls[0].id
    assert target.value.endswith(b"not a json document\n")
    ends = sink.of(EventCategory.SESSION_END)
    assert [event.payload["reason"] for event in ends] == [UPSTREAM_CLOSED_REASON]


def test_run_stdio_refuses_a_command_naming_no_executable() -> None:
    """A command that names nothing runnable is refused before anything is spawned.

    Validates: Requirements 2.1
    """
    with pytest.raises(ValueError, match="no executable"):
        run_stdio([], context(FrozenClock()), sink=NullSink())


# ---------------------------------------------------------------------------
# The HTTP transport
# ---------------------------------------------------------------------------


def test_run_http_relays_a_request_and_its_response_byte_for_byte() -> None:
    """The entity body crosses both hops unchanged, and the Events link.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4
    """
    request_body = b'{"jsonrpc":"2.0","id":"a1","method":"tools/call","params":{"name":"read"}}'
    response_body = b'{"jsonrpc":"2.0","id":"a1","result":{"content":"a file"}}'
    canned = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(response_body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + response_body
    )
    upstream = StubUpstream.opened(canned)
    sink = RecordingSink()
    watcher = Observer(ctx=context(FrozenClock()), transport=TRANSPORT_HTTP, sink=sink)
    server = build_http_server(
        upstream.address,
        f"{LOOPBACK}:0",
        context(FrozenClock()),
        observer=watcher,
    )
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    answering = threading.Thread(target=upstream.serve_once)
    try:
        serving.start()
        answering.start()
        host, port = server.server_address[:2]
        client = http.client.HTTPConnection(str(host), int(port), timeout=SOCKET_TIMEOUT_SECONDS)
        try:
            client.request(
                "POST",
                "/mcp",
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer a-tool-server-value",
                },
            )
            response = client.getresponse()
            returned = response.read()
        finally:
            client.close()
        answering.join(SOCKET_TIMEOUT_SECONDS)
    finally:
        server.shutdown()
        serving.join(SOCKET_TIMEOUT_SECONDS)
        server.server_close()
        upstream.close()
        watcher.close()

    assert response.status == OK
    assert returned == response_body
    assert len(upstream.received) == 1
    head, _, body = upstream.received[0].partition(b"\r\n\r\n")
    assert body == request_body
    assert b"Authorization: Bearer a-tool-server-value" in head
    assert f"Content-Length: {len(request_body)}".encode() in head
    assert b"Transfer-Encoding" not in head
    call = sink.of(EventCategory.TOOL_CALL)[0]
    result = sink.of(EventCategory.TOOL_RESULT)[0]
    assert call.payload["transport"] == TRANSPORT_HTTP
    assert call.payload["direction"] == str(Direction.TO_SERVER)
    assert result.payload["direction"] == str(Direction.TO_AGENT)
    assert result.parent_event_id == call.id


def test_run_http_closes_the_agent_connection_when_the_upstream_is_gone() -> None:
    """An upstream that refuses the connection ends the Session and the connection.

    Validates: Requirements 2.6
    """
    closed = socket.create_server((LOOPBACK, 0))
    unreachable = f"http://{LOOPBACK}:{closed.getsockname()[1]}"
    closed.close()

    sink = RecordingSink()
    watcher = Observer(ctx=context(FrozenClock()), transport=TRANSPORT_HTTP, sink=sink)
    server = build_http_server(
        unreachable,
        f"{LOOPBACK}:0",
        context(FrozenClock()),
        observer=watcher,
    )
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    try:
        serving.start()
        host, port = server.server_address[:2]
        client = http.client.HTTPConnection(str(host), int(port), timeout=SOCKET_TIMEOUT_SECONDS)
        try:
            client.request("POST", "/mcp", body=b"{}")
            response = client.getresponse()
            response.read()
        finally:
            client.close()
    finally:
        server.shutdown()
        serving.join(SOCKET_TIMEOUT_SECONDS)
        server.server_close()
        watcher.close()

    assert response.status == BAD_GATEWAY
    ends = sink.of(EventCategory.SESSION_END)
    assert [event.payload["reason"] for event in ends] == [UPSTREAM_CLOSED_REASON]
