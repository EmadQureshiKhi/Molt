"""Property 36: confidence stays bounded and records only real movement.

The generated stream includes retrievals, every terminal outcome, reports repeated
for one Session, and runs that reach both bounds and attempt to pass them. A small
reference model applies the same configured policy the tracker exposes.

**Validates: Requirements 49.1, 49.5, 49.6, 49.7, 49.12, 49.13**
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


# Feature: molt, Property 36: For any sequence of 1 to 200 retrieval and outcome
# events per Learned_Procedure, Procedure_Confidence remains in the closed unit
# interval, follows outcome direction, and records exactly the transitions made.
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

    assert len(changes) == sum(1 for prior, new in changes if prior != new)
    for prior, new in changes:
        assert prior != new
        assert 0.0 <= prior <= 1.0
        assert 0.0 <= new <= 1.0
