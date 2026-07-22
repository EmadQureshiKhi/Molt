"""Procedural standing: retrieval records, outcome records, the adjustment, the history.

This module is the data-access half of the Confidence_Tracker. It holds every
statement that touches a learned procedure's standing and nothing else: the
outcome-to-delta rule, the configured values behind it, and the measurement that
reports a movement live in the tracker package, because none of them is a
statement and all of them have to be testable without a cluster.

Five claims shape the statements here, and each is arranged so a caller cannot
lose it by forgetting something.

**A retrieval is evidence, not a verdict.** The retrieval insert writes a row and
touches no confidence value. Being returned by recall says an agent was shown a
procedure; it says nothing about whether following it helped. So there is no
statement here that moves a value on a retrieval, and the count of retrievals is
read for the console rather than fed into the arithmetic.

**An outcome cannot be asserted for a procedure a Session never retrieved, and the
classification stored is the Session's own.** The outcome insert selects the
classification out of the `session` row rather than from a bound value, and its
predicate requires a retrieval row for that procedure and that Session. The
caller's classification is bound as an assertion the cluster checks, not as the
value written: a caller that disagrees with the Session's recorded outcome inserts
nothing. Both refusals are the cluster's, so a recording path that skipped a check
could not produce the row anyway.

**A repeated report changes nothing.** The insert names the per-Session uniqueness
constraint as its arbiter and does nothing on conflict, so a retried delivery, a
duplicated report, or an operator re-running a step contributes no second
adjustment. Zero returned rows is then read as one of four situations, told apart
by a single diagnostic read taken only on that path: no such procedure, no
retrieval by that Session, a Session whose recorded outcome differs, or an outcome
already on the record.

**The clamp is the cluster's arithmetic.** The adjusting statement computes
`greatest(floor, least(ceiling, current + delta))` from the row's own current
value, with both bounds bound as parameters from the model's own constants, so the
closed unit interval holds even if a caller passes an absurd delta and the column
check never has to be the thing that catches it. The prior value is read in the
same transaction, so the value the change record names as prior is the value the
update actually started from.

**A change record accompanies every movement and never claims one that did not
happen.** The adjustment and the change record are two statements of one
SERIALIZABLE transaction, so no interleaving leaves a moved value without its
justification or a record for a movement that did not commit. When clamping
absorbs the whole adjustment the update commits an unchanged value and no record
is written, because the column check refuses a record whose endpoints are equal
and a history that overstates is no more usable than one that omits. What
distinguishes an absorbed adjustment from an absent one is therefore the outcome
row: an outcome on the record with no change record beside it is an adjustment that
was applied to a value already standing at a bound.

Every statement is a whole module-level literal, every caller-supplied value is a
bound parameter, and no identifier and no domain value is ever interpolated: the
artifact kind, the classification, and both interval bounds all travel as
parameters drawn from the model, so this module states none of them twice. No log
record here carries a procedure body, because a procedure body is content.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from molt.errors import StoreError
from molt.models.artifact import (
    CONFIDENCE_CEILING,
    CONFIDENCE_FLOOR,
    DerivedArtifactKind,
)
from molt.models.event import require_aware
from molt.models.session import SessionOutcome
from molt.store import Cursor, MemoryStore

__all__ = [
    "ADJUST_STANDING_STATEMENT",
    "COMPONENT",
    "COUNT_RETRIEVALS_QUERY",
    "DEFAULT_HISTORY_LIMIT",
    "INSERT_CHANGE_STATEMENT",
    "INSERT_OUTCOME_STATEMENT",
    "INSERT_RETRIEVAL_STATEMENT",
    "MAX_HISTORY_LIMIT",
    "OUTCOME_CONTEXT_QUERY",
    "SELECT_CHANGES_QUERY",
    "SELECT_OUTCOME_COUNTS_QUERY",
    "SELECT_STANDING_QUERY",
    "TERMINAL_OUTCOMES",
    "ConfidenceChange",
    "OutcomeApplication",
    "OutcomeContext",
    "OutcomeRecord",
    "ProcedureStanding",
    "RetrievalRecord",
    "adjust_standing",
    "apply_outcome",
    "change_history",
    "count_retrievals",
    "insert_change",
    "insert_outcome",
    "insert_retrieval",
    "procedure_standing",
    "read_outcome_context",
    "record_retrieval",
    "select_changes",
    "select_outcome_counts",
    "select_standing",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The three classifications a terminal Session reports and the outcome table
# admits. Drawn from the Session model rather than written out, so the set this
# module refuses outside of is the same set the schema's check names.
TERMINAL_OUTCOMES: Final[frozenset[SessionOutcome]] = frozenset(
    {SessionOutcome.SUCCEEDED, SessionOutcome.FAILED, SessionOutcome.ABANDONED}
)

# The kind that carries a standing at all. Bound as a value everywhere below, so
# the equivalence the schema states lives in the model and is quoted from there.
_PROCEDURE_KIND: Final[str] = DerivedArtifactKind.LEARNED_PROCEDURE.value

# How many change records a history read returns when a caller names no bound, and
# the ceiling a caller may not ask past, so no read of a long-lived procedure's
# history is an unbounded scan.
DEFAULT_HISTORY_LIMIT: Final[int] = 100
MAX_HISTORY_LIMIT: Final[int] = 10000

# How many columns each row shape carries, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_RETRIEVAL_ROW_WIDTH: Final[int] = 4
_OUTCOME_ROW_WIDTH: Final[int] = 5
_STANDING_ROW_WIDTH: Final[int] = 1
_CHANGE_ROW_WIDTH: Final[int] = 6
_WRITTEN_CHANGE_WIDTH: Final[int] = 2
_CONTEXT_ROW_WIDTH: Final[int] = 4
_COUNT_ROW_WIDTH: Final[int] = 1
_OUTCOME_COUNT_WIDTH: Final[int] = 2

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# One retrieval. The procedure identifier written is the cluster's own row
# identity rather than the caller's parameter, and the source row is restricted to
# the procedure kind, so a retrieval of a summary is unwritable rather than merely
# unwritten.
INSERT_RETRIEVAL_STATEMENT: Final[str] = (
    "INSERT INTO procedure_retrieval (procedure_id, session_id) "
    "SELECT d.id, %s FROM derived_artifact AS d WHERE d.id = %s AND d.kind = %s "
    "RETURNING id, procedure_id, session_id, retrieved_at"
)

# The current standing, read inside the adjusting transaction so the prior value a
# change record names is the value the update started from. No aggregate is joined
# in: this read is on the write path and the counts the console wants are not.
SELECT_STANDING_QUERY: Final[str] = (
    "SELECT procedure_confidence FROM derived_artifact WHERE id = %s AND kind = %s"
)

# One outcome. The classification comes out of the Session row, the retrieval
# requirement is the `EXISTS` clause, and the caller's classification is bound as
# an assertion the predicate checks rather than as the value stored. Both keyed
# rows contribute at most one source row each, so no duplicate can arrive from the
# source and the arbiter is only ever reached by a genuinely repeated report.
INSERT_OUTCOME_STATEMENT: Final[str] = (
    "INSERT INTO procedure_outcome (procedure_id, session_id, outcome) "
    "SELECT d.id, s.id, s.outcome FROM derived_artifact AS d, session AS s "
    "WHERE d.id = %s AND d.kind = %s AND s.id = %s AND s.outcome = %s "
    "AND EXISTS (SELECT 1 FROM procedure_retrieval AS r "
    "WHERE r.procedure_id = d.id AND r.session_id = s.id) "
    "ON CONFLICT (procedure_id, session_id) DO NOTHING "
    "RETURNING id, procedure_id, session_id, outcome, recorded_at"
)

# Why an outcome insert wrote nothing, read only when it wrote nothing. Four
# independent facts in one row, so the refusal names the situation rather than
# leaving a caller to guess which premise failed.
OUTCOME_CONTEXT_QUERY: Final[str] = (
    "SELECT "
    "(SELECT count(*) FROM derived_artifact WHERE id = %s AND kind = %s), "
    "(SELECT outcome FROM session WHERE id = %s), "
    "(SELECT count(*) FROM procedure_retrieval WHERE procedure_id = %s AND session_id = %s), "
    "(SELECT count(*) FROM procedure_outcome WHERE procedure_id = %s AND session_id = %s)"
)

# The adjustment. The arithmetic is the cluster's, over the row's own current
# value, and both bounds are bound from the model's constants, so the closed unit
# interval is enforced by the statement and the column check is a backstop rather
# than the mechanism. Only the standing column is assigned, which is the column
# the writer role is confined to.
ADJUST_STANDING_STATEMENT: Final[str] = (
    "UPDATE derived_artifact SET procedure_confidence = "
    "greatest(%s::FLOAT8, least(%s::FLOAT8, procedure_confidence + %s::FLOAT8)) "
    "WHERE id = %s AND kind = %s "
    "RETURNING procedure_confidence"
)

# The change record, written in the same transaction as the adjustment it
# describes and naming the outcome that caused it.
INSERT_CHANGE_STATEMENT: Final[str] = (
    "INSERT INTO procedure_confidence_change "
    "(procedure_id, prior_value, new_value, outcome_id) "
    "VALUES (%s, %s, %s, %s) RETURNING id, changed_at"
)

# The audited history, in the order the movements happened, served by the index
# over the procedure and the change instant. The identifier is the final key so the
# order is total even for two movements the cluster stamped at one instant.
SELECT_CHANGES_QUERY: Final[str] = (
    "SELECT id, procedure_id, prior_value, new_value, outcome_id, changed_at "
    "FROM procedure_confidence_change WHERE procedure_id = %s "
    "ORDER BY changed_at ASC, id ASC LIMIT %s"
)

# How much a procedure is used, which the console shows beside how well it has
# done. Served by the leading column of the retrieval index.
COUNT_RETRIEVALS_QUERY: Final[str] = (
    "SELECT count(*) FROM procedure_retrieval WHERE procedure_id = %s"
)

# The per-classification counts, aggregated by the cluster and served by the
# outcome index, so the console renders three numbers rather than fetching rows.
SELECT_OUTCOME_COUNTS_QUERY: Final[str] = (
    "SELECT outcome, count(*) FROM procedure_outcome WHERE procedure_id = %s "
    "GROUP BY outcome ORDER BY outcome"
)

# The labels the transactions of this module appear under in a log record and in
# the note an exhausted retry attaches.
_RETRIEVAL_LABEL: Final[str] = "procedure_retrieval"
_OUTCOME_LABEL: Final[str] = "procedure_outcome"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalRecord:
    """One recorded retrieval of a procedure by a consuming Session."""

    id: UUID
    procedure_id: UUID
    session_id: UUID
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One recorded outcome of a Session that consumed a procedure.

    The classification is the Session's own, read back off the inserted row rather
    than echoed from the caller's argument, so what a caller holds afterwards is
    what the cluster stored.
    """

    id: UUID
    procedure_id: UUID
    session_id: UUID
    outcome: SessionOutcome
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ConfidenceChange:
    """One audited movement of a procedure's standing.

    Both endpoints are stored, so the history states what the value moved from as
    well as what it moved to, and the outcome reference is what lets a value in the
    history be traced back to the Session whose ending produced it.
    """

    id: UUID
    procedure_id: UUID
    prior_value: float
    new_value: float
    outcome_id: UUID
    changed_at: datetime

    @property
    def applied_delta(self) -> float:
        """How far the value actually moved, which is what the record asserts."""
        return self.new_value - self.prior_value


@dataclass(frozen=True, slots=True)
class OutcomeContext:
    """Why an outcome insert wrote nothing, as one read of four facts.

    Read only when the insert returned no row, so the ordinary path pays nothing
    for a refusal message it does not need.
    """

    procedures: int
    session_outcome: SessionOutcome | None
    retrievals: int
    outcomes: int

    @property
    def reason(self) -> str:
        """The situation the insert refused, named for a failure message."""
        if self.procedures == 0:
            return "no learned procedure carries that identifier"
        if self.session_outcome is None:
            return "no session carries that identifier"
        if self.outcomes > 0:
            return "that session already contributed an outcome for that procedure"
        if self.retrievals == 0:
            return "that session never retrieved that procedure"
        return (
            "that session's own recorded outcome is "
            f"{self.session_outcome.value} rather than the asserted classification"
        )

    @property
    def already_recorded(self) -> bool:
        """Whether the refusal is a repeated report rather than a faulty one."""
        return self.procedures > 0 and self.session_outcome is not None and self.outcomes > 0


@dataclass(frozen=True, slots=True)
class OutcomeApplication:
    """What recording one outcome did: the row, the movement, and the record of it.

    Attributes:
        outcome: The stored outcome row, or None when this Session's outcome for
            this procedure was already on the record and nothing was written.
        prior_value: The standing the adjustment started from, or None when no
            adjustment was attempted at all.
        new_value: The standing the adjustment committed, or None for the same
            reason.
        change: The audited change record, or None when the value did not move.
    """

    outcome: OutcomeRecord | None
    prior_value: float | None
    new_value: float | None
    change: ConfidenceChange | None

    @property
    def recorded(self) -> bool:
        """Whether this call put an outcome on the record."""
        return self.outcome is not None

    @property
    def attempted(self) -> bool:
        """Whether an adjustment was applied to the standing."""
        return self.prior_value is not None and self.new_value is not None

    @property
    def absorbed(self) -> bool:
        """Whether an attempted adjustment was absorbed entirely by a bound.

        True is the case an operator reads as: the adjustment was applied and the
        value was already standing at a bound, so there is an outcome on the record
        and deliberately no change record beside it.
        """
        return self.attempted and self.change is None


@dataclass(frozen=True, slots=True)
class ProcedureStanding:
    """A procedure's current standing, how much it is used, and how it has done.

    The outcome counts carry every terminal classification, zero included, so a
    caller rendering three numbers never has to decide what an absent key means.
    """

    procedure_id: UUID
    confidence: float
    retrievals: int
    outcomes: tuple[tuple[SessionOutcome, int], ...]

    def count_of(self, outcome: SessionOutcome) -> int:
        """How many Sessions reported one classification for this procedure."""
        for classification, count in self.outcomes:
            if classification is outcome:
                return count
        return 0


# ---------------------------------------------------------------------------
# Retrieval records
# ---------------------------------------------------------------------------


def insert_retrieval(cursor: Cursor, procedure_id: UUID, session_id: UUID) -> RetrievalRecord:
    """Record one retrieval on a caller's cursor, moving no standing.

    This is the composable form the recall path uses: the record is written after
    the response has been composed, so it is not on the latency path, and it is a
    statement of use rather than of merit.

    Raises:
        StoreError: The identifier names no learned procedure, so a retrieval of
            one cannot be recorded.
    """
    cursor.execute(INSERT_RETRIEVAL_STATEMENT, (session_id, procedure_id, _PROCEDURE_KIND))
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            "the retrieval was not recorded because no learned procedure carries that identifier"
        )
    return RetrievalRecord(
        id=_as_uuid(_column(row, 0, _RETRIEVAL_ROW_WIDTH)),
        procedure_id=_as_uuid(row[1]),
        session_id=_as_uuid(row[2]),
        retrieved_at=_as_instant(row[3]),
    )


def record_retrieval(
    store: MemoryStore,
    procedure_id: UUID,
    session_id: UUID,
) -> RetrievalRecord:
    """Record one retrieval in a transaction of its own."""

    def body(cursor: Cursor) -> RetrievalRecord:
        return insert_retrieval(cursor, procedure_id, session_id)

    return store.in_serializable(body, label=_RETRIEVAL_LABEL)


# ---------------------------------------------------------------------------
# The standing, the outcome, and the adjustment
# ---------------------------------------------------------------------------


def select_standing(cursor: Cursor, procedure_id: UUID) -> float | None:
    """The current standing of one procedure, or None when it names none.

    Read without a lock on purpose. Two adjustments of one procedure conflict on
    this read set under SERIALIZABLE, so one aborts and its retry re-reads the
    committed value and writes a change record describing the transition it
    actually caused. A lock taken here would serialise the writers instead, and the
    change history would then record transitions in an order no reader could
    reconstruct from the values.
    """
    cursor.execute(SELECT_STANDING_QUERY, (procedure_id, _PROCEDURE_KIND))
    row = cursor.fetchone()
    if row is None:
        return None
    return _as_confidence(_column(row, 0, _STANDING_ROW_WIDTH))


def insert_outcome(
    cursor: Cursor,
    procedure_id: UUID,
    session_id: UUID,
    outcome: SessionOutcome,
) -> OutcomeRecord | None:
    """Record one outcome on a caller's cursor, sourced from the Session's own row.

    Args:
        cursor: The cursor the caller's transaction is running on.
        procedure_id: The procedure the Session consumed.
        session_id: The Session whose terminal outcome this is.
        outcome: What the caller asserts that Session reached. Bound as an
            assertion the predicate checks against the stored Session row, never
            as the value written.

    Returns:
        The stored row, or None when the insert wrote nothing. A caller reads the
        context to learn which premise failed.
    """
    cursor.execute(
        INSERT_OUTCOME_STATEMENT,
        (procedure_id, _PROCEDURE_KIND, session_id, _terminal(outcome).value),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return OutcomeRecord(
        id=_as_uuid(_column(row, 0, _OUTCOME_ROW_WIDTH)),
        procedure_id=_as_uuid(row[1]),
        session_id=_as_uuid(row[2]),
        outcome=SessionOutcome(_as_text(row[3])),
        recorded_at=_as_instant(row[4]),
    )


def read_outcome_context(cursor: Cursor, procedure_id: UUID, session_id: UUID) -> OutcomeContext:
    """Why an outcome insert wrote nothing, as one row of four facts."""
    cursor.execute(
        OUTCOME_CONTEXT_QUERY,
        (
            procedure_id,
            _PROCEDURE_KIND,
            session_id,
            procedure_id,
            session_id,
            procedure_id,
            session_id,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            "the outcome diagnosis reported no row, so why the outcome was refused cannot be stated"
        )
    stored = _column(row, 1, _CONTEXT_ROW_WIDTH)
    return OutcomeContext(
        procedures=_as_count(row[0]),
        session_outcome=None if stored is None else SessionOutcome(_as_text(stored)),
        retrievals=_as_count(row[2]),
        outcomes=_as_count(row[3]),
    )


def adjust_standing(cursor: Cursor, procedure_id: UUID, delta: float) -> float:
    """Apply one clamped delta to a procedure's standing on a caller's cursor.

    The arithmetic and the clamp are the cluster's, over the row's own current
    value, so a caller cannot commit a value outside the closed unit interval
    however absurd the delta it presents.

    Returns:
        The standing the statement committed, which equals the prior value when
        clamping absorbed the whole delta.

    Raises:
        StoreError: The identifier names no learned procedure, so there is no
            standing to move.
    """
    cursor.execute(
        ADJUST_STANDING_STATEMENT,
        (CONFIDENCE_FLOOR, CONFIDENCE_CEILING, delta, procedure_id, _PROCEDURE_KIND),
    )
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            "the adjustment moved no standing because no learned procedure carries that identifier"
        )
    return _as_confidence(_column(row, 0, _STANDING_ROW_WIDTH))


def insert_change(
    cursor: Cursor,
    procedure_id: UUID,
    *,
    prior_value: float,
    new_value: float,
    outcome_id: UUID,
) -> ConfidenceChange:
    """Append one change record on a caller's cursor, in the adjustment's transaction.

    Raises:
        StoreError: The record was not written, so a committed movement would
            stand without its justification and the transaction must not commit.
    """
    cursor.execute(
        INSERT_CHANGE_STATEMENT,
        (procedure_id, prior_value, new_value, outcome_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            "the confidence change record was not written, so the adjustment it "
            "justifies must not commit"
        )
    return ConfidenceChange(
        id=_as_uuid(_column(row, 0, _WRITTEN_CHANGE_WIDTH)),
        procedure_id=procedure_id,
        prior_value=prior_value,
        new_value=new_value,
        outcome_id=outcome_id,
        changed_at=_as_instant(row[1]),
    )


def apply_outcome(
    store: MemoryStore,
    procedure_id: UUID,
    session_id: UUID,
    outcome: SessionOutcome,
    *,
    delta: float | None,
) -> OutcomeApplication:
    """Record one outcome and its adjustment in one SERIALIZABLE transaction.

    The delta is the caller's, because which classification moves a standing by
    how much is the tracker's policy rather than this layer's. A delta of None is
    the deliberate non-adjustment: the outcome goes on the record, the standing is
    never read and never written, and no change record can exist.

    The statements run in one order for one reason each: the standing is read
    before the update so the prior value is the value the update started from, the
    outcome is inserted before the change record because the record references it,
    and the update and the record are the two halves of one commit so a moved value
    and its justification cannot disagree.

    Args:
        store: The store the transaction runs on.
        procedure_id: The procedure whose standing this outcome bears on.
        session_id: The Session whose terminal outcome this is.
        outcome: What the caller asserts that Session reached.
        delta: The signed adjustment to apply, or None to apply none.

    Returns:
        What the call did, which is None in every field a caller must not read as
        a movement.

    Raises:
        StoreError: The outcome was refused for a reason other than having already
            been recorded, and the message names which premise failed.
    """
    _terminal(outcome)

    def body(cursor: Cursor) -> OutcomeApplication:
        prior = None if delta is None else select_standing(cursor, procedure_id)
        recorded = insert_outcome(cursor, procedure_id, session_id, outcome)
        if recorded is None:
            context = read_outcome_context(cursor, procedure_id, session_id)
            if not context.already_recorded:
                raise StoreError(f"the outcome was not recorded: {context.reason}")
            return OutcomeApplication(
                outcome=None,
                prior_value=None,
                new_value=None,
                change=None,
            )
        if delta is None or prior is None:
            return OutcomeApplication(
                outcome=recorded,
                prior_value=None,
                new_value=None,
                change=None,
            )
        applied = adjust_standing(cursor, procedure_id, delta)
        change = (
            None
            if applied == prior
            else insert_change(
                cursor,
                procedure_id,
                prior_value=prior,
                new_value=applied,
                outcome_id=recorded.id,
            )
        )
        return OutcomeApplication(
            outcome=recorded,
            prior_value=prior,
            new_value=applied,
            change=change,
        )

    return store.in_serializable(body, label=_OUTCOME_LABEL)


# ---------------------------------------------------------------------------
# The history and the standing summary
# ---------------------------------------------------------------------------


def select_changes(
    cursor: Cursor,
    procedure_id: UUID,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> tuple[ConfidenceChange, ...]:
    """One procedure's change records on a caller's cursor, in change order."""
    cursor.execute(SELECT_CHANGES_QUERY, (procedure_id, _bounded(limit)))
    return tuple(_change_of(row) for row in cursor.fetchall())


def count_retrievals(cursor: Cursor, procedure_id: UUID) -> int:
    """How many times one procedure has been retrieved, as one aggregate count."""
    cursor.execute(COUNT_RETRIEVALS_QUERY, (procedure_id,))
    row = cursor.fetchone()
    if row is None:
        raise StoreError("the retrieval count reported no row, so it is unknown")
    return _as_count(_column(row, 0, _COUNT_ROW_WIDTH))


def select_outcome_counts(
    cursor: Cursor,
    procedure_id: UUID,
) -> tuple[tuple[SessionOutcome, int], ...]:
    """One procedure's outcome counts per classification, every classification present.

    A classification no Session reported reads as zero rather than as an absent
    key, so a caller rendering the three numbers never decides what absence means.
    """
    cursor.execute(SELECT_OUTCOME_COUNTS_QUERY, (procedure_id,))
    counted: dict[SessionOutcome, int] = {}
    for row in cursor.fetchall():
        classification = SessionOutcome(_as_text(_column(row, 0, _OUTCOME_COUNT_WIDTH)))
        counted[classification] = _as_count(row[1])
    return tuple(
        (classification, counted.get(classification, 0))
        for classification in sorted(TERMINAL_OUTCOMES, key=lambda member: member.value)
    )


def change_history(
    store: MemoryStore,
    procedure_id: UUID,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> tuple[ConfidenceChange, ...]:
    """One procedure's audited change history, ordered by change timestamp.

    This is the query the requirement asks the store to expose: the movements of
    one procedure's standing in the order they happened, so a claim that memory
    improved is a result rather than an assertion.
    """

    def body(cursor: Cursor) -> tuple[ConfidenceChange, ...]:
        return select_changes(cursor, procedure_id, limit=limit)

    return store.read(body)


def procedure_standing(store: MemoryStore, procedure_id: UUID) -> ProcedureStanding:
    """One procedure's standing, retrieval count, and outcome counts.

    Raises:
        StoreError: The identifier names no learned procedure, so it has no
            standing to report.
    """

    def body(cursor: Cursor) -> ProcedureStanding:
        confidence = select_standing(cursor, procedure_id)
        if confidence is None:
            raise StoreError(
                "no standing can be reported because no learned procedure carries that identifier"
            )
        return ProcedureStanding(
            procedure_id=procedure_id,
            confidence=confidence,
            retrievals=count_retrievals(cursor, procedure_id),
            outcomes=select_outcome_counts(cursor, procedure_id),
        )

    return store.read(body)


# ---------------------------------------------------------------------------
# Parameters and row decoding
# ---------------------------------------------------------------------------


def _terminal(outcome: SessionOutcome) -> SessionOutcome:
    """The classification unchanged, refusing one no terminal Session reports.

    A Session still in flight has reached no outcome, so there is nothing to
    report and nothing to adjust, and asking is a caller fault rather than a
    refusal the cluster should have to state.
    """
    member = SessionOutcome(outcome)
    if member not in TERMINAL_OUTCOMES:
        raise ValueError("an outcome record names a terminal session classification")
    return member


def _bounded(limit: int) -> int:
    """The row bound to send, refusing one that is not a usable bound."""
    if limit < 1:
        raise ValueError("a read bound must admit at least one record")
    if limit > MAX_HISTORY_LIMIT:
        raise ValueError(f"a read bound may not exceed {MAX_HISTORY_LIMIT} records")
    return limit


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _change_of(row: Sequence[object]) -> ConfidenceChange:
    """Build one change record from a selected row."""
    return ConfidenceChange(
        id=_as_uuid(_column(row, 0, _CHANGE_ROW_WIDTH)),
        procedure_id=_as_uuid(row[1]),
        prior_value=_as_confidence(row[2]),
        new_value=_as_confidence(row[3]),
        outcome_id=_as_uuid(row[4]),
        changed_at=_as_instant(row[5]),
    )


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a column whose type is not the one the schema declares."""
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_count(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a whole number")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a whole number")


def _as_confidence(value: object) -> float:
    if isinstance(value, bool):
        raise _unexpected(value, "a confidence value")
    if isinstance(value, int | float):
        return float(value)
    raise _unexpected(value, "a confidence value")


def _as_instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return require_aware(value, "a selected timestamp")
    raise _unexpected(value, "a timestamp")
