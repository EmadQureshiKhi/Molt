"""The serializable transaction wrapper: one explicit transaction, bounded retries.

Every write the store performs runs through `in_serializable`, and the shape of
that wrapper is what the isolation guarantee rests on.

**The transaction is explicit, and its isolation level is stated rather than
assumed.** Each attempt sends `BEGIN`, then sets the isolation level to
SERIALIZABLE, then runs the caller's body, then commits. Nothing infers the level
from a connection default, so a connection reused for a read cannot leave a write
running at a weaker level, and the level a write ran at is visible in the
statements that ran rather than in configuration.

**A conflict is retried, and only a conflict.** The cluster answers a
serialization conflict with the serialization-failure state, and that state alone
is what the wrapper retries. Any other failure propagates on the first attempt,
because retrying a constraint violation or a syntax fault would turn one clear
failure into several slow ones. A conflict is exactly the case where the same
work run again is expected to succeed, since the conflict says another
transaction touched the read set rather than that the work was wrong.

**Backoff is exponential and jittered, and the jitter is the point.** Two
transactions that conflict and then retry after the same fixed delay conflict
again. The delay doubles from a base of 50 milliseconds to a ceiling of 2
seconds, and each delay is multiplied by a random factor between one half and one
and one half, so contending writers spread out instead of marching in step.

**Exhaustion is a named failure with a metric, not a silent give-up.** After the
configured number of retries the wrapper raises the exhaustion error carrying the
attempt count, attaches a note naming the transaction so an operator learns which
write kept losing, and emits the exhaustion counter. The counter carries the
component dimension alone: a per-transaction or per-Client dimension would
multiply into the billable metric bound the telemetry surface exists to hold.

**Nothing here waits on a real clock unless the caller lets it.** The sleeper and
the jitter source are injected with defaults, so the backoff schedule is driven
directly by a test instead of waited out, and a hundred examples cost no seconds.

The driver is reached through one narrow structural protocol declared here rather
than by importing it, so this module imports and type-checks with no driver
installed and a test drives it with a recording cursor.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, TypeAlias, TypeVar

from molt.config.resolve import Configuration
from molt.errors import SerializationExhaustedError
from molt.telemetry import Severity, log, metric

__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "BEGIN_STATEMENT",
    "COMMIT_STATEMENT",
    "COMPONENT",
    "DEFAULT_JITTER",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_POLICY",
    "DEFAULT_SLEEP",
    "DEFAULT_TRANSACTION_LABEL",
    "EXHAUSTED_METRIC",
    "JITTER_HIGH",
    "JITTER_LOW",
    "RETRIES_METRIC",
    "ROLLBACK_STATEMENT",
    "SERIALIZABLE_STATEMENT",
    "SERIALIZATION_FAILURE_STATE",
    "Cursor",
    "Jitter",
    "RetryPolicy",
    "Sleeper",
    "in_serializable",
    "is_serialization_failure",
]

# The state the cluster reports when it aborts one of two conflicting
# transactions. It is the only state this module retries.
SERIALIZATION_FAILURE_STATE: Final[str] = "40001"

# The attribute names a driver may carry the state under. Both are read because
# the driver is reached structurally rather than imported, so its exception
# classes cannot be named here.
_STATE_ATTRIBUTES: Final[tuple[str, ...]] = ("sqlstate", "pgcode")

# The four transaction-control statements, each a whole literal. No value and no
# identifier is interpolated into any of them.
BEGIN_STATEMENT: Final[str] = "BEGIN"
SERIALIZABLE_STATEMENT: Final[str] = "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
COMMIT_STATEMENT: Final[str] = "COMMIT"
ROLLBACK_STATEMENT: Final[str] = "ROLLBACK"

# The backoff schedule: the first delay, the ceiling every later delay is capped
# at, and the multiplicative jitter window each delay is drawn from.
BACKOFF_BASE_SECONDS: Final[float] = 0.05
BACKOFF_CAP_SECONDS: Final[float] = 2.0
JITTER_LOW: Final[float] = 0.5
JITTER_HIGH: Final[float] = 1.5

# How many retries follow the first attempt unless the configuration says
# otherwise. This matches the default of the configuration surface key.
DEFAULT_MAX_RETRIES: Final[int] = 5

# The configuration surface key the retry count resolves from.
MAX_RETRIES_KEY: Final[str] = "MOLT_DB_MAX_RETRIES"

# What a transaction is called in a log record when a caller names none.
DEFAULT_TRANSACTION_LABEL: Final[str] = "write"

# The retry counter, the exhaustion counter, and the component every record here
# carries. The retry counter is the leading indicator and the exhaustion counter is
# the outcome: a cluster under contention shows a rising retry count long before a
# transaction is abandoned, and an alarm on the second alone arrives too late to be
# a warning. The transaction label is deliberately not a dimension: labels are
# added as the design grows and each one would cost a billable combination.
RETRIES_METRIC: Final[str] = "store.serialization_retries"
EXHAUSTED_METRIC: Final[str] = "store.serialization_exhausted"
COMPONENT: Final[str] = "store"

# A function that waits, and a function that draws a factor from a window. Both
# are injected so a test drives the schedule instead of waiting it out.
Sleeper: TypeAlias = Callable[[float], None]
Jitter: TypeAlias = Callable[[float, float], float]

# The jitter source. A system-seeded generator rather than the shared module
# state, so a caller that seeds the module generator for its own reasons does not
# accidentally make every writer back off in step again.
_jitter_source: Final[random.SystemRandom] = random.SystemRandom()

DEFAULT_SLEEP: Final[Sleeper] = time.sleep
DEFAULT_JITTER: Final[Jitter] = _jitter_source.uniform

T = TypeVar("T")


class Cursor(Protocol):
    """The one call this module makes on a cursor.

    Declared structurally rather than imported, so the wrapper is driven by a
    recording cursor in a test and by the driver in a deployment without either
    knowing about the other.
    """

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Send one statement, binding any parameters server-side."""


# The wrapper is generic in the cursor as well as in the result, so a caller whose
# body needs a richer cursor than the sending call keeps that richer shape instead
# of having it narrowed to what this module happens to use.
C = TypeVar("C", bound=Cursor)


def is_serialization_failure(error: BaseException) -> bool:
    """Whether a failure is the cluster aborting one of two conflicting transactions.

    The state is read off the failure rather than inferred from its type, because
    the driver is imported lazily and its exception classes are therefore not
    available to compare against. A failure carrying no state at all is not a
    conflict, which is the safe reading: an unrecognised failure propagates.
    """
    for attribute in _STATE_ATTRIBUTES:
        state = getattr(error, attribute, None)
        if isinstance(state, str) and state == SERIALIZATION_FAILURE_STATE:
            return True
    return False


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times a conflicting transaction is retried, and how long between.

    Attributes:
        max_retries: How many retries follow the first attempt. Zero means the
            first attempt is the only one.
        base_seconds: The delay before the first retry, doubled thereafter.
        cap_seconds: The ceiling every delay is capped at before jitter.
        jitter_low: The lower bound of the multiplicative jitter window.
        jitter_high: The upper bound of the multiplicative jitter window.
    """

    max_retries: int = DEFAULT_MAX_RETRIES
    base_seconds: float = BACKOFF_BASE_SECONDS
    cap_seconds: float = BACKOFF_CAP_SECONDS
    jitter_low: float = JITTER_LOW
    jitter_high: float = JITTER_HIGH

    def __post_init__(self) -> None:
        """Refuse a policy that could not produce a bounded, non-negative schedule."""
        if self.max_retries < 0:
            raise ValueError("a retry count cannot be negative")
        if self.base_seconds < 0.0 or self.cap_seconds < 0.0:
            raise ValueError("a backoff delay cannot be negative")
        if self.cap_seconds < self.base_seconds:
            raise ValueError("the backoff ceiling cannot be below the first delay")
        if self.jitter_low < 0.0 or self.jitter_high < self.jitter_low:
            raise ValueError("the jitter window must be non-negative and ordered")

    @property
    def attempts(self) -> int:
        """How many attempts the schedule permits in total, the first included."""
        return self.max_retries + 1

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> RetryPolicy:
        """Build a policy from the configuration surface's retry count."""
        return cls(max_retries=configuration.integer(MAX_RETRIES_KEY))

    def delay(self, retry: int, *, jitter: Jitter = DEFAULT_JITTER) -> float:
        """The seconds to wait before one retry, capped and then jittered.

        The retry index counts from zero, so the first retry waits the base delay
        scaled by the jitter factor and each later retry doubles the unjittered
        part until the ceiling holds it.
        """
        if retry < 0:
            raise ValueError("a retry index cannot be negative")
        unjittered = min(self.cap_seconds, self.base_seconds * float(2**retry))
        return unjittered * jitter(self.jitter_low, self.jitter_high)


# The policy used when a caller names none: the surface's own default.
DEFAULT_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()


def _discard(cursor: Cursor) -> None:
    """Abandon the open transaction, reporting rather than raising on a failure.

    A rollback that itself fails must not replace the failure that caused it, so
    the outcome becomes a log record and the original failure carries on.
    """
    try:
        cursor.execute(ROLLBACK_STATEMENT)
    except Exception as error:
        log(
            Severity.DEBUG,
            COMPONENT,
            "a transaction could not be rolled back after it failed",
            error_type=type(error).__name__,
        )


def in_serializable(
    cursor: C,
    body: Callable[[C], T],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    label: str = DEFAULT_TRANSACTION_LABEL,
    sleep: Sleeper = DEFAULT_SLEEP,
    jitter: Jitter = DEFAULT_JITTER,
) -> T:
    """Run a body in one explicit SERIALIZABLE transaction, retrying a conflict.

    The body receives the cursor and returns whatever the caller needs; the
    wrapper owns the transaction around it. On a serialization conflict the
    transaction is rolled back, the schedule waits, and the body runs again from
    the beginning, which is why a body must be free of effects outside the
    transaction it is given.

    Args:
        cursor: The cursor every statement of the transaction is sent on.
        body: The work to perform inside the transaction.
        policy: How many retries to permit and how long to wait between them.
        label: What to call this transaction in a log record and in the note the
            exhaustion failure carries.
        sleep: How to wait, injected so a test advances a clock instead.
        jitter: How to draw the jitter factor, injected for the same reason.

    Returns:
        Whatever the body returned, once its transaction has committed.

    Raises:
        SerializationExhaustedError: The transaction conflicted on every attempt
            the policy permitted. Nothing it wrote is committed.
    """
    last: BaseException | None = None
    for retry in range(policy.attempts):
        cursor.execute(BEGIN_STATEMENT)
        cursor.execute(SERIALIZABLE_STATEMENT)
        try:
            result = body(cursor)
            cursor.execute(COMMIT_STATEMENT)
        except Exception as error:
            _discard(cursor)
            if not is_serialization_failure(error):
                raise
            last = error
            if retry >= policy.max_retries:
                break
            waiting = policy.delay(retry, jitter=jitter)
            metric(RETRIES_METRIC, 1.0, component=COMPONENT)
            log(
                Severity.DEBUG,
                COMPONENT,
                "a transaction conflicted and is being retried",
                transaction=label,
                retry=retry + 1,
                retries_permitted=policy.max_retries,
                delay_seconds=round(waiting, 6),
            )
            sleep(waiting)
        else:
            return result

    metric(EXHAUSTED_METRIC, 1.0, component=COMPONENT)
    log(
        Severity.ERROR,
        COMPONENT,
        "a transaction was abandoned after conflicting on every attempt",
        transaction=label,
        attempts=policy.attempts,
    )
    exhausted = SerializationExhaustedError(policy.attempts)
    exhausted.add_note(
        f"the transaction {label} conflicted on all {policy.attempts} attempt(s) and "
        "committed nothing"
    )
    raise exhausted from last
