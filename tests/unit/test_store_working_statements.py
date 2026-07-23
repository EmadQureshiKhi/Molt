"""Unit tests for the working-tier statements, the expiry source, and the purge count.

Nothing here opens a socket. A scripted cursor answers each statement from a
script and keeps what it was sent, so the claims below are asserted by reading the
statements the module produced. The claims that need a cluster to be meaningful,
that a repeated write really overwrites in place and that the cluster really
removes an expired row, belong to the instance-backed suite.

Six properties of the shape are checked.

The write is an upsert on the Session and the scratch key together. The statement
is an `UPSERT` naming that pair as it is keyed, every column it writes is bound,
and the whole row comes back, so the stored expiry is the cluster's report rather
than the caller's expectation of it.

The expiry comes from the configuration surface on every write. Two
configurations naming two different counts of seconds produce two different stored
expiries from the same write instant, which is what makes the interval
configuration rather than a constant this module could hold.

The reads are a point read on the whole key and a listing over the leading column,
each scoped by tenant, each framing no transaction, and the listing bounded by a
row limit no caller may raise past.

The purge is one statement and one number. One deletion, one aggregate count
computed inside the same statement, no row bound, and no loop.

The measurement carries the aggregate count, undimensioned, once per commit. A
transaction the wrapper had to run twice emits one measurement, because the
emission happens after the commit rather than inside the body.

Nothing here can turn a working row into anything else. Every statement of the
module names the one table the tier taxonomy assigns to the `working` tier, and no
statement names a table of any other tier.

**Validates: Requirements 42.7, 42.8, 42.9, 42.12, 42.13**
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.errors import StoreError
from molt.models.event import JsonObject
from molt.models.tiers import MEMORY_TIERS, TIER_NAMES, WORKING_TIER
from molt.store import Connection, MemoryStore
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
    SERIALIZATION_FAILURE_STATE,
)
from molt.store.working import (
    MAX_SCRATCH_LIMIT,
    PURGE_CLIENT_SCRATCH_STATEMENT,
    SELECT_SCRATCH_STATEMENT,
    SELECT_SESSION_SCRATCH_STATEMENT,
    UPSERT_SCRATCH_STATEMENT,
    WORKING_ROWS_DELETED_METRIC,
    WORKING_TTL_KEY,
    ScratchRow,
    ScratchWrite,
    WorkingInterval,
    purge_working_rows,
    read_scratch,
    session_scratch,
    write_scratch,
)
from molt.telemetry import Telemetry, configure, current, reset

# Every statement the module holds, so the containment claim is asserted over the
# whole set rather than over the one a test happened to call.
ALL_STATEMENTS: Final[tuple[str, ...]] = (
    UPSERT_SCRATCH_STATEMENT,
    SELECT_SCRATCH_STATEMENT,
    SELECT_SESSION_SCRATCH_STATEMENT,
    PURGE_CLIENT_SCRATCH_STATEMENT,
)

# The one table the taxonomy assigns to the disposable tier, read from the
# taxonomy rather than restated, so this suite and the tier model cannot disagree.
WORKING_TABLE: Final[str] = MEMORY_TIERS[WORKING_TIER].tables[0]

# Every table of every other tier. A statement of this module naming one of them
# would be a path by which a working row became something a later reader depends
# on, and the containment claim is exactly that none exists.
FOREIGN_TABLES: Final[tuple[str, ...]] = tuple(
    sorted(
        {
            table
            for name in TIER_NAMES
            if name != WORKING_TIER
            for table in MEMORY_TIERS[name].tables
        }
    )
)

# The view a lineage insert proves its parent through. The tier is unreachable
# from the provenance, action, and attribution tiers because this table is not in
# that view, and this module naming it would be the first step towards changing
# that.
ARTIFACT_VIEW: Final[str] = "artifact_ref"

# Fragments the script matches a statement by.
UPSERT_FRAGMENT: Final[str] = "UPSERT INTO working_memory"
POINT_READ_FRAGMENT: Final[str] = "AND scratch_key = %s"
LISTING_FRAGMENT: Final[str] = "ORDER BY scratch_key ASC"
PURGE_FRAGMENT: Final[str] = "DELETE FROM working_memory"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# Two intervals, neither the surface default, so an assertion about the stored
# expiry cannot be satisfied by a constant the module might have held.
SHORT_SECONDS: Final[int] = 45
LONG_SECONDS: Final[int] = 900

# The identifiers and the value every driven write names.
SESSION_ID: Final[UUID] = uuid4()
CLIENT_ID: Final[UUID] = uuid4()
SCRATCH_KEY: Final[str] = "plan-under-revision"
SCRATCH_VALUE: Final[JsonObject] = {"step": 2, "note": "held while the agent works"}

# Text a caller might supply that would end a statement or comment out the rest of
# one, if it ever reached statement text.
HOSTILE_TEXT: Final[str] = "'; DROP TABLE ledger; --"

# How many rows the purge is scripted to report.
PURGED_COUNT: Final[int] = 12


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
    """A driver failure carrying the state a driver reports the fault under."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("the statement was refused")
        self.sqlstate = sqlstate


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def configuration_of(seconds: int) -> Configuration:
    """A configuration naming one expiry interval and reading no real environment."""
    return Configuration(environ={WORKING_TTL_KEY: str(seconds)}, file_values={})


def interval_of(seconds: int) -> WorkingInterval:
    """The interval the module reads from a configuration naming that many seconds."""
    return WorkingInterval.from_configuration(configuration_of(seconds))


def entry(value: JsonObject | None = None, *, scratch_key: str = SCRATCH_KEY) -> ScratchWrite:
    """One scratch write naming this module's Session, tenant, and key."""
    return ScratchWrite(
        session_id=SESSION_ID,
        scratch_key=scratch_key,
        client_id=CLIENT_ID,
        value=SCRATCH_VALUE if value is None else value,
    )


def stored_row(
    *,
    scratch_key: str = SCRATCH_KEY,
    updated_at: datetime = MOMENT,
    expires_at: datetime | None = None,
) -> tuple[object, ...]:
    """One row of the width the statements select, as the cluster would report it."""
    return (
        SESSION_ID,
        scratch_key,
        CLIENT_ID,
        json.dumps(SCRATCH_VALUE),
        updated_at,
        updated_at + timedelta(seconds=SHORT_SECONDS) if expires_at is None else expires_at,
    )


@pytest.fixture
def telemetry_sink() -> Iterator[io.StringIO]:
    """Install a process-wide telemetry instance writing to a sink for one test."""
    sink = io.StringIO()
    configure(Configuration(environ={"MOLT_LOG_LEVEL": "debug"}, file_values={}), stream=sink)
    try:
        yield sink
    finally:
        reset()


def instance() -> Telemetry:
    """The process-wide telemetry instance the module emitted through."""
    return current()


def purge_records(sink: io.StringIO) -> tuple[dict[str, object], ...]:
    """Every record the sink holds that reports a working-tier purge."""
    records: list[dict[str, object]] = []
    for line in sink.getvalue().splitlines():
        record: dict[str, object] = json.loads(line)
        if "working_rows_deleted" in record:
            records.append(record)
    return tuple(records)


# ---------------------------------------------------------------------------
# The write, on the composite key
# ---------------------------------------------------------------------------


def test_the_write_is_an_upsert_on_the_session_and_scratch_key() -> None:
    """One statement, keyed by the pair, reporting the whole row it left."""
    assert UPSERT_SCRATCH_STATEMENT.startswith(UPSERT_FRAGMENT)
    assert "(session_id, scratch_key, client_id, value, updated_at, expires_at)" in (
        UPSERT_SCRATCH_STATEMENT
    )
    assert "%s::JSONB" in UPSERT_SCRATCH_STATEMENT
    assert UPSERT_SCRATCH_STATEMENT.endswith(
        "RETURNING session_id, scratch_key, client_id, value, updated_at, expires_at"
    )
    assert "INSERT INTO" not in UPSERT_SCRATCH_STATEMENT, "a second version is not written"
    assert "ON CONFLICT" not in UPSERT_SCRATCH_STATEMENT
    assert UPSERT_SCRATCH_STATEMENT.count("%s") == 6


def test_the_write_binds_every_value_it_stores() -> None:
    """Six bound parameters, with the value as canonical text and no text in the statement."""
    script = Script(answers=[Answer(UPSERT_FRAGMENT, (stored_row(),))])
    hostile = entry({HOSTILE_TEXT: HOSTILE_TEXT}, scratch_key=HOSTILE_TEXT)

    write_scratch(build_store(script), hostile, interval=interval_of(SHORT_SECONDS), now=MOMENT)

    bound = script.parameters_of(UPSERT_SCRATCH_STATEMENT)
    assert bound == (
        SESSION_ID,
        HOSTILE_TEXT,
        CLIENT_ID,
        json.dumps({HOSTILE_TEXT: HOSTILE_TEXT}, sort_keys=True, separators=(",", ":")),
        MOMENT,
        MOMENT + timedelta(seconds=SHORT_SECONDS),
    )
    for statement in script.statements:
        assert HOSTILE_TEXT not in statement


def test_the_write_runs_in_one_serializable_transaction() -> None:
    """The write reaches the cluster through the serializable wrapper alone."""
    script = Script(answers=[Answer(UPSERT_FRAGMENT, (stored_row(),))])

    write_scratch(build_store(script), entry(), interval=interval_of(SHORT_SECONDS), now=MOMENT)

    statements = script.statements
    assert statements.index(SERIALIZABLE_STATEMENT) < statements.index(UPSERT_SCRATCH_STATEMENT)
    assert statements.index(UPSERT_SCRATCH_STATEMENT) < statements.index(COMMIT_STATEMENT)


def test_the_write_reports_the_row_the_cluster_holds() -> None:
    """A write answers with the stored row, its expiry included."""
    script = Script(answers=[Answer(UPSERT_FRAGMENT, (stored_row(),))])

    written = write_scratch(
        build_store(script), entry(), interval=interval_of(SHORT_SECONDS), now=MOMENT
    )

    assert written == ScratchRow(
        session_id=SESSION_ID,
        scratch_key=SCRATCH_KEY,
        client_id=CLIENT_ID,
        value=SCRATCH_VALUE,
        updated_at=MOMENT,
        expires_at=MOMENT + timedelta(seconds=SHORT_SECONDS),
    )


def test_a_write_reporting_no_row_is_refused() -> None:
    """A write that stored nothing is not reported as having stored something."""
    script = Script(answers=[Answer(UPSERT_FRAGMENT, ())])

    with pytest.raises(StoreError, match="no scratch state"):
        write_scratch(build_store(script), entry(), interval=interval_of(SHORT_SECONDS))


def test_a_write_names_the_scratch_key_it_is_stored_under() -> None:
    """The key is half the primary key, so an empty one names no row."""
    with pytest.raises(ValueError, match="scratch key"):
        ScratchWrite(
            session_id=SESSION_ID,
            scratch_key="",
            client_id=CLIENT_ID,
            value=SCRATCH_VALUE,
        )


# ---------------------------------------------------------------------------
# The expiry, taken from configuration
# ---------------------------------------------------------------------------


def test_the_expiry_interval_is_read_from_the_configuration_surface() -> None:
    """The count of seconds comes from one named key, whatever it says."""
    assert interval_of(SHORT_SECONDS).seconds == SHORT_SECONDS
    assert interval_of(LONG_SECONDS).seconds == LONG_SECONDS
    assert interval_of(SHORT_SECONDS).interval == timedelta(seconds=SHORT_SECONDS)


def test_two_configured_intervals_produce_two_stored_expiries() -> None:
    """The same write instant expires differently under two configurations."""
    written: list[datetime] = []
    for seconds in (SHORT_SECONDS, LONG_SECONDS):
        script = Script(answers=[Answer(UPSERT_FRAGMENT, (stored_row(),))])
        write_scratch(build_store(script), entry(), interval=interval_of(seconds), now=MOMENT)
        bound = script.parameters_of(UPSERT_SCRATCH_STATEMENT)
        assert bound is not None
        expiry = bound[5]
        assert isinstance(expiry, datetime)
        written.append(expiry)

    assert written == [
        MOMENT + timedelta(seconds=SHORT_SECONDS),
        MOMENT + timedelta(seconds=LONG_SECONDS),
    ]


def test_the_stored_instants_come_from_one_reading() -> None:
    """The last-write instant and the expiry cannot disagree about the write."""
    script = Script(answers=[Answer(UPSERT_FRAGMENT, (stored_row(),))])

    write_scratch(build_store(script), entry(), interval=interval_of(LONG_SECONDS), now=MOMENT)

    bound = script.parameters_of(UPSERT_SCRATCH_STATEMENT)
    assert bound is not None
    written, expiry = bound[4], bound[5]
    assert isinstance(written, datetime)
    assert isinstance(expiry, datetime)
    assert expiry == written + timedelta(seconds=LONG_SECONDS)


def test_an_interval_reaching_no_distance_is_refused() -> None:
    """A row whose life is zero seconds would be written already expired."""
    with pytest.raises(ValueError, match="positive number of seconds"):
        WorkingInterval(seconds=0)


def test_a_naive_write_instant_is_refused() -> None:
    """An instant with no offset would store an expiry nobody can place."""
    with pytest.raises(ValueError, match="timezone"):
        write_scratch(
            build_store(Script()),
            entry(),
            interval=interval_of(SHORT_SECONDS),
            now=datetime.fromtimestamp(0.0, tz=UTC).replace(tzinfo=None),
        )


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


def test_the_point_read_names_the_whole_key_and_the_tenant() -> None:
    """One seek on the primary key, scoped by the tenant that owns the row."""
    assert SELECT_SCRATCH_STATEMENT.endswith(
        "WHERE session_id = %s AND scratch_key = %s AND client_id = %s"
    )
    assert SELECT_SCRATCH_STATEMENT.count("%s") == 3

    script = Script(answers=[Answer(POINT_READ_FRAGMENT, (stored_row(),))])
    store = build_store(script)

    found = read_scratch(store, SESSION_ID, SCRATCH_KEY, CLIENT_ID)

    assert found is not None
    assert found.scratch_key == SCRATCH_KEY
    assert script.parameters_of(SELECT_SCRATCH_STATEMENT) == (SESSION_ID, SCRATCH_KEY, CLIENT_ID)
    assert BEGIN_STATEMENT not in script.statements, "a read frames no transaction"


def test_an_absent_row_reads_as_absent_rather_than_as_a_failure() -> None:
    """A forgotten row and a row that never existed are the same answer."""
    script = Script(answers=[Answer(POINT_READ_FRAGMENT, ())])

    assert read_scratch(build_store(script), SESSION_ID, SCRATCH_KEY, CLIENT_ID) is None


def test_the_listing_is_per_session_ordered_and_bounded() -> None:
    """The leading key column, a total order, and a bound the caller supplies."""
    assert LISTING_FRAGMENT in SELECT_SESSION_SCRATCH_STATEMENT
    assert SELECT_SESSION_SCRATCH_STATEMENT.endswith("LIMIT %s")
    assert SELECT_SESSION_SCRATCH_STATEMENT.count("%s") == 3

    script = Script(
        answers=[
            Answer(
                LISTING_FRAGMENT,
                (stored_row(scratch_key="a-key"), stored_row(scratch_key="b-key")),
            )
        ]
    )

    rows = session_scratch(build_store(script), SESSION_ID, CLIENT_ID, limit=7)

    assert tuple(row.scratch_key for row in rows) == ("a-key", "b-key")
    assert script.parameters_of(SELECT_SESSION_SCRATCH_STATEMENT) == (SESSION_ID, CLIENT_ID, 7)


@pytest.mark.parametrize("limit", [0, MAX_SCRATCH_LIMIT + 1])
def test_a_listing_bound_outside_the_admitted_range_is_refused(limit: int) -> None:
    """No caller asks for no rows, and none asks for an unbounded scan."""
    with pytest.raises(ValueError, match="bound"):
        session_scratch(build_store(Script()), SESSION_ID, CLIENT_ID, limit=limit)


def test_a_row_of_the_wrong_width_is_refused() -> None:
    """A statement and its decoder cannot drift apart silently."""
    script = Script(answers=[Answer(POINT_READ_FRAGMENT, ((SESSION_ID, SCRATCH_KEY),))])

    with pytest.raises(StoreError, match="column"):
        read_scratch(build_store(script), SESSION_ID, SCRATCH_KEY, CLIENT_ID)


# ---------------------------------------------------------------------------
# The purge, as one statement and one number
# ---------------------------------------------------------------------------


def test_the_purge_is_one_deleting_statement_carrying_its_own_count() -> None:
    """One deletion, one aggregate count, one bound value, and no row bound."""
    assert PURGE_CLIENT_SCRATCH_STATEMENT.count(PURGE_FRAGMENT) == 1
    assert "WHERE client_id = %s" in PURGE_CLIENT_SCRATCH_STATEMENT
    assert PURGE_CLIENT_SCRATCH_STATEMENT.endswith("SELECT count(*) FROM purged")
    assert PURGE_CLIENT_SCRATCH_STATEMENT.count("%s") == 1
    assert "LIMIT" not in PURGE_CLIENT_SCRATCH_STATEMENT.upper()


def test_the_purge_reports_the_aggregate_count_it_removed(telemetry_sink: io.StringIO) -> None:
    """One statement, one number, and the number is what comes back."""
    script = Script(answers=[Answer(PURGE_FRAGMENT, ((PURGED_COUNT,),))])

    removed = purge_working_rows(build_store(script), CLIENT_ID)

    assert removed == PURGED_COUNT
    assert script.parameters_of(PURGE_CLIENT_SCRATCH_STATEMENT) == (CLIENT_ID,)
    assert script.statements.count(PURGE_CLIENT_SCRATCH_STATEMENT) == 1

    records = purge_records(telemetry_sink)
    assert len(records) == 1
    assert records[0]["working_rows_deleted"] == PURGED_COUNT
    assert records[0]["client_id"] == str(CLIENT_ID)


def test_the_measurement_carries_the_aggregate_count_undimensioned(
    telemetry_sink: io.StringIO,
) -> None:
    """The count is the measurement's value, and the tenant is not a dimension."""
    script = Script(answers=[Answer(PURGE_FRAGMENT, ((PURGED_COUNT,),))])

    purge_working_rows(build_store(script), CLIENT_ID)

    counters = instance().counters()
    assert counters[(WORKING_ROWS_DELETED_METRIC, ())] == float(PURGED_COUNT)
    assert instance().combinations() == ((WORKING_ROWS_DELETED_METRIC, ()),)
    assert len(purge_records(telemetry_sink)) == 1


def test_a_retried_purge_emits_one_measurement(telemetry_sink: io.StringIO) -> None:
    """The emission follows the commit, so a conflicting attempt reports nothing."""
    script = Script(
        answers=[
            Answer(PURGE_FRAGMENT, error=DriverFailureError(SERIALIZATION_FAILURE_STATE)),
            Answer(PURGE_FRAGMENT, ((PURGED_COUNT,),)),
        ]
    )

    removed = purge_working_rows(build_store(script), CLIENT_ID)

    statements = script.statements
    assert removed == PURGED_COUNT
    assert statements.count(PURGE_CLIENT_SCRATCH_STATEMENT) == 2
    assert ROLLBACK_STATEMENT in statements
    assert instance().counters()[(WORKING_ROWS_DELETED_METRIC, ())] == float(PURGED_COUNT)
    assert len(purge_records(telemetry_sink)) == 1, "one commit produces one record"


def test_a_purge_reporting_no_count_is_refused() -> None:
    """A count nobody read cannot be recorded on a run row."""
    script = Script(answers=[Answer(PURGE_FRAGMENT, ())])

    with pytest.raises(StoreError, match="no count"):
        purge_working_rows(build_store(script), CLIENT_ID)


# ---------------------------------------------------------------------------
# Containment: no promotion path exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("statement", ALL_STATEMENTS)
def test_every_statement_names_the_one_table_the_tier_holds(statement: str) -> None:
    """The tier's table is the taxonomy's, read from it rather than restated."""
    assert WORKING_TABLE in statement


@pytest.mark.parametrize("statement", ALL_STATEMENTS)
def test_no_statement_names_a_table_of_another_tier(statement: str) -> None:
    """No reference from this tier reaches lineage, attribution, or evidence."""
    for table in FOREIGN_TABLES:
        assert table not in statement, f"{table} is reachable from the working tier"
    assert ARTIFACT_VIEW not in statement


def test_the_taxonomy_assigns_this_tier_exactly_one_table() -> None:
    """The tier the module serves holds one table and no other tier holds it."""
    assert MEMORY_TIERS[WORKING_TIER].tables == (WORKING_TABLE,)
    assert WORKING_TABLE not in FOREIGN_TABLES
