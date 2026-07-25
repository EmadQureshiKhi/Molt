"""The as-of-attribution query answers inside 1 second over a hundred versions.

**Validates: Requirements 43.10**

Why the bound exists. Attribution is an immutable version history, and the as-of
form is what an auditor reads: who was this Artifact attributed to at that instant,
under which method, at what confidence. An Artifact that has been re-detected many
times carries a long history, and a read whose cost grew with that history would
make the audit answer slower for exactly the Artifacts an audit is most interested
in. Requirement 43.10 fixes the history length the bound has to hold at, and this
module is the only place a history of that length is built.

**What is inside the measurement.** One `attribution_as_of` call per sample, which
is the store's own read: a transaction, the module's own statement with the instant
bound twice for the half-open interval, and the rows narrowed into version values.

**What is outside it, and reported separately.** Placing the history is this
module's setup rather than the design's work, so it is timed on its own line. The
statistics refresh is outside too, and it is not cosmetic: the covering index over
one Artifact's versions is the thing the bound rests on, and the optimiser will
prefer a scan until it has statistics to prefer the index with.

**Why the corpus carries no vector.** An Attribution_Version names an Artifact and a
Client and says nothing about a representation of it, and the as-of statement
projects only the version columns. So a history over an Artifact with no Embedding
is read exactly like one over an Artifact with an Embedding, while a vector per row
would cost per-row vector index maintenance in the setup for no change to what is
measured. The absence is stated here rather than hidden.

**The history is placed in one statement.** The whole version chain goes in as bound
arrays the cluster expands: every version but the last is closed naming its
successor, and the last is the current one. That shape is what the partial unique
index over unsuperseded versions admits, and it is the shape the product's own
supersession produces, one version at a time, on the write path.

**A latency claim needs a sample.** One timing is an anecdote, so the bound is taken
over a sample of reads at instants spread across the history, and the assertion is
made against the slowest of them rather than the average: a bound stated as "within
1 second" is a claim about a read, not about a mean.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.store import Connection, MemoryStore
from molt.store.attribution import VersionAsOf, attribution_as_of
from molt.store.migrate import apply_migrations

# A bound measured against a cluster: the history is placed by real statements and
# the read is the store's own, so this module needs a reachable instance and skips
# at collection naming what was missing when none is.
pytestmark = [pytest.mark.perf, pytest.mark.instance]

# The history length the requirement states and the bound it states for it. The
# corpus is placed a little longer than the floor, so the measurement is over a
# history that exceeds what is required rather than one that only just meets it.
VERSION_FLOOR: Final[int] = 100
VERSIONS_PLACED: Final[int] = 150
AS_OF_BOUND_SECONDS: Final[float] = 1.0

# How many reads the figure is taken over, and the fraction reported beside the
# slowest.
SAMPLE_READS: Final[int] = 40
REPORTED_FRACTION: Final[float] = 0.95

# The instant the history begins at, derived from the epoch rather than written as a
# literal, and how long one version stands for.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
VERSION_SPAN: Final[timedelta] = timedelta(hours=1)
RETENTION: Final[timedelta] = timedelta(days=90)

# What every placed row carries. None of them is what this module measures, so the
# method is one value and the confidence walks a fixed ladder inside the unit
# interval, which is what a re-detection changing its mind looks like.
ARTIFACT_KIND: Final[str] = "derived_artifact"
BINDING_METHOD: Final[str] = "marker"
BASE_CONFIDENCE: Final[float] = 0.50
CONFIDENCE_STEP: Final[float] = 0.001
DERIVATION_METHOD: Final[str] = "distilled"
ARTIFACT_BODY: Final[str] = "a derived body carrying a long attribution history"
ARTIFACT_DIGEST: Final[str] = "0" * 64
PENDING: Final[str] = "pending"
MARKER: Final[str] = "ACME-INTERNAL"

# The fixture's own statements. Every value is bound and no identifier is ever
# interpolated, the search path included.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, content_markers) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACT: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, embedding_state, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)

# The whole version history in one statement. The arrays are positionally aligned,
# so version n carries the identifier of version n plus one as its successor and the
# last carries none, which is the one unsuperseded row the partial unique index
# admits.
INSERT_VERSIONS_BULK: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from, valid_to, superseded_by) "
    "SELECT unnest(%s::UUID[]), %s, %s, %s, %s, unnest(%s::FLOAT8[]), "
    "unnest(%s::TIMESTAMPTZ[]), unnest(%s::TIMESTAMPTZ[]), unnest(%s::UUID[])"
)

COUNT_VERSIONS: Final[str] = "SELECT count(*) FROM client_binding WHERE artifact_id = %s"
COUNT_CURRENT: Final[str] = (
    "SELECT count(*) FROM client_binding WHERE artifact_id = %s AND superseded_by IS NULL"
)
ANALYSE_BINDINGS: Final[str] = "ANALYZE client_binding"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


@dataclass(frozen=True, slots=True)
class History:
    """The placed version history and what a read against it needs."""

    store: MemoryStore
    connection: DriverConnection
    artifact_id: UUID
    client_id: UUID
    versions: int
    placement_seconds: float
    analyse_seconds: float

    def count(self, statement: str) -> int:
        """Read one count over this Artifact on the fixture's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, (self.artifact_id,))
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def instant_for(self, sample: int) -> datetime:
        """The instant one sample asks about, spread across the whole history.

        Each sample lands inside a different version's validity interval, so no read
        is the previous read repeated and every read has an answer.
        """
        position = (sample * max(1, self.versions // SAMPLE_READS)) % self.versions
        return MOMENT + VERSION_SPAN * position + VERSION_SPAN / 2


def _send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def _place_history(
    connection: DriverConnection,
    artifact_id: UUID,
    client_id: UUID,
) -> None:
    """Place the whole version chain as one statement over aligned bound arrays."""
    identifiers = [uuid4() for _ in range(VERSIONS_PLACED)]
    confidences = [BASE_CONFIDENCE + index * CONFIDENCE_STEP for index in range(VERSIONS_PLACED)]
    starts = [MOMENT + VERSION_SPAN * index for index in range(VERSIONS_PLACED)]
    ends: list[datetime | None] = [start + VERSION_SPAN for start in starts]
    ends[-1] = None
    successors: list[UUID | None] = [*identifiers[1:], None]
    _send(
        connection,
        INSERT_VERSIONS_BULK,
        (
            identifiers,
            artifact_id,
            ARTIFACT_KIND,
            client_id,
            BINDING_METHOD,
            confidences,
            starts,
            ends,
            successors,
        ),
    )


@pytest.fixture(scope="module")
def history(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[History]:
    """Place one Artifact's long version history, and report what the setup cost."""
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    client_id = uuid4()
    artifact_id = uuid4()
    slug = f"tenant-{client_id.hex[:12]}"
    _send(fresh_schema, INSERT_CLIENT, (client_id, slug, f"Tenant {slug}", [MARKER]))
    _send(
        fresh_schema,
        INSERT_ARTIFACT,
        (
            artifact_id,
            "summary",
            client_id,
            ARTIFACT_BODY,
            ARTIFACT_DIGEST,
            DERIVATION_METHOD,
            PENDING,
            MOMENT + RETENTION,
        ),
    )

    started = time.perf_counter()
    _place_history(fresh_schema, artifact_id, client_id)
    placement = time.perf_counter() - started

    started = time.perf_counter()
    with fresh_schema.cursor() as cursor:
        cursor.execute(ANALYSE_BINDINGS)
    analysed = time.perf_counter() - started

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    print(
        f"\nsetup, outside every measurement: {VERSIONS_PLACED} attribution versions "
        f"placed in {placement:.2f}s, statistics refreshed in {analysed:.2f}s"
    )

    with MemoryStore(connect_with=connect_with) as store:
        yield History(
            store=store,
            connection=fresh_schema,
            artifact_id=artifact_id,
            client_id=client_id,
            versions=VERSIONS_PLACED,
            placement_seconds=placement,
            analyse_seconds=analysed,
        )


def _percentile(samples: Sequence[float], fraction: float) -> float:
    """The sample at a fraction of the sorted order, by nearest rank."""
    ordered = sorted(samples)
    rank = math.ceil(fraction * len(ordered)) - 1
    return ordered[min(len(ordered) - 1, max(0, rank))]


def test_the_history_is_the_length_the_bound_is_stated_over(history: History) -> None:
    """The measurement is worthless unless the Artifact carries the stated history."""
    placed = history.count(COUNT_VERSIONS)
    assert placed == VERSIONS_PLACED
    assert placed >= VERSION_FLOOR, (
        f"the Artifact carries {placed} versions where the bound is stated over at "
        f"least {VERSION_FLOOR}"
    )
    assert history.count(COUNT_CURRENT) == 1, (
        "more than one version is unsuperseded, so the placed history is not a history"
    )


def test_the_as_of_attribution_query_answers_inside_the_bound(history: History) -> None:
    """Requirement 43.10: every sampled read answers within 1 second."""
    latencies: list[float] = []
    answers: list[int] = []
    for sample in range(SAMPLE_READS):
        instant = history.instant_for(sample)
        started = time.perf_counter()
        found: tuple[VersionAsOf, ...] = attribution_as_of(
            history.store, history.artifact_id, instant
        )
        latencies.append(time.perf_counter() - started)
        answers.append(len(found))

    slowest = max(latencies)
    print(
        f"as-of attribution over {history.versions} versions: slowest "
        f"{slowest:.4f}s, p95 {_percentile(latencies, REPORTED_FRACTION):.4f}s, "
        f"median {_percentile(latencies, 0.5):.4f}s, samples {len(latencies)} "
        f"(bound {AS_OF_BOUND_SECONDS:.0f}s); setup "
        f"{history.placement_seconds:.2f}s placement plus "
        f"{history.analyse_seconds:.2f}s statistics, outside the measurement"
    )

    assert all(count == 1 for count in answers), (
        "a sampled instant returned other than one version, so the figure describes "
        "an empty or an ambiguous answer rather than an as-of read"
    )
    assert slowest < AS_OF_BOUND_SECONDS, (
        f"the slowest as-of read took {slowest:.4f}s against a bound of {AS_OF_BOUND_SECONDS:.0f}s"
    )
