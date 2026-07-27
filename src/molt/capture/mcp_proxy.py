"""The MCP proxy: a transparent relay that observes a copy of what it forwards.

An agent tool speaks to a tool server through this process instead of speaking to
it directly, and neither end is modified to permit that. The whole value of the
component is that it changes nothing about the conversation, so the design
question is not *how does it forward* but *how is forwarding made incapable of
being affected by observing*. Six claims arrange the module.

**The forwarding path is one function that reads nothing but its argument.**
`relay` returns the byte sequence it was given. It holds no state, consults no
observer, takes no lock, and cannot raise, so the bytes written downstream are
the bytes read upstream by construction rather than by inspection (Requirement
2.4). The direction travels with the call because a caller finds it useful to
state which way a frame is going, and it is discarded unread, so the returned
value depends on the frame alone.

**Observation happens after the forwarding write has returned, on a separate
thread, and the observing side holds no reference to either stream.** The relay
threads hand a frame copy to a bounded queue and go straight back to reading. The
worker that parses the copy, builds Events, and transmits them was constructed
with a Session context and an Event sink and with no stream handle at all, so
there is no channel through which a parse failure, an Event that could not be
built, or a transmission that could not be placed could reach the forwarded bytes
(Requirement 2.5). The handler that contains those failures is the last line of
that defence rather than the whole of it: were the handler deleted, the failing
thread would die without a stream to disturb.

**A full queue is a dropped Event rather than a stalled relay.** The queue is
bounded, the offer never waits, and a rejected offer increments the dropped-Event
counter. That is what keeps a slow Collector or a slow tool server from spending
the agent's latency: the relay's cost per frame is one write and one non-blocking
put, whatever the observation side is doing. The counter is reported as one
undimensioned measurement, matching the spool's precedent, because a dimension
here would multiply into the billable metric bound for no diagnostic gain.

**The observation copy is capped and the cap cannot reach the relay.** A frame
larger than the cap is still forwarded byte for byte, because the forwarding loop
writes each chunk as it is read and never consults the cap; what the cap bounds is
the copy retained for parsing, and a frame above it is counted as a dropped Event
rather than parsed. The relay therefore holds one chunk plus one capped copy per
direction however large a frame is.

**Request and response Events are linked by the JSON-RPC identifier, through a
map that cannot grow without bound.** A call Event's identifier is remembered
under a key derived from the message identifier and its JSON type, so the text
`"1"` and the number `1` are different keys; a response takes the entry rather
than reading it, and the map evicts its oldest entry once it holds more than the
configured maximum, so a long session with abandoned calls costs a fixed amount of
memory (Requirements 2.2, 2.3). A notification carries no identifier and a null
identifier identifies nothing, so neither is remembered and neither is linked to.

**Every ingest request the proxy makes is signed, and the signing is not restated
here.** Events reach the Collector through the same transmitter the hook uses, so
the spool-first ordering, the retry schedule, the per-operation cap, and the
timestamp and signature headers are the ones already written and verified rather
than a second implementation of them (Requirement 47.10). A transmitter is built
per batch, because its soft deadline is measured from construction and a
long-running process needs that budget afresh for each batch rather than once.

Nothing here imports a database driver: the proxy runs beside the agent on an
engineer machine, which holds no cluster credential.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import queue
import shutil
import socket
import socketserver
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from molt.capture.hook import HttpTransport, Transmitter, Transport
from molt.capture.protocol import CaptureContext
from molt.capture.spool import Spool, resolve_machine_id
from molt.config.resolve import ConfigError, Configuration, load_configuration
from molt.models.event import Event, EventCategory, JsonObject, JsonValue
from molt.models.session import SessionOutcome
from molt.redact import RedactionSettings, redact_payload
from molt.telemetry import Severity, log, metric

__all__ = [
    "COMPONENT",
    "DROPPED_EVENT_METRIC",
    "EMIT_BATCH_MAX",
    "EVENT_DATA_FIELD",
    "EVENT_SEPARATOR",
    "FRAME_SEPARATOR",
    "HOP_BY_HOP_HEADERS",
    "OBSERVATION_QUEUE_MAX",
    "OBSERVED_FRAME_MAX_BYTES",
    "PENDING_CALL_MAX",
    "PROXY_STOPPED_REASON",
    "READ_CHUNK_BYTES",
    "RELAY_FAILED_REASON",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "UPSTREAM_CLOSED_REASON",
    "ByteSource",
    "ByteTarget",
    "CollectorSink",
    "Direction",
    "DroppedEvents",
    "EventSink",
    "Framing",
    "NullSink",
    "Observer",
    "ProxyServer",
    "SpoolSink",
    "build_http_server",
    "forward_body",
    "forward_frames",
    "relay",
    "request_key",
    "resolve_sink",
    "run_http",
    "run_stdio",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "capture"

# What an Event records as the transport it was observed on.
TRANSPORT_STDIO: Final[str] = "stdio"
TRANSPORT_HTTP: Final[str] = "http"

# The undimensioned counter of Events the observation side could not produce or
# place (Requirement 2.5). Undimensioned for the reason the spool's counters are:
# a dimension would multiply into the billable metric bound, and the identities
# belong in the log record instead.
DROPPED_EVENT_METRIC: Final[str] = "capture.mcp_dropped_events"

# The stdio framing. A message occupies one line, so the newline is the framing
# and everything before it is the payload.
FRAME_SEPARATOR: Final[bytes] = b"\n"
CARRIAGE_RETURN: Final[bytes] = b"\r"

# The event-stream framing an HTTP response may carry. One event ends at a blank
# line, and the payload is the concatenation of that event's data fields.
EVENT_SEPARATOR: Final[bytes] = b"\n\n"
EVENT_DATA_FIELD: Final[bytes] = b"data:"

# How much is moved per read. Bounded so that a frame of any size costs the relay
# one chunk of memory rather than its own length.
READ_CHUNK_BYTES: Final[int] = 65536

# The most of one frame that is retained for parsing. This bounds the observation
# copy alone: the forwarding loop writes every chunk it reads and never consults
# it, so a frame above the cap is relayed in full and observed not at all.
OBSERVED_FRAME_MAX_BYTES: Final[int] = 1048576

# How many frame copies may await observation. A full queue is a dropped Event
# rather than a stalled relay, which is what keeps the relay's per-frame cost
# independent of how long observation takes.
OBSERVATION_QUEUE_MAX: Final[int] = 256

# How many unanswered calls the identifier map remembers, and how many
# observations are turned into one Event batch.
PENDING_CALL_MAX: Final[int] = 1024
EMIT_BATCH_MAX: Final[int] = 64

# How long a caller waits for the observation thread to finish its queue.
WORKER_JOIN_SECONDS: Final[float] = 5.0

# The reasons a session end Event carries. Exactly one is emitted per run: the
# first of these to be reported wins, so a Session is never ended twice.
UPSTREAM_CLOSED_REASON: Final[str] = "the MCP server closed the connection"
RELAY_FAILED_REASON: Final[str] = "the relay could not continue"
PROXY_STOPPED_REASON: Final[str] = "the proxy stopped"

# Headers that describe one hop of a connection rather than the message, so a
# relay presents its own rather than passing another's on.
HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# The host header names the connection rather than the message as well: the
# upstream address is this process's own, so the value is recomputed rather than
# forwarded.
_REQUEST_ONLY_DROPPED: Final[frozenset[str]] = frozenset({"host", "content-length"})

CONTENT_LENGTH_HEADER: Final[str] = "Content-Length"
TRANSFER_ENCODING_HEADER: Final[str] = "Transfer-Encoding"
CONTENT_TYPE_HEADER: Final[str] = "Content-Type"
CONNECTION_HEADER: Final[str] = "Connection"
CHUNKED_ENCODING: Final[str] = "chunked"
CLOSE_CONNECTION: Final[str] = "close"
EVENT_STREAM_MEDIA_TYPE: Final[str] = "text/event-stream"

# The longest chunk-size or trailer line a chunked body may present, so a body
# that never ends a line does not become an unbounded read.
CHUNK_HEADER_MAX_BYTES: Final[int] = 4096

# The statuses this process answers with on its own behalf. Everything else is
# the upstream's own status, forwarded unchanged.
BAD_REQUEST: Final[int] = 400
BAD_GATEWAY: Final[int] = 502

# The status a run reports, and the base a signal-terminated child is reported
# above, following the shell's own convention.
EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
SIGNAL_EXIT_BASE: Final[int] = 128

# The JSON-RPC fields the observation side reads. It reads no others, because it
# is deciding what kind of message this is rather than validating it.
METHOD_FIELD: Final[str] = "method"
ID_FIELD: Final[str] = "id"
PARAMS_FIELD: Final[str] = "params"
RESULT_FIELD: Final[str] = "result"
ERROR_FIELD: Final[str] = "error"

# The loopback host names a bind address is compared against, for the record that
# warns about a listener reachable from off the machine.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})

# What an absent field is distinguished by. A JSON-RPC response may carry a null
# identifier, which is a value, so absence cannot be expressed as None.
_ABSENT: Final[object] = object()


class Direction(StrEnum):
    """Which way a frame is travelling, as an Event payload records it."""

    TO_SERVER = "to_server"
    TO_AGENT = "to_agent"


class Framing(StrEnum):
    """How the payloads of a body are delimited inside it.

    `BODY` means the whole body is one payload, which is the JSON-RPC-over-HTTP
    case. `EVENT_STREAM` means the body carries a sequence of events, each ending
    at a blank line, whose data fields form the payload.
    """

    BODY = "body"
    EVENT_STREAM = "event_stream"


# ---------------------------------------------------------------------------
# The forwarding path
# ---------------------------------------------------------------------------


def relay(frame: bytes, direction: Direction) -> bytes:
    """Return the frame unchanged: the whole of what forwarding does.

    Every byte this process forwards passes through here, and nothing else does.
    The body reads no module state, calls nothing, and cannot raise, so the result
    is the argument for every input including a body that is not JSON and a frame
    larger than any bound this module applies elsewhere (Requirement 2.4).

    The direction is part of the call so that a caller states what it is doing
    rather than leaving it to the reader, and it is discarded here unread, which
    is what makes the returned value a function of the frame alone.
    """
    del direction
    return frame


def request_key(identifier: JsonValue) -> str | None:
    """The map key one JSON-RPC identifier is remembered under, or None.

    The JSON type participates in the key, so the text `"1"` and the number `1`
    are different calls and a response to one does not claim the other's Event as
    its parent. A whole-valued number is normalised to its integer form, so a
    client that sends `1` and a server that answers `1.0` still link.

    None means *not linkable*: a notification carries no identifier at all, a null
    identifier identifies no request by the protocol's own account, and a boolean
    is not an identifier any client should have sent.
    """
    if identifier is None or isinstance(identifier, bool):
        return None
    if isinstance(identifier, str):
        return f"s:{identifier}"
    if isinstance(identifier, int):
        return f"n:{identifier}"
    if isinstance(identifier, float):
        if identifier.is_integer():
            return f"n:{int(identifier)}"
        return f"n:{identifier!r}"
    return None


def _strip_framing(payload: bytes) -> bytes:
    """Remove one line terminator from a stdio frame, leaving the payload.

    One terminator rather than every trailing byte that resembles one: the framing
    is a single newline, optionally preceded by a carriage return, and anything
    further belongs to the message.
    """
    if payload.endswith(FRAME_SEPARATOR):
        payload = payload[: -len(FRAME_SEPARATOR)]
        if payload.endswith(CARRIAGE_RETURN):
            payload = payload[: -len(CARRIAGE_RETURN)]
    return payload


def _event_payload(block: bytes) -> bytes:
    """The payload one event-stream event carries, or empty when it carries none.

    An event's data fields are joined with a newline, which is what the stream
    format's own reassembly rule states, and every other field is framing.
    """
    lines: list[bytes] = []
    for line in block.split(FRAME_SEPARATOR):
        stripped = line.rstrip(CARRIAGE_RETURN)
        if not stripped.startswith(EVENT_DATA_FIELD):
            continue
        value = stripped[len(EVENT_DATA_FIELD) :]
        lines.append(value[1:] if value.startswith(b" ") else value)
    return FRAME_SEPARATOR.join(lines)


# ---------------------------------------------------------------------------
# Where observed Events go
# ---------------------------------------------------------------------------


class EventSink(Protocol):
    """The one call the observation side makes on a destination for Events.

    Declared structurally so that a test drives a recording sink, the delivered
    sink reaches the Collector, and neither the observer nor the transports know
    which of the two they hold.
    """

    def emit(self, events: Sequence[Event]) -> None:
        """Accept a batch of Events, in the order they were observed."""

    def close(self) -> None:
        """Release whatever the sink holds open."""


@dataclass(frozen=True, slots=True)
class NullSink:
    """A sink that accepts and discards, for a process Molt is not configured in."""

    def emit(self, events: Sequence[Event]) -> None:
        """Discard the batch."""

    def close(self) -> None:
        """Release nothing."""


@dataclass(frozen=True, slots=True)
class SpoolSink:
    """A sink appending Events to the bounded local spool.

    This is what an unconfigured Collector address resolves to. The spool holds
    records rather than prepared requests, so the next hook invocation on this
    machine claims them and signs them with a fresh timestamp, however long they
    sat in the file.
    """

    spool: Spool

    def emit(self, events: Sequence[Event]) -> None:
        """Append the batch to this machine's spool file."""
        self.spool.append(events)

    def close(self) -> None:
        """Release nothing: the spool holds no open file between calls."""


class CollectorSink:
    """A sink placing Events with the Collector through the hook's own transmitter.

    The transmitter is built per batch rather than held, because its soft deadline
    is measured from construction: a proxy runs for the length of an agent session,
    so a transmitter held from start-up would report its deadline elapsed for every
    batch after the first and spool them all. The spool and the connection *are*
    held, so the batches share one spool and one established connection, and the
    signing, the retry schedule, and the per-operation cap are the transmitter's
    rather than restated here (Requirement 47.10).
    """

    __slots__ = ("_configuration", "_spool", "_transport")

    def __init__(
        self,
        *,
        configuration: Configuration,
        spool: Spool,
        transport: Transport,
    ) -> None:
        self._configuration = configuration
        self._spool = spool
        self._transport = transport

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> CollectorSink:
        """Build a sink for the configured Collector address and spool directory."""
        machine_id = resolve_machine_id(configuration)
        return cls(
            configuration=configuration,
            spool=Spool.from_configuration(configuration, machine_id=machine_id),
            transport=HttpTransport.from_configuration(configuration),
        )

    @property
    def spool(self) -> Spool:
        """The spool a batch that could not be placed is buffered into."""
        return self._spool

    def emit(self, events: Sequence[Event]) -> None:
        """Place the batch, spooling whatever the Collector did not accept."""
        if not events:
            return
        Transmitter.from_configuration(
            self._configuration,
            spool=self._spool,
            transport=self._transport,
        ).emit(events)

    def close(self) -> None:
        """Release the connection the batches shared."""
        self._transport.close()


def resolve_sink(configuration: Configuration | None = None) -> EventSink:
    """The destination Events reach, chosen from what configuration names.

    A configured Collector address yields the transmitting sink. An unconfigured
    one yields the spool, because Events that reach a file are Events a later hook
    invocation transmits, whereas Events that reach nothing are lost. A
    configuration that cannot be read at all yields the discarding sink and one log
    record, because a proxy is not entitled to refuse to relay traffic over the
    state of its own configuration.
    """
    try:
        resolved = load_configuration() if configuration is None else configuration
        if resolved.optional_text("MOLT_COLLECTOR_URL") is None:
            return SpoolSink(
                Spool.from_configuration(resolved, machine_id=resolve_machine_id(resolved))
            )
        return CollectorSink.from_configuration(resolved)
    except (ConfigError, OSError, ValueError) as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "no destination for observed Events could be resolved, so the proxy only relays",
            error_type=type(error).__name__,
        )
        return NullSink()


# ---------------------------------------------------------------------------
# The dropped-Event counter
# ---------------------------------------------------------------------------


class DroppedEvents:
    """The count of Events the observation side could not produce or place.

    Several threads report into it — each relay thread when the observation queue
    is full, and the observation thread when a copy could not be parsed, an Event
    could not be built, or a batch could not be placed — so the increment is
    guarded. The guard is held for one addition and never while any input, output,
    or parsing happens, and this object holds no reference to any stream, so a
    thread reporting a drop can delay no forwarding write and reach no forwarded
    byte.
    """

    __slots__ = ("_count", "_lock")

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def increment(self, count: int = 1) -> None:
        """Add to the count, ignoring a non-positive addition."""
        if count <= 0:
            return
        with self._lock:
            self._count += count

    @property
    def count(self) -> int:
        """How many Events have been dropped since this counter was created."""
        with self._lock:
            return self._count


# ---------------------------------------------------------------------------
# What the relay hands over
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Frame:
    """One forwarded frame, as the observation side receives it.

    Attributes:
        payload: The retained copy, which is at most the observation cap and is an
            immutable value, so nothing the observation side does could alter what
            was forwarded even if it held the same object.
        length: What the frame actually measured, which exceeds the payload's
            length exactly when the frame was above the cap.
        direction: Which way the frame was travelling.
    """

    payload: bytes
    length: int
    direction: Direction


@dataclass(frozen=True, slots=True)
class _SessionEnd:
    """The instruction to end the Session, carrying the reason it ended."""

    reason: str


# ---------------------------------------------------------------------------
# The observation side
# ---------------------------------------------------------------------------


class Observer:
    """Turns copies of forwarded frames into Events, on a thread of its own.

    The constructor takes a Session context and a destination for Events, and
    takes no stream. That is the structural half of Requirement 2.5: there is no
    handle here through which a failure could reach the relay, so the containment
    handlers below decide only whether this thread survives a bad frame, never
    whether the frame was forwarded.

    Everything the observation state consists of — the identifier map, the
    counters, and the Session's end — is touched by this one thread alone, so none
    of it is guarded. The two members other threads touch are the queue, which is
    built for it, and the dropped-Event counter, which guards itself.
    """

    __slots__ = (
        "_batch_max",
        "_calls",
        "_context",
        "_dropped",
        "_end_lock",
        "_ended",
        "_frame_cap",
        "_frames",
        "_pending",
        "_pending_max",
        "_queue",
        "_redaction",
        "_results",
        "_sink",
        "_transport",
        "_worker",
    )

    def __init__(
        self,
        *,
        ctx: CaptureContext,
        transport: str,
        sink: EventSink | None = None,
        frame_cap: int = OBSERVED_FRAME_MAX_BYTES,
        queue_max: int = OBSERVATION_QUEUE_MAX,
        pending_max: int = PENDING_CALL_MAX,
        batch_max: int = EMIT_BATCH_MAX,
    ) -> None:
        if frame_cap < 1 or queue_max < 1 or pending_max < 1 or batch_max < 1:
            raise ValueError("every observation bound must admit at least one item")
        self._context = ctx
        self._transport = transport
        self._sink: EventSink = resolve_sink() if sink is None else sink
        self._frame_cap = frame_cap
        self._pending_max = pending_max
        self._batch_max = batch_max
        self._redaction = RedactionSettings() if ctx.redaction is None else ctx.redaction
        self._queue: queue.Queue[_Frame | _SessionEnd | None] = queue.Queue(maxsize=queue_max)
        self._pending: OrderedDict[str, UUID] = OrderedDict()
        self._dropped = DroppedEvents()
        self._frames = 0
        self._calls = 0
        self._results = 0
        self._ended = False
        self._end_lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # -- what a caller reads ---------------------------------------------

    @property
    def frame_cap(self) -> int:
        """The most of one frame that is retained for parsing."""
        return self._frame_cap

    @property
    def sink(self) -> EventSink:
        """Where the Events this observer builds are placed."""
        return self._sink

    @property
    def dropped_events(self) -> int:
        """How many Events could not be produced or placed (Requirement 2.5)."""
        return self._dropped.count

    # -- the relay side --------------------------------------------------

    def start(self) -> None:
        """Start the observation thread, if it is not already running."""
        if self._worker is not None:
            return
        worker = threading.Thread(target=self._work, name="molt-mcp-observer", daemon=True)
        self._worker = worker
        worker.start()

    def offer(self, payload: bytes, length: int, direction: Direction) -> None:
        """Hand one frame copy over for observation, or count it as dropped.

        Called from a relay thread once the forwarding write has returned. It never
        waits: a full queue means the observation side is behind, and the answer to
        that is a dropped Event rather than a paused relay.
        """
        try:
            self._queue.put_nowait(_Frame(payload=payload, length=length, direction=direction))
        except queue.Full:
            self._dropped.increment()

    def session_end(self, reason: str) -> None:
        """Ask for the session end Event, which is emitted once per run.

        The first reason reported wins, so an upstream close followed by a stop
        records the close, which is the cause the Session ended for (Requirement
        2.6). The claim on the Session's end is taken under a lock because the HTTP
        transport serves each exchange on a thread of its own and two of them may
        observe the tool server's departure at once; the lock is off the per-frame
        path, which is claimed once per run rather than once per frame.
        """
        with self._end_lock:
            if self._ended:
                return
            self._ended = True
        try:
            self._queue.put_nowait(_SessionEnd(reason=reason))
        except queue.Full:
            self._dropped.increment()

    def close(self, timeout: float = WORKER_JOIN_SECONDS) -> None:
        """Finish the queue, publish the dropped count, and release the sink.

        A run that ended for a reason nobody reported still ends its Session, so
        exactly one session end Event is produced per run whatever the cause.
        """
        self.session_end(PROXY_STOPPED_REASON)
        worker = self._worker
        if worker is not None:
            try:
                # A wait is safe here and nowhere else: the thread this is waiting
                # for is the one that empties the queue, and no relay thread ever
                # reaches this call.
                self._queue.put(None, timeout=timeout)
            except queue.Full:
                self._dropped.increment()
            worker.join(timeout)
            self._worker = None
        else:
            self._drain()
        self._publish()
        try:
            self._sink.close()
        except Exception as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "the Event destination could not be released cleanly",
                error_type=type(error).__name__,
            )

    def __enter__(self) -> Observer:
        """Start the observation thread and return this observer."""
        self.start()
        return self

    def __exit__(self, *_details: object) -> None:
        """Finish the queue and release the sink."""
        self.close()

    # -- the observation thread ------------------------------------------

    def _work(self) -> None:
        """Take observations off the queue in batches until the sentinel arrives."""
        stopping = False
        while not stopping:
            items: list[_Frame | _SessionEnd] = []
            item = self._queue.get()
            while True:
                if item is None:
                    stopping = True
                    break
                items.append(item)
                if len(items) >= self._batch_max:
                    break
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
            self._process(items)

    def _drain(self) -> None:
        """Observe whatever is queued on the calling thread, for a stopped worker."""
        items: list[_Frame | _SessionEnd] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                items.append(item)
        self._process(items)

    def _process(self, items: Sequence[_Frame | _SessionEnd]) -> None:
        """Build the Events a batch of observations produced and place them."""
        events: list[Event] = []
        for item in items:
            events.extend(self._build(item))
        if events:
            self._emit(events)

    def _build(self, item: _Frame | _SessionEnd) -> list[Event]:
        """Build the Events one observation produced, counting a failure as a drop.

        This is the containment handler, and it is the last line of the defence
        rather than the whole of it: the frame it is reading has already been
        forwarded, and this thread holds nothing that could unforward it.
        """
        try:
            if isinstance(item, _SessionEnd):
                return [self._session_end_event(item.reason)]
            return self._frame_events(item)
        except Exception as error:
            self._dropped.increment()
            log(
                Severity.WARNING,
                COMPONENT,
                "an observed frame produced no Event, and the relay is unaffected",
                error_type=type(error).__name__,
            )
            return []

    def _emit(self, events: Sequence[Event]) -> None:
        """Place a batch, counting every Event of a failed placement as dropped."""
        try:
            self._sink.emit(events)
        except Exception as error:
            self._dropped.increment(len(events))
            log(
                Severity.WARNING,
                COMPONENT,
                "observed Events could not be placed, and the relay is unaffected",
                event_count=len(events),
                error_type=type(error).__name__,
            )

    def _publish(self) -> None:
        """Report the dropped count as one undimensioned measurement."""
        dropped = self._dropped.count
        if not dropped:
            return
        metric(DROPPED_EVENT_METRIC, float(dropped))
        log(
            Severity.WARNING,
            COMPONENT,
            "observed frames produced no Event during this run",
            dropped=dropped,
            frames=self._frames,
        )

    # -- one frame -------------------------------------------------------

    def _frame_events(self, frame: _Frame) -> list[Event]:
        """The Events one frame copy produced, which may be none.

        A frame above the cap is not parsed, because the copy is incomplete by
        construction and half a document is not a message. It is counted rather
        than guessed at, and it has already been relayed in full.
        """
        self._frames += 1
        if frame.length > self._frame_cap:
            self._dropped.increment()
            log(
                Severity.WARNING,
                COMPONENT,
                "a frame above the observation cap was relayed in full and not parsed",
                frame_bytes=frame.length,
                observation_cap=self._frame_cap,
                direction=str(frame.direction),
            )
            return []
        payload = _strip_framing(frame.payload)
        if not payload.strip():
            return []
        decoded: object = json.loads(payload)
        messages = decoded if isinstance(decoded, list) else [decoded]
        events: list[Event] = []
        for message in messages:
            if not isinstance(message, Mapping):
                self._dropped.increment()
                continue
            built = self._message_event({str(key): value for key, value in message.items()}, frame)
            if built is None:
                self._dropped.increment()
                continue
            events.append(built)
        return events

    def _message_event(self, message: Mapping[str, object], frame: _Frame) -> Event | None:
        """The Event one JSON-RPC message produced, or None when it is neither kind.

        A message naming a method is a call, whether or not it carries an
        identifier, so a notification is recorded and simply has nothing that could
        later link to it. A message carrying a result or an error is a response,
        and it takes its parent out of the identifier map rather than reading it,
        so one call is answered once.
        """
        method = message.get(METHOD_FIELD)
        identifier = message.get(ID_FIELD, _ABSENT)
        if isinstance(method, str):
            return self._call_event(method, identifier, message.get(PARAMS_FIELD), frame)
        if identifier is not _ABSENT and (RESULT_FIELD in message or ERROR_FIELD in message):
            return self._result_event(identifier, message, frame)
        return None

    def _call_event(
        self,
        method: str,
        identifier: object,
        params: object,
        frame: _Frame,
    ) -> Event:
        """The tool call Event a request or a notification produced.

        The method name, the request identifier, and the parameters after redaction
        are what the Event carries (Requirement 2.2). The identifier is remembered
        so that the response can name this Event as its parent, unless the message
        carried nothing that could identify it.
        """
        self._calls += 1
        notification = identifier is _ABSENT
        carried = None if notification else _as_json(identifier)
        key = request_key(carried)
        payload: JsonObject = {
            "transport": self._transport,
            "direction": str(frame.direction),
            "frame_bytes": frame.length,
            "method": method,
            "request_id": carried,
            "notification": notification,
            PARAMS_FIELD: _as_json(params),
        }
        event = self._event(EventCategory.TOOL_CALL, payload, parent_event_id=None)
        if key is not None:
            self._remember(key, event.id)
        return event

    def _result_event(
        self,
        identifier: object,
        message: Mapping[str, object],
        frame: _Frame,
    ) -> Event:
        """The tool result Event a response produced, linked to its call.

        The link is the JSON-RPC identifier carried through `parent_event_id`
        (Requirement 2.3). A response to a call this process never saw links to
        nothing, which is what a proxy attached mid-conversation produces and is
        recorded as such rather than guessed at.
        """
        self._results += 1
        carried = _as_json(identifier)
        key = request_key(carried)
        parent = None if key is None else self._pending.pop(key, None)
        payload: JsonObject = {
            "transport": self._transport,
            "direction": str(frame.direction),
            "frame_bytes": frame.length,
            "request_id": carried,
            "linked": parent is not None,
        }
        if RESULT_FIELD in message:
            payload[RESULT_FIELD] = _as_json(message.get(RESULT_FIELD))
        if ERROR_FIELD in message:
            payload[ERROR_FIELD] = _as_json(message.get(ERROR_FIELD))
        return self._event(EventCategory.TOOL_RESULT, payload, parent_event_id=parent)

    def _session_end_event(self, reason: str) -> Event:
        """The session end Event a closed connection produced (Requirement 2.6)."""
        payload: JsonObject = {
            "transport": self._transport,
            "outcome": str(SessionOutcome.SUCCEEDED),
            "reason": reason,
            "frames_observed": self._frames,
            "tool_call_count": self._calls,
            "tool_result_count": self._results,
            "dropped_event_count": self._dropped.count,
        }
        return self._event(EventCategory.SESSION_END, payload, parent_event_id=None)

    def _remember(self, key: str, event_id: UUID) -> None:
        """Remember one call under its identifier, evicting the oldest if needed.

        The eviction is what bounds the map over a session whose calls are never
        answered: the map holds the most recent unanswered calls and nothing else,
        so its cost is fixed rather than proportional to the session's length.
        """
        self._pending[key] = event_id
        self._pending.move_to_end(key)
        while len(self._pending) > self._pending_max:
            self._pending.popitem(last=False)

    def _event(
        self,
        category: EventCategory,
        payload: JsonObject,
        *,
        parent_event_id: UUID | None,
    ) -> Event:
        """Redact one payload and build the Event carrying it."""
        result = redact_payload(
            payload,
            session_id=self._context.session_id,
            settings=self._redaction,
        )
        if result.warning is not None:
            log(
                Severity.WARNING,
                COMPONENT,
                "redaction is disabled, so this Session's frames are recorded unmodified",
                record=result.warning.record,
                session_id=str(self._context.session_id),
            )
        return Event(
            id=uuid4(),
            session_id=self._context.session_id,
            client_id=self._context.client.id,
            category=category,
            occurred_at=self._context.clock.now(),
            agent_cli=self._context.agent_cli,
            machine_id=self._context.machine_id,
            parent_event_id=parent_event_id,
            payload=result.payload,
            redacted=result.modified,
            text_body=None,
        )


def _as_json(value: object) -> JsonValue:
    """Narrow a decoded JSON value to the shape an Event payload holds.

    Every value here came from a JSON document, so the narrowing is a statement of
    that fact rather than a conversion. Anything else is rendered as its type name,
    which cannot happen from a parsed document and costs nothing to be certain of.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _as_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_json(item) for item in value]
    return f"<{type(value).__name__}>"


# ---------------------------------------------------------------------------
# The two forwarding loops
# ---------------------------------------------------------------------------


class ByteSource(Protocol):
    """A readable byte stream, declared structurally.

    Structural rather than nominal because the three things read from here are a
    process pipe, a request stream, and a response stream, and no base class spans
    all three.
    """

    def read(self, size: int = ..., /) -> bytes:
        """Read at most that many bytes, returning empty at end of stream."""

    def readline(self, limit: int = ..., /) -> bytes:
        """Read to the next newline or to that many bytes, whichever comes first."""

    def close(self) -> None:
        """Release the stream."""


class ByteTarget(Protocol):
    """A writable byte stream that can be flushed and closed."""

    def write(self, data: bytes, /) -> int:
        """Write the bytes."""

    def flush(self) -> None:
        """Push whatever is buffered onward."""

    def close(self) -> None:
        """Release the stream."""


@dataclass(slots=True)
class _Retainer:
    """The capped copy of one frame, and what the frame actually measured.

    Retaining is the only thing the forwarding loop does with a frame besides
    writing it. It is a bounded append with no branch that reaches the write, which
    is why the cap can be enforced here without the relay depending on it.
    """

    cap: int
    payload: bytearray = field(default_factory=bytearray)
    length: int = 0

    def add(self, chunk: bytes) -> None:
        """Count the chunk and keep as much of it as the cap still admits."""
        self.length += len(chunk)
        room = self.cap - len(self.payload)
        if room > 0:
            self.payload += chunk[:room]

    def take(self) -> tuple[bytes, int]:
        """Take the copy and the length, leaving the retainer empty."""
        taken = bytes(self.payload), self.length
        self.payload = bytearray()
        self.length = 0
        return taken

    def split_off(self, separator: bytes) -> bytes | None:
        """Take everything before the next separator, or None when there is none."""
        index = self.payload.find(separator)
        if index < 0:
            return None
        block = bytes(self.payload[:index])
        consumed = index + len(separator)
        del self.payload[:consumed]
        self.length = max(self.length - consumed, 0)
        return block

    @property
    def empty(self) -> bool:
        """Whether nothing has been retained since the last take."""
        return self.length == 0


def forward_frames(
    source: ByteSource,
    target: ByteTarget,
    direction: Direction,
    observer: Observer,
) -> None:
    """Forward newline-delimited frames until the source ends.

    Each chunk is written and flushed before anything else happens to it, so the
    bytes leaving equal the bytes arriving whatever the observation side does or
    fails to do. A frame larger than one chunk is forwarded as it is read rather
    than assembled first, which is why an oversized frame costs the relay one chunk
    of memory and is still relayed byte for byte.

    The newline is the framing: it is forwarded with the frame, because removing it
    would change the byte sequence, and it is removed from the copy handed over for
    parsing, because it is not part of the message.
    """
    retainer = _Retainer(observer.frame_cap)
    while True:
        chunk = source.readline(READ_CHUNK_BYTES)
        if not chunk:
            break
        target.write(relay(chunk, direction))
        target.flush()
        retainer.add(chunk)
        if chunk.endswith(FRAME_SEPARATOR):
            payload, length = retainer.take()
            observer.offer(payload, length, direction)
    if not retainer.empty:
        payload, length = retainer.take()
        observer.offer(payload, length, direction)


def forward_body(
    source: ByteSource,
    target: ByteTarget,
    direction: Direction,
    observer: Observer,
    *,
    remaining: int | None,
    framing: Framing = Framing.BODY,
) -> int:
    """Forward an entity body through unchanged, returning how much was forwarded.

    The framing of an HTTP message — its request line, its status line, its
    headers, and the chunk headers of a chunked body — is this transport's own and
    is presented rather than forwarded. The entity body is the payload, and it is
    copied here byte for byte (Requirement 2.4).

    A length of None means read until the source ends, which is what an upstream
    response carrying no content length calls for.
    """
    retainer = _Retainer(observer.frame_cap)
    forwarded = 0
    left = remaining
    while left is None or left > 0:
        want = READ_CHUNK_BYTES if left is None else min(READ_CHUNK_BYTES, left)
        chunk = source.read(want)
        if not chunk:
            break
        target.write(relay(chunk, direction))
        forwarded += len(chunk)
        if left is not None:
            left -= len(chunk)
        retainer.add(chunk)
        if framing is Framing.EVENT_STREAM:
            _offer_events(retainer, direction, observer)
    target.flush()
    if not retainer.empty:
        payload, length = retainer.take()
        if framing is Framing.EVENT_STREAM:
            payload = _event_payload(payload)
            length = len(payload)
        if payload.strip():
            observer.offer(payload, length, direction)
    return forwarded


def _offer_events(retainer: _Retainer, direction: Direction, observer: Observer) -> None:
    """Hand over every complete event the retained copy now holds.

    Offering per event rather than per body is what keeps a long-lived event stream
    observable while it is still open, rather than only once it closes.
    """
    while True:
        block = retainer.split_off(EVENT_SEPARATOR)
        if block is None:
            return
        payload = _event_payload(block)
        if payload:
            observer.offer(payload, len(payload), direction)


# ---------------------------------------------------------------------------
# The stdio transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Child:
    """The tool server this process spawned, and the two pipes it speaks over."""

    pid: int
    stdin: ByteTarget
    stdout: ByteSource


def _spawn(child_cmd: Sequence[str]) -> _Child:
    """Start the tool server with its own standard streams wired to two pipes.

    No shell is involved at any point, and there is no interface here through which
    one could be asked for: the executable is resolved once into an absolute path
    through an explicit path lookup, the arguments travel as a vector rather than as
    a line to be split, and the spawn is the operating system's own vector spawn,
    which has no shell parameter to set wrongly. Standard error is inherited rather
    than piped, because it carries a tool server's diagnostics rather than protocol
    frames, so there is nothing there to observe and nothing to relay.

    Only the two descriptors the child needs are duplicated onto its standard
    streams; the parent's ends of both pipes are created close-on-exec, so the child
    inherits no handle on its own pipes.
    """
    if not child_cmd:
        raise ValueError("the tool server command names no executable")
    located = shutil.which(child_cmd[0])
    if located is None:
        raise ValueError(f"the tool server command names no executable: {child_cmd[0]!r}")
    executable = Path(located).resolve()

    to_child_read, to_child_write = os.pipe()
    from_child_read, from_child_write = os.pipe()
    try:
        pid = os.posix_spawn(
            os.fspath(executable),
            list(child_cmd),
            os.environ,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, to_child_read, 0),
                (os.POSIX_SPAWN_DUP2, from_child_write, 1),
            ],
        )
    except OSError:
        for descriptor in (to_child_read, to_child_write, from_child_read, from_child_write):
            os.close(descriptor)
        raise
    os.close(to_child_read)
    os.close(from_child_write)
    return _Child(
        pid=pid,
        stdin=os.fdopen(to_child_write, "wb", buffering=0),
        stdout=os.fdopen(from_child_read, "rb"),
    )


def _reap(pid: int) -> int:
    """Wait for the tool server and report its status the way a shell would."""
    try:
        _, status = os.waitpid(pid, 0)
    except OSError:
        return EXIT_FAILED
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else SIGNAL_EXIT_BASE - code


def _close_quietly(stream: ByteTarget | ByteSource) -> None:
    """Close a stream, ignoring one that is already gone."""
    with contextlib.suppress(OSError, ValueError):
        stream.close()


def _pump_to_server(
    source: ByteSource,
    target: ByteTarget,
    observer: Observer,
) -> None:
    """Forward the agent's frames to the tool server, and pass its close on.

    The close matters as much as the frames: a tool server reading its own standard
    input treats end of input as the instruction to exit, so an agent that closed its
    side must be seen upstream to have closed it. Forwarding the frames without
    forwarding the close would leave the server waiting on input that will never
    arrive and this process waiting on a server that will never exit.

    A far end that has already gone is the ordinary way this ends rather than a
    fault, so it is returned on rather than reported.
    """
    try:
        forward_frames(source, target, Direction.TO_SERVER, observer)
    except (OSError, ValueError):
        return
    finally:
        _close_quietly(target)


def run_stdio(
    child_cmd: list[str],
    ctx: CaptureContext,
    *,
    agent_in: ByteSource | None = None,
    agent_out: ByteTarget | None = None,
    sink: EventSink | None = None,
    observer: Observer | None = None,
) -> int:
    """Relay one stdio conversation between an agent tool and a tool server.

    The proxy is invoked where the tool server would have been, so its own standard
    input carries what the agent writes and its own standard output is what the
    agent reads. Both are overridable, which is how a test drives the transport
    without a terminal.

    The agent-to-server direction runs on a thread of its own and the server-to-agent
    direction runs here, so that the return of this call is the tool server having
    closed its output. That close is what Requirement 2.6 turns on: the agent-facing
    stream is closed and the session end Event is recorded, in that order, because
    the agent is waiting on the close and the Event is not on its path.

    Returns the tool server's own exit status, so a caller invoking the proxy in the
    server's place reports what the server reported.
    """
    source = sys.stdin.buffer if agent_in is None else agent_in
    target = sys.stdout.buffer if agent_out is None else agent_out
    child = _spawn(child_cmd)
    watching = (
        Observer(ctx=ctx, transport=TRANSPORT_STDIO, sink=sink) if observer is None else observer
    )
    outbound = threading.Thread(
        target=_pump_to_server,
        args=(source, child.stdin, watching),
        name="molt-mcp-to-server",
        daemon=True,
    )
    reason = UPSTREAM_CLOSED_REASON
    try:
        watching.start()
        outbound.start()
        forward_frames(child.stdout, target, Direction.TO_AGENT, watching)
    except (OSError, ValueError):
        reason = RELAY_FAILED_REASON
    finally:
        _close_quietly(target)
        _close_quietly(child.stdin)
        _close_quietly(child.stdout)
        watching.session_end(reason)
        if observer is None:
            watching.close()
    return _reap(child.pid)


# ---------------------------------------------------------------------------
# The HTTP transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Upstream:
    """The tool server address every relayed request is placed against."""

    host: str
    port: int | None
    secure: bool
    prefix: str

    @classmethod
    def parse(cls, address: str) -> _Upstream:
        """Read an upstream address, defaulting to TLS when no scheme is given."""
        parts = urlsplit(address if "//" in address else f"//{address}", scheme="https")
        if not parts.hostname:
            raise ValueError("the tool server address names no host")
        return cls(
            host=parts.hostname,
            port=parts.port,
            secure=parts.scheme != "http",
            prefix=parts.path.rstrip("/"),
        )

    def open(self) -> http.client.HTTPConnection:
        """Open one connection, which one relayed request has to itself.

        A connection per request rather than a pooled one, because a request may be
        answered by an event stream that stays open for the rest of the session and
        a pooled connection would be unavailable for that whole time. No timeout is
        imposed for the same reason: how long to wait for a tool server is the
        agent's decision, and this process must not shorten it.
        """
        if self.secure:
            return http.client.HTTPSConnection(self.host, self.port)
        return http.client.HTTPConnection(self.host, self.port)

    def route(self, path: str) -> str:
        """The upstream path one downstream path maps to."""
        return f"{self.prefix}{path}"


def _read_exact(source: ByteSource, length: int) -> bytes:
    """Read exactly that many bytes, refusing a stream that ended early."""
    parts: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = source.read(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            raise ValueError("the request body ended before its declared length")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _read_chunked(source: ByteSource) -> bytes:
    """Read a chunked body into the entity body it frames.

    The chunk sizes, the chunk terminators, and any trailer are framing, so what is
    returned is the message the framing carried and the framing itself is presented
    afresh rather than forwarded.
    """
    body = bytearray()
    while True:
        header = source.readline(CHUNK_HEADER_MAX_BYTES)
        if not header:
            raise ValueError("a chunked body ended before its final chunk")
        try:
            size = int(header.split(b";", 1)[0].strip(), 16)
        except ValueError as error:
            raise ValueError("a chunked body carries an unreadable chunk size") from error
        if size == 0:
            while True:
                trailer = source.readline(CHUNK_HEADER_MAX_BYTES)
                if not trailer or trailer.strip() == b"":
                    break
            return bytes(body)
        body += _read_exact(source, size)
        source.readline(CHUNK_HEADER_MAX_BYTES)


class _HeaderView(Protocol):
    """The two readings a relayed message's headers are consulted through.

    Structural because the headers of a received request and the headers of an
    upstream response are two different types that answer the same two questions.
    """

    def get(self, name: str, /) -> str | None:
        """One header's value, or None when the message carries it not."""

    def items(self) -> list[tuple[str, str]]:
        """Every header as a text pair, in the order the message presented them."""


def _header_pairs(headers: _HeaderView) -> list[tuple[str, str]]:
    """Every header of a message as text pairs, in the order it presented them."""
    return [(str(name), str(value)) for name, value in headers.items()]


def _content_length(headers: _HeaderView) -> int | None:
    """The declared body length, or None when the message declares none."""
    raw = headers.get(CONTENT_LENGTH_HEADER)
    if raw is None:
        return None
    try:
        length = int(raw.strip(), 10)
    except ValueError as error:
        raise ValueError("a message declares an unreadable content length") from error
    if length < 0:
        raise ValueError("a message declares a negative content length")
    return length


def _is_chunked(headers: _HeaderView) -> bool:
    """Whether a message frames its body as chunks."""
    return CHUNKED_ENCODING in (headers.get(TRANSFER_ENCODING_HEADER) or "").lower()


def _is_event_stream(headers: _HeaderView) -> bool:
    """Whether a body carries a sequence of events rather than one document."""
    return EVENT_STREAM_MEDIA_TYPE in (headers.get(CONTENT_TYPE_HEADER) or "").lower()


def _forwarded_request_headers(headers: _HeaderView, length: int) -> dict[str, str]:
    """The headers a relayed request presents upstream.

    Every header the agent sent is carried, including the authorisation the tool
    server expects, because a proxy that stripped it would not be transparent. What
    is dropped names this hop rather than the message: the host is this process's
    upstream rather than its own listener, and the body length is recomputed because
    a chunked body is presented upstream with a length instead.
    """
    forwarded = {
        name: value
        for name, value in _header_pairs(headers)
        if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() not in _REQUEST_ONLY_DROPPED
    }
    forwarded[CONTENT_LENGTH_HEADER] = str(length)
    return forwarded


def _forwarded_response_headers(headers: _HeaderView) -> list[tuple[str, str]]:
    """The headers a relayed response presents downstream, duplicates preserved."""
    return [
        (name, value)
        for name, value in _header_pairs(headers)
        if name.lower() not in HOP_BY_HOP_HEADERS
    ]


class _ProxyHandler(BaseHTTPRequestHandler):
    """One relayed HTTP exchange.

    The request methods are resolved rather than enumerated. A transparent relay has
    no business holding a list of the methods it is willing to forward: whatever the
    agent asked for is placed upstream as asked, and the upstream's own answer to an
    unsupported method is what the agent receives.
    """

    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: socketserver.BaseServer,
        *,
        observer: Observer,
        upstream: _Upstream,
    ) -> None:
        self._observer = observer
        self._upstream = upstream
        super().__init__(request, client_address, server)

    def __getattr__(self, name: str) -> object:
        """Resolve any request-method handler to the one relay path.

        The base class dispatches a request by looking for a handler named after the
        method, so resolving every such name here is what makes the relay
        method-agnostic. Nothing else is resolved: any other absent attribute is
        absent, which is what keeps this from answering questions it was not asked.
        """
        if name.startswith("do_"):
            return self._proxy
        raise AttributeError(name)

    def log_message(self, template: str, *args: object) -> None:
        """Route the server's own logging into the structured record stream.

        The base implementation writes an unstructured line to standard error, which
        for a process relaying an agent's traffic is both noise and a place a path or
        an identifier could reach a stream nobody is filtering.
        """
        log(
            Severity.DEBUG,
            COMPONENT,
            "the proxy listener handled a request",
            detail=template % args if args else template,
        )

    # -- one exchange ----------------------------------------------------

    def _proxy(self) -> None:
        """Place one request upstream and return its response, byte for byte."""
        observer = self._observer
        try:
            body = self._request_body()
        except ValueError as error:
            self._refuse(BAD_REQUEST, f"the request could not be read: {type(error).__name__}")
            return
        headers = _forwarded_request_headers(self.headers, len(body))
        connection = self._upstream.open()
        try:
            connection.request(
                self.command,
                self._upstream.route(self.path),
                body=relay(body, Direction.TO_SERVER),
                headers=headers,
            )
            response = connection.getresponse()
        except (OSError, http.client.HTTPException):
            connection.close()
            self._upstream_closed()
            return
        observer.offer(body[: observer.frame_cap], len(body), Direction.TO_SERVER)
        try:
            self._relay_response(response)
        except (OSError, http.client.HTTPException):
            self.close_connection = True
            self._observer.session_end(RELAY_FAILED_REASON)
        finally:
            connection.close()

    def _request_body(self) -> bytes:
        """The entity body the agent sent, with its framing removed.

        Held whole rather than streamed, so that the bytes placed upstream are the
        bytes received with nothing between the read and the write that could
        re-render them. The observation copy is taken from it afterwards.
        """
        if _is_chunked(self.headers):
            return _read_chunked(self.rfile)
        length = _content_length(self.headers)
        if not length:
            return b""
        return _read_exact(self.rfile, length)

    def _relay_response(self, response: http.client.HTTPResponse) -> None:
        """Present the upstream's status and headers, then copy its body through."""
        length = _content_length(response.headers)
        streaming = _is_event_stream(response.headers)
        framing = Framing.EVENT_STREAM if streaming else Framing.BODY
        self.send_response_only(response.status, response.reason)
        for name, value in _forwarded_response_headers(response.headers):
            self.send_header(name, value)
        if length is None:
            # The upstream declared no length, so the close is what delimits the
            # body. The framing it used to delimit it is this hop's to choose.
            self.send_header(CONNECTION_HEADER, CLOSE_CONNECTION)
            self.close_connection = True
        self.end_headers()
        forward_body(
            response,
            self.wfile,
            Direction.TO_AGENT,
            self._observer,
            remaining=length,
            framing=framing,
        )

    def _upstream_closed(self) -> None:
        """Close the agent's connection and end the Session (Requirement 2.6)."""
        self._observer.session_end(UPSTREAM_CLOSED_REASON)
        self._refuse(BAD_GATEWAY, "the tool server closed the connection")

    def _refuse(self, status: int, detail: str) -> None:
        """Answer on this process's own behalf and close the connection."""
        self.close_connection = True
        log(Severity.WARNING, COMPONENT, detail, status=status)
        try:
            self.send_response_only(status)
            self.send_header(CONTENT_LENGTH_HEADER, "0")
            self.send_header(CONNECTION_HEADER, CLOSE_CONNECTION)
            self.end_headers()
        except OSError:
            return


class ProxyServer(ThreadingHTTPServer):
    """The listener the agent tool connects to, one thread per exchange.

    The observer is held here so that a caller driving the server directly, rather
    than through `run_http`, finishes the observation queue when it stops serving.
    """

    daemon_threads = True

    observer: Observer

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler] | partial[_ProxyHandler],
        *,
        observer: Observer,
    ) -> None:
        self.observer = observer
        super().__init__(address, handler)


def _bind_address(bind: str) -> tuple[str, int]:
    """Read a listener address, reporting a listener reachable from off the machine.

    The listener carries no authentication of its own, because it stands where a
    local tool server stood and the credentials in a relayed request are the tool
    server's own. That makes the bound interface the whole of its exposure, so a
    non-loopback bind is recorded as the operator's deliberate choice rather than
    passed over in silence.
    """
    parts = urlsplit(f"//{bind}" if "//" not in bind else bind)
    host = parts.hostname
    port = parts.port
    if host is None or port is None:
        raise ValueError("a listener address must name a host and a port")
    if host not in _LOOPBACK_HOSTS:
        log(
            Severity.WARNING,
            COMPONENT,
            "the proxy listener is bound beyond the loopback interface and requires "
            "no credential of its own",
            bind_host=host,
            bind_port=port,
        )
    return host, port


def build_http_server(
    upstream: str,
    bind: str,
    ctx: CaptureContext,
    *,
    sink: EventSink | None = None,
    observer: Observer | None = None,
) -> ProxyServer:
    """Build the listener for an HTTP conversation, without starting to serve.

    Separated from `run_http` so that a caller can read the bound address before
    traffic arrives, which is what a test needs when it asks for an ephemeral port.
    """
    watching = (
        Observer(ctx=ctx, transport=TRANSPORT_HTTP, sink=sink) if observer is None else observer
    )
    handler = partial(
        _ProxyHandler,
        observer=watching,
        upstream=_Upstream.parse(upstream),
    )
    server = ProxyServer(_bind_address(bind), handler, observer=watching)
    watching.start()
    return server


def run_http(
    upstream: str,
    bind: str,
    ctx: CaptureContext,
    *,
    sink: EventSink | None = None,
    observer: Observer | None = None,
) -> int:
    """Relay HTTP conversations between an agent tool and a tool server.

    Serves until it is interrupted. The listener is closed and the observation queue
    is finished on the way out, so the Session ends exactly once however the run
    ended.
    """
    server = build_http_server(upstream, bind, ctx, sink=sink, observer=observer)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()
    if observer is None:
        server.observer.close()
    return EXIT_OK
