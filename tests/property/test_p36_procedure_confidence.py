"""Property 36: confidence stays bounded and records only real movement.

The generated stream includes retrievals, every terminal outcome, reports repeated
for one Session, and runs that reach both bounds and attempt to pass them. A small
reference model applies the same configured policy the tracker exposes.

**Validates: Requirements 49.1, 49.5, 49.6, 49.7, 49.12, 49.13**

The change-count clause is compared against a second walk of the drawn event stream
rather than against the list of recorded changes, because a comparison of that list
with itself holds for any implementation at all.

One clause of Requirement 49.13 is not reachable from here. That the value change and
its change record commit in the same transaction is a claim about a transaction, and
this module keeps confidence in memory, so nothing here can observe an interleaving in
which one landed without the other. The bounds, the direction, and the change count are
arithmetic and are asserted here; the same-transaction obligation is asserted against a
live cluster in the integration suite, which is where a transaction exists to observe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from molt.confidence import ConfidencePolicy, movement
from molt.config.resolve import Configuration
from molt.models.session import SessionOutcome

INITIAL: Final[float] = 0.5
MAX_EVENTS: Final[int] = 200
BOUND_ATTEMPTS: Final[int] = 11


@dataclass(frozen=True, slots=True)
class ProcedureEvent:
    """One retrieval, or one reported terminal outcome for a Session."""

    session: int
    outcome: SessionOutcome | None


def policy() -> ConfidencePolicy:
    """The shipped confidence policy read from its configuration surface."""
    return ConfidencePolicy.from_configuration(Configuration(environ={}, file_values={}))


def events_that_moved_the_value(
    events: tuple[ProcedureEvent, ...],
    chosen: ConfidencePolicy,
) -> int:
    """How many of the generated events changed the value, walked from the stream alone.

    The change-record clause of the property compares two counts, so one of them has to
    come from somewhere other than the list of recorded changes. This walk is that other
    place: the interval arithmetic is written out here rather than taken from the
    tracker's own movement helper, and the only inputs are the drawn events and the
    configured deltas.

    Four rules decide whether an event moved anything, each of them a rule the property
    states rather than one this walk invents. A retrieval adjusts nothing. A Session
    contributes at most one outcome, so a repeated report of the same Session moves
    nothing. An abandoned outcome asks for no adjustment at all. And an adjustment a
    bound absorbs entirely leaves the value exactly where it stood.
    """
    value = INITIAL
    retrieved: set[int] = set()
    contributed: set[int] = set()
    moved = 0
    for event in events:
        if event.outcome is None:
            retrieved.add(event.session)
            continue
        if event.session in contributed or event.session not in retrieved:
            continue
        contributed.add(event.session)
        delta = chosen.delta_for(event.outcome)
        if delta is None:
            continue
        reached = min(1.0, max(0.0, value + delta))
        if reached != value:
            moved += 1
        value = reached
    return moved


@st.composite
def procedure_event_sequences(draw: st.DrawFn) -> tuple[ProcedureEvent, ...]:
    """Generate one bounded stream, including bound runs and repeated reports."""
    scenario = draw(st.sampled_from(("random", "ceiling", "floor", "both")))
    events: list[ProcedureEvent] = []
    next_session = 0

    def add_outcome(outcome: SessionOutcome, *, duplicate: bool = False) -> None:
        nonlocal next_session
        next_session += 1
        events.extend((ProcedureEvent(next_session, None), ProcedureEvent(next_session, outcome)))
        if duplicate:
            events.append(ProcedureEvent(next_session, outcome))

    if scenario in {"ceiling", "both"}:
        for index in range(BOUND_ATTEMPTS):
            add_outcome(SessionOutcome.SUCCEEDED, duplicate=index == 0)
    if scenario in {"floor", "both"}:
        for index in range(BOUND_ATTEMPTS):
            add_outcome(SessionOutcome.FAILED, duplicate=index == 0)

    target = draw(st.integers(min_value=max(1, len(events)), max_value=MAX_EVENTS))
    while len(events) < target:
        remaining = target - len(events)
        if remaining == 1:
            if next_session:
                events.append(
                    ProcedureEvent(
                        draw(st.integers(min_value=1, max_value=next_session)),
                        draw(
                            st.sampled_from(
                                (
                                    SessionOutcome.SUCCEEDED,
                                    SessionOutcome.FAILED,
                                    SessionOutcome.ABANDONED,
                                )
                            )
                        ),
                    )
                )
            else:
                next_session += 1
                events.append(ProcedureEvent(next_session, None))
            continue

        outcome = draw(
            st.sampled_from(
                (
                    SessionOutcome.SUCCEEDED,
                    SessionOutcome.FAILED,
                    SessionOutcome.ABANDONED,
                )
            )
        )
        add_outcome(outcome, duplicate=remaining >= 3 and draw(st.booleans()))

    return tuple(events[:target])


# Feature: molt, Property 36: For any sequence of 1 to 200 retrieval and outcome events
# per Learned_Procedure across the classifications `succeeded`, `failed`, and
# `abandoned`, Procedure_Confidence remains within the closed interval from 0.0 to 1.0
# after every event, moves upward on `succeeded`, moves downward on `failed`, stays equal
# on `abandoned`, and the count of change records equals the count of events that changed
# the value, with every change record's prior and new values matching the transition it
# describes.
@given(events=procedure_event_sequences())
@settings(max_examples=100, deadline=None)
def test_procedure_confidence_bounds_direction_and_history(
    events: tuple[ProcedureEvent, ...],
) -> None:
    """Every event preserves the interval and every recorded transition is exact."""
    chosen = policy()
    confidence = INITIAL
    retrieved: set[int] = set()
    contributed: set[int] = set()
    changes: list[tuple[float, float]] = []

    for event in events:
        prior = confidence
        if event.outcome is None:
            retrieved.add(event.session)
        elif event.session not in contributed:
            assert event.session in retrieved
            contributed.add(event.session)
            delta = chosen.delta_for(event.outcome)
            transition = movement(prior, delta)
            confidence = transition.new_value
            if transition.moved:
                changes.append((prior, confidence))

            if event.outcome is SessionOutcome.SUCCEEDED:
                assert confidence >= prior
            elif event.outcome is SessionOutcome.FAILED:
                assert confidence <= prior
            else:
                assert confidence == prior
        else:
            assert confidence == prior, "one Session contributes at most one outcome"

        assert 0.0 <= confidence <= 1.0

    # Requirement 49.12: the count of change records equals the count of events that
    # changed the value. The expectation is walked from the drawn event stream rather
    # than read back off the recorded changes, so this compares two independent counts:
    # an event that moved the value and recorded nothing, and a record written for an
    # event that moved nothing, each fail here.
    expected_changes = events_that_moved_the_value(events, chosen)
    assert len(changes) == expected_changes, (
        f"{len(changes)} change records were recorded for {expected_changes} events "
        "that changed the value"
    )
    for prior, new in changes:
        assert prior != new
        assert 0.0 <= prior <= 1.0
        assert 0.0 <= new <= 1.0
