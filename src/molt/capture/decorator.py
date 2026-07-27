"""The direct instrumentation surface: a tool decorator and a Session block.

This is the capture path for an in-house agent written in Python, so that a
service someone wrote themselves records into the same memory as the vendor
command-line tools do (Requirement 3). Two entry points: `molt_tool` wraps one
callable, and `molt_session` bounds a run. Five claims arrange the module.

**The wrapped callable's exception is re-raised, not reported.** A decorator that
swallowed a failure, or that replaced it with a wrapper carrying its own
traceback, would make instrumenting a service a behavioural change to that
service. The recording path therefore catches the exception, records what it was,
and ends with a bare `raise` in the same frame that made the call, so the object
that propagates is the object that was raised, carrying the traceback it already
had, its cause, and its context (Requirement 3.2).

**Unconfigured means pass-through, decided per call rather than at decoration.**
Configuration is read the first time a decorated callable runs, and a resolution
that finds no destination for memory, or that fails outright, leaves a recorder
that records nothing. The pass-through branch calls the wrapped callable and
returns, with no surrounding handler and no work after the return, so the return
value, the exception, and the traceback are exactly what the undecorated callable
produced (Requirement 3.5). Deciding per call rather than at decoration is what
lets a process configure Molt after its modules have been imported.

**Duration is measured on a monotonic reading from an injected time source.** A
wall reading can step backwards under a clock correction and would then report a
negative duration, so the interval comes from the monotonic reading while the
Event's own instant comes from the wall reading. Both are read through the same
injected source, which is what makes the millisecond figure a value a test sets
rather than a value a test measures (Requirement 3.4).

**Every Event belongs to a Session, so a decorated call outside a Session block
opens one for the duration of that call.** A Ledger row carries a Session
identifier and a Client identifier that are not nullable, so there is no such
thing as a tool call Event outside a Session. A call with no enclosing block is
therefore bracketed by a session start and a session end of its own, bound to the
reserved unassigned Client, rather than being dropped.

**Nothing here reaches a database and nothing here transmits.** Events go to an
`EventSink`, and the delivered sink appends them to the bounded local spool, which
holds records rather than prepared requests. That is the seam a transmitter
attaches to: pass a sink to `configure` and the same Events go wherever that sink
sends them, with nothing in this module holding a credential or computing a
signature.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Final, ParamSpec, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from molt.capture.spool import Spool, resolve_machine_id
from molt.config.resolve import ConfigError, Configuration, load_configuration
from molt.models.event import Event, EventCategory, JsonObject, JsonValue, decode_capture_text
from molt.models.session import (
    UNASSIGNED_CLIENT_ID,
    UNASSIGNED_CLIENT_SLUG,
    SessionOutcome,
)
from molt.redact import RedactionSettings, redact_payload
from molt.telemetry import Severity, log

__all__ = [
    "AGENT_CLI",
    "COMPONENT",
    "DESTINATION_KEY",
    "CallRecord",
    "Clock",
    "EventSink",
    "NullSink",
    "Recorder",
    "SessionRef",
    "SpoolSink",
    "SystemClock",
    "configure",
    "current",
    "molt_session",
    "molt_tool",
    "reset",
]

# The component name every log record from this module carries.
COMPONENT: Final[str] = "capture"

# What this surface reports as the agent tool identity. The hook adapters derive
# their identity from the invoking hook format; there is no hook here, so the
# identity is the surface itself.
AGENT_CLI: Final[str] = "decorator"

# The configuration key that decides whether Molt has a destination for memory.
# Absent, and there is nowhere for an Event to go, which is what "unconfigured"
# means for this surface.
DESTINATION_KEY: Final[str] = "MOLT_COLLECTOR_URL"

# How many milliseconds one second holds, and how many fractional digits a
# duration keeps. Three digits is microsecond resolution, which is finer than any
# call this surface wraps and coarse enough that the value stays readable.
MILLISECONDS_PER_SECOND: Final[float] = 1000.0
DURATION_DIGITS: Final[int] = 3

# How deep a described argument or result is walked, and how many entries of one
# container are kept. Both bounds exist because a payload of unbounded size costs
# the capture path its budget, and dropping whole entries can never cut a secret
# in half the way truncating a string could.
DESCRIBE_MAX_DEPTH: Final[int] = 8
DESCRIBE_MAX_ENTRIES: Final[int] = 64

# Above this rendered size a described result is replaced by its digest and its
# length, which is the same substitution the hook mapping makes for oversized
# file content. A digest is one-way, so the substitution reveals nothing the
# Redactor would have removed.
DESCRIBE_MAX_BYTES: Final[int] = 65536

# How a value of no JSON type, a container past the depth bound, and a truncated
# container are rendered. Each is a description of a value rather than the value,
# so each is text.
TYPE_MARKER: Final[str] = "<{name}>"
TRUNCATION_MARKER: Final[str] = "<truncated>"


# ---------------------------------------------------------------------------
# The two injected dependencies
# ---------------------------------------------------------------------------


class Clock(Protocol):
    """The two readings this module takes, declared structurally.

    A wall reading places an Event on the timeline and a monotonic reading
    measures an interval. They are separate calls because they answer separate
    questions and because only one of the two is safe to subtract.
    """

    def now(self) -> datetime:
        """The current instant, timezone aware."""

    def monotonic(self) -> float:
        """The current monotonic reading in seconds, never moving backwards."""


@dataclass(frozen=True, slots=True)
class SystemClock:
    """The delivered clock, reading the host.

    Held as a class rather than as two module functions so that the injected
    dependency has one name and one type at every call site.
    """

    def now(self) -> datetime:
        """The host's current instant, with an explicit offset attached."""
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        """The host's monotonic reading, which no clock correction moves."""
        return time.monotonic()


class EventSink(Protocol):
    """Where recorded Events go.

    This is the whole coupling between this module and everything downstream. A
    transmitter is attached by implementing this and passing it to `configure`;
    nothing here knows whether the Events are buffered, sent, or discarded.
    """

    def emit(self, events: Sequence[Event]) -> None:
        """Accept a batch of Events, in the order they were observed."""


@dataclass(frozen=True, slots=True)
class NullSink:
    """The sink of an unconfigured recorder, which accepts and discards."""

    def emit(self, events: Sequence[Event]) -> None:
        """Discard the batch."""


@dataclass(frozen=True, slots=True)
class SpoolSink:
    """The delivered sink, appending Events to the bounded local spool.

    The spool holds records rather than prepared requests, so a batch appended
    here is signed with a fresh timestamp by whatever drains it, however long the
    Events sat in the file.
    """

    spool: Spool

    def emit(self, events: Sequence[Event]) -> None:
        """Append the batch to this machine's spool file."""
        self.spool.append(events)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRef:
    """The Session a block opened, as its caller sees it.

    Attributes:
        session_id: The Session identifier every Event of the block carries.
        client_id: The resolved Client identifier.
        client: The Client the caller named, which is the resolved identifier
            rendered as text when the caller named one, and the slug the caller
            passed when that slug resolved to the reserved unassigned Client.
        agent_cli: The agent tool identity recorded on every Event.
        machine_id: The machine the Events were observed on.
        workspace_path: The workspace the run belongs to, when the caller named one.
        started_at: The instant the block was entered.
        recording: Whether Events are being recorded, which is false exactly when
            Molt is unconfigured.
    """

    session_id: UUID
    client_id: UUID
    client: str
    agent_cli: str
    machine_id: str
    workspace_path: str | None
    started_at: datetime
    recording: bool


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One decorated call in progress.

    Attributes:
        tool: The recorded tool name.
        event_id: The tool call Event's identifier, which the tool result Event
            and any error Event carry as their parent.
        started_ticks: The monotonic reading taken before the call.
    """

    tool: str
    event_id: UUID
    started_ticks: float


@dataclass(slots=True)
class _SessionState:
    """The mutable bookkeeping of one open Session.

    The counters are here rather than on `SessionRef` because a reference handed
    to a caller is a fact about the Session's identity, which does not change,
    while the counters are what the session end Event reports.
    """

    ref: SessionRef
    recorder: Recorder
    started_ticks: float
    implicit: bool
    tool_calls: int = 0
    errors: int = 0


_current_session: ContextVar[_SessionState | None] = ContextVar(
    "molt_decorator_session", default=None
)


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


class Recorder:
    """What the decorator surface records through.

    An instance holds the destination, the clock, the machine identity, and the
    redaction settings, so a test drives one directly while an application drives
    the process-wide one and passes no handle around.

    Nothing on this class raises. An Event that could not be built and a sink that
    failed are both reported as a log record, because a recording failure that
    failed its caller would make instrumenting a service a way to break it.
    """

    __slots__ = ("_clock", "_configured", "_machine_id", "_redaction", "_sink")

    def __init__(
        self,
        *,
        sink: EventSink,
        machine_id: str,
        clock: Clock | None = None,
        redaction: RedactionSettings | None = None,
        configured: bool = True,
    ) -> None:
        self._sink = sink
        self._machine_id = machine_id
        self._clock: Clock = SystemClock() if clock is None else clock
        self._redaction = RedactionSettings() if redaction is None else redaction
        self._configured = configured

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration,
        *,
        sink: EventSink | None = None,
        clock: Clock | None = None,
    ) -> Recorder:
        """Build a recorder from the resolved configuration surface.

        A configuration naming no destination for memory yields a recorder that
        records nothing, which is what makes an unconfigured process a
        pass-through rather than a failure. A sink passed here wins over the
        delivered spool sink, and is the seam a transmitter attaches to.
        """
        configured = configuration.optional_text(DESTINATION_KEY) is not None
        machine_id = resolve_machine_id(configuration)
        redaction = RedactionSettings(
            disabled=configuration.flag("MOLT_REDACTION_DISABLED"),
            max_depth=configuration.integer("MOLT_REDACTION_MAX_DEPTH"),
            sensitive_names=frozenset(configuration.text_list("MOLT_REDACTION_SENSITIVE_NAMES")),
        )
        resolved: EventSink
        if sink is not None:
            resolved = sink
        elif configured:
            resolved = SpoolSink(Spool.from_configuration(configuration, machine_id=machine_id))
        else:
            resolved = NullSink()
        return cls(
            sink=resolved,
            machine_id=machine_id,
            clock=clock,
            redaction=redaction,
            configured=configured,
        )

    @classmethod
    def unconfigured(cls, *, clock: Clock | None = None) -> Recorder:
        """A recorder that records nothing, for a process Molt is not set up in."""
        return cls(
            sink=NullSink(),
            machine_id=UNASSIGNED_CLIENT_SLUG,
            clock=clock,
            configured=False,
        )

    # -- properties ------------------------------------------------------

    @property
    def configured(self) -> bool:
        """Whether Events are recorded at all."""
        return self._configured

    @property
    def clock(self) -> Clock:
        """The time source both the Event instants and the durations are read from."""
        return self._clock

    @property
    def machine_id(self) -> str:
        """The machine identifier every recorded Event carries."""
        return self._machine_id

    @property
    def sink(self) -> EventSink:
        """Where recorded Events go."""
        return self._sink

    # -- recording -------------------------------------------------------

    def record(
        self,
        ref: SessionRef,
        category: EventCategory,
        payload: JsonObject,
        *,
        event_id: UUID | None = None,
        parent_event_id: UUID | None = None,
    ) -> None:
        """Redact one payload, build the Event, and hand it to the sink.

        Every failure along the way is contained here: the payload is redacted,
        the Event is constructed, and the sink is called inside one handler, so a
        clock returning an unusable instant and a spool directory that cannot be
        written are both log records rather than exceptions reaching the
        instrumented callable.
        """
        if not self._configured:
            return
        try:
            result = redact_payload(payload, session_id=ref.session_id, settings=self._redaction)
            if result.warning is not None:
                log(
                    Severity.WARNING,
                    COMPONENT,
                    "redaction is disabled, so this Session's payloads are recorded unmodified",
                    record=result.warning.record,
                    session_id=str(ref.session_id),
                )
            event = Event(
                id=uuid4() if event_id is None else event_id,
                session_id=ref.session_id,
                client_id=ref.client_id,
                category=category,
                occurred_at=self._clock.now(),
                agent_cli=ref.agent_cli,
                machine_id=ref.machine_id,
                parent_event_id=parent_event_id,
                payload=result.payload,
                redacted=result.modified,
                text_body=None,
            )
            self._sink.emit((event,))
        except Exception as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "an observation could not be recorded, and the instrumented call is unaffected",
                category=str(category),
                session_id=str(ref.session_id),
                error_type=type(error).__name__,
            )

    def elapsed_ms(self, started_ticks: float) -> float:
        """How many milliseconds have passed since a monotonic reading.

        The reading is monotonic, so the result cannot be negative, and it is read
        through the injected clock, so a test states the interval rather than
        waiting for it.
        """
        elapsed = self._clock.monotonic() - started_ticks
        return round(max(elapsed, 0.0) * MILLISECONDS_PER_SECOND, DURATION_DIGITS)


# ---------------------------------------------------------------------------
# The process-wide recorder
# ---------------------------------------------------------------------------

_default: Recorder | None = None


def current() -> Recorder:
    """The process-wide recorder, resolved from configuration on first use.

    Resolution happens on the first decorated call rather than at decoration, so
    a process that configures Molt after importing its own modules is still
    recorded. A configuration that cannot be resolved at all yields a recorder
    that records nothing, because a decorator is not entitled to fail a service
    over the state of its own configuration.
    """
    global _default
    if _default is None:
        _default = _resolve_recorder()
    return _default


def configure(
    configuration: Configuration,
    *,
    sink: EventSink | None = None,
    clock: Clock | None = None,
) -> Recorder:
    """Replace the process-wide recorder with one built from the configuration.

    The sink and the clock are the two seams: a transmitter is attached by
    passing a sink, and a test states the passage of time by passing a clock.
    """
    global _default
    _default = Recorder.from_configuration(configuration, sink=sink, clock=clock)
    return _default


def reset() -> None:
    """Discard the process-wide recorder, so the next call resolves a fresh one."""
    global _default
    _default = None


def _resolve_recorder() -> Recorder:
    """Build a recorder from the ambient configuration, or one that records nothing.

    Every failure mode of resolution lands in the same place: an unreadable
    configuration file, a key of the wrong kind, and a machine identifier that
    reduces to nothing all leave the surface a pass-through rather than raising
    inside somebody's tool call.
    """
    try:
        configuration = load_configuration()
        return Recorder.from_configuration(configuration)
    except (ConfigError, OSError, ValueError) as error:
        log(
            Severity.INFO,
            COMPONENT,
            "Molt is unconfigured for this process, so the decorators pass through",
            error_type=type(error).__name__,
        )
        return Recorder.unconfigured()


# ---------------------------------------------------------------------------
# Describing a value
# ---------------------------------------------------------------------------


def _describe(value: object, depth: int = 0) -> JsonValue:
    """Render one argument or result as a JSON value, bounded in depth and width.

    A value of a JSON type is carried as itself, bytes are decoded at the capture
    boundary, and a value of any other type is rendered as its type name rather
    than through its own text conversion: a type name is a description that costs
    nothing and reveals nothing, whereas an arbitrary conversion may be large,
    may embed an address that differs between runs, and may hold content the
    caller never meant to record.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else TYPE_MARKER.format(name=type(value).__name__)
    if isinstance(value, bytes):
        return decode_capture_text(value)
    if depth >= DESCRIBE_MAX_DEPTH:
        return TYPE_MARKER.format(name=type(value).__name__)
    if isinstance(value, Mapping):
        return _describe_mapping(value, depth)
    if isinstance(value, (list, tuple)):
        return _describe_sequence(value, depth)
    return TYPE_MARKER.format(name=type(value).__name__)


def _describe_mapping(value: Mapping[object, object], depth: int) -> JsonObject:
    """Describe a mapping's first entries, reporting how many were left out."""
    described: JsonObject = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= DESCRIBE_MAX_ENTRIES:
            described[TRUNCATION_MARKER] = len(value) - DESCRIBE_MAX_ENTRIES
            break
        described[str(key)] = _describe(item, depth + 1)
    return described


def _describe_sequence(value: Sequence[object], depth: int) -> list[JsonValue]:
    """Describe a sequence's first entries, marking that the rest were left out."""
    described: list[JsonValue] = []
    for index, item in enumerate(value):
        if index >= DESCRIBE_MAX_ENTRIES:
            described.append(TRUNCATION_MARKER)
            break
        described.append(_describe(item, depth + 1))
    return described


def _bounded(value: JsonValue) -> JsonValue:
    """Replace a description larger than the payload cap by its digest and length.

    The substitution is one-way, so an oversized value reveals nothing that the
    Redactor would have removed, and it is a substitution of the whole value
    rather than a truncation of it, because cutting a string in half can leave a
    fragment of a secret that no pattern still matches.
    """
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    raw = rendered.encode("utf-8")
    if len(raw) <= DESCRIBE_MAX_BYTES:
        return value
    return {"digest": hashlib.sha256(raw).hexdigest(), "length": len(raw)}


def _signature_of(function: Callable[..., object]) -> inspect.Signature | None:
    """The wrapped callable's signature, taken once at decoration.

    Held rather than read per call because reading a signature is an order of
    magnitude dearer than the recording it serves. A callable that reports no
    signature at all, such as one implemented in C, yields None and its arguments
    are described positionally.
    """
    try:
        return inspect.signature(function)
    except (TypeError, ValueError):
        return None


def _describe_arguments(
    signature: inspect.Signature | None,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> JsonValue:
    """Describe a call's arguments, under their parameter names where known.

    Naming the arguments is what lets the Redactor act on them: its
    sensitive-name set matches keys, so an argument named for a credential is
    replaced wholesale rather than only when its value happens to look like one.
    """
    if signature is not None:
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError:
            return _describe_positionally(args, kwargs)
        return {name: _describe(value) for name, value in bound.arguments.items()}
    return _describe_positionally(args, kwargs)


def _describe_positionally(args: tuple[object, ...], kwargs: Mapping[str, object]) -> JsonObject:
    """Describe arguments that could not be bound to parameter names."""
    return {
        "positional": [_describe(item) for item in args],
        "keyword": {name: _describe(item) for name, item in kwargs.items()},
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _resolve_client(client: str) -> tuple[UUID, str]:
    """Resolve what a caller named as a Client into an identifier and a label.

    A caller holding the identifier passes it and the Session is bound to it. A
    caller naming a slug is bound to the reserved unassigned Client, because the
    workspace-to-Client mapping is the Collector's to apply and this surface
    holds no database credential to resolve a slug with.
    """
    try:
        resolved = UUID(client)
    except ValueError:
        return UNASSIGNED_CLIENT_ID, client or UNASSIGNED_CLIENT_SLUG
    return resolved, str(resolved)


@contextmanager
def _opened(
    recorder: Recorder,
    *,
    client: str,
    agent_cli: str,
    workspace_path: str | None,
    implicit: bool,
) -> Iterator[_SessionState]:
    """Open a Session, bind it for the duration of the block, and close it.

    The outcome is failed until the block completes, so a block left by an
    exception closes its Session as failed without the exception being inspected
    or caught here. The close and the unbinding sit in a `finally`, which is what
    makes a Session that was opened always a Session that was closed.
    """
    state = _open_session(
        recorder,
        client=client,
        agent_cli=agent_cli,
        workspace_path=workspace_path,
        implicit=implicit,
    )
    token: Token[_SessionState | None] = _current_session.set(state)
    outcome = SessionOutcome.FAILED
    try:
        yield state
        outcome = SessionOutcome.SUCCEEDED
    finally:
        _close_session(state, outcome)
        _current_session.reset(token)


def _open_session(
    recorder: Recorder,
    *,
    client: str,
    agent_cli: str,
    workspace_path: str | None,
    implicit: bool,
) -> _SessionState:
    """Build the Session bookkeeping and record the session start Event."""
    client_id, label = _resolve_client(client)
    ref = SessionRef(
        session_id=uuid4(),
        client_id=client_id,
        client=label,
        agent_cli=agent_cli,
        machine_id=recorder.machine_id,
        workspace_path=workspace_path,
        started_at=recorder.clock.now(),
        recording=recorder.configured,
    )
    state = _SessionState(
        ref=ref,
        recorder=recorder,
        started_ticks=recorder.clock.monotonic(),
        implicit=implicit,
    )
    recorder.record(
        ref,
        EventCategory.SESSION_START,
        {
            "client": ref.client,
            "agent_cli": ref.agent_cli,
            "machine_id": ref.machine_id,
            "workspace_path": ref.workspace_path,
            "implicit": implicit,
        },
    )
    return state


def _close_session(state: _SessionState, outcome: SessionOutcome) -> None:
    """Record the session end Event, carrying the outcome and the counters."""
    state.recorder.record(
        state.ref,
        EventCategory.SESSION_END,
        {
            "outcome": str(outcome),
            "tool_call_count": state.tool_calls,
            "error_count": state.errors,
            "duration_ms": state.recorder.elapsed_ms(state.started_ticks),
        },
    )


@contextmanager
def molt_session(
    client: str,
    agent_cli: str = AGENT_CLI,
    *,
    workspace_path: str | None = None,
    recorder: Recorder | None = None,
) -> Iterator[SessionRef]:
    """Bound one run of an in-house agent, opening and closing a Session.

    Every decorated call inside the block records into this Session, including a
    call made in a function the block never mentions, because the binding travels
    on a context variable rather than on an argument.

    Args:
        client: The Client identifier, or a slug for a Session the Collector will
            map to a Client itself.
        agent_cli: What the recorded Events name as the agent tool identity.
        workspace_path: The workspace this run belongs to, where there is one.
        recorder: A recorder to use instead of the process-wide one, which is how
            a test drives the surface without configuring the process.

    Yields:
        The Session the block opened, whose `recording` attribute is false
        exactly when Molt is unconfigured.
    """
    active = current() if recorder is None else recorder
    with _opened(
        active,
        client=client,
        agent_cli=agent_cli,
        workspace_path=workspace_path,
        implicit=False,
    ) as state:
        yield state.ref


@contextmanager
def _session_for_call(recorder: Recorder) -> Iterator[_SessionState]:
    """The Session a decorated call records into, opening one where none is open.

    A Ledger row carries a Session identifier that is not nullable, so there is
    no such thing as a tool call Event outside a Session. A call made outside any
    block is therefore bracketed by a Session of its own rather than dropped.
    """
    existing = _current_session.get()
    if existing is not None:
        yield existing
        return
    with _opened(
        recorder,
        client=UNASSIGNED_CLIENT_SLUG,
        agent_cli=AGENT_CLI,
        workspace_path=None,
        implicit=True,
    ) as state:
        yield state


# ---------------------------------------------------------------------------
# Recording one call
# ---------------------------------------------------------------------------


def _begin_call(
    state: _SessionState,
    tool: str,
    signature: inspect.Signature | None,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> CallRecord:
    """Record the tool call Event and take the reading the duration is measured from.

    The reading is taken after the Event has been handed to the sink, so the
    duration reported is the wrapped callable's rather than the wrapped
    callable's plus this module's.
    """
    event_id = uuid4()
    state.tool_calls += 1
    state.recorder.record(
        state.ref,
        EventCategory.TOOL_CALL,
        {"tool": tool, "arguments": _bounded(_describe_arguments(signature, args, kwargs))},
        event_id=event_id,
    )
    return CallRecord(
        tool=tool,
        event_id=event_id,
        started_ticks=state.recorder.clock.monotonic(),
    )


def _finish_call(state: _SessionState, call: CallRecord, result: object) -> None:
    """Record the tool result Event, linked to its tool call Event."""
    state.recorder.record(
        state.ref,
        EventCategory.TOOL_RESULT,
        {
            "tool": call.tool,
            "duration_ms": state.recorder.elapsed_ms(call.started_ticks),
            "result": _bounded(_describe(result)),
        },
        parent_event_id=call.event_id,
    )


def _fail_call(state: _SessionState, call: CallRecord, error: Exception) -> None:
    """Record the error Event carrying the exception type and its redacted message.

    Only the observation is made here. The exception itself is re-raised by the
    caller of this function with a bare `raise`, so nothing in this module ever
    holds, wraps, or replaces the object that was raised.
    """
    state.errors += 1
    state.recorder.record(
        state.ref,
        EventCategory.ERROR,
        {
            "tool": call.tool,
            "duration_ms": state.recorder.elapsed_ms(call.started_ticks),
            "error_type": type(error).__name__,
            "message": _bounded(str(error)),
        },
        parent_event_id=call.event_id,
    )


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------

_P = ParamSpec("_P")
_R = TypeVar("_R")


def molt_tool(name: str | None = None) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Record one callable as a tool: a call Event before, a result Event after.

    The decorated callable's behaviour is unchanged in every case. It returns what
    it returned, it raises what it raised, and with Molt unconfigured it is called
    with nothing around it at all. A coroutine function is wrapped as a coroutine
    function, so the duration measured is the duration of the awaited call rather
    than the time taken to build a coroutine object.

    Args:
        name: What the Events call this tool. Defaults to the callable's
            qualified name, which distinguishes two methods sharing a name.

    Returns:
        The decorator, whose result keeps the wrapped callable's name, docstring,
        annotations, and introspectable signature.
    """

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        tool = name if name else getattr(function, "__qualname__", repr(function))
        signature = _signature_of(function)
        if inspect.iscoroutinefunction(function):
            return cast("Callable[_P, _R]", _wrap_async(function, tool, signature))
        return _wrap_sync(function, tool, signature)

    return decorate


def _wrap_sync(
    function: Callable[_P, _R],
    tool: str,
    signature: inspect.Signature | None,
) -> Callable[_P, _R]:
    """Wrap a synchronous callable, passing through when Molt is unconfigured."""

    @functools.wraps(function)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        recorder = _active_recorder()
        if not recorder.configured:
            return function(*args, **kwargs)
        with _session_for_call(recorder) as state:
            call = _begin_call(state, tool, signature, args, kwargs)
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                _fail_call(state, call, error)
                raise
            _finish_call(state, call, result)
            return result

    return wrapper


def _wrap_async(
    function: Callable[_P, Awaitable[_R]],
    tool: str,
    signature: inspect.Signature | None,
) -> Callable[_P, Awaitable[_R]]:
    """Wrap a coroutine function, measuring the awaited call rather than its creation."""

    @functools.wraps(function)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        recorder = _active_recorder()
        if not recorder.configured:
            return await function(*args, **kwargs)
        with _session_for_call(recorder) as state:
            call = _begin_call(state, tool, signature, args, kwargs)
            try:
                result = await function(*args, **kwargs)
            except Exception as error:
                _fail_call(state, call, error)
                raise
            _finish_call(state, call, result)
            return result

    return wrapper


def _active_recorder() -> Recorder:
    """The recorder a call records through: the open Session's, or the process's.

    An open Session keeps the recorder it was opened with, so a block a test gave
    its own recorder to is not overtaken by the process-wide one part way through.
    """
    state = _current_session.get()
    if state is not None:
        return state.recorder
    return current()
