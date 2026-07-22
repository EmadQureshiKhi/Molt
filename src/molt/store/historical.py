"""Historical reads: `AS OF SYSTEM TIME` at a validated instant, bounded by the horizon.

A historical read answers what the cluster held at an earlier instant rather than
what it holds now. Four claims are load-bearing here, and each is arranged so a
caller cannot lose it by forgetting something.

**The horizon is read from the capability record, never assumed.** The cluster's
garbage-collection interval decides how far back a historical read can reach, and
that interval is a measured property of the cluster rather than a constant of this
codebase. So nothing here carries a default: a horizon that has not been probed
makes every historical read refuse, which is the honest answer, rather than one
computed against a number nobody measured. The read contract, which the probe
writing the row must satisfy, is exact:

- the row lives in the `capability` table under the `name` value
  `gc_horizon_seconds`;
- `available` is true when the zone-configuration probe answered and false when
  it did not, and a false reading refuses a historical read just as an absent row
  does;
- `detail` carries the horizon as a base-ten count of seconds and nothing else:
  ASCII digits only, no sign, no unit suffix, no thousands separator, and no
  surrounding space, so a horizon of 4500 seconds is the four characters of that
  number;
- an absent row, an unavailable reading, or a detail in any other form is a
  refusal naming what was missing, not a fallback to some assumed interval.

The predicate `within_gc_horizon` exists so a caller consults the horizon before
attempting the read at all. That matters because the measured horizon is far
shorter than the evidence lifetime of an Erasure_Certificate: a certificate that
leaned on a point-in-time read would stop being re-derivable within about an hour
and a quarter of being issued. The derived count mechanism is therefore the
primary evidence, and a historical read is opportunistic corroboration a caller
attempts only where the predicate says the instant is still reachable.

**The timestamp is validated and rendered, and it is the one value the store
sends unbound.** Every other statement in the store binds every caller-supplied
value, because a bound value is data and can be nothing else. The
`AS OF SYSTEM TIME` argument cannot be bound: the cluster resolves it while
planning the statement, before a parameter would be substituted. So it is
rendered into statement text, and the rendering is made airtight in two
independent steps rather than trusted. First the instant is required to carry an
offset, converted to UTC, and written out component by component with explicit
widths, so the characters come from integer formatting rather than from a locale
or a platform renderer. Then the rendered text is matched against one anchored
ASCII pattern that admits nothing but those digits and their fixed punctuation,
and anything else is refused without being echoed. A quote character, a statement
terminator, a comment marker, a newline, or a non-ASCII digit cannot survive the
second step, so the composed clause holds a timestamp or the read never happens.

**The clause is set on the transaction rather than spliced into the caller's
statement.** A read is framed as one explicit transaction whose first statement
sets the historical instant, after which the caller's own statement runs
untouched with its own values bound. Nothing rewrites, parses, or concatenates
the caller's SQL, so a statement that is a module-level literal elsewhere in the
store stays exactly that literal here, and the rendered instant appears in one
place only.

**A horizon refusal is terminal, and no read is retried at a different instant.**
An instant the recorded horizon does not cover is refused before any statement is
sent. An instant the cluster itself refuses as beyond its own threshold, which is
what happens when the recorded horizon is staler than the cluster's current one,
is translated into the same named failure. In both cases the failure names the
horizon in seconds, and in neither case is a second read attempted at another
instant: a count read at an instant the caller did not ask about would be evidence
about something else.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from molt.errors import HistoricalHorizonError, StoreError
from molt.models.event import require_aware
from molt.store import Cursor, MemoryStore
from molt.store.retry import BEGIN_STATEMENT, COMMIT_STATEMENT, ROLLBACK_STATEMENT
from molt.telemetry import Severity, log

__all__ = [
    "AS_OF_STATEMENT_PREFIX",
    "CAPABILITY_QUERY",
    "COMPONENT",
    "GC_HORIZON_CAPABILITY",
    "HORIZON_FAILURE_FRAGMENT",
    "UTC_OFFSET",
    "GcHorizon",
    "as_of_system_time",
    "gc_horizon",
    "historical",
    "render_as_of_timestamp",
    "require_rendered_form",
    "within_gc_horizon",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The capability row this module reads, and the statement it reads it with. The
# name is a bound parameter, so the one thing this module sends unbound is the
# rendered instant and nothing else.
GC_HORIZON_CAPABILITY: Final[str] = "gc_horizon_seconds"
CAPABILITY_QUERY: Final[str] = "SELECT available, detail FROM capability WHERE name = %s"

# The offset every rendered instant carries. A two-digit offset is what the
# cluster's own rendering of a UTC instant produces, and fixing it here is what
# makes the rendered form a single shape rather than a family of them.
UTC_OFFSET: Final[str] = "+00"

# The statement that places one transaction at a historical instant. The rendered
# instant is appended to this prefix, quoted, and nothing else ever is.
AS_OF_STATEMENT_PREFIX: Final[str] = "SET TRANSACTION AS OF SYSTEM TIME "

# The quote the rendered instant is wrapped in. It is spelled once, here, so the
# composition below reads as what it is.
_QUOTE: Final[str] = "'"

# The one form a rendered instant may take: four digits, two, two, then the time
# components and six digits of fraction, then the fixed offset. The pattern is
# ASCII-only on purpose, because the unrestricted character class would admit
# digits from other scripts that no timestamp parser reads as numbers.
_TIMESTAMP_FORM: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}" + re.escape(UTC_OFFSET),
    re.ASCII,
)

# The one form the recorded horizon may take: ASCII digits and nothing else.
_SECONDS_FORM: Final[re.Pattern[str]] = re.compile(r"\d+", re.ASCII)

# What the cluster's own refusal of an instant beyond its threshold says, matched
# case-insensitively. The message is read rather than the state, because the
# cluster reports this refusal under an uncategorised state that names nothing.
HORIZON_FAILURE_FRAGMENT: Final[str] = "gc threshold"

# How many columns the capability read returns, checked before a row is read so
# the statement and its decoder cannot drift apart silently.
_CAPABILITY_ROW_WIDTH: Final[int] = 2


# ---------------------------------------------------------------------------
# The horizon
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GcHorizon:
    """How far back the cluster still holds the versions a historical read needs.

    Held as a count of seconds because that is what the capability row records
    and what a refusal names. The interval and the floor are derived rather than
    stored, so there is one value to keep true rather than three.
    """

    seconds: int

    def __post_init__(self) -> None:
        """Refuse a horizon that reaches no distance at all."""
        if self.seconds <= 0:
            raise ValueError("a garbage-collection horizon covers a positive number of seconds")

    @property
    def interval(self) -> timedelta:
        """The horizon as an interval, for arithmetic against an instant."""
        return timedelta(seconds=self.seconds)

    def floor(self, now: datetime) -> datetime:
        """The oldest instant a historical read can still reach from a reading."""
        return require_aware(now, "the current reading") - self.interval

    def covers(self, at: datetime, now: datetime) -> bool:
        """Whether an instant is reachable: no older than the floor, and not ahead.

        A future instant is not covered either. The cluster refuses one, and a
        caller asking for one has asked about a state that does not exist yet
        rather than about a state that has been collected.
        """
        moment = require_aware(at, "a historical read timestamp")
        reading = require_aware(now, "the current reading")
        return self.floor(reading) <= moment <= reading


def gc_horizon(store: MemoryStore) -> GcHorizon:
    """Read the measured garbage-collection horizon from the capability record.

    Args:
        store: The connection surface the read is leased from.

    Returns:
        The horizon the probe recorded.

    Raises:
        StoreError: The capability row is absent, reports the probe as
            unavailable, or carries a detail that is not a count of seconds. No
            default is substituted in any of those cases, because a horizon
            nobody measured would make every later refusal and every later
            acceptance a guess.
    """

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(CAPABILITY_QUERY, (GC_HORIZON_CAPABILITY,))
        return cursor.fetchone()

    return _horizon_of(store.read(body))


def within_gc_horizon(
    store: MemoryStore,
    at: datetime,
    *,
    now: datetime | None = None,
    horizon: GcHorizon | None = None,
) -> bool:
    """Whether a historical read at an instant is still reachable on this cluster.

    This is what a caller consults before attempting a historical read, so an
    unreachable instant is a recorded decision not to read rather than a failed
    read. The reading and the horizon are both injectable, so a caller that has
    already read either does not read it twice.

    Args:
        store: The connection surface the horizon is read from.
        at: The instant the caller would read at, timezone aware.
        now: The current reading to measure against, or None to take one.
        horizon: A horizon already read, or None to read it here.

    Returns:
        Whether the instant is no older than the horizon's floor and no later
        than the current reading.
    """
    reading = _reading(now)
    measured = gc_horizon(store) if horizon is None else horizon
    return measured.covers(at, reading)


# ---------------------------------------------------------------------------
# The one rendered value in the store
# ---------------------------------------------------------------------------


def require_rendered_form(text: str) -> str:
    """Return a rendered instant, refusing anything that is not the fixed form.

    This is the second of the two independent validation steps, and the one that
    makes the composition safe rather than merely careful: whatever produced the
    text, only the fixed digits and their punctuation survive here. The refusal
    names the fault and the length rather than echoing the text, because text
    that failed this check is exactly the text that should not be repeated into a
    message another system may render.
    """
    if _TIMESTAMP_FORM.fullmatch(text) is None:
        raise StoreError(
            "a historical read timestamp is rendered into statement text rather than "
            "bound, so a rendering that is not exactly the fixed digit form is refused; "
            f"the rendering was {len(text)} character(s) long"
        )
    return text


def render_as_of_timestamp(at: datetime) -> str:
    """Render an instant in the one textual form the historical clause admits.

    The instant is required to carry an offset, converted to UTC, and written
    component by component with explicit widths, so the characters come from
    integer formatting rather than from a locale-aware renderer. The result is
    then put through the form check, so a caller that hands over something
    datetime-shaped whose components render to anything else gets a refusal
    instead of a composed clause.
    """
    moment = require_aware(at, "a historical read timestamp").astimezone(UTC)
    rendered = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d} "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        f".{moment.microsecond:06d}{UTC_OFFSET}"
    )
    return require_rendered_form(rendered)


def as_of_system_time(at: datetime) -> str:
    """Compose the statement that places one transaction at a historical instant.

    The whole of the composition is the module's own prefix, a quote, the
    validated rendering, and a closing quote. Nothing else a caller supplies
    reaches statement text anywhere in this module.
    """
    return f"{AS_OF_STATEMENT_PREFIX}{_QUOTE}{render_as_of_timestamp(at)}{_QUOTE}"


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------


def historical(
    store: MemoryStore,
    statement: str,
    parameters: Sequence[object] | None = None,
    *,
    at: datetime,
    now: datetime | None = None,
    horizon: GcHorizon | None = None,
) -> tuple[tuple[object, ...], ...]:
    """Run one caller statement against the state the cluster held at an instant.

    The instant is validated and the horizon is consulted before anything is
    sent, so an unreachable instant costs no read. What is then sent is one
    explicit transaction: the historical clause first, the caller's statement
    next with its own values bound, and a commit. The caller's statement is not
    rewritten, parsed, or concatenated with anything.

    Args:
        store: The connection surface the read is leased from.
        statement: The caller's own statement, a literal of the caller's module
            exactly as every other statement in the store is.
        parameters: The values that statement binds, or None when it binds none.
        at: The instant to read at, timezone aware.
        now: The current reading the horizon is measured against, or None to take
            one.
        horizon: A horizon already read, or None to read it here.

    Returns:
        Every row the statement produced at that instant, in the order it
        produced them.

    Raises:
        HistoricalHorizonError: The instant is outside the recorded horizon, or
            the cluster refused it as beyond its own threshold. Nothing was read,
            and nothing was read at any other instant either.
        StoreError: The horizon has not been probed, or the rendered instant was
            not the fixed form.
    """
    clause = as_of_system_time(at)
    reading = _reading(now)
    measured = gc_horizon(store) if horizon is None else horizon
    if not measured.covers(at, reading):
        raise _unreachable(at, reading, measured)

    def body(cursor: Cursor) -> tuple[tuple[object, ...], ...]:
        cursor.execute(BEGIN_STATEMENT)
        try:
            cursor.execute(clause)
            cursor.execute(statement, parameters)
            rows = tuple(tuple(row) for row in cursor.fetchall())
        except Exception as error:
            _abandon(cursor)
            translated = _refused(error, measured)
            if translated is None:
                raise
            raise translated from error
        cursor.execute(COMMIT_STATEMENT)
        return rows

    return store.read(body)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _unreachable(at: datetime, reading: datetime, horizon: GcHorizon) -> HistoricalHorizonError:
    """The refusal for an instant the recorded horizon does not cover.

    The horizon is named in seconds, because that is the fact a caller needs to
    decide what to do instead, and the age is named rather than the instant
    itself, so the message states a distance rather than repeating a value.
    """
    age = int((reading - at).total_seconds())
    if age < 0:
        return HistoricalHorizonError(
            "a historical read named an instant later than the current reading, and a "
            "historical read reads the past; the cluster garbage-collection horizon is "
            f"{horizon.seconds} second(s), and no read was attempted"
        )
    return HistoricalHorizonError(
        f"a historical read named an instant {age} second(s) old, which is beyond the "
        f"cluster garbage-collection horizon of {horizon.seconds} second(s), so the "
        "versions it would read are no longer held; no read was attempted at that "
        "instant and none was attempted at any other"
    )


def _refused(error: BaseException, horizon: GcHorizon) -> HistoricalHorizonError | None:
    """The failure to raise when the cluster itself refused the instant.

    A refusal naming the cluster's own threshold means the recorded horizon is
    staler than the cluster's, which is worth a record: the predicate said the
    instant was reachable and the cluster disagreed. Anything else returns
    nothing and the original failure propagates untouched, so a syntax fault or a
    permission refusal is never renamed into a horizon failure.
    """
    if HORIZON_FAILURE_FRAGMENT not in str(error).casefold():
        return None
    log(
        Severity.WARNING,
        COMPONENT,
        "the cluster refused a historical read as beyond its collection threshold",
        recorded_horizon_seconds=horizon.seconds,
    )
    return HistoricalHorizonError(
        "the cluster refused a historical read as older than the threshold it holds "
        f"versions back to, while the recorded garbage-collection horizon reads "
        f"{horizon.seconds} second(s); no read was attempted at any other instant"
    )


def _abandon(cursor: Cursor) -> None:
    """Discard the historical transaction, reporting rather than raising on failure.

    The failure being handled is the one worth reporting, so a rollback that will
    not send is recorded and swallowed rather than allowed to replace it.
    """
    try:
        cursor.execute(ROLLBACK_STATEMENT)
    except Exception as error:
        log(
            Severity.DEBUG,
            COMPONENT,
            "a historical read transaction could not be abandoned",
            error_type=type(error).__name__,
        )


# ---------------------------------------------------------------------------
# Readings and row decoding
# ---------------------------------------------------------------------------


def _reading(now: datetime | None) -> datetime:
    """The current reading to measure the horizon against.

    A caller may inject one, which is what lets the horizon arithmetic be driven
    directly rather than waited out, and an injected reading must carry an offset
    for the same reason a stored instant must.
    """
    if now is None:
        return datetime.now(UTC)
    return require_aware(now, "the current reading")


def _horizon_of(row: Sequence[object] | None) -> GcHorizon:
    """Read the horizon out of the capability row, refusing every other shape."""
    if row is None:
        raise StoreError(
            f"the capability record holds no {GC_HORIZON_CAPABILITY} row, so the cluster "
            "garbage-collection horizon has not been probed; no historical read is "
            "attempted against an assumed horizon"
        )
    if len(row) != _CAPABILITY_ROW_WIDTH:
        raise StoreError(
            f"the capability read returned {len(row)} column(s) where "
            f"{_CAPABILITY_ROW_WIDTH} are read"
        )
    available, detail = row[0], row[1]
    if not isinstance(available, bool):
        raise StoreError("the capability column available did not return a boolean")
    if not available:
        raise StoreError(
            f"the capability record reports {GC_HORIZON_CAPABILITY} as unavailable, so the "
            "cluster garbage-collection horizon is unknown and no historical read is "
            "attempted"
        )
    if not isinstance(detail, str) or _SECONDS_FORM.fullmatch(detail) is None:
        raise StoreError(
            f"the capability record holds {GC_HORIZON_CAPABILITY} with a detail that is "
            "not a base-ten count of seconds, so the horizon it names cannot be read"
        )
    return GcHorizon(seconds=int(detail))
