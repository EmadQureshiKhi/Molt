"""Unit tests for the lease lifecycle: grant, refusal, renewal, transfer, finalisation.

Nothing here opens a socket. A scripted cursor answers each statement and keeps
what it was sent, so every claim below is read off the statements the modules
produced and the order they produced them in. The claims that need a cluster to
mean anything, that two racing grants really conflict and that the cluster's own
reading decides expiry, live in the instance-backed module.

Seven properties of the shape are checked.

A grant reads the current lease and the tenant's historical generation maximum, and
inserts, all inside one explicit serialisable transaction. The assertions are
positional, because a maximum read outside the transaction that used it would
satisfy a test asserting only that the arithmetic is right, and would still admit
two workers to the same generation.

A generation is the historical maximum plus one. The maximum the script reports
spans closed leases, so a takeover of a low-generation lease still records a
generation above every generation the tenant ever held.

A contended acquisition is refused, and the refusal names the winner: the current
owner, the current generation, and in a note the lease and the instant its window
closes. The refused attempt writes nothing.

A repeat of one granting attempt is that attempt. The same owner presenting the
same key gets the lease it already holds and no statement is sent that would write
a second one.

A transfer is two statements in one transaction, in the stated order: the current
lease is closed naming the successor's identifier, and only then is the successor
inserted. There is no common table expression anywhere in either.

A renewal and a release both run behind the fence, so a superseded owner extends
nothing and surrenders nothing, and neither of them closes a lease: a release gives
the window back and leaves the row current, because closure names a successor and a
worker finishing cleanly has none.

A finalisation is conditional on the run not being finalised already, so a repeat
matches no row, mutates nothing, and reports the outcome the first one recorded.

**Validates: Requirements 44.1, 44.2, 44.3, 44.4, 44.5, 44.6, 44.9, 44.10, 44.16,
44.17, 44.18**
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.erase.lease import (
    ACQUIRE_LABEL,
    LEASE_TAKEOVER_METRIC,
    RELEASE_LABEL,
    RENEW_LABEL,
    LeaseGrant,
    acquire,
    current,
    finalisation_for,
    finalise,
    next_generation,
    owner_identifier,
    register_run,
    release,
    renew,
)
from molt.errors import LeaseNotHeld, LeaseRefused, StaleFencingGeneration, StoreError
from molt.models.event import JsonObject
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.erasure_lease import (
    CLOSE_LEASE_STATEMENT,
    CURRENT_LEASE_QUERY,
    FINALISATION_QUERY,
    HIGHEST_GENERATION_QUERY,
    INSERT_LEASE_STATEMENT,
    MARK_FINALISED_STATEMENT,
    NO_GENERATION,
    RECORD_RUN_KEY_STATEMENT,
    RENEW_LEASE_STATEMENT,
    SURRENDER_LEASE_STATEMENT,
    LeaseInterval,
    LeaseRecord,
)
from molt.store.fencing import CURRENT_GENERATION_QUERY, FIRST_GENERATION
from molt.store.retry import BEGIN_STATEMENT, COMMIT_STATEMENT, SERIALIZABLE_STATEMENT
from molt.telemetry import Telemetry, configure, reset
from molt.telemetry import current as current_telemetry

# The fragments the script matches an answer to a statement by. Each names a phrase
# holding in exactly one statement of the lease surface.
CURRENT_FRAGMENT: Final[str] = "expires_at < now() AS expired"
MAXIMUM_FRAGMENT: Final[str] = "coalesce(max(generation)"
CLOSE_FRAGMENT: Final[str] = "SET superseded_at"
INSERT_FRAGMENT: Final[str] = "INSERT INTO erasure_lease"
RENEW_FRAGMENT: Final[str] = "renewed_at = coalesce"
SURRENDER_FRAGMENT: Final[str] = "greatest(coalesce("
OWNERSHIP_FRAGMENT: Final[str] = "SET idempotency_key"
FINALISE_FRAGMENT: Final[str] = "SET finalised_at"
RECORDED_FRAGMENT: Final[str] = "FROM erasure_run WHERE idempotency_key"
FENCE_FRAGMENT: Final[str] = "SELECT id, owner, generation FROM erasure_lease"

# The tenant, the run, and the two leases every example here reads.
CLIENT_ID: Final[UUID] = uuid4()
RUN_ID: Final[UUID] = uuid4()
HELD_LEASE_ID: Final[UUID] = uuid4()
SUCCESSOR_LEASE_ID: Final[UUID] = uuid4()

# The two workers contending, and the keys their attempts are identified by.
FIRST_OWNER: Final[str] = "worker-first"
SECOND_OWNER: Final[str] = "worker-second"
FIRST_KEY: Final[str] = "attempt-first"
SECOND_KEY: Final[str] = "attempt-second"

# The generation a scripted lease holds, the maximum a history reports beyond it,
# and the generation a takeover of that history records. Three distinct values, so
# no assertion is satisfied by a coincidence between them.
HELD_GENERATION: Final[int] = 3
HISTORICAL_MAXIMUM: Final[int] = 9
AFTER_TAKEOVER: Final[int] = 10

# How long a lease holds ownership for when nothing is configured. Stated here as
# the expectation rather than imported from the module under test, because the
# module holds no number of its own: the default lives on the configuration surface,
# and a test reading it from the same place the code does would assert only that one
# constant equals itself.
DEFAULT_LEASE_SECONDS: Final[int] = 30

# The window a scripted lease holds, and the interval a write binds. The interval is
# not the configured default, so no expectation about it is met by the surface.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
WINDOW_END: Final[datetime] = MOMENT + timedelta(seconds=DEFAULT_LEASE_SECONDS)
INTERVAL: Final[LeaseInterval] = LeaseInterval(seconds=45)

# The outcome a finalisation records.
RESULT: Final[JsonObject] = {"status": "completed", "dispositions": 4}


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
        """Release this cursor."""


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


# ---------------------------------------------------------------------------
# The rows the scripts answer with
# ---------------------------------------------------------------------------


def lease_row(
    *,
    lease_id: UUID = HELD_LEASE_ID,
    owner: str = FIRST_OWNER,
    generation: int = HELD_GENERATION,
    key: str = FIRST_KEY,
) -> tuple[object, ...]:
    """One stored lease, of the width every lease statement returns."""
    return (lease_id, CLIENT_ID, owner, generation, key, MOMENT, WINDOW_END)


def state_row(*, expired: bool) -> tuple[object, ...]:
    """One current-lease reading, the cluster's expiry verdict included."""
    return (*lease_row(), expired)


def fence_row(generation: int = AFTER_TAKEOVER) -> tuple[object, ...]:
    """The row the fence's own generation read returns."""
    return (HELD_LEASE_ID, FIRST_OWNER, generation)


def finalised_row(generation: int = HELD_GENERATION) -> tuple[object, ...]:
    """The row a finalisation marking or a recorded-finalisation read returns."""
    return (RUN_ID, FIRST_KEY, MOMENT, '{"status":"completed"}', generation)


def held_grant(generation: int = HELD_GENERATION) -> LeaseGrant:
    """A grant a holder acts through, at a generation the scripted fence admits."""
    return LeaseGrant(
        lease=LeaseRecord(
            lease_id=HELD_LEASE_ID,
            client_id=CLIENT_ID,
            owner=FIRST_OWNER,
            generation=generation,
            idempotency_key=FIRST_KEY,
            acquired_at=MOMENT,
            expires_at=WINDOW_END,
        )
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
    return current_telemetry()


def takeovers_counted() -> float:
    """How many transfers of ownership the process-wide instance has counted."""
    return instance().counters().get((LEASE_TAKEOVER_METRIC, ()), 0.0)


# ---------------------------------------------------------------------------
# The shape of the statements
# ---------------------------------------------------------------------------


def test_the_expiry_verdict_is_the_clusters_own_reading() -> None:
    """Takeover admission turns on the cluster's clock, so no instant is bound for it.

    The comparison is written into the statement against the cluster's reading, and
    the tenant is the only value the read binds. A bound comparison instant would be
    a worker's clock deciding who owns an erasure.
    """
    assert "expires_at < now()" in CURRENT_LEASE_QUERY
    assert CURRENT_LEASE_QUERY.count("%s") == 1
    assert "superseded_at IS NULL" in CURRENT_LEASE_QUERY


def test_the_history_maximum_spans_closed_leases() -> None:
    """The maximum is over the tenant's whole history, not over its current lease."""
    assert "max(generation)" in HIGHEST_GENERATION_QUERY
    assert "superseded" not in HIGHEST_GENERATION_QUERY


def test_the_supersession_is_two_statements_and_no_common_table_expression() -> None:
    """Closing and inserting are separate statements, each without a leading term."""
    for statement in (CLOSE_LEASE_STATEMENT, INSERT_LEASE_STATEMENT):
        assert not statement.upper().startswith("WITH")
        assert " AS (" not in statement
    assert "superseded_by = %s" in CLOSE_LEASE_STATEMENT
    assert "superseded_at IS NULL" in CLOSE_LEASE_STATEMENT


def test_a_release_moves_no_closure_column() -> None:
    """Surrendering a window is not closing a lease, so the closure pair is untouched."""
    assert "superseded_at = " not in SURRENDER_LEASE_STATEMENT
    assert "superseded_by" not in SURRENDER_LEASE_STATEMENT
    assert "expires_at = " in SURRENDER_LEASE_STATEMENT


def test_the_finalisation_marking_carries_its_own_state_guard() -> None:
    """The condition in the statement is what makes a repeat mutate nothing."""
    assert "finalised_at IS NULL" in MARK_FINALISED_STATEMENT
    assert "idempotency_key = %s" in MARK_FINALISED_STATEMENT
    assert "finalised_at IS NOT NULL" in FINALISATION_QUERY


# ---------------------------------------------------------------------------
# The generation arithmetic
# ---------------------------------------------------------------------------


def test_the_first_grant_of_a_tenant_takes_the_first_generation() -> None:
    """An empty history reports the floor, and one above the floor is the first fence."""
    assert next_generation(NO_GENERATION) == FIRST_GENERATION


def test_a_generation_is_the_historical_maximum_plus_one() -> None:
    """One rule for a first grant and for every takeover."""
    assert next_generation(HISTORICAL_MAXIMUM) == AFTER_TAKEOVER


def test_a_maximum_below_the_floor_names_no_history() -> None:
    """A number no stored lease and no empty history could report is refused."""
    with pytest.raises(ValueError, match="fencing generation"):
        next_generation(NO_GENERATION - 1)


def test_an_interval_gives_a_lease_a_window() -> None:
    """An interval that covers no time would grant ownership that never held."""
    with pytest.raises(ValueError, match="positive"):
        LeaseInterval(seconds=0)
    assert INTERVAL.expiry_from(MOMENT) == MOMENT + timedelta(seconds=INTERVAL.seconds)


def test_the_configured_interval_defaults_to_thirty_seconds() -> None:
    """The default lives on the configuration surface, and this is what it resolves to."""
    surface = Configuration(environ={}, file_values={})
    assert LeaseInterval.from_configuration(surface).seconds == DEFAULT_LEASE_SECONDS


def test_a_configured_owner_is_used_as_it_stands() -> None:
    """An operator naming its workers gets those names in every refusal."""
    named = Configuration(environ={"MOLT_LEASE_OWNER": SECOND_OWNER}, file_values={})
    assert owner_identifier(named) == SECOND_OWNER


def test_an_unnamed_owner_is_derived_rather_than_shared() -> None:
    """Two workers on one host must not hold leases under one identity."""
    derived = owner_identifier(Configuration(environ={"MOLT_LEASE_OWNER": ""}, file_values={}))
    assert derived
    assert derived != SECOND_OWNER


# ---------------------------------------------------------------------------
# A first grant
# ---------------------------------------------------------------------------


def test_a_first_grant_reads_and_writes_in_one_transaction() -> None:
    """The reads the generation rests on sit inside the transaction that inserts it."""
    script = Script(
        answers=[
            Answer(CURRENT_FRAGMENT),
            Answer(MAXIMUM_FRAGMENT, ((NO_GENERATION,),)),
            Answer(INSERT_FRAGMENT, (lease_row(generation=FIRST_GENERATION),)),
        ]
    )

    grant = acquire(
        build_store(script),
        CLIENT_ID,
        FIRST_OWNER,
        FIRST_KEY,
        interval=INTERVAL,
        lease_id=HELD_LEASE_ID,
    )

    assert grant.generation == FIRST_GENERATION
    assert not grant.took_over
    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_LEASE_QUERY,
        HIGHEST_GENERATION_QUERY,
        INSERT_LEASE_STATEMENT,
        COMMIT_STATEMENT,
    ]
    assert CLOSE_LEASE_STATEMENT not in script.issued, "nothing was current to close"


def test_a_first_grant_binds_the_interval_and_leaves_the_anchor_to_the_cluster() -> None:
    """The deployed path supplies no reading of its own, so both instants are the cluster's."""
    script = Script(
        answers=[
            Answer(CURRENT_FRAGMENT),
            Answer(MAXIMUM_FRAGMENT, ((NO_GENERATION,),)),
            Answer(INSERT_FRAGMENT, (lease_row(generation=FIRST_GENERATION),)),
        ]
    )

    acquire(
        build_store(script),
        CLIENT_ID,
        FIRST_OWNER,
        FIRST_KEY,
        interval=INTERVAL,
        lease_id=HELD_LEASE_ID,
    )

    assert script.parameters_of(INSERT_LEASE_STATEMENT) == (
        HELD_LEASE_ID,
        CLIENT_ID,
        FIRST_OWNER,
        FIRST_GENERATION,
        FIRST_KEY,
        None,
        None,
        INTERVAL.seconds,
    )


def test_an_acquisition_naming_no_owner_sends_no_statement() -> None:
    """Ownership is an identity, so an acquisition without one is refused before the cluster."""
    script = Script()

    with pytest.raises(ValueError, match="owner"):
        acquire(build_store(script), CLIENT_ID, "", FIRST_KEY, interval=INTERVAL)

    assert script.statements == []


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_a_current_lease_refuses_another_owner_naming_the_winner(
    telemetry_sink: io.StringIO,
) -> None:
    """The loser of a contest learns who won and when it may ask again."""
    script = Script(answers=[Answer(CURRENT_FRAGMENT, (state_row(expired=False),))])

    with pytest.raises(LeaseRefused) as raised:
        acquire(
            build_store(script),
            CLIENT_ID,
            SECOND_OWNER,
            SECOND_KEY,
            interval=INTERVAL,
        )

    assert raised.value.owner == FIRST_OWNER
    assert raised.value.generation == HELD_GENERATION
    notes = "\n".join(raised.value.__notes__)
    assert str(HELD_LEASE_ID) in notes
    assert WINDOW_END.isoformat() in notes
    assert FIRST_OWNER in telemetry_sink.getvalue()


def test_a_refused_acquisition_writes_nothing() -> None:
    """No lease is inserted, nothing is closed, and the transaction is not committed."""
    script = Script(answers=[Answer(CURRENT_FRAGMENT, (state_row(expired=False),))])

    with pytest.raises(LeaseRefused):
        acquire(build_store(script), CLIENT_ID, SECOND_OWNER, SECOND_KEY, interval=INTERVAL)

    issued = script.issued
    assert INSERT_LEASE_STATEMENT not in issued
    assert CLOSE_LEASE_STATEMENT not in issued
    assert HIGHEST_GENERATION_QUERY not in issued, "the maximum is read only once a grant may go on"
    assert COMMIT_STATEMENT not in issued


def test_a_uniqueness_collision_is_reported_as_the_same_refusal() -> None:
    """Two racing grants end the same way whichever refusal the platform reports."""
    script = Script(
        answers=[
            Answer(CURRENT_FRAGMENT),
            Answer(MAXIMUM_FRAGMENT, ((NO_GENERATION,),)),
            Answer(INSERT_FRAGMENT, error=DriverFailureError("23505")),
            Answer(CURRENT_FRAGMENT, (state_row(expired=False),)),
        ]
    )

    with pytest.raises(LeaseRefused) as raised:
        acquire(build_store(script), CLIENT_ID, SECOND_OWNER, SECOND_KEY, interval=INTERVAL)

    assert raised.value.owner == FIRST_OWNER
    assert raised.value.generation == HELD_GENERATION


def test_a_repeat_of_one_attempt_returns_the_lease_it_already_holds() -> None:
    """A granting attempt is identified once, so a repeat contends with nobody."""
    script = Script(answers=[Answer(CURRENT_FRAGMENT, (state_row(expired=False),))])

    grant = acquire(
        build_store(script),
        CLIENT_ID,
        FIRST_OWNER,
        FIRST_KEY,
        interval=INTERVAL,
    )

    assert grant.lease_id == HELD_LEASE_ID
    assert grant.generation == HELD_GENERATION
    assert not grant.took_over
    assert INSERT_LEASE_STATEMENT not in script.issued
    assert CLOSE_LEASE_STATEMENT not in script.issued


# ---------------------------------------------------------------------------
# Takeover
# ---------------------------------------------------------------------------


def takeover_script() -> Script:
    """A history whose current lease has expired and whose maximum lies above it."""
    return Script(
        answers=[
            Answer(CURRENT_FRAGMENT, (state_row(expired=True),)),
            Answer(MAXIMUM_FRAGMENT, ((HISTORICAL_MAXIMUM,),)),
            Answer(CLOSE_FRAGMENT, (lease_row(),)),
            Answer(
                INSERT_FRAGMENT,
                (
                    lease_row(
                        lease_id=SUCCESSOR_LEASE_ID,
                        owner=SECOND_OWNER,
                        generation=AFTER_TAKEOVER,
                        key=SECOND_KEY,
                    ),
                ),
            ),
        ]
    )


def take_over(script: Script) -> LeaseGrant:
    """Take over the expired lease the script holds, under the second owner."""
    return acquire(
        build_store(script),
        CLIENT_ID,
        SECOND_OWNER,
        SECOND_KEY,
        interval=INTERVAL,
        lease_id=SUCCESSOR_LEASE_ID,
    )


def test_a_takeover_closes_the_expired_lease_before_inserting_its_successor() -> None:
    """The two statements are ordered, and both are inside one transaction."""
    script = takeover_script()

    grant = take_over(script)

    assert grant.superseded == HELD_LEASE_ID
    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_LEASE_QUERY,
        HIGHEST_GENERATION_QUERY,
        CLOSE_LEASE_STATEMENT,
        INSERT_LEASE_STATEMENT,
        COMMIT_STATEMENT,
    ]


def test_a_takeover_names_the_successor_in_the_closing_statement() -> None:
    """The closed lease names the lease that replaced it, which the successor then becomes."""
    script = takeover_script()

    grant = take_over(script)

    assert script.parameters_of(CLOSE_LEASE_STATEMENT) == (None, SUCCESSOR_LEASE_ID, HELD_LEASE_ID)
    assert grant.lease_id == SUCCESSOR_LEASE_ID


def test_a_takeover_generation_exceeds_every_generation_the_tenant_held() -> None:
    """The maximum spans closed leases, so a successor cannot repeat a retired fence."""
    script = takeover_script()

    grant = take_over(script)

    assert grant.generation == AFTER_TAKEOVER
    assert grant.generation > HISTORICAL_MAXIMUM > HELD_GENERATION
    bound = script.parameters_of(INSERT_LEASE_STATEMENT)
    assert bound is not None
    assert bound[3] == AFTER_TAKEOVER


def test_a_takeover_is_counted_once_and_recorded(telemetry_sink: io.StringIO) -> None:
    """The measurement is undimensioned and the identities travel in the log record."""
    take_over(takeover_script())

    assert takeovers_counted() == 1.0
    written = telemetry_sink.getvalue()
    assert str(CLIENT_ID) in written
    assert str(HELD_LEASE_ID) in written


def test_a_grant_that_took_nothing_over_is_not_counted(telemetry_sink: io.StringIO) -> None:
    """A first grant transfers no ownership, so it is not a transfer."""
    script = Script(
        answers=[
            Answer(CURRENT_FRAGMENT),
            Answer(MAXIMUM_FRAGMENT, ((NO_GENERATION,),)),
            Answer(INSERT_FRAGMENT, (lease_row(generation=FIRST_GENERATION),)),
        ]
    )

    acquire(build_store(script), CLIENT_ID, FIRST_OWNER, FIRST_KEY, interval=INTERVAL)

    assert takeovers_counted() == 0.0
    assert "taken over" not in telemetry_sink.getvalue()


def test_a_lease_still_inside_its_window_is_not_takeable() -> None:
    """An unrenewed lease belongs to its owner however silent that owner has gone."""
    script = Script(answers=[Answer(CURRENT_FRAGMENT, (state_row(expired=False),))])

    state = current(build_store(script), CLIENT_ID)

    assert state is not None
    assert not state.takeable
    assert state.owner == FIRST_OWNER
    assert state.generation == HELD_GENERATION


# ---------------------------------------------------------------------------
# Renewal and release
# ---------------------------------------------------------------------------


def test_a_renewal_extends_the_window_behind_the_fence() -> None:
    """The generation read sits ahead of the extension, in the extension's own transaction."""
    script = Script(
        answers=[
            Answer(FENCE_FRAGMENT, (fence_row(HELD_GENERATION),)),
            Answer(RENEW_FRAGMENT, (lease_row(),)),
        ]
    )

    renewed = renew(build_store(script), held_grant(), interval=INTERVAL)

    assert renewed.generation == HELD_GENERATION
    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        RENEW_LEASE_STATEMENT,
        COMMIT_STATEMENT,
    ]
    assert script.parameters_of(RENEW_LEASE_STATEMENT) == (
        None,
        INTERVAL.seconds,
        None,
        HELD_LEASE_ID,
    )


def test_a_renewal_by_a_superseded_owner_extends_nothing() -> None:
    """A worker that lost ownership cannot keep a window open, and learns both generations."""
    script = Script(answers=[Answer(FENCE_FRAGMENT, (fence_row(AFTER_TAKEOVER),))])

    with pytest.raises(StaleFencingGeneration) as raised:
        renew(build_store(script), held_grant(), interval=INTERVAL)

    assert raised.value.presented == HELD_GENERATION
    assert raised.value.current == AFTER_TAKEOVER
    assert RENEW_LEASE_STATEMENT not in script.issued
    assert COMMIT_STATEMENT not in script.issued


def test_a_renewal_for_a_client_holding_no_lease_is_refused_as_not_held() -> None:
    """No current lease is a different answer from a superseded one."""
    script = Script(answers=[Answer(FENCE_FRAGMENT)])

    with pytest.raises(LeaseNotHeld):
        renew(build_store(script), held_grant(), interval=INTERVAL)

    assert RENEW_LEASE_STATEMENT not in script.issued


def test_a_renewal_of_a_lease_no_longer_current_is_reported() -> None:
    """A statement that matched no row means ownership no longer stands."""
    script = Script(
        answers=[Answer(FENCE_FRAGMENT, (fence_row(HELD_GENERATION),)), Answer(RENEW_FRAGMENT)]
    )

    with pytest.raises(StoreError, match="no longer current"):
        renew(build_store(script), held_grant(), interval=INTERVAL)


def test_a_release_gives_the_window_back_without_closing_the_lease() -> None:
    """The row stays current and becomes takeable, so the next acquirer supersedes it."""
    script = Script(
        answers=[
            Answer(FENCE_FRAGMENT, (fence_row(HELD_GENERATION),)),
            Answer(SURRENDER_FRAGMENT, (lease_row(),)),
        ]
    )

    release(build_store(script), held_grant())

    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        SURRENDER_LEASE_STATEMENT,
        COMMIT_STATEMENT,
    ]
    assert CLOSE_LEASE_STATEMENT not in script.issued


def test_a_release_by_a_superseded_owner_shortens_nothing() -> None:
    """A window that is no longer this owner's is not this owner's to give back."""
    script = Script(answers=[Answer(FENCE_FRAGMENT, (fence_row(AFTER_TAKEOVER),))])

    with pytest.raises(StaleFencingGeneration):
        release(build_store(script), held_grant())

    assert SURRENDER_LEASE_STATEMENT not in script.issued


def test_the_lifecycle_transactions_are_told_apart_by_their_labels() -> None:
    """A log record names which act of the lifecycle kept losing, not merely that one did."""
    assert len({ACQUIRE_LABEL, RENEW_LABEL, RELEASE_LABEL}) == 3


# ---------------------------------------------------------------------------
# The run's ownership record and its finalisation
# ---------------------------------------------------------------------------


def test_a_run_records_the_attempt_and_the_generation_behind_the_fence() -> None:
    """A finalisation is attributable because the ownership was recorded when the run began."""
    script = Script(
        answers=[
            Answer(FENCE_FRAGMENT, (fence_row(HELD_GENERATION),)),
            Answer(OWNERSHIP_FRAGMENT, ((RUN_ID, FIRST_KEY, HELD_GENERATION),)),
        ]
    )

    assert register_run(build_store(script), held_grant(), RUN_ID) == FIRST_KEY
    assert script.parameters_of(RECORD_RUN_KEY_STATEMENT) == (
        FIRST_KEY,
        HELD_LEASE_ID,
        HELD_GENERATION,
        RUN_ID,
        CLIENT_ID,
        FIRST_KEY,
    )


def test_a_run_claimed_by_another_attempt_is_refused() -> None:
    """One run carries one attempt's key, so a second attempt cannot adopt it."""
    script = Script(
        answers=[Answer(FENCE_FRAGMENT, (fence_row(HELD_GENERATION),)), Answer(OWNERSHIP_FRAGMENT)]
    )

    with pytest.raises(StoreError, match="another attempt"):
        register_run(build_store(script), held_grant(), RUN_ID)


def test_a_finalisation_marks_the_run_once_and_reports_the_outcome() -> None:
    """The marking runs behind the fence and reports what the cluster recorded."""
    script = Script(
        answers=[
            Answer(FENCE_FRAGMENT, (fence_row(HELD_GENERATION),)),
            Answer(FINALISE_FRAGMENT, (finalised_row(),)),
        ]
    )

    record = finalise(build_store(script), held_grant(), RUN_ID, RESULT)

    assert record.run_id == RUN_ID
    assert record.generation == HELD_GENERATION
    assert record.result == {"status": "completed"}
    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        MARK_FINALISED_STATEMENT,
        COMMIT_STATEMENT,
    ]


def test_a_repeated_finalisation_mutates_nothing_and_returns_the_record() -> None:
    """The state guard matched no row, so the recorded outcome is read and reported."""
    script = Script(
        answers=[
            Answer(FENCE_FRAGMENT, (fence_row(HELD_GENERATION),)),
            Answer(FINALISE_FRAGMENT),
            Answer(RECORDED_FRAGMENT, (finalised_row(),)),
        ]
    )

    record = finalise(build_store(script), held_grant(), RUN_ID, RESULT)

    assert record.run_id == RUN_ID
    assert record.finalised_at == MOMENT
    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        MARK_FINALISED_STATEMENT,
        FINALISATION_QUERY,
        COMMIT_STATEMENT,
    ]
    assert script.statements.count(MARK_FINALISED_STATEMENT) == 1


def test_a_finalisation_by_a_superseded_owner_records_nothing() -> None:
    """A worker that lost the lease may not declare the run finished."""
    script = Script(answers=[Answer(FENCE_FRAGMENT, (fence_row(AFTER_TAKEOVER),))])

    with pytest.raises(StaleFencingGeneration):
        finalise(build_store(script), held_grant(), RUN_ID, RESULT)

    assert MARK_FINALISED_STATEMENT not in script.issued


def test_a_finalisation_naming_no_recorded_attempt_is_reported() -> None:
    """Nothing was finalised and nothing recorded can be reported, so it is said plainly."""
    script = Script(
        answers=[
            Answer(FENCE_FRAGMENT, (fence_row(HELD_GENERATION),)),
            Answer(FINALISE_FRAGMENT),
            Answer(RECORDED_FRAGMENT),
        ]
    )

    with pytest.raises(StoreError, match="no recorded attempt"):
        finalise(build_store(script), held_grant(), RUN_ID, RESULT)


def test_a_recorded_finalisation_is_read_by_its_key_alone() -> None:
    """A resuming attempt asks by key before doing any work."""
    script = Script(answers=[Answer(RECORDED_FRAGMENT, (finalised_row(),))])

    record = finalisation_for(build_store(script), FIRST_KEY)

    assert record is not None
    assert record.idempotency_key == FIRST_KEY
    assert script.parameters_of(FINALISATION_QUERY) == (FIRST_KEY,)
    assert BEGIN_STATEMENT not in script.statements


def test_an_unfinalised_attempt_reports_no_record() -> None:
    """Absent is the answer for an attempt that has not finalised, not a failure."""
    script = Script(answers=[Answer(RECORDED_FRAGMENT)])

    assert finalisation_for(build_store(script), FIRST_KEY) is None
