"""Unit tests for the procedural-standing statements, their order, and their refusals.

Nothing here opens a socket. A scripted cursor answers each statement and keeps
what it was sent, so every claim below is asserted by reading the statements the
module produced. The claims that need a cluster to be meaningful -- that the clamp
really holds, that the adjustment and its change record really commit together --
belong to the instance-backed suite.

Seven properties of the shape are checked.

A retrieval moves nothing. The retrieval insert names one table, writes two
columns, and holds no reference to the standing column at all, so there is no
statement by which being shown to an agent could become evidence of being right.

An outcome is the Session's own. The insert selects the classification out of the
`session` row rather than binding it as the stored value, requires a retrieval row
for the pair, and names the per-Session uniqueness constraint as its arbiter, so a
classification a caller made up, an outcome for a procedure the Session never saw,
and a duplicated report are each refused by the cluster rather than by a caller
remembering to check.

The clamp is in the statement. The adjustment assigns one column, computes from the
row's own current value, and binds both interval bounds, so no caller can commit a
value outside the closed unit interval and the column check is a backstop.

The order inside the transaction is the order the guarantees need: the prior value
is read before the update, the outcome is inserted before the record that
references it, and the update and the record are the last two statements before the
commit.

A deliberate non-adjustment sends no adjusting statement at all. An abandoned
outcome is one insert between the isolation level and the commit, with no read of
the standing and no update of it, which is a stronger claim than a delta of zero
having been applied.

An adjustment a bound absorbed writes no change record and is still visible: the
outcome row is on the record and the application reports the absorption, which is
what tells an operator that the adjustment happened and the value was already at a
bound.

A refusal names the premise that failed. The diagnostic read is taken only when the
insert wrote nothing, and each of its four situations produces a distinct message.

**Validates: Requirements 49.1, 49.3, 49.4, 49.5, 49.6, 49.7, 49.12, 49.13, 49.15**
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.errors import StoreError
from molt.models.artifact import CONFIDENCE_CEILING, CONFIDENCE_FLOOR, DerivedArtifactKind
from molt.models.session import SessionOutcome
from molt.models.tiers import MEMORY_TIERS, TIER_NAMES
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.confidence import (
    ADJUST_STANDING_STATEMENT,
    COUNT_RETRIEVALS_QUERY,
    INSERT_CHANGE_STATEMENT,
    INSERT_OUTCOME_STATEMENT,
    INSERT_RETRIEVAL_STATEMENT,
    MAX_HISTORY_LIMIT,
    OUTCOME_CONTEXT_QUERY,
    SELECT_CHANGES_QUERY,
    SELECT_OUTCOME_COUNTS_QUERY,
    SELECT_STANDING_QUERY,
    TERMINAL_OUTCOMES,
    apply_outcome,
    change_history,
    procedure_standing,
    record_retrieval,
)
from molt.store.retry import BEGIN_STATEMENT, COMMIT_STATEMENT, SERIALIZABLE_STATEMENT

# Every statement the module holds, so the containment claim is made over the whole
# set rather than over the ones a test happened to drive.
ALL_STATEMENTS: Final[tuple[str, ...]] = (
    INSERT_RETRIEVAL_STATEMENT,
    SELECT_STANDING_QUERY,
    INSERT_OUTCOME_STATEMENT,
    OUTCOME_CONTEXT_QUERY,
    ADJUST_STANDING_STATEMENT,
    INSERT_CHANGE_STATEMENT,
    SELECT_CHANGES_QUERY,
    COUNT_RETRIEVALS_QUERY,
    SELECT_OUTCOME_COUNTS_QUERY,
)

# The tier the standing belongs to, and the tables the taxonomy assigns to it. Read
# from the taxonomy rather than restated, so this suite and the tier model cannot
# disagree about which tables a movement of a standing may write.
PROCEDURAL_TIER: Final[str] = "procedural_semantic"
WRITABLE_TABLES: Final[frozenset[str]] = frozenset(MEMORY_TIERS[PROCEDURAL_TIER].tables)

# The one table a statement here reads and never writes: the classification comes
# out of the Session's own row.
SESSION_TABLE: Final[str] = "session"

# The words that make a statement a write.
WRITING_WORDS: Final[tuple[str, ...]] = ("INSERT INTO", "UPDATE", "DELETE FROM", "UPSERT INTO")

# The kind that carries a standing, as the model names it.
PROCEDURE_KIND: Final[str] = DerivedArtifactKind.LEARNED_PROCEDURE.value

# Fragments the script matches a statement by, each specific to one statement.
RETRIEVAL_FRAGMENT: Final[str] = "INSERT INTO procedure_retrieval"
STANDING_FRAGMENT: Final[str] = "SELECT procedure_confidence FROM"
OUTCOME_FRAGMENT: Final[str] = "INSERT INTO procedure_outcome"
CONTEXT_FRAGMENT: Final[str] = "SELECT (SELECT count(*) FROM derived_artifact"
ADJUST_FRAGMENT: Final[str] = "UPDATE derived_artifact SET"
CHANGE_FRAGMENT: Final[str] = "INSERT INTO procedure_confidence_change"
HISTORY_FRAGMENT: Final[str] = "ORDER BY changed_at ASC"
RETRIEVAL_COUNT_FRAGMENT: Final[str] = "SELECT count(*) FROM procedure_retrieval"
OUTCOME_COUNT_FRAGMENT: Final[str] = "GROUP BY outcome"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# The identifiers every driven call names.
PROCEDURE_ID: Final[UUID] = uuid4()
SESSION_ID: Final[UUID] = uuid4()
RETRIEVAL_ID: Final[UUID] = uuid4()
OUTCOME_ID: Final[UUID] = uuid4()
CHANGE_ID: Final[UUID] = uuid4()

# The standings the examples move between. None is a bound, so an assertion about a
# movement cannot be satisfied by a clamp that happened to land on the same number.
PRIOR_STANDING: Final[float] = 0.5
RAISED_STANDING: Final[float] = 0.55
LOWERED_STANDING: Final[float] = 0.4

# The deltas the examples apply, distinct from each other and from the surface
# defaults, so nothing asserted here could be satisfied by a constant the module
# held instead of the caller's value.
UPWARD_DELTA: Final[float] = 0.05
DOWNWARD_DELTA: Final[float] = -0.1

# The bound one history read asks for, not the module's default.
HISTORY_LIMIT: Final[int] = 7

# The counts the summary example reads back.
RETRIEVAL_COUNT: Final[int] = 9
SUCCEEDED_COUNT: Final[int] = 4
FAILED_COUNT: Final[int] = 2


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()


@dataclass(slots=True)
class Script:
    """The answers a connection hands out, consumed in the order they match."""

    answers: list[Answer] = field(default_factory=list)
    sent: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    armed: tuple[tuple[object, ...], ...] = ()

    @property
    def statements(self) -> list[str]:
        """Every statement the script was sent, in order."""
        return [query for query, _ in self.sent]

    def issued(self) -> tuple[str, ...]:
        """Every statement a caller sent, with the pool's own statements removed."""
        return tuple(
            query
            for query in self.statements
            if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT)
        )

    def parameters_of(self, statement: str) -> tuple[object, ...] | None:
        """The bound parameters of the one occurrence of a statement."""
        matches = [params for query, params in self.sent if query == statement]
        assert len(matches) == 1, f"the statement should have been sent once, not {len(matches)}"
        return matches[0]

    def take(self, query: str) -> Answer | None:
        """The next answer matching a statement, removed from the script."""
        for index, answer in enumerate(self.answers):
            if answer.fragment in query:
                return self.answers.pop(index)
        return None


class ScriptedCursor:
    """A cursor answering from a script and recording what it was sent."""

    def __init__(self, script: Script) -> None:
        self._script = script
        self.released = False

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then arm the rows the script answers with."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        self._script.armed = () if answer is None else answer.rows
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or None when the statement armed none."""
        rows = self._script.armed
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._script.armed)

    def close(self) -> None:
        """Mark this cursor released."""
        self.released = True


class ScriptedConnection:
    """A connection handing out scripted cursors over one shared script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Open a recording cursor over this connection's script."""
        return ScriptedCursor(self.script)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def retrieval_row() -> tuple[object, ...]:
    """One retrieval row of the width the insert returns."""
    return (RETRIEVAL_ID, PROCEDURE_ID, SESSION_ID, MOMENT)


def outcome_row(outcome: SessionOutcome) -> tuple[object, ...]:
    """One outcome row of the width the insert returns."""
    return (OUTCOME_ID, PROCEDURE_ID, SESSION_ID, outcome.value, MOMENT)


def change_row() -> tuple[object, ...]:
    """The identifier and instant the change insert reports."""
    return (CHANGE_ID, MOMENT)


def context_row(
    *,
    procedures: int = 1,
    session_outcome: str | None = SessionOutcome.SUCCEEDED.value,
    retrievals: int = 1,
    outcomes: int = 0,
) -> tuple[object, ...]:
    """One diagnostic row of the width the context query selects."""
    return (procedures, session_outcome, retrievals, outcomes)


def adjustment_script(
    outcome: SessionOutcome,
    *,
    prior: float,
    applied: float,
) -> Script:
    """The answers a whole recorded outcome with an adjustment consumes."""
    return Script(
        answers=[
            Answer(STANDING_FRAGMENT, ((prior,),)),
            Answer(OUTCOME_FRAGMENT, (outcome_row(outcome),)),
            Answer(ADJUST_FRAGMENT, ((applied,),)),
            Answer(CHANGE_FRAGMENT, (change_row(),)),
        ]
    )


# ---------------------------------------------------------------------------
# What the statements name, and what they cannot
# ---------------------------------------------------------------------------


def test_every_statement_writes_only_inside_the_procedural_tier() -> None:
    """A standing movement writes three tables and the artifact it belongs to."""
    assert PROCEDURAL_TIER in TIER_NAMES
    for statement in ALL_STATEMENTS:
        for word in WRITING_WORDS:
            position = statement.find(word)
            if position < 0:
                continue
            target = statement[position + len(word) :].split()[0]
            assert target in WRITABLE_TABLES, f"{word} {target} is outside the tier"


def test_the_session_table_is_read_and_never_written() -> None:
    """The outcome comes out of the Session row; nothing here writes a Session."""
    naming = [statement for statement in ALL_STATEMENTS if SESSION_TABLE in statement]
    assert naming, "the outcome path reads the session's own recorded outcome"
    for statement in naming:
        for word in WRITING_WORDS:
            assert f"{word} {SESSION_TABLE} " not in f"{statement} "


def test_the_retrieval_insert_holds_no_reference_to_the_standing() -> None:
    """Being retrieved is not evidence of being right, so the statement cannot move it."""
    assert INSERT_RETRIEVAL_STATEMENT.startswith(RETRIEVAL_FRAGMENT)
    assert "procedure_confidence" not in INSERT_RETRIEVAL_STATEMENT
    assert "(procedure_id, session_id)" in INSERT_RETRIEVAL_STATEMENT
    assert INSERT_RETRIEVAL_STATEMENT.count("%s") == 3


def test_the_outcome_insert_sources_the_classification_from_the_session_row() -> None:
    """The stored classification is a column, the caller's is a predicate."""
    assert "SELECT d.id, s.id, s.outcome FROM" in INSERT_OUTCOME_STATEMENT
    assert "s.outcome = %s" in INSERT_OUTCOME_STATEMENT
    assert "EXISTS (SELECT 1 FROM procedure_retrieval" in INSERT_OUTCOME_STATEMENT
    assert "ON CONFLICT (procedure_id, session_id) DO NOTHING" in INSERT_OUTCOME_STATEMENT
    assert "VALUES" not in INSERT_OUTCOME_STATEMENT, "no classification is written as a value"


def test_the_adjustment_clamps_in_the_statement_and_assigns_one_column() -> None:
    """The interval is the cluster's arithmetic and the standing is the only column set."""
    assert "greatest(%s::FLOAT8, least(%s::FLOAT8, procedure_confidence + %s::FLOAT8))" in (
        ADJUST_STANDING_STATEMENT
    )
    assignments = ADJUST_STANDING_STATEMENT.count("=")
    comparisons = ADJUST_STANDING_STATEMENT.count("id = %s") + ADJUST_STANDING_STATEMENT.count(
        "kind = %s"
    )
    assert assignments - comparisons == 1, "only the standing column is assigned"
    assert ADJUST_STANDING_STATEMENT.endswith("RETURNING procedure_confidence")


def test_the_change_record_states_both_endpoints_and_its_cause() -> None:
    """A movement in the history can be traced back to the outcome that produced it."""
    assert "(procedure_id, prior_value, new_value, outcome_id)" in INSERT_CHANGE_STATEMENT
    assert INSERT_CHANGE_STATEMENT.count("%s") == 4


def test_the_history_is_ordered_by_the_change_instant_and_bounded() -> None:
    """The movements come back in the order they happened, and a read is bounded."""
    assert HISTORY_FRAGMENT in SELECT_CHANGES_QUERY
    assert SELECT_CHANGES_QUERY.index("changed_at ASC") < SELECT_CHANGES_QUERY.index("id ASC")
    assert SELECT_CHANGES_QUERY.endswith("LIMIT %s")


# ---------------------------------------------------------------------------
# The retrieval record
# ---------------------------------------------------------------------------


def test_a_retrieval_is_one_statement_in_a_transaction_of_its_own() -> None:
    """One insert between the isolation level and the commit, and nothing else."""
    script = Script(answers=[Answer(RETRIEVAL_FRAGMENT, (retrieval_row(),))])

    written = record_retrieval(build_store(script), PROCEDURE_ID, SESSION_ID)

    assert script.issued() == (
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        INSERT_RETRIEVAL_STATEMENT,
        COMMIT_STATEMENT,
    )
    assert script.parameters_of(INSERT_RETRIEVAL_STATEMENT) == (
        SESSION_ID,
        PROCEDURE_ID,
        PROCEDURE_KIND,
    )
    assert written.id == RETRIEVAL_ID
    assert written.procedure_id == PROCEDURE_ID
    assert written.session_id == SESSION_ID
    assert written.retrieved_at == MOMENT


def test_a_retrieval_of_something_that_is_not_a_procedure_is_refused() -> None:
    """The source row is restricted to the kind, so no row comes back and none is claimed."""
    script = Script()

    with pytest.raises(StoreError, match="no learned procedure"):
        record_retrieval(build_store(script), PROCEDURE_ID, SESSION_ID)


# ---------------------------------------------------------------------------
# The adjustment, its order, and its change record
# ---------------------------------------------------------------------------


def test_a_succeeded_outcome_reads_then_records_then_moves_then_justifies() -> None:
    """Four statements in the one order the guarantees need, inside one transaction."""
    script = adjustment_script(
        SessionOutcome.SUCCEEDED,
        prior=PRIOR_STANDING,
        applied=RAISED_STANDING,
    )

    application = apply_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.SUCCEEDED,
        delta=UPWARD_DELTA,
    )

    assert script.issued() == (
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        SELECT_STANDING_QUERY,
        INSERT_OUTCOME_STATEMENT,
        ADJUST_STANDING_STATEMENT,
        INSERT_CHANGE_STATEMENT,
        COMMIT_STATEMENT,
    )
    assert script.parameters_of(SELECT_STANDING_QUERY) == (PROCEDURE_ID, PROCEDURE_KIND)
    assert script.parameters_of(INSERT_OUTCOME_STATEMENT) == (
        PROCEDURE_ID,
        PROCEDURE_KIND,
        SESSION_ID,
        SessionOutcome.SUCCEEDED.value,
    )
    assert script.parameters_of(ADJUST_STANDING_STATEMENT) == (
        CONFIDENCE_FLOOR,
        CONFIDENCE_CEILING,
        UPWARD_DELTA,
        PROCEDURE_ID,
        PROCEDURE_KIND,
    )
    assert script.parameters_of(INSERT_CHANGE_STATEMENT) == (
        PROCEDURE_ID,
        PRIOR_STANDING,
        RAISED_STANDING,
        OUTCOME_ID,
    )
    assert application.change is not None
    assert application.change.prior_value == PRIOR_STANDING
    assert application.change.new_value == RAISED_STANDING
    assert application.change.outcome_id == OUTCOME_ID
    assert not application.absorbed


def test_a_failed_outcome_binds_the_negative_delta_the_caller_presented() -> None:
    """Which way a classification moves a standing is the caller's policy, not this layer's."""
    script = adjustment_script(
        SessionOutcome.FAILED,
        prior=PRIOR_STANDING,
        applied=LOWERED_STANDING,
    )

    application = apply_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.FAILED,
        delta=DOWNWARD_DELTA,
    )

    assert script.parameters_of(ADJUST_STANDING_STATEMENT) == (
        CONFIDENCE_FLOOR,
        CONFIDENCE_CEILING,
        DOWNWARD_DELTA,
        PROCEDURE_ID,
        PROCEDURE_KIND,
    )
    assert application.change is not None
    assert application.change.applied_delta == pytest.approx(DOWNWARD_DELTA)
    assert application.outcome is not None
    assert application.outcome.outcome is SessionOutcome.FAILED


def test_a_deliberate_non_adjustment_sends_no_adjusting_statement() -> None:
    """An abandoned outcome is one insert; the standing is neither read nor written."""
    script = Script(
        answers=[Answer(OUTCOME_FRAGMENT, (outcome_row(SessionOutcome.ABANDONED),))],
    )

    application = apply_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.ABANDONED,
        delta=None,
    )

    assert script.issued() == (
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        INSERT_OUTCOME_STATEMENT,
        COMMIT_STATEMENT,
    )
    assert application.recorded
    assert not application.attempted
    assert not application.absorbed
    assert application.change is None


def test_an_adjustment_a_bound_absorbed_writes_no_change_record() -> None:
    """The value did not move, so a record claiming a movement is not written."""
    script = Script(
        answers=[
            Answer(STANDING_FRAGMENT, ((CONFIDENCE_CEILING,),)),
            Answer(OUTCOME_FRAGMENT, (outcome_row(SessionOutcome.SUCCEEDED),)),
            Answer(ADJUST_FRAGMENT, ((CONFIDENCE_CEILING,),)),
        ]
    )

    application = apply_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.SUCCEEDED,
        delta=UPWARD_DELTA,
    )

    assert INSERT_CHANGE_STATEMENT not in script.statements
    assert application.absorbed, "the adjustment was applied and a bound took all of it"
    assert application.recorded, "the outcome is still on the record"
    assert application.prior_value == CONFIDENCE_CEILING
    assert application.new_value == CONFIDENCE_CEILING
    assert application.change is None


def test_a_repeated_report_changes_nothing_and_raises_nothing() -> None:
    """The arbiter refuses the second report, so no second adjustment is applied."""
    script = Script(
        answers=[
            Answer(STANDING_FRAGMENT, ((PRIOR_STANDING,),)),
            Answer(CONTEXT_FRAGMENT, (context_row(outcomes=1),)),
        ]
    )

    application = apply_outcome(
        build_store(script),
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.SUCCEEDED,
        delta=UPWARD_DELTA,
    )

    assert ADJUST_STANDING_STATEMENT not in script.statements
    assert INSERT_CHANGE_STATEMENT not in script.statements
    assert not application.recorded
    assert application.change is None
    assert script.parameters_of(OUTCOME_CONTEXT_QUERY) == (
        PROCEDURE_ID,
        PROCEDURE_KIND,
        SESSION_ID,
        PROCEDURE_ID,
        SESSION_ID,
        PROCEDURE_ID,
        SESSION_ID,
    )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (context_row(procedures=0), "no learned procedure"),
        (context_row(session_outcome=None), "no session carries"),
        (context_row(retrievals=0), "never retrieved"),
        (context_row(session_outcome=SessionOutcome.FAILED.value), "own recorded outcome"),
    ],
    ids=["absent_procedure", "absent_session", "no_retrieval", "disagreeing_classification"],
)
def test_a_refused_outcome_names_the_premise_that_failed(
    row: tuple[object, ...],
    expected: str,
) -> None:
    """Four situations produce four messages, so a caller learns which one it hit."""
    script = Script(
        answers=[
            Answer(STANDING_FRAGMENT, ((PRIOR_STANDING,),)),
            Answer(CONTEXT_FRAGMENT, (row,)),
        ]
    )

    with pytest.raises(StoreError, match=expected):
        apply_outcome(
            build_store(script),
            PROCEDURE_ID,
            SESSION_ID,
            SessionOutcome.SUCCEEDED,
            delta=UPWARD_DELTA,
        )

    assert ADJUST_STANDING_STATEMENT not in script.statements


def test_an_outcome_that_no_terminal_session_reports_is_refused_before_any_statement() -> None:
    """A Session in flight has reached no outcome, so there is nothing to record."""
    script = Script()

    with pytest.raises(ValueError, match="terminal session classification"):
        apply_outcome(
            build_store(script),
            PROCEDURE_ID,
            SESSION_ID,
            SessionOutcome.IN_PROGRESS,
            delta=UPWARD_DELTA,
        )

    assert script.statements == []
    assert SessionOutcome.IN_PROGRESS not in TERMINAL_OUTCOMES


# ---------------------------------------------------------------------------
# The history and the standing summary
# ---------------------------------------------------------------------------


def test_the_history_read_frames_no_transaction_and_binds_the_bound() -> None:
    """One statement on a leased connection, in change order, bounded by a row limit."""
    script = Script(
        answers=[
            Answer(
                HISTORY_FRAGMENT,
                ((CHANGE_ID, PROCEDURE_ID, PRIOR_STANDING, RAISED_STANDING, OUTCOME_ID, MOMENT),),
            )
        ]
    )

    records = change_history(build_store(script), PROCEDURE_ID, limit=HISTORY_LIMIT)

    assert script.issued() == (SELECT_CHANGES_QUERY,)
    assert script.parameters_of(SELECT_CHANGES_QUERY) == (PROCEDURE_ID, HISTORY_LIMIT)
    assert len(records) == 1
    assert records[0].prior_value == PRIOR_STANDING
    assert records[0].new_value == RAISED_STANDING
    assert records[0].applied_delta == pytest.approx(RAISED_STANDING - PRIOR_STANDING)


@pytest.mark.parametrize("limit", [0, -1, MAX_HISTORY_LIMIT + 1])
def test_a_history_read_refuses_a_bound_that_is_not_a_usable_bound(limit: int) -> None:
    """No caller asks for an unbounded scan of a long-lived procedure's history."""
    script = Script()

    with pytest.raises(ValueError, match="read bound"):
        change_history(build_store(script), PROCEDURE_ID, limit=limit)

    assert script.issued() == (), "the bound is refused before any statement is sent"


def test_the_summary_reports_every_classification_including_the_absent_ones() -> None:
    """Three numbers with no gaps, so a caller never decides what an absent key means."""
    script = Script(
        answers=[
            Answer(STANDING_FRAGMENT, ((PRIOR_STANDING,),)),
            Answer(RETRIEVAL_COUNT_FRAGMENT, ((RETRIEVAL_COUNT,),)),
            Answer(
                OUTCOME_COUNT_FRAGMENT,
                (
                    (SessionOutcome.FAILED.value, FAILED_COUNT),
                    (SessionOutcome.SUCCEEDED.value, SUCCEEDED_COUNT),
                ),
            ),
        ]
    )

    standing = procedure_standing(build_store(script), PROCEDURE_ID)

    assert script.issued() == (
        SELECT_STANDING_QUERY,
        COUNT_RETRIEVALS_QUERY,
        SELECT_OUTCOME_COUNTS_QUERY,
    )
    assert standing.confidence == PRIOR_STANDING
    assert standing.retrievals == RETRIEVAL_COUNT
    assert standing.count_of(SessionOutcome.SUCCEEDED) == SUCCEEDED_COUNT
    assert standing.count_of(SessionOutcome.FAILED) == FAILED_COUNT
    assert standing.count_of(SessionOutcome.ABANDONED) == 0
    assert len(standing.outcomes) == len(TERMINAL_OUTCOMES)


def test_a_summary_of_something_that_is_not_a_procedure_is_refused() -> None:
    """A kind that carries no standing has none to report, and says so."""
    script = Script()

    with pytest.raises(StoreError, match="no learned procedure"):
        procedure_standing(build_store(script), PROCEDURE_ID)
