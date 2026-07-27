"""Unit checks over the direct instrumentation surface: the decorator and the block.

These cover the five claims Requirement 3 makes rather than every branch of the
module: the pair of Events around one call and the link between them, the
duration in milliseconds read from an injected clock, an exception becoming an
error Event while the original object propagates, the block opening and closing
one Session, and an unconfigured process observing no change at all.

The decorated callables sit at module level so that each recorded tool name is
the plain name of the callable, and the clock is the injected manual one, so the
millisecond figure is a value these tests state rather than one they measure.
"""

from __future__ import annotations

import asyncio
import inspect
import traceback
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

import pytest

from molt.capture.decorator import (
    AGENT_CLI,
    DESTINATION_KEY,
    Recorder,
    configure,
    current,
    molt_session,
    molt_tool,
    reset,
)
from molt.capture.spool import Spool
from molt.config.resolve import Configuration
from molt.models.event import Event, EventCategory, JsonObject
from molt.models.session import UNASSIGNED_CLIENT_ID, UNASSIGNED_CLIENT_SLUG
from molt.redact import REDACTION_PLACEHOLDER

MACHINE_ID: Final[str] = "test-machine"

# A destination for memory, which is what makes a process configured. Nothing in
# this module opens a connection to it.
DESTINATION: Final[str] = "https://collector.example/ingest"

# A credential-shaped span built from a keyword and a long run of one character,
# so this module states no plausible secret while still exercising the Redactor.
BEARER_TAIL: Final[str] = "z" * 32
BEARER_SPAN: Final[str] = f"bearer {BEARER_TAIL}"


class ManualClock(Protocol):
    """The three calls these tests make on the injected clock."""

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
    """Stand in for a tool an in-house agent calls."""
    return f"{subject}:{limit}"


@molt_tool(name="slow-read")
def slow_read(clock: ManualClock, seconds: float) -> None:
    """Spend a stated interval, so the recorded duration is a value a test set."""
    clock.advance(seconds)


@molt_tool()
def failing_tool(error: Exception) -> None:
    """Raise the exception it was handed, so the test owns the object raised."""
    raise error


@molt_tool()
def fetch(url: str, api_key: str) -> str:
    """Take an argument named for a credential, which the Redactor acts on."""
    return url if api_key else ""


@molt_tool()
def standalone() -> int:
    """Be called outside any Session block."""
    return 7


@molt_tool()
async def fetch_rows(clock: ManualClock, count: int) -> int:
    """Await an interval, so the coroutine's own duration is measured."""
    clock.advance(0.75)
    return count


# ---------------------------------------------------------------------------
# Fixtures and helpers
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

    def only(self, category: EventCategory) -> Event:
        """The one recorded Event of a category, refusing any other count."""
        matches = [event for event in self.events if event.category is category]
        assert len(matches) == 1, f"expected one {category} Event, saw {len(matches)}"
        return matches[0]


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


def mapping_at(payload: JsonObject, key: str) -> JsonObject:
    """One payload field read as a mapping."""
    value = payload[key]
    assert isinstance(value, dict)
    return value


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
# The pair of Events around one call
# ---------------------------------------------------------------------------


def test_a_call_records_a_tool_call_then_a_tool_result_linked_back_to_it(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    with molt_session("acme") as session:
        assert summarise("ledger", limit=3) == "ledger:3"

    assert sink.categories == [
        EventCategory.SESSION_START,
        EventCategory.TOOL_CALL,
        EventCategory.TOOL_RESULT,
        EventCategory.SESSION_END,
    ]
    call = sink.only(EventCategory.TOOL_CALL)
    result = sink.only(EventCategory.TOOL_RESULT)
    assert result.parent_event_id == call.id
    assert call.session_id == session.session_id == result.session_id
    assert call.agent_cli == AGENT_CLI
    assert call.machine_id == MACHINE_ID
    assert text_at(call.payload, "tool") == "summarise"
    assert mapping_at(call.payload, "arguments") == {"subject": "ledger", "limit": 3}
    assert text_at(result.payload, "result") == "ledger:3"


def test_the_recorded_duration_is_the_interval_the_clock_was_advanced_by(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    with molt_session("acme"):
        slow_read(time_source, 0.25)

    result = sink.only(EventCategory.TOOL_RESULT)
    assert text_at(result.payload, "tool") == "slow-read"
    assert number_at(result.payload, "duration_ms") == pytest.approx(250.0)
    # A value of no JSON type is described by its type rather than carried.
    assert mapping_at(sink.only(EventCategory.TOOL_CALL).payload, "arguments") == {
        "clock": "<ManualTimeSource>",
        "seconds": 0.25,
    }


# ---------------------------------------------------------------------------
# The exception path
# ---------------------------------------------------------------------------


def test_an_exception_becomes_an_error_event_and_the_original_object_propagates(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(with_destination(tmp_path), sink=sink, clock=time_source)
    refusal = RuntimeError(f"upstream refused: {BEARER_SPAN}")

    with molt_session("acme"), pytest.raises(RuntimeError) as caught:
        failing_tool(refusal)

    # The object that propagates is the object that was raised, carrying the
    # frame it was raised in and neither a cause nor a context of its own.
    assert caught.value is refusal
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    frames = [frame.name for frame in traceback.extract_tb(caught.value.__traceback__)]
    assert "failing_tool" in frames

    error = sink.only(EventCategory.ERROR)
    assert error.parent_event_id == sink.only(EventCategory.TOOL_CALL).id
    assert text_at(error.payload, "error_type") == "RuntimeError"
    assert REDACTION_PLACEHOLDER in text_at(error.payload, "message")
    assert BEARER_TAIL not in text_at(error.payload, "message")
    assert error.redacted is True
    assert EventCategory.TOOL_RESULT not in sink.categories


def test_a_block_left_by_an_exception_closes_its_session_as_failed(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    with pytest.raises(ValueError, match="refused"), molt_session("acme"):
        failing_tool(ValueError("refused"))

    end = sink.only(EventCategory.SESSION_END)
    assert text_at(end.payload, "outcome") == "failed"
    assert number_at(end.payload, "tool_call_count") == 1
    assert number_at(end.payload, "error_count") == 1


# ---------------------------------------------------------------------------
# The Session block
# ---------------------------------------------------------------------------


def test_the_block_opens_one_session_on_entry_and_closes_it_on_exit(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    with molt_session("acme", "in-house-agent", workspace_path="/work/acme") as session:
        time_source.advance(1.5)

    assert sink.categories == [EventCategory.SESSION_START, EventCategory.SESSION_END]
    start = sink.only(EventCategory.SESSION_START)
    end = sink.only(EventCategory.SESSION_END)
    assert start.session_id == session.session_id == end.session_id
    assert session.client == "acme"
    assert session.client_id == UNASSIGNED_CLIENT_ID
    assert session.agent_cli == "in-house-agent"
    assert session.recording is True
    assert text_at(start.payload, "workspace_path") == "/work/acme"
    assert text_at(end.payload, "outcome") == "succeeded"
    assert number_at(end.payload, "duration_ms") == pytest.approx(1500.0)


def test_a_call_outside_any_block_is_bracketed_by_a_session_of_its_own(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    assert standalone() == 7

    assert sink.categories == [
        EventCategory.SESSION_START,
        EventCategory.TOOL_CALL,
        EventCategory.TOOL_RESULT,
        EventCategory.SESSION_END,
    ]
    start = sink.only(EventCategory.SESSION_START)
    assert start.payload["implicit"] is True
    assert text_at(start.payload, "client") == UNASSIGNED_CLIENT_SLUG
    assert {event.session_id for event in sink.events} == {start.session_id}


# ---------------------------------------------------------------------------
# Pass-through when Molt is unconfigured
# ---------------------------------------------------------------------------


def test_an_unconfigured_process_records_nothing_and_returns_what_the_callable_returns(
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    recorder = configure(without_destination(), sink=sink, clock=time_source)

    with molt_session("acme") as session:
        assert summarise("ledger", limit=3) == "ledger:3"

    assert recorder.configured is False
    assert session.recording is False
    assert sink.events == []


def test_an_unconfigured_process_propagates_the_original_exception_untouched(
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(without_destination(), sink=sink, clock=time_source)
    refusal = KeyError("absent")

    with pytest.raises(KeyError) as caught:
        failing_tool(refusal)

    assert caught.value is refusal
    assert caught.value.__context__ is None
    frames = [frame.name for frame in traceback.extract_tb(caught.value.__traceback__)]
    assert "failing_tool" in frames
    assert sink.events == []


def test_the_wrapped_callable_keeps_its_name_docstring_annotations_and_signature() -> None:
    def original(subject: str, limit: int = 2) -> str:
        """What the wrapped callable's docstring says."""
        return f"{subject}:{limit}"

    decorated = molt_tool()(original)

    assert decorated.__name__ == original.__name__
    assert decorated.__doc__ == original.__doc__
    assert decorated.__annotations__ == original.__annotations__
    assert inspect.signature(decorated) == inspect.signature(original)


# ---------------------------------------------------------------------------
# Redaction, coroutines, and the spool
# ---------------------------------------------------------------------------


def test_an_argument_named_for_a_credential_is_replaced_before_the_event_is_built(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    with molt_session("acme"):
        fetch("https://service.example/items", api_key="the-event-must-not-hold-this")

    call = sink.only(EventCategory.TOOL_CALL)
    arguments = mapping_at(call.payload, "arguments")
    assert arguments["api_key"] == REDACTION_PLACEHOLDER
    assert arguments["url"] == "https://service.example/items"
    assert call.redacted is True


def test_a_coroutine_function_is_recorded_around_the_awaited_call(
    tmp_path: Path,
    sink: RecordingSink,
    time_source: ManualClock,
) -> None:
    configure(with_destination(tmp_path), sink=sink, clock=time_source)

    assert inspect.iscoroutinefunction(fetch_rows)
    assert asyncio.run(fetch_rows(time_source, 4)) == 4

    result = sink.only(EventCategory.TOOL_RESULT)
    assert result.payload["result"] == 4
    assert number_at(result.payload, "duration_ms") == pytest.approx(750.0)


def test_with_no_sink_delivered_the_events_land_in_this_machines_spool(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    configuration = with_destination(tmp_path)
    configure(configuration, clock=time_source)

    with molt_session("acme"):
        assert standalone() == 7

    spool = Spool.from_configuration(configuration)
    assert spool.machine_id == MACHINE_ID
    assert [event.category for event in spool.records()] == [
        EventCategory.SESSION_START,
        EventCategory.TOOL_CALL,
        EventCategory.TOOL_RESULT,
        EventCategory.SESSION_END,
    ]


def test_a_process_with_no_resolvable_configuration_resolves_a_recorder_that_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # An empty environment and a working directory holding no configuration file
    # is the state of a process Molt was never set up in.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DESTINATION_KEY, raising=False)

    recorder = current()

    assert isinstance(recorder, Recorder)
    assert recorder.configured is False
