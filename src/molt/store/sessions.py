"""Session writes and the tenancy-scoped reads over Sessions, Events, and Artifacts.

Four claims of the design are load-bearing here, and each is arranged so that a
caller cannot get it wrong by forgetting something.

**Nesting depth is derived from the parent row, never trusted from the caller.**
The schema carries half of the invariant: a Session naming no parent sits at depth
zero, and that half is a check constraint. The other half, that a Session naming a
parent sits one below it, is carried by the inserting statement, which reads the
parent's depth in the same statement that writes the child. A caller presenting a
depth of its own does not decide anything; the value it presented is not even
sent. The insert is written as a `SELECT` over a single-row anchor left-joined to
the parent, rather than as a `SELECT` from the parent table alone, for a reason
worth stating: a join that produced no row for an absent parent would insert
nothing at all and report success. The anchor makes the row exist regardless, so
depth is derived for a root Session and for a child alike.

**A Session naming a row that does not exist is refused by this module, not by a
reference.** The refusal used to be the cluster's: a parent identifier no Session
held reached the foreign key on the parent column, and a spawning identifier no
Ledger row held reached the one on the spawning column. Both of those references
are references an authorised erasure has to be able to cut — a parent Session and
a child Session leave together, and an Event and the Session it was recorded in
leave together, so each pair is a cycle no delete order satisfies — and migration
017 drops them for that reason. The invariant is worth keeping independently of
the reference, because a Session placed under a parent nobody holds records a
lineage position that is not true of the stored graph, and with the depth
derivation being total it would land at depth zero rather than being refused. So
the inserting statement carries the check itself: its guard requires the named
parent row and the named spawning row to have been found, and a named row that
was not found leaves the statement inserting nothing, which this module reports as
a missing parent. The check costs no extra round trip and it sits inside the
transaction that writes the row, so it cannot race a concurrent delete.

**Counters move by increment, never by read-modify-write.** Every counter
statement here is a single `UPDATE` whose right-hand side names the column it
assigns, so two concurrent writers each add their own delta and neither observes
the other's value in application memory. There is no path through this module by
which a counter is read, adjusted, and written back, which is the difference the
concurrency suite demonstrates against an implementation that does exactly that.
The terminal path takes the same care in a different way: a terminal counter is
applied as the greater of the stored value and the presented one, so a final
total restated by a closing record can raise a counter and can never undo an
increment a concurrent bump already recorded.

**Every read is scoped by Client.** Each read statement carries the tenant
identifier in its predicate, for Sessions, for Events, and for Derived_Artifacts
alike. There is no accessor here that reads a row by identifier alone, so a
caller cannot reach another tenant's row by holding a Session identifier.

**A spawning Event is inserted before the Session it spawned.** Nothing in this
design defers a check to commit: the cluster implements no deferred constraint
checking, and the guard described above is evaluated by the inserting statement
itself. A transaction writing both therefore has exactly one admissible order, and
the helper that composes the two takes the Event insert as a callable it invokes
first and binds the identifier that came back into the Session insert. The
ordering is structural rather than documented: there is no way to call it that
writes the Session first.

The statements are whole module-level literals. Every caller-supplied value is a
bound parameter, no identifier is ever interpolated, and the attribution mapping
is bound as canonical JSON text with the cast written in the statement, so the
module needs no driver-specific adapter and stays importable with no driver
installed.

What each statement writes is confined to what the schema's update guard permits
the connecting role to write. Tenancy and lineage columns appear in the insert
alone and in no update, the counter and terminal columns belong to the writer
role's own paths, and the halt columns are not touched here at all.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from molt.errors import MissingParentError, StoreError
from molt.models.artifact import DerivedArtifactKind
from molt.models.event import EmbeddingState, EventCategory, JsonObject, require_aware
from molt.models.session import Session, SessionOutcome
from molt.store import Cursor, MemoryStore
from molt.telemetry import Severity, log

__all__ = [
    "BUMP_COUNTERS_FOR_CLIENT_STATEMENT",
    "BUMP_COUNTERS_STATEMENT",
    "COMPONENT",
    "DEFAULT_READ_LIMIT",
    "END_SESSION_STATEMENT",
    "END_SESSION_WITH_COUNTERS_STATEMENT",
    "FOREIGN_KEY_VIOLATION_STATE",
    "INSERT_SESSION_STATEMENT",
    "MAX_READ_LIMIT",
    "PARENT_REFERENCE_CONSTRAINT",
    "SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT",
    "SELECT_CHILD_SESSIONS_STATEMENT",
    "SELECT_EVENTS_FOR_SESSION_STATEMENT",
    "SELECT_SESSIONS_FOR_CLIENT_STATEMENT",
    "SELECT_SESSION_STATEMENT",
    "SPAWNING_REFERENCE_CONSTRAINT",
    "ArtifactSummary",
    "CounterDelta",
    "EventSummary",
    "SessionCounters",
    "artifacts_of_client",
    "bump_session_counters",
    "child_sessions",
    "end_session",
    "events_of_session",
    "insert_session_in_transaction",
    "insert_spawned_session",
    "session_of_client",
    "sessions_of_client",
    "upsert_session",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The state the cluster reports when a write names a row that does not exist in
# a referenced table. It is read off the failure rather than inferred from a
# type, because the driver is imported lazily and its exception classes are
# therefore not nameable here.
FOREIGN_KEY_VIOLATION_STATE: Final[str] = "23503"

# The attribute names a driver may carry the state under, matching the pair the
# transaction wrapper reads.
_STATE_ATTRIBUTES: Final[tuple[str, ...]] = ("sqlstate", "pgcode")

# The two references a Session insert can violate. The parent reference is the
# one the depth derivation depends on; the spawning reference is the one the
# insert ordering of a spawned Session exists to satisfy.
PARENT_REFERENCE_CONSTRAINT: Final[str] = "session_parent_session_id_fkey"
SPAWNING_REFERENCE_CONSTRAINT: Final[str] = "session_spawning_event_fk"

# How many rows a read returns when a caller names no bound, and the ceiling a
# caller may not ask past. The bound is a parameter of every read statement, so a
# caller cannot ask for an unbounded scan of a tenant's history.
DEFAULT_READ_LIMIT: Final[int] = 100
MAX_READ_LIMIT: Final[int] = 10000

# How many columns each row shape carries, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_SESSION_ROW_WIDTH: Final[int] = 22
_COUNTER_ROW_WIDTH: Final[int] = 5
_EVENT_ROW_WIDTH: Final[int] = 11
_ARTIFACT_ROW_WIDTH: Final[int] = 8

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The Session insert. Depth is the one column whose value the caller does not
# supply: it is `coalesce(parent.depth + 1, 0)`, read from the parent row in the
# same statement. The anchor row is what makes the derivation total. With no
# parent named the join matches nothing and the expression yields zero, which is
# the root case the schema's own check also states.
#
# The two guards in the `WHERE` are the absent-parent refusal, and they are what
# the dropped references used to do. Each reads: either this Session named no such
# row, or the join for it found one. A Session naming a parent or a spawning Event
# that does not exist therefore selects nothing, inserts nothing, and returns
# nothing, and the caller below turns that empty result into a named failure. Both
# lookups are joins of this one statement, so the check is inside the inserting
# transaction and costs no round trip of its own.
#
# The guard for the spawning reference is written before the guard for the parent
# reference so that the parent identifier remains the last bound value of the
# statement, which is where the depth derivation's own join reads it from.
#
# The conflict path restates neither tenancy nor lineage nor counters. It closes
# a Session and nothing else, and it closes it monotonically: an end timestamp is
# never cleared and an outcome already terminal is never reopened, so a repeated
# metadata write is idempotent rather than destructive.
INSERT_SESSION_STATEMENT: Final[str] = (
    "INSERT INTO session AS target ("
    "id, client_id, agent_cli, machine_id, team_id, attribution, workspace_path, "
    "started_at, ended_at, outcome, parent_session_id, spawning_event_id, depth, "
    "tool_call_count, model_request_count, error_count, token_count, cost_usd) "
    "SELECT %s, %s, %s, %s, %s, %s::JSONB, %s, "
    "%s, %s, %s, %s, %s, coalesce(parent.depth + 1, 0), "
    "%s, %s, %s, %s, %s "
    "FROM (VALUES (1)) AS anchor (present) "
    "LEFT JOIN ledger AS spawning ON spawning.id = %s "
    "LEFT JOIN session AS parent ON parent.id = %s "
    "WHERE (%s::UUID IS NULL OR spawning.id IS NOT NULL) "
    "AND (%s::UUID IS NULL OR parent.id IS NOT NULL) "
    "ON CONFLICT (id) DO UPDATE SET "
    "ended_at = coalesce(excluded.ended_at, target.ended_at), "
    "outcome = CASE WHEN excluded.outcome = 'in_progress' "
    "THEN target.outcome ELSE excluded.outcome END "
    "RETURNING depth"
)

# The counter increment, in the one form that loses nothing: each assignment
# names the column it is assigning, so the cluster performs the addition and no
# value passes through application memory on its way back. The read-back is part
# of the same statement rather than a following select, so a caller learns the
# totals its own increment produced.
BUMP_COUNTERS_STATEMENT: Final[str] = (
    "UPDATE session SET "
    "tool_call_count = tool_call_count + %s, "
    "model_request_count = model_request_count + %s, "
    "error_count = error_count + %s, "
    "token_count = token_count + %s, "
    "cost_usd = cost_usd + %s "
    "WHERE id = %s "
    "RETURNING tool_call_count, model_request_count, error_count, token_count, cost_usd"
)

# The same increment, scoped by tenant. A caller that knows which Client it is
# writing for uses this one, so a Session identifier belonging to another tenant
# matches nothing rather than moving that tenant's counters.
BUMP_COUNTERS_FOR_CLIENT_STATEMENT: Final[str] = (
    "UPDATE session SET "
    "tool_call_count = tool_call_count + %s, "
    "model_request_count = model_request_count + %s, "
    "error_count = error_count + %s, "
    "token_count = token_count + %s, "
    "cost_usd = cost_usd + %s "
    "WHERE id = %s AND client_id = %s "
    "RETURNING tool_call_count, model_request_count, error_count, token_count, cost_usd"
)

# The terminal write: an outcome and an end timestamp, and nothing else. This is
# the form available to every role the guard permits a terminal write to, which
# is the capture path and the erasure path both.
END_SESSION_STATEMENT: Final[str] = (
    "UPDATE session SET ended_at = %s, outcome = %s "
    "WHERE id = %s AND client_id = %s "
    "RETURNING tool_call_count, model_request_count, error_count, token_count, cost_usd"
)

# The terminal write carrying the final totals a closing record states. Each
# counter is raised to the greater of what is stored and what is presented, so a
# total computed before a concurrent increment landed cannot roll that increment
# back. Counters are writable by the capture path alone, so this form is the
# capture path's.
END_SESSION_WITH_COUNTERS_STATEMENT: Final[str] = (
    "UPDATE session SET ended_at = %s, outcome = %s, "
    "tool_call_count = greatest(tool_call_count, %s), "
    "model_request_count = greatest(model_request_count, %s), "
    "error_count = greatest(error_count, %s), "
    "token_count = greatest(token_count, %s), "
    "cost_usd = greatest(cost_usd, %s) "
    "WHERE id = %s AND client_id = %s "
    "RETURNING tool_call_count, model_request_count, error_count, token_count, cost_usd"
)

# The Session reads. Every one of them names the tenant, and the single-row read
# names it alongside the identifier rather than instead of it.
SELECT_SESSION_STATEMENT: Final[str] = (
    "SELECT id, client_id, agent_cli, machine_id, team_id, attribution, workspace_path, "
    "started_at, ended_at, outcome, parent_session_id, spawning_event_id, depth, "
    "tool_call_count, model_request_count, error_count, token_count, cost_usd, "
    "halted, halted_at, halt_reason, halt_rule_id "
    "FROM session WHERE id = %s AND client_id = %s"
)

SELECT_SESSIONS_FOR_CLIENT_STATEMENT: Final[str] = (
    "SELECT id, client_id, agent_cli, machine_id, team_id, attribution, workspace_path, "
    "started_at, ended_at, outcome, parent_session_id, spawning_event_id, depth, "
    "tool_call_count, model_request_count, error_count, token_count, cost_usd, "
    "halted, halted_at, halt_reason, halt_rule_id "
    "FROM session WHERE client_id = %s ORDER BY started_at DESC, id ASC LIMIT %s"
)

SELECT_CHILD_SESSIONS_STATEMENT: Final[str] = (
    "SELECT id, client_id, agent_cli, machine_id, team_id, attribution, workspace_path, "
    "started_at, ended_at, outcome, parent_session_id, spawning_event_id, depth, "
    "tool_call_count, model_request_count, error_count, token_count, cost_usd, "
    "halted, halted_at, halt_reason, halt_rule_id "
    "FROM session WHERE client_id = %s AND parent_session_id = %s "
    "ORDER BY started_at ASC, id ASC LIMIT %s"
)

# The Event read, ordered by the sequence the chain is a line in. It carries the
# digests and the linkage rather than the payload: this is the tenancy-scoped
# index over a Session's stream, and the content-carrying reads belong to the
# modules that own the chain and the recall path.
SELECT_EVENTS_FOR_SESSION_STATEMENT: Final[str] = (
    "SELECT id, session_id, client_id, seq, category, occurred_at, recorded_at, "
    "parent_event_id, redacted, content_digest, chain_digest "
    "FROM ledger WHERE session_id = %s AND client_id = %s ORDER BY seq ASC LIMIT %s"
)

# The Derived_Artifact read. The tenant column is named `owner_client_id` on that
# table, and it is the same scoping obligation under a different column name.
SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT: Final[str] = (
    "SELECT id, kind, owner_client_id, content_digest, derivation_method, revision, "
    "created_at, embedding_state "
    "FROM derived_artifact WHERE owner_client_id = %s "
    "ORDER BY created_at DESC, id ASC LIMIT %s"
)

# The labels the transactions of this module appear under in a log record and in
# the note an exhausted retry attaches.
_UPSERT_LABEL: Final[str] = "session_upsert"
_SPAWN_LABEL: Final[str] = "session_spawn"
_BUMP_LABEL: Final[str] = "session_counters"
_END_LABEL: Final[str] = "session_end"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CounterDelta:
    """How far each Session counter moves in one increment.

    Every field is a non-negative addition, because the counters are running
    counts of things that happened and nothing unhappens. A negative delta would
    also make the increment form indistinguishable in effect from a correction,
    and a correction to an append-only count is not something any requirement
    asks for.
    """

    tool_calls: int = 0
    model_requests: int = 0
    errors: int = 0
    tokens: int = 0
    cost_usd: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for label, count in (
            ("tool call", self.tool_calls),
            ("model request", self.model_requests),
            ("error", self.errors),
            ("token", self.tokens),
        ):
            if count < 0:
                raise ValueError(f"a {label} counter increment cannot be negative")
        if self.cost_usd < Decimal(0):
            raise ValueError("a cost increment cannot be negative")

    @property
    def is_empty(self) -> bool:
        """Whether this delta would move no counter at all."""
        return (
            self.tool_calls == 0
            and self.model_requests == 0
            and self.errors == 0
            and self.tokens == 0
            and self.cost_usd == Decimal(0)
        )


@dataclass(frozen=True, slots=True)
class SessionCounters:
    """The five running counts a Session carries, as the cluster holds them."""

    tool_call_count: int
    model_request_count: int
    error_count: int
    token_count: int
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class EventSummary:
    """One Event of a Session, as the tenancy-scoped stream read returns it."""

    id: UUID
    session_id: UUID
    client_id: UUID
    seq: int
    category: EventCategory
    occurred_at: datetime
    recorded_at: datetime
    parent_event_id: UUID | None
    redacted: bool
    content_digest: str
    chain_digest: str


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    """One Derived_Artifact, as the tenancy-scoped Artifact read returns it."""

    id: UUID
    kind: DerivedArtifactKind
    client_id: UUID
    content_digest: str
    derivation_method: str
    revision: int
    created_at: datetime
    embedding_state: EmbeddingState


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def upsert_session(store: MemoryStore, record: Session) -> int:
    """Write a Session, deriving its nesting depth from its parent row.

    The depth the record carries is not sent. What is written is the depth the
    cluster computes from the parent row inside the inserting statement, and that
    value is what comes back.

    Args:
        store: The connection surface the transaction is framed by.
        record: The Session to write. Its identifier decides whether this is a
            first write or a restatement.

    Returns:
        The nesting depth the row now holds: the parent's depth plus one, or zero
        for a Session with no parent, or the depth already stored when the write
        restated an existing Session.

    Raises:
        MissingParentError: The record names a parent Session or a spawning Event
            that does not exist. Nothing was written.
        StoreError: The statement produced no row, which would mean the insert
            neither inserted nor resolved a conflict.
    """

    def body(cursor: Cursor) -> int:
        return insert_session_in_transaction(
            cursor, record, spawning_event_id=record.spawning_event_id
        )

    return _with_reference_check(lambda: store.in_serializable(body, label=_UPSERT_LABEL), record)


def insert_spawned_session(
    store: MemoryStore,
    record: Session,
    *,
    append_spawning_event: Callable[[Cursor], UUID],
) -> int:
    """Write a spawning Event and the child Session it spawned, in that order.

    The Event insert is a callable this function invokes first, and the
    identifier it returns is what the Session's spawning reference is bound to.
    The order is not a convention a caller could get wrong: the cluster checks
    each foreign key per statement and supports no deferred checking, so a
    Session inserted before its spawning Event would be refused, and there is no
    way to call this that produces that order.

    Args:
        store: The connection surface the one transaction is framed by.
        record: The child Session. Any spawning reference it carries is replaced
            by the identifier the Event insert reports.
        append_spawning_event: Inserts the spawning Event on the transaction's
            own cursor and returns its identifier. It runs inside the
            transaction, so a conflict runs it again from the beginning.

    Returns:
        The nesting depth the child Session now holds, derived from its parent.

    Raises:
        MissingParentError: The record names a parent Session that does not
            exist, or the Event insert reported an identifier no Ledger row
            holds. Nothing was written.
    """

    def body(cursor: Cursor) -> int:
        spawning_event_id = append_spawning_event(cursor)
        return insert_session_in_transaction(cursor, record, spawning_event_id=spawning_event_id)

    return _with_reference_check(lambda: store.in_serializable(body, label=_SPAWN_LABEL), record)


def insert_session_in_transaction(
    cursor: Cursor,
    record: Session,
    *,
    spawning_event_id: UUID | None,
) -> int:
    """Send the one Session insert on the caller's cursor and report the derived depth.

    This is the entry point for a caller that must write a Session inside a
    transaction of its own, which is what the two functions above are, and what
    the ingest path is when it creates an absent Session in the transaction that
    writes the Events that named it (Requirement 5.7). Such a caller may write
    unconditionally rather than reading first, because the conflict path of the
    statement closes a Session and nothing else: restating an open Session leaves
    tenancy, lineage, and every counter exactly as they were, so an absent Session
    is created and a present one is untouched.

    The absent-parent refusal is this statement's own, and that is a deliberate
    move rather than a duplication. It used to be the cluster's: a parent
    identifier no Session held and a spawning identifier no Ledger row held each
    reached a foreign key and were refused there. Those two references are ones an
    authorised erasure must be able to cut, because a parent Session leaves with
    its children and an Event leaves with the Session it was recorded in, so each
    pair is a cycle no delete order satisfies; migration 017 drops them for that
    reason. The invariant they carried is not theirs to take with them, so the
    statement's own guards require a named parent row and a named spawning row to
    have been found, and a named row that was not found leaves the statement
    selecting nothing. That empty result is what becomes `MissingParentError`
    here. The check is two joins of the inserting statement, so it adds no round
    trip and cannot race a delete that commits between a probe and an insert.

    A driver-reported reference violation is still left untranslated here, because
    that translation names the record that caused it and belongs to whichever
    transaction frames the write; the two functions above frame their own and
    translate. Those paths remain live: the first migration generation still
    declares the two references, so a schema at that generation refuses the write
    before the guard is ever consulted.

    The record's own depth is compared against the derived value afterwards
    rather than before: the comparison is a report, not a gate, because the
    derived value is authoritative either way. A disagreement is worth a record
    because it means a caller computed a lineage position that the stored graph
    does not agree with.
    """
    require_aware(record.started_at, "a Session start timestamp")
    cursor.execute(
        INSERT_SESSION_STATEMENT,
        (
            record.id,
            record.client_id,
            record.agent_cli,
            record.machine_id,
            record.team_id,
            _canonical_json(record.attribution),
            record.workspace_path,
            record.started_at,
            record.ended_at,
            SessionOutcome(record.outcome).value,
            record.parent_session_id,
            spawning_event_id,
            record.tool_call_count,
            record.model_request_count,
            record.error_count,
            record.token_count,
            record.cost_usd,
            spawning_event_id,
            record.parent_session_id,
            spawning_event_id,
            record.parent_session_id,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise _nothing_written(record, spawning_event_id)
    derived = _as_int(_column(row, 0, 1))
    if derived != record.depth:
        log(
            Severity.DEBUG,
            COMPONENT,
            "the stored Session depth was derived from the parent row rather than presented",
            session_id=str(record.id),
            presented_depth=record.depth,
            stored_depth=derived,
        )
    return derived


def bump_session_counters(
    store: MemoryStore,
    session_id: UUID,
    delta: CounterDelta,
    *,
    client_id: UUID | None = None,
) -> SessionCounters | None:
    """Add one delta to a Session's counters in a single statement.

    Nothing is read and written back. The statement names each counter column on
    both sides of its own assignment, so two writers incrementing at the same
    instant both land, and the totals that come back are the totals after this
    increment rather than before it.

    Args:
        store: The connection surface the transaction is framed by.
        session_id: The Session whose counters move.
        delta: How far each counter moves. A delta moving nothing is refused,
            because an update that changes no column is a round trip that says
            nothing.
        client_id: The tenant to scope the write by. Naming it is preferred: a
            Session identifier belonging to another tenant then matches no row
            instead of moving that tenant's counters.

    Returns:
        The counters the Session holds after the increment, or None when no row
        matched the identifier and the tenant scope.
    """
    if delta.is_empty:
        raise ValueError("a counter increment must move at least one counter")

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        if client_id is None:
            cursor.execute(
                BUMP_COUNTERS_STATEMENT,
                (
                    delta.tool_calls,
                    delta.model_requests,
                    delta.errors,
                    delta.tokens,
                    delta.cost_usd,
                    session_id,
                ),
            )
        else:
            cursor.execute(
                BUMP_COUNTERS_FOR_CLIENT_STATEMENT,
                (
                    delta.tool_calls,
                    delta.model_requests,
                    delta.errors,
                    delta.tokens,
                    delta.cost_usd,
                    session_id,
                    client_id,
                ),
            )
        return cursor.fetchone()

    row = store.in_serializable(body, label=_BUMP_LABEL)
    return None if row is None else _counters_of(row)


def end_session(
    store: MemoryStore,
    session_id: UUID,
    client_id: UUID,
    *,
    outcome: SessionOutcome,
    ended_at: datetime,
    counters: SessionCounters | None = None,
) -> SessionCounters | None:
    """Close a Session, writing its outcome, its end timestamp, and final totals.

    The terminal counters are optional and are applied as the greater of the
    stored value and the presented one. That is what keeps this path consistent
    with the increment path: a closing record computes its totals at some instant
    before it is written, and an increment that lands in between must not be
    undone by it.

    Args:
        store: The connection surface the transaction is framed by.
        session_id: The Session to close.
        client_id: The tenant the Session belongs to. The write is scoped by it.
        outcome: How the Session ended. The open outcome is refused, because
            closing a Session to it would record nothing.
        ended_at: When the Session ended, timezone aware.
        counters: The final totals the closing record states, or None to leave
            the counters exactly as the increments left them.

    Returns:
        The counters the Session holds after the write, or None when no row
        matched the identifier and the tenant scope.
    """
    chosen = SessionOutcome(outcome)
    if chosen is SessionOutcome.IN_PROGRESS:
        raise ValueError("closing a Session requires a terminal outcome")
    require_aware(ended_at, "a Session end timestamp")

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        if counters is None:
            cursor.execute(
                END_SESSION_STATEMENT,
                (ended_at, chosen.value, session_id, client_id),
            )
        else:
            cursor.execute(
                END_SESSION_WITH_COUNTERS_STATEMENT,
                (
                    ended_at,
                    chosen.value,
                    counters.tool_call_count,
                    counters.model_request_count,
                    counters.error_count,
                    counters.token_count,
                    counters.cost_usd,
                    session_id,
                    client_id,
                ),
            )
        return cursor.fetchone()

    row = store.in_serializable(body, label=_END_LABEL)
    return None if row is None else _counters_of(row)


# ---------------------------------------------------------------------------
# Reads, every one of them scoped by Client
# ---------------------------------------------------------------------------


def session_of_client(store: MemoryStore, session_id: UUID, client_id: UUID) -> Session | None:
    """Read one Session, by identifier and by the tenant it must belong to.

    There is deliberately no accessor that reads a Session by identifier alone:
    holding an identifier is not authority over the row it names.
    """

    def body(cursor: Cursor) -> Session | None:
        cursor.execute(SELECT_SESSION_STATEMENT, (session_id, client_id))
        row = cursor.fetchone()
        return None if row is None else _session_of(row)

    return store.read(body)


def sessions_of_client(
    store: MemoryStore,
    client_id: UUID,
    *,
    limit: int = DEFAULT_READ_LIMIT,
) -> tuple[Session, ...]:
    """Read a tenant's Sessions, most recently started first."""
    bound = _bounded(limit)

    def body(cursor: Cursor) -> tuple[Session, ...]:
        cursor.execute(SELECT_SESSIONS_FOR_CLIENT_STATEMENT, (client_id, bound))
        return tuple(_session_of(row) for row in cursor.fetchall())

    return store.read(body)


def child_sessions(
    store: MemoryStore,
    parent_session_id: UUID,
    client_id: UUID,
    *,
    limit: int = DEFAULT_READ_LIMIT,
) -> tuple[Session, ...]:
    """Read the Sessions spawned by one Session, in start order.

    The tenant scope is not redundant with the parent: it is what makes the read
    answerable without first trusting that the parent identifier the caller holds
    belongs to the tenant it claims.
    """
    bound = _bounded(limit)

    def body(cursor: Cursor) -> tuple[Session, ...]:
        cursor.execute(SELECT_CHILD_SESSIONS_STATEMENT, (client_id, parent_session_id, bound))
        return tuple(_session_of(row) for row in cursor.fetchall())

    return store.read(body)


def events_of_session(
    store: MemoryStore,
    session_id: UUID,
    client_id: UUID,
    *,
    limit: int = DEFAULT_READ_LIMIT,
) -> tuple[EventSummary, ...]:
    """Read a Session's Events in sequence order, scoped by tenant."""
    bound = _bounded(limit)

    def body(cursor: Cursor) -> tuple[EventSummary, ...]:
        cursor.execute(SELECT_EVENTS_FOR_SESSION_STATEMENT, (session_id, client_id, bound))
        return tuple(_event_of(row) for row in cursor.fetchall())

    return store.read(body)


def artifacts_of_client(
    store: MemoryStore,
    client_id: UUID,
    *,
    limit: int = DEFAULT_READ_LIMIT,
) -> tuple[ArtifactSummary, ...]:
    """Read a tenant's Derived_Artifacts, most recently created first."""
    bound = _bounded(limit)

    def body(cursor: Cursor) -> tuple[ArtifactSummary, ...]:
        cursor.execute(SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT, (client_id, bound))
        return tuple(_artifact_of(row) for row in cursor.fetchall())

    return store.read(body)


# ---------------------------------------------------------------------------
# Reference failures
# ---------------------------------------------------------------------------


def _with_reference_check(work: Callable[[], int], record: Session) -> int:
    """Run a Session write, reporting an absent referenced row as such.

    A failure that is not a reference violation propagates untouched, so a
    conflict still reaches the retry wrapper's own handling and a constraint
    failure is not renamed into something it is not.
    """
    try:
        return work()
    except Exception as error:
        translated = _missing_reference(error, record)
        if translated is None:
            raise
        raise translated from error


def _nothing_written(
    record: Session, spawning_event_id: UUID | None
) -> MissingParentError | StoreError:
    """The failure for an insert that reported no row.

    A Session naming neither a parent nor a spawning Event cannot be filtered by
    the statement's guards, because the anchor row makes the selection total for
    it. An empty result there means the insert neither inserted nor resolved a
    conflict, which is not a lineage fault and is not reported as one.

    Otherwise the record named a row and the join for it found none, which is the
    absent-parent refusal. Which of the two references it was is read off the
    record rather than asked of the cluster: a Session naming only one of them
    names the unsatisfied reference by naming it at all, and a Session naming both
    would need a second read to separate them — a read whose answer would say
    nothing a caller acts on differently.
    """
    named_spawning = spawning_event_id is not None
    named_parent = record.parent_session_id is not None
    if not named_parent and not named_spawning:
        return StoreError("the Session write reported no row, so no depth was derived")
    if named_parent and not named_spawning:
        return MissingParentError(
            f"the Session {record.id} names the parent Session {record.parent_session_id}, "
            "which does not exist, so nothing was written"
        )
    if named_spawning and not named_parent:
        return MissingParentError(
            f"the Session {record.id} names a spawning Event that does not exist, so "
            "nothing was written; a spawning Event is inserted before the Session it spawns"
        )
    return MissingParentError(
        f"the Session {record.id} names a parent Session or a spawning Event that does "
        "not exist, so nothing was written"
    )


def _missing_reference(error: BaseException, record: Session) -> MissingParentError | None:
    """The failure to raise for a Session naming a row that does not exist.

    The constraint name is used when the driver reports one, so the message says
    which reference was unsatisfied. When it reports none, both candidate
    references are named rather than one being guessed at.

    This path is the cluster's half of the same refusal and is kept because it
    still fires: a schema at the first migration generation declares both
    references, and the write is refused there before the statement's own guards
    are consulted.
    """
    if _state_of(error) != FOREIGN_KEY_VIOLATION_STATE:
        return None
    constraint = _constraint_of(error)
    if constraint == PARENT_REFERENCE_CONSTRAINT:
        return MissingParentError(
            f"the Session {record.id} names the parent Session {record.parent_session_id}, "
            "which does not exist, so nothing was written"
        )
    if constraint == SPAWNING_REFERENCE_CONSTRAINT:
        return MissingParentError(
            f"the Session {record.id} names a spawning Event that does not exist, so "
            "nothing was written; a spawning Event is inserted before the Session it spawns"
        )
    return MissingParentError(
        f"the Session {record.id} names a parent Session or a spawning Event that does "
        "not exist, so nothing was written"
    )


def _state_of(error: BaseException) -> str | None:
    """The state a driver failure carries, or None when it carries none."""
    for attribute in _STATE_ATTRIBUTES:
        state = getattr(error, attribute, None)
        if isinstance(state, str):
            return state
    return None


def _constraint_of(error: BaseException) -> str | None:
    """The constraint a driver failure names, or None when it names none."""
    diagnostic: object = getattr(error, "diag", None)
    if diagnostic is None:
        return None
    name: object = getattr(diagnostic, "constraint_name", None)
    return name if isinstance(name, str) else None


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------


def _bounded(limit: int) -> int:
    """The row bound to send, refusing one that is not a usable bound."""
    if limit < 1:
        raise ValueError("a read bound must admit at least one row")
    if limit > MAX_READ_LIMIT:
        raise ValueError(f"a read bound may not exceed {MAX_READ_LIMIT} rows")
    return limit


def _canonical_json(payload: JsonObject) -> str:
    """Render an attribution mapping in the canonical JSON form the column holds."""
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


def _session_of(row: Sequence[object]) -> Session:
    """Build a Session from one selected row."""
    if len(row) != _SESSION_ROW_WIDTH:
        raise StoreError(
            f"a Session row carries {len(row)} column(s) where {_SESSION_ROW_WIDTH} were selected"
        )
    return Session(
        id=_as_uuid(row[0]),
        client_id=_as_uuid(row[1]),
        agent_cli=_as_str(row[2]),
        machine_id=_as_str(row[3]),
        team_id=_as_optional_str(row[4]),
        attribution=_as_object(row[5]),
        workspace_path=_as_optional_str(row[6]),
        started_at=_as_instant(row[7]),
        ended_at=_as_optional_instant(row[8]),
        outcome=SessionOutcome(_as_str(row[9])),
        parent_session_id=_as_optional_uuid(row[10]),
        spawning_event_id=_as_optional_uuid(row[11]),
        depth=_as_int(row[12]),
        tool_call_count=_as_int(row[13]),
        model_request_count=_as_int(row[14]),
        error_count=_as_int(row[15]),
        token_count=_as_int(row[16]),
        cost_usd=_as_decimal(row[17]),
        halted=_as_bool(row[18]),
        halted_at=_as_optional_instant(row[19]),
        halt_reason=_as_optional_str(row[20]),
        halt_rule_id=_as_optional_uuid(row[21]),
    )


def _counters_of(row: Sequence[object]) -> SessionCounters:
    """Build the counter snapshot an increment or a terminal write returned."""
    return SessionCounters(
        tool_call_count=_as_int(_column(row, 0, _COUNTER_ROW_WIDTH)),
        model_request_count=_as_int(row[1]),
        error_count=_as_int(row[2]),
        token_count=_as_int(row[3]),
        cost_usd=_as_decimal(row[4]),
    )


def _event_of(row: Sequence[object]) -> EventSummary:
    """Build one Event summary from a selected Ledger row."""
    return EventSummary(
        id=_as_uuid(_column(row, 0, _EVENT_ROW_WIDTH)),
        session_id=_as_uuid(row[1]),
        client_id=_as_uuid(row[2]),
        seq=_as_int(row[3]),
        category=EventCategory(_as_str(row[4])),
        occurred_at=_as_instant(row[5]),
        recorded_at=_as_instant(row[6]),
        parent_event_id=_as_optional_uuid(row[7]),
        redacted=_as_bool(row[8]),
        content_digest=_as_str(row[9]),
        chain_digest=_as_str(row[10]),
    )


def _artifact_of(row: Sequence[object]) -> ArtifactSummary:
    """Build one Artifact summary from a selected Derived_Artifact row."""
    return ArtifactSummary(
        id=_as_uuid(_column(row, 0, _ARTIFACT_ROW_WIDTH)),
        kind=DerivedArtifactKind(_as_str(row[1])),
        client_id=_as_uuid(row[2]),
        content_digest=_as_str(row[3]),
        derivation_method=_as_str(row[4]),
        revision=_as_int(row[5]),
        created_at=_as_instant(row[6]),
        embedding_state=EmbeddingState(_as_str(row[7])),
    )


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a column whose type is not the one the schema declares.

    The type is named and the value is not: a column of this schema may hold
    memory content, and a message naming the fault belongs in a log record while
    the content does not.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_optional_uuid(value: object) -> UUID | None:
    return None if value is None else _as_uuid(value)


def _as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_optional_str(value: object) -> str | None:
    return None if value is None else _as_str(value)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a whole number")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a whole number")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise _unexpected(value, "a boolean")


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise _unexpected(value, "a decimal amount")


def _as_instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return require_aware(value, "a selected timestamp")
    raise _unexpected(value, "a timestamp")


def _as_optional_instant(value: object) -> datetime | None:
    return None if value is None else _as_instant(value)


def _as_object(value: object) -> JsonObject:
    """Read a document column, whether the driver returns it decoded or as text."""
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise _unexpected(value, "a JSON object")
    fields: JsonObject = {}
    for key, item in decoded.items():
        if not isinstance(key, str):
            raise _unexpected(key, "a text key")
        fields[key] = item
    return fields
