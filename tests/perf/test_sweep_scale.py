"""The explicit sweep over a hundred thousand Artifacts stays inside sixty seconds.

Why the bound exists. Requirement 44.8 states the sweep of a hundred thousand
Artifacts as a sixty-second budget, and the sweep is the phase that budget is
about: it is six set-based statements against the cluster, so the whole cost is
the cluster's plan rather than a round trip per candidate. A sweep that fell back
to per-row work would blow the bound long before it blew any correctness claim.

What is inside the measurement, and what is deliberately outside it. The timed
section is one `run_sweep` call: the six inserting statements, the chain-tip
capture, the per-reason counts, and the pending-embedding count, in one
SERIALIZABLE transaction. Placing the corpus is outside it and is reported on its
own line, because the placement is this module's setup rather than the design's
work, and charging a bulk insert to the sweep's budget would describe neither.

Why the corpus carries no vector. The sweep selects by identity, by scope, by
current attribution, and by lineage, and reaches an Embedding only through the
Artifact it belongs to. So an Artifact with no vector is swept exactly like one
with a vector, and a hundred thousand vectors would add nothing to what is
measured while costing per-row vector index maintenance in the setup. The one
thing the absent vectors change is the pending-embedding count, which the sweep
records on the run row and this module asserts, so the absence is on the record
rather than hidden.

The corpus is placed in bulk, as arrays bound to one statement per batch, which is
what keeps the setup to seconds rather than minutes.

**Validates: Requirements 44.8**
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.confidence import ConfidencePolicy
from molt.config.resolve import Configuration
from molt.erase.engine import INSERT_REQUEST_STATEMENT, INSERT_RUN_STATEMENT, Phase, RunStatus
from molt.erase.sweep import SweepResult, run_sweep
from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations

pytestmark = [pytest.mark.perf, pytest.mark.instance]

# The scale the requirement states and the bound it states for it.
ARTIFACT_TARGET: Final[int] = 100000
SWEEP_BOUND_SECONDS: Final[float] = 60.0

# How many rows one statement places. Large enough that the corpus is a few dozen
# statements, small enough that no one statement carries the whole array.
BATCH_ROWS: Final[int] = 5000

# The fixture's own statements. Every value is bound and no identifier is ever
# interpolated, the search path included.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, content_markers) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACTS_BULK: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, embedding_state, expires_at) "
    "SELECT unnest(%s::UUID[]), %s, %s, %s, %s, %s, %s, %s"
)
INSERT_BINDINGS_BULK: Final[str] = (
    "INSERT INTO client_binding (artifact_id, artifact_kind, client_id, method, confidence) "
    "SELECT unnest(%s::UUID[]), %s, %s, %s, %s"
)
COUNT_ARTIFACTS: Final[str] = "SELECT count(*) FROM derived_artifact"
COUNT_CANDIDATES: Final[str] = "SELECT count(*) FROM erasure_candidate WHERE run_id = %s"
READ_UNEMBEDDED: Final[str] = "SELECT unembedded_count FROM erasure_run WHERE id = %s"
ANALYSE_ARTIFACTS: Final[str] = "ANALYZE derived_artifact"
ANALYSE_BINDINGS: Final[str] = "ANALYZE client_binding"

# What every placed row carries. None of them is what this module measures, so none
# is varied, and the digest is one value of the width the schema declares because
# the column carries no uniqueness constraint.
ARTIFACT_KIND: Final[str] = "summary"
BINDING_METHOD: Final[str] = "marker"
BINDING_CONFIDENCE: Final[float] = 0.9
DERIVATION_METHOD: Final[str] = "distilled"
ARTIFACT_BODY: Final[str] = "a derived body the sweep reaches by attribution"
ARTIFACT_DIGEST: Final[str] = "0" * 64
PENDING: Final[str] = "pending"
REQUESTER: Final[str] = "governance-owner"
JUSTIFICATION: Final[str] = "engagement concluded under a contractual purge obligation"
MARKER: Final[str] = "ACME-INTERNAL"

# An instant with an offset derived from the epoch rather than written as a
# literal, so a run embeds nothing about when it happened.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# The thresholds the run row records. The sweep turns on neither, and a run row
# holds both, so they are stated rather than defaulted.
AUTO_INCLUDE_THRESHOLD: Final[float] = 0.20
REVIEW_THRESHOLD: Final[float] = 0.45

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def example_configuration() -> Configuration:
    """A configuration naming the one number the sweep reads, rather than defaulting it."""
    return Configuration(environ={"MOLT_PROCEDURE_RECALL_FLOOR": "0.15"}, file_values={})


@dataclass(frozen=True, slots=True)
class Corpus:
    """A schema holding every migration, a store over it, and the placed corpus."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID
    run_id: UUID
    placement_seconds: float

    def count(self, statement: str, params: tuple[object, ...] | None = None) -> int:
        """Read one count on the fixture's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])


def _send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def _place_artifacts(
    connection: DriverConnection,
    artifacts: Sequence[UUID],
    client_id: UUID,
) -> None:
    """Place one Derived_Artifact per identifier, in batches of one bound array."""
    for start in range(0, len(artifacts), BATCH_ROWS):
        batch = list(artifacts[start : start + BATCH_ROWS])
        _send(
            connection,
            INSERT_ARTIFACTS_BULK,
            (
                batch,
                ARTIFACT_KIND,
                client_id,
                ARTIFACT_BODY,
                ARTIFACT_DIGEST,
                DERIVATION_METHOD,
                PENDING,
                EXPIRY,
            ),
        )


def _place_bindings(
    connection: DriverConnection,
    artifacts: Sequence[UUID],
    client_id: UUID,
) -> None:
    """Place one current Attribution_Version per Artifact, in the same batches."""
    for start in range(0, len(artifacts), BATCH_ROWS):
        batch = list(artifacts[start : start + BATCH_ROWS])
        _send(
            connection,
            INSERT_BINDINGS_BULK,
            (
                batch,
                ArtifactKind.DERIVED_ARTIFACT.value,
                client_id,
                BINDING_METHOD,
                BINDING_CONFIDENCE,
            ),
        )


def _open_run(connection: DriverConnection, client_id: UUID) -> UUID:
    """Open the request and the run row the sweep records its counts on."""
    request_id = uuid4()
    run_id = uuid4()
    _send(
        connection,
        INSERT_REQUEST_STATEMENT,
        (request_id, client_id, REQUESTER, JUSTIFICATION, RunStatus.RUNNING.value),
    )
    _send(
        connection,
        INSERT_RUN_STATEMENT,
        (
            run_id,
            request_id,
            client_id,
            REQUESTER,
            False,
            RunStatus.RUNNING.value,
            Phase.SWEEP.value,
            AUTO_INCLUDE_THRESHOLD,
            REVIEW_THRESHOLD,
        ),
    )
    return run_id


@pytest.fixture(scope="module")
def corpus(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Corpus]:
    """Place a hundred thousand attributed Artifacts in bulk, and report what it cost."""
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    client_id = uuid4()
    slug = f"tenant-{client_id.hex[:12]}"
    _send(fresh_schema, INSERT_CLIENT, (client_id, slug, f"Tenant {slug}", [MARKER]))
    artifacts = tuple(uuid4() for _ in range(ARTIFACT_TARGET))

    started = time.perf_counter()
    _place_artifacts(fresh_schema, artifacts, client_id)
    _place_bindings(fresh_schema, artifacts, client_id)
    placement = time.perf_counter() - started

    with fresh_schema.cursor() as cursor:
        cursor.execute(ANALYSE_ARTIFACTS)
        cursor.execute(ANALYSE_BINDINGS)

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    with MemoryStore(connect_with=connect_with, statement_timeout_ms=180000) as store:
        yield Corpus(
            store=store,
            connection=fresh_schema,
            client_id=client_id,
            run_id=_open_run(fresh_schema, client_id),
            placement_seconds=placement,
        )


def test_the_explicit_sweep_of_a_hundred_thousand_artifacts_stays_inside_the_bound(
    corpus: Corpus,
) -> None:
    """One `run_sweep` call over the whole corpus, timed and reported."""
    assert corpus.count(COUNT_ARTIFACTS) == ARTIFACT_TARGET, "the corpus is the stated scale"

    started = time.perf_counter()
    swept: SweepResult = run_sweep(
        corpus.store,
        corpus.run_id,
        corpus.client_id,
        policy=ConfidencePolicy.from_configuration(example_configuration()),
    )
    elapsed = time.perf_counter() - started

    print(
        f"\nsweep of {ARTIFACT_TARGET} artifacts: {elapsed:.2f}s "
        f"(bound {SWEEP_BOUND_SECONDS:.0f}s); corpus placement: "
        f"{corpus.placement_seconds:.2f}s, outside the measurement"
    )
    assert swept.counts.client_binding == ARTIFACT_TARGET, (
        "every attributed Artifact was selected, so the bound was measured over the whole corpus"
    )
    assert corpus.count(COUNT_CANDIDATES, (corpus.run_id,)) == ARTIFACT_TARGET
    assert corpus.count(READ_UNEMBEDDED, (corpus.run_id,)) == ARTIFACT_TARGET, (
        "the corpus carries no vector, and the count of that is on the run row"
    )
    assert elapsed < SWEEP_BOUND_SECONDS, (
        f"the sweep took {elapsed:.2f}s against a bound of {SWEEP_BOUND_SECONDS:.0f}s"
    )
