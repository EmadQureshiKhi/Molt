"""Unit tests for the confidence delta arithmetic and the clamp that bounds it.

Nothing here opens a socket. The two deltas are read from a configuration surface
this module builds empty, so the values asserted are the surface's own rather than
constants this module carries, and the arithmetic is a pure function of a prior
value and a delta, so the whole closed unit interval can be driven without a
cluster.

Four claims are checked.

**The increment and the decrement are the configured magnitudes, and the decrement
is the larger of the two.** A succeeded outcome asks for the increment upward and a
failed outcome asks for the decrement downward, so the sign belongs to the
classification while the magnitude belongs to the configuration. The asymmetry is
asserted rather than assumed: standing is lost faster than it is earned, which is
what puts a procedure that has started to mislead under the recall floor after a
couple of failures rather than after a dozen.

**Both bounds absorb whatever a delta asks for past them.** The ceiling holds
against an increment and against an absurd delta, and the floor holds the same way,
so a caller cannot produce a standing the artifact could not carry.

**An adjustment a bound absorbed reports no movement, which is what writes no
change record.** The distinction the change table rests on is stated here as a
property of the arithmetic: an absorbed adjustment was attempted, moved nothing, and
names no direction, so there is nothing for a change record to describe.

**An abandoned outcome attempts no adjustment at all.** None is a different answer
from a delta of nought: nothing is read and nothing is written, which is why an
abandoned outcome leaves an outcome row with no change record beside it and no
statement having touched the standing.

**Validates: Requirements 49.5, 49.6, 49.7, 49.12, 36.1**
"""

from __future__ import annotations

from typing import Final

import pytest

from molt.confidence import (
    DIRECTION_DOWN,
    DIRECTION_UP,
    ConfidencePolicy,
    adjusted,
    clamped,
    direction_of,
    movement,
)
from molt.config.resolve import Configuration
from molt.models.artifact import CONFIDENCE_CEILING, CONFIDENCE_FLOOR
from molt.models.session import SessionOutcome

# The two magnitudes the requirement names, asserted against the configuration
# surface rather than substituted for it.
CONFIGURED_INCREMENT: Final[float] = 0.05
CONFIGURED_DECREMENT: Final[float] = 0.10

# A standing sitting in the interior of the interval, so an ordinary movement is
# nowhere near a bound and the clamp plays no part in it.
INTERIOR_STANDING: Final[float] = 0.5

# A delta far outside anything a classification asks for, so the clamp is shown to
# hold against a value no policy would produce.
ABSURD_DELTA: Final[float] = 12.5


def surface_policy() -> ConfidencePolicy:
    """The policy an empty configuration surface produces, which is the shipped one."""
    return ConfidencePolicy.from_configuration(Configuration(environ={}, file_values={}))


# ---------------------------------------------------------------------------
# The two configured magnitudes
# ---------------------------------------------------------------------------


def test_the_configured_increment_and_decrement_are_the_two_named_magnitudes() -> None:
    """The surface carries both magnitudes, and both are magnitudes rather than signs."""
    policy = surface_policy()

    assert policy.success_delta == CONFIGURED_INCREMENT
    assert policy.failure_delta == CONFIGURED_DECREMENT
    assert policy.failure_delta > policy.success_delta, "standing is lost faster than it is earned"


def test_the_classification_supplies_the_sign_the_configuration_does_not() -> None:
    """A success asks upward by the increment and a failure downward by the decrement."""
    policy = surface_policy()

    assert policy.delta_for(SessionOutcome.SUCCEEDED) == CONFIGURED_INCREMENT
    assert policy.delta_for(SessionOutcome.FAILED) == -CONFIGURED_DECREMENT


def test_an_ordinary_movement_applies_the_magnitude_and_names_its_direction() -> None:
    """Away from both bounds the arithmetic is the delta and nothing else."""
    policy = surface_policy()

    upward = movement(INTERIOR_STANDING, policy.delta_for(SessionOutcome.SUCCEEDED))
    downward = movement(INTERIOR_STANDING, policy.delta_for(SessionOutcome.FAILED))

    assert upward.new_value == pytest.approx(INTERIOR_STANDING + CONFIGURED_INCREMENT)
    assert upward.applied_delta == pytest.approx(CONFIGURED_INCREMENT)
    assert upward.direction == DIRECTION_UP
    assert downward.new_value == pytest.approx(INTERIOR_STANDING - CONFIGURED_DECREMENT)
    assert downward.applied_delta == pytest.approx(-CONFIGURED_DECREMENT)
    assert downward.direction == DIRECTION_DOWN
    assert upward.moved and downward.moved


# ---------------------------------------------------------------------------
# The clamp at both bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prior", "delta", "expected"),
    [
        (CONFIDENCE_CEILING, CONFIGURED_INCREMENT, CONFIDENCE_CEILING),
        (CONFIDENCE_CEILING, ABSURD_DELTA, CONFIDENCE_CEILING),
        (CONFIDENCE_FLOOR, -CONFIGURED_DECREMENT, CONFIDENCE_FLOOR),
        (CONFIDENCE_FLOOR, -ABSURD_DELTA, CONFIDENCE_FLOOR),
        (INTERIOR_STANDING, ABSURD_DELTA, CONFIDENCE_CEILING),
        (INTERIOR_STANDING, -ABSURD_DELTA, CONFIDENCE_FLOOR),
    ],
    ids=[
        "ceiling-increment",
        "ceiling-absurd",
        "floor-decrement",
        "floor-absurd",
        "interior-past-ceiling",
        "interior-past-floor",
    ],
)
def test_the_closed_unit_interval_holds_whatever_delta_is_asked_for(
    prior: float,
    delta: float,
    expected: float,
) -> None:
    """Both ends are admitted and neither is passed, however large the delta."""
    assert adjusted(prior, delta) == expected


def test_clamping_admits_both_ends_and_refuses_everything_outside() -> None:
    """The clamp is stated over values rather than over transitions, and holds the ends."""
    assert clamped(CONFIDENCE_FLOOR) == CONFIDENCE_FLOOR
    assert clamped(CONFIDENCE_CEILING) == CONFIDENCE_CEILING
    assert clamped(INTERIOR_STANDING) == INTERIOR_STANDING
    assert clamped(-ABSURD_DELTA) == CONFIDENCE_FLOOR
    assert clamped(ABSURD_DELTA) == CONFIDENCE_CEILING


def test_a_prior_standing_outside_the_interval_is_refused_rather_than_clamped() -> None:
    """A movement from a standing the artifact could not hold is a caller fault."""
    with pytest.raises(ValueError, match="procedure confidence"):
        movement(CONFIDENCE_CEILING + CONFIGURED_INCREMENT, CONFIGURED_INCREMENT)


# ---------------------------------------------------------------------------
# What writes no change record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bound", "outcome"),
    [
        (CONFIDENCE_CEILING, SessionOutcome.SUCCEEDED),
        (CONFIDENCE_FLOOR, SessionOutcome.FAILED),
    ],
    ids=["ceiling", "floor"],
)
def test_an_adjustment_a_bound_absorbed_claims_no_movement(
    bound: float,
    outcome: SessionOutcome,
) -> None:
    """An absorbed adjustment was attempted, moved nothing, and describes no transition."""
    policy = surface_policy()

    absorbed = movement(bound, policy.delta_for(outcome))

    assert absorbed.new_value == bound
    assert absorbed.applied_delta == 0.0
    assert not absorbed.moved, "a change record describes a movement that happened"
    assert absorbed.absorbed, "and this one was applied and taken by the bound"
    assert absorbed.direction is None
    assert direction_of(absorbed.prior, absorbed.new_value) is None


def test_an_abandoned_outcome_attempts_no_adjustment_at_all() -> None:
    """No delta is a different answer from a delta of nought, and reads as no attempt."""
    policy = surface_policy()

    assert policy.delta_for(SessionOutcome.ABANDONED) is None

    untouched = movement(INTERIOR_STANDING, policy.delta_for(SessionOutcome.ABANDONED))

    assert untouched.attempted_delta is None
    assert untouched.new_value == INTERIOR_STANDING
    assert not untouched.moved
    assert not untouched.absorbed, "nothing was attempted, so no bound absorbed anything"
    assert untouched.direction is None
