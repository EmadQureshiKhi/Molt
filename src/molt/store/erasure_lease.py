"""The Erasure_Lease rows: granting, closing, renewing, surrendering, finalising.

The fence module reads the generation a lease records and guards evidence writes
with it. This module holds the statements that put those rows there and move the
three columns a lease is permitted to move. The lifecycle decisions live with the
Lease_Manager; what lives here is the data access, because no statement of this
system is composed outside this package.

Six claims arrange the statements below.

**Every instant a lease records comes from the cluster unless a caller anchors
one, and no admission decision ever reads a caller's anchor.** Whether a lease may
be taken over is `expires_at < now()`, evaluated by the cluster inside the reading
transaction, so a worker with a fast clock cannot talk itself into a takeover and a
worker with a slow one cannot deny a takeover to somebody else. What a caller may
supply is the anchor the window it is *writing* is measured from, which is what
lets an expiry be reached in a test by arithmetic rather than by waiting: a lease
written from an anchor already behind the cluster's reading is expired the moment
it exists, and the cluster is still the one that says so. The anchor defaults to
absent, and the deployed path never supplies one, so the ordinary lease is written
and judged on one clock.

**The generation is read, not computed here.** The highest generation ever
recorded for a tenant is one seek on the history index, and it is returned as a
number for the manager to increment. Keeping the arithmetic out of SQL is what
makes it assertable without a cluster, and reading it inside the granting
transaction is what makes two racing grants conflict rather than agree.

**A supersession is two ordered statements and no common table expression.** The
current lease is closed first, naming the successor's generated identifier, and the
successor is inserted second. The closing statement's assignment names the
supersession pair and nothing else, which is exactly the pair the schema's update
guard leaves writable, and the identifier it stores carries no foreign-key
reference, so the order does not have to satisfy a constraint mid-transaction.

**Surrender is expiry brought forward, not closure.** A lease's closure pair is
consistent or refused: a closed lease names the lease that replaced it. A worker
finishing cleanly has no successor to name, so releasing cannot mean closing, and
it means giving back the remainder of the window instead. The row stays current and
becomes takeable at once, and the next acquirer closes it through the ordinary
ordered supersession. A crashed worker reaches the same state without anyone
acting, on the cluster's clock, so both endings take one path rather than two.

**Finalisation is a conditional transition, and that is what makes it idempotent.**
The marking statement matches the run only while its finalisation instant is
absent, so a second marking matches no row, mutates nothing, and leaves the caller
to read the recorded outcome. The uniqueness over the run's idempotency key is what
makes that outcome one run's, and the state guard is what makes the repeat a
no-op rather than a second completion.

**Nothing here decides who may act.** These functions send statements; refusing a
superseded owner is the fence's, and refusing a contended acquisition is the
manager's. Every statement is a whole module-level literal, every caller value is
a bound parameter, no identifier is interpolated, and the finalisation result
travels as canonical JSON text with the cast written into the statement, so this
module needs no driver-side document adapter.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from molt.config.resolve import Configuration, load_configuration
from molt.errors import StoreError
from molt.models.event import JsonObject, require_aware
from molt.store import Cursor, MemoryStore
from molt.store.fencing import FIRST_GENERATION

__all__ = [
    "CLOSE_LEASE_STATEMENT",
    "CURRENT_LEASE_QUERY",
    "FINALISATION_QUERY",
    "HIGHEST_GENERATION_QUERY",
    "INSERT_LEASE_STATEMENT",
    "LEASE_INTERVAL_KEY",
    "MARK_FINALISED_STATEMENT",
    "NO_GENERATION",
    "RECORD_RUN_KEY_STATEMENT",
    "RENEW_LEASE_STATEMENT",
    "SURRENDER_LEASE_STATEMENT",
    "UNIQUE_VIOLATION_STATE",
    "FinalisationRecord",
    "LeaseInsert",
    "LeaseInterval",
    "LeaseRecord",
    "LeaseState",
    "RunOwnership",
    "close_lease",
    "extend_lease",
    "insert_lease",
    "is_unique_violation",
    "mark_finalised",
    "read_current_lease",
    "read_finalisation",
    "record_run_key",
    "select_current_lease",
    "select_finalisation",
    "select_highest_generation",
    "surrender_lease",
]

# The configuration surface key the lease interval is read from, as a count of
# seconds. The number it defaults to lives on that surface and nowhere here, so an
# operator shortening a lease changes a setting rather than this module.
LEASE_INTERVAL_KEY: Final[str] = "MOLT_LEASE_INTERVAL_SECONDS"

# What the highest-generation read reports for a tenant that has never held a
# lease. Below the first generation the schema admits, so incrementing it yields
# exactly that first generation.
NO_GENERATION: Final[int] = 0

# The state the cluster reports when a write repeats a value held unique. Read off
# the failure rather than inferred from a type, because the driver is imported
# lazily and its exception classes are not nameable here.
UNIQUE_VIOLATION_STATE: Final[str] = "23505"

# The attribute names a driver may carry the state under, matching the pair the
# transaction wrapper reads.
_STATE_ATTRIBUTES: Final[tuple[str, ...]] = ("sqlstate", "pgcode")

# How many columns each row shape carries, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_LEASE_ROW_WIDTH: Final[int] = 7
_STATE_ROW_WIDTH: Final[int] = 8
_GENERATION_ROW_WIDTH: Final[int] = 1
_OWNERSHIP_ROW_WIDTH: Final[int] = 3
_FINALISATION_ROW_WIDTH: Final[int] = 5

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The lease that is current for one tenant, which the partial uniqueness
# constraint admits at most one of, together with the cluster's own verdict on
# whether its window has run out. The verdict is computed by the cluster inside
# the reading transaction, so takeover admission never turns on a worker's clock,
# and it travels beside the row rather than being recomputed by a caller, so the
# instant it was judged at is the instant the row was read at.
CURRENT_LEASE_QUERY: Final[str] = (
    "SELECT id, client_id, owner, generation, idempotency_key, acquired_at, expires_at, "
    "expires_at < now() AS expired "
    "FROM erasure_lease WHERE client_id = %s AND superseded_at IS NULL"
)

# The highest generation ever recorded for one tenant, closed leases included, so
# a takeover generation exceeds every generation the tenant has held rather than
# every generation it holds now. The history index answers this at the first key
# it lands on. Read inside the granting transaction: two workers racing to acquire
# read the same number, and the second commit conflicts on it.
HIGHEST_GENERATION_QUERY: Final[str] = (
    "SELECT coalesce(max(generation), %s) FROM erasure_lease WHERE client_id = %s"
)

# The grant. The identifier is generated by the caller rather than defaulted,
# because the closing statement of a supersession has to name it before this
# statement runs. Both instants come from one anchor, so a lease's acquisition and
# its expiry cannot disagree about when the window opened, and the anchor defaults
# to the cluster's own reading. The interval is a bound count of seconds, so the
# configured lease length reaches the cluster as a value.
INSERT_LEASE_STATEMENT: Final[str] = (
    "INSERT INTO erasure_lease "
    "(id, client_id, owner, generation, idempotency_key, acquired_at, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, coalesce(%s::TIMESTAMPTZ, now()), "
    "coalesce(%s::TIMESTAMPTZ, now()) + %s::INT8 * INTERVAL '1 second') "
    "RETURNING id, client_id, owner, generation, idempotency_key, acquired_at, expires_at"
)

# Statement one of a supersession: close the current lease of one tenant, naming
# the successor's generated identifier. The assignment names the closure pair and
# nothing else, which is the pair the schema's update guard leaves writable, and
# the predicate restricts to a lease still current, so a closed lease cannot be
# closed twice and a history cannot be rewritten by closing one again.
CLOSE_LEASE_STATEMENT: Final[str] = (
    "UPDATE erasure_lease SET superseded_at = coalesce(%s::TIMESTAMPTZ, now()), superseded_by = %s "
    "WHERE id = %s AND superseded_at IS NULL "
    "RETURNING id, client_id, owner, generation, idempotency_key, acquired_at, expires_at"
)

# The renewal: the window is extended by the configured interval from the anchor,
# and the renewal instant is stamped from the same anchor. Only a lease still
# current is renewable, so a superseded lease cannot be revived by extending it.
# Whether the renewing worker is still the current owner is not asked here: that
# is the fence's question, and the manager asks it in this statement's own
# transaction.
RENEW_LEASE_STATEMENT: Final[str] = (
    "UPDATE erasure_lease SET "
    "expires_at = coalesce(%s::TIMESTAMPTZ, now()) + %s::INT8 * INTERVAL '1 second', "
    "renewed_at = coalesce(%s::TIMESTAMPTZ, now()) "
    "WHERE id = %s AND superseded_at IS NULL "
    "RETURNING id, client_id, owner, generation, idempotency_key, acquired_at, expires_at"
)

# The surrender: the remainder of the window is given back, so the lease becomes
# takeable at once without being closed. The floor keeps the stored window running
# forwards, which the schema requires, in the case where the anchor is at or
# before the instant the lease was acquired at.
SURRENDER_LEASE_STATEMENT: Final[str] = (
    "UPDATE erasure_lease SET expires_at = "
    "greatest(coalesce(%s::TIMESTAMPTZ, now()), acquired_at + INTERVAL '1 microsecond') "
    "WHERE id = %s AND superseded_at IS NULL "
    "RETURNING id, client_id, owner, generation, idempotency_key, acquired_at, expires_at"
)

# The run's ownership record: the key that identifies this attempt at the run, the
# lease it is performed under, and that lease's generation. The predicate admits a
# run carrying no key yet or carrying this same key, so recording it twice is the
# same recording rather than a second one, and a run already claimed by another
# attempt matches no row.
RECORD_RUN_KEY_STATEMENT: Final[str] = (
    "UPDATE erasure_run SET idempotency_key = %s, lease_id = %s, fencing_generation = %s "
    "WHERE id = %s AND client_id = %s AND (idempotency_key IS NULL OR idempotency_key = %s) "
    "RETURNING id, idempotency_key, fencing_generation"
)

# The finalisation marking, conditional on the run not being finalised already.
# That condition is the whole of the idempotency: a repeat matches no row, mutates
# nothing, and leaves the recorded outcome to be read. The generation is not
# written here, because the ownership record wrote it when the run began and a
# generation that could be restated at finalisation would name an owner other than
# the one that performed it.
MARK_FINALISED_STATEMENT: Final[str] = (
    "UPDATE erasure_run SET finalised_at = coalesce(%s::TIMESTAMPTZ, now()), "
    "finalisation_result = %s::JSONB "
    "WHERE id = %s AND client_id = %s AND idempotency_key = %s AND finalised_at IS NULL "
    "RETURNING id, idempotency_key, finalised_at, finalisation_result, fencing_generation"
)

# The recorded finalisation of one attempt, read by its key. Only a finalised run
# is reported, because the record is the outcome of a finalisation rather than the
# existence of a run, and the key is unique among the runs that carry one.
FINALISATION_QUERY: Final[str] = (
    "SELECT id, idempotency_key, finalised_at, finalisation_result, fencing_generation "
    "FROM erasure_run WHERE idempotency_key = %s AND finalised_at IS NOT NULL"
)


# ---------------------------------------------------------------------------
# The configured interval
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeaseInterval:
    """How long a granted lease holds ownership for, as the surface states it.

    Held as a count of seconds because that is the form the configuration surface
    holds and the form the statements bind. The interval carries no number of its
    own: a constant here standing in for the surface's default would be a second
    place to change, and the one an operator did not change would be the one that
    took effect.
    """

    seconds: int

    def __post_init__(self) -> None:
        """Refuse an interval that would give a lease no window at all."""
        if self.seconds <= 0:
            raise ValueError("an erasure lease interval covers a positive number of seconds")

    @classmethod
    def from_configuration(cls, configuration: Configuration | None = None) -> LeaseInterval:
        """Read the interval from the configuration surface, resolving it if needed."""
        resolved = load_configuration() if configuration is None else configuration
        return cls(seconds=resolved.integer(LEASE_INTERVAL_KEY))

    @property
    def interval(self) -> timedelta:
        """The interval as a duration, for arithmetic a caller performs in process."""
        return timedelta(seconds=self.seconds)

    def expiry_from(self, now: datetime) -> datetime:
        """The instant a lease acquired at a reading stops holding ownership.

        The cluster performs this same arithmetic when a lease is written. This
        form exists for a caller deciding whether to renew, which is a judgement
        about its own reading rather than a decision the cluster makes.
        """
        return require_aware(now, "an erasure lease anchor") + self.interval


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """One stored Erasure_Lease, as the cluster reports it.

    The same shape comes back from a grant, a renewal, a surrender, and a read,
    because each of those reports the row it left rather than an acknowledgement:
    the stored window is the fact a caller wants, and reading it off the row is the
    only way to have it rather than assume it.
    """

    lease_id: UUID
    client_id: UUID
    owner: str
    generation: int
    idempotency_key: str
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        """Refuse a reading that could not have come from a granted lease."""
        if not self.owner:
            raise ValueError("a granted lease records the owner that holds it")
        if not self.idempotency_key:
            raise ValueError("a granted lease records the key identifying its attempt")
        if self.generation < FIRST_GENERATION:
            raise ValueError(
                f"a fencing generation is at least {FIRST_GENERATION}, "
                "so nothing below it names a granted lease"
            )
        require_aware(self.acquired_at, "a lease acquisition timestamp")
        require_aware(self.expires_at, "a lease expiry timestamp")
        if self.expires_at <= self.acquired_at:
            raise ValueError("a lease window runs forwards from its acquisition to its expiry")

    def admits(self, presented: int) -> bool:
        """Whether a write presenting one generation is this lease's owner's."""
        return presented == self.generation


@dataclass(frozen=True, slots=True)
class LeaseState:
    """The current lease of one Client, with the cluster's verdict on its window.

    The verdict is carried rather than derived, because deriving it would mean
    comparing the stored expiry against a reading taken here, and a reading taken
    here is a worker's clock. What the cluster said, when it read the row, is the
    only answer takeover admission is permitted to turn on.
    """

    lease: LeaseRecord
    expired: bool

    @property
    def lease_id(self) -> UUID:
        """The lease this state describes."""
        return self.lease.lease_id

    @property
    def owner(self) -> str:
        """The worker identity holding erasure for this Client now."""
        return self.lease.owner

    @property
    def generation(self) -> int:
        """The fence every guarded write for this Client must present."""
        return self.lease.generation

    @property
    def takeable(self) -> bool:
        """Whether a requesting owner may supersede this lease.

        A lease is takeable exactly when the cluster read its expiry as already
        past. Nothing else makes it takeable: an unrenewed lease whose window is
        still open belongs to its owner however silent that owner has gone.
        """
        return self.expired


@dataclass(frozen=True, slots=True)
class LeaseInsert:
    """The values one grant writes, identifier included.

    The identifier is part of the request rather than generated by the statement,
    because the closing half of a supersession names the successor before the
    successor exists.
    """

    lease_id: UUID
    client_id: UUID
    owner: str
    generation: int
    idempotency_key: str

    def __post_init__(self) -> None:
        """Refuse a grant that names no owner, no attempt, or no ordered fence."""
        if not self.owner:
            raise ValueError("a lease grant names the owner it is granted to")
        if not self.idempotency_key:
            raise ValueError("a lease grant names the key identifying its attempt")
        if self.generation < FIRST_GENERATION:
            raise ValueError(
                f"a granted fencing generation is at least {FIRST_GENERATION}, "
                "so nothing below it can order a write"
            )


@dataclass(frozen=True, slots=True)
class RunOwnership:
    """What an Erasure_Run records about the ownership it is performed under."""

    run_id: UUID
    idempotency_key: str
    generation: int


@dataclass(frozen=True, slots=True)
class FinalisationRecord:
    """The recorded outcome of one finalisation, which a repeat returns unchanged.

    The generation is carried because a finalisation is attributable: the document
    a run produces states which owner's generation finalised it, and the record is
    where that claim is read from.
    """

    run_id: UUID
    idempotency_key: str
    finalised_at: datetime
    result: JsonObject
    generation: int | None


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


def select_current_lease(cursor: Cursor, client_id: UUID) -> LeaseState | None:
    """The lease currently held for one Client, or None where none is held.

    Sent on a caller's cursor, which matters twice over. Inside a granting
    transaction the row it matched joins that transaction's read set, so a
    concurrent grant for the same tenant conflicts with it rather than racing past
    it. And the expiry verdict it carries was computed by the cluster in that same
    transaction, so a takeover decision made from it is a decision about the
    cluster's clock.
    """
    cursor.execute(CURRENT_LEASE_QUERY, (client_id,))
    row = cursor.fetchone()
    return None if row is None else _state_of(row)


def select_highest_generation(cursor: Cursor, client_id: UUID) -> int:
    """The highest generation ever recorded for one Client, closed leases included.

    Returns:
        The historical maximum, or the no-generation floor where the Client has
        never held a lease, so that incrementing it yields the first generation.
    """
    cursor.execute(HIGHEST_GENERATION_QUERY, (NO_GENERATION, client_id))
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            "the lease history read reported no row, so the generation a grant would "
            "take cannot be established and nothing was written"
        )
    return _as_int(_column(row, 0, _GENERATION_ROW_WIDTH))


def select_finalisation(cursor: Cursor, idempotency_key: str) -> FinalisationRecord | None:
    """The recorded finalisation of one attempt, or None where none is recorded."""
    cursor.execute(FINALISATION_QUERY, (idempotency_key,))
    row = cursor.fetchone()
    return None if row is None else _finalisation_of(row)


def read_current_lease(store: MemoryStore, client_id: UUID) -> LeaseState | None:
    """Read one Client's current lease on a leased connection, framing no transaction."""

    def body(cursor: Cursor) -> LeaseState | None:
        return select_current_lease(cursor, client_id)

    return store.read(body)


def read_finalisation(store: MemoryStore, idempotency_key: str) -> FinalisationRecord | None:
    """Read one attempt's recorded finalisation on a leased connection."""

    def body(cursor: Cursor) -> FinalisationRecord | None:
        return select_finalisation(cursor, idempotency_key)

    return store.read(body)


# ---------------------------------------------------------------------------
# The writes
# ---------------------------------------------------------------------------


def insert_lease(
    cursor: Cursor,
    request: LeaseInsert,
    *,
    interval: LeaseInterval,
    now: datetime | None = None,
) -> LeaseRecord:
    """Write one lease on a caller's cursor and report the row the cluster holds.

    Args:
        cursor: The cursor the caller's transaction is running on.
        request: The ownership to record, its generated identifier included.
        interval: How long the window runs for, as the surface states it.
        now: The anchor both stored instants are measured from, or None to leave
            them to the cluster's own reading. No admission decision reads this.

    Returns:
        The stored lease, its window as the cluster computed it.

    Raises:
        StoreError: The write reported no row, so no ownership is known to stand.
    """
    anchor = _anchor(now)
    cursor.execute(
        INSERT_LEASE_STATEMENT,
        (
            request.lease_id,
            request.client_id,
            request.owner,
            request.generation,
            request.idempotency_key,
            anchor,
            anchor,
            interval.seconds,
        ),
    )
    return _required(cursor, "the lease grant")


def close_lease(
    cursor: Cursor,
    lease_id: UUID,
    successor_id: UUID,
    *,
    now: datetime | None = None,
) -> LeaseRecord | None:
    """Close one current lease, naming the successor that replaces it.

    Statement one of a supersession. It runs before the successor's insert, and
    the identifier it stores carries no foreign-key reference, so the successor
    need not exist yet.

    Returns:
        The lease that was closed, or None where the lease named is no longer
        current and so had nothing to close.
    """
    cursor.execute(CLOSE_LEASE_STATEMENT, (_anchor(now), successor_id, lease_id))
    row = cursor.fetchone()
    return None if row is None else _lease_of(row)


def extend_lease(
    cursor: Cursor,
    lease_id: UUID,
    *,
    interval: LeaseInterval,
    now: datetime | None = None,
) -> LeaseRecord | None:
    """Extend one current lease's window by the interval and stamp the renewal.

    Returns:
        The lease with its new window, or None where the lease named is no longer
        current and so cannot be renewed.
    """
    anchor = _anchor(now)
    cursor.execute(RENEW_LEASE_STATEMENT, (anchor, interval.seconds, anchor, lease_id))
    row = cursor.fetchone()
    return None if row is None else _lease_of(row)


def surrender_lease(
    cursor: Cursor,
    lease_id: UUID,
    *,
    now: datetime | None = None,
) -> LeaseRecord | None:
    """Give back the remainder of one current lease's window, leaving it takeable.

    The lease is not closed, because closure names a successor and a worker
    finishing cleanly has none. It becomes takeable at once instead, which is the
    same state an unrenewed lease reaches on its own.

    Returns:
        The lease with its window ended, or None where the lease named is no
        longer current and so has no window to give back.
    """
    cursor.execute(SURRENDER_LEASE_STATEMENT, (_anchor(now), lease_id))
    row = cursor.fetchone()
    return None if row is None else _lease_of(row)


def record_run_key(
    cursor: Cursor,
    run_id: UUID,
    client_id: UUID,
    *,
    idempotency_key: str,
    lease_id: UUID,
    generation: int,
) -> RunOwnership | None:
    """Record one run's idempotency key and the ownership it is performed under.

    Recording the same key again is the same recording rather than a second one,
    so a retried start reports what already stands.

    Returns:
        What the run now records, or None where the run named is another Client's
        or already carries a different attempt's key.
    """
    cursor.execute(
        RECORD_RUN_KEY_STATEMENT,
        (idempotency_key, lease_id, generation, run_id, client_id, idempotency_key),
    )
    row = cursor.fetchone()
    return None if row is None else _ownership_of(row)


def mark_finalised(
    cursor: Cursor,
    run_id: UUID,
    client_id: UUID,
    *,
    idempotency_key: str,
    result: JsonObject,
    now: datetime | None = None,
) -> FinalisationRecord | None:
    """Mark one run finalised and store the outcome, only if it is not already.

    Returns:
        The recorded finalisation this statement wrote, or None where the run was
        finalised already, names another Client, or carries another attempt's key.
        A None answer means the statement mutated nothing.
    """
    cursor.execute(
        MARK_FINALISED_STATEMENT,
        (_anchor(now), _canonical_json(result), run_id, client_id, idempotency_key),
    )
    row = cursor.fetchone()
    return None if row is None else _finalisation_of(row)


# ---------------------------------------------------------------------------
# Failure states
# ---------------------------------------------------------------------------


def is_unique_violation(error: BaseException) -> bool:
    """Whether a failure is the cluster refusing a value it holds unique.

    The state is read off the failure rather than inferred from its type, because
    the driver is imported lazily and its exception classes are not nameable here.
    A failure carrying no state at all is not a uniqueness refusal, which is the
    safe reading: an unrecognised failure is reported as itself.
    """
    for attribute in _STATE_ATTRIBUTES:
        state = getattr(error, attribute, None)
        if isinstance(state, str) and state == UNIQUE_VIOLATION_STATE:
            return True
    return False


# ---------------------------------------------------------------------------
# Parameters and row decoding
# ---------------------------------------------------------------------------


def _anchor(now: datetime | None) -> datetime | None:
    """The anchor to bind, refusing one that carries no offset.

    Absent is the ordinary case and the deployed one: the statement then measures
    the window from the cluster's own reading. A supplied anchor must carry an
    offset for the same reason a stored instant must, and it is checked here so a
    naive reading is refused before it reaches a column.
    """
    if now is None:
        return None
    return require_aware(now, "an erasure lease anchor")


def _canonical_json(payload: JsonObject) -> str:
    """Render a finalisation outcome in the canonical JSON form the column holds."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _required(cursor: Cursor, what: str) -> LeaseRecord:
    """The lease a write reported, refusing a write that reported none."""
    row = cursor.fetchone()
    if row is None:
        raise StoreError(f"{what} reported no row, so no lease is known to be recorded")
    return _lease_of(row)


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _lease_of(row: Sequence[object]) -> LeaseRecord:
    """Build one stored lease from a selected or returned row."""
    return LeaseRecord(
        lease_id=_as_uuid(_column(row, 0, _LEASE_ROW_WIDTH)),
        client_id=_as_uuid(row[1]),
        owner=_as_str(row[2]),
        generation=_as_int(row[3]),
        idempotency_key=_as_str(row[4]),
        acquired_at=_as_instant(row[5]),
        expires_at=_as_instant(row[6]),
    )


def _state_of(row: Sequence[object]) -> LeaseState:
    """Build one current-lease reading, the cluster's expiry verdict included."""
    _column(row, 0, _STATE_ROW_WIDTH)
    return LeaseState(lease=_lease_of(tuple(row[:_LEASE_ROW_WIDTH])), expired=_as_bool(row[7]))


def _ownership_of(row: Sequence[object]) -> RunOwnership:
    """Build one run's ownership record from a returned row."""
    return RunOwnership(
        run_id=_as_uuid(_column(row, 0, _OWNERSHIP_ROW_WIDTH)),
        idempotency_key=_as_str(row[1]),
        generation=_as_int(row[2]),
    )


def _finalisation_of(row: Sequence[object]) -> FinalisationRecord:
    """Build one recorded finalisation from a selected or returned row."""
    generation = row[4]
    return FinalisationRecord(
        run_id=_as_uuid(_column(row, 0, _FINALISATION_ROW_WIDTH)),
        idempotency_key=_as_str(row[1]),
        finalised_at=_as_instant(row[2]),
        result=_as_object(row[3]),
        generation=None if generation is None else _as_int(generation),
    )


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a column whose type is not the one the schema declares.

    The type is named and the value is not, for the reason every decoder in this
    package names one: a message belongs in a log record and stored content does
    not.
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


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a whole number")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a whole number")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise _unexpected(value, "a truth value")


def _as_instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return require_aware(value, "a selected timestamp")
    raise _unexpected(value, "a timestamp")


def _as_object(value: object) -> JsonObject:
    """Read the outcome column, whether the driver returns it decoded or as text."""
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise _unexpected(value, "a JSON object")
    fields: JsonObject = {}
    for key, item in decoded.items():
        if not isinstance(key, str):
            raise _unexpected(key, "a text key")
        fields[key] = item
    return fields
