"""Unit tests for the generation read, the guarded write predicate, and the wrapper.

Nothing here opens a socket. A scripted cursor answers each statement from a
script and keeps what it was sent, so every claim below is read off the statements
the module produced and the order it produced them in. The claims that need a
cluster to be meaningful, that a concurrent takeover really conflicts with the
guard read, live in the instance-backed module.

Six properties of the shape are checked, and the first is the one the module
exists for.

The generation read and the guarded write are one transaction, in that order. The
assertions are positional rather than merely presence-based: the read is sent
after the isolation level is set and before the write, and the write is sent
before the commit. A read taken in an earlier transaction would satisfy a test
that only asserted a stale generation is refused, and would still admit the row
the fence exists to refuse.

A refusal persists nothing. The write statement is not among the statements the
refused attempt sent at all, and no commit follows it.

A refusal names both generations and is counted once. The failure carries the
presented and the current generation, the undimensioned counter is emitted, and an
admitted write emits nothing.

A retry that sees a bumped generation refuses rather than loops. The conflicting
attempt is retried, the generation read runs again on the retry, and the refusal
that re-read produces ends the transaction on that attempt rather than consuming
the remaining retries. The fact this rests on is asserted directly too: the
supersession failure carries no serialization state, so the wrapper propagates it.

A Client holding no current lease is a different answer from a superseded one. It
is refused as a lease that is not held, and it is not counted against the
supersession metric, which the acceptance criterion attaches to a generation that
is not current.

The three named forms differ in the transaction label alone. Each sends the same
guard read ahead of the same body in one transaction, and the labels are distinct,
so a refused disposition and a refused finalisation are distinguishable in a log
record.

**Validates: Requirements 44.7, 44.8, 44.15**
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.errors import LeaseNotHeld, StaleFencingGeneration, StoreError
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, Cursor, MemoryStore
from molt.store.fencing import (
    CERTIFICATE_LABEL,
    CURRENT_GENERATION_QUERY,
    DISPOSITION_LABEL,
    FENCED_LABEL,
    FIRST_GENERATION,
    RUN_COMPLETION_LABEL,
    STALE_GENERATION_METRIC,
    CurrentGeneration,
    fenced,
    fenced_certificate,
    fenced_disposition,
    fenced_run_completion,
    select_current_generation,
)
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
    is_serialization_failure,
)
from molt.telemetry import Telemetry, configure, current, reset

# The write a guarded body sends. It belongs to this module, as the Disposition
# write, the completion record, and the certificate insert belong to the modules
# that own them, and it travels as one whole literal with its value bound.
WRITE_STATEMENT: Final[str] = "INSERT INTO disposition (run_id, fencing_generation) VALUES (%s, %s)"

# The fragments the script matches an answer to a statement by.
LEASE_FRAGMENT: Final[str] = "FROM erasure_lease"
WRITE_FRAGMENT: Final[str] = "INSERT INTO disposition"

# The tenant, the lease, and the owner every example here reads.
CLIENT_ID: Final[UUID] = uuid4()
LEASE_ID: Final[UUID] = uuid4()
OWNER: Final[str] = "worker-a"
RUN_ID: Final[UUID] = uuid4()

# The generation the scripted lease holds, and the one a superseded owner still
# believes it holds. Distinct values well above the floor, so no assertion is
# satisfied by a coincidence with the floor.
CURRENT: Final[int] = 7
SUPERSEDED: Final[int] = 6
TAKEN_OVER: Final[int] = 8

# The three named forms, each with the label it is expected to carry.
NAMED_FORMS: Final[tuple[tuple[str, str], ...]] = (
    ("disposition", DISPOSITION_LABEL),
    ("run_completion", RUN_COMPLETION_LABEL),
    ("certificate", CERTIFICATE_LABEL),
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

    @property
    def issued(self) -> list[str]:
        """What the modules sent, with the pool's own setup and reset removed."""
        return [
            query
            for query in self.statements
            if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT)
        ]

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
    """A driver failure carrying the state a driver reports it under."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("the statement was refused")
        self.sqlstate = sqlstate


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def lease_row(generation: int = CURRENT) -> tuple[object, ...]:
    """The row the generation read returns for a Client holding a current lease."""
    return (LEASE_ID, OWNER, generation)


def write_body(generation: int) -> Callable[[Cursor], int]:
    """A body sending one parameterised write and reporting that it ran."""

    def body(cursor: Cursor) -> int:
        cursor.execute(WRITE_STATEMENT, (RUN_ID, generation))
        return generation

    return body


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


def refusals_counted() -> float:
    """How many supersession refusals the process-wide instance has counted."""
    return instance().counters().get((STALE_GENERATION_METRIC, ()), 0.0)


# ---------------------------------------------------------------------------
# The shape of the generation read
# ---------------------------------------------------------------------------


def test_the_generation_read_names_the_current_lease_of_one_tenant() -> None:
    """One statement, scoped by tenant and to the lease that is current."""
    assert CURRENT_GENERATION_QUERY.startswith("SELECT id, owner, generation FROM erasure_lease")
    assert "WHERE client_id = %s" in CURRENT_GENERATION_QUERY
    assert "superseded_at IS NULL" in CURRENT_GENERATION_QUERY
    assert CURRENT_GENERATION_QUERY.count("%s") == 1
    assert "LIMIT" not in CURRENT_GENERATION_QUERY.upper()


def test_the_generation_read_binds_the_tenant_on_a_callers_cursor() -> None:
    """The read runs on the cursor it was handed, and the tenant is bound."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),))])
    store = build_store(script)

    with store.cursor() as cursor:
        held = select_current_generation(cursor, CLIENT_ID)

    assert held == CurrentGeneration(lease_id=LEASE_ID, owner=OWNER, generation=CURRENT)
    assert script.parameters_of(CURRENT_GENERATION_QUERY) == (CLIENT_ID,)
    assert BEGIN_STATEMENT not in script.statements


def test_a_lease_row_of_the_wrong_width_is_refused() -> None:
    """A statement and its decoder cannot drift apart silently."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, ((LEASE_ID, CURRENT),))])
    store = build_store(script)

    with pytest.raises(StoreError, match="column"), store.cursor() as cursor:
        select_current_generation(cursor, CLIENT_ID)


def test_a_generation_column_holding_no_number_is_refused() -> None:
    """The fence is an ordering, so a value that orders nothing is not read as one."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, ((LEASE_ID, OWNER, "seven"),))])
    store = build_store(script)

    with pytest.raises(StoreError, match="whole number"), store.cursor() as cursor:
        select_current_generation(cursor, CLIENT_ID)


# ---------------------------------------------------------------------------
# The read and the write are one transaction, in order
# ---------------------------------------------------------------------------


def test_the_generation_read_and_the_write_are_one_transaction_in_order() -> None:
    """The read joins the write's own transaction, ahead of the write and before the commit."""
    script = Script(
        answers=[Answer(LEASE_FRAGMENT, (lease_row(),)), Answer(WRITE_FRAGMENT)],
    )

    assert fenced(build_store(script), CLIENT_ID, CURRENT, write_body(CURRENT)) == CURRENT

    issued = script.issued
    assert issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        WRITE_STATEMENT,
        COMMIT_STATEMENT,
    ]
    assert issued.count(BEGIN_STATEMENT) == 1, "one transaction carries both statements"
    assert script.parameters_of(CURRENT_GENERATION_QUERY) == (CLIENT_ID,)
    assert script.parameters_of(WRITE_STATEMENT) == (RUN_ID, CURRENT)


def test_an_admitted_write_counts_no_refusal(telemetry_sink: io.StringIO) -> None:
    """The counter measures refusals, so an admitted write leaves it untouched."""
    script = Script(
        answers=[Answer(LEASE_FRAGMENT, (lease_row(),)), Answer(WRITE_FRAGMENT)],
    )

    fenced(build_store(script), CLIENT_ID, CURRENT, write_body(CURRENT))

    assert refusals_counted() == 0.0
    assert "superseded" not in telemetry_sink.getvalue()


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def test_a_stale_generation_is_refused_carrying_both_generations() -> None:
    """The caller learns it was superseded rather than only that it failed."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),))])

    with pytest.raises(StaleFencingGeneration) as raised:
        fenced(build_store(script), CLIENT_ID, SUPERSEDED, write_body(SUPERSEDED))

    assert raised.value.presented == SUPERSEDED
    assert raised.value.current == CURRENT
    assert OWNER in "\n".join(raised.value.__notes__)


def test_a_refused_write_persists_nothing() -> None:
    """The write is not sent at all, and the transaction is abandoned rather than committed."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),))])

    with pytest.raises(StaleFencingGeneration):
        fenced(build_store(script), CLIENT_ID, SUPERSEDED, write_body(SUPERSEDED))

    issued = script.issued
    assert WRITE_STATEMENT not in issued
    assert COMMIT_STATEMENT not in issued
    assert issued == [BEGIN_STATEMENT, SERIALIZABLE_STATEMENT, CURRENT_GENERATION_QUERY]
    statements = script.statements
    assert statements.index(CURRENT_GENERATION_QUERY) < statements.index(ROLLBACK_STATEMENT)


def test_a_refused_write_is_counted_once(telemetry_sink: io.StringIO) -> None:
    """One undimensioned measurement per refused write, and the identities in the record."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),))])

    with pytest.raises(StaleFencingGeneration):
        fenced(build_store(script), CLIENT_ID, SUPERSEDED, write_body(SUPERSEDED))

    assert refusals_counted() == 1.0
    assert (STALE_GENERATION_METRIC, ()) in instance().counters()
    written = telemetry_sink.getvalue()
    assert str(CLIENT_ID) in written
    assert OWNER in written


def test_a_generation_above_the_current_one_is_refused_too() -> None:
    """The predicate admits the current generation, not any generation that is not stale."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),))])

    with pytest.raises(StaleFencingGeneration) as raised:
        fenced(build_store(script), CLIENT_ID, TAKEN_OVER, write_body(TAKEN_OVER))

    assert raised.value.presented == TAKEN_OVER
    assert raised.value.current == CURRENT
    assert WRITE_STATEMENT not in script.issued


def test_a_presented_generation_below_the_floor_sends_no_statement() -> None:
    """A value naming no granted lease is refused before the cluster is asked."""
    script = Script()

    with pytest.raises(ValueError, match="fencing generation"):
        fenced(build_store(script), CLIENT_ID, FIRST_GENERATION - 1, write_body(CURRENT))

    assert CURRENT_GENERATION_QUERY not in script.statements
    assert WRITE_STATEMENT not in script.statements


# ---------------------------------------------------------------------------
# A Client holding no current lease
# ---------------------------------------------------------------------------


def test_a_client_holding_no_lease_refuses_the_write_as_not_held(
    telemetry_sink: io.StringIO,
) -> None:
    """No current lease is a write belonging to no owner, and it is not a supersession."""
    script = Script(answers=[Answer(LEASE_FRAGMENT)])

    with pytest.raises(LeaseNotHeld, match="no current erasure lease"):
        fenced(build_store(script), CLIENT_ID, CURRENT, write_body(CURRENT))

    assert WRITE_STATEMENT not in script.issued
    assert COMMIT_STATEMENT not in script.issued
    assert refusals_counted() == 0.0
    assert "no current erasure lease" in telemetry_sink.getvalue()


# ---------------------------------------------------------------------------
# The interaction with the retry wrapper
# ---------------------------------------------------------------------------


def test_the_supersession_refusal_carries_no_serialization_state() -> None:
    """This is what makes the wrapper propagate the refusal rather than retry it."""
    assert not is_serialization_failure(StaleFencingGeneration(SUPERSEDED, CURRENT))


def test_a_conflicting_write_is_retried_and_the_generation_is_read_again() -> None:
    """The read runs inside each attempt, so a retry re-reads rather than trusting the first."""
    script = Script(
        answers=[
            Answer(LEASE_FRAGMENT, (lease_row(),)),
            Answer(WRITE_FRAGMENT, error=DriverFailureError("40001")),
            Answer(LEASE_FRAGMENT, (lease_row(),)),
            Answer(WRITE_FRAGMENT),
        ]
    )

    assert fenced(build_store(script), CLIENT_ID, CURRENT, write_body(CURRENT)) == CURRENT

    issued = script.issued
    assert issued.count(BEGIN_STATEMENT) == 2
    assert issued.count(CURRENT_GENERATION_QUERY) == 2
    assert issued.count(WRITE_STATEMENT) == 2
    assert issued.count(COMMIT_STATEMENT) == 1


def test_a_retry_that_sees_a_bumped_generation_refuses_rather_than_loops() -> None:
    """A superseded write cannot become admissible by running again, so it is not run again."""
    script = Script(
        answers=[
            Answer(LEASE_FRAGMENT, (lease_row(),)),
            Answer(WRITE_FRAGMENT, error=DriverFailureError("40001")),
            Answer(LEASE_FRAGMENT, (lease_row(TAKEN_OVER),)),
        ]
    )

    with pytest.raises(StaleFencingGeneration) as raised:
        fenced(build_store(script), CLIENT_ID, CURRENT, write_body(CURRENT))

    assert raised.value.presented == CURRENT
    assert raised.value.current == TAKEN_OVER
    issued = script.issued
    assert issued.count(BEGIN_STATEMENT) == 2, "the refusal ended the second attempt"
    assert issued.count(WRITE_STATEMENT) == 1, "the second attempt wrote nothing"
    assert COMMIT_STATEMENT not in issued
    statements = script.statements
    re_read = len(statements) - 1 - statements[::-1].index(CURRENT_GENERATION_QUERY)
    assert statements[re_read + 1] == ROLLBACK_STATEMENT, "the re-read refused there and then"


# ---------------------------------------------------------------------------
# The three named forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("form", [fenced_disposition, fenced_run_completion, fenced_certificate])
def test_each_named_form_guards_its_body_in_one_transaction(
    form: Callable[[MemoryStore, UUID, int, Callable[[Cursor], int]], int],
) -> None:
    """Every evidence write goes through the same predicate ahead of the same body."""
    script = Script(
        answers=[Answer(LEASE_FRAGMENT, (lease_row(),)), Answer(WRITE_FRAGMENT)],
    )

    assert form(build_store(script), CLIENT_ID, CURRENT, write_body(CURRENT)) == CURRENT

    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        WRITE_STATEMENT,
        COMMIT_STATEMENT,
    ]


@pytest.mark.parametrize("form", [fenced_disposition, fenced_run_completion, fenced_certificate])
def test_each_named_form_refuses_a_superseded_owner(
    form: Callable[[MemoryStore, UUID, int, Callable[[Cursor], int]], int],
) -> None:
    """A stale owner can neither record evidence, declare a run finished, nor sign for it."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),))])

    with pytest.raises(StaleFencingGeneration):
        form(build_store(script), CLIENT_ID, SUPERSEDED, write_body(SUPERSEDED))

    assert WRITE_STATEMENT not in script.issued


def test_the_named_forms_are_told_apart_by_their_labels() -> None:
    """The label is what makes a refused evidence write identifiable in a log record."""
    labels = [label for _name, label in NAMED_FORMS]
    assert len(set(labels)) == len(labels)
    assert FENCED_LABEL not in labels


# ---------------------------------------------------------------------------
# The reading itself
# ---------------------------------------------------------------------------


def test_a_reading_admits_only_the_generation_it_holds() -> None:
    """The predicate is equality, not an ordering comparison."""
    held = CurrentGeneration(lease_id=LEASE_ID, owner=OWNER, generation=CURRENT)

    assert held.admits(CURRENT)
    assert not held.admits(SUPERSEDED)
    assert not held.admits(TAKEN_OVER)


def test_a_reading_refuses_a_lease_that_names_no_owner() -> None:
    """A refusal reports whom to ask, so a reading without an owner is no reading."""
    with pytest.raises(ValueError, match="owner"):
        CurrentGeneration(lease_id=LEASE_ID, owner="", generation=CURRENT)


def test_a_reading_refuses_a_generation_below_the_floor() -> None:
    """The schema refuses one too, and an ordering starting below one orders nothing."""
    with pytest.raises(ValueError, match="fencing generation"):
        CurrentGeneration(lease_id=LEASE_ID, owner=OWNER, generation=FIRST_GENERATION - 1)
