"""Unit tests for the serializable transaction wrapper and its backoff schedule.

Everything here runs with no cluster and no wall clock. The cursor is a recording
stub that fails on the attempts a test chooses, and the waiting is the injected
manual clock, so the whole retry schedule is asserted by reading what was sent and
how far the clock was advanced rather than by waiting for anything.

Four claims are checked directly, because each is one the isolation guarantee
rests on: the transaction is framed explicitly and its isolation level is stated
on every attempt; a conflict is rolled back and retried while any other failure
propagates immediately; the delays double from the base to the ceiling and stay
inside the jitter window; and exhaustion raises the named failure, names the
transaction, and emits the exhaustion counter with the component dimension alone.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from typing import Final

import pytest
from tests.conftest import ManualTimeSource

from molt.config.resolve import Configuration
from molt.errors import SerializationExhaustedError
from molt.store.retry import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    COMPONENT,
    DEFAULT_MAX_RETRIES,
    EXHAUSTED_METRIC,
    JITTER_HIGH,
    JITTER_LOW,
    RETRIES_METRIC,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
    SERIALIZATION_FAILURE_STATE,
    RetryPolicy,
    in_serializable,
    is_serialization_failure,
)
from molt.telemetry import Telemetry, configure, reset

# The label a test names its transaction with, so the note and the log record can
# be checked for it.
LABEL: Final[str] = "append_events"

# A jitter source that draws the middle of no window at all, so a schedule can be
# asserted exactly. The real window is exercised by its own test.
NO_JITTER: Final[float] = 1.0


def no_jitter(low: float, high: float) -> float:
    """Return one, ignoring the window, so a delay is its unjittered value."""
    assert low <= high
    return NO_JITTER


class ConflictError(Exception):
    """A failure carrying the state the cluster reports for a lost conflict."""

    def __init__(self) -> None:
        self.sqlstate = SERIALIZATION_FAILURE_STATE
        super().__init__("the transaction was aborted for a conflict")


class LegacyConflictError(Exception):
    """The same failure as reported by a driver naming the state differently."""

    def __init__(self) -> None:
        self.pgcode = SERIALIZATION_FAILURE_STATE
        super().__init__("the transaction was aborted for a conflict")


class RefusalError(Exception):
    """A failure that is no conflict, so retrying it would repeat a certainty."""

    def __init__(self) -> None:
        self.sqlstate = "23505"
        super().__init__("a uniqueness constraint was violated")


class RecordingCursor:
    """A cursor that records every statement it is sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement and return the parameters unchanged."""
        self.sent.append(query)
        return params


class FailingBody:
    """A body that conflicts a chosen number of times and then succeeds."""

    def __init__(self, conflicts: int, *, failure: type[Exception] = ConflictError) -> None:
        self.conflicts = conflicts
        self.failure = failure
        self.calls = 0

    def __call__(self, cursor: RecordingCursor) -> str:
        """Raise while conflicts remain, then answer."""
        self.calls += 1
        cursor.execute("SELECT 1")
        if self.calls <= self.conflicts:
            raise self.failure()
        return f"committed on attempt {self.calls}"


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
    """The process-wide telemetry instance the wrapper emitted through."""
    from molt.telemetry import current

    return current()


# ---------------------------------------------------------------------------
# Transaction framing
# ---------------------------------------------------------------------------


def test_a_successful_body_frames_one_explicit_serializable_transaction() -> None:
    cursor = RecordingCursor()

    result = in_serializable(cursor, lambda sent: sent.execute("SELECT 1"), label=LABEL)

    assert cursor.sent == [BEGIN_STATEMENT, SERIALIZABLE_STATEMENT, "SELECT 1", COMMIT_STATEMENT]
    assert result is None


def test_the_isolation_level_is_stated_on_every_attempt(time_source: ManualTimeSource) -> None:
    cursor = RecordingCursor()
    body = FailingBody(conflicts=2)

    in_serializable(cursor, body, label=LABEL, sleep=time_source.sleep, jitter=no_jitter)

    assert cursor.sent.count(BEGIN_STATEMENT) == 3
    assert cursor.sent.count(SERIALIZABLE_STATEMENT) == 3
    assert cursor.sent.count(COMMIT_STATEMENT) == 1
    assert cursor.sent.count(ROLLBACK_STATEMENT) == 2


def test_a_conflict_is_rolled_back_and_the_body_runs_again(
    time_source: ManualTimeSource,
) -> None:
    cursor = RecordingCursor()
    body = FailingBody(conflicts=1)

    result = in_serializable(cursor, body, label=LABEL, sleep=time_source.sleep, jitter=no_jitter)

    assert result == "committed on attempt 2"
    assert body.calls == 2
    assert time_source.ticks == pytest.approx(BACKOFF_BASE_SECONDS)


def test_a_conflict_named_by_the_other_state_attribute_is_also_retried(
    time_source: ManualTimeSource,
) -> None:
    cursor = RecordingCursor()
    body = FailingBody(conflicts=1, failure=LegacyConflictError)

    result = in_serializable(cursor, body, label=LABEL, sleep=time_source.sleep, jitter=no_jitter)

    assert result == "committed on attempt 2"


def test_a_failure_that_is_no_conflict_propagates_on_the_first_attempt() -> None:
    cursor = RecordingCursor()
    body = FailingBody(conflicts=1, failure=RefusalError)

    with pytest.raises(RefusalError):
        in_serializable(cursor, body, label=LABEL, sleep=_never_sleeps, jitter=no_jitter)

    assert body.calls == 1
    assert cursor.sent.count(ROLLBACK_STATEMENT) == 1
    assert COMMIT_STATEMENT not in cursor.sent


def _never_sleeps(seconds: float) -> None:
    """Fail rather than wait, so a test asserting no retry cannot silently wait."""
    raise AssertionError(f"no delay was expected, but {seconds} second(s) were waited")


def test_a_failure_carrying_no_state_at_all_propagates() -> None:
    cursor = RecordingCursor()

    def body(sent: RecordingCursor) -> None:
        sent.execute("SELECT 1")
        raise RuntimeError("something else went wrong")

    with pytest.raises(RuntimeError, match="something else"):
        in_serializable(cursor, body, label=LABEL, sleep=_never_sleeps, jitter=no_jitter)


def test_the_state_reader_recognises_only_the_conflict_state() -> None:
    assert is_serialization_failure(ConflictError())
    assert is_serialization_failure(LegacyConflictError())
    assert not is_serialization_failure(RefusalError())
    assert not is_serialization_failure(RuntimeError("no state at all"))


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------


def test_exhaustion_raises_the_named_failure_naming_the_transaction(
    time_source: ManualTimeSource,
    telemetry_sink: io.StringIO,
) -> None:
    cursor = RecordingCursor()
    policy = RetryPolicy(max_retries=2)
    body = FailingBody(conflicts=99)

    with pytest.raises(SerializationExhaustedError) as raised:
        in_serializable(
            cursor,
            body,
            policy=policy,
            label=LABEL,
            sleep=time_source.sleep,
            jitter=no_jitter,
        )

    assert raised.value.attempts == policy.attempts
    assert body.calls == policy.attempts == 3
    assert any(LABEL in note for note in raised.value.__notes__)
    assert isinstance(raised.value.__cause__, ConflictError)
    assert cursor.sent.count(COMMIT_STATEMENT) == 0
    assert LABEL in telemetry_sink.getvalue()


def test_exhaustion_emits_the_counter_with_the_component_dimension_alone(
    time_source: ManualTimeSource,
    telemetry_sink: io.StringIO,
) -> None:
    assert telemetry_sink is not None
    cursor = RecordingCursor()

    with pytest.raises(SerializationExhaustedError):
        in_serializable(
            cursor,
            FailingBody(conflicts=99),
            policy=RetryPolicy(max_retries=1),
            label=LABEL,
            sleep=time_source.sleep,
            jitter=no_jitter,
        )

    # Two counters, both dimensioned by the component alone: one retry was taken
    # before the single permitted retry was spent, and the transaction was then
    # abandoned. The transaction label appears in the record rather than as a
    # dimension, so neither counter grows as labels are added.
    counters = instance().counters()
    assert counters == {
        (RETRIES_METRIC, (("component", COMPONENT),)): 1.0,
        (EXHAUSTED_METRIC, (("component", COMPONENT),)): 1.0,
    }


def test_a_policy_permitting_no_retry_makes_exactly_one_attempt(
    telemetry_sink: io.StringIO,
) -> None:
    assert telemetry_sink is not None
    body = FailingBody(conflicts=99)

    with pytest.raises(SerializationExhaustedError):
        in_serializable(
            RecordingCursor(),
            body,
            policy=RetryPolicy(max_retries=0),
            label=LABEL,
            sleep=_never_sleeps,
            jitter=no_jitter,
        )

    assert body.calls == 1


# ---------------------------------------------------------------------------
# The backoff schedule
# ---------------------------------------------------------------------------


def test_the_schedule_doubles_from_the_base_and_stops_at_the_ceiling() -> None:
    policy = RetryPolicy(max_retries=8)

    delays = [policy.delay(retry, jitter=no_jitter) for retry in range(8)]

    assert delays[:6] == pytest.approx([0.05, 0.10, 0.20, 0.40, 0.80, 1.60])
    assert delays[6] == pytest.approx(BACKOFF_CAP_SECONDS)
    assert delays[7] == pytest.approx(BACKOFF_CAP_SECONDS)


def test_every_delay_lies_inside_the_jitter_window() -> None:
    policy = RetryPolicy(max_retries=6)

    for retry in range(policy.max_retries):
        unjittered = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * float(2**retry))
        for _ in range(20):
            delay = policy.delay(retry)
            assert unjittered * JITTER_LOW <= delay <= unjittered * JITTER_HIGH


def test_the_total_wait_is_the_sum_of_the_schedule(time_source: ManualTimeSource) -> None:
    policy = RetryPolicy(max_retries=3)
    body = FailingBody(conflicts=3)

    in_serializable(
        RecordingCursor(),
        body,
        policy=policy,
        label=LABEL,
        sleep=time_source.sleep,
        jitter=no_jitter,
    )

    assert time_source.ticks == pytest.approx(0.05 + 0.10 + 0.20)


def test_a_negative_retry_index_is_refused() -> None:
    with pytest.raises(ValueError, match="retry index"):
        RetryPolicy().delay(-1)


def test_a_negative_retry_count_is_refused() -> None:
    with pytest.raises(ValueError, match="retry count"):
        RetryPolicy(max_retries=-1)


def test_a_negative_delay_is_refused() -> None:
    with pytest.raises(ValueError, match="backoff delay"):
        RetryPolicy(base_seconds=-0.1)


def test_a_ceiling_below_the_first_delay_is_refused() -> None:
    with pytest.raises(ValueError, match="backoff ceiling"):
        RetryPolicy(base_seconds=0.5, cap_seconds=0.01)


def test_an_unordered_jitter_window_is_refused() -> None:
    with pytest.raises(ValueError, match="jitter window"):
        RetryPolicy(jitter_low=0.9, jitter_high=0.1)


# ---------------------------------------------------------------------------
# The configured retry count
# ---------------------------------------------------------------------------


def test_the_retry_count_defaults_to_the_surface_default() -> None:
    policy = RetryPolicy.from_configuration(Configuration(environ={}, file_values={}))

    assert policy.max_retries == DEFAULT_MAX_RETRIES == 5
    assert policy.attempts == 6


def test_the_retry_count_comes_from_the_configuration_surface() -> None:
    resolved = Configuration(environ={"MOLT_DB_MAX_RETRIES": "2"}, file_values={})

    policy = RetryPolicy.from_configuration(resolved)

    assert policy.max_retries == 2
    assert policy.attempts == 3
