"""The lease lifecycle against a live instance: grant, refusal, renewal, transfer.

The unit module asserts the statements and the order they are sent in. This module
asserts what only a cluster can answer, and the first of those is the one the design
turns on.

**Expiry is the cluster's verdict, not a worker's.** A takeover is admitted only
once the cluster, reading the row inside the transaction that would supersede it,
reports the window as past. Two cases here make that concrete from both sides: a
worker presenting a reading far in the future is still refused while the stored
window is open, so a skewed clock cannot manufacture ownership; and a lease written
from an anchor already behind the cluster's reading is takeable at once, which is
how a window is reached here by arithmetic instead of by waiting. Nothing in this
module sleeps.

**A generation is the tenant's historical maximum plus one.** Two successive
takeovers are driven, and the closed leases stay resident, so the third grant's
generation is asserted against a history rather than against the count of current
leases.

**A transfer is the ordered supersession the schema is shaped for.** The
predecessor is read back afterwards: it carries both halves of the closure pair, and
the superseding identifier is the successor that exists. A partial closure would be
visible as one column set without the other, which the schema refuses outright.

**A release surrenders the window without closing the lease.** The row stays
current and unclosed and becomes takeable at once, which is the same state an
unrenewed lease reaches on the cluster's clock, so a clean ending and a crash take
one path.

**Finalisation happens once.** The run is finalised, then finalised again, and the
recorded instant, the recorded outcome, and the recorded generation are unchanged by
the repeat.

Every migration is applied, because the run columns a finalisation writes and the
update guard confining which columns of a lease may move both arrive after the table
that holds them.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.erase.lease import (
    LEASE_TAKEOVER_METRIC,
    LeaseGrant,
    acquire,
    current,
    finalisation_for,
    finalise,
    register_run,
    release,
    renew,
)
from molt.errors import LeaseNotHeld, LeaseRefused, StaleFencingGeneration
from molt.models.event import JsonObject
from molt.store import Connection, MemoryStore
from molt.store.erasure_lease import LeaseInterval, LeaseRecord
from molt.store.migrate import apply_migrations
from molt.telemetry import Telemetry, configure, reset
from molt.telemetry import current as current_telemetry

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows every scenario is built from, placed by this module because it owns no
# Client insert and no run insert.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_REQUEST: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification) VALUES (%s, %s, %s, %s)"
)
INSERT_RUN: Final[str] = (
    "INSERT INTO erasure_run (id, request_id, client_id, requester, t_before) "
    "VALUES (%s, %s, %s, %s, now())"
)

# The reads the assertions are made from. Each names the lease history or the run
# row directly, so what is asserted is what the cluster holds rather than what the
# module under test returned.
CLUSTER_READING: Final[str] = "SELECT now()"
SELECT_CLOSURE: Final[str] = (
    "SELECT superseded_at, superseded_by, generation FROM erasure_lease WHERE id = %s"
)
SELECT_WINDOW: Final[str] = (
    "SELECT acquired_at, expires_at, renewed_at FROM erasure_lease WHERE id = %s"
)
COUNT_LEASES: Final[str] = "SELECT count(*) FROM erasure_lease WHERE client_id = %s"
COUNT_CURRENT_LEASES: Final[str] = (
    "SELECT count(*) FROM erasure_lease WHERE client_id = %s AND superseded_at IS NULL"
)
SELECT_RUN_OWNERSHIP: Final[str] = (
    "SELECT idempotency_key, lease_id, fencing_generation, finalised_at FROM erasure_run "
    "WHERE id = %s"
)

# The values the placed rows carry. None of them is what an assertion turns on.
JURISDICTION: Final[str] = "eu"
REQUESTER: Final[str] = "operator"
JUSTIFICATION: Final[str] = "a governed request"

# The two workers contending, and the interval their leases run for. The interval is
# not the configured default, so an assertion about a stored window cannot be met by
# a value the surface holds.
FIRST_OWNER: Final[str] = "worker-first"
SECOND_OWNER: Final[str] = "worker-second"
THIRD_OWNER: Final[str] = "worker-third"
INTERVAL: Final[LeaseInterval] = LeaseInterval(seconds=40)

# The generations a first grant and the two takeovers after it record.
FIRST_GRANT: Final[int] = 1
AFTER_TAKEOVER: Final[int] = 2
AFTER_SECOND_TAKEOVER: Final[int] = 3

# How far behind the cluster's reading an already-elapsed window is anchored, and
# how far ahead of it a skewed worker's reading is placed. Both comfortably beyond
# the interval, so neither case rests on a fine margin.
ELAPSED: Final[timedelta] = timedelta(seconds=600)
SKEW: Final[timedelta] = timedelta(seconds=3600)

# The outcome a finalisation records, and the one a repeat offers instead.
RESULT: Final[JsonObject] = {"status": "completed", "dispositions": 3}
SECOND_RESULT: Final[JsonObject] = {"status": "completed", "dispositions": 99}

# A connection and a cursor are typed loosely because the driver is reached through
# a fixture rather than imported, which keeps this module collectable with no driver
# installed.
DriverConnection = Any


@dataclass(frozen=True, slots=True)
class Scenario:
    """One tenant and one run of it, with no lease yet granted."""

    client_id: UUID
    run_id: UUID


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The ownership columns one run row carries, read straight from the cluster."""

    idempotency_key: str | None
    lease_id: UUID | None
    generation: int | None
    finalised_at: datetime | None


class ManualClock(Protocol):
    """The three calls this module makes on the injected clock.

    Declared structurally rather than imported, because the clock is delivered by a
    fixture and a test module reaches a fixture by name rather than by import.
    """

    def now(self) -> datetime:
        """The current wall reading, timezone aware."""

    def advance(self, seconds: float) -> None:
        """Move the reading forward by a non-negative number of seconds."""

    def set_now(self, instant: datetime) -> None:
        """Place the reading at a chosen timezone-aware instant."""


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, and a store over it."""

    store: MemoryStore
    connection: DriverConnection

    def scenario(self) -> Scenario:
        """Place a tenant, a request, and a run, and report them.

        Each scenario carries its own tenant, so the uniqueness admitting one
        current lease per tenant never brings two examples into contact.
        """
        client_id = uuid4()
        request_id = uuid4()
        run_id = uuid4()
        slug = f"tenant-{client_id.hex[:12]}"
        self.send(INSERT_CLIENT, (client_id, slug, "Tenant", JURISDICTION))
        self.send(INSERT_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION))
        self.send(INSERT_RUN, (run_id, request_id, client_id, REQUESTER))
        return Scenario(client_id=client_id, run_id=run_id)

    def send(self, statement: str, params: tuple[object, ...]) -> None:
        """Send one parameterised statement on this module's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)

    def row(self, statement: str, params: tuple[object, ...]) -> tuple[object, ...]:
        """The one row a read of this module's own returns."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            row = cursor.fetchone()
        assert row is not None
        return tuple(row)

    def reading(self) -> datetime:
        """The cluster's current reading, which every anchor here is placed against."""
        moment = self.row(CLUSTER_READING, ())[0]
        assert isinstance(moment, datetime)
        return moment

    def closure(self, lease_id: UUID) -> tuple[datetime | None, UUID | None, int]:
        """The closure pair one lease carries, and the generation it recorded."""
        superseded_at, superseded_by, generation = self.row(SELECT_CLOSURE, (lease_id,))
        assert superseded_at is None or isinstance(superseded_at, datetime)
        assert superseded_by is None or isinstance(superseded_by, UUID)
        return superseded_at, superseded_by, int(str(generation))

    def window(self, lease_id: UUID) -> tuple[datetime, datetime, datetime | None]:
        """The window one lease holds, and the instant it was last renewed at."""
        acquired_at, expires_at, renewed_at = self.row(SELECT_WINDOW, (lease_id,))
        assert isinstance(acquired_at, datetime)
        assert isinstance(expires_at, datetime)
        assert renewed_at is None or isinstance(renewed_at, datetime)
        return acquired_at, expires_at, renewed_at

    def leases(self, client_id: UUID) -> tuple[int, int]:
        """How many leases this tenant has ever held, and how many are current."""
        return (
            int(str(self.row(COUNT_LEASES, (client_id,))[0])),
            int(str(self.row(COUNT_CURRENT_LEASES, (client_id,))[0])),
        )

    def ownership(self, run_id: UUID) -> RunRecord:
        """What one run records about the ownership it is performed under."""
        key, lease_id, generation, finalised_at = self.row(SELECT_RUN_OWNERSHIP, (run_id,))
        assert key is None or isinstance(key, str)
        assert lease_id is None or isinstance(lease_id, UUID)
        assert finalised_at is None or isinstance(finalised_at, datetime)
        return RunRecord(
            idempotency_key=key,
            lease_id=lease_id,
            generation=None if generation is None else int(str(generation)),
            finalised_at=finalised_at,
        )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store bound to that schema."""
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


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


def key_for(owner: str) -> str:
    """One granting attempt's key, unique across every example in the module."""
    return f"{owner}-{uuid4().hex[:12]}"


def grant_for(
    cluster: Cluster,
    scenario: Scenario,
    owner: str,
    *,
    now: datetime | None = None,
) -> LeaseGrant:
    """Acquire erasure ownership of one tenant under one owner."""
    return acquire(
        cluster.store,
        scenario.client_id,
        owner,
        key_for(owner),
        interval=INTERVAL,
        now=now,
    )


def elapsed_grant(cluster: Cluster, scenario: Scenario, owner: str) -> LeaseGrant:
    """A lease whose window has already run out on the cluster's own clock.

    The anchor is placed behind the cluster's reading, so the stored window closes
    before the instant the cluster is already at. Nothing waits: the window is
    reached by arithmetic, and the cluster is still the one that judges it.
    """
    return grant_for(cluster, scenario, owner, now=cluster.reading() - ELAPSED)


# ---------------------------------------------------------------------------
# A first grant
# ---------------------------------------------------------------------------


def test_a_first_grant_records_the_first_generation(cluster: Cluster) -> None:
    """A tenant that has never held a lease is granted the first fence."""
    scenario = cluster.scenario()

    grant = grant_for(cluster, scenario, FIRST_OWNER)

    assert grant.generation == FIRST_GRANT
    assert grant.owner == FIRST_OWNER
    assert not grant.took_over
    acquired_at, expires_at, renewed_at = cluster.window(grant.lease_id)
    assert expires_at - acquired_at == INTERVAL.interval
    assert renewed_at is None
    assert cluster.leases(scenario.client_id) == (1, 1)


def test_a_first_grant_is_current_and_not_takeable(cluster: Cluster) -> None:
    """The cluster reads the window as open, so the grant holds until it does not."""
    scenario = cluster.scenario()

    grant = grant_for(cluster, scenario, FIRST_OWNER)
    state = current(cluster.store, scenario.client_id)

    assert state is not None
    assert state.lease_id == grant.lease_id
    assert state.owner == FIRST_OWNER
    assert state.generation == FIRST_GRANT
    assert not state.takeable


def test_a_client_holding_no_lease_reports_none(cluster: Cluster) -> None:
    """Absent is the answer for a tenant nobody has taken erasure ownership of."""
    scenario = cluster.scenario()

    assert current(cluster.store, scenario.client_id) is None


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_a_second_owner_is_refused_while_the_window_is_open(cluster: Cluster) -> None:
    """The loser of a contest learns who won and at which generation."""
    scenario = cluster.scenario()
    held = grant_for(cluster, scenario, FIRST_OWNER)

    with pytest.raises(LeaseRefused) as raised:
        grant_for(cluster, scenario, SECOND_OWNER)

    assert raised.value.owner == FIRST_OWNER
    assert raised.value.generation == held.generation
    assert str(held.lease_id) in "\n".join(raised.value.__notes__)
    assert cluster.leases(scenario.client_id) == (1, 1)


def test_a_skewed_worker_cannot_talk_itself_into_a_takeover(cluster: Cluster) -> None:
    """The reading a requester presents never enters the takeover decision.

    The requester presents an anchor an hour ahead of the cluster, which is what a
    worker with a fast clock would present, and the stored window is still open by
    the cluster's own reading. The refusal is the point: had admission compared the
    stored expiry against the requester's reading, this acquisition would have
    succeeded and two workers would have held one erasure.
    """
    scenario = cluster.scenario()
    held = grant_for(cluster, scenario, FIRST_OWNER)

    with pytest.raises(LeaseRefused) as raised:
        grant_for(cluster, scenario, SECOND_OWNER, now=cluster.reading() + SKEW)

    assert raised.value.generation == held.generation
    assert cluster.leases(scenario.client_id) == (1, 1)


def test_a_repeated_attempt_returns_the_lease_it_already_holds(cluster: Cluster) -> None:
    """A granting attempt is identified once, so a repeat contends with nobody."""
    scenario = cluster.scenario()
    key = key_for(FIRST_OWNER)

    first = acquire(cluster.store, scenario.client_id, FIRST_OWNER, key, interval=INTERVAL)
    again = acquire(cluster.store, scenario.client_id, FIRST_OWNER, key, interval=INTERVAL)

    assert again.lease_id == first.lease_id
    assert again.generation == first.generation
    assert cluster.leases(scenario.client_id) == (1, 1)


# ---------------------------------------------------------------------------
# Renewal
# ---------------------------------------------------------------------------


def test_a_renewal_extends_the_window_and_stamps_the_renewal(cluster: Cluster) -> None:
    """The holder keeps ownership by moving the expiry the cluster judges it by."""
    scenario = cluster.scenario()
    grant = grant_for(cluster, scenario, FIRST_OWNER)
    _acquired_at, before, _unrenewed = cluster.window(grant.lease_id)

    renewed = renew(cluster.store, grant, interval=INTERVAL)

    _again, after, renewed_at = cluster.window(renewed.lease_id)
    assert after > before
    assert renewed_at is not None
    assert renewed.generation == grant.generation
    assert renewed.lease_id == grant.lease_id


def test_a_renewal_by_a_superseded_owner_extends_nothing(cluster: Cluster) -> None:
    """A worker that lost ownership cannot keep its window open, and learns both fences."""
    scenario = cluster.scenario()
    lapsed = elapsed_grant(cluster, scenario, FIRST_OWNER)
    successor = grant_for(cluster, scenario, SECOND_OWNER)
    _acquired_at, before, _unrenewed = cluster.window(lapsed.lease_id)

    with pytest.raises(StaleFencingGeneration) as raised:
        renew(cluster.store, lapsed, interval=INTERVAL)

    assert raised.value.presented == lapsed.generation
    assert raised.value.current == successor.generation
    _again, after, renewed_at = cluster.window(lapsed.lease_id)
    assert after == before
    assert renewed_at is None


# ---------------------------------------------------------------------------
# Takeover
# ---------------------------------------------------------------------------


def test_a_takeover_is_admitted_once_the_window_has_passed(
    cluster: Cluster,
    telemetry_sink: io.StringIO,
) -> None:
    """An elapsed window transfers ownership, and the transfer is counted and recorded."""
    scenario = cluster.scenario()
    lapsed = elapsed_grant(cluster, scenario, FIRST_OWNER)
    state = current(cluster.store, scenario.client_id)
    assert state is not None
    assert state.takeable, "the cluster reads the stored window as already past"

    successor = grant_for(cluster, scenario, SECOND_OWNER)

    assert successor.generation == AFTER_TAKEOVER
    assert successor.superseded == lapsed.lease_id
    assert takeovers_counted() == 1.0
    assert str(scenario.client_id) in telemetry_sink.getvalue()


def test_a_takeover_closes_the_predecessor_naming_the_successor(cluster: Cluster) -> None:
    """The closure pair is complete and names a lease that exists."""
    scenario = cluster.scenario()
    lapsed = elapsed_grant(cluster, scenario, FIRST_OWNER)

    successor = grant_for(cluster, scenario, SECOND_OWNER)

    superseded_at, superseded_by, generation = cluster.closure(lapsed.lease_id)
    assert superseded_at is not None
    assert superseded_by == successor.lease_id
    assert generation == lapsed.generation
    assert cluster.closure(successor.lease_id)[:2] == (None, None)
    assert cluster.leases(scenario.client_id) == (2, 1)


def test_a_takeover_generation_exceeds_every_generation_the_tenant_held(cluster: Cluster) -> None:
    """The maximum spans closed leases, so a third grant cannot repeat a retired fence."""
    scenario = cluster.scenario()
    first = elapsed_grant(cluster, scenario, FIRST_OWNER)
    second = acquire(
        cluster.store,
        scenario.client_id,
        SECOND_OWNER,
        key_for(SECOND_OWNER),
        interval=INTERVAL,
        now=cluster.reading() - ELAPSED,
    )

    third = grant_for(cluster, scenario, THIRD_OWNER)

    assert (first.generation, second.generation, third.generation) == (
        FIRST_GRANT,
        AFTER_TAKEOVER,
        AFTER_SECOND_TAKEOVER,
    )
    assert cluster.leases(scenario.client_id) == (3, 1)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_a_release_leaves_the_lease_current_and_takeable_at_once(cluster: Cluster) -> None:
    """Surrendering a window is not closing a lease, so the next acquirer supersedes it."""
    scenario = cluster.scenario()
    grant = grant_for(cluster, scenario, FIRST_OWNER)

    release(cluster.store, grant)

    assert cluster.closure(grant.lease_id)[:2] == (None, None), "a release closes nothing"
    state = current(cluster.store, scenario.client_id)
    assert state is not None
    assert state.lease_id == grant.lease_id
    assert state.takeable
    successor = grant_for(cluster, scenario, SECOND_OWNER)
    assert successor.generation == AFTER_TAKEOVER
    assert successor.superseded == grant.lease_id


def test_a_release_by_a_superseded_owner_shortens_nothing(cluster: Cluster) -> None:
    """A window that is no longer this owner's is not this owner's to give back."""
    scenario = cluster.scenario()
    lapsed = elapsed_grant(cluster, scenario, FIRST_OWNER)
    successor = grant_for(cluster, scenario, SECOND_OWNER)
    _acquired_at, before, _unrenewed = cluster.window(successor.lease_id)

    with pytest.raises(StaleFencingGeneration):
        release(cluster.store, lapsed)

    assert cluster.window(successor.lease_id)[1] == before


# ---------------------------------------------------------------------------
# The run's ownership record and its finalisation
# ---------------------------------------------------------------------------


def test_a_run_records_the_attempt_the_lease_and_the_generation(cluster: Cluster) -> None:
    """A finalisation is attributable because the ownership was recorded at the start."""
    scenario = cluster.scenario()
    grant = grant_for(cluster, scenario, FIRST_OWNER)

    recorded = register_run(cluster.store, grant, scenario.run_id)

    assert recorded == grant.idempotency_key
    assert cluster.ownership(scenario.run_id) == RunRecord(
        idempotency_key=grant.idempotency_key,
        lease_id=grant.lease_id,
        generation=grant.generation,
        finalised_at=None,
    )


def test_a_run_begun_with_no_held_lease_mutates_nothing(cluster: Cluster) -> None:
    """A run whose tenant nobody owns is refused before any column moves."""
    scenario = cluster.scenario()
    unheld = LeaseGrant(
        lease=LeaseRecord(
            lease_id=uuid4(),
            client_id=scenario.client_id,
            owner=FIRST_OWNER,
            generation=FIRST_GRANT,
            idempotency_key=key_for(FIRST_OWNER),
            acquired_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + INTERVAL.interval,
        )
    )

    with pytest.raises(LeaseNotHeld):
        register_run(cluster.store, unheld, scenario.run_id)

    assert cluster.ownership(scenario.run_id) == RunRecord(None, None, None, None)


def test_a_finalisation_records_the_outcome_once(cluster: Cluster) -> None:
    """The run is marked finalised and the outcome is stored with the generation."""
    scenario = cluster.scenario()
    grant = grant_for(cluster, scenario, FIRST_OWNER)
    register_run(cluster.store, grant, scenario.run_id)

    record = finalise(cluster.store, grant, scenario.run_id, RESULT)

    assert record.run_id == scenario.run_id
    assert record.result == RESULT
    assert record.generation == grant.generation
    assert cluster.ownership(scenario.run_id).finalised_at == record.finalised_at


def test_a_repeated_finalisation_returns_the_record_and_mutates_nothing(cluster: Cluster) -> None:
    """A duplicated finalisation reports the original outcome rather than a second one."""
    scenario = cluster.scenario()
    grant = grant_for(cluster, scenario, FIRST_OWNER)
    register_run(cluster.store, grant, scenario.run_id)
    first = finalise(cluster.store, grant, scenario.run_id, RESULT)

    again = finalise(cluster.store, grant, scenario.run_id, SECOND_RESULT)

    assert again == first
    assert again.result == RESULT, "the second outcome offered was not recorded"
    assert cluster.ownership(scenario.run_id).finalised_at == first.finalised_at


def test_a_recorded_finalisation_is_found_by_its_key(cluster: Cluster) -> None:
    """A resuming attempt asks by key and finds the outcome it must not repeat."""
    scenario = cluster.scenario()
    grant = grant_for(cluster, scenario, FIRST_OWNER)
    register_run(cluster.store, grant, scenario.run_id)
    recorded = finalise(cluster.store, grant, scenario.run_id, RESULT)

    found = finalisation_for(cluster.store, grant.idempotency_key)

    assert found == recorded
    assert finalisation_for(cluster.store, key_for(SECOND_OWNER)) is None


def test_a_finalisation_by_a_superseded_owner_records_nothing(cluster: Cluster) -> None:
    """A worker that lost the lease may not declare the run finished."""
    scenario = cluster.scenario()
    lapsed = elapsed_grant(cluster, scenario, FIRST_OWNER)
    register_run(cluster.store, lapsed, scenario.run_id)
    grant_for(cluster, scenario, SECOND_OWNER)

    with pytest.raises(StaleFencingGeneration):
        finalise(cluster.store, lapsed, scenario.run_id, RESULT)

    assert cluster.ownership(scenario.run_id).finalised_at is None


# ---------------------------------------------------------------------------
# The window a test drives rather than waits for
# ---------------------------------------------------------------------------


def test_the_window_is_reached_by_advancing_a_clock_rather_than_by_waiting(
    cluster: Cluster,
    time_source: ManualClock,
) -> None:
    """An expiry costs one call, and the cluster is still what judges it.

    The injected clock is synchronised to a reading behind the cluster's, the lease
    is granted from that reading, and the clock is then advanced past the interval.
    The cluster, being at least that far on already, reads the stored window as past
    and admits the transfer. No interval of real time is spent.
    """
    scenario = cluster.scenario()
    time_source.set_now(cluster.reading() - ELAPSED)
    granted_at = time_source.now()
    lapsed = grant_for(cluster, scenario, FIRST_OWNER, now=granted_at)

    time_source.advance(INTERVAL.seconds + 1)

    assert time_source.now() > lapsed.expires_at, "the holder's own window has run out"
    assert time_source.now() <= cluster.reading(), "and the cluster is at least that far on"
    state = current(cluster.store, scenario.client_id)
    assert state is not None
    assert state.takeable
    assert grant_for(cluster, scenario, SECOND_OWNER).generation == AFTER_TAKEOVER
