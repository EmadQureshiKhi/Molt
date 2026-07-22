"""The working tier: scratch state written in place, read by key, and purged wholesale.

The working tier is the one tier in this schema whose rows nothing is permitted to
depend on. Every other tier exists because something has to remain readable
later; this one exists because something has to be forgettable. Five claims carry
this module, and each is arranged so a caller cannot lose it by forgetting
something.

**A write overwrites in place, because the key is the Session and the scratch key
together.** The primary key is exactly that pair, and the write is an `UPSERT` on
it, so a plan rewritten forty times leaves one row rather than forty. A tier that
accumulated versions would be a history, and a history is precisely what this tier
must not become: a history is something a later reader can depend on.

**The expiry is set on every write from the configured interval, and the interval
is read rather than written here.** The count of seconds lives on the
configuration surface under one key, so an operator shortening the tier's lifetime
changes a setting rather than this module, and nothing here carries a fallback
number of its own. The stored expiry is the write instant plus that interval, and
both columns are bound from one reading, so the instant a row records as its last
write and the instant it records as its expiry cannot disagree by however long a
statement took to reach the cluster. The cluster removes the row afterwards under
the Row-Level TTL configuration the migration applied, with no process outside the
cluster involved; nothing here configures that expiry and nothing here deletes an
expired row, because a tier whose disposability depended on a sweeper of its own
would be disposable only while the sweeper ran.

**Nothing here can make a working row into anything else.** The four statements
below name `working_memory` and no other table. There is no statement that writes
a working row's key into `lineage_edge`, `client_binding`, `disposition`, or
`ledger_checkpoint`, and there is no statement that reads a working row into a
candidate set. The enforcement is upstream of the choice: a lineage insert proves
its parent exists by joining `artifact_ref`, that view spans an Event, a Session,
and a Derived_Artifact, and this table is not among them, so the join that would
have to succeed returns no row. The absence of a promotion path here is therefore
what makes the schema's own refusal reachable rather than incidental, and the
taxonomy in the tier model records the same fact from the other direction: the
`working` tier holds one table and that table appears in no other tier.

**The purge is one set-based statement and one number.** Erasure deletes every
working row of a Client with a single statement served by the index over the tenant
column, and that statement reports the count it removed as an aggregate rather
than as a row per deletion. The Erasure_Engine records that number on the run row
and the measurement carries the same number, because a Disposition is evidence
about content that mattered and a working row is by construction content that did
not. There is no loop, no batching, and no per-row evidence anywhere on this path.

**The measurement is emitted after the transaction that produced it committed.**
A write transaction here is retried on a serialization conflict, and a body that
emitted a count would report one for an attempt that was rolled back. So the
deleting function returns the count and the emission is a separate step the
committing caller takes, which is what makes one commit produce exactly one
measurement. The measurement carries no tenant dimension: a per-Client dimension
would grow the billable combination count with the number of Clients, and the
tenant belongs in the log record and on the run row where it costs nothing.

Every statement is a whole module-level literal, every caller-supplied value is a
bound parameter, and no identifier is ever interpolated. The scratch value travels
as canonical JSON text with the cast written into the statement, so this module
needs no driver-side document adapter and stays importable with no driver
installed. No log record from this module carries a scratch value: the tier holds
whatever an agent put in it, which is content.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from molt.config.resolve import Configuration, load_configuration
from molt.errors import StoreError
from molt.models.event import JsonObject, require_aware
from molt.store import Cursor, MemoryStore
from molt.telemetry import Severity, log, metric

__all__ = [
    "COMPONENT",
    "DEFAULT_SCRATCH_LIMIT",
    "MAX_SCRATCH_LIMIT",
    "PURGE_CLIENT_SCRATCH_STATEMENT",
    "SELECT_SCRATCH_STATEMENT",
    "SELECT_SESSION_SCRATCH_STATEMENT",
    "UPSERT_SCRATCH_STATEMENT",
    "WORKING_ROWS_DELETED_METRIC",
    "WORKING_TTL_KEY",
    "ScratchRow",
    "ScratchWrite",
    "WorkingInterval",
    "delete_client_scratch",
    "purge_working_rows",
    "read_scratch",
    "record_working_purge",
    "select_scratch",
    "select_session_scratch",
    "session_scratch",
    "upsert_scratch",
    "write_scratch",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The configuration surface key the expiry interval is read from, as a count of
# seconds. The number it defaults to lives on that surface and nowhere here: a
# constant of this module standing in for it would be a second place to change,
# and the one that an operator did not change would be the one that took effect.
WORKING_TTL_KEY: Final[str] = "MOLT_WORKING_TTL_SECONDS"

# How many rows a per-Session listing returns when a caller names no bound, and
# the ceiling a caller may not ask past. The bound is a parameter of the listing
# statement, so no caller can ask for an unbounded scan of a Session's scratch.
DEFAULT_SCRATCH_LIMIT: Final[int] = 100
MAX_SCRATCH_LIMIT: Final[int] = 10000

# The measurement the aggregate purge count is carried by, undimensioned for the
# reason the module docstring gives.
WORKING_ROWS_DELETED_METRIC: Final[str] = "erasure.working_rows_deleted"

# How many columns each row shape carries, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_SCRATCH_ROW_WIDTH: Final[int] = 6
_COUNT_ROW_WIDTH: Final[int] = 1

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The write. `UPSERT` on the primary key is what makes a repeated write an
# overwrite in place rather than a second version, and the pair the key spans is
# the Session and the scratch key, so a Session's scratch is a set of named
# values rather than a stream of them. Both instants are bound from one reading,
# so the row's last-write instant and its expiry cannot disagree about when the
# write happened. The whole row comes back, which is what makes the stored expiry
# the cluster's own report rather than the caller's expectation of it.
UPSERT_SCRATCH_STATEMENT: Final[str] = (
    "UPSERT INTO working_memory "
    "(session_id, scratch_key, client_id, value, updated_at, expires_at) "
    "VALUES (%s, %s, %s, %s::JSONB, %s, %s) "
    "RETURNING session_id, scratch_key, client_id, value, updated_at, expires_at"
)

# The point read: the whole primary key, so the cluster answers with one seek
# rather than a scan. The tenant is named alongside the key rather than instead
# of it, for the reason every other read in this layer names it: holding a
# Session identifier is not authority over the rows that name it.
SELECT_SCRATCH_STATEMENT: Final[str] = (
    "SELECT session_id, scratch_key, client_id, value, updated_at, expires_at "
    "FROM working_memory WHERE session_id = %s AND scratch_key = %s AND client_id = %s"
)

# The per-Session listing, over the leading column of the primary key and so
# served by it. Ordered by scratch key, which makes the order total, since the
# pair is unique and the Session is fixed; two reads of one Session therefore
# report the same sequence.
SELECT_SESSION_SCRATCH_STATEMENT: Final[str] = (
    "SELECT session_id, scratch_key, client_id, value, updated_at, expires_at "
    "FROM working_memory WHERE session_id = %s AND client_id = %s "
    "ORDER BY scratch_key ASC LIMIT %s"
)

# The purge: one statement, one predicate on the tenant column the index covers,
# and one aggregate number. The deletion is wrapped in a common table expression
# whose rows are counted, so the count is part of the same statement rather than
# a property of a driver's cursor, and it is one row of one column rather than a
# row per deletion. A loop over batches would have produced the same rows and a
# count nobody could attribute to a single instant.
PURGE_CLIENT_SCRATCH_STATEMENT: Final[str] = (
    "WITH purged AS ("
    "DELETE FROM working_memory WHERE client_id = %s RETURNING session_id"
    ") SELECT count(*) FROM purged"
)

# The labels the transactions of this module appear under in a log record and in
# the note an exhausted retry attaches.
_WRITE_LABEL: Final[str] = "working_write"
_PURGE_LABEL: Final[str] = "working_purge"


# ---------------------------------------------------------------------------
# The configured interval
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkingInterval:
    """How long a working row lives, as the configuration surface states it.

    Held as a count of seconds because that is the form the surface holds and the
    form the migration's own expiry default is stated in. The interval and the
    expiry are derived rather than stored, so there is one value to keep true
    rather than three.
    """

    seconds: int

    def __post_init__(self) -> None:
        """Refuse an interval that gives a row no life at all."""
        if self.seconds <= 0:
            raise ValueError("a working-tier expiry interval covers a positive number of seconds")

    @classmethod
    def from_configuration(cls, configuration: Configuration | None = None) -> WorkingInterval:
        """Read the interval from the configuration surface, resolving it if needed.

        A caller writing many rows resolves the configuration once and hands the
        interval to each write, so the ordinary path costs one resolution rather
        than one per row.
        """
        resolved = load_configuration() if configuration is None else configuration
        return cls(seconds=resolved.integer(WORKING_TTL_KEY))

    @property
    def interval(self) -> timedelta:
        """The interval as a duration, for arithmetic against an instant."""
        return timedelta(seconds=self.seconds)

    def expiry_from(self, now: datetime) -> datetime:
        """The instant a row written at a reading stops being live."""
        return require_aware(now, "a working-tier write instant") + self.interval


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScratchWrite:
    """One piece of scratch state to write, keyed by Session and scratch key.

    The shape deliberately carries no expiry: the expiry is a property of the
    tier read from configuration at write time, so a caller cannot present a
    lifetime of its own and cannot present none. It carries no identifier of its
    own either, because the row has none: the key is the pair, which is what
    makes a repeated write an overwrite.
    """

    session_id: UUID
    scratch_key: str
    client_id: UUID
    value: JsonObject

    def __post_init__(self) -> None:
        """Refuse a write that names no scratch key."""
        if not self.scratch_key:
            raise ValueError("a working row is named by the scratch key it is written under")


@dataclass(frozen=True, slots=True)
class ScratchRow:
    """One stored working row, as the cluster reports it.

    The same shape comes back from a write and from a read, because a write of
    this tier reports the row it left rather than an acknowledgement: the stored
    expiry is the fact a caller may want, and reading it off the row it was
    written into is the only way to have it rather than assume it.
    """

    session_id: UUID
    scratch_key: str
    client_id: UUID
    value: JsonObject
    updated_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def upsert_scratch(
    cursor: Cursor,
    entry: ScratchWrite,
    *,
    interval: WorkingInterval,
    now: datetime | None = None,
) -> ScratchRow:
    """Write one working row on a caller's cursor, overwriting any row it replaces.

    The interval is a required argument here rather than one this function
    resolves, because this is the composable form: a caller framing its own
    transaction has already resolved the configuration, and resolving it again per
    row inside that transaction would read a surface the transaction does not
    depend on.

    Args:
        cursor: The cursor the caller's transaction is running on.
        entry: The scratch state to write.
        interval: How long the row lives, as the configuration surface states it.
        now: The write instant, timezone aware, or None to take a reading. Both
            stored instants come from this one reading.

    Returns:
        The row the cluster holds after the write, its stored expiry included.

    Raises:
        StoreError: The write reported no row, so nothing is known to be stored.
    """
    written = _reading(now)
    cursor.execute(
        UPSERT_SCRATCH_STATEMENT,
        (
            entry.session_id,
            entry.scratch_key,
            entry.client_id,
            _canonical_json(entry.value),
            written,
            interval.expiry_from(written),
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            "the working-tier write reported no row, so no scratch state is known to be stored"
        )
    return _scratch_of(row)


def write_scratch(
    store: MemoryStore,
    entry: ScratchWrite,
    *,
    interval: WorkingInterval | None = None,
    now: datetime | None = None,
) -> ScratchRow:
    """Write one working row in a transaction of its own.

    The interval is optional here and read from the configuration surface when it
    is absent, so a single write reads as one expression. A caller writing many
    rows resolves it once and passes it, which is what keeps a batch from
    resolving the surface per row.
    """
    chosen = WorkingInterval.from_configuration() if interval is None else interval

    def body(cursor: Cursor) -> ScratchRow:
        return upsert_scratch(cursor, entry, interval=chosen, now=now)

    return store.in_serializable(body, label=_WRITE_LABEL)


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


def select_scratch(
    cursor: Cursor,
    session_id: UUID,
    scratch_key: str,
    client_id: UUID,
) -> ScratchRow | None:
    """Read one working row by its whole key on a caller's cursor.

    Returns:
        The stored row, or None when the key names none for this tenant. An
        expired row the cluster has already removed is absent in exactly the same
        way, which is the point of the tier: nothing here distinguishes a row that
        never existed from one that has been forgotten.
    """
    cursor.execute(SELECT_SCRATCH_STATEMENT, (session_id, scratch_key, client_id))
    row = cursor.fetchone()
    return None if row is None else _scratch_of(row)


def select_session_scratch(
    cursor: Cursor,
    session_id: UUID,
    client_id: UUID,
    *,
    limit: int = DEFAULT_SCRATCH_LIMIT,
) -> tuple[ScratchRow, ...]:
    """Read one Session's working rows on a caller's cursor, in scratch-key order."""
    cursor.execute(SELECT_SESSION_SCRATCH_STATEMENT, (session_id, client_id, _bounded(limit)))
    return tuple(_scratch_of(row) for row in cursor.fetchall())


def read_scratch(
    store: MemoryStore,
    session_id: UUID,
    scratch_key: str,
    client_id: UUID,
) -> ScratchRow | None:
    """Read one working row on a leased connection, framing no transaction."""

    def body(cursor: Cursor) -> ScratchRow | None:
        return select_scratch(cursor, session_id, scratch_key, client_id)

    return store.read(body)


def session_scratch(
    store: MemoryStore,
    session_id: UUID,
    client_id: UUID,
    *,
    limit: int = DEFAULT_SCRATCH_LIMIT,
) -> tuple[ScratchRow, ...]:
    """Read one Session's working rows on a leased connection, framing no transaction."""

    def body(cursor: Cursor) -> tuple[ScratchRow, ...]:
        return select_session_scratch(cursor, session_id, client_id, limit=limit)

    return store.read(body)


# ---------------------------------------------------------------------------
# The purge
# ---------------------------------------------------------------------------


def delete_client_scratch(cursor: Cursor, client_id: UUID) -> int:
    """Delete every working row of one Client on a caller's cursor, as one statement.

    This is the form the Erasure_Engine composes: the count it returns is written
    onto the run row in the same transaction that removed the rows, so the number
    on the run row is the number that was deleted rather than a number read
    afterwards. No Disposition is produced for any of these rows, because a
    Disposition is evidence about content that mattered.

    No measurement is emitted here. The caller's transaction may be retried, and
    a count emitted from inside it would report work that was rolled back; the
    committing caller records the measurement afterwards instead.

    Returns:
        How many rows the statement removed, as one aggregate count.
    """
    cursor.execute(PURGE_CLIENT_SCRATCH_STATEMENT, (client_id,))
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            "the working-tier purge reported no count, so the number of rows it removed "
            "is unknown and nothing can be recorded on the run"
        )
    return _as_count(_column(row, 0, _COUNT_ROW_WIDTH))


def record_working_purge(count: int, client_id: UUID) -> int:
    """Emit the aggregate purge count, once, for a transaction that has committed.

    Separate from the deletion on purpose: exactly one commit produces exactly
    one measurement, whatever the retry wrapper had to do to get there. The
    measurement carries the count as its value and no dimension at all, while the
    tenant travels in the log record, where naming it costs nothing.

    Returns:
        The count, unchanged, so a caller records and reports in one expression.
    """
    if count < 0:
        raise ValueError("a purge count is the number of rows removed and cannot be negative")
    metric(WORKING_ROWS_DELETED_METRIC, float(count))
    log(
        Severity.INFO,
        COMPONENT,
        "purged the working tier for a client as one aggregate count",
        client_id=str(client_id),
        working_rows_deleted=count,
    )
    return count


def purge_working_rows(store: MemoryStore, client_id: UUID) -> int:
    """Delete every working row of one Client in a transaction of its own.

    The count is emitted once the transaction has committed, so a conflict that
    made the wrapper run the deletion again reports one number rather than two.

    Returns:
        How many rows were removed, the number the run row records.
    """

    def body(cursor: Cursor) -> int:
        return delete_client_scratch(cursor, client_id)

    return record_working_purge(store.in_serializable(body, label=_PURGE_LABEL), client_id)


# ---------------------------------------------------------------------------
# Parameters and row decoding
# ---------------------------------------------------------------------------


def _reading(now: datetime | None) -> datetime:
    """The write instant both stored columns are bound from.

    A caller may inject one, which is what lets the expiry arithmetic be driven
    directly rather than waited out, and an injected reading must carry an offset
    for the same reason a stored instant must.
    """
    if now is None:
        return datetime.now(UTC)
    return require_aware(now, "a working-tier write instant")


def _bounded(limit: int) -> int:
    """The row bound to send, refusing one that is not a usable bound."""
    if limit < 1:
        raise ValueError("a read bound must admit at least one row")
    if limit > MAX_SCRATCH_LIMIT:
        raise ValueError(f"a read bound may not exceed {MAX_SCRATCH_LIMIT} rows")
    return limit


def _canonical_json(payload: JsonObject) -> str:
    """Render a scratch value in the canonical JSON form the column holds."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _scratch_of(row: Sequence[object]) -> ScratchRow:
    """Build one working row from a selected or returned row."""
    return ScratchRow(
        session_id=_as_uuid(_column(row, 0, _SCRATCH_ROW_WIDTH)),
        scratch_key=_as_str(row[1]),
        client_id=_as_uuid(row[2]),
        value=_as_object(row[3]),
        updated_at=_as_instant(row[4]),
        expires_at=_as_instant(row[5]),
    )


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a column whose type is not the one the schema declares.

    The type is named and the value is not: this tier holds whatever an agent put
    in it, and a message naming the fault belongs in a log record while the
    content does not.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_count(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a whole number")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a whole number")


def _as_instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return require_aware(value, "a selected timestamp")
    raise _unexpected(value, "a timestamp")


def _as_object(value: object) -> JsonObject:
    """Read the value column, whether the driver returns it decoded or as text."""
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise _unexpected(value, "a JSON object")
    fields: JsonObject = {}
    for key, item in decoded.items():
        if not isinstance(key, str):
            raise _unexpected(key, "a text key")
        fields[key] = item
    return fields
