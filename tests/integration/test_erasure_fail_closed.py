"""One fault per fail-closed path of an erasure run, injected at the engine's seams.

Every claim here is a claim about what the cluster holds after a fault, because
that is the only place a fail-closed bias is real: a run that says it failed
closed and left an erased Client's content in place has failed open.

**Adjudication unavailable includes the review-band candidate anyway.** The
Adjudicator collapses every provider fault into one inclusion carrying the
fail-closed reason and the adjudicated flag false, so a provider that answers
nothing widens the erasure rather than narrowing it.

**A rewrite that cannot be produced, and a rewrite produced badly, both delete.**
Unavailability and a reply that still carries the erased Client's marker reach the
same disposition by the same rule: blended memory is lost rather than erased
content left behind, and the disposition records the rewriter's own collapsed
reason.

**The self-managed path probed unavailable falls back to a referenced
Managed_Backup.** The fallback answers a capability question, so a record probed
unavailable takes it, and the row it writes is referenced rather than taken: the
run names a backup that exists rather than claiming it made one.

**No backup evidence means no mutation.** A run whose only admissible path fails
aborts with every memory-content table unchanged, and the failed attempt is itself
recorded.

**A lease lost mid-run refuses the next evidence write.** A superseded owner cannot
record a disposition, cannot move the phase marker, and cannot declare the run
finished; the evidence written under the valid generation stays where it is. It
cannot record its own abort either, so the run row is left at the running status
with the phase it reached, which is the fence working rather than a second fault:
the ending belongs to the owner that now holds the lease.

**An exhausted retry leaves a resumable aborted run.** The run row names the phase
it reached and the evidence committed up to that point stands, which is what makes
the ending resumable rather than ambiguous.

**Validates: Requirements 15.5, 16.8, 17.8, 18.7, 19.3, 19.6, 32.1, 44.8**
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Final, TypeVar
from uuid import UUID, uuid4

import pytest
from tests.integration.test_erasure_engine import (
    INSERT_ARTIFACT,
    INSERT_EMBEDDING,
    SEARCH_PATH_STATEMENT,
    Cluster,
    Fixture,
    Recorder,
    StubTextProvider,
    body_digest,
    issued,
    refused,
    request_for,
    seams,
)

from molt.backup import BackupPath, BackupStatus, CommandResult
from molt.erase.adjudicator import FAIL_CLOSED_REASON as ADJUDICATION_FAIL_CLOSED
from molt.erase.engine import (
    EXTEND_LABEL,
    EngineSeams,
    Phase,
    PhaseProgress,
    RunStatus,
    run_erasure,
)
from molt.erase.lease import acquire
from molt.erase.rewriter import FAIL_CLOSED_REASON as REWRITE_FAIL_CLOSED
from molt.erase.sweep import REASON_CLIENT_BINDING
from molt.errors import (
    BackupFailedError,
    ModelUnavailableError,
    SerializationExhaustedError,
    StaleFencingGenerationError,
)
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind, DerivedArtifactKind
from molt.providers import Prompt, TextResult
from molt.store import Connection, Cursor, MemoryStore
from molt.store.capability import SELF_MANAGED_BACKUP, Capability, CapabilityRecord
from molt.store.embeddings import vector_text
from molt.store.migrate import apply_migrations
from molt.store.retry import DEFAULT_TRANSACTION_LABEL

pytestmark = pytest.mark.integration

# The rows and readings this module owns, over and above the harness it reuses.
# Every one is a whole literal with bound parameters.
EXPIRE_CURRENT_LEASE: Final[str] = (
    "UPDATE erasure_lease SET expires_at = acquired_at + INTERVAL '1 microsecond' "
    "WHERE client_id = %s AND superseded_at IS NULL"
)
READ_RESIDUE_FINDING: Final[str] = (
    "SELECT included, adjudicated, decision_reason, band, cosine_distance "
    "FROM residue_candidate WHERE run_id = %s AND artifact_id = %s"
)
READ_DISPOSITION: Final[str] = (
    "SELECT disposition, reason FROM disposition WHERE run_id = %s AND artifact_id = %s"
)
READ_BACKUP_RECORD: Final[str] = (
    "SELECT backup_path, taken, referenced, status, backup_id FROM backup_record WHERE run_id = %s"
)
READ_RUN_ENDING: Final[str] = (
    "SELECT id, status, phase, error_detail, finished_at, finalised_at "
    "FROM erasure_run WHERE client_id = %s"
)
READ_REQUEST_STATUS: Final[str] = (
    "SELECT r.status FROM erasure_request AS r JOIN erasure_run AS n ON n.request_id = r.id "
    "WHERE n.id = %s"
)
COUNT_ARTIFACT: Final[str] = "SELECT count(*) FROM derived_artifact WHERE id = %s"
COUNT_CANDIDATES_BY_REASON: Final[str] = (
    "SELECT count(*) FROM erasure_candidate WHERE run_id = %s AND selection_reason = %s"
)
COUNT_CANDIDATES: Final[str] = "SELECT count(*) FROM erasure_candidate WHERE run_id = %s"
COUNT_DISPOSITIONS: Final[str] = "SELECT count(*) FROM disposition WHERE run_id = %s"
COUNT_BACKUP_RECORDS: Final[str] = "SELECT count(*) FROM backup_record WHERE run_id = %s"

# The two bodies the review-band pair carries. Distinct text, so the pair is a
# neighbour pair because of the vectors placed for it rather than by accident.
QUERY_BODY: Final[str] = "the invoicing exporter reconciles the ledger every night at close"
NEIGHBOUR_BODY: Final[str] = "a nightly reconciliation job reads the invoice ledger and exports it"

# Where the pair's vectors sit. The query is one axis; the neighbour is that axis
# at seven tenths with the remainder on a second axis, so the cosine distance
# between them is three tenths: above the auto-inclusion threshold the example
# configuration names and inside its review band, which is the one band that
# reaches the Adjudicator at all.
_NEIGHBOUR_COSINE: Final[float] = 0.7
_EXPECTED_DISTANCE: Final[float] = 1.0 - _NEIGHBOUR_COSINE

# The listing the control plane answers the fallback with, and the identifier a
# referenced backup is recorded under.
MANAGED_BACKUP_ID: Final[str] = "the-scheduled-managed-backup"
# The instant the listing reports, derived from the epoch rather than written as a
# literal, so the example embeds nothing about when it was authored.
MANAGED_BACKUP_INSTANT: Final[str] = datetime.fromtimestamp(0.0, tz=UTC).isoformat()
MANAGED_LISTING: Final[str] = json.dumps(
    {"backups": [{"id": MANAGED_BACKUP_ID, "as_of": MANAGED_BACKUP_INSTANT}]}
)

OTHER_OWNER: Final[str] = "the-worker-that-took-over"

# A connection is typed loosely for the same reason the harness types it loosely:
# the driver is reached through a fixture rather than imported.
DriverConnection = Any

# What one injected transaction returns, carried through the overriding store so the
# override keeps the shape the surface it replaces declares.
T = TypeVar("T")

# How near the recorded distance has to land for the placed pair to be the pair the
# example intends, in the units the stored column holds.
_DISTANCE_TOLERANCE: Final[float] = 0.01


# ---------------------------------------------------------------------------
# The seams the faults are injected at
# ---------------------------------------------------------------------------


class UnavailableTextProvider(StubTextProvider):
    """A Text_Provider that is reachable and answers nothing, for every prompt.

    One stub covers the adjudication fault and the rewrite fault, because the
    design collapses every provider fault into one outcome and a stub that
    distinguished them would be asserting a distinction the code does not make.
    """

    def generate(self, prompt: Prompt) -> TextResult:
        """Refuse as an unreachable provider does."""
        assert prompt.stable_prefix, "every prompt this design sends carries a stable prefix"
        self.calls += 1
        raise ModelUnavailableError("the stub provider is unreachable")


@dataclass(slots=True)
class RecordingRunner:
    """A control-plane seam that records its invocations and answers from a script."""

    result: CommandResult | None = None
    fault: str = ""
    vectors: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
        """Record the invocation, then answer or fail as the script says."""
        assert timeout_seconds > 0, "the fallback is invoked under a bound"
        self.vectors.append(tuple(vector))
        if self.result is not None:
            return self.result
        raise OSError(self.fault or "the control plane could not be reached")

    @property
    def entered(self) -> bool:
        """Whether the fallback path was entered at all."""
        return bool(self.vectors)


class ExhaustingStore(MemoryStore):
    """A store whose transaction under one label always reports an exhausted retry.

    Injected rather than provoked with a competing writer, because what is being
    asserted is the run's ending and not the platform's conflict detection: the
    exhausted failure is the one the retry wrapper raises after its last attempt,
    and it commits nothing, which is exactly the state a real exhaustion leaves.
    """

    def __init__(self, *, connect_with: Callable[[], Connection], failing_label: str) -> None:
        super().__init__(connect_with=connect_with)
        self.failing_label = failing_label
        self.attempts = 0

    def in_serializable(
        self,
        body: Callable[[Cursor], T],
        *,
        label: str = DEFAULT_TRANSACTION_LABEL,
    ) -> T:
        """Run the body, unless this is the label whose retries are exhausted."""
        if label == self.failing_label:
            self.attempts += 1
            raise SerializationExhaustedError(self.attempts)
        return super().in_serializable(body, label=label)


@dataclass(frozen=True, slots=True)
class Instance:
    """The cluster harness plus the connection factory a second store is built on."""

    cluster: Cluster
    connect_with: Callable[[], Connection]


@pytest.fixture(scope="module")
def instance(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Instance]:
    """Every migration, a store over this module's own schema, and the factory behind it.

    The factory is exposed because one example runs the engine against a store
    whose retries are exhausted, and that store has to reach the same schema.
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
        yield Instance(
            cluster=Cluster(store=store, connection=fresh_schema),
            connect_with=connect_with,
        )


# ---------------------------------------------------------------------------
# The corpus the residue phase acts on
# ---------------------------------------------------------------------------


def axis_vector(*places: tuple[int, float]) -> tuple[float, ...]:
    """A unit vector carrying the named component on each named axis."""
    held = dict(places)
    return tuple(held.get(index, 0.0) for index in range(EMBEDDING_DIMENSION))


def embedded_artifact(
    cluster: Cluster,
    owner_client_id: UUID,
    body: str,
    vector: tuple[float, ...],
) -> UUID:
    """Place one Derived_Artifact with a vector this module chose rather than derived.

    The harness derives a vector from the body's digest, which places every pair at
    the maximum distance. A review-band example needs a chosen distance, so the
    vector is supplied here.
    """
    identifier = uuid4()
    cluster.send(
        INSERT_ARTIFACT,
        (
            identifier,
            DerivedArtifactKind.SUMMARY.value,
            owner_client_id,
            body,
            body_digest(body),
            "distilled",
        ),
    )
    cluster.send(
        INSERT_EMBEDDING,
        (
            identifier,
            ArtifactKind.DERIVED_ARTIFACT.value,
            owner_client_id,
            "stub-embedding",
            "stub-embedding-model",
            EMBEDDING_DIMENSION,
            vector_text(vector),
        ),
    )
    return identifier


def review_band_pair(cluster: Cluster, fixture: Fixture) -> tuple[UUID, UUID]:
    """One query Artifact of the erased tenant and one neighbour in the review band.

    The neighbour holds no binding to the erased tenant, so it is absent from the
    explicit sweep's candidate set and reachable only as residue, which is what
    puts it in front of the Adjudicator.
    """
    query_id = embedded_artifact(
        cluster,
        fixture.erased_id,
        QUERY_BODY,
        axis_vector((0, 1.0)),
    )
    cluster.bind(query_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
    neighbour_id = embedded_artifact(
        cluster,
        fixture.retained_id,
        NEIGHBOUR_BODY,
        axis_vector((0, _NEIGHBOUR_COSINE), (1, (1.0 - _NEIGHBOUR_COSINE**2) ** 0.5)),
    )
    cluster.bind(neighbour_id, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
    return query_id, neighbour_id


def probed_unavailable() -> CapabilityRecord:
    """A capability record reporting the self-managed path probed and refused."""
    return CapabilityRecord((Capability(SELF_MANAGED_BACKUP, available=False, detail="s3"),))


def with_backup_seams(
    base: EngineSeams,
    *,
    capabilities: CapabilityRecord,
    runner: RecordingRunner,
) -> EngineSeams:
    """The harness seams with the backup path's two seams replaced."""
    return dataclasses.replace(base, capabilities=capabilities, runner=runner)


# ---------------------------------------------------------------------------
# Adjudication unavailable
# ---------------------------------------------------------------------------


def test_adjudication_unavailable_includes_the_review_band_candidate_unjudged(
    instance: Instance,
) -> None:
    """A provider answering nothing widens the erasure rather than narrowing it."""
    cluster = instance.cluster
    fixture = cluster.fixture()
    _, neighbour_id = review_band_pair(cluster, fixture)
    unavailable = UnavailableTextProvider(answer="")

    outcome = run_erasure(
        cluster.store,
        request_for(fixture, dry_run=True),
        seams(fixture, text=unavailable),
    )

    assert outcome.run_id is not None
    assert unavailable.calls >= 1, "the provider was actually asked"
    finding = cluster.one(READ_RESIDUE_FINDING, (outcome.run_id, neighbour_id))
    assert finding[0] is True, "an unjudged review-band candidate is included"
    assert finding[1] is False, "and it is recorded as unadjudicated"
    assert finding[2] == ADJUDICATION_FAIL_CLOSED, "under the fail-closed reason"
    assert finding[3] == "review", "from the review band, which is the only band that judges"
    assert abs(float(finding[4]) - _EXPECTED_DISTANCE) < _DISTANCE_TOLERANCE, (
        "and the distance is the one the placed vectors fix"
    )
    assert outcome.residue is not None
    assert any(
        found.artifact_id == neighbour_id and found.included for found in outcome.residue.findings
    ), "and the report states the inclusion"


# ---------------------------------------------------------------------------
# The two rewrite faults
# ---------------------------------------------------------------------------


def test_a_rewrite_the_provider_cannot_answer_falls_to_the_hard_delete(
    instance: Instance,
) -> None:
    """Rewrite unavailability deletes the blended Artifact and records the reason."""
    cluster = instance.cluster
    fixture = cluster.fixture()
    _, blended_id = cluster.corpus(fixture)
    unavailable = UnavailableTextProvider(answer="")

    outcome = run_erasure(cluster.store, request_for(fixture), seams(fixture, text=unavailable))

    assert outcome.status is RunStatus.COMPLETED, "the run finishes; the rewrite is what failed"
    assert outcome.fail_closed_rewrites == 1
    assert outcome.run_id is not None
    assert cluster.count(COUNT_ARTIFACT, (blended_id,)) == 0, "the blended Artifact is gone"
    recorded = cluster.one(READ_DISPOSITION, (outcome.run_id, blended_id))
    assert recorded[0] == "hard_delete"
    assert recorded[1] == REWRITE_FAIL_CLOSED, "the rewriter's own collapsed reason is recorded"


def test_a_rewrite_answering_badly_falls_to_the_hard_delete(instance: Instance) -> None:
    """A reply that still carries the erased marker is unusable, so nothing is left."""
    cluster = instance.cluster
    fixture = cluster.fixture()
    _, blended_id = cluster.corpus(fixture)
    badly = StubTextProvider(answer="both tenants are described here and neither line is removed")

    outcome = run_erasure(cluster.store, request_for(fixture), seams(fixture, text=badly))

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.fail_closed_rewrites == 1
    assert outcome.run_id is not None
    assert badly.calls >= 1, "the provider answered, and the answer was refused"
    assert cluster.count(COUNT_ARTIFACT, (blended_id,)) == 0
    recorded = cluster.one(READ_DISPOSITION, (outcome.run_id, blended_id))
    assert recorded[0] == "hard_delete"
    assert recorded[1] == REWRITE_FAIL_CLOSED


# ---------------------------------------------------------------------------
# The backup paths
# ---------------------------------------------------------------------------


def test_the_self_managed_path_probed_unavailable_references_a_managed_backup(
    instance: Instance,
) -> None:
    """The fallback names a backup that exists, and records referencing rather than taking."""
    cluster = instance.cluster
    fixture = cluster.fixture()
    cluster.corpus(fixture)
    runner = RecordingRunner(result=CommandResult(0, MANAGED_LISTING))

    outcome = run_erasure(
        cluster.store,
        request_for(fixture),
        with_backup_seams(
            seams(fixture, issuer=refused),
            capabilities=probed_unavailable(),
            runner=runner,
        ),
    )

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.run_id is not None
    assert runner.entered, "the fallback was entered because the path was probed unavailable"
    assert outcome.backup is not None
    assert outcome.backup.referenced is True
    assert outcome.backup.taken is False, "a referenced backup is not one this run made"
    assert outcome.backup.backup_path is BackupPath.MANAGED_REFERENCED
    recorded = cluster.one(READ_BACKUP_RECORD, (outcome.run_id,))
    assert recorded[0] == BackupPath.MANAGED_REFERENCED.value
    assert recorded[1] is False
    assert recorded[2] is True
    assert recorded[3] == BackupStatus.SUCCEEDED.value
    assert recorded[4] == MANAGED_BACKUP_ID, "the referenced backup is named on the record"


def test_both_backup_paths_failing_aborts_before_any_memory_content_is_touched(
    instance: Instance,
) -> None:
    """No backup evidence, no deletion: the run ends with every content table unchanged."""
    cluster = instance.cluster
    fixture = cluster.fixture()
    cluster.corpus(fixture)
    before = cluster.content
    runner = RecordingRunner(fault="the control plane answered nothing")

    with pytest.raises(BackupFailedError):
        run_erasure(
            cluster.store,
            request_for(fixture),
            with_backup_seams(
                seams(fixture, issuer=refused),
                capabilities=probed_unavailable(),
                runner=runner,
            ),
        )

    assert runner.entered, "the admissible path was attempted"
    assert cluster.content == before, "and not one content row moved"
    ended = cluster.one(READ_RUN_ENDING, (fixture.erased_id,))
    assert ended[1] == RunStatus.ABORTED.value
    assert ended[2] == Phase.SWEEP.value, "the run stopped before phase one"
    assert ended[3], "the detail says why"
    assert cluster.count(COUNT_CANDIDATES, (ended[0],)) == 0
    assert cluster.count(COUNT_DISPOSITIONS, (ended[0],)) == 0
    assert cluster.count(COUNT_BACKUP_RECORDS, (ended[0],)) == 1, (
        "the failed attempt is itself recorded as evidence"
    )


# ---------------------------------------------------------------------------
# The lease lost mid-run
# ---------------------------------------------------------------------------


def test_a_lease_lost_mid_run_refuses_the_next_evidence_write_and_aborts(
    instance: Instance,
) -> None:
    """A superseded owner records no further evidence, and what it wrote stands."""
    cluster = instance.cluster
    fixture = cluster.fixture()
    cluster.corpus(fixture)
    recorder = Recorder()
    taken_over: list[int] = []

    def supersede_after_the_sweep(progress: PhaseProgress) -> None:
        """Take ownership away once phase one has committed its evidence."""
        recorder(progress)
        if progress.phase is Phase.RESIDUE and not taken_over:
            cluster.send(EXPIRE_CURRENT_LEASE, (fixture.erased_id,))
            successor = acquire(cluster.store, fixture.erased_id, OTHER_OWNER, uuid4().hex)
            taken_over.append(successor.generation)

    with pytest.raises(StaleFencingGenerationError):
        run_erasure(
            cluster.store,
            request_for(fixture),
            dataclasses.replace(seams(fixture), progress=supersede_after_the_sweep),
        )

    assert taken_over, "ownership really moved to another worker mid-run"
    assert Phase.RESIDUE in recorder.phases
    assert Phase.CERTIFICATE not in recorder.phases, (
        "the superseded owner declared nothing finished"
    )
    ended = cluster.one(READ_RUN_ENDING, (fixture.erased_id,))
    assert ended[1] == RunStatus.RUNNING.value, (
        "the superseded owner cannot record its own abort either, which is the fence "
        "working rather than a second fault: the status is left for the owner that now "
        "holds the lease to settle"
    )
    assert ended[2] == Phase.RESIDUE.value, "the phase marker stopped where the fence refused it"
    assert ended[4] is None, "an owner that owns nothing records no ending instant"
    assert ended[5] is None, "and the attempt was never finalised"
    assert cluster.count(COUNT_DISPOSITIONS, (ended[0],)) == 0, "no disposition was recorded"
    assert cluster.count(COUNT_CANDIDATES_BY_REASON, (ended[0], REASON_CLIENT_BINDING)) > 0, (
        "and the evidence written under the valid generation is retained"
    )


# ---------------------------------------------------------------------------
# The exhausted retry
# ---------------------------------------------------------------------------


def test_serialization_retries_exhausted_leave_a_resumable_aborted_run(
    instance: Instance,
) -> None:
    """The run names the phase it reached, and phase one's evidence stands."""
    cluster = instance.cluster
    fixture = cluster.fixture()
    cluster.corpus(fixture)
    exhausting = ExhaustingStore(
        connect_with=instance.connect_with,
        failing_label=EXTEND_LABEL,
    )
    try:
        with pytest.raises(SerializationExhaustedError):
            run_erasure(exhausting, request_for(fixture), seams(fixture, issuer=issued))
    finally:
        exhausting.close()

    assert exhausting.attempts == 1, "the exhausted transaction is the one that was injected"
    ended = cluster.one(READ_RUN_ENDING, (fixture.erased_id,))
    assert ended[1] == RunStatus.ABORTED.value
    assert ended[2] == Phase.RESIDUE.value, "the phase reached is on the row, so a resume knows it"
    assert ended[3], "and the detail says what stopped it"
    assert ended[4] is not None, "the run is finished rather than left in flight"
    assert ended[5] is None, "no finalisation was recorded, so the attempt may be retried"
    assert cluster.one(READ_REQUEST_STATUS, (ended[0],))[0] == RunStatus.ABORTED.value
    assert cluster.count(COUNT_CANDIDATES_BY_REASON, (ended[0], REASON_CLIENT_BINDING)) > 0, (
        "phase one's committed evidence stands, which is what makes the abort resumable"
    )
    assert cluster.count(COUNT_DISPOSITIONS, (ended[0],)) == 0, "and no disposition was reached"
