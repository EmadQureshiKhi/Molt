"""Unit tests for the attribution statements, the ordering, and the refusals.

Nothing here opens a socket. A scripted cursor answers each statement from a
script and keeps what it was sent, so every claim below is asserted by reading
what the module produced. The claims that need a cluster to mean anything, that
the partial uniqueness really admits a history and that the closure and the
insert really commit together, are asserted in the instance-backed module.

Six properties of the shape are checked here.

A supersession is two statements in a fixed order. The closing update goes out
before the successor's insert, both on one cursor inside one serializable
transaction, and neither is a common table expression over the other. The
closing statement names the successor's identifier, which the module generated
before it ran, so the reference it writes points at a row that does not exist
yet.

The closure writes two columns and no others. The assignment names the validity
end and the superseding reference alone, which is exactly the pair the
database-side guard leaves writable, so the module holds no path by which a
stored version's method, confidence, Artifact, or Client could change.

The greater-confidence rule is the cluster's. The successor's insert carries
`greatest` over two bound values, the submitted confidence and the confidence the
closing statement returned, so a supersession never lowers a claim and no
arithmetic happens in application memory.

The Ledger Event is appended on the same cursor as both writes. The append is the
chain's own single statement, it goes out after the successor's insert and before
the commit, and its payload names the Artifact, the Client, and both version
identifiers.

A restatement is refused rather than performed. Presenting the identifier of the
version being superseded, re-using an identifier a stored version already holds,
and the guard's own refusal each raise the immutability failure, while a refusal
of the partial uniqueness is reported as the different thing it is.

Every caller-supplied value is bound and every current-form statement in the
layer carries one predicate. The two tenancy terms of the neighbour query are
checked against the same canonical predicate as this module's three reads, which
is what keeps *current* meaning one thing across the layer.

**Validates: Requirements 12.7, 43.1, 43.2, 43.3, 43.5, 43.8**
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.errors import AttributionImmutableError, StoreError
from molt.models.artifact import ArtifactKind
from molt.models.binding import BindingMethod
from molt.models.event import EventCategory
from molt.store import Connection, MemoryStore
from molt.store.attribution import (
    ATTRIBUTION_AS_OF_QUERY,
    CLOSE_CURRENT_VERSION_STATEMENT,
    CURRENT_ATTRIBUTION_QUERY,
    CURRENT_PAIR_QUERY,
    CURRENT_UNIQUE_INDEX,
    CURRENT_VERSION_PREDICATE,
    FIRST_ATTRIBUTION_QUERY,
    IMMUTABILITY_GUARD_MESSAGE,
    INSERT_ERASURE_MARKER_STATEMENT,
    INSERT_SUCCESSOR_STATEMENT,
    INSERT_VERSION_STATEMENT,
    RAISED_EXCEPTION_STATE,
    SUPERSESSION_REASON,
    UNIQUE_VIOLATION_STATE,
    WITHDRAWAL_REASON,
    AttributionOutcome,
    AttributionSubmission,
    SupersessionContext,
    attribution_as_of,
    current_attribution,
    first_attributions,
    remove_attribution,
    write_attribution,
)
from molt.store.chain import APPEND_STATEMENT
from molt.store.embeddings import NEAREST_SCAN_STATEMENT, NEAREST_STATEMENT
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
)

# The three reads of this module that restrict to the current claim, plus the two
# tenancy terms of the neighbour query. One predicate has to serve all five.
CURRENT_FORM_TEXTS: Final[tuple[str, ...]] = (
    CLOSE_CURRENT_VERSION_STATEMENT,
    CURRENT_PAIR_QUERY,
    CURRENT_ATTRIBUTION_QUERY,
    NEAREST_STATEMENT,
    NEAREST_SCAN_STATEMENT,
)

# The identifiers every driven write names, fixed so an expected bound tuple names
# the same values the operation was given.
ARTIFACT_ID: Final[UUID] = uuid4()
CLIENT_ID: Final[UUID] = uuid4()
SESSION_ID: Final[UUID] = uuid4()
PRIOR_ID: Final[UUID] = uuid4()
SUCCESSOR_ID: Final[UUID] = uuid4()

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
CLOSED_AT: Final[datetime] = MOMENT + timedelta(seconds=1)
RETENTION: Final[timedelta] = timedelta(days=90)

# The confidences the cases use: the prior claim, a stronger submission, and a
# weaker one, each distinct so no expectation is satisfied by another's value.
PRIOR_CONFIDENCE: Final[float] = 0.6
STRONGER_CONFIDENCE: Final[float] = 0.9
WEAKER_CONFIDENCE: Final[float] = 0.3

# How many values each write statement binds.
FIRST_INSERT_PARAMETERS: Final[int] = 7
CLOSE_PARAMETERS: Final[int] = 3
SUCCESSOR_PARAMETERS: Final[int] = 8
MARKER_PARAMETERS: Final[int] = 8

# The row shapes the scripts answer with, each of the width its statement selects.
CLOSED_ROW: Final[tuple[object, ...]] = (
    PRIOR_ID,
    ArtifactKind.DERIVED_ARTIFACT.value,
    BindingMethod.SCOPE.value,
    PRIOR_CONFIDENCE,
    MOMENT,
    CLOSED_AT,
)
APPENDED_ROW: Final[tuple[object, ...]] = (4, "b" * 64, "0" * 64, "c" * 64)

CONTEXT: Final[SupersessionContext] = SupersessionContext(
    session_id=SESSION_ID,
    agent_cli="molt",
    machine_id="machine",
    expires_at=MOMENT + RETENTION,
)


def submission(
    *,
    method: BindingMethod = BindingMethod.SCOPE,
    confidence: float = STRONGER_CONFIDENCE,
) -> AttributionSubmission:
    """One detection result for the module's fixed Artifact and Client pair."""
    return AttributionSubmission(
        artifact_id=ARTIFACT_ID,
        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
        client_id=CLIENT_ID,
        method=method,
        confidence=confidence,
        detected_at=MOMENT,
    )


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()
    error: Exception | None = None


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

    def parameters_of(self, statement: str) -> tuple[object, ...]:
        """The bound parameters of the one occurrence of a statement."""
        matches = [params for query, params in self.sent if query == statement]
        assert len(matches) == 1, f"the statement should have been sent once, not {len(matches)}"
        bound = matches[0]
        assert bound is not None, "the statement should have carried bound parameters"
        return bound

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
        """Record the statement, then raise or arm rows as the script says."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        if answer is None:
            self._script.armed = ()
            return None
        if answer.error is not None:
            raise answer.error
        self._script.armed = answer.rows
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


class DriverFailureError(Exception):
    """A driver failure carrying the state and the constraint a driver reports."""

    def __init__(self, sqlstate: str, constraint_name: str | None, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = _Diagnostic(constraint_name)


class _Diagnostic:
    """The diagnostic attribute a driver failure carries the constraint under."""

    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def supersession_script(*, written_confidence: float = STRONGER_CONFIDENCE) -> Script:
    """The answers a whole supersession needs: the pair read, both writes, the append."""
    return Script(
        answers=[
            Answer(
                "SELECT id, method, confidence FROM client_binding",
                ((PRIOR_ID, BindingMethod.SCOPE.value, PRIOR_CONFIDENCE),),
            ),
            Answer("UPDATE client_binding SET valid_to", (CLOSED_ROW,)),
            Answer(
                "VALUES (%s, %s, %s, %s, %s, greatest(",
                ((SUCCESSOR_ID, written_confidence, CLOSED_AT),),
            ),
            Answer("INSERT INTO ledger", (APPENDED_ROW,)),
        ]
    )


# ---------------------------------------------------------------------------
# The shape of the two statements
# ---------------------------------------------------------------------------


def test_the_supersession_is_two_statements_and_neither_wraps_the_other() -> None:
    """An update and an insert, each whole, with no common table expression."""
    assert CLOSE_CURRENT_VERSION_STATEMENT.startswith("UPDATE client_binding SET valid_to = now()")
    assert INSERT_SUCCESSOR_STATEMENT.startswith("INSERT INTO client_binding (")
    for statement in (CLOSE_CURRENT_VERSION_STATEMENT, INSERT_SUCCESSOR_STATEMENT):
        assert "WITH" not in statement.upper()
        assert statement.upper().count("INSERT") + statement.upper().count("UPDATE") == 1
    assert CLOSE_CURRENT_VERSION_STATEMENT.count("%s") == CLOSE_PARAMETERS
    assert INSERT_SUCCESSOR_STATEMENT.count("%s") == SUCCESSOR_PARAMETERS


def test_the_closure_writes_the_two_columns_the_guard_leaves_writable() -> None:
    """A stored version's immutable columns appear in no assignment of this module."""
    assignment = CLOSE_CURRENT_VERSION_STATEMENT.split(" WHERE ", maxsplit=1)[0]
    assert assignment.endswith("valid_to = now(), superseded_by = %s")
    for column in ("artifact_id =", "artifact_kind =", "client_id =", "method =", "confidence ="):
        assert column not in assignment
    assert CURRENT_VERSION_PREDICATE in CLOSE_CURRENT_VERSION_STATEMENT


def test_the_closure_returns_what_the_successor_and_the_event_need() -> None:
    """The closed identifier, its kind, its method, its confidence, and its interval."""
    assert CLOSE_CURRENT_VERSION_STATEMENT.endswith(
        "RETURNING id, artifact_kind, method, confidence, valid_from, valid_to"
    )


def test_the_greater_confidence_rule_is_evaluated_by_the_cluster() -> None:
    """`greatest` over two bound values, so no confidence arithmetic reaches memory."""
    assert "greatest(%s::FLOAT8, %s::FLOAT8)" in INSERT_SUCCESSOR_STATEMENT
    assert "greatest" not in INSERT_VERSION_STATEMENT
    assert "greatest" not in INSERT_ERASURE_MARKER_STATEMENT


def test_the_terminal_marker_carries_an_empty_interval_and_a_reference() -> None:
    """A withdrawal's marker is closed in both columns and contained by no instant."""
    assert "valid_from, valid_to, superseded_by)" in INSERT_ERASURE_MARKER_STATEMENT
    assert "now(), now(), %s)" in INSERT_ERASURE_MARKER_STATEMENT
    assert INSERT_ERASURE_MARKER_STATEMENT.count("%s") == MARKER_PARAMETERS


def test_the_as_of_query_is_half_open_and_ordered_by_client() -> None:
    """Inclusive at the start, exclusive at the end, one version per Client."""
    assert "valid_from <= %s" in ATTRIBUTION_AS_OF_QUERY
    assert "(valid_to IS NULL OR valid_to > %s)" in ATTRIBUTION_AS_OF_QUERY
    assert ATTRIBUTION_AS_OF_QUERY.endswith("ORDER BY client_id")
    assert CURRENT_VERSION_PREDICATE not in ATTRIBUTION_AS_OF_QUERY


def test_the_earliest_version_query_reads_the_first_instant_and_its_method() -> None:
    """One grouped read over a bound Artifact array, per Client."""
    assert "min(valid_from)" in FIRST_ATTRIBUTION_QUERY
    assert "(array_agg(method ORDER BY valid_from))[1]" in FIRST_ATTRIBUTION_QUERY
    assert "artifact_id = ANY (%s::UUID[])" in FIRST_ATTRIBUTION_QUERY
    assert "GROUP BY artifact_id" in FIRST_ATTRIBUTION_QUERY


@pytest.mark.parametrize("text", CURRENT_FORM_TEXTS)
def test_every_current_form_statement_carries_one_predicate(text: str) -> None:
    """One canonical term for *current*, in this module's reads and in the vector filter."""
    assert CURRENT_VERSION_PREDICATE in text


# ---------------------------------------------------------------------------
# The ordering, the parameters, and the Event
# ---------------------------------------------------------------------------


def test_a_supersession_closes_before_it_inserts_in_one_transaction() -> None:
    """The order is the point: the closure names a row the next statement writes."""
    script = supersession_script()

    written = write_attribution(build_store(script), submission(), context=CONTEXT)

    assert written.outcome is AttributionOutcome.SUPERSEDED
    assert written.superseded_id == PRIOR_ID
    statements = script.statements
    assert statements.count(BEGIN_STATEMENT) == 1
    assert statements.index(SERIALIZABLE_STATEMENT) < statements.index(CURRENT_PAIR_QUERY)
    assert statements.index(CURRENT_PAIR_QUERY) < statements.index(CLOSE_CURRENT_VERSION_STATEMENT)
    assert statements.index(CLOSE_CURRENT_VERSION_STATEMENT) < statements.index(
        INSERT_SUCCESSOR_STATEMENT
    )
    assert statements.index(INSERT_SUCCESSOR_STATEMENT) < statements.index(APPEND_STATEMENT)
    assert statements.index(APPEND_STATEMENT) < statements.index(COMMIT_STATEMENT)


def test_the_closure_names_the_successor_the_insert_then_writes() -> None:
    """One generated identifier, bound into the closure before the row exists."""
    script = supersession_script()

    write_attribution(build_store(script), submission(), context=CONTEXT, version_id=SUCCESSOR_ID)

    assert script.parameters_of(CLOSE_CURRENT_VERSION_STATEMENT) == (
        SUCCESSOR_ID,
        ARTIFACT_ID,
        CLIENT_ID,
    )
    assert script.parameters_of(INSERT_SUCCESSOR_STATEMENT) == (
        SUCCESSOR_ID,
        ARTIFACT_ID,
        ArtifactKind.DERIVED_ARTIFACT.value,
        CLIENT_ID,
        BindingMethod.SCOPE.value,
        STRONGER_CONFIDENCE,
        PRIOR_CONFIDENCE,
        MOMENT,
    )


def test_the_successor_is_bound_the_prior_confidence_the_closure_returned() -> None:
    """The rule reads from the version that was closed, not from a caller's memory."""
    script = supersession_script()

    write_attribution(
        build_store(script),
        submission(method=BindingMethod.MARKER, confidence=WEAKER_CONFIDENCE),
        context=CONTEXT,
    )

    bound = script.parameters_of(INSERT_SUCCESSOR_STATEMENT)
    assert bound[5] == WEAKER_CONFIDENCE, "the submitted confidence is the first operand"
    assert bound[6] == PRIOR_CONFIDENCE, "the closed version's confidence is the second"


def test_the_supersession_event_is_appended_on_the_same_cursor() -> None:
    """One Event, in the same transaction, naming both versions and the pair."""
    script = supersession_script()

    written = write_attribution(
        build_store(script), submission(), context=CONTEXT, version_id=SUCCESSOR_ID
    )

    bound = script.parameters_of(APPEND_STATEMENT)
    assert bound[0] == SESSION_ID, "the append reads the tip of the context's Session"
    assert written.event_id == bound[11]
    assert bound[13] == CLIENT_ID
    assert bound[14] == EventCategory.ATTRIBUTION_SUPERSEDED.value
    payload = str(bound[19])
    assert str(ARTIFACT_ID) in payload
    assert str(CLIENT_ID) in payload
    assert str(PRIOR_ID) in payload
    assert str(written.version_id) in payload
    assert SUPERSESSION_REASON in payload


def test_the_event_records_the_instant_the_closure_wrote() -> None:
    """The Event happened when the supersession did, not when it was composed."""
    script = supersession_script()

    write_attribution(build_store(script), submission(), context=CONTEXT)

    assert script.parameters_of(APPEND_STATEMENT)[15] == CLOSED_AT


def test_a_first_write_inserts_one_row_and_appends_no_event() -> None:
    """A first attribution supersedes nothing, so it records no supersession."""
    version_id = uuid4()
    script = Script(
        answers=[
            Answer("SELECT id, method, confidence FROM client_binding", ()),
            Answer("INSERT INTO client_binding", ((version_id, WEAKER_CONFIDENCE, MOMENT),)),
        ]
    )

    written = write_attribution(
        build_store(script),
        submission(confidence=WEAKER_CONFIDENCE),
        context=CONTEXT,
        version_id=version_id,
    )

    assert written.outcome is AttributionOutcome.INSERTED
    assert written.version_id == version_id
    assert written.event_id is None
    assert APPEND_STATEMENT not in script.statements
    assert CLOSE_CURRENT_VERSION_STATEMENT not in script.statements
    assert script.parameters_of(INSERT_VERSION_STATEMENT) == (
        version_id,
        ARTIFACT_ID,
        ArtifactKind.DERIVED_ARTIFACT.value,
        CLIENT_ID,
        BindingMethod.SCOPE.value,
        WEAKER_CONFIDENCE,
        MOMENT,
    )
    assert INSERT_VERSION_STATEMENT.count("%s") == FIRST_INSERT_PARAMETERS


@pytest.mark.parametrize(
    "confidence",
    [WEAKER_CONFIDENCE, PRIOR_CONFIDENCE],
    ids=["lower", "equal"],
)
def test_a_repeated_write_saying_nothing_new_writes_nothing(confidence: float) -> None:
    """Same method and no greater confidence leaves the current version alone."""
    script = Script(
        answers=[
            Answer(
                "SELECT id, method, confidence FROM client_binding",
                ((PRIOR_ID, BindingMethod.SCOPE.value, PRIOR_CONFIDENCE),),
            )
        ]
    )

    written = write_attribution(
        build_store(script),
        submission(confidence=confidence),
        context=CONTEXT,
    )

    assert written.outcome is AttributionOutcome.UNCHANGED
    assert written.version_id == PRIOR_ID
    assert written.confidence == PRIOR_CONFIDENCE
    assert CLOSE_CURRENT_VERSION_STATEMENT not in script.statements
    assert INSERT_SUCCESSOR_STATEMENT not in script.statements
    assert APPEND_STATEMENT not in script.statements


def test_a_differing_method_supersedes_at_the_same_confidence() -> None:
    """The method is part of the claim, so changing it is a change worth a version."""
    script = supersession_script(written_confidence=PRIOR_CONFIDENCE)

    written = write_attribution(
        build_store(script),
        submission(method=BindingMethod.INHERITED, confidence=PRIOR_CONFIDENCE),
        context=CONTEXT,
    )

    assert written.outcome is AttributionOutcome.SUPERSEDED
    assert script.parameters_of(INSERT_SUCCESSOR_STATEMENT)[4] == BindingMethod.INHERITED.value


# ---------------------------------------------------------------------------
# The withdrawal
# ---------------------------------------------------------------------------


def test_a_withdrawal_closes_and_marks_rather_than_deleting() -> None:
    """No delete, one closure, one terminal marker, and one Event."""
    marker = uuid4()
    script = Script(
        answers=[
            Answer("UPDATE client_binding SET valid_to", (CLOSED_ROW,)),
            Answer(
                "valid_from, valid_to, superseded_by)", ((marker, PRIOR_CONFIDENCE, CLOSED_AT),)
            ),
            Answer("INSERT INTO ledger", (APPENDED_ROW,)),
        ]
    )

    written = remove_attribution(
        build_store(script),
        ARTIFACT_ID,
        CLIENT_ID,
        context=CONTEXT,
        marker_id=marker,
    )

    assert written is not None
    assert written.outcome is AttributionOutcome.WITHDRAWN
    assert written.version_id == marker
    assert written.superseded_id == PRIOR_ID
    assert not any("DELETE" in statement.upper() for statement in script.statements)
    assert script.parameters_of(INSERT_ERASURE_MARKER_STATEMENT) == (
        marker,
        ARTIFACT_ID,
        ArtifactKind.DERIVED_ARTIFACT.value,
        CLIENT_ID,
        BindingMethod.SCOPE.value,
        PRIOR_CONFIDENCE,
        CLOSED_AT,
        PRIOR_ID,
    )
    assert WITHDRAWAL_REASON in str(script.parameters_of(APPEND_STATEMENT)[19])


def test_withdrawing_a_pair_holding_no_current_version_writes_nothing() -> None:
    """A repeated erasure of the same Artifact is idempotent rather than a failure."""
    script = Script(answers=[Answer("UPDATE client_binding SET valid_to", ())])

    assert remove_attribution(build_store(script), ARTIFACT_ID, CLIENT_ID, context=CONTEXT) is None
    assert INSERT_ERASURE_MARKER_STATEMENT not in script.statements
    assert APPEND_STATEMENT not in script.statements


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_presenting_the_stored_identifier_is_refused_as_a_restatement() -> None:
    """Superseding a version writes a further version rather than restating that one."""
    script = Script(
        answers=[
            Answer(
                "SELECT id, method, confidence FROM client_binding",
                ((PRIOR_ID, BindingMethod.SCOPE.value, PRIOR_CONFIDENCE),),
            )
        ]
    )

    with pytest.raises(AttributionImmutableError, match="immutable"):
        write_attribution(build_store(script), submission(), context=CONTEXT, version_id=PRIOR_ID)

    assert CLOSE_CURRENT_VERSION_STATEMENT not in script.statements
    assert ROLLBACK_STATEMENT in script.statements


def test_the_database_guard_refusal_is_reported_as_immutability() -> None:
    """The guard is the enforcement, and its own words are what name the refusal."""
    script = Script(
        answers=[
            Answer("SELECT id, method, confidence FROM client_binding", ()),
            Answer(
                "INSERT INTO client_binding",
                error=DriverFailureError(
                    RAISED_EXCEPTION_STATE,
                    None,
                    IMMUTABILITY_GUARD_MESSAGE + " and may only be closed",
                ),
            ),
        ]
    )

    with pytest.raises(AttributionImmutableError, match="may only be closed"):
        write_attribution(build_store(script), submission(), context=CONTEXT)


def test_reusing_a_stored_identifier_is_reported_as_immutability() -> None:
    """The primary key refuses an identifier a stored version already holds."""
    script = Script(
        answers=[
            Answer("SELECT id, method, confidence FROM client_binding", ()),
            Answer(
                "INSERT INTO client_binding",
                error=DriverFailureError(
                    UNIQUE_VIOLATION_STATE, "client_binding_pkey", "duplicate key value"
                ),
            ),
        ]
    )

    with pytest.raises(AttributionImmutableError, match="already holds"):
        write_attribution(build_store(script), submission(), context=CONTEXT)


def test_a_second_current_version_is_reported_as_the_different_thing_it_is() -> None:
    """The partial uniqueness refusing a pair is a concurrent write, not a restatement."""
    script = Script(
        answers=[
            Answer("SELECT id, method, confidence FROM client_binding", ()),
            Answer(
                "INSERT INTO client_binding",
                error=DriverFailureError(
                    UNIQUE_VIOLATION_STATE, CURRENT_UNIQUE_INDEX, "duplicate key value"
                ),
            ),
        ]
    )

    with pytest.raises(StoreError, match="already holds a current attribution version"):
        write_attribution(build_store(script), submission(), context=CONTEXT)


def test_a_refusal_this_module_does_not_name_propagates_untouched() -> None:
    """A failure with no name here is not renamed into something it is not."""
    script = Script(
        answers=[
            Answer("SELECT id, method, confidence FROM client_binding", ()),
            Answer(
                "INSERT INTO client_binding",
                error=DriverFailureError("42601", None, "syntax error"),
            ),
        ]
    )

    with pytest.raises(DriverFailureError):
        write_attribution(build_store(script), submission(), context=CONTEXT)


def test_a_vanished_current_version_is_reported_rather_than_written_around() -> None:
    """The closure matching no row after the decision read is a conflict, not a first write."""
    script = Script(
        answers=[
            Answer(
                "SELECT id, method, confidence FROM client_binding",
                ((PRIOR_ID, BindingMethod.SCOPE.value, PRIOR_CONFIDENCE),),
            ),
            Answer("UPDATE client_binding SET valid_to", ()),
        ]
    )

    with pytest.raises(StoreError, match="another transaction"):
        write_attribution(build_store(script), submission(), context=CONTEXT)

    assert INSERT_SUCCESSOR_STATEMENT not in script.statements


def test_a_submission_outside_the_unit_interval_sends_no_statement() -> None:
    """A confidence the schema would refuse is refused before anything is sent."""
    with pytest.raises(ValueError, match="closed interval"):
        submission(confidence=1.5)


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


def test_the_reads_frame_no_transaction_and_bind_every_value() -> None:
    """A read needs no explicit transaction, so none is opened for one."""
    script = Script(
        answers=[
            Answer(
                CURRENT_ATTRIBUTION_QUERY,
                ((PRIOR_ID, CLIENT_ID, BindingMethod.MARKER.value, PRIOR_CONFIDENCE, MOMENT),),
            )
        ]
    )

    versions = current_attribution(build_store(script), ARTIFACT_ID)

    assert len(versions) == 1
    assert versions[0].client_id == CLIENT_ID
    assert versions[0].method is BindingMethod.MARKER
    assert script.parameters_of(CURRENT_ATTRIBUTION_QUERY) == (ARTIFACT_ID,)
    assert BEGIN_STATEMENT not in script.statements


def test_the_as_of_read_binds_one_instant_twice() -> None:
    """One instant, both ends of the half-open containment predicate."""
    script = Script(
        answers=[
            Answer(
                ATTRIBUTION_AS_OF_QUERY,
                ((PRIOR_ID, CLIENT_ID, BindingMethod.SCOPE.value, PRIOR_CONFIDENCE, MOMENT, None),),
            )
        ]
    )

    versions = attribution_as_of(build_store(script), ARTIFACT_ID, CLOSED_AT)

    assert versions[0].valid_to is None
    assert script.parameters_of(ATTRIBUTION_AS_OF_QUERY) == (ARTIFACT_ID, CLOSED_AT, CLOSED_AT)


def test_the_earliest_version_read_sends_one_bound_array() -> None:
    """Many Artifacts cost one statement, and a repeated Artifact is collapsed."""
    other = uuid4()
    script = Script(
        answers=[
            Answer(
                FIRST_ATTRIBUTION_QUERY,
                ((ARTIFACT_ID, MOMENT, BindingMethod.INHERITED.value),),
            )
        ]
    )

    earliest = first_attributions(build_store(script), CLIENT_ID, [ARTIFACT_ID, other, ARTIFACT_ID])

    assert earliest[0].first_method is BindingMethod.INHERITED
    assert earliest[0].first_attributed_at == MOMENT
    bound = script.parameters_of(FIRST_ATTRIBUTION_QUERY)
    assert bound == (CLIENT_ID, [ARTIFACT_ID, other])


def test_an_earliest_version_read_over_no_artifact_sends_no_statement() -> None:
    """An empty Artifact set is answered without a round trip."""
    script = Script()

    assert first_attributions(build_store(script), CLIENT_ID, []) == ()
    assert FIRST_ATTRIBUTION_QUERY not in script.statements


def test_a_row_of_the_wrong_width_is_refused() -> None:
    """A statement and its decoder cannot drift apart silently."""
    script = Script(answers=[Answer(CURRENT_ATTRIBUTION_QUERY, ((PRIOR_ID, CLIENT_ID),))])

    with pytest.raises(StoreError, match="column"):
        current_attribution(build_store(script), ARTIFACT_ID)
