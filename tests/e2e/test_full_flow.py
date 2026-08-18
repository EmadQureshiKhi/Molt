"""The whole governed sequence against a live instance, driven once, in order.

Every other suite asserts one component against a cluster. This module asserts
that the components compose: a seeded corpus is contaminated, an Event batch is
admitted through the Collector's own signed request path, a recall page is served
with a Learned_Procedure's standing deciding its position, a threshold grid is
answered over the read-only role, a signed Ledger_Checkpoint is taken, a leased
erasure runs, a certificate is issued, and an independent verifier holding nothing
but a public key and a read-only connection reports on it.

The sequence runs once in a module-scoped fixture and the assertions read the
result, because the eight stages are one history rather than eight independent
setups, and re-running them per assertion would assert eight different histories.

Four claims are what the module exists for, and each is a claim about what the
database holds rather than about what the flow returned.

**The certificate verifies.** Not "was produced", not "parses": the outcome of an
independent verification against the erased cluster is the verified one, which is
the auditor's whole workflow and the only assertion that exercises the signature,
the embedded queries, the derived counts, the chain tips, the checkpoint, and the
disposition claims together.

**The counts are confirmed through the derived mechanism.** The before-count and
the after-count are re-derived from append-only rows, so the agreement holds
whatever the opportunistic point-in-time read happened to find, and the assertion
says so explicitly rather than depending on the corroboration outcome.

**The ownership fence, the named checkpoint, and the first-attribution pair each
agree with a stored row.** The fencing generation is compared against the lease
the run held, the checkpoint against the checkpoint row the cluster stores, and
the first-attribution instant against the attribution rows it was read from
before the disposition phase removed the rest.

**The working-row count is the tenant's own.** The aggregate the certificate
states is compared against the number of Working_Memory rows the tenant held
before the run and the number it holds after.

**Validates: Requirements 20.2, 36.12, 42.13, 43.7, 44.11, 45.11, 47.1, 48.9**
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from molt.attest.builder import (
    COUNT_DERIVATION,
    CertificatePolicy,
    IssuedCertificate,
    first_attribution_snapshot,
    issue,
)
from molt.attest.checkpoint import (
    CheckpointPolicy,
    CheckpointWindow,
    StoredCheckpoint,
    compute,
    latest_before,
    sign_and_store,
)
from molt.attest.verifier import (
    DERIVED_MECHANISM,
    VerificationReport,
    parse_envelope,
    verify_certificate,
    verify_signature,
)
from molt.backup import BackupSettings, CommandResult
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
from molt.collector.routes import EVENTS_PATH, Headers
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.erase import sensitivity
from molt.erase.engine import EngineSeams, ErasureRequest, RunOutcome, run_erasure
from molt.models.artifact import (
    EMBEDDING_DIMENSION,
    ArtifactKind,
    DerivedArtifact,
    DerivedArtifactKind,
)
from molt.models.event import EmbeddingState, Event, EventCategory
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.providers import Prompt, ProviderProbe, TextResult
from molt.recall import Recalled, RecallEngine
from molt.seed import contaminate
from molt.seed.corpora import SeedVolumes
from molt.seed.generator import SeedResult, generate
from molt.seed.vectors import SeedEmbedder
from molt.store import Connection, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.store.capability import CapabilityRecord
from molt.store.embeddings import EmbeddingWrite, write_derived_artifact
from molt.store.historical import GC_HORIZON_CAPABILITY
from molt.store.lineage import ParentRef, insert_lineage_edge
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.e2e

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The read-only role every read-only stage runs as. The migrations create it, and
# the fixture opens this module's own schema to it, because a migration grants on
# the tables it creates and which namespace a test places them in is no
# migration's business.
READER_ROLE: Final[str] = "molt_reader"
SET_READER_ROLE_STATEMENT: Final[str] = "SET ROLE molt_reader"

# The reads every claim about stored rows is made from.
CLUSTER_NOW: Final[str] = "SELECT now()"
CURRENT_SCHEMA: Final[str] = "SELECT current_schema()"
UPSERT_CAPABILITY: Final[str] = (
    "UPSERT INTO capability (name, available, detail) VALUES (%s, %s, %s)"
)
COUNT_TENANT_SESSIONS: Final[str] = "SELECT count(*) FROM session WHERE client_id = %s"
COUNT_TENANT_LEDGER: Final[str] = "SELECT count(*) FROM ledger WHERE client_id = %s"
COUNT_TENANT_WORKING: Final[str] = "SELECT count(*) FROM working_memory WHERE client_id = %s"
COUNT_ARTIFACT: Final[str] = "SELECT count(*) FROM derived_artifact WHERE id = %s"
COUNT_SESSION_LEDGER: Final[str] = "SELECT count(*) FROM ledger WHERE session_id = %s"
COUNT_SESSION_ROW: Final[str] = "SELECT count(*) FROM session WHERE id = %s"
CURRENT_BOUND_ARTIFACTS: Final[str] = (
    "SELECT artifact_id FROM client_binding WHERE client_id = %s AND superseded_by IS NULL"
)
FIRST_ATTRIBUTION_ROW: Final[str] = (
    "SELECT min(valid_from), (array_agg(method ORDER BY valid_from))[1] "
    "FROM client_binding WHERE client_id = %s AND artifact_id = %s"
)
RUN_OWNERSHIP_ROW: Final[str] = (
    "SELECT r.fencing_generation, r.idempotency_key, l.owner, l.generation "
    "FROM erasure_run AS r JOIN erasure_lease AS l ON l.id = r.lease_id WHERE r.id = %s"
)
RUN_WORKING_ROWS: Final[str] = "SELECT working_rows_deleted FROM erasure_run WHERE id = %s"
CHECKPOINT_ROW: Final[str] = (
    "SELECT root_digest, covered_session_count, kms_key_id, signing_algorithm "
    "FROM ledger_checkpoint WHERE id = %s"
)

# The synthetic candidate set the threshold analysis is answered over. Nothing is
# being erased on that path, so the run row exists to give the candidate rows
# something to belong to and is closed before it is read.
INSERT_ANALYSIS_REQUEST: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification, status) "
    "VALUES (%s, %s, %s, %s, 'completed')"
)
INSERT_ANALYSIS_RUN: Final[str] = (
    "INSERT INTO erasure_run "
    "(id, request_id, client_id, requester, dry_run, status, phase, t_before, t_after) "
    "VALUES (%s, %s, %s, %s, true, 'completed', 'done', now(), now())"
)
INSERT_ANALYSIS_CANDIDATE: Final[str] = (
    "INSERT INTO erasure_candidate (run_id, artifact_id, artifact_kind, selection_reason) "
    "VALUES (%s, %s, 'event', 'event_of_scoped_session')"
)

# The binding the recall corpus is attributed through. The detector owns the
# ordinary path; this module places the row directly because what it is arranging
# is a ranking, not a detection.
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) VALUES (%s, %s, %s, %s, 'scope', 1.0, %s)"
)

# The generation the corpus is produced at, small enough to run the whole flow
# quickly and large enough to carry nested Sessions, blended Artifacts, planted
# fragments, and a working row per Session.
SEED: Final[int] = 4242
VOLUMES: Final[SeedVolumes] = SeedVolumes(
    clients=4,
    sessions=6,
    events=90,
    subagent_sessions_depth_two=1,
    subagent_sessions_depth_three=1,
    blended_artifacts=2,
    planted_fragments=3,
    working_rows_per_session=1,
)

# How far in the past the corpus is dated. Every seeded Session sits ahead of the
# generation instant by a fixed spacing, so the corpus is dated from a reading
# behind the cluster's own in order that a checkpoint window can close over it and
# still precede the erasure run.
CORPUS_AGE: Final[timedelta] = timedelta(hours=3)
CHECKPOINT_LEAD: Final[timedelta] = timedelta(hours=4)
CHECKPOINT_INTERVAL: Final[int] = 3600

# The signing surface. One locally generated key pair signs the checkpoint and the
# certificate, and the same pair's public half is what the verifier retrieves, so
# nothing here reaches a key service and no credential is present.
SIGNING_KEY_ID: Final[str] = "a-local-signing-key-the-flow-suite-holds"
SIGNING_ALGORITHM: Final[str] = "ECDSA_SHA_256"
EVIDENCE_BUCKET: Final[str] = "a-stub-evidence-bucket"
EVIDENCE_PREFIX: Final[str] = "certificates/"
LOCK_DAYS: Final[int] = 1
OBJECT_VERSION: Final[str] = "a-stub-object-version"

# The two credential values the signed ingest presents. Both are composed from
# separately named parts so neither reads as a deployed value, and both name what
# they are for rather than carrying anything shaped like one.
BEARER_PARTS: Final[tuple[str, ...]] = ("a-bearer-value", "the-flow-suite", "presents")
INGRESS_PARTS: Final[tuple[str, ...]] = ("a-shared-ingress-value", "the-flow-suite", "signs-with")
MAX_AGE_SECONDS: Final[int] = 60
MAX_BODY_BYTES: Final[int] = 1048576
TIMEOUT_MS: Final[int] = 30000
OK: Final[int] = 200

# What the ingest batch says about itself, and how many records it carries. More
# than one, so a partial admission would be visible in the sequence numbers.
INGEST_AGENT_CLI: Final[str] = "claude_code"
INGEST_MACHINE_ID: Final[str] = "a-machine-under-test"
INGEST_RECORDS: Final[int] = 3
RECORD_GAP: Final[timedelta] = timedelta(seconds=1)

# The recall page under test: the query, the standings placed either side of the
# floor, and how many results are read.
RECALL_QUERY_TEXT: Final[str] = "the deployment gate that runs before the migration"
RECALL_FLOOR: Final[float] = 0.15
STRONG_CONFIDENCE: Final[float] = 0.90
WEAK_CONFIDENCE: Final[float] = 0.40
BELOW_FLOOR_CONFIDENCE: Final[float] = 0.05
PAGE: Final[int] = 6
DERIVATION_METHOD: Final[str] = "distilled"

# The grid the sensitivity stage answers. Two axes crossed, arranged so two pairs
# describe a band and two describe none, because an inapplicable pair is reported
# rather than skipped and that is worth asserting.
AUTO_AXIS: Final[tuple[float, ...]] = (0.20, 0.60)
REVIEW_AXIS: Final[tuple[float, ...]] = (0.30, 0.45)
ANALYSIS_CANDIDATES: Final[int] = 2

# The run the erasure stage performs, and the backup arrangement it performs it
# under. The target is named here because the configuration surface declares no
# key for it, and no statement reaches a real backup.
REQUESTER: Final[str] = "governance-owner-principal"
JUSTIFICATION: Final[str] = "engagement concluded under a contractual purge obligation"
RUN_OWNER: Final[str] = "the-flow-suite-worker"
BACKUP_TARGET: Final[str] = "an-object-store-backup-target"
CCLOUD_BINARY: Final[str] = "ccloud-stub"
CLUSTER_ID: Final[str] = "cluster-stub"
BACKUP_TIMEOUT_SECONDS: Final[int] = 30
TEXT_PROVIDER_NAME: Final[str] = "stub-text"
TEXT_MODEL: Final[str] = "stub-text-model"
EMBEDDING_PROVIDER_NAME: Final[str] = "stub-embedding"
EMBEDDING_MODEL: Final[str] = "stub-embedding-model"
HORIZON_SECONDS: Final[str] = "4500"
RETENTION: Final[timedelta] = timedelta(days=90)

# A connection is typed loosely because the driver arrives through a fixture
# rather than an import, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# The signing surface, the object surface, and the two stub providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalKey:
    """One locally generated key pair, offered as both signer and retrieval source.

    The same object satisfies the signing shape the checkpoint and the certificate
    reach the key service through, and the retrieval shape the verifier holds,
    because a deployment holds one key and both documents are signed with it. The
    verification call is a local computation over the retrieved public half rather
    than a question asked of a service, which is what makes the reviewer's side of
    this flow independent of the issuer's privileges.
    """

    private: ec.EllipticCurvePrivateKey

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        """Sign an already-computed digest under the named key."""
        assert key_id == SIGNING_KEY_ID, "the flow signs with one key and names it"
        assert algorithm == SIGNING_ALGORITHM, "the delivered key uses one algorithm"
        return self.private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    def public_key(self, *, key_id: str) -> bytes:
        """The public half of the named key, in the encoded form a retrieval returns."""
        assert key_id == SIGNING_KEY_ID, "the emulated key service holds one key"
        return self.private.public_key().public_bytes(
            encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo
        )

    def verify_digest(
        self,
        digest: bytes,
        signature: bytes,
        *,
        key_id: str,
        algorithm: str,
        public_key: bytes,
    ) -> bool:
        """Check a signature locally against the retrieved public half."""
        del key_id
        return verify_signature(digest, signature, public_key=public_key, algorithm=algorithm)


@dataclass(frozen=True, slots=True)
class PlacedObject:
    """One recorded object write, so the retention posture is read rather than assumed."""

    bucket: str
    key: str
    body: bytes
    object_lock_mode: str
    retain_until: datetime


@dataclass(slots=True)
class StubObjectStore:
    """An object store that records what it was asked to write."""

    written: list[PlacedObject] = field(default_factory=list)

    def put_certificate(
        self,
        body: bytes,
        *,
        bucket: str,
        key: str,
        object_lock_mode: str,
        retain_until: datetime,
    ) -> str:
        """Record the write and answer with a version identifier."""
        self.written.append(
            PlacedObject(
                bucket=bucket,
                key=key,
                body=body,
                object_lock_mode=object_lock_mode,
                retain_until=retain_until,
            )
        )
        return OBJECT_VERSION


@dataclass(slots=True)
class StubTextProvider:
    """A Text_Provider answering from a rule rather than from a model.

    The capability fields are settable because the protocol declares them as
    variables, and a frozen shape would satisfy the runtime and not the contract.
    """

    answer: str
    name: str = TEXT_PROVIDER_NAME
    model_id: str = TEXT_MODEL
    supports_prompt_cache: bool = False
    calls: int = 0

    def generate(self, prompt: Prompt) -> TextResult:
        """Answer the prompt with the configured text."""
        assert prompt.stable_prefix, "every prompt this design sends carries a stable prefix"
        self.calls += 1
        return TextResult(
            text=self.answer,
            model_id=self.model_id,
            input_tokens=1,
            output_tokens=1,
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )

    def probe(self) -> ProviderProbe:
        """Report reachability and the cache capability the selector records."""
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            supports_prompt_cache=self.supports_prompt_cache,
        )


@dataclass(slots=True)
class StubEmbeddingProvider:
    """Deterministic vectors from the seed's own local function, at the fixed width."""

    name: str = EMBEDDING_PROVIDER_NAME
    model_id: str = EMBEDDING_MODEL
    dimensions: int = EMBEDDING_DIMENSION

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One unit vector per text, in the input order."""
        embedder = SeedEmbedder()
        return [embedder.embed_one(text) for text in texts]

    def probe(self) -> ProviderProbe:
        """Report reachability and the declared width the startup gate reads."""
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


@dataclass(frozen=True, slots=True)
class QueryEmbedder:
    """The one-call embedding surface the recall path uses.

    The seed's own embedder exposes a batch call and a single call under different
    names, so it is wrapped rather than passed, and the wrapper is what makes the
    recall page rank against exactly the vectors the corpus was placed with.
    """

    embedder: SeedEmbedder

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One unit vector per text, in the input order."""
        return self.embedder.embed(list(texts))


def issued(_statement: str, _parameters: tuple[object, ...]) -> None:
    """A backup statement the cluster accepts, so the primary path succeeds."""


def unreachable_runner(vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
    """A control-plane command nothing should reach, because the primary path succeeds."""
    assert vector and timeout_seconds > 0
    raise AssertionError("the fallback backup path is not entered when the primary one works")


def flow_configuration() -> Configuration:
    """Every number the flow turns on, stated rather than defaulted.

    Stated so a passing run is passing because of the values named here, and a
    changed surface default cannot silently change what is being asserted. The
    lease interval is long enough that the background renewal never races the run.
    """
    return Configuration(
        environ={
            "MOLT_ERASURE_BATCH_SIZE": "8",
            "MOLT_LEASE_INTERVAL_SECONDS": "60",
            "MOLT_AUTO_INCLUDE_THRESHOLD": "0.20",
            "MOLT_REVIEW_THRESHOLD": "0.45",
            "MOLT_RESIDUE_QUERY_LIMIT": "10",
            "MOLT_RESIDUE_TOP_K": "20",
            "MOLT_ADJUDICATION_PREFIX_BUDGET_BYTES": "4096",
            "MOLT_MINIMUM_CACHEABLE_PREFIX_BYTES": "16384",
            "MOLT_REWRITE_LENGTH_RATIO_MIN": "0.25",
            "MOLT_RETENTION_DEFAULT_INTERVAL": "90 days",
            "MOLT_LEASE_OWNER": RUN_OWNER,
            "MOLT_PROCEDURE_RECALL_FLOOR": str(RECALL_FLOOR),
        },
        file_values={},
    )


def collector_configuration() -> Configuration:
    """The request bounds the Collector admits an ingest under."""
    return Configuration(
        environ={
            "MOLT_COLLECTOR_MAX_BODY_BYTES": str(MAX_BODY_BYTES),
            "MOLT_DB_STATEMENT_TIMEOUT_MS": str(TIMEOUT_MS),
            "MOLT_INGRESS_MAX_AGE_SECONDS": str(MAX_AGE_SECONDS),
        },
        file_values={},
    )


def certificate_policy() -> CertificatePolicy:
    """The policy the certificate is issued under, naming no real key and no real bucket."""
    return CertificatePolicy(
        kms_key_id=SIGNING_KEY_ID,
        signing_algorithm=SIGNING_ALGORITHM,
        bucket=EVIDENCE_BUCKET,
        prefix=EVIDENCE_PREFIX,
        object_lock_days=LOCK_DAYS,
    )


def checkpoint_policy() -> CheckpointPolicy:
    """The policy the checkpoint is signed under, with the certificate's own key."""
    return CheckpointPolicy(
        interval_seconds=CHECKPOINT_INTERVAL,
        kms_key_id=SIGNING_KEY_ID,
        signing_algorithm=SIGNING_ALGORITHM,
    )


def joined(parts: Sequence[str]) -> str:
    """One fixture value, composed from separately named parts.

    Composed rather than written whole so that nothing in this module reads as a
    deployed value: each part names what the value is for.
    """
    return "-".join(parts)


def wrapped(value: str, *, source_name: str) -> Credential:
    """One value, wrapped as the credential accessors would hand it over."""
    return Credential(value, source_name=source_name, source=CredentialSource.ENVIRONMENT)


# ---------------------------------------------------------------------------
# The schema, the two stores over it, and the direct reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a writing store, and a read-only store."""

    store: MemoryStore
    reader: MemoryStore
    connection: DriverConnection

    def rows(
        self,
        statement: str,
        params: tuple[object, ...] | None = None,
    ) -> list[tuple[Any, ...]]:
        """Send one statement on this module's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            if cursor.description is None:
                return []
            return list(cursor.fetchall())

    def send(self, statement: str, params: tuple[object, ...] | None = None) -> None:
        """Send one statement whose rows nothing reads."""
        self.rows(statement, params)

    def one(self, statement: str, params: tuple[object, ...] | None = None) -> tuple[Any, ...]:
        """The single row a statement is expected to produce."""
        produced = self.rows(statement, params)
        assert len(produced) == 1, f"the statement produced {len(produced)} rows where one was read"
        return produced[0]

    def count(self, statement: str, params: tuple[object, ...] | None = None) -> int:
        """The number one counting statement reports."""
        return int(self.one(statement, params)[0])

    def now(self) -> datetime:
        """The cluster's own reading, which every instant of the flow is cut from."""
        reading = self.one(CLUSTER_NOW)[0]
        assert isinstance(reading, datetime)
        return reading


@pytest.fixture(scope="module")
def keys() -> LocalKey:
    """One key pair, generated in this process and reaching no service."""
    return LocalKey(private=ec.generate_private_key(ec.SECP256R1()))


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build both stores over this module's own schema.

    Every migration is applied because the flow reaches the lease columns, the
    checkpoint tables, the working tier, and the procedural standing columns, and
    those arrive in later generations than the content tables beside them.

    Two stores rather than one: the flow writes through the first, and the two
    read-only stages run through the second, whose connections authenticate as the
    read-only role. Reading the schema is granted here rather than by a migration,
    because a migration grants on the tables it creates and which namespace a test
    places them in is no migration's business; without it the role is refused at
    the namespace and never reaches the tables its grants name.
    """
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute(CURRENT_SCHEMA)
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])
        cursor.execute(UPSERT_CAPABILITY, (GC_HORIZON_CAPABILITY, True, HORIZON_SECONDS))

    composer = database_driver.sql
    with fresh_schema.cursor() as cursor:
        cursor.execute(
            composer.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                composer.Identifier(schema), composer.Identifier(READER_ROLE)
            )
        )

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    def connect_as_reader() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
            cursor.execute(SET_READER_ROLE_STATEMENT)
        connection: Connection = opened
        return connection

    with (
        MemoryStore(connect_with=connect_with, statement_timeout_ms=TIMEOUT_MS) as store,
        MemoryStore(
            connect_with=connect_as_reader,
            statement_timeout_ms=TIMEOUT_MS,
            role=READER_ROLE,
        ) as reader,
    ):
        yield Cluster(store=store, reader=reader, connection=fresh_schema)


# ---------------------------------------------------------------------------
# What the eight stages produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecallCorpus:
    """Three Learned_Procedures at one distance, placed either side of the floor."""

    session_id: UUID
    client_id: UUID
    strong_id: UUID
    weak_id: UUID
    below_floor_id: UUID


@dataclass(frozen=True, slots=True)
class Attributed:
    """The earliest attribution of one Artifact, as the cluster held it before the run."""

    first_attributed_at: datetime
    first_attribution_method: str


@dataclass(frozen=True, slots=True)
class Flow:
    """Everything the one driven sequence produced, for the assertions to read."""

    seeded: SeedResult
    truth: contaminate.GroundTruth
    erased_id: UUID
    erased_slug: str
    ingest_status: int
    ingest_accepted: int
    ingest_session_id: UUID
    corpus: RecallCorpus
    recalled: tuple[Recalled, ...]
    analysis: sensitivity.SensitivityReport
    checkpoint: StoredCheckpoint
    attributed_before: dict[UUID, Attributed]
    outcome: RunOutcome
    issued: IssuedCertificate
    report: VerificationReport
    payload: dict[str, Any]
    sessions_before: int
    sessions_after: int
    ledger_before: int
    ledger_after: int
    working_before: int
    working_after: int

    @property
    def named_sessions(self) -> list[dict[str, Any]]:
        """The Sessions the certificate names, as the document carries them."""
        listed = self.payload["sessions"]
        assert isinstance(listed, list)
        return [entry for entry in listed if isinstance(entry, dict)]

    def evidence(self) -> str:
        """Everything a failed verification claim has to report, as one block.

        Rendered here rather than at the assertion so one run answers the whole
        question: which checks failed, how many Sessions the document named, what
        each one's recorded and verified tips were, and what the tenant's Session
        and Ledger row counts were on either side of the run.
        """
        subjects = [(entry.check, entry.subject) for entry in self.report.failed_checks]
        lines = [
            f"outcome: {self.report.outcome}",
            f"failed checks: {list(self.report.failed_check_names)}",
            f"failed subjects: {subjects}",
            f"sessions the certificate names: {len(self.named_sessions)}",
            f"tenant session rows before and after: {self.sessions_before}, {self.sessions_after}",
            f"tenant ledger rows before and after: {self.ledger_before}, {self.ledger_after}",
        ]
        lines.extend(
            f"session {entry.session_id}: recorded tip {entry.recorded_tip}, "
            f"verified tip {entry.verified_tip}, rows {entry.rows}, chain ok {entry.ok}"
            for entry in self.report.chains
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The eight stages
# ---------------------------------------------------------------------------


def seed_corpus(cluster: Cluster, mapping_path: Path) -> tuple[SeedResult, contaminate.GroundTruth]:
    """Stage one: generate a corpus and plant the cross-tenant contamination.

    The corpus is dated from a reading behind the cluster's own, because every
    seeded Session sits ahead of the generation instant and a checkpoint window has
    to be able to close over the whole corpus while still preceding the erasure
    run. The mapping is written under a temporary path, so nothing in the tree is
    touched.
    """
    base = cluster.now() - CORPUS_AGE
    seeded = generate(cluster.store, seed=SEED, volumes=VOLUMES, now=base)
    truth = contaminate.plant_contamination(
        cluster.store, seeded, volumes=VOLUMES, path=mapping_path
    )
    return seeded, truth


def ingest_event(session_id: UUID, occurred_at: datetime) -> Event:
    """One well-formed record of the shape the capture side transmits."""
    return Event(
        id=uuid4(),
        session_id=session_id,
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=occurred_at,
        agent_cli=INGEST_AGENT_CLI,
        machine_id=INGEST_MACHINE_ID,
        parent_event_id=None,
        payload={"tool": "a-tool"},
        redacted=False,
        text_body=None,
    )


def signed_ingest(cluster: Cluster) -> tuple[int, int, UUID]:
    """Stage two: admit an Event batch through the Collector's own signed request path.

    No verification seam is injected, so the handler resolves the signature check
    through its own module lookup and reveals the shared value inside it. That is
    the deployed arrangement, and it is the one worth driving here: the point of
    this stage is that a real Ingress_Signature carried real records into the
    cluster, not that a seam was called.
    """
    bearer_value = joined(BEARER_PARTS)
    shared_value = joined(INGRESS_PARTS)
    collector = Collector(
        configuration=collector_configuration(),
        store=cluster.store,
        bearer=wrapped(bearer_value, source_name=COLLECTOR_BEARER_ENV),
        ingress_key=wrapped(shared_value, source_name=INGRESS_KEY_ENV),
    )
    session_id = uuid4()
    reading = datetime.now(tz=UTC)
    body = batch_body(
        tuple(
            ingest_event(session_id, reading + RECORD_GAP * offset)
            for offset in range(INGEST_RECORDS)
        )
    )
    presented = ingress_timestamp(reading)
    answer = collector.serve(
        Invocation(
            method="POST",
            path=EVENTS_PATH,
            headers=Headers(
                {
                    AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {bearer_value}",
                    TIMESTAMP_HEADER: presented,
                    SIGNATURE_HEADER: sign_ingress(body, shared_value, presented),
                }
            ),
            body_text=body.decode("utf-8"),
            base64_encoded=False,
        )
    )
    reported = answer.document.get("accepted")
    accepted = reported if isinstance(reported, int) else -1
    return answer.status, accepted, session_id


def procedure(
    cluster: Cluster,
    *,
    session_id: UUID,
    client_id: UUID,
    confidence: float,
    vector: Sequence[float],
    label: str,
    moment: datetime,
) -> UUID:
    """Place one Learned_Procedure with a vector, a lineage edge, and an attribution."""
    body = f"the procedure distilled as {label}"
    record = DerivedArtifact(
        id=uuid4(),
        kind=DerivedArtifactKind.LEARNED_PROCEDURE,
        owner_client_id=client_id,
        body=body,
        content_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        derivation_method=DERIVATION_METHOD,
        revision=1,
        created_at=moment,
        updated_at=moment,
        redacted_at=None,
        embedding_state=EmbeddingState.EMBEDDED,
        expires_at=moment + RETENTION,
        procedure_confidence=confidence,
    )
    write_derived_artifact(
        cluster.store,
        record,
        embedding=EmbeddingWrite(
            artifact_id=record.id,
            artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=client_id,
            provider=SeedEmbedder().provider,
            model_id=SeedEmbedder().model_id,
            vec=tuple(vector),
            expires_at=moment + RETENTION,
        ),
    )
    insert_lineage_edge(
        cluster.store,
        record.id,
        ParentRef(
            parent_id=session_id,
            parent_kind=ArtifactKind.SESSION,
            derivation_method=DERIVATION_METHOD,
        ),
    )
    cluster.send(
        INSERT_BINDING,
        (
            uuid4(),
            record.id,
            ArtifactKind.DERIVED_ARTIFACT.value,
            client_id,
            moment,
        ),
    )
    return record.id


def place_recall_corpus(cluster: Cluster, seeded: SeedResult, *, client_id: UUID) -> RecallCorpus:
    """Place three Learned_Procedures at one distance from the query vector.

    Confidence is a tie-break on equal distance rather than a multiplier, so the
    only arrangement that shows a standing deciding a position is two procedures
    the ranking cannot separate by distance. All three carry the query's own
    vector, which puts them at distance zero and therefore at the head of the page,
    and the standings are placed either side of the floor so one of the three is
    excluded from the answer while staying inside erasure's reach.
    """
    session = next(item for item in seeded.sessions if item.client_id == client_id)
    vector = SeedEmbedder().embed_one(RECALL_QUERY_TEXT)
    moment = seeded.generated_at
    placed = tuple(
        procedure(
            cluster,
            session_id=session.id,
            client_id=client_id,
            confidence=standing,
            vector=vector,
            label=label,
            moment=moment,
        )
        for standing, label in (
            (STRONG_CONFIDENCE, "the trusted gate"),
            (WEAK_CONFIDENCE, "the doubted gate"),
            (BELOW_FLOOR_CONFIDENCE, "the discredited gate"),
        )
    )
    return RecallCorpus(
        session_id=session.id,
        client_id=client_id,
        strong_id=placed[0],
        weak_id=placed[1],
        below_floor_id=placed[2],
    )


def read_recall_page(cluster: Cluster, corpus: RecallCorpus) -> tuple[Recalled, ...]:
    """Stage three: the recall page, whose tenancy comes from the asking Session's row."""
    engine = RecallEngine(
        cluster.store,
        QueryEmbedder(embedder=SeedEmbedder()),
        recall_floor=RECALL_FLOOR,
    )
    return engine.recall(RECALL_QUERY_TEXT, PAGE, session_id=corpus.session_id)


def analyse_thresholds(
    cluster: Cluster,
    seeded: SeedResult,
    truth: contaminate.GroundTruth,
    *,
    client_id: UUID,
) -> sensitivity.SensitivityReport:
    """Stage four: answer a threshold grid over the read-only role.

    The analyser reads its query Artifacts from a candidate set, so a candidate set
    is placed for it: a closed run of its own and a row per query Artifact, which
    is the synthetic arrangement the analysis path documents, because nothing is
    being erased here.

    The mapping is built rather than loaded. The seed writes its ground truth
    naming a host Event and an owning tenant's slug, and the analyser's own mapping
    names an Artifact and an owning tenant's identifier, so the slugs are resolved
    through the generation's own tenants and the pairs are handed over directly.
    """
    request_id = uuid4()
    run_id = uuid4()
    cluster.send(INSERT_ANALYSIS_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION))
    cluster.send(INSERT_ANALYSIS_RUN, (run_id, request_id, client_id, REQUESTER))
    queries = tuple(
        event_id
        for session in seeded.sessions
        if session.client_id == client_id
        for event_id in session.text_event_ids
    )[:ANALYSIS_CANDIDATES]
    assert len(queries) == ANALYSIS_CANDIDATES, "the analysis needs query Artifacts to search from"
    for artifact_id in queries:
        cluster.send(INSERT_ANALYSIS_CANDIDATE, (run_id, artifact_id))

    mapping = {
        fragment.host_event_id: seeded.client_of(fragment.owner_client_slug).id
        for fragment in truth.fragments
    }
    return sensitivity.analyse_client(
        cluster.reader,
        run_id,
        permitted_clients=[client.id for client in seeded.clients],
        configuration=flow_configuration(),
        grid=sensitivity.ThresholdGrid.from_axes(AUTO_AXIS, REVIEW_AXIS),
        ground_truth=sensitivity.GroundTruth.from_mapping(mapping),
    )


def take_flow_checkpoint(cluster: Cluster, keys: LocalKey) -> StoredCheckpoint:
    """Stage five: a signed Ledger_Checkpoint whose window closes before the run opens.

    The certificate names the most recent checkpoint whose window ended at or
    before the instant the run began reading, so the boundaries are cut from the
    cluster's own reading rather than from this process's, and the window is taken
    before the erasure so that the checkpoint the certificate names exists.
    """
    closing = cluster.now()
    window = CheckpointWindow(start=closing - CHECKPOINT_LEAD, end=closing)
    return sign_and_store(
        cluster.store,
        compute(cluster.store, window),
        signer=keys,
        policy=checkpoint_policy(),
    )


def attributions_before(cluster: Cluster, client_id: UUID) -> dict[UUID, Attributed]:
    """Read the earliest attribution of every Artifact the tenant currently holds.

    Read on this module's own connection rather than through the surface the
    certificate is assembled with, so the comparison later is against the rows
    rather than against a second call of the same function. It is read before the
    run because a hard delete removes the rows it is read from, which is the whole
    reason the snapshot exists.
    """
    found: dict[UUID, Attributed] = {}
    for row in cluster.rows(CURRENT_BOUND_ARTIFACTS, (client_id,)):
        artifact_id = row[0]
        assert isinstance(artifact_id, UUID)
        moment, method = cluster.one(FIRST_ATTRIBUTION_ROW, (client_id, artifact_id))
        assert isinstance(moment, datetime)
        found[artifact_id] = Attributed(
            first_attributed_at=moment,
            first_attribution_method=str(method),
        )
    return found


def erase_tenant(
    cluster: Cluster,
    *,
    client_id: UUID,
    supersession_session_id: UUID,
) -> RunOutcome:
    """Stage six: the leased erasure, which acquires its own lease and holds its own fence."""
    seams = EngineSeams(
        configuration=flow_configuration(),
        backup=BackupSettings(
            target=BACKUP_TARGET,
            ccloud_binary=CCLOUD_BINARY,
            cluster_id=CLUSTER_ID,
            timeout_seconds=BACKUP_TIMEOUT_SECONDS,
        ),
        capabilities=CapabilityRecord(),
        supersession=SupersessionContext(
            session_id=supersession_session_id,
            agent_cli=INGEST_AGENT_CLI,
            machine_id=INGEST_MACHINE_ID,
            expires_at=datetime.now(tz=UTC) + RETENTION,
        ),
        text_provider=StubTextProvider(answer="the rewritten body the flow suite answers with"),
        embedding_provider=StubEmbeddingProvider(),
        issuer=issued,
        runner=unreachable_runner,
        progress=None,
        owner=RUN_OWNER,
    )
    request = ErasureRequest(
        client_id=client_id,
        requester=REQUESTER,
        justification=JUSTIFICATION,
        idempotency_key=uuid4().hex,
        dry_run=False,
    )
    return run_erasure(cluster.store, request, seams)


# ---------------------------------------------------------------------------
# The one driven sequence
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def flow(
    cluster: Cluster,
    keys: LocalKey,
    tmp_path_factory: pytest.TempPathFactory,
) -> Flow:
    """Drive the eight stages once, in order, and hand back what they produced.

    One fixture rather than one per stage, because the stages are one history: the
    checkpoint has to precede the run, the attribution snapshot has to precede the
    disposition phase, and the verification has to follow the issue. Re-running the
    sequence per assertion would assert eight different histories instead of one.
    """
    seeded, truth = seed_corpus(cluster, tmp_path_factory.mktemp("ground-truth") / "mapping.json")
    erased = seeded.clients[0]
    retained = seeded.clients[1]

    status, accepted, ingest_session_id = signed_ingest(cluster)

    corpus = place_recall_corpus(cluster, seeded, client_id=retained.id)
    recalled = read_recall_page(cluster, corpus)

    analysis = analyse_thresholds(cluster, seeded, truth, client_id=erased.id)

    checkpoint = take_flow_checkpoint(cluster, keys)

    attributed = attributions_before(cluster, erased.id)
    sessions_before = cluster.count(COUNT_TENANT_SESSIONS, (erased.id,))
    ledger_before = cluster.count(COUNT_TENANT_LEDGER, (erased.id,))
    working_before = cluster.count(COUNT_TENANT_WORKING, (erased.id,))
    snapshot = first_attribution_snapshot(cluster.store, erased.id, sorted(attributed, key=str))

    outcome = erase_tenant(
        cluster,
        client_id=erased.id,
        supersession_session_id=corpus.session_id,
    )
    assert outcome.run_id is not None, "a completed run records the row its evidence hangs off"
    sessions_after = cluster.count(COUNT_TENANT_SESSIONS, (erased.id,))
    ledger_after = cluster.count(COUNT_TENANT_LEDGER, (erased.id,))
    working_after = cluster.count(COUNT_TENANT_WORKING, (erased.id,))

    object_store = StubObjectStore()
    certificate = issue(
        cluster.store,
        outcome.run_id,
        signer=keys,
        object_store=object_store,
        policy=certificate_policy(),
        attributions=snapshot,
    )
    assert len(object_store.written) == 1, "one run writes one certificate object"
    written = object_store.written[0]
    assert (written.bucket, written.key) == (EVIDENCE_BUCKET, certificate.object_key)
    assert written.object_lock_mode
    assert written.retain_until > cluster.now()
    raw = written.body
    report = verify_certificate(parse_envelope(raw), store=cluster.reader, keys=keys)
    document = json.loads(raw.decode("utf-8"))
    payload = document["payload"]
    assert isinstance(payload, dict)

    return Flow(
        seeded=seeded,
        truth=truth,
        erased_id=erased.id,
        erased_slug=erased.slug,
        ingest_status=status,
        ingest_accepted=accepted,
        ingest_session_id=ingest_session_id,
        corpus=corpus,
        recalled=recalled,
        analysis=analysis,
        checkpoint=checkpoint,
        attributed_before=attributed,
        outcome=outcome,
        issued=certificate,
        report=report,
        payload=payload,
        sessions_before=sessions_before,
        sessions_after=sessions_after,
        ledger_before=ledger_before,
        ledger_after=ledger_after,
        working_before=working_before,
        working_after=working_after,
    )


# ---------------------------------------------------------------------------
# The stages, each asserted against what the cluster holds
# ---------------------------------------------------------------------------


def test_the_seeded_corpus_and_its_contamination_are_in_the_cluster(flow: Flow) -> None:
    """The generation reached its volumes and every planted fragment sits in a row."""
    assert len(flow.seeded.clients) == VOLUMES.clients
    assert flow.seeded.events >= VOLUMES.sessions
    assert flow.seeded.embeddings > 0
    assert flow.seeded.blended_artifacts, "no seeded Artifact bound more than one tenant"
    assert len(flow.truth.fragments) == VOLUMES.planted_fragments
    for fragment in flow.truth.fragments:
        assert fragment.owner_client_slug != fragment.host_client_slug


def test_the_signed_ingest_request_was_admitted(cluster: Cluster, flow: Flow) -> None:
    """A real Ingress_Signature carried the batch through the deployed request path.

    The response is read alongside the rows, because a status alone would be
    satisfied by a request that was accepted and persisted nothing.
    """
    assert flow.ingest_status == OK
    assert flow.ingest_accepted == INGEST_RECORDS
    assert cluster.count(COUNT_SESSION_LEDGER, (flow.ingest_session_id,)) == INGEST_RECORDS
    assert cluster.count(COUNT_SESSION_ROW, (flow.ingest_session_id,)) == 1


def test_the_recall_page_is_decided_by_standing_where_distance_cannot_decide(
    cluster: Cluster,
    flow: Flow,
) -> None:
    """Two procedures the ranking cannot separate are separated by standing, higher first.

    And the third, below the floor, is absent from the page while its row stays in
    the cluster: exclusion from recall is not a soft delete, so erasure still has to
    reach it.
    """
    ranked = [result.artifact_id for result in flow.recalled]

    assert flow.corpus.strong_id in ranked
    assert flow.corpus.weak_id in ranked
    assert ranked.index(flow.corpus.strong_id) < ranked.index(flow.corpus.weak_id)
    assert flow.corpus.below_floor_id not in ranked
    assert cluster.count(COUNT_ARTIFACT, (flow.corpus.below_floor_id,)) == 1
    distances = [result.distance for result in flow.recalled]
    assert distances == sorted(distances)
    standings = {result.artifact_id: result.confidence for result in flow.recalled}
    assert standings[flow.corpus.strong_id] == pytest.approx(STRONG_CONFIDENCE)
    assert standings[flow.corpus.weak_id] == pytest.approx(WEAK_CONFIDENCE)


def test_every_grid_pair_is_answered_or_reported_as_inapplicable(flow: Flow) -> None:
    """The grid stays rectangular: an inapplicable pair carries a reason, not a blank."""
    report = flow.analysis
    pairs = len(AUTO_AXIS) * len(REVIEW_AXIS)

    assert len(report.outcomes) == pairs
    assert report.ground_truth_available is True
    assert report.query_artifact_ids, "the analysis searched from no query Artifact"
    assert report.searched_at == pytest.approx(max(REVIEW_AXIS))
    for outcome in report.outcomes:
        if outcome.applicable:
            assert outcome.candidate_count is not None
            assert outcome.auto_included_count is not None
            assert outcome.referred_count is not None
            assert outcome.auto_included_count + outcome.referred_count == outcome.candidate_count
        else:
            assert outcome.inapplicable_reason == sensitivity.INAPPLICABLE_REASON
            assert outcome.candidate_count is None
    assert report.inapplicable_outcomes, "the grid was arranged to hold an inapplicable pair"


def test_the_leased_run_completed_and_admits_a_certificate(flow: Flow) -> None:
    """The engine took its own lease, recorded a generation, and finished."""
    assert flow.outcome.completed is True
    assert flow.outcome.certificate_admissible is True
    assert flow.outcome.generation is not None
    assert flow.outcome.generation >= 1


# ---------------------------------------------------------------------------
# The certificate, and the four claims about it
# ---------------------------------------------------------------------------


def test_the_issued_certificate_verifies_against_the_erased_cluster(flow: Flow) -> None:
    """The auditor's whole workflow: an independent verification reports verified.

    The verifier holds a public key and a read-only connection and nothing else,
    and it checks the signature, the embedded queries, the derived counts, every
    named Session's chain, the named checkpoint, and the disposition claims. Any one
    of those failing is a certificate a reviewer would be right to reject, so the
    assertion is on the outcome rather than on a subset of the checks.
    """
    assert flow.issued.stored is True
    assert flow.payload["client"] == {
        "client_id": str(flow.erased_id),
        "slug": flow.erased_slug,
    }
    assert flow.report.verified is True, flow.evidence()
    assert flow.report.failed_checks == (), flow.evidence()


def test_the_counts_are_confirmed_through_the_derived_mechanism(flow: Flow) -> None:
    """The agreement is re-derived from append-only rows, so the horizon cannot decide it.

    The before-count and the after-count are the Dispositions naming the tenant plus
    the attribution standing now, both of which outlive the cluster's collection
    horizon. The point-in-time read is corroboration beside that: whether it was
    attempted, and whatever it found, the counts still agree. The assertion says so
    by asserting the agreement unconditionally and only then reading the
    corroboration, which is deliberately not required to have run.
    """
    counts = flow.report.counts
    corroboration = flow.report.corroboration
    assert counts is not None
    assert corroboration is not None

    assert counts.mechanism == DERIVED_MECHANISM
    assert flow.payload["counts"]["count_derivation"] == COUNT_DERIVATION
    assert COUNT_DERIVATION == DERIVED_MECHANISM, (
        "the builder and the verifier name one mechanism, or neither confirms the other"
    )
    # Unconditional: the derived figures agree whatever the corroboration did.
    assert counts.before_agrees is True
    assert counts.after_agrees is True
    assert counts.derived_after == 0, "no attribution of the erased tenant stands"

    assert corroboration.within_horizon is True
    assert isinstance(corroboration.attempted, bool)
    if corroboration.attempted:
        assert isinstance(corroboration.agrees, bool)


def test_the_ownership_block_agrees_with_the_lease_the_run_held(
    cluster: Cluster,
    flow: Flow,
) -> None:
    """The fencing generation on the document is the generation the lease row records."""
    assert flow.outcome.run_id is not None
    generation, key, owner, lease_generation = cluster.one(
        RUN_OWNERSHIP_ROW, (flow.outcome.run_id,)
    )
    ownership = flow.payload["ownership"]

    assert ownership["fencing_generation"] == str(generation)
    assert ownership["fencing_generation"] == str(lease_generation)
    assert ownership["owner"] == owner
    assert ownership["idempotency_key"] == key
    assert flow.outcome.generation == generation


def test_the_named_checkpoint_is_the_stored_one_preceding_the_run(
    cluster: Cluster,
    flow: Flow,
) -> None:
    """The document names a checkpoint row, and it is the latest one closing before the run."""
    block = flow.payload["ledger_checkpoint"]
    assert block is not None, "the run followed a checkpoint, so the block is not null"
    digest, covered, key_id, algorithm = cluster.one(
        CHECKPOINT_ROW, (flow.checkpoint.checkpoint_id,)
    )

    assert block["checkpoint_id"] == str(flow.checkpoint.checkpoint_id)
    assert block["root_digest"] == digest
    assert block["covered_session_count"] == str(covered)
    assert (key_id, algorithm) == (SIGNING_KEY_ID, SIGNING_ALGORITHM)
    assert int(covered) > 0, "a checkpoint covering no Session would commit to nothing"
    assert block["window_end"] is not None

    t_before = datetime.fromisoformat(str(flow.payload["run"]["t_before"]))
    latest = latest_before(cluster.reader, t_before)
    assert latest is not None
    assert latest.checkpoint_id == flow.checkpoint.checkpoint_id, (
        "the certificate names the checkpoint a verifier extends integrity from"
    )
    assert flow.checkpoint.window.end <= t_before


def test_the_first_attribution_pair_is_what_the_cluster_held_before_the_run(flow: Flow) -> None:
    """The certificate states when the tenant's content began being held, from stored rows.

    The comparison is against a read taken on this module's own connection before
    the run, because for a hard-deleted Artifact that is the only moment the rows
    exist. At least one Disposition has to be covered, so a snapshot that came back
    empty would fail here rather than pass with nothing compared.
    """
    compared = 0
    for entry in flow.payload["dispositions"]:
        held = flow.attributed_before.get(UUID(entry["artifact_id"]))
        if held is None:
            continue
        compared += 1
        stated = entry["first_attributed_at"]
        assert stated is not None, "an Artifact the tenant held carries its first attribution"
        assert datetime.fromisoformat(stated) == held.first_attributed_at
        assert entry["first_attribution_method"] == held.first_attribution_method
    assert compared > 0, "no Disposition named an Artifact the tenant was recorded as holding"


def test_the_working_row_count_is_the_tenants_own(cluster: Cluster, flow: Flow) -> None:
    """One aggregate number, and it is the number of rows the tenant held and no longer does."""
    assert flow.outcome.run_id is not None
    recorded = cluster.count(RUN_WORKING_ROWS, (flow.outcome.run_id,))

    assert flow.working_before > 0, "the seeded corpus placed working rows for this tenant"
    assert flow.working_after == 0, "the working tier of an erased tenant holds nothing"
    assert recorded == flow.working_before
    assert flow.payload["run"]["working_rows_deleted"] == str(recorded)
    assert flow.outcome.working_rows_deleted == recorded
