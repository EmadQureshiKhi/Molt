"""Consuming memory mutations: the change stream first, the timestamp poll retained.

The Policy_Watcher's job is to see every mutation once and hand it to an evaluation.
How it sees them is the whole content of this module, and four claims arrange it.

**The sinkless change stream is the primary mechanism, not an attempt.** The delivered
cluster serves `EXPERIMENTAL CHANGEFEED FOR` and reads the rangefeed setting enabled,
so the statement is opened at start with a short resolved interval and consumed
through a streaming cursor on a connection of its own. A dedicated connection is not
tidiness: a sinkless stream never returns, so a pooled connection carrying it would
never come back to the pool, and every other statement of the process would queue
behind a cursor that by design never finishes.

**A batch ends at a resolved timestamp, which is what makes consumption bounded.**
The stream emits a resolved row every configured interval whether or not anything
changed, so pulling until the first resolved row terminates in about that interval
even on an idle cluster. That is why nothing here blocks indefinitely and why a caller
drives the loop in bounded steps it can stop between, rather than handing this module
a thread it cannot recall.

**The position is durable, and it is what a restart resumes from.** The resolved
timestamp is persisted on `watcher_watermark` and passed back as the stream's cursor,
so a restart replays only the unresolved tail. Replay is safe rather than merely short
because the uniqueness constraints on `policy_match` and `approval_queue` absorb a
redelivered mutation, which `molt.policy.apply` relies on and records the one caveat
of.

**The poll is retained for tiers that reject the statement, and entering it is
recorded three ways.** A rejection records `changefeed` unavailable in the capability
record, emits `watcher.degraded_to_polling`, and persists `polling` as the mode, so
the degradation is visible to a probe, to a metric, and to the next process to start.
The poll reads the Ledger on `(recorded_at, id)` from the watermark, served by
`ledger_by_recorded`, at the configured interval; at the surface default of two
seconds the halt bound of the kill switch still holds in the degraded mode. The health
route reports the mode and the last consumed mutation's timestamp in both modes, which
is the same answer either way rather than two answers a reader must reconcile.

Every statement is a whole module-level literal with bound parameters. The two table
names of the change stream are this module's own literals and no caller value reaches
statement text, the resolved interval and the cursor are bound, and both the clock and
the sleeper are injected so a test drives the loop without waiting.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, cast
from uuid import UUID

from molt.config.resolve import Configuration
from molt.errors import StoreError
from molt.lifecycle import current_termination
from molt.models.event import EventCategory, JsonObject, JsonValue, require_aware
from molt.policy.apply import Application, Clock, apply_outcomes, system_clock
from molt.policy.evaluate import Mutation, MutationTable, evaluate
from molt.policy.rules import MatchKind, PolicyRule, load_rules
from molt.store import Cursor, MemoryStore
from molt.store.capability import CHANGEFEED, Capability, record_capability
from molt.telemetry import Severity, log, metric

__all__ = [
    "ALL_CATEGORIES_QUERY",
    "ARTIFACT_SESSION_QUERY",
    "CHANGEFEED_RESUMED_STATEMENT",
    "CHANGEFEED_STATEMENT",
    "COMPONENT",
    "CONSUMED_METRIC",
    "DEGRADED_METRIC",
    "HEALTH_PATH",
    "LIVENESS_PATH",
    "MODE_SETTING",
    "PERSIST_MODE_STATEMENT",
    "POLL_FROM_START_QUERY",
    "POLL_FROM_WATERMARK_QUERY",
    "POLL_INTERVAL_SETTING",
    "RECENT_CATEGORIES_QUERY",
    "RESOLVED_INTERVAL_SETTING",
    "SESSION_COST_QUERY",
    "UPSERT_WATERMARK_STATEMENT",
    "WATERMARK_ID",
    "WATERMARK_QUERY",
    "ChangefeedRejectedError",
    "ChangefeedRow",
    "ConsumptionMode",
    "DedicatedStream",
    "ModePreference",
    "MutationStream",
    "StreamOpener",
    "StreamingConnection",
    "StreamingCursor",
    "Watcher",
    "WatcherHealth",
    "WatcherSettings",
    "Watermark",
    "dedicated_opener",
    "persist_mode",
    "read_watermark",
    "route_answer",
    "store_opener",
    "write_watermark",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "watcher"

# The three counters this module publishes. The halt and approval counters belong to
# the applying module, which is what writes the rows they count.
CONSUMED_METRIC: Final[str] = "watcher.mutations_consumed"
DEGRADED_METRIC: Final[str] = "watcher.degraded_to_polling"

# The configuration keys the consumption surface is read from. Nothing about an
# interval or a bound is stated in this module as a number.
MODE_SETTING: Final[str] = "MOLT_WATCHER_MODE"
POLL_INTERVAL_SETTING: Final[str] = "MOLT_WATCHER_POLL_INTERVAL_SECONDS"
RESOLVED_INTERVAL_SETTING: Final[str] = "MOLT_WATCHER_RESOLVED_INTERVAL"

# The two routes the mode is reported on. One answer serves both, because the mode and
# the last consumed mutation are what a liveness probe and a health probe both need,
# and two answers would be two things to keep in agreement.
HEALTH_PATH: Final[str] = "/health"
LIVENESS_PATH: Final[str] = "/live"

# The watermark is one row, so its key is fixed rather than generated: a second
# watermark row would be a second opinion about where consumption had reached.
WATERMARK_ID: Final[UUID] = UUID("f1a7c3d2-5b48-4e69-9a01-7c8d2e5f36b4")

# The change stream, in the two forms a start takes. The tables are named in this
# module's own literal because they are the schema's tables rather than a caller's
# choice; the interval and the resume position are bound.
#
# The checkpoint frequency is stated alongside the resolved interval and bound to the
# same configured value, because the cluster clamps the resolved interval to the
# checkpoint frequency: with the frequency left at its default, a resolved interval of
# two seconds is served at the default frequency instead, and a batch that ends at a
# resolved row would wait that long. Asking for both is what makes the configured
# interval the interval that is actually served.
CHANGEFEED_STATEMENT: Final[str] = (
    "EXPERIMENTAL CHANGEFEED FOR ledger, derived_artifact "
    "WITH updated, resolved = %s, min_checkpoint_frequency = %s"
)
CHANGEFEED_RESUMED_STATEMENT: Final[str] = (
    "EXPERIMENTAL CHANGEFEED FOR ledger, derived_artifact "
    "WITH updated, resolved = %s, min_checkpoint_frequency = %s, cursor = %s"
)

# The poll, in the two forms a watermark admits. The tuple comparison is what the
# `ledger_by_recorded` index serves, and it is a tuple rather than a timestamp alone
# because two rows may carry one recorded instant and a timestamp-only cursor would
# either skip the second or replay the first forever.
POLL_FROM_START_QUERY: Final[str] = (
    "SELECT id, session_id, client_id, category, occurred_at, recorded_at, payload "
    "FROM ledger ORDER BY recorded_at ASC, id ASC LIMIT %s"
)
POLL_FROM_WATERMARK_QUERY: Final[str] = (
    "SELECT id, session_id, client_id, category, occurred_at, recorded_at, payload "
    "FROM ledger WHERE (recorded_at, id) > (%s, %s) "
    "ORDER BY recorded_at ASC, id ASC LIMIT %s"
)

# The watermark row, read and written.
WATERMARK_QUERY: Final[str] = (
    "SELECT mode, last_mutation_at, last_event_id, resolved_at FROM watcher_watermark WHERE id = %s"
)
UPSERT_WATERMARK_STATEMENT: Final[str] = (
    "UPSERT INTO watcher_watermark "
    "(id, mode, last_mutation_at, last_event_id, resolved_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, now())"
)
# Persisting the mode alone leaves the position where it was, so a degradation does
# not cost a replay of everything already consumed.
PERSIST_MODE_STATEMENT: Final[str] = (
    "INSERT INTO watcher_watermark (id, mode) VALUES (%s, %s) "
    "ON CONFLICT (id) DO UPDATE SET mode = excluded.mode, updated_at = now()"
)

# What an evaluation needs about a Session that the mutation itself does not carry.
SESSION_COST_QUERY: Final[str] = "SELECT cost_usd FROM session WHERE id = %s"
RECENT_CATEGORIES_QUERY: Final[str] = (
    "SELECT category FROM ("
    "SELECT category, seq FROM ledger WHERE session_id = %s ORDER BY seq DESC LIMIT %s"
    ") AS trailing ORDER BY seq ASC"
)
# A rate rule naming no window reads the whole Session, which is what naming no window
# asks for. It is a separate statement rather than a bound of a made-up size, because
# a ceiling this module invented would change what such a rule means.
ALL_CATEGORIES_QUERY: Final[str] = (
    "SELECT category FROM ledger WHERE session_id = %s ORDER BY seq ASC"
)
# A Derived_Artifact row holds no Session, so the Session an artifact was derived
# within is read from its lineage: the earliest edge naming a Session, or naming an
# Event whose Session answers for it.
ARTIFACT_SESSION_QUERY: Final[str] = (
    "SELECT CASE WHEN e.parent_kind = %s THEN e.parent_id ELSE l.session_id END "
    "FROM lineage_edge AS e LEFT JOIN ledger AS l ON l.id = e.parent_id "
    "WHERE e.child_id = %s AND e.parent_kind IN (%s, %s) "
    "ORDER BY e.created_at ASC, e.id ASC LIMIT 1"
)

# The lineage parent kinds a Session is resolved through.
_SESSION_PARENT: Final[str] = "session"
_EVENT_PARENT: Final[str] = "event"

# The keys the change stream's value column carries.
_AFTER_KEY: Final[str] = "after"
# How the stream marks a rendered instant as sitting in the coordinated zone.
_ZONE_MARKER: Final[str] = "Z"
_RESOLVED_KEY: Final[str] = "resolved"

# The columns a decoded row is read from, by the name the table gives them.
_ROW_ID: Final[str] = "id"
_ROW_SESSION: Final[str] = "session_id"
_ROW_CLIENT: Final[str] = "client_id"
_ROW_OWNER: Final[str] = "owner_client_id"
_ROW_CATEGORY: Final[str] = "category"
_ROW_OCCURRED: Final[str] = "occurred_at"
_ROW_RECORDED: Final[str] = "recorded_at"
_ROW_PAYLOAD: Final[str] = "payload"
_ROW_CREATED: Final[str] = "created_at"
_ROW_UPDATED: Final[str] = "updated_at"

# How wide each read is, checked before a row is decoded.
_CHANGEFEED_ROW_WIDTH: Final[int] = 3
_POLL_ROW_WIDTH: Final[int] = 7
_SINGLE_COLUMN: Final[int] = 1

# The transaction labels the two watermark writes appear under.
_WATERMARK_LABEL: Final[str] = "watcher_watermark"
_MODE_LABEL: Final[str] = "watcher_mode"

# How many trailing Events an error-rate rule is offered when no enabled rule names a
# window. Zero means no read at all: a rule set with no rate rule in it must not cost
# a Ledger read per mutation.
_NO_WINDOW: Final[int] = 0

# The divisor that turns a hybrid logical timestamp into a count of seconds. The
# stream reports the resolved position in nanoseconds with a logical suffix, and the
# whole nanosecond part is what a timestamp column holds.
_NANOSECONDS: Final[Decimal] = Decimal(10) ** 9

# The sleeper a caller may replace so a test waits for nothing.
Sleeper = Callable[[float], None]


def _first_line(error: BaseException) -> str:
    """The first line of a failure's message, or its type when it carries none.

    A cluster refusal arrives as several lines, of which the first states what was refused
    and the rest restate the statement. Only the first belongs in a log record, which is one
    field of one line by construction.
    """
    lines = str(error).strip().splitlines()
    return lines[0].strip() if lines else type(error).__name__


class ChangefeedRejectedError(StoreError):
    """The cluster would not serve the sinkless change stream, so the poll is used.

    Raised only where the refusal is the cluster's answer to the statement. It is
    caught by the start path, which records the capability, emits the degradation
    metric, and persists the mode, so no caller of this module handles it.
    """


class ConsumptionMode(StrEnum):
    """How mutations are being consumed, in the schema's constraint order."""

    CHANGEFEED = "changefeed"
    POLLING = "polling"


class ModePreference(StrEnum):
    """What the operator asked for, in the order the configuration surface lists.

    `AUTO` is the delivered value and means the change stream is opened and the poll
    is used only if the statement is refused. The two explicit values exist for an
    operator who knows their tier, and `POLLING` is the one way to reach the fallback
    without a refusal, which is what makes the degraded path testable on a cluster
    that serves the stream perfectly well.
    """

    AUTO = "auto"
    CHANGEFEED = "changefeed"
    POLLING = "polling"


@dataclass(frozen=True, slots=True)
class WatcherSettings:
    """The intervals and bounds consumption is driven by, all read from the surface.

    Attributes:
        preference: Which mechanism the operator asked for.
        poll_interval_seconds: How long the poll waits between batches.
        resolved_interval: The resolved-timestamp interval asked of the stream, in
            the interval form the statement carries.
        batch_limit: How many rows one batch consumes at most, in either mode.
    """

    preference: ModePreference = ModePreference.AUTO
    poll_interval_seconds: int = 2
    resolved_interval: str = "2s"
    batch_limit: int = 100

    def __post_init__(self) -> None:
        ModePreference(self.preference)
        if self.poll_interval_seconds <= 0:
            raise ValueError("the poll interval must be a positive number of seconds")
        if not self.resolved_interval:
            raise ValueError("the resolved interval must be stated")
        if self.batch_limit < 1:
            raise ValueError("a consumption batch must admit at least one row")

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration,
        *,
        batch_limit: int = 100,
    ) -> WatcherSettings:
        """Read the consumption surface, refusing a mode the surface does not name."""
        named = configuration.text(MODE_SETTING).strip().lower()
        try:
            preference = ModePreference(named)
        except ValueError as exc:
            raise ValueError(f"the configured watcher mode {named!r} names no mechanism") from exc
        return cls(
            preference=preference,
            poll_interval_seconds=configuration.integer(POLL_INTERVAL_SETTING),
            resolved_interval=configuration.text(RESOLVED_INTERVAL_SETTING),
            batch_limit=batch_limit,
        )


@dataclass(frozen=True, slots=True)
class Watermark:
    """Where consumption reached, and which mechanism reached it.

    Attributes:
        mode: The mechanism in use when the row was last written.
        last_mutation_at: The recorded instant of the last consumed mutation, which
            is what the poll resumes from and what the health route reports.
        last_event_id: That mutation's identifier, the second half of the poll cursor.
        resolved_at: The last resolved timestamp the stream reported, which is what
            the stream resumes from.
    """

    mode: ConsumptionMode = ConsumptionMode.CHANGEFEED
    last_mutation_at: datetime | None = None
    last_event_id: UUID | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        ConsumptionMode(self.mode)

    @property
    def poll_cursor(self) -> tuple[datetime, UUID] | None:
        """The `(recorded_at, id)` pair the poll resumes after, or None from the start."""
        if self.last_mutation_at is None or self.last_event_id is None:
            return None
        return self.last_mutation_at, self.last_event_id


@dataclass(frozen=True, slots=True)
class ChangefeedRow:
    """One row the stream yielded: a row change, or a resolved timestamp.

    Attributes:
        table: The table a row change belongs to, absent on a resolved row.
        key: The primary key the stream reported, absent on a resolved row.
        value: The decoded value column, which carries the row under `after` or the
            position under `resolved`.
    """

    table: str | None
    key: JsonValue
    value: JsonObject

    @property
    def resolved(self) -> str | None:
        """The resolved position this row reports, or None where it reports a change."""
        position = self.value.get(_RESOLVED_KEY)
        return position if isinstance(position, str) else None

    @property
    def after(self) -> JsonObject | None:
        """The row as it now stands, or None for a resolved row and for a deletion."""
        row = self.value.get(_AFTER_KEY)
        return row if isinstance(row, dict) else None


@dataclass(frozen=True, slots=True)
class WatcherHealth:
    """What the health and liveness routes report: the mode and the position.

    Attributes:
        mode: Which mechanism is consuming.
        changefeed_available: What the capability record says about the stream, so a
            reader can tell a configured poll from a refused stream.
        last_mutation_at: The last consumed mutation's recorded instant.
        mutations_consumed: How many mutations this process has applied.
        running: Whether the loop has been stopped.
    """

    mode: ConsumptionMode
    changefeed_available: bool
    last_mutation_at: datetime | None
    mutations_consumed: int
    running: bool

    @property
    def body(self) -> JsonObject:
        """The answer as a route body, carrying no memory content of any kind."""
        return {
            "mode": ConsumptionMode(self.mode).value,
            "changefeed_available": self.changefeed_available,
            "last_mutation_at": (
                None if self.last_mutation_at is None else self.last_mutation_at.isoformat()
            ),
            "mutations_consumed": self.mutations_consumed,
            "running": self.running,
        }


# ---------------------------------------------------------------------------
# The stream, held on a connection of its own
# ---------------------------------------------------------------------------


class StreamingCursor(Protocol):
    """A cursor that yields rows as they arrive rather than buffering the result."""

    def stream(
        self,
        statement: str,
        params: tuple[object, ...] | None = None,
    ) -> Iterator[tuple[object, ...]]:
        """Send a statement and yield its rows one at a time."""

    def close(self) -> None:
        """Release the cursor."""


class StreamingConnection(Protocol):
    """The dedicated connection the stream is held on."""

    def cursor(self) -> StreamingCursor:
        """Open a streaming cursor."""

    def close(self) -> None:
        """Release the connection."""


class MutationStream(Protocol):
    """The seam the change stream is consumed through.

    A test supplies its own, which is what lets the primary path and a refusal both
    be exercised without a cluster deciding which happens.
    """

    def next_row(self) -> ChangefeedRow | None:
        """The next row, or None where the stream ended."""

    def close(self) -> None:
        """Stop consuming and release whatever the stream holds."""


StreamOpener = Callable[[str, tuple[object, ...]], MutationStream]


class DedicatedStream:
    """A sinkless change stream held open on a connection nothing else uses.

    The first row is pulled during construction, which is what turns a tier that
    refuses the statement into a refusal a caller can catch at start rather than a
    failure arriving mid-batch. It costs at most one resolved interval, because the
    stream reports a resolved position on that interval whether or not anything
    changed.
    """

    __slots__ = ("_buffered", "_closed", "_connection", "_cursor", "_rows")

    def __init__(
        self,
        connection: StreamingConnection,
        statement: str,
        params: tuple[object, ...],
    ) -> None:
        self._connection = connection
        self._cursor: StreamingCursor | None = None
        self._rows: Iterator[tuple[object, ...]] | None = None
        self._buffered: list[tuple[object, ...]] = []
        self._closed = False
        try:
            opened = connection.cursor()
            rows = opened.stream(statement, params)
            first = next(rows)
        except StopIteration:
            self._cursor = opened
            self._rows = None
        # The cause is named in the message rather than only chained. The refusal is caught
        # by the start path, which records a capability, counts a degradation, and logs the
        # reason -- and the reason it logged was this sentence, so every cause arrived as the
        # same six words. A cluster that will not serve the statement and a connection that
        # could not be opened to ask are different problems with different repairs, and one
        # of them was a missing table read that went unnoticed for as long as the statement
        # had existed precisely because the message never varied.
        except Exception as error:
            self.close()
            raise ChangefeedRejectedError(
                "the sinkless change stream was not opened: "
                f"{type(error).__name__}: {_first_line(error)}"
            ) from error
        else:
            self._cursor = opened
            self._rows = rows
            self._buffered.append(first)

    def next_row(self) -> ChangefeedRow | None:
        """The next row of the stream, or None once it has ended or been closed."""
        if self._closed:
            return None
        if self._buffered:
            return _changefeed_row(self._buffered.pop(0))
        if self._rows is None:
            return None
        try:
            return _changefeed_row(next(self._rows))
        except StopIteration:
            self._rows = None
            return None
        except Exception as error:
            raise StoreError("the sinkless change stream stopped answering") from error

    def close(self) -> None:
        """Close the cursor and the connection, in that order, swallowing nothing."""
        self._closed = True
        self._rows = None
        cursor = self._cursor
        self._cursor = None
        if cursor is not None:
            # A cursor or a connection already released is the state close is asking
            # for, so its refusal to be released again is not news.
            with suppress(Exception):
                cursor.close()
        with suppress(Exception):
            self._connection.close()


def store_opener(store: MemoryStore) -> StreamOpener:
    """An opener over connections of the store's own, taken outside its pool.

    This is the opener a deployment uses, and the one `Watcher.from_configuration` defaults
    to. It exists as a named function rather than inline at the call site because it is where
    one narrowing is performed and explained.

    The store's connection protocol describes the pooled statement path, which has no
    streaming cursor in it; the driver's own cursor does have one, and the stream consumes it.
    So the narrowing is a statement about the driver rather than a widening of the store's
    protocol — widening it would oblige every other consumer of a connection to satisfy a
    member none of them uses.
    """

    def connect_with() -> StreamingConnection:
        return cast(StreamingConnection, store.open_dedicated())

    return dedicated_opener(connect_with)


def dedicated_opener(connect_with: Callable[[], StreamingConnection]) -> StreamOpener:
    """An opener that builds one connection per stream and hands the stream its own.

    The factory is called at open time rather than held as a connection, so a
    degradation that never opens a stream never opens a connection either.
    """

    def opener(statement: str, params: tuple[object, ...]) -> MutationStream:
        return DedicatedStream(connect_with(), statement, params)

    return opener


# ---------------------------------------------------------------------------
# The watermark
# ---------------------------------------------------------------------------


def read_watermark(store: MemoryStore) -> Watermark | None:
    """The persisted position, or None where nothing has been consumed yet."""

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(WATERMARK_QUERY, (WATERMARK_ID,))
        return cursor.fetchone()

    row = store.read(body)
    return None if row is None else _watermark_of(row)


def write_watermark(store: MemoryStore, watermark: Watermark) -> Watermark:
    """Persist the whole position, replacing whatever the row held."""

    def body(cursor: Cursor) -> None:
        cursor.execute(
            UPSERT_WATERMARK_STATEMENT,
            (
                WATERMARK_ID,
                ConsumptionMode(watermark.mode).value,
                watermark.last_mutation_at,
                watermark.last_event_id,
                watermark.resolved_at,
            ),
        )

    store.in_serializable(body, label=_WATERMARK_LABEL)
    return watermark


def persist_mode(store: MemoryStore, mode: ConsumptionMode) -> ConsumptionMode:
    """Persist the mechanism alone, leaving the position where it stood."""
    chosen = ConsumptionMode(mode)

    def body(cursor: Cursor) -> None:
        cursor.execute(PERSIST_MODE_STATEMENT, (WATERMARK_ID, chosen.value))

    store.in_serializable(body, label=_MODE_LABEL)
    return chosen


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------


class Watcher:
    """Bounded, cancellable consumption of memory mutations in whichever mode holds.

    A caller drives the loop: `start` chooses the mechanism, `consume_once` takes one
    batch, `run` takes a bounded number of batches, and `stop` ends the loop and
    releases the stream. Nothing here spawns a thread and nothing here loops without
    a bound, which is what lets a test consume exactly as much as it means to.
    """

    __slots__ = (
        "_clock",
        "_consumed",
        "_mode",
        "_opener",
        "_rules",
        "_settings",
        "_sleep",
        "_stopped",
        "_store",
        "_stream",
        "_watermark",
        "_window",
    )

    def __init__(
        self,
        store: MemoryStore,
        rules: Sequence[PolicyRule],
        *,
        settings: WatcherSettings | None = None,
        opener: StreamOpener | None = None,
        clock: Clock = system_clock,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self._store = store
        self._rules = tuple(rules)
        self._settings = WatcherSettings() if settings is None else settings
        self._opener = opener
        self._clock = clock
        self._sleep = sleep
        self._stream: MutationStream | None = None
        self._mode = ConsumptionMode.CHANGEFEED
        self._watermark = Watermark()
        self._consumed = 0
        self._stopped = False
        self._window = _widest_window(self._rules)

    @classmethod
    def from_configuration(
        cls,
        store: MemoryStore,
        configuration: Configuration,
        *,
        opener: StreamOpener | None = None,
        clock: Clock = system_clock,
        sleep: Sleeper = time.sleep,
    ) -> Watcher:
        """Build a watcher whose rule set, intervals, and stream all come from the surface.

        The opener defaults to one that opens a connection of the store's own outside its
        pool, which is what a sinkless stream requires and what makes the primary mechanism
        the mechanism a deployment actually gets.

        It defaulted to nothing, and the consequence was invisible for as long as the module
        existed. A watcher with no opener refuses its own stream before the cluster is asked
        — `_open_stream` raises when the opener is absent — and that refusal is caught by the
        same handler that catches a refusal from the cluster, recorded as the `changefeed`
        capability being unavailable, and logged. So every deployed watcher ran the timestamp
        poll, reported the stream as unavailable, and the deployment's own documentation
        concluded from that record that the cluster's plan did not serve one. The cluster
        served it the whole time. Nothing had ever asked.

        A caller may still pass its own, which is what the tests do: the seam exists so both
        the primary path and a refusal are exercisable without a cluster deciding which
        happens.
        """
        return cls(
            store,
            load_rules(configuration),
            settings=WatcherSettings.from_configuration(configuration),
            opener=store_opener(store) if opener is None else opener,
            clock=clock,
            sleep=sleep,
        )

    # -- what the routes report -----------------------------------------

    @property
    def mode(self) -> ConsumptionMode:
        """The mechanism currently consuming."""
        return self._mode

    @property
    def watermark(self) -> Watermark:
        """The position as this process last advanced it."""
        return self._watermark

    @property
    def mutations_consumed(self) -> int:
        """How many mutations this process has evaluated and applied."""
        return self._consumed

    @property
    def stopped(self) -> bool:
        """Whether the loop has been stopped."""
        return self._stopped

    @property
    def can_open_stream(self) -> bool:
        """Whether this watcher has the means to open a stream at all.

        False means the poll is the only mechanism available to it, whatever the cluster
        would serve, because no opener was supplied. That is worth being able to ask
        separately from what the cluster said: a watcher that cannot ask and a cluster that
        refuses both end in the poll, and for a long while they were indistinguishable from
        outside — which is how a missing default came to be documented as a limitation of the
        cluster's plan.
        """
        return self._opener is not None

    def health(self) -> WatcherHealth:
        """The mode and the last consumed mutation's timestamp, for either route."""
        return WatcherHealth(
            mode=self._mode,
            changefeed_available=self._store.known_capabilities().changefeed,
            last_mutation_at=self._watermark.last_mutation_at,
            mutations_consumed=self._consumed,
            running=not self._stopped,
        )

    # -- the mechanism ---------------------------------------------------

    def start(self) -> ConsumptionMode:
        """Resume from the persisted position and open the mechanism that holds.

        The stream is opened unless the operator asked for the poll outright. A
        refusal is recorded as a capability, counted as a degradation, and persisted
        as the mode, so the next process starts where this one ended up rather than
        rediscovering the refusal.
        """
        persisted = read_watermark(self._store)
        if persisted is not None:
            self._watermark = persisted
        if ModePreference(self._settings.preference) is ModePreference.POLLING:
            return self._degrade("the operator configured the timestamp poll")
        try:
            self._stream = self._open_stream()
        # The refusal's own message is the reason, not a sentence written here. It says which
        # of several unrelated causes actually happened, and a fixed sentence in its place is
        # what let a missing table read read identically to an unsupported plan for as long as
        # the statement had existed.
        except ChangefeedRejectedError as refusal:
            self._record_changefeed(available=False)
            return self._degrade(_first_line(refusal))
        self._record_changefeed(available=True)
        self._mode = ConsumptionMode.CHANGEFEED
        self._watermark = write_watermark(
            self._store,
            _with_mode(self._watermark, ConsumptionMode.CHANGEFEED),
        )
        log(
            Severity.INFO,
            COMPONENT,
            "consuming mutations from the sinkless change stream",
            resumed=self._watermark.resolved_at is not None,
        )
        return self._mode

    def _record_changefeed(self, *, available: bool) -> None:
        """Record what the cluster did with the statement, and re-read the record.

        The re-read is what makes the health route report the fact just probed rather
        than whatever the store held before the probe: the record is read once and held
        by design, so a component that writes a row is the one that must refresh it.
        """
        record_capability(self._store, Capability(CHANGEFEED, available=available))
        self._store.capabilities(refresh=True)

    def _open_stream(self) -> MutationStream:
        """Open the stream, resuming from the persisted resolved position where there is one."""
        if self._opener is None:
            raise ChangefeedRejectedError("no streaming connection was supplied to the watcher")
        interval = self._settings.resolved_interval
        resume = self._watermark.resolved_at
        if resume is None:
            return self._opener(CHANGEFEED_STATEMENT, (interval, interval))
        return self._opener(
            CHANGEFEED_RESUMED_STATEMENT,
            (interval, interval, _hlc_of(resume)),
        )

    def _degrade(self, why: str) -> ConsumptionMode:
        """Enter the poll: count it, persist it, and say the reason it was entered for."""
        self._mode = ConsumptionMode.POLLING
        metric(DEGRADED_METRIC)
        persist_mode(self._store, ConsumptionMode.POLLING)
        self._watermark = _with_mode(self._watermark, ConsumptionMode.POLLING)
        log(Severity.WARNING, COMPONENT, "degraded to the timestamp poll", reason=why)
        return self._mode

    def stop(self) -> None:
        """End the loop and release the stream. Calling it twice is harmless."""
        self._stopped = True
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.close()

    def __enter__(self) -> Watcher:
        """Start consuming, so a caller's block owns the stream's lifetime."""
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Release the stream however the block ended."""
        self.stop()

    # -- the loop --------------------------------------------------------

    def run(self, *, batches: int) -> int:
        """Take at most a stated number of batches, stopping early once stopped.

        The bound is the caller's, not this module's. Between poll batches the
        injected sleeper is called with the configured interval, so a deployment
        waits and a test does not.
        """
        if batches < 1:
            raise ValueError("a bounded run must admit at least one batch")
        applied = 0
        termination = current_termination()
        for taken in range(batches):
            if self._stopped:
                break
            # A requested termination ends the loop between batches rather than
            # inside one: the batch in progress applies its mutations and advances
            # its watermark, so the position the next process resumes from is the
            # position of work that actually completed.
            if termination.stopping:
                self.stop()
                log(
                    Severity.INFO,
                    COMPONENT,
                    "the watcher stopped consuming because a termination was requested",
                    batches_taken=taken,
                    mode=str(self._mode),
                )
                break
            if taken and self._mode is ConsumptionMode.POLLING:
                self._sleep(float(self._settings.poll_interval_seconds))
            with termination.in_flight_work():
                applied += self.consume_once()
        return applied

    def consume_once(self) -> int:
        """Consume one batch and report how many mutations it applied.

        A stream batch ends at the first resolved row or at the batch bound, so it
        terminates within about one resolved interval even with nothing changing. A
        poll batch ends when the statement's rows are exhausted.
        """
        if self._stopped:
            return 0
        if self._mode is ConsumptionMode.POLLING:
            return self._poll_once()
        return self._stream_once()

    def _stream_once(self) -> int:
        """One batch from the change stream, ending at the resolved row."""
        stream = self._stream
        if stream is None:
            self._degrade("the change stream was not open")
            return 0
        applied = 0
        advanced = False
        for _ in range(self._settings.batch_limit):
            row = stream.next_row()
            if row is None:
                break
            position = row.resolved
            if position is not None:
                self._watermark = _resolved_at(self._watermark, position)
                advanced = True
                break
            consumed = self._mutation_of(row)
            if consumed is None:
                continue
            mutation, recorded_at = consumed
            self._apply(mutation)
            applied += 1
            advanced = True
            self._watermark = Watermark(
                mode=ConsumptionMode.CHANGEFEED,
                last_mutation_at=recorded_at,
                last_event_id=mutation.event_id,
                resolved_at=self._watermark.resolved_at,
            )
        if advanced:
            self._watermark = write_watermark(self._store, self._watermark)
        return applied

    def _poll_once(self) -> int:
        """One poll batch, read on `(recorded_at, id)` from the persisted position."""
        rows = self._poll_rows()
        applied = 0
        for row in rows:
            mutation, recorded_at = _polled_mutation(row)
            self._apply(self._enriched(mutation))
            applied += 1
            self._watermark = Watermark(
                mode=ConsumptionMode.POLLING,
                last_mutation_at=recorded_at,
                last_event_id=mutation.row_id,
                resolved_at=self._watermark.resolved_at,
            )
        if applied:
            self._watermark = write_watermark(self._store, self._watermark)
        return applied

    def _poll_rows(self) -> tuple[tuple[object, ...], ...]:
        """The next batch of Ledger rows after the persisted position."""
        cursor_pair = self._watermark.poll_cursor
        limit = self._settings.batch_limit

        def body(cursor: Cursor) -> tuple[tuple[object, ...], ...]:
            if cursor_pair is None:
                cursor.execute(POLL_FROM_START_QUERY, (limit,))
            else:
                cursor.execute(
                    POLL_FROM_WATERMARK_QUERY,
                    (cursor_pair[0], cursor_pair[1], limit),
                )
            return tuple(cursor.fetchall())

        return self._store.read(body)

    # -- one mutation ----------------------------------------------------

    def _apply(self, mutation: Mutation) -> Application:
        """Evaluate one mutation and write whatever the outcomes ask for."""
        outcomes = evaluate(mutation, self._rules)
        applied = apply_outcomes(self._store, mutation, outcomes, clock=self._clock)
        self._consumed += 1
        metric(CONSUMED_METRIC)
        return applied

    def _mutation_of(self, row: ChangefeedRow) -> tuple[Mutation, datetime] | None:
        """One streamed row change and the instant the watermark records for it.

        A deletion carries no row to evaluate, a row of an unexpected table is not
        this stream's business, and a Derived_Artifact row whose lineage names no
        Session cannot be recorded against one. Each is skipped rather than raised,
        because one unusable row must not end consumption of the rest.

        The recorded instant is the row's own where the table carries one, so a
        watermark written in this mode is the same kind of position a poll would
        resume from, and a later degradation resumes from something meaningful.
        """
        after = row.after
        table = _table_of(row.table)
        if after is None or table is None:
            return None
        if table is MutationTable.LEDGER:
            built = _streamed_ledger_mutation(after)
        else:
            built = self._streamed_artifact_mutation(after)
        if built is None:
            return None
        recorded = _instant_of(after.get(_ROW_RECORDED)) or built.occurred_at
        return self._enriched(built), recorded

    def _streamed_artifact_mutation(self, after: JsonObject) -> Mutation | None:
        """Build a Derived_Artifact mutation, attributing it through its lineage."""
        row_id = _uuid_of(after.get(_ROW_ID))
        client_id = _uuid_of(after.get(_ROW_OWNER))
        occurred = _instant_of(after.get(_ROW_UPDATED)) or _instant_of(after.get(_ROW_CREATED))
        if row_id is None or client_id is None or occurred is None:
            return None
        session_id = self._artifact_session(row_id)
        if session_id is None:
            log(
                Severity.DEBUG,
                COMPONENT,
                "a derived artifact names no session in its lineage, so no outcome is recorded",
            )
            return None
        return Mutation(
            table=MutationTable.DERIVED_ARTIFACT,
            row_id=row_id,
            session_id=session_id,
            client_id=client_id,
            occurred_at=occurred,
        )

    def _artifact_session(self, artifact_id: UUID) -> UUID | None:
        """The Session an artifact was derived within, read from its lineage."""

        def body(cursor: Cursor) -> tuple[object, ...] | None:
            cursor.execute(
                ARTIFACT_SESSION_QUERY,
                (_SESSION_PARENT, artifact_id, _EVENT_PARENT, _SESSION_PARENT),
            )
            return cursor.fetchone()

        row = self._store.read(body)
        if row is None:
            return None
        return _uuid_of(_one_column(row))

    def _enriched(self, mutation: Mutation) -> Mutation:
        """Attach the Session-scoped values the two session rules read.

        The cost and the trailing categories are read here rather than inside the
        evaluation, which is what keeps the evaluation a function of its arguments and
        the order-independence property testable. The trailing window is read only
        where an enabled rule names one.
        """
        cost = self._session_cost(mutation.session_id)
        categories = self._recent_categories(mutation.session_id)
        return Mutation(
            table=mutation.table,
            row_id=mutation.row_id,
            session_id=mutation.session_id,
            client_id=mutation.client_id,
            occurred_at=mutation.occurred_at,
            category=mutation.category,
            payload=mutation.payload,
            session_cost_usd=cost,
            recent_categories=categories,
        )

    def _session_cost(self, session_id: UUID) -> Decimal:
        """The Session's accrued cost, read as the exact decimal the column holds."""

        def body(cursor: Cursor) -> tuple[object, ...] | None:
            cursor.execute(SESSION_COST_QUERY, (session_id,))
            return cursor.fetchone()

        row = self._store.read(body)
        if row is None:
            return Decimal(0)
        value = _one_column(row)
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, str)):
            return Decimal(value)
        return Decimal(0)

    def _recent_categories(self, session_id: UUID) -> tuple[EventCategory, ...]:
        """The trailing Event categories an error-rate rule reads, oldest first."""
        window = self._window
        if window == _NO_WINDOW:
            return ()

        def body(cursor: Cursor) -> tuple[tuple[object, ...], ...]:
            if window is None:
                cursor.execute(ALL_CATEGORIES_QUERY, (session_id,))
            else:
                cursor.execute(RECENT_CATEGORIES_QUERY, (session_id, window))
            return tuple(cursor.fetchall())

        categories: list[EventCategory] = []
        for row in self._store.read(body):
            named = _one_column(row)
            if isinstance(named, str):
                categories.append(EventCategory(named))
        return tuple(categories)


def route_answer(watcher: Watcher, path: str) -> WatcherHealth | None:
    """The answer for the health route and the liveness route, or None for any other.

    Both routes answer the same thing on purpose: the mode and the last consumed
    mutation's timestamp are what a probe of either kind needs, and two bodies would
    be two things to keep in agreement.
    """
    normalised = path.rstrip("/") or "/"
    if normalised in {HEALTH_PATH, LIVENESS_PATH}:
        return watcher.health()
    return None


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _widest_window(rules: Sequence[PolicyRule]) -> int | None:
    """How many trailing Event categories a mutation must carry for this rule set.

    Zero means no rate rule is enabled, so no Ledger read is paid per mutation. None
    means some enabled rate rule names no window, which asks for the whole Session.
    Otherwise it is the widest window any enabled rate rule named, because one read
    serves every rule and the widest is the only one that serves them all.
    """
    windows: list[int] = []
    for rule in rules:
        if not rule.enabled or MatchKind(rule.match_kind) is not MatchKind.ERROR_RATE:
            continue
        if rule.window_events is None:
            return None
        windows.append(rule.window_events)
    return max(windows, default=_NO_WINDOW)


def _resolved_at(watermark: Watermark, position: str) -> Watermark:
    """The same position under a fresh resolved timestamp, which a restart resumes from.

    A position the stream reported in a form this module cannot read leaves the stored
    resolved timestamp where it was, so a restart replays a little rather than
    resuming from something invented.
    """
    resolved = _instant_of_hlc(position)
    return Watermark(
        mode=ConsumptionMode.CHANGEFEED,
        last_mutation_at=watermark.last_mutation_at,
        last_event_id=watermark.last_event_id,
        resolved_at=watermark.resolved_at if resolved is None else resolved,
    )


def _with_mode(watermark: Watermark, mode: ConsumptionMode) -> Watermark:
    """The same position under a different mechanism."""
    return Watermark(
        mode=mode,
        last_mutation_at=watermark.last_mutation_at,
        last_event_id=watermark.last_event_id,
        resolved_at=watermark.resolved_at,
    )


def _table_of(named: str | None) -> MutationTable | None:
    """Which covered table a streamed row belongs to, or None where it is neither."""
    if named is None:
        return None
    trimmed = named.strip().strip('"').rsplit(".", maxsplit=1)[-1]
    try:
        return MutationTable(trimmed)
    except ValueError:
        return None


def _changefeed_row(row: Sequence[object]) -> ChangefeedRow:
    """Decode one streamed row, refusing a result of any other shape."""
    if len(row) != _CHANGEFEED_ROW_WIDTH:
        raise StoreError(
            f"the change stream yielded {len(row)} column(s) where {_CHANGEFEED_ROW_WIDTH} are read"
        )
    table = row[0]
    return ChangefeedRow(
        table=table if isinstance(table, str) else None,
        key=_decoded(row[1]),
        value=_decoded_object(row[2]),
    )


def _decoded(value: object) -> JsonValue:
    """One stream column, decoded from text where the driver did not decode it."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            decoded: JsonValue = json.loads(value)
        except ValueError:
            return value
        return decoded
    if value is None or isinstance(value, (bool, int, float, list, dict)):
        return value
    return str(value)


def _decoded_object(value: object) -> JsonObject:
    """The value column of a streamed row, which is always an object."""
    decoded = _decoded(value)
    return decoded if isinstance(decoded, dict) else {}


def _streamed_ledger_mutation(after: JsonObject) -> Mutation | None:
    """Build a Ledger mutation from a streamed row, or None where a column is absent."""
    row_id = _uuid_of(after.get(_ROW_ID))
    session_id = _uuid_of(after.get(_ROW_SESSION))
    client_id = _uuid_of(after.get(_ROW_CLIENT))
    occurred = _instant_of(after.get(_ROW_OCCURRED))
    named = after.get(_ROW_CATEGORY)
    if row_id is None or session_id is None or client_id is None or occurred is None:
        return None
    if not isinstance(named, str):
        return None
    payload = after.get(_ROW_PAYLOAD)
    return Mutation(
        table=MutationTable.LEDGER,
        row_id=row_id,
        session_id=session_id,
        client_id=client_id,
        occurred_at=occurred,
        category=EventCategory(named),
        payload=payload if isinstance(payload, dict) else {},
    )


def _polled_mutation(row: Sequence[object]) -> tuple[Mutation, datetime]:
    """Build a mutation from one polled Ledger row, with the instant the poll resumes after."""
    if len(row) != _POLL_ROW_WIDTH:
        raise StoreError(f"the poll returned {len(row)} column(s) where {_POLL_ROW_WIDTH} are read")
    row_id = _uuid_of(row[0])
    session_id = _uuid_of(row[1])
    client_id = _uuid_of(row[2])
    occurred = _instant_of(row[4])
    recorded = _instant_of(row[5])
    named = row[3]
    if row_id is None or session_id is None or client_id is None:
        raise StoreError("a polled Ledger row named no identifier")
    if occurred is None or recorded is None:
        raise StoreError("a polled Ledger row carried no timestamp")
    if not isinstance(named, str):
        raise StoreError("a polled Ledger row named no category")
    payload = row[6]
    return (
        Mutation(
            table=MutationTable.LEDGER,
            row_id=row_id,
            session_id=session_id,
            client_id=client_id,
            occurred_at=occurred,
            category=EventCategory(named),
            payload=payload if isinstance(payload, dict) else {},
        ),
        recorded,
    )


def _watermark_of(row: Sequence[object]) -> Watermark:
    """Build the position from the stored row."""
    named = row[0]
    if not isinstance(named, str):
        raise StoreError("the watermark column mode did not return text")
    return Watermark(
        mode=ConsumptionMode(named),
        last_mutation_at=_instant_of(row[1]),
        last_event_id=_uuid_of(row[2]),
        resolved_at=_instant_of(row[3]),
    )


def _one_column(row: Sequence[object]) -> object:
    """The single column of a single-column read."""
    if len(row) != _SINGLE_COLUMN:
        raise StoreError(f"a read returned {len(row)} column(s) where {_SINGLE_COLUMN} is read")
    return row[0]


def _uuid_of(value: object) -> UUID | None:
    """One identifier, however the driver or the stream rendered it."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _instant_of(value: object) -> datetime | None:
    """One offset-aware instant, however the driver or the stream rendered it."""
    if isinstance(value, datetime):
        return require_aware(value, "a consumed mutation timestamp")
    if not isinstance(value, str) or not value:
        return None
    # A trailing zone marker is dropped rather than rewritten as an offset, and the
    # zone is attached afterwards, so the one form the stream renders needs no literal
    # offset written anywhere in this module.
    text = value[: -len(_ZONE_MARKER)] if value.endswith(_ZONE_MARKER) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _instant_of_hlc(position: str) -> datetime | None:
    """The instant a hybrid logical timestamp names, or None where it names none.

    The stream reports the position as nanoseconds with a logical suffix. Only the
    nanosecond part is a time, so the suffix is dropped rather than interpreted.
    """
    whole = position.split(".", maxsplit=1)[0]
    if not whole.isdigit():
        return None
    seconds = Decimal(whole) / _NANOSECONDS
    return datetime.fromtimestamp(float(seconds), tz=UTC)


def _hlc_of(resume: datetime) -> str:
    """The resume position as the count of nanoseconds the cursor option reads."""
    return str(int(Decimal(str(resume.timestamp())) * _NANOSECONDS))
