"""The decorator's three obligations to the service it instruments.

Requirement 3 asks for three things of the direct instrumentation surface, and
this suite states each of them where the surface's own behavioural cases do not:
the tool call and tool result Events of one call (Requirement 3.1), an error Event
carrying what failed while the object that was raised propagates unchanged
(Requirement 3.2), and a process Molt is not configured in observing no change at
all (Requirement 3.5).

The angles here are the ones a single-call case cannot reach. A call is made twice
so that the pair-to-pair linkage is asserted rather than the one pair; an exception
is raised with a cause so that the chain is asserted to survive the recording; an
exception that is not an `Exception` at all is raised so that the surface is held
to catching what it declares and no more; a sink is made to fail so that a
recording failure is asserted to cost the observation rather than the call; and the
configured state is moved in both directions around one decorated callable, which
is what makes the pass-through decision a per-call one rather than a per-decoration
one.

The clock is the injected manual time source and the sink is a local double, so no
configuration is read, no spool file is written unless a case asks for one, and no
duration is measured by waiting.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

import pytest

from molt.capture.decorator import (
    DESTINATION_KEY,
    configure,
    molt_session,
    molt_tool,
    reset,
)
from molt.config.resolve import Configuration
from molt.models.event import Event, EventCategory, JsonObject
from molt.redact import REDACTION_PLACEHOLDER

MACHINE_ID: Final[str] = "test-machine"

# A destination for memory, which is what makes a process configured. Nothing
# here opens a connection to it.
DESTINATION: Final[str] = "https://collector.example/ingest"

# A credential-shaped span built from a keyword and a long run of one character,
# so this module states no plausible secret while still exercising the Redactor.
SHAPED_TAIL: Final[str] = "q" * 32
SHAPED_SPAN: Final[str] = f"bearer {SHAPED_TAIL}"


class ManualClock(Protocol):
    """The readings the surface takes from the injected time source."""

    def now(self) -> datetime:
        """The current wall reading."""

    def monotonic(self) -> float:
        """The current monotonic reading in seconds."""

    def advance(self, seconds: float) -> None:
        """Move both readings forward."""


# ---------------------------------------------------------------------------
# The instrumented callables under test
# ---------------------------------------------------------------------------


@molt_tool()
def summarise(subject: str, limit: int = 2) -> str:
    """Stand in for a tool an in-house agent calls more than once."""
    return f"{subject}:{limit}"


@molt_tool()
def raise_given(error: BaseException) -> None:
    """Raise the object it was handed, so the case owns the object raised."""
    raise error


@molt_tool()
def raise_chained() -> None:
    """Fail with a cause, so the chain the caller would see is a real one."""
    try:
        raise KeyError("the row is absent")
    except KeyError as cause:
        raise ValueError(f"the writer refused: {SHAPED_SPAN}") from cause


@molt_tool()
def standalone() -> int:
    """Be called outside any Session block."""
    return 7


# ---------------------------------------------------------------------------
# Doubles, fixtures, and helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecordingSink:
    """A sink that keeps what it was handed, in the order it was handed it."""

    events: list[Event] = field(default_factory=list)

    def emit(self, events: Sequence[Event]) -> None:
        """Keep the batch."""
        self.events.extend(events)

    @property
    def categories(self) -> list[EventCategory]:
        """Every recorded category, in order."""
        return [event.category for event in self.events]

    def of(self, category: EventCategory) -> list[Event]:
        """Every recorded Event of one category, in order."""
        return [event for event in self.events if event.category is category]

    def only(self, category: EventCategory) -> Event:
        """The one recorded Event of a category, refusing any other count."""
        matches = self.of(category)
        assert len(matches) == 1, f"expected one {category} Event, saw {len(matches)}"
        return matches[0]


@dataclass(slots=True)
class FailingSink:
    """A sink that refuses every batch, standing in for a spool that cannot be written."""

    refusals: int = 0

    def emit(self, events: Sequence[Event]) -> None:
        """Refuse the batch, as a filesystem fault would."""
        assert events is not None
        self.refusals += 1
        raise OSError("the spool directory cannot be written")


@pytest.fixture(autouse=True)
def _fresh_surface() -> Iterator[None]:
    """Leave no process-wide recorder behind, in either direction."""
    reset()
    yield
    reset()


@pytest.fixture
def sink() -> RecordingSink:
    """A fresh recording sink per test."""
    return RecordingSink()


def with_destination(tmp_path: Path) -> Configuration:
    """A configuration naming a destination for memory, so Molt is configured."""
    return Configuration(
        environ={
            DESTINATION_KEY: DESTINATION,
            "MOLT_SPOOL_DIR": str(tmp_path / "spool"),
            "MOLT_MACHINE_ID": MACHINE_ID,
        },
        file_values={},
    )


def without_destination() -> Configuration:
    """A configuration naming no destination, which is what unconfigured means."""
    return Configuration(environ={"MOLT_MACHINE_ID": MACHINE_ID}, file_values={})


def text_at(payload: JsonObject, key: str) -> str:
    """One payload field read as text."""
    value = payload[key]
    assert isinstance(value, str)
    return value


def number_at(payload: JsonObject, key: str) -> float:
    """One payload field read as a number."""
    value = payload[key]
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


# ---------------------------------------------------------------------------
# Requirement 3.1: the pair, and one pair per call
# ---------------------------------------------------------------------------


def test_two_calls_in_one_session_produce_two_pairs_each_linked_to_its_own_call(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    """Each result names the call it belongs to, not merely some call of the Session.

    One pair proves the link exists; two pairs prove the link is per call, which is
    what a Ledger consumer walking from a result back to its arguments depends on.
    """
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    with molt_session("acme"):
        assert summarise("ledger", limit=1) == "ledger:1"
        time_source.advance(0.5)
        assert summarise("writer", limit=2) == "writer:2"

    assert sink.categories == [
        EventCategory.SESSION_START,
        EventCategory.TOOL_CALL,
        EventCategory.TOOL_RESULT,
        EventCategory.TOOL_CALL,
        EventCategory.TOOL_RESULT,
        EventCategory.SESSION_END,
    ]
    calls = sink.of(EventCategory.TOOL_CALL)
    results = sink.of(EventCategory.TOOL_RESULT)
    assert calls[0].id != calls[1].id
    assert [result.parent_event_id for result in results] == [calls[0].id, calls[1].id]
    assert [text_at(result.payload, "result") for result in results] == ["ledger:1", "writer:2"]
    assert number_at(sink.only(EventCategory.SESSION_END).payload, "tool_call_count") == 2


def test_the_arguments_and_the_result_travel_on_the_pair_and_not_on_the_session(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    """The Session Events describe the run; the pair describes the call.

    Stated because the two kinds of Event are recorded through one path: a payload
    field that leaked from a call into the session end Event would make the Session
    row carry a tool's arguments, which is not what the Ledger's shape says it holds.
    """
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    with molt_session("acme"):
        summarise("ledger")

    call = sink.only(EventCategory.TOOL_CALL)
    result = sink.only(EventCategory.TOOL_RESULT)
    session_fields = set(sink.only(EventCategory.SESSION_START).payload) | set(
        sink.only(EventCategory.SESSION_END).payload
    )
    assert set(call.payload) == {"tool", "arguments"}
    assert set(result.payload) == {"tool", "duration_ms", "result"}
    assert session_fields.isdisjoint({"tool", "arguments", "result"})
    assert call.parent_event_id is None


# ---------------------------------------------------------------------------
# Requirement 3.2: the error Event, and the object that propagates
# ---------------------------------------------------------------------------


def test_a_chained_exception_keeps_its_cause_and_its_context_through_the_recording(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    """The error Event names the type and the redacted message; the chain survives.

    A caller's handler may act on the cause of a failure, so a recording path that
    re-raised through a fresh frame, or that raised a wrapper, would change what
    that handler sees. The bare re-raise leaves the cause and the context as the
    instrumented callable set them.
    """
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    with molt_session("acme"), pytest.raises(ValueError, match="refused") as caught:
        raise_chained()

    assert isinstance(caught.value.__cause__, KeyError)
    assert caught.value.__context__ is caught.value.__cause__
    error = sink.only(EventCategory.ERROR)
    assert error.parent_event_id == sink.only(EventCategory.TOOL_CALL).id
    assert text_at(error.payload, "error_type") == "ValueError"
    assert REDACTION_PLACEHOLDER in text_at(error.payload, "message")
    assert SHAPED_TAIL not in text_at(error.payload, "message")
    assert sink.of(EventCategory.TOOL_RESULT) == []


def test_an_interrupt_is_neither_recorded_nor_swallowed_and_still_closes_the_session(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    """The surface records failures of the call, not the shutdown of the process.

    An interrupt is not a tool failure: recording it would put a machine's shutdown
    in the Ledger as the tool's error, and holding it long enough to record would
    delay the shutdown. It therefore passes through untouched, while the Session it
    interrupted is still closed as failed, because a Session that was opened is
    always a Session that was closed.
    """
    configure(with_destination(tmp_path), sink=sink, clock=time_source)
    interrupt = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt) as caught, molt_session("acme"):
        raise_given(interrupt)

    assert caught.value is interrupt
    assert sink.of(EventCategory.ERROR) == []
    assert sink.of(EventCategory.TOOL_RESULT) == []
    end = sink.only(EventCategory.SESSION_END)
    assert text_at(end.payload, "outcome") == "failed"
    assert number_at(end.payload, "error_count") == 0


def test_a_sink_that_fails_costs_the_observation_rather_than_the_call(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Instrumenting a service must not become a way to break it.

    Every recording failure is contained where the Event is built and handed over,
    so a destination that cannot be written leaves the return value, the raised
    object, and the traceback exactly as the undecorated callable produced them.
    """
    failing = FailingSink()
    configure(with_destination(tmp_path), sink=failing, clock=time_source)
    refusal = RuntimeError("upstream refused")

    with molt_session("acme"):
        assert summarise("ledger", limit=3) == "ledger:3"
        with pytest.raises(RuntimeError) as caught:
            raise_given(refusal)

    assert caught.value is refusal
    assert caught.value.__cause__ is None
    assert failing.refusals > 1


# ---------------------------------------------------------------------------
# Requirement 3.5: pass-through, decided per call
# ---------------------------------------------------------------------------


def test_the_pass_through_decision_follows_the_configured_state_in_both_directions(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    """Configuration is consulted per call, so a process may configure Molt late.

    One decorated callable is called three times: with no destination for memory,
    with one, and with none again. The sink is the same object throughout and is
    handed to the unconfigured recorder as well, which is what shows the gate to be
    the configured state rather than the absence of a destination for the Events.
    """
    configure(without_destination(), sink=sink, clock=time_source)
    assert summarise("first") == "first:2"
    before = list(sink.categories)

    configure(with_destination(tmp_path), sink=sink, clock=time_source)
    assert summarise("second") == "second:2"
    during = list(sink.categories)

    configure(without_destination(), sink=sink, clock=time_source)
    assert summarise("third") == "third:2"

    assert before == []
    assert during == [
        EventCategory.SESSION_START,
        EventCategory.TOOL_CALL,
        EventCategory.TOOL_RESULT,
        EventCategory.SESSION_END,
    ]
    assert sink.categories == during


def test_an_unconfigured_call_outside_any_block_opens_no_session_and_records_nothing(
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    """A call with no enclosing block is bracketed by a Session only when recording.

    With Molt unconfigured the wrapped callable is called with nothing around it,
    so there is no implicit Session to open and nothing to report, and the value
    the caller receives is the one the undecorated callable returned.
    """
    recorder = configure(without_destination(), sink=sink, clock=time_source)

    assert standalone() == 7

    assert recorder.configured is False
    assert sink.events == []
