"""Property 31: fencing safety under contention.

**Validates: Requirements 44.2, 44.3, 44.4, 44.6, 44.7, 44.8, 44.12**

For any interleaving of lease acquisition, renewal, expiry, takeover, abrupt
termination, revival, and Disposition writes across 2 to 20 workers contending for
1 to 3 Clients, at most one Fencing_Generation is current per Client at every point,
every write presenting a generation other than the current one is refused as a stale
fencing generation and persists no row, every takeover generation strictly exceeds
every generation the Client has held, no takeover is granted while the cluster reads
the predecessor's window as still open, and a write from a run holding no lease at
all mutates nothing.

Five decisions shape the module.

**The cluster is not optional here and every clause says why.** The single current
lease is a partial unique index; the generation a grant takes is the historical
maximum plus one, read and written in one serialisable transaction; expiry is the
cluster's own `expires_at < now()` verdict; and the fence's generation read sits
inside the guarded write's transaction. A replay of the same schedule against a
dictionary would be evidence about a reimplementation rather than about the stored
ownership a governance claim is read from, so the module is marked to gate on a
reachable instance like every other suite that needs one.

**No interval of real time is spent, and nothing sleeps.** A window is placed by the
anchor the acquiring worker writes it from, and that anchor is the injected clock's
reading expressed in the cluster's frame: `reading - horizon + elapsed`, where the
horizon is one interval and one second and the elapsed span comes from the clock
alone. So while the workers' shared clock still lags the cluster by more than the
interval, a window written from it is one the cluster reads as already past, and
ownership moves; once the let-expire operations have advanced the clock past the
horizon, the windows written from it reach into the cluster's present and hold, and
a contender is refused. Both regimes are reached inside one example, and the only
thing that moves the clock is `advance`.

**The anchor is re-expressed against a fresh cluster reading at every write rather
than derived once from a stored offset.** An example spends real time on its round
trips, and a fixed offset would let that drift decide whether a window is open,
which would make a boundary example fail for being slow rather than for being
wrong. Re-reading the frame at each write keeps every anchor either a whole interval
and a second behind the reading or a whole number of steps ahead of it, and never
inside the band where drift could matter.

**Every expectation is derived from the cluster's own verdict, read immediately
before the operation, and the two race-free directions are asserted rather than a
guess about which one should hold.** A window the cluster has read as past stays
past, so a refusal is admissible only against a window it read as open, and that is
asserted on the refusal path. A takeover is asserted the other way round and without
any reliance on the earlier reading: the superseded lease's stored expiry is read
back and compared against the cluster's reading, which is monotone in the right
direction, so requirement 44.6 is checked exactly rather than within a tolerance.

**A terminated worker performs nothing and forgets nothing.** It neither renews nor
releases, which is the failure a graceful path would hide, and on revival it still
believes the generation it last held. Its Disposition write is therefore the case
the fence exists for, and the assertion is the whole of requirement 44.8: the
refusal names both generations, the row count for the run is unchanged, and the
refusal metric was emitted exactly once.

The example budget is the hundred the plan states, with no per-example deadline, and
it needed nothing given up to reach it. Per-example cost here is already flat in the
example's position: an example places up to three tenant, request, and run rows of
its own and performs up to fourteen operations, each a lease transaction or a fenced
write plus the ownership reads its assertions are made from, so an example is a few
hundred round trips at most. No erasure run is driven, no migration is applied past
the module's first, and every read is keyed by a client identifier this example
placed, so nothing an earlier example left standing is work a later one pays for and
the hundredth example costs what the first did. A deadline would fail a fourteen-step
example for being large rather than for being wrong, which is why there is none.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from types import ModuleType
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from molt.config.resolve import Configuration
from molt.erase.disposition import Candidate, DispositionKind, RunOwnership, decide, retain
from molt.erase.lease import LeaseGrant, acquire, current, renew
from molt.errors import LeaseNotHeldError, LeaseRefusedError, StaleFencingGenerationError
from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.erasure_lease import LeaseInterval
from molt.store.fencing import FIRST_GENERATION, STALE_GENERATION_METRIC
from molt.store.migrate import apply_migrations
from molt.telemetry import configure, reset
from molt.telemetry import current as current_telemetry

pytestmark = pytest.mark.integration

# How many examples the property runs and how long a drawn schedule is. The
# reasoning behind the budget is in the module docstring.
MAX_EXAMPLES: Final[int] = 100
MIN_STEPS: Final[int] = 4
MAX_STEPS: Final[int] = 14

# The contention the property states: two to twenty workers, one to three tenants.
MIN_WORKERS: Final[int] = 2
MAX_WORKERS: Final[int] = 20
MIN_CLIENTS: Final[int] = 1
MAX_CLIENTS: Final[int] = 3

# The intervals a schedule draws from, in seconds. Each is far below the configured
# lease length, so a window closes inside an example rather than outliving it, and
# each is at least two seconds so the margin either side of the horizon is wider
# than the real time an example's round trips can consume.
INTERVAL_SECONDS: Final[tuple[int, ...]] = (2, 3, 5)

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows every schedule is driven against, placed by this module because the
# modules under test own no tenant insert and no run insert.
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

# The reads every assertion is made from. Each names the lease history, the stored
# window, or the Disposition rows directly, so what is asserted is what the cluster
# holds rather than what the modules under test reported.
CLUSTER_READING: Final[str] = "SELECT now()"
COUNT_CURRENT_LEASES: Final[str] = (
    "SELECT count(*), count(DISTINCT generation) FROM erasure_lease "
    "WHERE client_id = %s AND superseded_at IS NULL"
)
COUNT_LEASES: Final[str] = "SELECT count(*) FROM erasure_lease WHERE client_id = %s"
SELECT_GENERATIONS: Final[str] = (
    "SELECT generation FROM erasure_lease WHERE client_id = %s "
    "ORDER BY acquired_at ASC, generation ASC"
)
SELECT_EXPIRY: Final[str] = "SELECT expires_at FROM erasure_lease WHERE id = %s"
COUNT_DISPOSITIONS: Final[str] = "SELECT count(*) FROM disposition WHERE run_id = %s"

# The values the placed rows carry. None of them is what an assertion turns on.
JURISDICTION: Final[str] = "eu"
REQUESTER: Final[str] = "operator"
JUSTIFICATION: Final[str] = "a governed request"

# What a drawn Disposition write records. The candidate holds no current claim for
# the erased tenant, so the decision table retains it: the write is evidence about
# the run and nothing else, which is what makes it usable as the fenced write of an
# arbitrary schedule without a memory-content mutation of its own.
SELECTION_REASON: Final[str] = "client_binding"

# How much of the interval the workers' shared clock starts behind the cluster's
# reading, as a multiple of the interval plus one second. One horizon, so a single
# let-expire operation carries the clock across it.
HORIZON_MARGIN: Final[int] = 1

# A connection and a cursor are typed loosely because the driver is reached through
# a fixture rather than imported, which keeps this module collectable with no driver
# installed.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


class Operation(StrEnum):
    """The seven acts a drawn schedule interleaves."""

    ACQUIRE = "acquire"
    RENEW = "renew"
    LET_EXPIRE = "let_expire"
    TAKE_OVER = "take_over"
    TERMINATE = "terminate_abruptly"
    REVIVE = "revive"
    WRITE = "attempt_disposition_write"

    @classmethod
    def drawn_from(cls) -> tuple[Operation, ...]:
        """The pool a step's act is sampled from, holding all seven and biased.

        A uniform sample over the seven spends most of a schedule terminating and
        reviving workers, and the two outcomes the property is most at risk from
        need a contested acquisition and a write from a superseded owner. The
        acquisition, the write, and the expiry are therefore repeated in the pool,
        which makes both reachable in a schedule of moderate length rather than
        vanishingly rare. Every act still appears.
        """
        return (
            cls.ACQUIRE,
            cls.ACQUIRE,
            cls.ACQUIRE,
            cls.TAKE_OVER,
            cls.TAKE_OVER,
            cls.WRITE,
            cls.WRITE,
            cls.WRITE,
            cls.LET_EXPIRE,
            cls.LET_EXPIRE,
            cls.RENEW,
            cls.RENEW,
            cls.TERMINATE,
            cls.REVIVE,
        )


@dataclass(frozen=True, slots=True)
class Step:
    """One act of a schedule, and the worker and tenant it is performed by and for."""

    operation: Operation
    worker: int
    client: int


@dataclass(frozen=True, slots=True)
class Schedule:
    """One drawn interleaving: the contenders, the tenants, and the lease length."""

    owners: tuple[str, ...]
    client_count: int
    interval: LeaseInterval
    steps: tuple[Step, ...]

    @property
    def horizon(self) -> timedelta:
        """How far the workers' shared clock starts behind the cluster's reading.

        One interval and one second, so a window written before the first
        let-expire operation is one the cluster reads as already past, and the
        single advance of that operation carries the clock to the reading itself.
        """
        return self.interval.interval + timedelta(seconds=HORIZON_MARGIN)


@st.composite
def lease_schedules(draw: st.DrawFn) -> Schedule:
    """Draw an interleaving of the seven acts across the contenders and the tenants.

    The worker and tenant counts are drawn first because the indices of every step
    are bounded by them, which is what keeps a drawn schedule total: every step
    names a contender that exists and a tenant that exists.
    """
    worker_count = draw(st.integers(min_value=MIN_WORKERS, max_value=MAX_WORKERS))
    client_count = draw(st.integers(min_value=MIN_CLIENTS, max_value=MAX_CLIENTS))
    interval = LeaseInterval(seconds=draw(st.sampled_from(INTERVAL_SECONDS)))
    steps = draw(
        st.lists(
            st.builds(
                Step,
                operation=st.sampled_from(Operation.drawn_from()),
                worker=st.integers(min_value=0, max_value=worker_count - 1),
                client=st.integers(min_value=0, max_value=client_count - 1),
            ),
            min_size=MIN_STEPS,
            max_size=MAX_STEPS,
        )
    )
    return Schedule(
        owners=tuple(f"worker-{index}" for index in range(worker_count)),
        client_count=client_count,
        interval=interval,
        steps=tuple(steps),
    )


# ---------------------------------------------------------------------------
# The clock, the tenants, and the contenders
# ---------------------------------------------------------------------------


class ManualClock(Protocol):
    """The two calls this module makes on the injected clock.

    Declared structurally rather than imported, because the clock is delivered by a
    fixture and a test module reaches a fixture by name rather than by import.
    """

    def now(self) -> datetime:
        """The current wall reading, timezone aware."""

    def advance(self, seconds: float) -> None:
        """Move the reading forward by a non-negative number of seconds."""


@dataclass(frozen=True, slots=True)
class Tenant:
    """One tenant of an example, with the run its Disposition writes belong to."""

    client_id: UUID
    slug: str
    run_id: UUID


@dataclass(slots=True)
class Contender:
    """One worker's own state, which is what it believes rather than what is true.

    The believed grants survive an abrupt termination, because a worker that was
    killed released nothing and forgot nothing: on revival it presents the
    generation it last held, which is exactly the write the fence exists to refuse.
    """

    owner: str
    alive: bool = True
    believed: dict[int, LeaseGrant] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The cluster the schedule is driven against
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, and a store over it."""

    store: MemoryStore
    connection: DriverConnection

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

    def column(self, statement: str, params: tuple[object, ...]) -> tuple[int, ...]:
        """One whole-number column of a read of this module's own, in its stated order."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            rows = cursor.fetchall()
        return tuple(int(str(row[0])) for row in rows)

    def reading(self) -> datetime:
        """The cluster's current reading, which every anchor is expressed against."""
        moment = self.row(CLUSTER_READING, ())[0]
        assert isinstance(moment, datetime)
        return moment

    def tenant(self) -> Tenant:
        """Place a tenant, an erasure request, and a run, and report them.

        Each tenant belongs to one example, so the uniqueness admitting one current
        lease per tenant never brings two examples into contact.
        """
        client_id = uuid4()
        request_id = uuid4()
        run_id = uuid4()
        slug = f"tenant-{client_id.hex[:12]}"
        self.send(INSERT_CLIENT, (client_id, slug, "Tenant", JURISDICTION))
        self.send(INSERT_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION))
        self.send(INSERT_RUN, (run_id, request_id, client_id, REQUESTER))
        return Tenant(client_id=client_id, slug=slug, run_id=run_id)

    def current_leases(self, client_id: UUID) -> tuple[int, int]:
        """How many leases are current for a tenant, and how many generations they hold."""
        count, generations = self.row(COUNT_CURRENT_LEASES, (client_id,))
        return int(str(count)), int(str(generations))

    def lease_total(self, client_id: UUID) -> int:
        """How many leases the tenant has ever held, closed ones included."""
        return int(str(self.row(COUNT_LEASES, (client_id,))[0]))

    def generations(self, client_id: UUID) -> tuple[int, ...]:
        """Every generation the tenant has recorded, in the order it acquired them."""
        return self.column(SELECT_GENERATIONS, (client_id,))

    def expiry_of(self, lease_id: UUID) -> datetime:
        """The stored instant one lease's window closes at."""
        expires_at = self.row(SELECT_EXPIRY, (lease_id,))[0]
        assert isinstance(expires_at, datetime)
        return expires_at

    def dispositions(self, run_id: UUID) -> int:
        """How many Disposition rows one run has persisted."""
        return int(str(self.row(COUNT_DISPOSITIONS, (run_id,))[0]))


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store bound to that schema.

    Every migration is applied because the partial uniqueness admitting one current
    lease, the update guard confining which columns of a lease may move, the run
    columns an ownership record writes, and the Disposition table a fenced write
    lands in all arrive after the tables that hold them.

    Module scope keeps the schema cost paid once: examples are isolated from each
    other by tenants of their own rather than by a schema of their own.
    """
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


@pytest.fixture(scope="module")
def telemetry_sink() -> Iterator[io.StringIO]:
    """Install a process-wide telemetry instance the refusal metric is counted on.

    Module scope rather than per test, because the counters are read as differences
    either side of one write and a fixture rebuilt per example would be a
    function-scoped fixture under a property.
    """
    sink = io.StringIO()
    configure(Configuration(environ={"MOLT_LOG_LEVEL": "warning"}, file_values={}), stream=sink)
    try:
        yield sink
    finally:
        reset()


def stale_refusals_counted() -> float:
    """How many fenced writes the process-wide instance has counted as superseded."""
    return current_telemetry().counters().get((STALE_GENERATION_METRIC, ()), 0.0)


def retained_candidate() -> Candidate:
    """One candidate the decision table retains, for the fenced write of a schedule.

    The erased tenant holds no current claim on it, so the decision is a retention:
    the write records evidence about the run under the presented generation and
    mutates no memory content, which is what makes it the write to attempt from an
    arbitrary point of a drawn schedule.
    """
    return Candidate(
        artifact_id=uuid4(),
        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
        selection_reason=SELECTION_REASON,
        content_digest=None,
        other_client_count=0,
        erased_client_count=0,
        binding_slugs=(),
    )


def strictly_increasing(values: Sequence[int]) -> bool:
    """Whether every value of a sequence exceeds the one before it."""
    return all(later > earlier for earlier, later in pairwise(values))


def count_band(count: int) -> str:
    """How often an outcome occurred, for the coverage record."""
    if count == 0:
        return "none"
    if count == 1:
        return "1"
    if count <= 4:
        return "2-4"
    return "5+"


# ---------------------------------------------------------------------------
# The drive
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Drive:
    """One example's execution: the state outside the cluster, and the assertions.

    The assertions live beside the operations they are about rather than at the end,
    because the property is stated over every point of the interleaving: a claim
    checked only after the last step would be satisfied by a history that violated
    it in the middle and then recovered.
    """

    cluster: Cluster
    clock: ManualClock
    schedule: Schedule
    tenants: tuple[Tenant, ...]
    contenders: tuple[Contender, ...]
    epoch: datetime
    highest: list[int]
    persisted: list[int]
    takeovers: int = 0
    refusals: int = 0
    stale_writes: int = 0
    unheld_writes: int = 0
    admitted_writes: int = 0
    expiries: int = 0
    renewals: int = 0
    stale_renewals: int = 0

    def anchor(self) -> datetime:
        """The reading a worker writes a window from, in the cluster's own frame.

        The elapsed span comes from the injected clock and from nothing else, and
        the frame is re-read here rather than stored, so the real time an example
        spends on its round trips never decides whether a window is open.
        """
        return self.cluster.reading() - self.schedule.horizon + (self.clock.now() - self.epoch)

    def drive(self) -> None:
        """Perform every step of the schedule, asserting as it goes."""
        for step in self.schedule.steps:
            self.perform(step)
            self.assert_one_current_generation()

    def perform(self, step: Step) -> None:
        """Perform one step, or nothing at all where its worker is terminated."""
        contender = self.contenders[step.worker]
        tenant = self.tenants[step.client]
        if step.operation is Operation.LET_EXPIRE:
            self.let_expire()
            return
        if step.operation is Operation.TERMINATE:
            contender.alive = False
            return
        if step.operation is Operation.REVIVE:
            contender.alive = True
            return
        if not contender.alive:
            # A terminated worker performs nothing at all until it is revived: it
            # neither renews nor releases, which is the failure a graceful ending
            # would hide, and it keeps believing the generation it last held.
            return
        if step.operation in (Operation.ACQUIRE, Operation.TAKE_OVER):
            self.attempt_acquisition(contender, step.client, tenant)
        elif step.operation is Operation.RENEW:
            self.attempt_renewal(contender, step.client, tenant)
        else:
            self.attempt_write(contender, step.client, tenant)

    def let_expire(self) -> None:
        """Advance the injected clock past one whole interval, waiting for nothing.

        Nothing renews, and nothing is released. What the advance does is move the
        anchor every later window is written from, so the workers' shared clock
        crosses the horizon it started behind and the cluster's verdict on the
        windows written either side of it differs.
        """
        self.clock.advance(self.schedule.interval.seconds + 1)
        self.expiries += 1

    def attempt_acquisition(self, contender: Contender, index: int, tenant: Tenant) -> None:
        """Ask for ownership of one tenant, and assert whichever answer came back."""
        before = current(self.cluster.store, tenant.client_id)
        leases_before = self.cluster.lease_total(tenant.client_id)
        granted: LeaseGrant | None = None
        refused: LeaseRefusedError | None = None
        try:
            granted = acquire(
                self.cluster.store,
                tenant.client_id,
                contender.owner,
                f"{contender.owner}-{uuid4().hex[:12]}",
                interval=self.schedule.interval,
                now=self.anchor(),
            )
        except LeaseRefusedError as error:
            refused = error
        if refused is not None:
            # Requirements 44.2 and 44.3. A window the cluster read as past stays
            # past, so a refusal is admissible against an open window alone, and it
            # names the owner and the generation that hold the tenant now.
            assert before is not None, "an acquisition was refused for a Client holding no lease"
            assert not before.takeable, (
                "an acquisition was refused although the cluster read the current window "
                "as already past, so ownership should have transferred"
            )
            assert refused.owner == before.owner
            assert refused.generation == before.generation
            assert self.cluster.lease_total(tenant.client_id) == leases_before, (
                "a refused acquisition left a lease behind"
            )
            self.refusals += 1
            return
        assert granted is not None

        # Requirement 44.4. The generation exceeds every generation this tenant has
        # ever recorded, closed leases included, so the sequence strictly increases
        # over the whole history rather than over the leases that are current.
        assert granted.generation > self.highest[index], (
            f"a grant recorded generation {granted.generation} where the tenant has already "
            f"held {self.highest[index]}"
        )
        if before is None:
            assert leases_before == 0
            assert not granted.took_over
            assert granted.generation == FIRST_GENERATION
        else:
            assert granted.took_over
            assert granted.superseded == before.lease_id
            # Requirement 44.6, checked against the stored window rather than
            # against the earlier reading: a closed lease's expiry never moves and
            # the cluster's reading only advances, so this comparison holds exactly
            # if the transfer was admitted for the reason it must be.
            assert self.cluster.expiry_of(before.lease_id) < self.cluster.reading(), (
                "a takeover was granted while the superseded lease's window was still open"
            )
            self.takeovers += 1
        self.highest[index] = granted.generation
        contender.believed[index] = granted

    def attempt_renewal(self, contender: Contender, index: int, tenant: Tenant) -> None:
        """Extend a window this worker believes it holds, or learn it was superseded."""
        held = contender.believed.get(index)
        if held is None:
            return
        before = current(self.cluster.store, tenant.client_id)
        assert before is not None, "a worker believes a grant for a Client holding no lease"
        renewed: LeaseGrant | None = None
        stale: StaleFencingGenerationError | None = None
        try:
            renewed = renew(
                self.cluster.store,
                held,
                interval=self.schedule.interval,
                now=self.anchor(),
            )
        except StaleFencingGenerationError as error:
            stale = error
        if stale is not None:
            # A renewal is a claim about ownership, so it goes through the fence and
            # a superseded owner extends nothing.
            assert before.generation != held.generation
            assert stale.presented == held.generation
            assert stale.current == before.generation
            self.stale_renewals += 1
            return
        assert renewed is not None
        assert before.generation == held.generation
        assert renewed.lease_id == held.lease_id
        assert renewed.generation == held.generation
        contender.believed[index] = renewed
        self.renewals += 1

    def attempt_write(self, contender: Contender, index: int, tenant: Tenant) -> None:
        """Write one Disposition under the generation this worker believes it holds.

        A worker believing nothing presents the first generation, which is the run
        begun with no lease of requirement 44.12: where the tenant holds no current
        lease the write belongs to no owner and is refused before any mutation.
        """
        held = contender.believed.get(index)
        presented = FIRST_GENERATION if held is None else held.generation
        before = current(self.cluster.store, tenant.client_id)
        rows = self.cluster.dispositions(tenant.run_id)
        refused = stale_refusals_counted()
        ownership = RunOwnership(
            run_id=tenant.run_id,
            client_id=tenant.client_id,
            slug=tenant.slug,
            generation=presented,
        )
        decision = decide(retained_candidate())
        assert decision.disposition is DispositionKind.RETAINED
        unheld: LeaseNotHeldError | None = None
        stale: StaleFencingGenerationError | None = None
        try:
            retain(self.cluster.store, ownership, decision)
        except LeaseNotHeldError as error:
            unheld = error
        except StaleFencingGenerationError as error:
            stale = error
        if unheld is not None:
            # Requirement 44.12: a run begun for a Client nobody owns mutates nothing.
            assert before is None
            assert self.cluster.dispositions(tenant.run_id) == rows, (
                "a write from a run holding no lease persisted a row"
            )
            self.unheld_writes += 1
            return
        if stale is not None:
            # Requirements 44.7 and 44.8: the refusal names both generations, no row
            # is persisted, and the refusal is counted once.
            assert before is not None
            assert before.generation != presented
            assert stale.presented == presented
            assert stale.current == before.generation
            assert self.cluster.dispositions(tenant.run_id) == rows, (
                "a write refused as a stale fencing generation persisted a row"
            )
            assert stale_refusals_counted() == refused + 1.0
            self.stale_writes += 1
            return
        assert before is not None
        assert before.generation == presented, (
            "a write was admitted although the generation it presented is not current"
        )
        assert self.cluster.dispositions(tenant.run_id) == rows + 1
        self.persisted[index] += 1
        self.admitted_writes += 1

    def assert_one_current_generation(self) -> None:
        """Requirement 44.2 at this point of the interleaving, for every tenant."""
        for tenant in self.tenants:
            leases, generations = self.cluster.current_leases(tenant.client_id)
            assert leases <= 1, f"{leases} leases are current for one Client at once"
            assert generations <= 1, (
                f"{generations} fencing generations are current for one Client at once"
            )

    def assert_history(self) -> None:
        """The whole recorded history of every tenant, once the schedule has run."""
        for index, tenant in enumerate(self.tenants):
            recorded = self.cluster.generations(tenant.client_id)
            assert strictly_increasing(recorded), (
                f"the generations {recorded} a Client recorded do not strictly increase"
            )
            assert len(set(recorded)) == len(recorded)
            assert self.highest[index] == (max(recorded) if recorded else 0)
            leases, _generations = self.cluster.current_leases(tenant.client_id)
            assert leases == (1 if recorded else 0)
            assert self.cluster.dispositions(tenant.run_id) == self.persisted[index]


def drive_for(cluster: Cluster, clock: ManualClock, schedule: Schedule) -> Drive:
    """Place one example's tenants and build the drive over them."""
    tenants = tuple(cluster.tenant() for _ in range(schedule.client_count))
    return Drive(
        cluster=cluster,
        clock=clock,
        schedule=schedule,
        tenants=tenants,
        contenders=tuple(Contender(owner=owner) for owner in schedule.owners),
        epoch=clock.now(),
        highest=[0] * schedule.client_count,
        persisted=[0] * schedule.client_count,
    )


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 31: For any interleaving of lease acquisition, renewal,
# expiry, takeover, worker termination, worker revival, and disposition writes across 2
# to 20 workers contending for 1 to 3 Clients, at every point in the interleaving at
# most one Fencing_Generation is current per Client, every write carrying a
# Fencing_Generation other than the current one for that Client is refused with
# `stale_fencing_generation` and persists no row, every takeover generation strictly
# exceeds every generation previously recorded for that Client, no takeover is granted
# while the current lease's expiry timestamp is in the future, and a run begun with no
# held lease performs no mutation.
@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    # The clock is the injected time source, delivered per test rather than per
    # example. It only ever moves forward and every example measures against an
    # epoch it reads for itself, so sharing one instance across examples carries no
    # state between them.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(schedule=lease_schedules())
def test_one_generation_is_current_and_every_stale_write_is_refused(
    cluster: Cluster,
    telemetry_sink: io.StringIO,
    time_source: ManualClock,
    schedule: Schedule,
) -> None:
    event(f"workers={count_band(len(schedule.owners))}")
    event(f"clients={schedule.client_count}")
    event(f"interval={schedule.interval.seconds}")
    event(f"steps={count_band(len(schedule.steps))}")

    drive = drive_for(cluster, time_source, schedule)
    drive.drive()
    drive.assert_history()

    event(f"expiries={count_band(drive.expiries)}")
    event(f"takeovers={count_band(drive.takeovers)}")
    event(f"refusals={count_band(drive.refusals)}")
    event(f"renewals={count_band(drive.renewals)}")
    event(f"superseded renewals={count_band(drive.stale_renewals)}")
    event(f"stale writes={count_band(drive.stale_writes)}")
    event(f"unheld writes={count_band(drive.unheld_writes)}")
    event(f"admitted writes={count_band(drive.admitted_writes)}")

    # The refusal is recorded as well as counted, so an operator reading a log
    # record learns which tenant and which owner the superseded write belonged to.
    if drive.stale_writes:
        assert "superseded" in telemetry_sink.getvalue()
