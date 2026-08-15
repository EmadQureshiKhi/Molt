"""Graceful termination: stop accepting work, let it settle, close the pool, flush.

A termination signal is not a reason to lose a transaction. Requirement 32.7 fixes
the order every long-lived component shuts down in, and this module is that order
written once: the component stops accepting new work, the work already in flight is
allowed to finish, the connection pool is closed so no connection leaks, and the
buffered telemetry is flushed so the measurements of the final moments are not the
ones that go missing.

**Stopping is a flag rather than an exception.** A signal handler runs between
bytecodes on whatever the interpreter was doing, so raising from one would abort
an in-flight transaction -- the exact failure the requirement exists to prevent.
Instead the handler sets a flag; every accept point reads it and declines, and the
work already inside its `in_flight` block runs to its own end. A component that
declines an arriving request while draining is behaving correctly: the request is
retried against another instance, whereas a half-applied transaction is not
retryable at all.

**Draining is bounded.** A component whose in-flight work never finishes must not
hang forever holding the pool open, so the wait carries a grace period. When it
expires the pool is closed anyway and the number of unsettled units is reported,
because a reported forced close is a fact an operator can act on and a silent hang
is not.

**Closing the pool does not close a leased connection.** The store closes idle
connections immediately and closes leased ones as they come back, so the sequence
here is exactly the one the pool was built for: stop accepting, let the holders
commit, then close.

**Nothing here raises at a caller.** A store that fails to close and a flush that
fails are both reported and stepped over, because a shutdown path that raises turns
a clean termination into a crash and loses the rest of the sequence.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Final, Protocol

from molt.telemetry import Severity, current, log

__all__ = [
    "COMPONENT",
    "DEFAULT_GRACE_SECONDS",
    "TERMINATION_SIGNALS",
    "Closeable",
    "Termination",
    "TerminationReport",
    "current_termination",
    "install_signal_handlers",
    "reset_termination",
]

# What this module calls itself in a log record.
COMPONENT: Final[str] = "lifecycle"

# How long in-flight work is given to settle before the pool is closed anyway.
DEFAULT_GRACE_SECONDS: Final[float] = 30.0

# How long a drain waits between readings of the in-flight count. Short enough that
# a fast drain is not padded out, long enough that the wait is not a spin.
_POLL_SECONDS: Final[float] = 0.05

# The signals a deployment stops a component with. SIGINT is included because an
# interactive run is stopped the same way and must shut down by the same sequence.
TERMINATION_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGTERM, signal.SIGINT)


class Closeable(Protocol):
    """The one call this module makes on a connection pool.

    Declared as a shape rather than an import of the store, so the termination
    sequence depends on nothing that needs a cluster to construct and a test drives
    it with a recording double.
    """

    def close(self) -> None:
        """Close idle connections and refuse further leases."""


class TerminationReport:
    """What one termination did, so a caller can exit on the facts rather than hope."""

    __slots__ = ("closed", "flushed", "forced", "unsettled")

    def __init__(self, *, closed: int, unsettled: int, forced: bool, flushed: bool) -> None:
        self.closed = closed
        self.unsettled = unsettled
        self.forced = forced
        self.flushed = flushed

    def __repr__(self) -> str:
        return (
            f"TerminationReport(closed={self.closed}, unsettled={self.unsettled}, "
            f"forced={self.forced}, flushed={self.flushed})"
        )


class Termination:
    """The stop flag, the in-flight count, and the registered pools.

    One instance per process in production, reached through `current_termination`.
    A test builds its own, which is why nothing here reads module state.
    """

    __slots__ = ("_condition", "_grace_seconds", "_in_flight", "_pools", "_stopping")

    def __init__(self, *, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> None:
        if grace_seconds < 0.0:
            raise ValueError("the termination grace period cannot be negative")
        self._grace_seconds = grace_seconds
        self._condition = threading.Condition(threading.Lock())
        self._in_flight = 0
        self._stopping = False
        self._pools: list[Closeable] = []

    # -- the flag --------------------------------------------------------

    @property
    def grace_seconds(self) -> float:
        """How long in-flight work is given to settle."""
        return self._grace_seconds

    @property
    def stopping(self) -> bool:
        """Whether a termination has been requested, so new work is declined."""
        with self._condition:
            return self._stopping

    @property
    def accepting(self) -> bool:
        """Whether new work may still be accepted, which is the negation of stopping."""
        return not self.stopping

    def in_flight(self) -> int:
        """How many units of work are inside their block right now."""
        with self._condition:
            return self._in_flight

    def request_stop(self) -> None:
        """Stop accepting new work, without disturbing what is already in flight."""
        with self._condition:
            already = self._stopping
            self._stopping = True
            self._condition.notify_all()
        if not already:
            log(
                Severity.INFO,
                COMPONENT,
                "a termination was requested, so no further work is accepted",
                in_flight=self.in_flight(),
            )

    # -- registration ----------------------------------------------------

    def register_pool(self, pool: Closeable) -> None:
        """Register a connection pool to be closed once work has settled."""
        with self._condition:
            if pool not in self._pools:
                self._pools.append(pool)

    # -- in-flight accounting --------------------------------------------

    @contextmanager
    def in_flight_work(self) -> Iterator[None]:
        """Mark one unit of work as in flight for the duration of a block.

        The count is decremented in a finally, so a failing unit still settles.
        Entering is not gated on the flag: an accept point decides whether to admit
        work, and a unit already admitted is always allowed to finish.
        """
        with self._condition:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._condition:
                self._in_flight -= 1
                self._condition.notify_all()

    # -- the sequence ----------------------------------------------------

    def terminate(self, *, grace_seconds: float | None = None) -> TerminationReport:
        """Perform the whole sequence and report what it did.

        The order is fixed by the requirement and is not a preference: stop
        accepting, wait for in-flight work, close every pool, flush telemetry. The
        flush is last because the records the earlier steps write belong in it.
        """
        self.request_stop()
        grace = self._grace_seconds if grace_seconds is None else grace_seconds
        unsettled = self._drain(grace)
        closed = self._close_pools()
        flushed = self._flush()
        report = TerminationReport(
            closed=closed, unsettled=unsettled, forced=unsettled > 0, flushed=flushed
        )
        log(
            Severity.WARNING if report.forced else Severity.INFO,
            COMPONENT,
            "the termination sequence finished",
            pools_closed=closed,
            unsettled_units=unsettled,
            forced=report.forced,
            telemetry_flushed=flushed,
        )
        return report

    def _drain(self, grace: float) -> int:
        """Wait for in-flight work to settle, returning how much never did."""
        with self._condition:
            if self._in_flight == 0:
                return 0
            self._condition.wait_for(lambda: self._in_flight == 0, timeout=max(grace, 0.0))
            remaining = self._in_flight
        if remaining:
            log(
                Severity.WARNING,
                COMPONENT,
                "in-flight work did not settle inside the grace period, so the pool is "
                "closed anyway",
                unsettled_units=remaining,
                grace_seconds=grace,
            )
        return remaining

    def _close_pools(self) -> int:
        """Close every registered pool, stepping over one that refuses to close."""
        with self._condition:
            pools = list(self._pools)
            self._pools.clear()
        closed = 0
        for pool in pools:
            try:
                pool.close()
            except Exception as error:
                log(
                    Severity.WARNING,
                    COMPONENT,
                    "a connection pool could not be closed during termination",
                    error_type=type(error).__name__,
                )
                continue
            closed += 1
        return closed

    def _flush(self) -> bool:
        """Flush buffered telemetry, reporting rather than raising on a failure."""
        try:
            current().flush()
        except Exception:
            return False
        return True


_default: Termination | None = None
_default_lock: Final[threading.Lock] = threading.Lock()


def current_termination() -> Termination:
    """The process-wide instance, built with the default grace period on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Termination()
        return _default


def reset_termination() -> None:
    """Discard the process-wide instance, so the next use builds a fresh one."""
    global _default
    with _default_lock:
        _default = None


def install_signal_handlers(
    termination: Termination | None = None,
) -> tuple[signal.Signals, ...]:
    """Register the stop flag against every termination signal, and report which.

    The handler only sets the flag. It performs no close, no flush, and no wait,
    because a handler runs on whatever the interpreter was doing: doing the work
    here would run the shutdown sequence in the middle of an unrelated transaction.
    The component's own loop reads the flag and calls `terminate` from a place where
    stopping is safe.

    Registration is only possible from the main thread of the main interpreter, so a
    component embedded in a worker thread gets an empty tuple rather than an
    exception, and still terminates through its own call to `terminate`.
    """
    target = current_termination() if termination is None else termination

    def handler(_number: int, _frame: FrameType | None) -> None:
        target.request_stop()

    installed: list[signal.Signals] = []
    for number in TERMINATION_SIGNALS:
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            continue
        installed.append(number)
    if not installed:
        log(
            Severity.WARNING,
            COMPONENT,
            "no termination signal could be registered, so termination is caller-driven",
        )
    return tuple(installed)
