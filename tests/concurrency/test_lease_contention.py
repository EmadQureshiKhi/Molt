"""Ten real worker processes contend for one lease, and the loser is fenced.

**Validates: Requirements 44.13, 44.15, 36.4**

The scenario is the design's contention demonstration, driven in the order it states
it: ten processes ask for erasure ownership of one Client at once, exactly one is
granted and every other is refused by name, the winner is killed outright, a second
worker is refused while the window it left behind is still open and granted once the
cluster reads that window as past, and the killed worker is revived to write a
Disposition under the generation it still believes it holds.

Five decisions shape the module.

**Real processes rather than threads, because the claim is about workers.** A thread
pool would share this interpreter, this store, and this pool of connections, so a
"contender" would be a call rather than a worker and the uniqueness that admits one
current lease per Client would be contended from a single session. Each contender
here is a separate interpreter with a connection of its own, spawned with an explicit
argument vector, which is the same shape the latency benchmark spawns its subject
with. This module is both the harness and the worker: the same file runs as a script
under a mode argument, so the code that contends is the code that is read here.

**The race is arranged rather than hoped for.** Ten spawns take longer than a lease
window, so a bare loop of spawns would measure interpreter start-up and would let the
first grant expire before the last contender asked, which is a takeover rather than a
refusal. Each contender is therefore handed one instant to begin at, in the epoch
frame every process shares, and it holds until then. They ask within milliseconds of
each other, and the outcome is one grant and nine refusals rather than a sequence of
transfers.

**The window is real time, and that is not avoidable here.** Takeability is settled
by comparing the stored `expires_at` against the cluster's own reading inside the
superseding transaction, so neither an injected clock nor an anchor a worker chooses
can retro-close a window already stored: a lease whose window the cluster reads as open
becomes takeable only once the cluster's reading passes it. The lease interval is
therefore short, and the only real waiting is the remainder of that one window, most
of which the race and the refused attempt have already consumed. Both readings the
claim turns on are taken from the cluster either side of each attempt, so what is
asserted is the cluster's verdict rather than this process's clock.

**The winner is killed, not asked to stop.** It renews nothing and releases nothing,
which is the failure a graceful ending would hide, and the stored window is read
before and after the signal to show the lease was left where it stood. On revival it
still believes its generation, so its Disposition write is the write the fence exists
to refuse.

**The refusal is observed where it happened.** The fence emits its measurement in the
process whose write was refused, so the revived worker reports the counter reading
either side of its own attempt, and this process asserts what that worker counted
alongside the row count it reads from the cluster itself.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.erase.disposition import Candidate, DispositionKind, RunOwnership, decide, retain
from molt.erase.lease import acquire
from molt.errors import LeaseRefusedError, StaleFencingGenerationError
from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.erasure_lease import LeaseInterval
from molt.store.fencing import STALE_GENERATION_METRIC
from molt.store.migrate import apply_migrations
from molt.telemetry import configure
from molt.telemetry import current as current_telemetry

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

# The contention the requirement states: at least ten workers, exactly one grant, and
# a refusal from every other one.
CONTENDERS: Final[int] = 10
EXPECTED_GRANTS: Final[int] = 1
MINIMUM_REFUSALS: Final[int] = CONTENDERS - EXPECTED_GRANTS

# How long the contested window runs for, in seconds. Short, because the only real
# waiting this module does is the remainder of it, and long enough that the race and
# the refused attempt both land inside it rather than after it.
LEASE_INTERVAL_SECONDS: Final[int] = 5

# How long a contender is given to reach its starting instant, and the margin added
# to the remainder of the window before ownership is asked for again. The first is
# generous because a contender pays interpreter start-up and the package import
# before it can begin; the second is small because the comparison it protects is the
# cluster's own.
START_DELAY_SECONDS: Final[float] = 3.0
EXPIRY_MARGIN_SECONDS: Final[float] = 0.5

# How long a granted contender stays alive doing nothing at all, and how long this
# process waits for a worker to report or to die. The hold is bounded so a worker
# this process fails to kill leaves nothing running behind the suite.
HOLD_SECONDS: Final[float] = 90.0
WORKER_TIMEOUT_SECONDS: Final[float] = 60.0

# The modes this file runs under as a script, and the outcomes a worker reports.
CONTEND_MODE: Final[str] = "contend"
REVIVE_MODE: Final[str] = "revive"
GRANTED: Final[str] = "granted"
REFUSED: Final[str] = "refused"
ADMITTED: Final[str] = "admitted"
FAILED: Final[str] = "failed"

# Where a worker reads the connection string from, and where it imports the package
# from, neither of which a spawned interpreter inherits by itself.
DSN_KEY: Final[str] = "MOLT_TEST_DSN"
MODULE_PATH: Final[Path] = Path(__file__).resolve()
SOURCE_ROOT: Final[Path] = MODULE_PATH.parents[2] / "src"

# The identities of the three workers that are not contenders in the race: the one
# refused inside the window, the one granted once it has passed, and the revived one.
EARLY_OWNER: Final[str] = "worker-inside-the-window"
SUCCESSOR_OWNER: Final[str] = "worker-after-the-window"

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows the scenario is driven against, placed by this module because the modules
# under test own no tenant insert and no run insert.
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

# The reads every assertion is made from. Each names the stored lease, the stored
# window, or the Disposition rows directly, so what is asserted is what the cluster
# holds rather than what a worker reported about it.
CLUSTER_READING: Final[str] = "SELECT now()"
SELECT_CURRENT_LEASE: Final[str] = (
    "SELECT id, owner, generation, expires_at FROM erasure_lease "
    "WHERE client_id = %s AND superseded_at IS NULL"
)
SELECT_EXPIRY: Final[str] = "SELECT expires_at FROM erasure_lease WHERE id = %s"
SELECT_SUPERSESSION: Final[str] = "SELECT superseded_by FROM erasure_lease WHERE id = %s"
COUNT_DISPOSITIONS: Final[str] = "SELECT count(*) FROM disposition WHERE run_id = %s"

# The values the placed rows carry. None of them is what an assertion turns on.
JURISDICTION: Final[str] = "eu"
REQUESTER: Final[str] = "operator"
JUSTIFICATION: Final[str] = "a governed request"

# What the revived worker's Disposition write records. The candidate holds no current
# claim for the erased tenant, so the decision table retains it: the write is
# evidence about the run and mutates no memory content, which is what makes it usable
# as the fenced write of a worker that has lost ownership.
SELECTION_REASON: Final[str] = "client_binding"

# A connection is typed loosely because the driver is imported lazily, which keeps
# this module collectable with no driver installed.
DriverConnection = Any


# ---------------------------------------------------------------------------
# The worker: this file, run as a script
# ---------------------------------------------------------------------------


def _silenced_telemetry() -> io.StringIO:
    """Install a telemetry instance whose records go to a buffer of this process.

    Every worker reports its outcome as one line of its standard output, so a log
    record on that stream would be read as part of the report. The buffer is also
    what lets the revived worker say whether the refusal was recorded as well as
    counted.
    """
    sink = io.StringIO()
    configure(Configuration(environ={"MOLT_LOG_LEVEL": "warning"}, file_values={}), stream=sink)
    return sink


def _stale_refusals_counted() -> float:
    """How many fenced writes this process has counted as superseded."""
    return current_telemetry().counters().get((STALE_GENERATION_METRIC, ()), 0.0)


def _worker_store(schema: str) -> MemoryStore:
    """A store of this worker's own, bound to the schema the harness created.

    The driver is imported here rather than at module scope so the harness stays
    collectable on a checkout with no driver, and the connection string is read from
    the environment rather than from the argument vector so it appears in no process
    listing.
    """
    driver: ModuleType = importlib.import_module("psycopg")
    dsn = os.environ[DSN_KEY]

    def connect_with() -> Connection:
        opened = driver.connect(dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    return MemoryStore(connect_with=connect_with)


def _announce(report: dict[str, object]) -> None:
    """Write one worker's outcome as a single line the harness reads back."""
    sys.stdout.write(json.dumps(report) + "\n")
    sys.stdout.flush()


def _hold_until(instant: float) -> None:
    """Hold until a shared starting instant, so every contender asks at once."""
    remaining = instant - time.time()
    if remaining > 0.0:
        time.sleep(remaining)


def _retained_candidate() -> Candidate:
    """One candidate the decision table retains, for the revived worker's write."""
    return Candidate(
        artifact_id=uuid4(),
        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
        selection_reason=SELECTION_REASON,
        content_digest=None,
        other_client_count=0,
        erased_client_count=0,
        binding_slugs=(),
    )


def _contend(arguments: Sequence[str]) -> int:
    """Ask for ownership at the shared instant, report the answer, then hold.

    A granted worker does nothing at all afterwards: it neither renews nor releases,
    so the harness's signal leaves the window exactly where the grant wrote it.
    """
    schema, client_text, owner, key, interval_text, start_text, hold_text = arguments
    _silenced_telemetry()
    _hold_until(float(start_text))
    report: dict[str, object]
    with _worker_store(schema) as store:
        try:
            grant = acquire(
                store,
                UUID(client_text),
                owner,
                key,
                interval=LeaseInterval(seconds=int(interval_text)),
            )
        except LeaseRefusedError as refusal:
            report = {
                "owner": owner,
                "outcome": REFUSED,
                "current_owner": refusal.owner,
                "current_generation": refusal.generation,
            }
        except Exception as failure:
            report = {"owner": owner, "outcome": FAILED, "failure": _described(failure)}
        else:
            report = {
                "owner": owner,
                "outcome": GRANTED,
                "generation": grant.generation,
                "lease_id": str(grant.lease_id),
                "took_over": grant.took_over,
            }
        _announce(report)
        if report["outcome"] == GRANTED:
            time.sleep(float(hold_text))
    return 0


def _revive(arguments: Sequence[str]) -> int:
    """Write one Disposition under the generation this worker still believes it holds."""
    schema, client_text, run_text, slug, generation_text = arguments
    sink = _silenced_telemetry()
    presented = int(generation_text)
    ownership = RunOwnership(
        run_id=UUID(run_text),
        client_id=UUID(client_text),
        slug=slug,
        generation=presented,
    )
    decision = decide(_retained_candidate())
    assert decision.disposition is DispositionKind.RETAINED
    counted = _stale_refusals_counted()
    report: dict[str, object]
    with _worker_store(schema) as store:
        try:
            retain(store, ownership, decision)
        except StaleFencingGenerationError as refusal:
            report = {
                "outcome": REFUSED,
                "presented": refusal.presented,
                "current": refusal.current,
                "counted": _stale_refusals_counted() - counted,
                "recorded": "superseded" in sink.getvalue(),
            }
        except Exception as failure:
            report = {"outcome": FAILED, "failure": _described(failure)}
        else:
            report = {"outcome": ADMITTED, "presented": presented}
    _announce(report)
    return 0


def _described(failure: BaseException) -> str:
    """Name a failure by type and message, for a report the harness reads."""
    return f"{type(failure).__name__}: {failure}"


def worker_main(argv: Sequence[str]) -> int:
    """Run this file as one worker, in the mode its first argument names."""
    mode, arguments = argv[0], argv[1:]
    if mode == CONTEND_MODE:
        return _contend(arguments)
    if mode == REVIVE_MODE:
        return _revive(arguments)
    raise SystemExit(f"a worker runs in the {CONTEND_MODE} or the {REVIVE_MODE} mode")


# ---------------------------------------------------------------------------
# The harness: the cluster the scenario is driven against
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tenant:
    """The tenant of this module, with the run the fenced write belongs to."""

    client_id: UUID
    slug: str
    run_id: UUID


@dataclass(frozen=True, slots=True)
class StoredLease:
    """The lease the cluster holds as current, read column by column."""

    lease_id: UUID
    owner: str
    generation: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, and the reads the assertions are made from."""

    connection: DriverConnection
    schema: str
    dsn: str

    def send(self, statement: str, params: tuple[object, ...]) -> None:
        """Send one parameterised statement on this module's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)

    def row(self, statement: str, params: tuple[object, ...]) -> tuple[object, ...] | None:
        """The one row a read of this module's own returns, or None where none is."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            row = cursor.fetchone()
        return None if row is None else tuple(row)

    def reading(self) -> datetime:
        """The cluster's own current reading, which every window is compared against."""
        row = self.row(CLUSTER_READING, ())
        assert row is not None
        moment = row[0]
        assert isinstance(moment, datetime)
        return moment

    def tenant(self) -> Tenant:
        """Place a tenant, an erasure request, and the run the write belongs to."""
        client_id = uuid4()
        request_id = uuid4()
        run_id = uuid4()
        slug = f"tenant-{client_id.hex[:12]}"
        self.send(INSERT_CLIENT, (client_id, slug, "Tenant", JURISDICTION))
        self.send(INSERT_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION))
        self.send(INSERT_RUN, (run_id, request_id, client_id, REQUESTER))
        return Tenant(client_id=client_id, slug=slug, run_id=run_id)

    def current_lease(self, client_id: UUID) -> StoredLease | None:
        """The lease the cluster holds as current for one tenant, or None where none is."""
        row = self.row(SELECT_CURRENT_LEASE, (client_id,))
        if row is None:
            return None
        expires_at = row[3]
        assert isinstance(expires_at, datetime)
        return StoredLease(
            lease_id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
            owner=str(row[1]),
            generation=int(str(row[2])),
            expires_at=expires_at,
        )

    def expiry_of(self, lease_id: UUID) -> datetime:
        """The stored instant one lease's window closes at."""
        row = self.row(SELECT_EXPIRY, (lease_id,))
        assert row is not None
        expires_at = row[0]
        assert isinstance(expires_at, datetime)
        return expires_at

    def superseded_by(self, lease_id: UUID) -> UUID | None:
        """The lease that replaced one lease, or None while it is still current."""
        row = self.row(SELECT_SUPERSESSION, (lease_id,))
        assert row is not None
        return None if row[0] is None else UUID(str(row[0]))

    def dispositions(self, run_id: UUID) -> int:
        """How many Disposition rows one run has persisted."""
        row = self.row(COUNT_DISPOSITIONS, (run_id,))
        assert row is not None
        return int(str(row[0]))


@pytest.fixture(scope="module")
def cluster(fresh_schema: DriverConnection, local_instance_dsn: str) -> Cluster:
    """Apply every migration and report the schema the workers are pointed at.

    Every migration is applied because the partial uniqueness admitting one current
    lease, the guard confining which columns of a lease may move, and the Disposition
    table a fenced write lands in all arrive after the tables that hold them. The
    schema is created and dropped by the shared fixture, so nothing this module places
    outlives it.
    """
    apply_migrations(fresh_schema)
    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
    assert local_instance_dsn, "the workers are given the connection string the suite resolved"
    return Cluster(connection=fresh_schema, schema=str(row[0]), dsn=local_instance_dsn)


# ---------------------------------------------------------------------------
# Spawning the workers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Attempt:
    """What one worker reported about its own acquisition."""

    owner: str
    report: dict[str, object]

    @property
    def outcome(self) -> str:
        """Whether this worker was granted ownership, refused it, or failed."""
        return str(self.report["outcome"])

    @property
    def granted(self) -> bool:
        """Whether this worker holds the lease."""
        return self.outcome == GRANTED

    @property
    def generation(self) -> int:
        """The generation this worker was granted."""
        return int(str(self.report["generation"]))

    @property
    def lease_id(self) -> UUID:
        """The lease this worker was granted."""
        return UUID(str(self.report["lease_id"]))

    @property
    def refused_owner(self) -> str:
        """The owner this worker was told holds the lease."""
        return str(self.report["current_owner"])

    @property
    def refused_generation(self) -> int:
        """The generation this worker was told is current."""
        return int(str(self.report["current_generation"]))


def _spawn(arguments: Sequence[str], *, dsn: str) -> subprocess.Popen[str]:
    """Start one worker as a real process, with the argument vector written out here.

    The connection string is placed in the child's environment from the value the
    fixture resolved rather than inherited from this process's own. Under parallel
    execution each worker of the suite runs against a database of its own, so the
    ambient value names a database this module's schema does not live in, and a worker
    inheriting it would connect somewhere its tables do not exist.
    """
    environment = dict(os.environ)
    environment[DSN_KEY] = dsn
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.Popen(  # noqa: S603
        [sys.executable, str(MODULE_PATH), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )


def _reported(process: subprocess.Popen[str]) -> dict[str, object]:
    """Read one worker's single report line, or fail naming what it wrote instead."""
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line.strip():
        errors = "" if process.stderr is None else process.stderr.read()
        raise AssertionError(f"a worker reported nothing and wrote: {errors.strip()}")
    document = json.loads(line)
    assert isinstance(document, dict)
    report: dict[str, object] = document
    assert report["outcome"] != FAILED, f"a worker failed: {report.get('failure')}"
    return report


def _contend_arguments(
    cluster: Cluster,
    tenant: Tenant,
    *,
    owner: str,
    start: float,
    hold: float,
) -> tuple[str, ...]:
    """The argument vector one contender is spawned with."""
    return (
        CONTEND_MODE,
        cluster.schema,
        str(tenant.client_id),
        owner,
        f"{owner}-{uuid4().hex[:12]}",
        str(LEASE_INTERVAL_SECONDS),
        f"{start:.3f}",
        f"{hold:.3f}",
    )


def _one_attempt(cluster: Cluster, tenant: Tenant, *, owner: str) -> Attempt:
    """Spawn one worker that asks for ownership at once and then exits."""
    process = _spawn(
        _contend_arguments(cluster, tenant, owner=owner, start=0.0, hold=0.0),
        dsn=cluster.dsn,
    )
    try:
        return Attempt(owner=owner, report=_reported(process))
    finally:
        _finish(process)


def _finish(process: subprocess.Popen[str]) -> int | None:
    """Let one worker end, and release the pipes it was read through."""
    try:
        return process.wait(timeout=WORKER_TIMEOUT_SECONDS)
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _kill(process: subprocess.Popen[str]) -> int | None:
    """End one worker abruptly: no release, no final renewal, no chance to act."""
    process.kill()
    return _finish(process)


# ---------------------------------------------------------------------------
# The scenario, driven once
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Contention:
    """One settled run of the scenario, in the order the design states it.

    Attributes:
        tenant: The tenant every worker contended for.
        attempts: What each of the ten contenders reported.
        winner: The one contender that was granted ownership.
        granted_lease: The lease the cluster held once the race settled.
        exit_status: What the killed winner's process reported, which is a signal
            rather than an exit.
        expiry_before_kill: The stored window before the signal.
        expiry_after_kill: The stored window afterwards, which must be the same one.
        early: The worker refused while the window was still open.
        reading_before_early: The cluster's reading before that attempt.
        reading_after_early: The cluster's reading afterwards, still inside the window.
        slept_seconds: How much real time was spent on the remainder of the window.
        reading_after_window: The cluster's reading once the window had passed.
        successor: The worker granted ownership once it had.
        revived: What the revived winner reported about its Disposition write.
        dispositions_before: How many Disposition rows the run held before that write.
        dispositions_after: How many it held afterwards.
    """

    tenant: Tenant
    attempts: tuple[Attempt, ...]
    winner: Attempt
    granted_lease: StoredLease
    exit_status: int | None
    expiry_before_kill: datetime
    expiry_after_kill: datetime
    early: Attempt
    reading_before_early: datetime
    reading_after_early: datetime
    slept_seconds: float
    reading_after_window: datetime
    successor: Attempt
    revived: dict[str, object]
    dispositions_before: int
    dispositions_after: int

    @property
    def refusals(self) -> tuple[Attempt, ...]:
        """Every contender of the race that was refused."""
        return tuple(attempt for attempt in self.attempts if attempt.outcome == REFUSED)

    @property
    def grants(self) -> tuple[Attempt, ...]:
        """Every contender of the race that was granted ownership."""
        return tuple(attempt for attempt in self.attempts if attempt.granted)


def _race(cluster: Cluster, tenant: Tenant) -> tuple[tuple[Attempt, ...], subprocess.Popen[str]]:
    """Spawn ten workers that ask at one shared instant, and keep the winner alive.

    The winner's process is returned still running, because the next step of the
    scenario is to kill it while it holds the lease.
    """
    start = time.time() + START_DELAY_SECONDS
    processes = {
        f"contender-{index}": _spawn(
            _contend_arguments(
                cluster,
                tenant,
                owner=f"contender-{index}",
                start=start,
                hold=HOLD_SECONDS,
            ),
            dsn=cluster.dsn,
        )
        for index in range(CONTENDERS)
    }
    attempts: list[Attempt] = []
    winner: subprocess.Popen[str] | None = None
    try:
        for owner, process in processes.items():
            attempt = Attempt(owner=owner, report=_reported(process))
            attempts.append(attempt)
            if attempt.granted:
                winner = process
    finally:
        for process in processes.values():
            if process is not winner:
                _finish(process)
    assert winner is not None, "no worker of the race was granted ownership"
    return tuple(attempts), winner


def _hold_out_the_window(cluster: Cluster, expiry: datetime) -> float:
    """Spend the remainder of one stored window, and report how much was spent.

    This is the one place real time passes, and it is unavoidable: takeability is the
    cluster's comparison of a stored instant against its own reading, so a window
    already written cannot be closed by any clock a worker controls. Most of the
    interval has already been spent by the race and by the refused attempt, so what is
    spent here is the remainder of it.
    """
    remaining = (expiry - cluster.reading()).total_seconds() + EXPIRY_MARGIN_SECONDS
    if remaining <= 0.0:
        return 0.0
    time.sleep(remaining)
    return remaining


def _drive(cluster: Cluster) -> Contention:
    """Drive the whole scenario once, in the order the design states it."""
    tenant = cluster.tenant()
    attempts, winner_process = _race(cluster, tenant)
    granted = cluster.current_lease(tenant.client_id)
    assert granted is not None, "the race left no lease current"
    expiry_before_kill = cluster.expiry_of(granted.lease_id)
    exit_status = _kill(winner_process)
    expiry_after_kill = cluster.expiry_of(granted.lease_id)

    reading_before_early = cluster.reading()
    early = _one_attempt(cluster, tenant, owner=EARLY_OWNER)
    reading_after_early = cluster.reading()

    slept = _hold_out_the_window(cluster, expiry_after_kill)
    reading_after_window = cluster.reading()
    successor = _one_attempt(cluster, tenant, owner=SUCCESSOR_OWNER)

    dispositions_before = cluster.dispositions(tenant.run_id)
    revived_process = _spawn(
        (
            REVIVE_MODE,
            cluster.schema,
            str(tenant.client_id),
            str(tenant.run_id),
            tenant.slug,
            str(granted.generation),
        ),
        dsn=cluster.dsn,
    )
    try:
        revived = _reported(revived_process)
    finally:
        _finish(revived_process)
    dispositions_after = cluster.dispositions(tenant.run_id)

    return Contention(
        tenant=tenant,
        attempts=attempts,
        winner=next(attempt for attempt in attempts if attempt.granted),
        granted_lease=granted,
        exit_status=exit_status,
        expiry_before_kill=expiry_before_kill,
        expiry_after_kill=expiry_after_kill,
        early=early,
        reading_before_early=reading_before_early,
        reading_after_early=reading_after_early,
        slept_seconds=slept,
        reading_after_window=reading_after_window,
        successor=successor,
        revived=revived,
        dispositions_before=dispositions_before,
        dispositions_after=dispositions_after,
    )


@pytest.fixture(scope="module")
def contention(cluster: Cluster) -> Contention:
    """One settled scenario, driven once and asserted stage by stage."""
    settled = _drive(cluster)
    print(f"the winning owner is {settled.winner.owner}")
    print(f"the generation recorded at grant is {settled.winner.generation}")
    print(f"{len(settled.refusals)} of {CONTENDERS} workers were refused")
    print(f"the generation recorded at takeover is {settled.successor.generation}")
    print(f"the remainder of the window cost {settled.slept_seconds:.2f} seconds of real time")
    return settled


# ---------------------------------------------------------------------------
# The claims
# ---------------------------------------------------------------------------


def test_ten_workers_contend_and_exactly_one_is_granted_by_name(
    cluster: Cluster,
    contention: Contention,
) -> None:
    """One grant, nine or more refusals, and every refusal names the same winner."""
    assert len(contention.attempts) == CONTENDERS
    assert len(contention.grants) == EXPECTED_GRANTS, (
        f"{len(contention.grants)} workers were granted ownership of one Client at once"
    )
    assert len(contention.refusals) >= MINIMUM_REFUSALS

    # The refusal names who won and under which generation, so the loser of the
    # contest learns whose lease it lost to rather than only that it lost.
    winner = contention.winner
    assert {attempt.refused_owner for attempt in contention.refusals} == {winner.owner}
    assert {attempt.refused_generation for attempt in contention.refusals} == {winner.generation}

    # And the cluster holds exactly what the winner was told it holds.
    assert contention.granted_lease.lease_id == winner.lease_id
    assert contention.granted_lease.owner == winner.owner
    assert contention.granted_lease.generation == winner.generation
    assert not winner.report["took_over"], "the first grant of a Client superseded a lease"
    held = cluster.current_lease(contention.tenant.client_id)
    assert held is not None


def test_the_winner_is_killed_leaving_the_window_where_it_stood(
    contention: Contention,
) -> None:
    """The winner died by signal, renewed nothing, and released nothing."""
    assert contention.exit_status is not None
    assert contention.exit_status < 0, (
        f"the winning worker ended with status {contention.exit_status} rather than by a signal, "
        "so it was not terminated abruptly"
    )
    assert contention.expiry_after_kill == contention.expiry_before_kill, (
        "the stored window moved across the signal, so the winner renewed or released"
    )
    assert contention.granted_lease.expires_at == contention.expiry_after_kill


def test_a_takeover_is_refused_inside_the_window_and_granted_once_it_has_passed(
    cluster: Cluster,
    contention: Contention,
) -> None:
    """Ownership moves on the cluster's clock: refused before the instant, granted after.

    Both readings are the cluster's own and both are taken around the refused attempt,
    so the refusal is asserted against a window the cluster itself read as open rather
    than against this process's opinion of the time.
    """
    expiry = contention.expiry_after_kill
    assert contention.reading_before_early < expiry
    assert contention.reading_after_early < expiry, (
        "the window closed while the refused attempt was in flight, so the refusal "
        "below says nothing about a takeover before expiry"
    )
    assert contention.early.outcome == REFUSED
    assert contention.early.refused_owner == contention.winner.owner
    assert contention.early.refused_generation == contention.winner.generation

    # Once the cluster reads the window as past, the same request is granted, with the
    # generation incremented and the expired lease superseded by the successor.
    assert expiry < contention.reading_after_window
    assert contention.successor.granted
    assert contention.successor.generation == contention.winner.generation + 1
    assert contention.successor.report["took_over"] is True
    assert cluster.superseded_by(contention.winner.lease_id) == contention.successor.lease_id
    held = cluster.current_lease(contention.tenant.client_id)
    assert held is not None
    assert held.lease_id == contention.successor.lease_id
    assert held.generation == contention.successor.generation


def test_the_revived_worker_is_fenced_and_records_no_disposition(
    contention: Contention,
) -> None:
    """The revived winner's write is refused by both generations and persists nothing."""
    revived = contention.revived
    assert revived["outcome"] == REFUSED, "the revived worker's Disposition write was admitted"
    assert revived["presented"] == contention.winner.generation
    assert revived["current"] == contention.successor.generation

    # Nothing was persisted, read from the cluster rather than from the worker.
    assert contention.dispositions_before == 0
    assert contention.dispositions_after == contention.dispositions_before

    # And the refusal was counted once, in the process whose write was refused, and
    # recorded as well as counted.
    assert revived["counted"] == 1.0
    assert revived["recorded"] is True


if __name__ == "__main__":  # pragma: no cover - the worker entry point
    raise SystemExit(worker_main(sys.argv[1:]))
