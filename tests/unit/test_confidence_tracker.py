"""Unit tests for the Confidence_Tracker's policy, its arithmetic, and its reporting.

Nothing here opens a socket. The configured numbers come from a configuration this
module builds, and the statements are answered by a scripted cursor, so every claim
below is asserted over what the tracker decided and what it emitted rather than
over a cluster's behaviour. What the cluster has to make true -- that the clamp it
performs agrees with the arithmetic here, and that a movement and its record commit
together -- is asserted against a live instance instead.

Six properties are checked.

Every number comes from the configuration surface. Two configurations naming
different values produce different policies, and the surface's own defaults are the
values the requirement names, so no number the tracker applies is a constant of this
codebase.

A retrieval moves nothing and an outcome moves something. Recording a retrieval
touches no standing at all; only a succeeded or a failed outcome asks for an
adjustment, and an abandoned one asks for none rather than for zero.

Standing is lost faster than it is earned. The decrement exceeds the increment on
the configured defaults, which is the asymmetry the design argues for rather than an
accident of two numbers.

The arithmetic holds the closed unit interval and names its own direction. A delta
that would carry a standing past a bound is absorbed by the bound, an absorbed
adjustment reports no direction and no movement, and a movement reports the
direction it went.

One committed movement produces one measurement. The measurement is emitted after
the transaction rather than inside it, so a transaction the retry wrapper had to run
twice is counted once, and the dimension is the direction and nothing else.

A non-movement is reported without being counted. An abandoned outcome, an
adjustment a bound absorbed, and a report already on the record each produce a
record naming which of the three it was and no measurement at all, because the
counter counts movements.

**Validates: Requirements 49.2, 49.3, 49.5, 49.6, 49.7, 49.9, 49.12, 49.15**
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.confidence import (
    CONFIDENCE_CHANGES_METRIC,
    DIRECTION_DIMENSION,
    DIRECTION_DOWN,
    DIRECTION_UP,
    FAILURE_DELTA_KEY,
    INITIAL_KEY,
    RECALL_FLOOR_KEY,
    SUCCESS_DELTA_KEY,
    ConfidencePolicy,
    adjusted,
    clamped,
    direction_of,
    history,
    initial_standing,
    movement,
    record_outcome,
    record_retrieval,
    summary,
)
from molt.config.resolve import Configuration
from molt.models.artifact import CONFIDENCE_CEILING, CONFIDENCE_FLOOR, DerivedArtifactKind
from molt.models.session import SessionOutcome
from molt.store import Connection, MemoryStore
from molt.store.confidence import (
    ADJUST_STANDING_STATEMENT,
    INSERT_CHANGE_STATEMENT,
    INSERT_RETRIEVAL_STATEMENT,
    SELECT_CHANGES_QUERY,
    SELECT_OUTCOME_COUNTS_QUERY,
)
from molt.store.retry import SERIALIZATION_FAILURE_STATE
from molt.telemetry import Telemetry, configure, current, reset

# The values the requirement names as the surface defaults. Asserted against the
# surface rather than used as this module's own numbers.
DEFAULT_INITIAL: Final[float] = 0.5
DEFAULT_SUCCESS_DELTA: Final[float] = 0.05
DEFAULT_FAILURE_DELTA: Final[float] = 0.10
DEFAULT_RECALL_FLOOR: Final[float] = 0.15

# A configured policy naming none of the defaults, so an assertion about a policy
# cannot be satisfied by a value the tracker held instead of reading the surface.
OTHER_INITIAL: Final[float] = 0.4
OTHER_SUCCESS_DELTA: Final[float] = 0.02
OTHER_FAILURE_DELTA: Final[float] = 0.25
OTHER_FLOOR: Final[float] = 0.3

# Fragments the script matches a statement by.
RETRIEVAL_FRAGMENT: Final[str] = "INSERT INTO procedure_retrieval"
STANDING_FRAGMENT: Final[str] = "SELECT procedure_confidence FROM"
OUTCOME_FRAGMENT: Final[str] = "INSERT INTO procedure_outcome"
CONTEXT_FRAGMENT: Final[str] = "SELECT (SELECT count(*) FROM derived_artifact"
ADJUST_FRAGMENT: Final[str] = "UPDATE derived_artifact SET"
CHANGE_FRAGMENT: Final[str] = "INSERT INTO procedure_confidence_change"
HISTORY_FRAGMENT: Final[str] = "ORDER BY changed_at ASC"
RETRIEVAL_COUNT_FRAGMENT: Final[str] = "SELECT count(*) FROM procedure_retrieval"
OUTCOME_COUNT_FRAGMENT: Final[str] = "GROUP BY outcome"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# The identifiers every driven call names.
PROCEDURE_ID: Final[UUID] = uuid4()
SESSION_ID: Final[UUID] = uuid4()
RETRIEVAL_ID: Final[UUID] = uuid4()
OUTCOME_ID: Final[UUID] = uuid4()
CHANGE_ID: Final[UUID] = uuid4()

# The two billable combinations a movement may occupy, and no third.
UPWARD_COMBINATION: Final[tuple[str, tuple[tuple[str, str], ...]]] = (
    CONFIDENCE_CHANGES_METRIC,
    ((DIRECTION_DIMENSION, DIRECTION_UP),),
)
DOWNWARD_COMBINATION: Final[tuple[str, tuple[tuple[str, str], ...]]] = (
    CONFIDENCE_CHANGES_METRIC,
    ((DIRECTION_DIMENSION, DIRECTION_DOWN),),
)

# The standings the driven examples move between, and a standing near the ceiling
# so an increment overshoots it.
PRIOR_STANDING: Final[float] = 0.5
NEAR_CEILING: Final[float] = 0.98
RETRIEVAL_COUNT: Final[int] = 3
SUCCEEDED_COUNT: Final[int] = 2


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


class DriverFailureError(Exception):
    """A driver failure carrying the state a driver reports the fault under."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("the statement was refused")
        self.sqlstate = sqlstate


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()
    error: Exception | None = None


@dataclass(slots=True)
class Script:
    """The answers a connection hands out, consumed in the order they match."""

    answers: list[Answer] = field(default_factory=list)
    sent: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    armed: tuple[tuple[object, ...], ...] = ()

    @property
    def statements(self) -> list[str]:
        """Every statement the script was sent, in order."""
        return [query for query, _ in self.sent]

    def count_of(self, statement: str) -> int:
        """How many times one statement was sent."""
        return self.statements.count(statement)

    def take(self, query: str) -> Answer | None:
        """The next answer matching a statement, removed from the script."""
        for index, answer in enumerate(self.answers):
            if answer.fragment in query:
                return self.answers.pop(index)
        return None


class ScriptedCursor:
    """A cursor answering from a script and recording what it was sent."""

    def __init__(self, script: Script) -> None:
        self._script = script

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then raise or arm rows as the script says."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        if answer is None:
            self._script.armed = ()
            return None
        if answer.error is not None:
            raise answer.error
        self._script.armed = answer.rows
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or None when the statement armed none."""
        rows = self._script.armed
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._script.armed)

    def close(self) -> None:
        """Release this cursor."""


class ScriptedConnection:
    """A connection handing out scripted cursors over one shared script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Open a recording cursor over this connection's script."""
        return ScriptedCursor(self.script)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def policy_of(
    *,
    initial: float = OTHER_INITIAL,
    success: float = OTHER_SUCCESS_DELTA,
    failure: float = OTHER_FAILURE_DELTA,
    floor: float = OTHER_FLOOR,
) -> ConfidencePolicy:
    """A policy read from a configuration naming four values and no real environment."""
    return ConfidencePolicy.from_configuration(
        Configuration(
            environ={
                INITIAL_KEY: str(initial),
                SUCCESS_DELTA_KEY: str(success),
                FAILURE_DELTA_KEY: str(failure),
                RECALL_FLOOR_KEY: str(floor),
            },
            file_values={},
        )
    )


def outcome_row(outcome: SessionOutcome) -> tuple[object, ...]:
    """One outcome row of the width the insert returns."""
    return (OUTCOME_ID, PROCEDURE_ID, SESSION_ID, outcome.value, MOMENT)


def outcome_answers(
    outcome: SessionOutcome,
    *,
    prior: float,
    applied: float,
    conflict: bool = False,
) -> list[Answer]:
    """The answers one recorded outcome with an adjustment consumes.

    When a conflict is asked for, the change record's first attempt is refused with
    the serialization state, so the wrapper runs the whole body again and the same
    answers are consumed a second time.
    """
    attempts = 2 if conflict else 1
    answers: list[Answer] = []
    for attempt in range(attempts):
        failing = conflict and attempt == 0
        answers.extend(
            [
                Answer(STANDING_FRAGMENT, ((prior,),)),
                Answer(OUTCOME_FRAGMENT, (outcome_row(outcome),)),
                Answer(ADJUST_FRAGMENT, ((applied,),)),
                Answer(
                    CHANGE_FRAGMENT,
                    rows=() if failing else ((CHANGE_ID, MOMENT),),
                    error=DriverFailureError(SERIALIZATION_FAILURE_STATE) if failing else None,
                ),
            ]
        )
    return answers


@pytest.fixture
def telemetry_sink() -> Iterator[io.StringIO]:
    """Install a process-wide telemetry instance writing to a sink for one test."""
    sink = io.StringIO()
    configure(Configuration(environ={"MOLT_LOG_LEVEL": "debug"}, file_values={}), stream=sink)
    try:
        yield sink
    finally:
        reset()


def instance() -> Telemetry:
    """The process-wide telemetry instance the tracker emitted through."""
    return current()


def records_of(sink: io.StringIO, message_fragment: str) -> tuple[dict[str, object], ...]:
    """Every record the sink holds whose message carries a fragment."""
    found: list[dict[str, object]] = []
    for line in sink.getvalue().splitlines():
        record: dict[str, object] = json.loads(line)
        message = record.get("message")
        if isinstance(message, str) and message_fragment in message:
            found.append(record)
    return tuple(found)


# ---------------------------------------------------------------------------
# The configured policy
# ---------------------------------------------------------------------------


def test_the_surface_defaults_are_the_values_the_requirement_names() -> None:
    """The four numbers live on the configuration surface, at the stated defaults."""
    default = ConfidencePolicy.from_configuration(Configuration(environ={}, file_values={}))

    assert default.initial == DEFAULT_INITIAL
    assert default.success_delta == DEFAULT_SUCCESS_DELTA
    assert default.failure_delta == DEFAULT_FAILURE_DELTA
    assert default.recall_floor == DEFAULT_RECALL_FLOOR


def test_standing_is_lost_faster_than_it_is_earned() -> None:
    """A procedure that misleads an agent costs more than one that helps."""
    default = ConfidencePolicy.from_configuration(Configuration(environ={}, file_values={}))

    assert default.failure_delta > default.success_delta


def test_a_configured_policy_reads_every_number_from_the_surface() -> None:
    """Four keys, four values, none of them a constant of this codebase."""
    configured = policy_of()

    assert configured.initial == OTHER_INITIAL
    assert configured.success_delta == OTHER_SUCCESS_DELTA
    assert configured.failure_delta == OTHER_FAILURE_DELTA
    assert configured.recall_floor == OTHER_FLOOR


def test_a_policy_naming_a_value_outside_the_interval_is_refused() -> None:
    """The closed unit interval bounds the configuration as well as the stored value."""
    with pytest.raises(ValueError, match="closed interval"):
        ConfidencePolicy(initial=1.5, success_delta=0.05, failure_delta=0.1, recall_floor=0.15)


def test_the_recall_floor_comparison_is_stated_once() -> None:
    """Every reader of the floor asks the same question of it."""
    configured = policy_of(floor=OTHER_FLOOR)

    assert configured.below_floor(OTHER_FLOOR - 0.01)
    assert not configured.below_floor(OTHER_FLOOR), "the floor itself is not below the floor"


# ---------------------------------------------------------------------------
# Which classification moves a standing
# ---------------------------------------------------------------------------


def test_a_succeeded_outcome_asks_to_raise_and_a_failed_one_to_lower() -> None:
    """The sign belongs to the classification and the magnitude to the configuration."""
    configured = policy_of()

    assert configured.delta_for(SessionOutcome.SUCCEEDED) == OTHER_SUCCESS_DELTA
    assert configured.delta_for(SessionOutcome.FAILED) == -OTHER_FAILURE_DELTA


def test_an_abandoned_outcome_asks_for_no_adjustment_rather_than_a_zero_one() -> None:
    """A Session the engineer walked away from says nothing about the procedure."""
    assert policy_of().delta_for(SessionOutcome.ABANDONED) is None


def test_a_session_in_flight_has_no_classification_to_apply() -> None:
    """A Session that has not ended has reached no outcome, so there is nothing to record."""
    with pytest.raises(ValueError, match="terminal session classification"):
        policy_of().delta_for(SessionOutcome.IN_PROGRESS)


def test_only_a_learned_procedure_carries_an_initial_standing() -> None:
    """The kind equivalence the schema states is computable rather than remembered."""
    configured = policy_of(initial=OTHER_INITIAL)

    assert (
        initial_standing(DerivedArtifactKind.LEARNED_PROCEDURE, policy=configured) == OTHER_INITIAL
    )
    for kind in (DerivedArtifactKind.SUMMARY, DerivedArtifactKind.BEHAVIORAL_BASELINE):
        with pytest.raises(ValueError, match="only a learned procedure"):
            initial_standing(kind, policy=configured)


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_the_interval_holds_both_ends_and_absorbs_what_would_pass_them() -> None:
    """A clamp at a bound is what keeps an absurd delta from leaving the interval."""
    assert clamped(CONFIDENCE_FLOOR) == CONFIDENCE_FLOOR
    assert clamped(CONFIDENCE_CEILING) == CONFIDENCE_CEILING
    assert adjusted(CONFIDENCE_CEILING, 5.0) == CONFIDENCE_CEILING
    assert adjusted(CONFIDENCE_FLOOR, -5.0) == CONFIDENCE_FLOOR


def test_an_absorbed_adjustment_moved_nothing_and_reports_no_direction() -> None:
    """A standing already at a bound is where a change record must not be written."""
    absorbed = movement(CONFIDENCE_CEILING, OTHER_SUCCESS_DELTA)

    assert absorbed.new_value == CONFIDENCE_CEILING
    assert absorbed.absorbed
    assert not absorbed.moved
    assert absorbed.direction is None
    assert absorbed.applied_delta == 0.0


def test_a_movement_names_the_direction_it_went() -> None:
    """The dimension the measurement carries is read off the two endpoints."""
    raised = movement(PRIOR_STANDING, OTHER_SUCCESS_DELTA)
    lowered = movement(PRIOR_STANDING, -OTHER_FAILURE_DELTA)

    assert raised.direction == DIRECTION_UP
    assert lowered.direction == DIRECTION_DOWN
    assert direction_of(PRIOR_STANDING, PRIOR_STANDING) is None


def test_a_deliberate_non_adjustment_produces_a_movement_that_went_nowhere() -> None:
    """One shape is returned whether an adjustment was attempted or not."""
    unattempted = movement(PRIOR_STANDING, None)

    assert unattempted.new_value == PRIOR_STANDING
    assert not unattempted.moved
    assert not unattempted.absorbed, "nothing was attempted, so no bound absorbed anything"


# ---------------------------------------------------------------------------
# The public surface, and what it emits
# ---------------------------------------------------------------------------


def test_recording_a_retrieval_writes_one_row_and_moves_no_standing(
    telemetry_sink: io.StringIO,
) -> None:
    """Being shown to an agent is not evidence of being right."""
    script = Script(
        answers=[Answer(RETRIEVAL_FRAGMENT, ((RETRIEVAL_ID, PROCEDURE_ID, SESSION_ID, MOMENT),))]
    )

    record_retrieval(build_store(script), PROCEDURE_ID, SESSION_ID)

    assert script.count_of(INSERT_RETRIEVAL_STATEMENT) == 1
    assert script.count_of(ADJUST_STANDING_STATEMENT) == 0
    assert instance().combinations() == ()
    assert records_of(telemetry_sink, "moving no standing")


def test_one_committed_movement_produces_one_measurement_carrying_its_direction(
    telemetry_sink: io.StringIO,
) -> None:
    """The dimension is the direction, and the count is one per commit."""
    script = Script(
        answers=outcome_answers(
            SessionOutcome.SUCCEEDED,
            prior=PRIOR_STANDING,
            applied=PRIOR_STANDING + OTHER_SUCCESS_DELTA,
        )
    )

    change = record_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.SUCCEEDED,
        policy=policy_of(),
    )

    assert change is not None
    assert change.new_value == pytest.approx(PRIOR_STANDING + OTHER_SUCCESS_DELTA)
    assert instance().combinations() == (UPWARD_COMBINATION,)
    assert instance().counters()[UPWARD_COMBINATION] == 1.0
    assert len(records_of(telemetry_sink, "recorded the change")) == 1


def test_a_failed_outcome_is_counted_in_the_other_direction(
    telemetry_sink: io.StringIO,
) -> None:
    """Two dimension values and no more, so the billable pair cannot grow."""
    script = Script(
        answers=outcome_answers(
            SessionOutcome.FAILED,
            prior=PRIOR_STANDING,
            applied=PRIOR_STANDING - OTHER_FAILURE_DELTA,
        )
    )

    change = record_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.FAILED,
        policy=policy_of(),
    )

    assert change is not None
    assert instance().combinations() == (DOWNWARD_COMBINATION,)
    assert len(records_of(telemetry_sink, "recorded the change")) == 1


def test_a_retried_transaction_is_counted_once(telemetry_sink: io.StringIO) -> None:
    """The emission follows the commit, so work that was rolled back is not counted."""
    script = Script(
        answers=outcome_answers(
            SessionOutcome.SUCCEEDED,
            prior=PRIOR_STANDING,
            applied=PRIOR_STANDING + OTHER_SUCCESS_DELTA,
            conflict=True,
        )
    )

    change = record_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.SUCCEEDED,
        policy=policy_of(),
    )

    assert change is not None
    assert script.count_of(INSERT_CHANGE_STATEMENT) == 2, "the body really ran twice"
    assert instance().counters()[UPWARD_COMBINATION] == 1.0
    assert len(records_of(telemetry_sink, "recorded the change")) == 1


def test_an_abandoned_outcome_is_recorded_and_counted_nowhere(
    telemetry_sink: io.StringIO,
) -> None:
    """The outcome is on the record and the standing was never touched."""
    script = Script(
        answers=[Answer(OUTCOME_FRAGMENT, (outcome_row(SessionOutcome.ABANDONED),))],
    )

    change = record_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.ABANDONED,
        policy=policy_of(),
    )

    assert change is None
    assert script.count_of(ADJUST_STANDING_STATEMENT) == 0
    assert instance().combinations() == ()
    assert records_of(telemetry_sink, "adjusts no standing by design")


def test_an_adjustment_a_bound_absorbed_is_reported_and_not_counted(
    telemetry_sink: io.StringIO,
) -> None:
    """An outcome with no change record beside it is what an absorbed adjustment looks like."""
    script = Script(
        answers=[
            Answer(STANDING_FRAGMENT, ((NEAR_CEILING,),)),
            Answer(OUTCOME_FRAGMENT, (outcome_row(SessionOutcome.SUCCEEDED),)),
            Answer(ADJUST_FRAGMENT, ((NEAR_CEILING,),)),
        ]
    )

    change = record_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.SUCCEEDED,
        policy=policy_of(),
    )

    assert change is None
    assert script.count_of(ADJUST_STANDING_STATEMENT) == 1, "the adjustment was applied"
    assert script.count_of(INSERT_CHANGE_STATEMENT) == 0, "and it claimed no movement"
    assert instance().combinations() == ()
    assert records_of(telemetry_sink, "already at a bound")


def test_a_report_already_on_the_record_is_reported_as_such(
    telemetry_sink: io.StringIO,
) -> None:
    """A repeated delivery leaves the standing where the first one left it."""
    script = Script(
        answers=[
            Answer(STANDING_FRAGMENT, ((PRIOR_STANDING,),)),
            Answer(CONTEXT_FRAGMENT, ((1, SessionOutcome.SUCCEEDED.value, 1, 1),)),
        ]
    )

    change = record_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.SUCCEEDED,
        policy=policy_of(),
    )

    assert change is None
    assert script.count_of(ADJUST_STANDING_STATEMENT) == 0
    assert instance().combinations() == ()
    assert records_of(telemetry_sink, "already recorded")


def test_the_history_and_the_summary_reach_the_queries_that_answer_them() -> None:
    """The tracker's two reads are the store's two queries, with the defaults applied."""
    history_script = Script(
        answers=[
            Answer(
                HISTORY_FRAGMENT,
                ((CHANGE_ID, PROCEDURE_ID, PRIOR_STANDING, NEAR_CEILING, OUTCOME_ID, MOMENT),),
            )
        ]
    )
    summary_script = Script(
        answers=[
            Answer(STANDING_FRAGMENT, ((PRIOR_STANDING,),)),
            Answer(RETRIEVAL_COUNT_FRAGMENT, ((RETRIEVAL_COUNT,),)),
            Answer(OUTCOME_COUNT_FRAGMENT, ((SessionOutcome.SUCCEEDED.value, SUCCEEDED_COUNT),)),
        ]
    )

    records = history(build_store(history_script), PROCEDURE_ID)
    standing = summary(build_store(summary_script), PROCEDURE_ID)

    assert history_script.count_of(SELECT_CHANGES_QUERY) == 1
    assert len(records) == 1
    assert records[0].id == CHANGE_ID
    assert summary_script.count_of(SELECT_OUTCOME_COUNTS_QUERY) == 1
    assert standing.confidence == PRIOR_STANDING
    assert standing.retrievals == RETRIEVAL_COUNT
    assert standing.count_of(SessionOutcome.SUCCEEDED) == SUCCEEDED_COUNT
