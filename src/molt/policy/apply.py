"""Applying what an evaluation decided: the kill switch, the match record, the queue.

`molt.policy.evaluate` decides; this module writes. The split is the same one the
rule module draws for a different reason: an evaluation that wrote rows could not be
replayed, and replay is exactly what a consumption restart does.

**The halt marker lives in the cluster, so it is fleet-wide.** The statement sets the
four halt columns of `session` under a `NOT halted` predicate, which is what makes a
second application of the same outcome move nothing. Capture on any machine learns of
the halt from the Collector's response envelope rather than from a signal sent to the
offending machine, so nothing here reaches out to a host.

**The bound is measured, reported, and never enforced by abandoning the write.** The
halt is applied and the elapsed interval is recorded beside it. A write that overran
the bound is a halt that happened late, which an operator must know about; a write
abandoned at the bound would be a Session left running, which is the failure the
bound exists to prevent. So `Application.within_bound` is a report and the halt is
unconditional.

**Deduplication is the schema's, not this module's.** Both inserts are written
`ON CONFLICT DO NOTHING`, and the constraints they collide with are `match_unique`
and `approval_unique`. Nothing here reads a table to decide whether to write, which
is what makes a redelivered mutation after a consumption restart cost one refused
insert rather than a second halt or a second approval entry. One caveat is recorded
rather than papered over: those constraints treat two null triggering-mutation
identifiers as distinct, so deduplication holds for a Ledger mutation, which always
names an Event, and not for a Derived_Artifact mutation, which names none. The
consuming side is what keeps a Derived_Artifact mutation from being replayed, and
`REDELIVERY_DEDUPLICATED_KINDS` states where the guarantee comes from.

**Every statement is a whole module-level literal with bound parameters.** No
identifier and no domain value is interpolated, the match detail is bound as
canonical JSON text cast to the column type, and the resolution instant is a bound
value read from the injected clock rather than the cluster's own, so a test drives it
without sleeping.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from molt.models.event import JsonObject, require_aware
from molt.policy.evaluate import Mutation, MutationTable, PolicyOutcome, governing_outcome
from molt.policy.rules import PolicyAction
from molt.store import Cursor, MemoryStore
from molt.telemetry import Severity, log, metric

__all__ = [
    "APPROVALS_METRIC",
    "APPROVAL_DECISION_VALUES",
    "APPROVAL_STATUS_VALUES",
    "COMPONENT",
    "HALTS_METRIC",
    "HALT_BOUND_SECONDS",
    "HALT_SESSION_STATEMENT",
    "INSERT_APPROVAL_STATEMENT",
    "INSERT_MATCH_STATEMENT",
    "PENDING_APPROVALS_QUERY",
    "QUEUE_ENTRY_QUERY",
    "QUEUE_LIST_QUERY",
    "QUEUE_PAGE_LIMIT",
    "REDELIVERY_DEDUPLICATED_KINDS",
    "RESOLVE_APPROVAL_STATEMENT",
    "SESSION_HALT_QUERY",
    "Application",
    "ApprovalDecision",
    "ApprovalStatus",
    "Clock",
    "PendingApproval",
    "QueuedApproval",
    "ResolvedApproval",
    "SessionHalt",
    "apply_outcomes",
    "pending_approvals",
    "queued_approval",
    "queued_approvals",
    "resolve_approval",
    "session_halt",
    "system_clock",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "policy"

# The interval a halt is required to land inside, in seconds. It is a requirement's
# bound rather than a deployment's choice, so it is stated here rather than read from
# the configuration surface, and it is injectable so a test can assert the report
# rather than wait for the interval.
HALT_BOUND_SECONDS: Final[float] = 10.0

# The two counters this module publishes.
HALTS_METRIC: Final[str] = "watcher.halts"
APPROVALS_METRIC: Final[str] = "watcher.approvals_raised"

# The mutation kinds whose redelivery the schema's uniqueness constraints
# deduplicate. A Ledger mutation names the Event that triggered it, so a second
# delivery collides; a Derived_Artifact mutation names none, and two null
# identifiers do not collide, so its redelivery is prevented by the watermark the
# consuming side resumes from instead.
REDELIVERY_DEDUPLICATED_KINDS: Final[tuple[MutationTable, ...]] = (MutationTable.LEDGER,)

# The stored vocabulary of `approval_queue`, in the order the schema lists it.
APPROVAL_STATUS_VALUES: Final[tuple[str, ...]] = ("pending", "resolved")
APPROVAL_DECISION_VALUES: Final[tuple[str, ...]] = ("approved", "denied")

# The kill switch. The `NOT halted` predicate is what makes a redelivered halt a
# no-op: the row is already marked, so no row is updated and the first halt's reason
# and instant stand. The instant is the cluster's own, because a halt is an event in
# the cluster's history rather than a reading of whichever machine consumed it.
HALT_SESSION_STATEMENT: Final[str] = (
    "UPDATE session SET halted = true, halted_at = now(), halt_reason = %s, halt_rule_id = %s "
    "WHERE id = %s AND NOT halted "
    "RETURNING halted_at"
)

# One match row per applied outcome. The conflict target is left to the constraint,
# so the statement names no index and still resolves a redelivery to nothing.
INSERT_MATCH_STATEMENT: Final[str] = (
    "INSERT INTO policy_match (rule_id, session_id, event_id, action, detail) "
    "VALUES (%s, %s, %s, %s, %s::JSONB) "
    "ON CONFLICT DO NOTHING "
    "RETURNING id"
)

# One queue entry per require-approval match, left pending for an operator.
INSERT_APPROVAL_STATEMENT: Final[str] = (
    "INSERT INTO approval_queue (rule_id, session_id, event_id, status) "
    "VALUES (%s, %s, %s, %s) "
    "ON CONFLICT DO NOTHING "
    "RETURNING id"
)

# Resolution writes the principal, the decision, and the instant together, and only
# over an entry still pending, so two resolutions of one entry keep the first.
RESOLVE_APPROVAL_STATEMENT: Final[str] = (
    "UPDATE approval_queue SET status = %s, resolved_by = %s, decision = %s, resolved_at = %s "
    "WHERE id = %s AND status = %s "
    "RETURNING id, rule_id, session_id, event_id, status, resolved_by, decision, resolved_at"
)

# What capture is told while an entry is unresolved: the rule each pending entry
# names, so the adapter blocks the actions that rule matches and nothing else.
PENDING_APPROVALS_QUERY: Final[str] = (
    "SELECT id, rule_id, session_id, event_id, created_at FROM approval_queue "
    "WHERE session_id = %s AND status = %s "
    "ORDER BY created_at ASC, id ASC"
)

# What an operator is shown: the queue of one tenant set, whatever each entry's
# standing. A resolved entry stays on the list because the principal, the decision,
# and the instant are the evidence that the entry was answered, and a list that
# dropped them would leave nowhere to read that from. Tenancy is inside the
# statement: the entry's Session names the Client, so an entry outside the bound
# identifier set is not selected rather than selected and then filtered. Pending
# entries lead, because those are the rows an operator came to act on, and the
# `approval_pending` index is what serves that half of the ordering.
QUEUE_LIST_QUERY: Final[str] = (
    "SELECT a.id, a.rule_id, r.name, r.action, a.session_id, s.client_id, a.event_id, "
    "a.status, a.created_at, a.resolved_by, a.decision, a.resolved_at "
    "FROM approval_queue AS a "
    "JOIN policy_rule AS r ON r.id = a.rule_id "
    "JOIN session AS s ON s.id = a.session_id "
    "WHERE s.client_id = ANY (%s::UUID[]) "
    "ORDER BY a.status ASC, a.created_at ASC, a.id ASC LIMIT %s"
)

# One entry, admitted by the same tenancy predicate. A resolution reads this first,
# so an entry whose Session belongs to no bound Client is answered as absent rather
# than resolved by a principal that may not see it.
QUEUE_ENTRY_QUERY: Final[str] = (
    "SELECT a.id, a.rule_id, r.name, r.action, a.session_id, s.client_id, a.event_id, "
    "a.status, a.created_at, a.resolved_by, a.decision, a.resolved_at "
    "FROM approval_queue AS a "
    "JOIN policy_rule AS r ON r.id = a.rule_id "
    "JOIN session AS s ON s.id = a.session_id "
    "WHERE a.id = %s AND s.client_id = ANY (%s::UUID[])"
)

# What the halt columns read back as, for a caller asserting the marker landed.
SESSION_HALT_QUERY: Final[str] = (
    "SELECT halted, halted_at, halt_reason, halt_rule_id FROM session WHERE id = %s"
)

# How many entries one page of the queue carries. A bound rather than a caller's
# choice, because an unbounded queue read is a scan of every approval a long-lived
# fleet ever raised.
QUEUE_PAGE_LIMIT: Final[int] = 100

# The transaction labels the two writes appear under.
_APPLY_LABEL: Final[str] = "policy_apply"
_RESOLVE_LABEL: Final[str] = "approval_resolve"

# How many columns each read returns, checked before a row is decoded.
_PENDING_ROW_WIDTH: Final[int] = 5
_HALT_ROW_WIDTH: Final[int] = 4
_RESOLVED_ROW_WIDTH: Final[int] = 8
_QUEUED_ROW_WIDTH: Final[int] = 12

# The time source a resolution instant and the bound measurement are read from,
# injected so no recorded instant is a reading of whichever machine ran.
Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """The current instant, offset-aware, as the default time source."""
    return datetime.now(UTC)


class ApprovalStatus(StrEnum):
    """Whether a queue entry is awaiting an operator, in schema-constraint order."""

    PENDING = "pending"
    RESOLVED = "resolved"


class ApprovalDecision(StrEnum):
    """What an operator decided, in schema-constraint order."""

    APPROVED = "approved"
    DENIED = "denied"


if tuple(member.value for member in ApprovalStatus) != APPROVAL_STATUS_VALUES:
    raise ValueError("the approval statuses must match the schema's constraint, in its order")
if tuple(member.value for member in ApprovalDecision) != APPROVAL_DECISION_VALUES:
    raise ValueError("the approval decisions must match the schema's constraint, in its order")


@dataclass(frozen=True, slots=True)
class SessionHalt:
    """The halt columns of one Session, as a caller reads them back.

    Attributes:
        halted: Whether the Session carries the fleet-wide marker.
        halted_at: When the marker was written, absent while the Session is running.
        reason: The reason recorded with the marker.
        rule_id: The rule that asked for the halt.
    """

    halted: bool
    halted_at: datetime | None = None
    reason: str | None = None
    rule_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One unresolved queue entry, named by the rule it was raised under."""

    id: UUID
    rule_id: UUID
    session_id: UUID
    event_id: UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class QueuedApproval:
    """One queue entry as an operator reads it, whatever its standing.

    The rule is carried by name and by action rather than by identifier alone,
    because what an operator decides about is the rule that asked, and the three
    resolution columns stay optional: a pending entry names no principal, no
    decision, and no instant, and rendering a default for any of them would state a
    decision nobody made.
    """

    id: UUID
    rule_id: UUID
    rule_name: str
    action: str
    session_id: UUID
    client_id: UUID
    event_id: UUID | None
    status: ApprovalStatus
    created_at: datetime
    resolved_by: str | None = None
    decision: ApprovalDecision | None = None
    resolved_at: datetime | None = None

    @property
    def pending(self) -> bool:
        """Whether this entry is still awaiting an operator."""
        return self.status is ApprovalStatus.PENDING


@dataclass(frozen=True, slots=True)
class ResolvedApproval:
    """One resolved queue entry, carrying the principal, decision, and instant."""

    id: UUID
    rule_id: UUID
    session_id: UUID
    event_id: UUID | None
    resolved_by: str
    decision: ApprovalDecision
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class Application:
    """What applying one mutation's outcomes wrote, and how long the halt took.

    Attributes:
        session_id: The Session the outcomes were applied to.
        matches_written: How many match rows the inserts created. A redelivered
            mutation writes none, which is the deduplication being reported rather
            than an application that failed.
        approvals_raised: How many queue entries the inserts created.
        halted: Whether this application wrote the halt marker. False for a
            redelivered halt, whose marker is already standing.
        halt_rule_id: The rule the halt was recorded under, absent where nothing
            asked for a halt.
        halt_reason: The reason recorded with the marker.
        elapsed_seconds: How long the whole application took, measured on the
            injected clock.
        bound_seconds: The bound the measurement is reported against.
    """

    session_id: UUID
    matches_written: int
    approvals_raised: int
    halted: bool
    halt_rule_id: UUID | None
    halt_reason: str | None
    elapsed_seconds: float
    bound_seconds: float

    @property
    def within_bound(self) -> bool:
        """Whether the application landed inside the bound it is measured against."""
        return self.elapsed_seconds <= self.bound_seconds

    @property
    def halt_requested(self) -> bool:
        """Whether an outcome asked for a halt, whether or not this call wrote it."""
        return self.halt_rule_id is not None


def apply_outcomes(
    store: MemoryStore,
    mutation: Mutation,
    outcomes: Sequence[PolicyOutcome],
    *,
    clock: Clock = system_clock,
    bound_seconds: float = HALT_BOUND_SECONDS,
) -> Application:
    """Write every outcome of one mutation in one transaction, and report the interval.

    One transaction is what makes the halt and the match row that explains it arrive
    together: a halt with no match row beside it would be a Session stopped for a
    reason nobody could read. The governing outcome decides whether the marker is
    written, while every outcome writes its own match row, so a warn that accompanied
    a halt is still on the record.

    Args:
        store: The connection surface the transaction is framed by.
        mutation: The consumed mutation the outcomes were evaluated from.
        outcomes: Every outcome the evaluation produced, in any order.
        clock: The time source the elapsed measurement is read from.
        bound_seconds: The interval the halt is reported against.

    Returns:
        What was written, with the measured interval and the bound beside it.
    """
    if bound_seconds <= 0:
        raise ValueError("the halt bound must be a positive number of seconds")
    started = require_aware(clock(), "a policy application start instant")
    governing = governing_outcome(outcomes)
    halting = governing if governing is not None and _asks_for_halt(governing) else None
    reason = None if halting is None else _halt_reason(halting)

    def body(cursor: Cursor) -> tuple[int, int, bool]:
        matches = 0
        approvals = 0
        for outcome in outcomes:
            if _insert_match(cursor, outcome):
                matches += 1
            if PolicyAction(outcome.action) is PolicyAction.REQUIRE_APPROVAL and _insert_approval(
                cursor, outcome
            ):
                approvals += 1
        marked = False
        if halting is not None and reason is not None:
            marked = _mark_halted(cursor, halting.session_id, reason, halting.rule_id)
        return matches, approvals, marked

    matches_written, approvals_raised, marked_halted = store.in_serializable(
        body, label=_APPLY_LABEL
    )
    elapsed = _elapsed_seconds(started, require_aware(clock(), "a policy application end instant"))
    application = Application(
        session_id=mutation.session_id,
        matches_written=matches_written,
        approvals_raised=approvals_raised,
        halted=marked_halted,
        halt_rule_id=None if halting is None else halting.rule_id,
        halt_reason=reason,
        elapsed_seconds=elapsed,
        bound_seconds=bound_seconds,
    )
    _report(application, mutation)
    return application


def _asks_for_halt(outcome: PolicyOutcome) -> bool:
    """Whether the governing outcome is the one that stops a Session."""
    return PolicyAction(outcome.action) is PolicyAction.HALT_AGENT


def _halt_reason(outcome: PolicyOutcome) -> str:
    """The reason recorded with the marker: the rule's name and the kind it matched.

    The rule's name and its match kind, and nothing from the payload. A reason is
    read by an operator and by capture's blocking response, so it must not carry a
    path, a command, or any other value the mutation held.
    """
    return f"{outcome.rule_name} ({outcome.match_kind.value})"


def _insert_match(cursor: Cursor, outcome: PolicyOutcome) -> bool:
    """Write one match row, reporting whether the constraint admitted it."""
    cursor.execute(
        INSERT_MATCH_STATEMENT,
        (
            outcome.rule_id,
            outcome.session_id,
            outcome.event_id,
            PolicyAction(outcome.action).value,
            _canonical_json(outcome.detail),
        ),
    )
    return cursor.fetchone() is not None


def _insert_approval(cursor: Cursor, outcome: PolicyOutcome) -> bool:
    """Enqueue one approval, reporting whether the constraint admitted it."""
    cursor.execute(
        INSERT_APPROVAL_STATEMENT,
        (
            outcome.rule_id,
            outcome.session_id,
            outcome.event_id,
            ApprovalStatus.PENDING.value,
        ),
    )
    return cursor.fetchone() is not None


def _mark_halted(cursor: Cursor, session_id: UUID, reason: str, rule_id: UUID) -> bool:
    """Write the halt marker, reporting whether this call is the one that wrote it."""
    cursor.execute(HALT_SESSION_STATEMENT, (reason, rule_id, session_id))
    return cursor.fetchone() is not None


def _report(application: Application, mutation: Mutation) -> None:
    """Publish the counters and one record naming what was applied.

    Neither the counters nor the record carries a Session identifier as a dimension:
    a per-Session dimension would multiply into the billable cardinality bound with
    the fleet's size, which is the one growth the bound exists to refuse.
    """
    if application.halted:
        metric(HALTS_METRIC)
    if application.approvals_raised:
        metric(APPROVALS_METRIC, float(application.approvals_raised))
    if not application.halt_requested and not application.matches_written:
        return
    log(
        Severity.WARNING if application.halt_requested else Severity.INFO,
        COMPONENT,
        "applied the policy outcomes of one consumed mutation",
        table=MutationTable(mutation.table).value,
        matches_written=application.matches_written,
        approvals_raised=application.approvals_raised,
        halted=application.halted,
        halt_reason=application.halt_reason,
        elapsed_seconds=round(application.elapsed_seconds, 6),
        within_bound=application.within_bound,
    )


def resolve_approval(
    store: MemoryStore,
    approval_id: UUID,
    *,
    principal: str,
    decision: ApprovalDecision,
    clock: Clock = system_clock,
) -> ResolvedApproval | None:
    """Record who resolved an entry, what they decided, and when.

    The three are written together because an entry resolved without a principal
    would be a decision nobody is accountable for. An entry already resolved is left
    as it stands and None is returned, so a repeated resolution cannot overwrite the
    first principal's decision.

    Args:
        store: The connection surface the write is framed by.
        approval_id: The queue entry being resolved.
        principal: The operator the console authenticated.
        decision: Approved or denied.
        clock: The time source the resolution instant is read from.

    Returns:
        The resolved entry, or None where it was already resolved or does not exist.

    Raises:
        ValueError: The principal is empty, so the entry would name nobody.
    """
    if not principal.strip():
        raise ValueError("a resolved approval records the principal that resolved it")
    resolved_at = require_aware(clock(), "an approval resolution instant")
    chosen = ApprovalDecision(decision)

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(
            RESOLVE_APPROVAL_STATEMENT,
            (
                ApprovalStatus.RESOLVED.value,
                principal,
                chosen.value,
                resolved_at,
                approval_id,
                ApprovalStatus.PENDING.value,
            ),
        )
        return cursor.fetchone()

    row = store.in_serializable(body, label=_RESOLVE_LABEL)
    if row is None:
        return None
    resolved = _resolved_of(row)
    log(
        Severity.INFO,
        COMPONENT,
        "an operator resolved a pending approval",
        decision=resolved.decision.value,
        rule_id=str(resolved.rule_id),
    )
    return resolved


def pending_approvals(store: MemoryStore, session_id: UUID) -> tuple[PendingApproval, ...]:
    """Every unresolved entry for one Session, oldest first.

    This is what the Collector returns to capture: each entry names its rule, so the
    adapter blocks the actions that rule matches rather than every action.
    """

    def body(cursor: Cursor) -> tuple[PendingApproval, ...]:
        cursor.execute(PENDING_APPROVALS_QUERY, (session_id, ApprovalStatus.PENDING.value))
        return tuple(_pending_of(row) for row in cursor.fetchall())

    return store.read(body)


def queued_approvals(
    store: MemoryStore,
    client_ids: Sequence[UUID],
    *,
    limit: int = QUEUE_PAGE_LIMIT,
) -> tuple[QueuedApproval, ...]:
    """The queue of one tenant set, pending entries first and oldest first inside each.

    This is what the console's queue list renders. The Client set is bound into the
    statement rather than applied to its answer, so an entry belonging to a Session of
    another tenant is never read at all. An empty set reads nothing, because a read
    scoped to no tenant can only return rows that are outside every tenant it was
    meant to be scoped by.

    Args:
        store: The connection surface the read is issued on.
        client_ids: The Clients an operator may see, from the roster rather than from
            a request.
        limit: How many entries one page carries.

    Returns:
        The entries, pending first, each naming the rule that raised it.

    Raises:
        ValueError: The page bound is not positive, so it would describe no page.
    """
    if limit <= 0:
        raise ValueError("a queue page carries a positive number of entries")
    if not client_ids:
        return ()

    def body(cursor: Cursor) -> tuple[QueuedApproval, ...]:
        cursor.execute(QUEUE_LIST_QUERY, (list(client_ids), limit))
        return tuple(_queued_of(row) for row in cursor.fetchall())

    return store.read(body)


def queued_approval(
    store: MemoryStore, approval_id: UUID, client_ids: Sequence[UUID]
) -> QueuedApproval | None:
    """One entry of the queue, or None where no bound Client's Session holds it.

    A resolution reads this before it writes, so an entry outside the permitted tenant
    set is answered as absent rather than resolved.
    """
    if not client_ids:
        return None

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(QUEUE_ENTRY_QUERY, (approval_id, list(client_ids)))
        return cursor.fetchone()

    row = store.read(body)
    return None if row is None else _queued_of(row)


def session_halt(store: MemoryStore, session_id: UUID) -> SessionHalt | None:
    """The halt columns of one Session, or None where no such Session is stored."""

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(SESSION_HALT_QUERY, (session_id,))
        return cursor.fetchone()

    row = store.read(body)
    return None if row is None else _halt_of(row)


def _elapsed_seconds(started: datetime, ended: datetime) -> float:
    """The interval between two readings, never negative.

    A clock that went backwards reads as no elapsed interval rather than as a
    negative one, because a negative interval would report a bound satisfied by
    arithmetic that never happened.
    """
    return max((ended - started).total_seconds(), 0.0)


def _canonical_json(payload: JsonObject) -> str:
    """Render a match detail in the canonical JSON form the column holds."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _checked(row: Sequence[object], width: int, what: str) -> Sequence[object]:
    """A row whose width is the one its decoder reads, or a refusal naming both."""
    if len(row) != width:
        raise ValueError(f"the {what} read returned {len(row)} column(s) where {width} are read")
    return row


def _uuid(value: object, what: str) -> UUID:
    """One identifier column, refusing anything that is not one."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise ValueError(f"the column {what} did not return an identifier")


def _optional_uuid(value: object, what: str) -> UUID | None:
    """One nullable identifier column."""
    return None if value is None else _uuid(value, what)


def _instant(value: object, what: str) -> datetime:
    """One timestamp column, required to be offset-aware."""
    if not isinstance(value, datetime):
        raise ValueError(f"the column {what} did not return a timestamp")
    return require_aware(value, f"the column {what}")


def _pending_of(row: Sequence[object]) -> PendingApproval:
    """Build one pending entry from a stored row."""
    columns = _checked(row, _PENDING_ROW_WIDTH, "pending approval")
    return PendingApproval(
        id=_uuid(columns[0], "id"),
        rule_id=_uuid(columns[1], "rule_id"),
        session_id=_uuid(columns[2], "session_id"),
        event_id=_optional_uuid(columns[3], "event_id"),
        created_at=_instant(columns[4], "created_at"),
    )


def _queued_of(row: Sequence[object]) -> QueuedApproval:
    """Build one queue entry from a stored row, refusing a vocabulary the schema forbids."""
    columns = _checked(row, _QUEUED_ROW_WIDTH, "queued approval")
    decision = columns[10]
    principal = columns[9]
    if principal is not None and not isinstance(principal, str):
        raise ValueError("the column resolved_by did not return text")
    if decision is not None and not isinstance(decision, str):
        raise ValueError("the column decision did not return text")
    return QueuedApproval(
        id=_uuid(columns[0], "id"),
        rule_id=_uuid(columns[1], "rule_id"),
        rule_name=_text(columns[2], "name"),
        action=_text(columns[3], "action"),
        session_id=_uuid(columns[4], "session_id"),
        client_id=_uuid(columns[5], "client_id"),
        event_id=_optional_uuid(columns[6], "event_id"),
        status=ApprovalStatus(_text(columns[7], "status")),
        created_at=_instant(columns[8], "created_at"),
        resolved_by=principal,
        decision=None if decision is None else ApprovalDecision(decision),
        resolved_at=None if columns[11] is None else _instant(columns[11], "resolved_at"),
    )


def _text(value: object, what: str) -> str:
    """One text column, refusing anything that is not text."""
    if isinstance(value, str):
        return value
    raise ValueError(f"the column {what} did not return text")


def _resolved_of(row: Sequence[object]) -> ResolvedApproval:
    """Build one resolved entry from the row the resolving statement returned."""
    columns = _checked(row, _RESOLVED_ROW_WIDTH, "resolved approval")
    decision = columns[6]
    principal = columns[5]
    if not isinstance(decision, str) or not isinstance(principal, str):
        raise ValueError("a resolved approval names both a principal and a decision")
    return ResolvedApproval(
        id=_uuid(columns[0], "id"),
        rule_id=_uuid(columns[1], "rule_id"),
        session_id=_uuid(columns[2], "session_id"),
        event_id=_optional_uuid(columns[3], "event_id"),
        resolved_by=principal,
        decision=ApprovalDecision(decision),
        resolved_at=_instant(columns[7], "resolved_at"),
    )


def _halt_of(row: Sequence[object]) -> SessionHalt:
    """Build the halt reading from a stored Session row."""
    columns = _checked(row, _HALT_ROW_WIDTH, "session halt")
    halted = columns[0]
    reason = columns[2]
    if not isinstance(halted, bool):
        raise ValueError("the column halted did not return a boolean")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("the column halt_reason did not return text")
    return SessionHalt(
        halted=halted,
        halted_at=None if columns[1] is None else _instant(columns[1], "halted_at"),
        reason=reason,
        rule_id=_optional_uuid(columns[3], "halt_rule_id"),
    )
