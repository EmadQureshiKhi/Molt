"""Property 24: the proxy forwards what it received, and links every answer to its call.

**Validates: Requirements 2.4, 2.2, 2.3**

Two claims are asserted together because they are the two halves of one promise:
a component sitting inside somebody else's conversation must change nothing about
the bytes, and must still be able to say which answer belongs to which question.
Either one alone is easy; the point of holding them together is that the second is
attempted on a copy and must therefore be incapable of touching the first.

Four decisions shape what is generated.

**The observation side is driven without its thread.** An observer that was never
started processes its whole queue on the calling thread when it is closed, which
turns "the Events this frame sequence produced" from a race into a value. A
property loop that spawned a thread per example would be asserting about a
schedule; this one asserts about a sequence.

**The size cap is injected small rather than reached for real.** The delivered
observation cap is a megabyte, and generating a megabyte per example would spend
the whole budget on padding rather than on shapes. The observer therefore takes a
cap of a few hundred bytes and the generator straddles *that* — one byte under, on
it, and one byte over — so the boundary is crossed in most examples. The delivered
cap is crossed exactly twice, in the two explicit cases at the foot of the module,
which is where the chunked read of a frame larger than one read also gets covered.

**Identifiers are drawn from a small shared pool.** Twenty freely drawn
identifiers almost never collide, and a collision is the whole subject: linkage
only happens when an answer names a question that was asked. A pool of at most
four, sampled per message, makes a matched pair ordinary. The pool spans the JSON
types on purpose, because the type participates in the identity: the text one and
the number one are different questions, while the integer one and the whole-valued
real one are the same question.

**The expected linkage is walked in Python from the drawn sequence.** Reading the
answer off the component would assert that it agrees with itself. The walk below
reimplements the bounded map — remember on a call, take on an answer, evict the
oldest past the bound — so the three cases where linkage legitimately does not
happen are modelled rather than excused: an identifier no call carried, an
identifier that identifies nothing at all, and a call the bound evicted before its
answer arrived.

Event-stream framing is the one seam not driven here. It is covered against the
real reassembly rule in the unit suite, and repeating it in a property loop would
add a second framing model without adding a shape.
"""

from __future__ import annotations

import io
import json
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.capture.mcp_proxy import (
    FRAME_SEPARATOR,
    OBSERVED_FRAME_MAX_BYTES,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    Direction,
    Framing,
    Observer,
    forward_body,
    forward_frames,
    relay,
)
from molt.capture.protocol import UNASSIGNED_CLIENT, CaptureContext, derive_session_id
from molt.models.event import Event, EventCategory, JsonObject, JsonValue

# The identity every example's Events belong to.
AGENT_CLI: Final[str] = "proxied-agent"
MACHINE_ID: Final[str] = "machine-under-test"
SESSION_KEY: Final[str] = "a-transparency-session-key"

# The observation cap the property loop injects. Small enough that a frame
# straddling it costs a few hundred bytes rather than a megabyte, and comfortably
# above the longest unpadded batch so the padding solve always has room.
FRAME_CAP: Final[int] = 512

# How many unanswered calls the injected identifier map remembers. Two, because
# eviction is one of the three reasons an answer legitimately links to nothing and
# a bound of a thousand would never be reached inside one example.
PENDING_MAX: Final[int] = 2

# How many frame copies the injected queue holds. Above any example's frame count,
# because a full queue is a dropped Event and that is a different property's
# subject.
QUEUE_MAX: Final[int] = 1024

# How many frames one example draws, and how many messages one batch carries.
MIN_FRAMES: Final[int] = 1
MAX_FRAMES: Final[int] = 10
MIN_BATCH: Final[int] = 2
MAX_BATCH: Final[int] = 4

# How many identifiers one example's messages are sampled from. Above the bound on
# purpose, so a pool can hold more unanswered calls than the map remembers.
MIN_POOL: Final[int] = 1
MAX_POOL: Final[int] = 5

# What a generated JSON-RPC message says. The content is fixed because the subject
# is the framing and the identifier, not the method vocabulary.
PROTOCOL_VERSION: Final[str] = "2.0"
METHOD_NAME: Final[str] = "tools/call"
TOOL_NAME: Final[str] = "read_file"
ERROR_CODE: Final[int] = -32000
ERROR_MESSAGE: Final[str] = "the tool refused the call"

# The separators that make a rendered document compact, so the padding solve is
# linear in the pad length.
COMPACT: Final[tuple[str, str]] = (",", ":")

# The byte a padded frame is grown with. One byte per character in the rendered
# document, which is what makes the solve exact.
PAD_BYTE: Final[bytes] = b"a"

# The instant every observed Event is placed at, read from a fixed offset rather
# than from the host so no run embeds a reading of the machine it ran on.
FIXED_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


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
    """A byte target keeping every byte written, which is the whole assertion."""

    written: bytearray = field(default_factory=bytearray)

    def write(self, data: bytes) -> int:
        """Keep the bytes and report them all written."""
        self.written += data
        return len(data)

    def flush(self) -> None:
        """Push nothing onward: everything is already kept."""

    def close(self) -> None:
        """Release nothing, keeping what was written readable afterwards."""

    @property
    def value(self) -> bytes:
        """Everything written so far, in the order it was written."""
        return bytes(self.written)


@dataclass(slots=True)
class RecordingSink:
    """An Event destination keeping the batches it was given, in order."""

    events: list[Event] = field(default_factory=list)

    def emit(self, events: Sequence[Event]) -> None:
        """Keep the batch."""
        self.events.extend(events)

    def close(self) -> None:
        """Release nothing."""


def capture_context() -> CaptureContext:
    """The Session context every observed Event is built against."""
    return CaptureContext(
        session_id=derive_session_id(AGENT_CLI, SESSION_KEY),
        client=UNASSIGNED_CLIENT,
        machine_id=MACHINE_ID,
        agent_cli=AGENT_CLI,
        clock=FrozenClock(),
    )


# ---------------------------------------------------------------------------
# What a generated sequence is made of
# ---------------------------------------------------------------------------


class IdKind(StrEnum):
    """The JSON types a request identifier is drawn across.

    The type is part of the identity, so these are five identifiers rather than
    one written five ways — except for the integer and the whole-valued real,
    which name the same request and must link to each other.
    """

    TEXT = "text"
    INTEGER = "integer"
    WHOLE_REAL = "whole_real"
    NULL = "null"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class Identifier:
    """One request identifier, held as its JSON type and its value."""

    kind: IdKind
    text: str = ""
    number: int = 0

    @property
    def as_json(self) -> JsonValue:
        """The value a rendered document carries for this identifier."""
        if self.kind is IdKind.TEXT:
            return self.text
        if self.kind is IdKind.INTEGER:
            return self.number
        if self.kind is IdKind.WHOLE_REAL:
            return float(self.number)
        if self.kind is IdKind.BOOLEAN:
            return True
        return None

    @property
    def linkage_key(self) -> str | None:
        """What two messages must share to be one call and its answer, or None.

        None means *not linkable at all*: a null identifier identifies no request
        by the protocol's own account, and a boolean is not an identifier any
        client should have sent. The number branch drops the distinction between
        the integer and the whole-valued real, and keeps the distinction between
        either of them and the text spelling the same digits.
        """
        if self.kind is IdKind.TEXT:
            return f"text:{self.text}"
        if self.kind in (IdKind.INTEGER, IdKind.WHOLE_REAL):
            return f"number:{self.number}"
        return None


class MessageKind(StrEnum):
    """The four JSON-RPC message forms one frame may carry."""

    REQUEST = "request"
    NOTIFICATION = "notification"
    RESULT = "result"
    ERROR = "error"


# The two forms that ask something, and are therefore remembered.
CALL_KINDS: Final[frozenset[MessageKind]] = frozenset(
    {MessageKind.REQUEST, MessageKind.NOTIFICATION}
)


@dataclass(frozen=True, slots=True)
class Message:
    """One JSON-RPC message, as the generator decided it rather than as bytes."""

    kind: MessageKind
    identifier: Identifier

    @property
    def asks(self) -> bool:
        """Whether this message is a call rather than an answer."""
        return self.kind in CALL_KINDS

    @property
    def remembered_key(self) -> str | None:
        """The key this call is remembered under, or None when it is not.

        A notification carries no identifier at all, so there is nothing a later
        answer could name, which is the absent-identifier case.
        """
        if self.kind is MessageKind.NOTIFICATION:
            return None
        return self.identifier.linkage_key

    def document(self, pad: int = 0) -> JsonObject:
        """Render this message as the JSON object a frame carries.

        The padding is an extra top-level field rather than a longer method name
        or a deeper parameter, because an unread field cannot change which kind of
        message this is while still growing the frame to any length asked for.
        """
        body: JsonObject = {"jsonrpc": PROTOCOL_VERSION}
        if self.asks:
            body["method"] = METHOD_NAME
            if self.kind is MessageKind.REQUEST:
                body["id"] = self.identifier.as_json
            body["params"] = {"name": TOOL_NAME}
        else:
            body["id"] = self.identifier.as_json
            if self.kind is MessageKind.RESULT:
                body["result"] = {"ok": True}
            else:
                body["error"] = {"code": ERROR_CODE, "message": ERROR_MESSAGE}
        if pad > 0:
            body["pad"] = (PAD_BYTE * pad).decode()
        return body


class FrameKind(StrEnum):
    """What one frame holds, which decides what observing it produces."""

    SINGLE = "single"
    BATCH = "batch"
    TRUNCATED = "truncated"
    TEXT = "text"
    BINARY = "binary"
    SCALAR = "scalar"
    BLANK = "blank"


# The frame kinds carrying JSON-RPC messages, and the kinds carrying none. Half of
# every draw comes from each group, so a sequence is neither all well formed nor
# mostly rubbish.
MESSAGE_KINDS: Final[tuple[FrameKind, ...]] = (FrameKind.SINGLE, FrameKind.BATCH)
NO_MESSAGE_KINDS: Final[tuple[FrameKind, ...]] = (
    FrameKind.TRUNCATED,
    FrameKind.TEXT,
    FrameKind.BINARY,
    FrameKind.SCALAR,
    FrameKind.BLANK,
)

# The kinds a size band can be applied to. A JSON document and a run of plain text
# both grow to any length; a truncated prefix, a non-text byte run, and a blank
# frame are what they are.
PADDABLE_KINDS: Final[frozenset[FrameKind]] = frozenset(
    {FrameKind.SINGLE, FrameKind.BATCH, FrameKind.TEXT}
)

# The kinds no JSON reader accepts, and the kind it accepts as something that is
# not a message. Both cost one Event; neither produces one.
UNPARSEABLE_KINDS: Final[frozenset[FrameKind]] = frozenset(
    {FrameKind.TRUNCATED, FrameKind.TEXT, FrameKind.BINARY}
)

# The literal bodies each kind that carries no message is drawn from. Three per
# kind, so one drawn index covers every kind.
FORMS_PER_KIND: Final[int] = 3
NO_MESSAGE_FORMS: Final[dict[FrameKind, tuple[bytes, ...]]] = {
    FrameKind.TRUNCATED: (b'{"jsonrpc":"2.0","id":7,', b'[{"method":"tools/call"', b"{"),
    FrameKind.TEXT: (b"not a json document", b"OK", b"<html>no</html>"),
    FrameKind.BINARY: (b"\xff\xfe\x00\x01", b"\xc3\x28 broken", b"\x80\x80\x80"),
    FrameKind.SCALAR: (b"42", b'"a text"', b"true"),
    FrameKind.BLANK: (b"", b"   ", b"\t"),
}


class SizeBand(StrEnum):
    """Where a frame sits relative to the observation cap."""

    SMALL = "small"
    UNDER_CAP = "under_cap"
    AT_CAP = "at_cap"
    OVER_CAP = "over_cap"


# How far past the cap each band lands, as a signed offset from the cap itself.
BAND_OFFSETS: Final[dict[SizeBand, int]] = {
    SizeBand.UNDER_CAP: -1,
    SizeBand.AT_CAP: 0,
    SizeBand.OVER_CAP: 1,
}


class TransportArm(StrEnum):
    """Which forwarding loop an example drives.

    Both loops make the same promise about bytes and differ in what counts as
    framing: a line terminator on one, the whole entity body on the other.
    """

    STDIO = "stdio"
    HTTP_BODY = "http_body"


@dataclass(frozen=True, slots=True)
class FrameSpec:
    """One frame of a drawn sequence, before it is rendered to bytes."""

    kind: FrameKind
    direction: Direction
    messages: tuple[Message, ...]
    band: SizeBand
    variant: int
    read_to_end: bool


@dataclass(frozen=True, slots=True)
class FrameSequence:
    """One drawn conversation: a transport, its frames, and whether the last is torn.

    A torn final frame is a stream that ended mid-message, which is what a tool
    server killed part-way through produces. It carries no terminator, so its
    payload is the whole of what was read.
    """

    transport: TransportArm
    frames: tuple[FrameSpec, ...]
    torn: bool
    pipelined: bool


@dataclass(frozen=True, slots=True)
class Rendered:
    """One frame as bytes, with the framing held apart from the payload.

    The framed form is a field rather than a computed property because the identity
    of the forwarding path is asserted by object identity: a property returning a
    fresh concatenation on every read would make that assertion unsatisfiable by
    any implementation, including the correct one.

    Attributes:
        spec: What was drawn.
        payload: The message, which is what observation reads.
        terminator: The framing, which is forwarded and not observed.
        wire: Every byte of this frame, framing included, as it crosses the relay.
    """

    spec: FrameSpec
    payload: bytes
    terminator: bytes
    wire: bytes

    @property
    def length(self) -> int:
        """What this frame measures, which is what the observation cap is compared to."""
        return len(self.wire)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def encode_messages(messages: tuple[Message, ...], *, batch: bool, pad: int) -> bytes:
    """Render messages as one frame body, padding the first of them."""
    documents: list[JsonValue] = [
        message.document(pad if index == 0 else 0) for index, message in enumerate(messages)
    ]
    body: JsonValue = documents if batch else documents[0]
    return json.dumps(body, separators=COMPACT).encode()


def padded_json(messages: tuple[Message, ...], *, batch: bool, payload_length: int) -> bytes:
    """Render messages padded to exactly that many bytes, or unpadded if too long.

    One pad byte adds one byte to the rendered document, so the solve is a
    subtraction rather than a search. A batch already longer than the target keeps
    its natural length; the observation model reads the length off the rendered
    bytes, so a frame that could not reach its band is still modelled correctly.
    """
    shortest = encode_messages(messages, batch=batch, pad=1)
    pad = payload_length - len(shortest) + 1
    if pad < 1:
        return shortest
    return encode_messages(messages, batch=batch, pad=pad)


def payload_of(spec: FrameSpec, *, terminator_length: int) -> bytes:
    """The body one frame carries, before its framing is attached."""
    target = FRAME_CAP + BAND_OFFSETS.get(spec.band, 0) - terminator_length
    if spec.kind in MESSAGE_KINDS:
        if spec.band is SizeBand.SMALL:
            return encode_messages(spec.messages, batch=spec.kind is FrameKind.BATCH, pad=0)
        return padded_json(
            spec.messages,
            batch=spec.kind is FrameKind.BATCH,
            payload_length=target,
        )
    forms = NO_MESSAGE_FORMS[spec.kind]
    base = forms[spec.variant % len(forms)]
    if spec.kind in PADDABLE_KINDS and spec.band is not SizeBand.SMALL:
        return base + PAD_BYTE * max(target - len(base), 0)
    return base


def render(sequence: FrameSequence) -> tuple[Rendered, ...]:
    """Turn a drawn sequence into the exact bytes each frame occupies.

    The line terminator belongs to the stdio framing alone, and the last frame of
    a torn sequence carries none, so a band is solved against the length the frame
    will really measure rather than against the length of its body.
    """
    last = len(sequence.frames) - 1
    rendered: list[Rendered] = []
    for index, spec in enumerate(sequence.frames):
        framed = sequence.transport is TransportArm.STDIO and not (sequence.torn and index == last)
        terminator = FRAME_SEPARATOR if framed else b""
        payload = payload_of(spec, terminator_length=len(terminator))
        rendered.append(
            Rendered(
                spec=spec,
                payload=payload,
                terminator=terminator,
                wire=payload + terminator,
            )
        )
    return tuple(rendered)


# ---------------------------------------------------------------------------
# What observing a rendered sequence ought to produce
# ---------------------------------------------------------------------------


def observed_messages(item: Rendered) -> tuple[tuple[Message, ...], int]:
    """The messages one frame is observed as, and the Events observing it cost.

    Four branches, in the order the observation side takes them. A frame that
    never reached the reader produces nothing. A frame above the cap is relayed in
    full and parsed not at all, which costs one Event. A frame holding nothing but
    space is neither a message nor a failure. Everything else is read, and what a
    reader cannot turn into a message costs one Event as well.
    """
    if item.length == 0:
        return (), 0
    if item.length > FRAME_CAP:
        return (), 1
    if not item.payload.strip():
        return (), 0
    if item.spec.kind in UNPARSEABLE_KINDS or item.spec.kind is FrameKind.SCALAR:
        return (), 1
    return item.spec.messages, 0


class Linkage(StrEnum):
    """Why one answer did or did not name a call as its parent."""

    LINKED = "linked"
    UNLINKABLE_ID = "unlinkable identifier"
    NEVER_CALLED = "never called"
    EVICTED = "evicted by the bound"
    ALREADY_ANSWERED = "already answered"


@dataclass(frozen=True, slots=True)
class ExpectedEvent:
    """One Event a drawn sequence ought to produce.

    Attributes:
        category: Which kind of Event this is.
        parent: For an answer, which call it links to, counted in the order calls
            were observed; None when it links to nothing. Always None for a call.
        linkage: Why an answer linked or did not, for the coverage record.
    """

    category: EventCategory
    parent: int | None = None
    linkage: Linkage | None = None


@dataclass(frozen=True, slots=True)
class Expectation:
    """What a drawn sequence ought to produce, walked from the sequence itself."""

    events: tuple[ExpectedEvent, ...]
    dropped: int

    @property
    def answers(self) -> tuple[ExpectedEvent, ...]:
        """Every expected answer, in the order it was observed."""
        return tuple(item for item in self.events if item.category is EventCategory.TOOL_RESULT)


def expectation_of(rendered: tuple[Rendered, ...]) -> Expectation:
    """Walk a rendered sequence and state the Events it ought to produce.

    The bounded map is reimplemented here rather than consulted: a call is
    remembered under its identifier and moved to the most recent end, an answer
    takes its entry rather than reading it, and the oldest entry leaves once the
    map holds more than the bound. That reimplementation is what makes the three
    non-linking cases statements about the drawn sequence instead of excuses.
    """
    pending: OrderedDict[str, int] = OrderedDict()
    evicted: set[str] = set()
    remembered: set[str] = set()
    events: list[ExpectedEvent] = []
    calls = 0
    dropped = 0

    for item in rendered:
        messages, cost = observed_messages(item)
        dropped += cost
        for message in messages:
            if message.asks:
                events.append(ExpectedEvent(EventCategory.TOOL_CALL))
                key = message.remembered_key
                if key is not None:
                    pending[key] = calls
                    pending.move_to_end(key)
                    remembered.add(key)
                    while len(pending) > PENDING_MAX:
                        stale, _ = pending.popitem(last=False)
                        evicted.add(stale)
                calls += 1
                continue
            key = message.identifier.linkage_key
            parent = None if key is None else pending.pop(key, None)
            events.append(
                ExpectedEvent(
                    category=EventCategory.TOOL_RESULT,
                    parent=parent,
                    linkage=_why(key, parent, evicted=evicted, remembered=remembered),
                )
            )
    return Expectation(events=tuple(events), dropped=dropped)


def _why(
    key: str | None,
    parent: int | None,
    *,
    evicted: set[str],
    remembered: set[str],
) -> Linkage:
    """Name the reason one answer linked or did not, for the coverage record."""
    if parent is not None:
        return Linkage.LINKED
    if key is None:
        return Linkage.UNLINKABLE_ID
    if key not in remembered:
        return Linkage.NEVER_CALLED
    if key in evicted:
        return Linkage.EVICTED
    return Linkage.ALREADY_ANSWERED


# ---------------------------------------------------------------------------
# Driving the two forwarding loops
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Relayed:
    """What one drive produced: the bytes per direction and the Events observed."""

    forwarded: dict[Direction, bytes]
    events: tuple[Event, ...]
    dropped: int


def direction_runs(
    rendered: tuple[Rendered, ...],
) -> list[tuple[Direction, tuple[Rendered, ...]]]:
    """Group consecutive frames travelling the same way into one stream each.

    One stdio loop carries one direction, so a sequence that changes direction is
    several streams. Grouping consecutive frames keeps the order the observation
    side sees them in identical to the drawn order, while still putting more than
    one frame into a single stream, which is what exercises the framing.
    """
    runs: list[tuple[Direction, list[Rendered]]] = []
    for item in rendered:
        if runs and runs[-1][0] is item.spec.direction:
            runs[-1][1].append(item)
        else:
            runs.append((item.spec.direction, [item]))
    return [(direction, tuple(items)) for direction, items in runs]


def drive(sequence: FrameSequence, rendered: tuple[Rendered, ...]) -> Relayed:
    """Forward a rendered sequence through the loop the example chose."""
    sink = RecordingSink()
    observer = Observer(
        ctx=capture_context(),
        transport=(TRANSPORT_STDIO if sequence.transport is TransportArm.STDIO else TRANSPORT_HTTP),
        sink=sink,
        frame_cap=FRAME_CAP,
        queue_max=QUEUE_MAX,
        pending_max=PENDING_MAX,
    )
    targets = {direction: Recorder() for direction in Direction}

    if sequence.transport is TransportArm.STDIO:
        for direction, items in direction_runs(rendered):
            stream = b"".join(item.wire for item in items)
            forward_frames(io.BytesIO(stream), targets[direction], direction, observer)
    else:
        for item in rendered:
            direction = item.spec.direction
            forward_body(
                io.BytesIO(item.wire),
                targets[direction],
                direction,
                observer,
                remaining=None if item.spec.read_to_end else len(item.wire),
                framing=Framing.BODY,
            )

    observer.close()
    return Relayed(
        forwarded={direction: target.value for direction, target in targets.items()},
        events=tuple(sink.events),
        dropped=observer.dropped_events,
    )


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# The identifier values one pool is drawn from. Small sets, so a pool of four
# repeats values and an answer ordinarily names a question that was asked. The
# text spelling of a digit is in there on purpose: it must not link to the number.
ID_TEXTS: Final[tuple[str, ...]] = ("1", "call-a")
ID_NUMBERS: Final[tuple[int, ...]] = (1, 2, 7)

# The identifiers that identify nothing, drawn as one branch so the linkable kinds
# keep the greater share of the draw.
UNLINKABLE_IDS: Final[tuple[Identifier, ...]] = (
    Identifier(IdKind.NULL),
    Identifier(IdKind.BOOLEAN),
)

# An identifier only an answer ever carries, so the never-called case is reachable
# by construction rather than by luck.
ORPHAN_ID: Final[Identifier] = Identifier(IdKind.TEXT, "never-called")


def identifiers() -> st.SearchStrategy[Identifier]:
    """Draw one identifier across the JSON types a client may send."""
    return st.one_of(
        st.builds(Identifier, st.just(IdKind.TEXT), st.sampled_from(ID_TEXTS)),
        st.builds(
            Identifier,
            st.just(IdKind.INTEGER),
            st.just(""),
            st.sampled_from(ID_NUMBERS),
        ),
        st.builds(
            Identifier,
            st.just(IdKind.WHOLE_REAL),
            st.just(""),
            st.sampled_from(ID_NUMBERS),
        ),
        st.sampled_from(UNLINKABLE_IDS),
    )


def messages(
    asked: st.SearchStrategy[Identifier],
    answered: st.SearchStrategy[Identifier],
) -> st.SearchStrategy[Message]:
    """Draw one message: a request, a notification, a result, or an error."""
    return st.one_of(
        st.builds(Message, st.just(MessageKind.REQUEST), asked),
        st.builds(Message, st.just(MessageKind.NOTIFICATION), asked),
        st.builds(Message, st.just(MessageKind.RESULT), answered),
        st.builds(Message, st.just(MessageKind.ERROR), answered),
    )


@st.composite
def frame_specs(
    draw: st.DrawFn,
    asked: st.SearchStrategy[Identifier],
    answered: st.SearchStrategy[Identifier],
) -> FrameSpec:
    """Draw one frame: what it holds, which way it goes, and how large it is.

    The kind comes half from the forms carrying messages and half from the forms
    carrying none, so an example is a real conversation with rubbish in it rather
    than either extreme. A size band applies only to a form that can be grown, and
    the plain band takes five eighths of that draw, which makes a frame near the cap
    common in a sequence without making every frame of one a padded frame.
    """
    kind = draw(st.one_of(st.sampled_from(MESSAGE_KINDS), st.sampled_from(NO_MESSAGE_KINDS)))
    carried: tuple[Message, ...]
    if kind is FrameKind.SINGLE:
        carried = (draw(messages(asked, answered)),)
    elif kind is FrameKind.BATCH:
        carried = tuple(
            draw(st.lists(messages(asked, answered), min_size=MIN_BATCH, max_size=MAX_BATCH))
        )
    else:
        carried = ()
    band = (
        draw(st.one_of(st.just(SizeBand.SMALL), st.sampled_from(SizeBand)))
        if kind in PADDABLE_KINDS
        else SizeBand.SMALL
    )
    return FrameSpec(
        kind=kind,
        direction=draw(st.sampled_from(Direction)),
        messages=carried,
        band=band,
        variant=draw(st.integers(min_value=0, max_value=FORMS_PER_KIND - 1)),
        read_to_end=draw(st.booleans()),
    )


def single_frame(kind: MessageKind, identifier: Identifier, direction: Direction) -> FrameSpec:
    """One small frame carrying exactly one message, for the pipelined arm."""
    return FrameSpec(
        kind=FrameKind.SINGLE,
        direction=direction,
        messages=(Message(kind, identifier),),
        band=SizeBand.SMALL,
        variant=0,
        read_to_end=False,
    )


def pipelined_around(pool: Sequence[Identifier], drawn: Sequence[FrameSpec]) -> list[FrameSpec]:
    """Ask every identifier of the pool, then the drawn frames, then answer them all.

    This is the shape that reaches the bound. An answer to a call the map already
    evicted needs several distinct calls to have displaced it and its answer to
    arrive afterwards, which a freely drawn interleaving produces vanishingly
    rarely; asking the whole pool up front and collecting the whole pool at the end
    produces it whenever the pool holds more linkable identifiers than the bound
    remembers. It is also an ordinary shape rather than a contrivance: a client that
    pipelines its requests and then reads its answers does exactly this.
    """
    asking = [single_frame(MessageKind.REQUEST, item, Direction.TO_SERVER) for item in pool]
    answering = [single_frame(MessageKind.RESULT, item, Direction.TO_AGENT) for item in pool]
    return [*asking, *drawn, *answering]


@st.composite
def jsonrpc_sequences(draw: st.DrawFn) -> FrameSequence:
    """Draw one conversation: a transport, a pool of identifiers, and its frames.

    The pool is drawn before the frames and every call samples from it, so calls
    and answers collide by construction. Answers sample from the pool and from one
    identifier no call ever carries, which is what makes an answer to a call this
    process never saw an ordinary draw rather than a rare one.

    The frame count is drawn as a number before the frames are drawn, rather than
    left to a list's own sizing, because the long sequences are the interesting
    end: a second answer to one call and a torn tail both need room.

    The pool size is drawn as a number for the same reason the frame count is: a
    list generator concentrates at its lower bound, and a pool of one identifier
    can neither collide with a second nor displace one.

    Half the examples wrap the drawn frames in a pipelined exchange, which is what
    reaches the bound; the other half leave the interleaving entirely to the draw,
    which is what keeps a call that is never answered ordinary.
    """
    transport = draw(st.sampled_from(TransportArm))
    torn = draw(st.booleans())
    pipelined = draw(st.booleans())
    pool_size = draw(st.integers(min_value=MIN_POOL, max_value=MAX_POOL))
    pool = [draw(identifiers()) for _ in range(pool_size)]
    asked = st.sampled_from(pool)
    answered = st.sampled_from([*pool, ORPHAN_ID])
    size = draw(st.integers(min_value=MIN_FRAMES, max_value=MAX_FRAMES))
    drawn = [draw(frame_specs(asked, answered)) for _ in range(size)]
    return FrameSequence(
        transport=transport,
        frames=tuple(pipelined_around(pool, drawn) if pipelined else drawn),
        torn=torn,
        pipelined=pipelined,
    )


# ---------------------------------------------------------------------------
# The coverage record
# ---------------------------------------------------------------------------


def band_of(item: Rendered) -> str:
    """Where a rendered frame really landed relative to the cap."""
    if item.length == FRAME_CAP:
        return "at the cap"
    if item.length == FRAME_CAP - 1:
        return "one below the cap"
    if item.length > FRAME_CAP:
        return "above the cap"
    return "well below the cap"


def record(sequence: FrameSequence, rendered: tuple[Rendered, ...], expected: Expectation) -> None:
    """Report what one example covered, so the arms can be seen to be reached."""
    event(f"transport={sequence.transport}")
    event(f"frames={len(rendered)}")
    event(f"torn tail={sequence.torn and sequence.transport is TransportArm.STDIO}")
    event(f"pipelined={sequence.pipelined}")
    for item in rendered:
        event(f"frame kind={item.spec.kind}")
        event(f"frame size={band_of(item)}")
    for answer in expected.answers:
        event(f"linkage={answer.linkage}")
    if not expected.answers:
        event("linkage=no answer observed")


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 24: For any JSON-RPC message sequence, including non-JSON
# bodies and oversized frames, the byte sequence the proxy forwards equals the byte
# sequence the proxy received, excluding transport framing, and every result Event
# links to its call Event.
# No per-example deadline, as everywhere else in this suite: a wall-clock deadline
# fails an example for the load on the machine rather than for the property, which
# under parallel execution reports contention as a correctness failure. Latency
# bounds are stated deliberately in the performance suite.
@settings(max_examples=100, deadline=None)
@given(sequence=jsonrpc_sequences())
def test_the_proxy_forwards_verbatim_and_links_every_answer_to_its_call(
    sequence: FrameSequence,
) -> None:
    rendered = render(sequence)
    expected = expectation_of(rendered)
    record(sequence, rendered, expected)

    # The forwarding path is the identity function, asserted by object identity
    # rather than by equality: an equal copy would satisfy equality and would
    # already be a second buffer the relay does not have.
    for item in rendered:
        assert relay(item.wire, item.spec.direction) is item.wire

    relayed = drive(sequence, rendered)

    # Requirement 2.4, over the whole stream rather than frame by frame: what left
    # in each direction is what arrived in that direction, byte for byte, framing
    # included, whatever the frames held and however large they were.
    for direction in Direction:
        arrived = b"".join(item.wire for item in rendered if item.spec.direction is direction)
        assert relayed.forwarded[direction] == arrived, (
            f"{len(relayed.forwarded[direction])} byte(s) were forwarded {direction} "
            f"where {len(arrived)} byte(s) arrived"
        )

    # Exactly one session end Event per run, and the Events before it are the ones
    # the drawn sequence accounts for, in order.
    assert relayed.events, "a run produces at least the session end Event"
    assert relayed.events[-1].category is EventCategory.SESSION_END
    observed = relayed.events[:-1]
    assert [item.category for item in observed] == [item.category for item in expected.events], (
        f"the run produced {[str(item.category) for item in observed]} where the drawn "
        f"sequence accounts for {[str(item.category) for item in expected.events]}"
    )

    # Requirements 2.2 and 2.3: every answer whose call was observed, was not
    # already answered, and was not evicted by the bound names that call's Event as
    # its parent; every other answer names nothing, and no call names a parent.
    call_ids: list[UUID] = [
        item.id for item in observed if item.category is EventCategory.TOOL_CALL
    ]
    for want, got in zip(expected.events, observed, strict=True):
        parent = None if want.parent is None else call_ids[want.parent]
        assert got.parent_event_id == parent, (
            f"an Event of category {got.category} names {got.parent_event_id} as its "
            f"parent where the drawn sequence makes it {parent} ({want.linkage})"
        )

    # A frame the observation side could not read is one Event it could not
    # produce, which is how an oversized frame is accounted for rather than guessed
    # at. The frame itself was already asserted forwarded in full above.
    assert relayed.dropped == expected.dropped, (
        f"{relayed.dropped} Event(s) were reported dropped where the drawn sequence "
        f"accounts for {expected.dropped}"
    )


# ---------------------------------------------------------------------------
# The delivered cap, crossed for real
# ---------------------------------------------------------------------------


def stdio_frame(message: Message, total: int) -> bytes:
    """One stdio frame carrying a message and measuring exactly that many bytes."""
    payload = padded_json((message,), batch=False, payload_length=total - len(FRAME_SEPARATOR))
    frame = payload + FRAME_SEPARATOR
    assert len(frame) == total
    return frame


def test_the_delivered_cap_admits_a_frame_on_it_and_refuses_the_next_byte() -> None:
    """A megabyte frame is observed; a megabyte and one byte is relayed and not read.

    The property loop straddles an injected cap of a few hundred bytes, because a
    megabyte per example would buy padding rather than shapes. This is where the
    delivered cap itself is crossed, and it is also the only place a frame larger
    than one read of the source appears, so the loop's per-chunk write is exercised
    against a frame it cannot hold in one chunk.
    """
    asked = stdio_frame(
        Message(MessageKind.REQUEST, Identifier(IdKind.INTEGER, number=1)),
        OBSERVED_FRAME_MAX_BYTES,
    )
    answered = stdio_frame(
        Message(MessageKind.RESULT, Identifier(IdKind.INTEGER, number=1)),
        OBSERVED_FRAME_MAX_BYTES + 1,
    )
    stream = asked + answered
    sink = RecordingSink()
    target = Recorder()
    observer = Observer(ctx=capture_context(), transport=TRANSPORT_STDIO, sink=sink)

    forward_frames(io.BytesIO(stream), target, Direction.TO_SERVER, observer)
    observer.close()

    # Both frames crossed unchanged, the one on the cap was read, and the one past
    # it produced no Event at all.
    assert target.value == stream
    assert [str(item.category) for item in sink.events] == ["tool_call", "session_end"]
    assert sink.events[0].parent_event_id is None
    assert observer.dropped_events == 1
