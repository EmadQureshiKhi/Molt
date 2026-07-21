"""Unit tests for the Session write statements and the tenancy-scoped reads.

Nothing here opens a socket. A recording cursor answers each statement from a
script and keeps what it was sent, so the claims below are asserted by reading the
statements the module produced rather than by reaching a cluster. The claims that
need a cluster to be meaningful, that depth really is read from the parent row and
that an absent parent really is refused by the reference, are asserted by the
instance-backed session-write module of the integration suite.

The script answers the module's own statements and no others. A connection also
carries statements neither this module nor its caller wrote: the pool establishes
the session statement timeout when it opens a connection and clears any open
transaction when it takes one back, and the transaction wrapper frames every
write. None of those draw a row from the script, because a script describes what
the module under test asked for, and an assertion about one transaction is made
over the statements between that transaction's own framing rather than over
everything a pooled connection ever saw.

Four properties of the shape are checked here.

The depth a caller presents is not sent. The insert binds every other column and
derives depth inside the statement, so a caller cannot place a Session at a depth
the stored graph disagrees with.

A counter moves by increment. Each assignment names the column it is assigning,
and the transaction holds exactly one statement between its framing, so there is
no read of a counter anywhere on the path that writes one.

Every read names the tenant. Not one read statement selects a row by identifier
alone.

A spawning Event is inserted before the Session it spawned. The Event insert is a
callable that must have run before the Session statement is sent, and the recorded
order is what shows it did.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.errors import MissingParentError
from molt.models.session import Session, SessionOutcome
from molt.store import STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
)
from molt.store.sessions import (
    BUMP_COUNTERS_FOR_CLIENT_STATEMENT,
    BUMP_COUNTERS_STATEMENT,
    END_SESSION_STATEMENT,
    END_SESSION_WITH_COUNTERS_STATEMENT,
    FOREIGN_KEY_VIOLATION_STATE,
    INSERT_SESSION_STATEMENT,
    MAX_READ_LIMIT,
    PARENT_REFERENCE_CONSTRAINT,
    SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT,
    SELECT_CHILD_SESSIONS_STATEMENT,
    SELECT_EVENTS_FOR_SESSION_STATEMENT,
    SELECT_SESSION_STATEMENT,
    SELECT_SESSIONS_FOR_CLIENT_STATEMENT,
    SPAWNING_REFERENCE_CONSTRAINT,
    CounterDelta,
    SessionCounters,
    bump_session_counters,
    end_session,
    insert_spawned_session,
    session_of_client,
    sessions_of_client,
    upsert_session,
)

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# The five counter columns, in the order every statement here lists them.
COUNTER_COLUMNS: Final[tuple[str, ...]] = (
    "tool_call_count",
    "model_request_count",
    "error_count",
    "token_count",
    "cost_usd",
)

# Every read statement the module exposes, so the tenancy claim is asserted over
# the whole set rather than over the ones a test happened to call.
READ_STATEMENTS: Final[tuple[str, ...]] = (
    SELECT_SESSION_STATEMENT,
    SELECT_SESSIONS_FOR_CLIENT_STATEMENT,
    SELECT_CHILD_SESSIONS_STATEMENT,
    SELECT_EVENTS_FOR_SESSION_STATEMENT,
    SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT,
)

# The statements the connection surface and the transaction wrapper send on their
# own account: the per-connection setting the pool establishes, the four
# transaction-control statements, and the reset a returned connection is cleared
# with. None of them belongs to a script, so none of them consumes a scripted row.
FRAMING_STATEMENTS: Final[frozenset[str]] = frozenset(
    {
        STATEMENT_TIMEOUT_STATEMENT,
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        COMMIT_STATEMENT,
        ROLLBACK_STATEMENT,
    }
)

# The statements that end a transaction, either of which closes the slice a
# per-transaction assertion is made over.
TERMINATING_STATEMENTS: Final[frozenset[str]] = frozenset({COMMIT_STATEMENT, ROLLBACK_STATEMENT})


class ScriptedCursor:
    """A cursor answering from a script and recording what it was sent."""

    def __init__(self, owner: ScriptedConnection) -> None:
        self._owner = owner
        self.released = False

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then raise or arm a row as the script says.

        A framing statement is recorded and nothing more. Drawing a row for one
        would hand the script's first answer to the pool's session setting or to
        the wrapper's `BEGIN`, and the statement the script was written for would
        then find no row.
        """
        self._owner.sent.append((query, None if params is None else tuple(params)))
        failure = self._owner.failure
        if failure is not None and failure.statement_fragment in query:
            raise failure.error
        if query not in FRAMING_STATEMENTS:
            self._owner.armed = self._owner.rows.pop(0) if self._owner.rows else None
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the row the last statement armed, if any."""
        return self._owner.armed

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return the armed row as a one-row result, or no rows."""
        return [] if self._owner.armed is None else [self._owner.armed]

    def close(self) -> None:
        """Mark this cursor released."""
        self.released = True


class ScriptedFailure:
    """A failure the script raises for statements holding a given fragment."""

    def __init__(self, statement_fragment: str, error: Exception) -> None:
        self.statement_fragment = statement_fragment
        self.error = error


class ScriptedConnection:
    """A connection handing out recording cursors over one shared script."""

    def __init__(
        self,
        rows: list[tuple[object, ...] | None] | None = None,
        failure: ScriptedFailure | None = None,
    ) -> None:
        self.sent: list[tuple[str, tuple[object, ...] | None]] = []
        self.rows: list[tuple[object, ...] | None] = [] if rows is None else list(rows)
        self.failure = failure
        self.armed: tuple[object, ...] | None = None
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Open a recording cursor over this connection's script."""
        return ScriptedCursor(self)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True

    @property
    def statements(self) -> list[str]:
        """Every statement this connection was sent, in order."""
        return [query for query, _ in self.sent]

    @property
    def transaction_statements(self) -> list[str]:
        """The one transaction's statements, from its `BEGIN` to the statement that ended it.

        The pool's own session setting and its reset of a returned connection sit
        outside this slice, which is what makes an assertion of the form "these
        statements and no others" a statement about the transaction rather than
        about a pooled connection's whole life.
        """
        statements = self.statements
        assert BEGIN_STATEMENT in statements, "a write frames an explicit transaction"
        start = statements.index(BEGIN_STATEMENT)
        for offset, query in enumerate(statements[start:]):
            if query in TERMINATING_STATEMENTS:
                return statements[start : start + offset + 1]
        return statements[start:]

    def parameters_of(self, statement: str) -> tuple[object, ...] | None:
        """The bound parameters of the one occurrence of a statement."""
        matches = [params for query, params in self.sent if query == statement]
        assert len(matches) == 1, f"the statement should have been sent once, not {len(matches)}"
        return matches[0]


class DriverFailureError(Exception):
    """A driver failure carrying the state and the constraint a driver reports."""

    def __init__(self, sqlstate: str, constraint_name: str | None) -> None:
        super().__init__("the statement was refused")
        self.sqlstate = sqlstate
        self.diag = _Diagnostic(constraint_name)


class _Diagnostic:
    """The diagnostic attribute a driver failure carries the constraint under."""

    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


def build_store(connection: ScriptedConnection) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def build_session(
    *,
    parent_session_id: UUID | None = None,
    depth: int = 0,
    spawning_event_id: UUID | None = None,
) -> Session:
    """A Session record with the counters at rest and the outcome open."""
    return Session(
        id=uuid4(),
        client_id=uuid4(),
        agent_cli="agent",
        machine_id="machine",
        team_id="team",
        attribution={"principal": "operator"},
        workspace_path="/workspace",
        started_at=MOMENT,
        ended_at=None,
        outcome=SessionOutcome.IN_PROGRESS,
        parent_session_id=parent_session_id,
        spawning_event_id=spawning_event_id,
        depth=depth,
        tool_call_count=0,
        model_request_count=0,
        error_count=0,
        token_count=0,
        cost_usd=Decimal(0),
        halted=False,
        halted_at=None,
        halt_reason=None,
        halt_rule_id=None,
    )


# ---------------------------------------------------------------------------
# Depth derivation
# ---------------------------------------------------------------------------


def test_the_insert_derives_depth_and_binds_no_depth_value() -> None:
    """Depth is an expression over the parent row, not a bound parameter."""
    assert "coalesce(parent.depth + 1, 0)" in INSERT_SESSION_STATEMENT
    assert "LEFT JOIN session AS parent ON parent.id = %s" in INSERT_SESSION_STATEMENT

    connection = ScriptedConnection(rows=[(4,)])
    record = build_session(parent_session_id=uuid4(), depth=99)

    derived = upsert_session(build_store(connection), record)

    assert derived == 4, "the depth the cluster derived is what the caller is told"
    bound = connection.parameters_of(INSERT_SESSION_STATEMENT)
    assert bound is not None
    assert record.depth not in bound[:17], "the presented depth reaches no bound parameter"
    assert bound[-1] == record.parent_session_id, (
        "the last parameter is the parent the depth is derived from"
    )


def test_a_root_session_derives_depth_zero_from_an_unmatched_join() -> None:
    """A Session naming no parent binds no parent and the join matches nothing."""
    connection = ScriptedConnection(rows=[(0,)])
    record = build_session()

    assert upsert_session(build_store(connection), record) == 0
    bound = connection.parameters_of(INSERT_SESSION_STATEMENT)
    assert bound is not None
    assert bound[-1] is None


def test_an_absent_parent_is_reported_as_a_missing_parent() -> None:
    """The reference violation on the parent column becomes the named failure."""
    parent = uuid4()
    connection = ScriptedConnection(
        failure=ScriptedFailure(
            "INSERT INTO session",
            DriverFailureError(FOREIGN_KEY_VIOLATION_STATE, PARENT_REFERENCE_CONSTRAINT),
        )
    )
    record = build_session(parent_session_id=parent)

    with pytest.raises(MissingParentError) as raised:
        upsert_session(build_store(connection), record)

    assert str(parent) in str(raised.value)
    assert COMMIT_STATEMENT not in connection.statements, "nothing was committed"


def test_an_absent_spawning_event_is_reported_as_a_missing_parent() -> None:
    """The reference violation on the spawning column names that reference."""
    connection = ScriptedConnection(
        failure=ScriptedFailure(
            "INSERT INTO session",
            DriverFailureError(FOREIGN_KEY_VIOLATION_STATE, SPAWNING_REFERENCE_CONSTRAINT),
        )
    )

    with pytest.raises(MissingParentError, match="spawning Event"):
        upsert_session(build_store(connection), build_session(spawning_event_id=uuid4()))


def test_a_failure_that_is_not_a_reference_violation_propagates() -> None:
    """Only the reference state is translated; anything else reaches the caller."""
    connection = ScriptedConnection(
        failure=ScriptedFailure(
            "INSERT INTO session", DriverFailureError("23514", "session_root_depth")
        )
    )

    with pytest.raises(DriverFailureError):
        upsert_session(build_store(connection), build_session())


def test_the_conflict_path_restates_no_tenancy_or_lineage_column() -> None:
    """A restatement closes a Session and touches nothing the guard protects."""
    _, _, conflict = INSERT_SESSION_STATEMENT.partition("ON CONFLICT (id) DO UPDATE SET ")
    assignments = conflict.partition(" RETURNING")[0]

    for protected in (
        "client_id",
        "agent_cli",
        "machine_id",
        "team_id",
        "attribution",
        "workspace_path",
        "started_at",
        "parent_session_id",
        "spawning_event_id",
        "depth",
        "halted",
        *COUNTER_COLUMNS,
    ):
        assert f"{protected} =" not in assignments, (
            f"a restatement should not assign {protected}, which the guard protects"
        )
    assert "ended_at = coalesce(" in assignments, "an end timestamp is never cleared"
    assert "outcome = CASE WHEN excluded.outcome = 'in_progress'" in assignments


# ---------------------------------------------------------------------------
# Spawning order
# ---------------------------------------------------------------------------


def test_the_spawning_event_is_inserted_before_the_child_session() -> None:
    """The Event insert runs first and its identifier is what the Session binds."""
    connection = ScriptedConnection(rows=[(1,)])
    event_id = uuid4()

    def append_event(cursor: object) -> UUID:
        assert hasattr(cursor, "execute")
        connection.sent.append(("INSERT INTO ledger", (event_id,)))
        return event_id

    record = build_session(parent_session_id=uuid4())
    depth = insert_spawned_session(
        build_store(connection), record, append_spawning_event=append_event
    )

    assert depth == 1
    framed = connection.transaction_statements
    assert framed == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        "INSERT INTO ledger",
        INSERT_SESSION_STATEMENT,
        COMMIT_STATEMENT,
    ], "the Event insert precedes the Session insert inside the one transaction"

    bound = connection.parameters_of(INSERT_SESSION_STATEMENT)
    assert bound is not None
    assert bound[11] == event_id, "the Session binds the identifier the Event insert reported"


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [BUMP_COUNTERS_STATEMENT, BUMP_COUNTERS_FOR_CLIENT_STATEMENT],
)
def test_every_counter_moves_by_increment(statement: str) -> None:
    """Each assignment adds to the column it assigns, so no increment is lost."""
    for column in COUNTER_COLUMNS:
        assert f"{column} = {column} + %s" in statement
    assert "SELECT" not in statement, "an increment reads nothing back first"


def test_the_increment_is_the_only_statement_in_its_transaction() -> None:
    """No read of a counter precedes the write of one."""
    connection = ScriptedConnection(rows=[(3, 2, 1, 40, Decimal("0.25"))])

    counters = bump_session_counters(
        build_store(connection),
        uuid4(),
        CounterDelta(tool_calls=1, tokens=40, cost_usd=Decimal("0.25")),
    )

    assert counters == SessionCounters(
        tool_call_count=3,
        model_request_count=2,
        error_count=1,
        token_count=40,
        cost_usd=Decimal("0.25"),
    )
    assert connection.transaction_statements == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        BUMP_COUNTERS_STATEMENT,
        COMMIT_STATEMENT,
    ], "one increment and no read of a counter before it"


def test_a_client_scoped_increment_binds_the_tenant() -> None:
    """Naming the tenant scopes the write to it."""
    connection = ScriptedConnection(rows=[(1, 0, 0, 0, Decimal(0))])
    session_id = uuid4()
    client_id = uuid4()

    bump_session_counters(
        build_store(connection), session_id, CounterDelta(tool_calls=1), client_id=client_id
    )

    bound = connection.parameters_of(BUMP_COUNTERS_FOR_CLIENT_STATEMENT)
    assert bound == (1, 0, 0, 0, Decimal(0), session_id, client_id)


def test_an_increment_matching_no_row_reports_nothing() -> None:
    """A Session another tenant owns moves nothing and is reported as no row."""
    connection = ScriptedConnection(rows=[None])

    assert (
        bump_session_counters(
            build_store(connection), uuid4(), CounterDelta(errors=1), client_id=uuid4()
        )
        is None
    )


def test_an_empty_delta_is_refused() -> None:
    """An update that would change no column is refused before it is sent."""
    connection = ScriptedConnection()

    with pytest.raises(ValueError, match="at least one counter"):
        bump_session_counters(build_store(connection), uuid4(), CounterDelta())

    assert connection.statements == []


@pytest.mark.parametrize(
    "build",
    [
        lambda: CounterDelta(tool_calls=-1),
        lambda: CounterDelta(model_requests=-1),
        lambda: CounterDelta(errors=-1),
        lambda: CounterDelta(tokens=-1),
        lambda: CounterDelta(cost_usd=Decimal("-0.01")),
    ],
)
def test_a_negative_delta_is_refused(build: Callable[[], CounterDelta]) -> None:
    """A running count of things that happened never moves backwards."""
    with pytest.raises(ValueError, match="cannot be negative"):
        build()


# ---------------------------------------------------------------------------
# The terminal path
# ---------------------------------------------------------------------------


def test_the_terminal_write_names_the_tenant_and_the_terminal_columns() -> None:
    """Closing a Session writes an outcome and an end timestamp, scoped by tenant."""
    connection = ScriptedConnection(rows=[(2, 1, 0, 10, Decimal("1.50"))])
    session_id = uuid4()
    client_id = uuid4()

    counters = end_session(
        build_store(connection),
        session_id,
        client_id,
        outcome=SessionOutcome.SUCCEEDED,
        ended_at=MOMENT,
    )

    assert counters is not None
    assert counters.cost_usd == Decimal("1.50")
    assert connection.parameters_of(END_SESSION_STATEMENT) == (
        MOMENT,
        "succeeded",
        session_id,
        client_id,
    )


def test_terminal_counters_are_raised_rather_than_replaced() -> None:
    """A final total may raise a counter and can never undo an increment."""
    for column in COUNTER_COLUMNS:
        assert f"{column} = greatest({column}, %s)" in END_SESSION_WITH_COUNTERS_STATEMENT

    connection = ScriptedConnection(rows=[(7, 3, 0, 90, Decimal("2.00"))])
    presented = SessionCounters(
        tool_call_count=7,
        model_request_count=3,
        error_count=0,
        token_count=90,
        cost_usd=Decimal("2.00"),
    )

    end_session(
        build_store(connection),
        uuid4(),
        uuid4(),
        outcome=SessionOutcome.FAILED,
        ended_at=MOMENT,
        counters=presented,
    )

    bound = connection.parameters_of(END_SESSION_WITH_COUNTERS_STATEMENT)
    assert bound is not None
    assert bound[2:7] == (7, 3, 0, 90, Decimal("2.00"))


def test_closing_a_session_to_the_open_outcome_is_refused() -> None:
    """A terminal write states how a Session ended, so the open outcome is refused."""
    connection = ScriptedConnection()

    with pytest.raises(ValueError, match="terminal outcome"):
        end_session(
            build_store(connection),
            uuid4(),
            uuid4(),
            outcome=SessionOutcome.IN_PROGRESS,
            ended_at=MOMENT,
        )

    assert connection.statements == []


def test_a_naive_end_timestamp_is_refused() -> None:
    """An instant with no offset has no defined position on the timeline."""
    connection = ScriptedConnection()

    with pytest.raises(ValueError, match="timezone aware"):
        end_session(
            build_store(connection),
            uuid4(),
            uuid4(),
            outcome=SessionOutcome.ABANDONED,
            ended_at=datetime.fromtimestamp(0.0, tz=UTC).replace(tzinfo=None),
        )

    assert connection.statements == []


# ---------------------------------------------------------------------------
# Tenancy scoping of the reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("statement", READ_STATEMENTS)
def test_every_read_is_scoped_by_client(statement: str) -> None:
    """No read selects a row without naming the tenant it belongs to."""
    assert "client_id = %s" in statement
    assert "LIMIT %s" in statement or statement == SELECT_SESSION_STATEMENT


def test_the_single_session_read_binds_both_the_identifier_and_the_tenant() -> None:
    """Holding a Session identifier is not authority over the row it names."""
    connection = ScriptedConnection(
        rows=[
            (
                uuid4(),
                uuid4(),
                "agent",
                "machine",
                None,
                {"principal": "operator"},
                None,
                MOMENT,
                None,
                "in_progress",
                None,
                None,
                0,
                0,
                0,
                0,
                0,
                Decimal(0),
                False,
                None,
                None,
                None,
            )
        ]
    )
    session_id = uuid4()
    client_id = uuid4()

    found = session_of_client(build_store(connection), session_id, client_id)

    assert found is not None
    assert found.attribution == {"principal": "operator"}
    assert connection.parameters_of(SELECT_SESSION_STATEMENT) == (session_id, client_id)
    assert BEGIN_STATEMENT not in connection.statements, "a read frames no transaction"


def test_a_read_bound_outside_the_permitted_range_is_refused() -> None:
    """A read is bounded, so no caller can ask for a tenant's whole history."""
    store = build_store(ScriptedConnection())

    with pytest.raises(ValueError, match="at least one row"):
        sessions_of_client(store, uuid4(), limit=0)

    with pytest.raises(ValueError, match=str(MAX_READ_LIMIT)):
        sessions_of_client(store, uuid4(), limit=MAX_READ_LIMIT + 1)
