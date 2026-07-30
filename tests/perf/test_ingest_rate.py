"""The signed ingest path sustains more than 100 Events per second to a cluster.

**Validates: Requirements 33.2**

Why the bound exists. The Collector is the only way an Event becomes durable. When
it cannot keep up the capture side spools, and a spool that drains slower than it
fills is memory that never lands: the Ledger stops being the system of record and
the halt state the capture side reads back stops being current. Requirement 33.2
fixes the floor the ingest path must hold, and this module is the only place the
whole path is driven at a rate rather than one request at a time.

**The measurement is of `serve`, not of `ingest`.** The handler exposes both: the
batch route on its own, and the request path that reaches it through the bearer
gate, the request bound, the transport decode, and the Ingress_Signature
verification. Driving the route on its own would take the signature off the path
and report the remainder as if it were the whole, so every timed call here is one
`serve`.

**Nothing is injected at the signature seam.** The Collector is given the shared
value and no verifier, so the handler resolves the real verification call by name
and the digest is recomputed over the exact body bytes once per batch. The case
ahead of the benchmark is what makes that a fact rather than a hope: the same
bytes are refused when the signature is altered and when the presented timestamp
falls outside the age bound, the rejection counter the verifying module owns
records both refusals, no row lands from either, and then the same bytes,
correctly signed, are accepted and their rows are counted in the Ledger. A
benchmark that had quietly bypassed the signature could not produce that pair of
outcomes from one Collector.

**What is inside the timer.** One `serve` call, and everything it does: the
constant-time bearer comparison, the body bound, the decode, the keyed digest over
the whole body, the batch read into Events, the Session upsert, one hash-chain
append per Event inside a single SERIALIZABLE transaction against a real cluster,
and the halt read the response envelope carries.

**What is outside it.** Schema creation and the migration; the corpus, which is
built into request bodies before the sample begins; connection establishment,
which the reported warm-up batch pays for; and the capture side's own signing.
Signing is the sender's cost and is measured where the sender is measured, in the
hook benchmark; verification is the Collector's cost and is inside.

**Single-threaded, deliberately.** A chain append is sequence-ordered within a
Session: two writers appending to one Session read the same tip, so the second
commit conflicts and retries, and a concurrent measurement over one Session would
report contention rather than throughput. A concurrent measurement over distinct
Sessions would report the aggregate of several writers, which is Requirement 33.1's
claim about concurrent machines rather than this one's about a sustained rate. One
writer is therefore the honest reading of the floor: many machines writing to
distinct Sessions can only exceed what one writer holds, and one writer keeps the
load this benchmark places on a shared instance bounded.

**Sustained rather than sampled once.** A single small batch timed once would say
nothing about a rate. The sample is twenty batches of fifty Events, a thousand
Events in total, spread over four Sessions so the chains under measurement grow
rather than restarting at genesis on every batch. Each batch is timed on its own,
so the report carries the median, the 95th percentile, the worst batch, and the
rate the worst batch alone achieved: an aggregate that passes while one batch has
become an order of magnitude slower is worth seeing rather than averaging away.

The corpus is kept small on purpose. A thousand rows of modest payload is enough
to state a rate and little enough to leave nothing behind worth mentioning; the
schema this module builds is dropped when the module finishes.

Each measurement also asserts what came back, because a path that silently
persisted nothing would post an excellent rate: every batch must report all fifty
of its records accepted and none rejected, the Ledger row count must grow by
exactly the number of Events driven, and one Session's chain must verify by
independent recomputation over the rows the benchmark wrote.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.capture.hook import batch_body
from molt.capture.signing import (
    AUTHORIZATION_HEADER,
    BEARER_SCHEME,
    COLLECTOR_BEARER_ENV,
    INGRESS_KEY_ENV,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ingress_timestamp,
    sign_ingress,
)
from molt.collector.handler import Collector, Invocation
from molt.collector.ingress import SIGNATURE_REJECTED_METRIC
from molt.collector.routes import EVENTS_PATH, Headers
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.models.event import Event, EventCategory, JsonObject
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.store import Connection, MemoryStore
from molt.store.chain import verify_chain
from molt.store.migrate import apply_migrations, discover_migrations
from molt.telemetry import current, reset

# A bound measured against a cluster: every timed call opens a real transaction
# and writes real rows through the store's own retry wrapper. The performance
# marker says what is measured and the instance marker states what that
# measurement needs, so with no instance reachable this module skips at collection
# naming what was missing, while the benchmarks that need nothing beyond this
# process still run.
pytestmark = [pytest.mark.perf, pytest.mark.instance]

# The rate Requirement 33.2 states, in Events per second.
INGEST_RATE_BOUND: Final[float] = 100.0

# How the sample reaches a thousand Events. Fifty records is the size a busy hook
# invocation flushing a spool presents, and twenty batches is long enough that one
# slow batch cannot carry the aggregate on its own.
BATCH_EVENTS: Final[int] = 50
SAMPLE_BATCHES: Final[int] = 20
TOTAL_EVENTS: Final[int] = BATCH_EVENTS * SAMPLE_BATCHES

# How many Sessions the sample spreads over, and what each one accumulates. More
# than one, so the measurement is not one chain's growth alone; few enough that
# every chain under measurement is hundreds of rows deep by the end rather than
# starting from genesis on each batch.
SAMPLE_SESSIONS: Final[int] = 4
BATCHES_PER_SESSION: Final[int] = SAMPLE_BATCHES // SAMPLE_SESSIONS
EVENTS_PER_SESSION: Final[int] = BATCHES_PER_SESSION * BATCH_EVENTS

# The fraction reported beside the median, and how many records the signature case
# ahead of the benchmark presents.
REPORTED_FRACTION: Final[float] = 0.95
GUARD_RECORDS: Final[int] = 4

# Milliseconds in a second, for the per-batch report.
MS_PER_SECOND: Final[float] = 1000.0

# The only migration this module needs: the tenant table with its reserved row,
# the Session table, and the Ledger with its two uniqueness constraints. Staging
# one generation rather than every one keeps the schema this benchmark builds to
# what the ingest path actually writes.
CORE_MIGRATION_VERSION: Final[int] = 1

# The two credentials, shaped like the values a deployment holds and obviously
# synthetic. Neither name carries a word the credential-shape lint inspects, so no
# call site below needs a suppression in order to pass a literal.
SHARED_VALUE: Final[str] = "an-ingress-shared-value"
BEARER_VALUE: Final[str] = "a-collector-bearer-value"

MACHINE: Final[str] = "machine-under-test"
TOOL: Final[str] = "claude_code"

# The statuses this module distinguishes. A refused caller and an accepted batch
# are different answers, and no assertion below may conflate them.
OK: Final[int] = 200
UNAUTHORISED: Final[int] = 401

# The bound a presented timestamp must fall inside, read from the configuration
# surface rather than restated. The Collector below is given no value for it
# either, so both sides resolve the same default from the same declaration.
MAX_AGE_SECONDS: Final[int] = Configuration(environ={}).integer("MOLT_INGRESS_MAX_AGE_SECONDS")

# The instant the generated records observe, derived from the epoch rather than
# written as a literal so a run embeds nothing about when it happened, and the
# spacing between two records so no two rows of one chain share an instant. The
# record instants are unrelated to the request timestamp on purpose: the age bound
# is measured against what a request presents, not against what its records saw.
RECORD_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RECORD_STEP: Final[timedelta] = timedelta(milliseconds=250)

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture, and the two counts read back.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
COUNT_LEDGER: Final[str] = "SELECT count(*) FROM ledger"
COUNT_SESSIONS: Final[str] = "SELECT count(*) FROM session"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# The batch one request carries
# ---------------------------------------------------------------------------


def record_payload(index: int) -> JsonObject:
    """The payload one record carries: modest, structured, and realistic.

    A command, its result, and the correlation identifier a result links by, so
    each record costs a real canonical rendering and a real digest rather than the
    smallest document the wire form admits.
    """
    return {
        "tool": "Bash",
        "command": "pytest tests/unit -q",
        "exit_code": 0,
        "duration_ms": 412,
        "tool_use_id": f"a-tool-use-identifier-{index}",
    }


def build_record(session_id: UUID, index: int) -> Event:
    """One well-formed Event of the shape the capture side transmits.

    The text body is present, so the row lands owing a vector exactly as a captured
    tool result does. That is the state the ingest path writes for real content, and
    a benchmark over records carrying none would measure a narrower row.
    """
    return Event(
        id=uuid4(),
        session_id=session_id,
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_RESULT,
        occurred_at=RECORD_INSTANT + RECORD_STEP * index,
        agent_cli=TOOL,
        machine_id=MACHINE,
        parent_event_id=None,
        payload=record_payload(index),
        redacted=False,
        text_body="the suite passed and nothing was written",
    )


@dataclass(frozen=True, slots=True)
class Batch:
    """One request body, the Session its records name, and how many it carries."""

    session_id: UUID
    body: bytes
    records: int


def build_batch(session_id: UUID, first: int, records: int) -> Batch:
    """Build one newline-delimited batch body for one Session."""
    return Batch(
        session_id=session_id,
        body=batch_body([build_record(session_id, first + offset) for offset in range(records)]),
        records=records,
    )


def presented_headers(body: bytes, *, moment: datetime) -> Headers:
    """The three headers one ingest request presents, signed over these bytes.

    The bearer header is always correct: the bearer gate sits ahead of the
    signature gate and answers with the same status, so a request failing both
    would say nothing about the second.
    """
    timestamp = ingress_timestamp(moment)
    return Headers(
        {
            AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {BEARER_VALUE}",
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign_ingress(body, SHARED_VALUE, timestamp),
        }
    )


def with_mutated_signature(headers: Headers) -> Headers:
    """The same headers with one hex character of the digest changed.

    The replacement is chosen against the character it replaces, so the mutation
    always changes the digest rather than silently leaving it as it was.
    """
    presented = headers[SIGNATURE_HEADER]
    swapped = "0" if presented[0] != "0" else "1"
    return Headers(dict(headers) | {SIGNATURE_HEADER: swapped + presented[1:]})


def invocation_for(body: bytes, headers: Headers) -> Invocation:
    """One request as the transport would deliver it, carried as text.

    Built outside every timer: the decode to transport text is the harness standing
    in for a transport, while the re-encode the handler performs is the deployed
    path's own cost and is inside.
    """
    return Invocation(
        method="POST",
        path=EVENTS_PATH,
        headers=headers,
        body_text=body.decode("utf-8"),
        base64_encoded=False,
    )


# ---------------------------------------------------------------------------
# The cluster the batches are driven against
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bench:
    """A Collector over a real cluster, plus the prepared corpus."""

    collector: Collector
    store: MemoryStore
    connection: DriverConnection
    warmup: Batch
    batches: tuple[Batch, ...]

    def scalar(self, statement: str) -> int:
        """Read one count on the fixture's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement)
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def ledger_rows(self) -> int:
        """How many Ledger rows the schema holds."""
        return self.scalar(COUNT_LEDGER)


def stage_core_migration(destination: Path) -> None:
    """Copy the core migration file into a directory of its own."""
    for migration in discover_migrations():
        if migration.version == CORE_MIGRATION_VERSION:
            destination.joinpath(migration.path.name).write_bytes(migration.path.read_bytes())


def build_collector(store: MemoryStore) -> Collector:
    """A Collector holding both credentials, with nothing injected at the seam.

    No verifier is attached, so the handler resolves the verification call by name
    and the real digest comparison runs on every timed request. Every setting is the
    configuration surface's own default, so the bounds this benchmark runs under are
    the deployed ones rather than values chosen to flatter it.
    """
    return Collector(
        configuration=Configuration(environ={}),
        store=store,
        bearer=Credential(
            BEARER_VALUE,
            source_name=COLLECTOR_BEARER_ENV,
            source=CredentialSource.ENVIRONMENT,
        ),
        ingress_key=Credential(
            SHARED_VALUE,
            source_name=INGRESS_KEY_ENV,
            source=CredentialSource.ENVIRONMENT,
        ),
    )


def plan_batches() -> tuple[Batch, ...]:
    """Build every sample body up front, so no rendering happens inside a timer.

    Batches rotate over the Sessions, so each chain grows by a batch every fourth
    request instead of every request starting a fresh chain at genesis.
    """
    sessions = tuple(uuid4() for _ in range(SAMPLE_SESSIONS))
    return tuple(
        build_batch(sessions[index % SAMPLE_SESSIONS], index * BATCH_EVENTS, BATCH_EVENTS)
        for index in range(SAMPLE_BATCHES)
    )


@pytest.fixture(scope="module")
def bench(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Bench]:
    """Apply the core migration, build the Collector, and prepare the corpus.

    Module scope pays the schema and the corpus once for both cases. The schema is
    created and dropped by the shared fixture this one builds on, so the rows this
    benchmark writes leave nothing behind after the module finishes.
    """
    directory = tmp_path_factory.mktemp("molt_ingest_rate_core")
    stage_core_migration(directory)
    apply_migrations(fresh_schema, directory=directory)

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

    started = time.perf_counter()
    warmup = build_batch(uuid4(), 0, BATCH_EVENTS)
    batches = plan_batches()
    built = time.perf_counter() - started
    total_bytes = len(warmup.body) + sum(len(batch.body) for batch in batches)

    print(
        f"corpus: {TOTAL_EVENTS} Events in {SAMPLE_BATCHES} batches of {BATCH_EVENTS} "
        f"across {SAMPLE_SESSIONS} Sessions, {total_bytes} request bytes, "
        f"rendered in {built:.3f} s outside every timer"
    )

    with MemoryStore(connect_with=connect_with) as store:
        yield Bench(
            collector=build_collector(store),
            store=store,
            connection=fresh_schema,
            warmup=warmup,
            batches=batches,
        )


# ---------------------------------------------------------------------------
# Driving one batch
# ---------------------------------------------------------------------------


def drive(bench: Bench, batch: Batch) -> float:
    """Present one signed batch and return what the whole path cost, in seconds.

    The signature is computed here, outside the timer, because signing is the
    capture side's cost. Verification of that signature happens inside the call
    below, on the Collector's own path, which is what the rate is stated over.
    """
    invocation = invocation_for(batch.body, presented_headers(batch.body, moment=datetime.now(UTC)))

    started = time.perf_counter()
    answer = bench.collector.serve(invocation)
    elapsed = time.perf_counter() - started

    assert answer.status == OK, f"the batch was answered {answer.status}: {answer.document}"
    assert answer.document["accepted"] == batch.records, (
        f"the batch reported {answer.document['accepted']} of {batch.records} records "
        "accepted, so the timing describes less work than the rate is stated over"
    )
    assert answer.document["rejected"] == 0
    return elapsed


# ---------------------------------------------------------------------------
# Statistics and reporting
# ---------------------------------------------------------------------------


def percentile(samples: tuple[float, ...], fraction: float) -> float:
    """The sample at a fraction of the sorted order, by nearest rank.

    Nearest rank rather than an interpolation, so the answer is one of the batches
    that actually happened.
    """
    if not samples:
        raise ValueError("a percentile needs at least one sample")
    ordered = sorted(samples)
    rank = math.ceil(fraction * len(ordered)) - 1
    return ordered[min(len(ordered) - 1, max(0, rank))]


@dataclass(frozen=True, slots=True)
class Spread:
    """The per-batch timings of one sustained run, in seconds."""

    samples: tuple[float, ...]
    events_per_batch: int

    @property
    def events(self) -> int:
        """How many Events the whole sample carried."""
        return len(self.samples) * self.events_per_batch

    @property
    def seconds(self) -> float:
        """How long the sample spent inside the timer, in total."""
        return sum(self.samples)

    @property
    def rate(self) -> float:
        """The sustained rate: every Event over the time the path spent on them."""
        return self.events / self.seconds

    @property
    def slowest_batch_rate(self) -> float:
        """The rate the worst single batch achieved on its own."""
        return self.events_per_batch / max(self.samples)

    def summary(self) -> str:
        """Two lines: the sustained figure, then the spread behind it."""
        return (
            f"sustained ingest: {self.events} Events in {len(self.samples)} batches of "
            f"{self.events_per_batch}, {self.seconds:.3f} s inside the timer, "
            f"{self.rate:.1f} Events/s, bound {INGEST_RATE_BOUND:.0f} Events/s\n"
            f"per batch: p50 {percentile(self.samples, 0.50) * MS_PER_SECOND:.1f} ms, "
            f"p95 {percentile(self.samples, REPORTED_FRACTION) * MS_PER_SECOND:.1f} ms, "
            f"max {max(self.samples) * MS_PER_SECOND:.1f} ms, "
            f"min {min(self.samples) * MS_PER_SECOND:.1f} ms; "
            f"slowest batch alone {self.slowest_batch_rate:.1f} Events/s"
        )


# ---------------------------------------------------------------------------
# The signature really is on the measured path
# ---------------------------------------------------------------------------


def test_the_signature_is_verified_on_the_path_the_rate_is_measured_over(bench: Bench) -> None:
    """One Collector refuses these bytes unsigned and accepts them signed.

    Three requests carry the same body. The first presents a mutated digest, the
    second presents a correct digest over a timestamp outside the age bound, and the
    third presents the digest the shared value produces for the reading now. The
    first two are refused and cost the Ledger nothing; the third lands every record.

    The rejection counter read afterwards belongs to the verifying module itself, so
    two refusals counted under it is evidence that the real verification call ran
    rather than that some seam somewhere returned false. Nothing else in this module
    emits it.
    """
    reset()
    batch = build_batch(uuid4(), TOTAL_EVENTS, GUARD_RECORDS)
    before = bench.ledger_rows()

    honest = presented_headers(batch.body, moment=datetime.now(UTC))
    refused = bench.collector.serve(invocation_for(batch.body, with_mutated_signature(honest)))
    assert refused.status == UNAUTHORISED
    assert bench.ledger_rows() == before, "a refused batch persisted a row"

    stale_moment = datetime.now(UTC) - timedelta(seconds=MAX_AGE_SECONDS + 1)
    stale = bench.collector.serve(
        invocation_for(batch.body, presented_headers(batch.body, moment=stale_moment))
    )
    assert stale.status == UNAUTHORISED
    assert bench.ledger_rows() == before, "an out-of-window batch persisted a row"

    counted = current().counters().get((SIGNATURE_REJECTED_METRIC, ()), 0.0)
    assert counted == 2.0, (
        f"the verifying module counted {counted} rejections where two were made, so "
        "the signature check the benchmark relies on may not be the real one"
    )

    accepted = bench.collector.serve(invocation_for(batch.body, honest))
    assert accepted.status == OK
    assert accepted.document["accepted"] == GUARD_RECORDS
    assert bench.ledger_rows() == before + GUARD_RECORDS


# ---------------------------------------------------------------------------
# The benchmark
# ---------------------------------------------------------------------------


def test_the_signed_ingest_path_sustains_the_stated_rate(bench: Bench) -> None:
    """A thousand Events, signed and verified per batch, above 100 per second.

    The warm-up batch is timed and reported rather than dropped, because it pays the
    first connection, the verifier resolution, and the cluster's first plan for each
    statement, and charging those to a rate stated over twenty batches would
    describe neither the first batch nor the other twenty.

    The assertion is made against the aggregate rather than against every batch: on
    an instance several writers share, one batch can lose a scheduling slice without
    the path having got slower. The worst batch's own rate is reported on every run
    so that a batch which really has become slow is visible instead of averaged away.
    """
    first = drive(bench, bench.warmup)
    before = bench.ledger_rows()

    samples = tuple(drive(bench, batch) for batch in bench.batches)
    spread = Spread(samples=samples, events_per_batch=BATCH_EVENTS)

    print(
        f"warm-up batch, including the first connection and the verifier resolution: "
        f"{first * MS_PER_SECOND:.1f} ms for {BATCH_EVENTS} Events"
    )
    print(spread.summary())

    assert spread.events == TOTAL_EVENTS
    assert bench.ledger_rows() == before + TOTAL_EVENTS, (
        "the Ledger did not grow by the number of Events driven, so the rate above "
        "does not describe rows that landed"
    )
    assert bench.scalar(COUNT_SESSIONS) >= SAMPLE_SESSIONS

    report = verify_chain(bench.store, bench.batches[0].session_id)
    assert report.ok, f"the chain disagreed at sequence {report.first_mismatch_seq}"
    assert report.rows == EVENTS_PER_SESSION, (
        f"one Session's chain holds {report.rows} rows where {EVENTS_PER_SESSION} were "
        "appended to it, so the batches did not land where they were addressed"
    )

    assert spread.rate >= INGEST_RATE_BOUND, spread.summary()
