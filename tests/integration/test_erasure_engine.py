"""The run skeleton against a live instance: what a run leaves behind, and what it does not.

The unit surface can assert the order of the phases. Only a cluster can answer the
five claims this module is about, and every one of them is a claim about stored rows.

**A run that holds no lease mutates nothing.** Ownership is contended before the
first statement that would write anything, so a refused acquisition leaves the run
table, the candidate table, and every content table exactly as they were.

**A backup failure ends the run before any memory content is touched.** The run row
exists and says why it stopped, and no Session, Event, Artifact, edge, binding, or
vector moved.

**A dry run writes its evidence and mutates no memory content.** Candidates,
residue findings, and dispositions are all recorded, and every content table is
byte-for-byte what it was.

**A completed run records the after-instant, the finalising generation, and the
working-row count.** The three together are what a certificate's before-and-after
claim and its ownership claim are read from.

**A repeated run under one idempotency key returns the recorded result and mutates
nothing.** The second call performs no phase at all, which is asserted by counting
rows rather than by trusting the returned value.

**Validates: Requirements 15.2, 15.3, 18.9, 18.10, 18.11, 19.3, 42.13, 44.5, 44.10, 44.12**
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.backup import BackupSettings, CommandResult, StatementIssuer
from molt.config.resolve import Configuration
from molt.erase.engine import (
    EngineSeams,
    ErasureRequest,
    Phase,
    PhaseProgress,
    RunStatus,
    run_erasure,
)
from molt.erase.lease import acquire
from molt.errors import BackupFailedError, LeaseNotHeldError, ModelUnavailableError
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind, DerivedArtifactKind
from molt.providers import Prompt, ProviderProbe, TextResult
from molt.store import Connection, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.store.capability import CapabilityRecord
from molt.store.embeddings import vector_text
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places. The engine owns no Client insert and no Artifact
# insert, so everything a run acts on is placed here.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, content_markers) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
INSERT_EVENT: Final[str] = (
    "INSERT INTO ledger ("
    "id, session_id, client_id, seq, category, agent_cli, machine_id, payload, text_body, "
    "content_digest, prev_chain_digest, chain_digest, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::JSONB, %s, %s, %s, %s, "
    "now() + INTERVAL '90 days')"
)
INSERT_ARTIFACT: Final[str] = (
    "INSERT INTO derived_artifact ("
    "id, kind, owner_client_id, body, content_digest, derivation_method, embedding_state, "
    "expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, 'embedded', now() + INTERVAL '90 days')"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, confidence) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
INSERT_EMBEDDING: Final[str] = (
    "INSERT INTO embedding ("
    "artifact_id, artifact_kind, client_id, provider, model_id, dimension, normalised, vec, "
    "expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, true, %s::VECTOR, now() + INTERVAL '90 days')"
)
INSERT_WORKING: Final[str] = (
    "INSERT INTO working_memory (session_id, client_id, scratch_key, value, expires_at) "
    "VALUES (%s, %s, %s, %s::JSONB, now() + INTERVAL '1 hour')"
)

# What every claim about stored rows is read from. The content tally is one
# statement over every table a run could mutate, so *nothing moved* is one number
# rather than a list of numbers a reader has to compare pairwise.
COUNT_CONTENT: Final[str] = (
    "SELECT (SELECT count(*) FROM session) + (SELECT count(*) FROM ledger) "
    "+ (SELECT count(*) FROM derived_artifact) + (SELECT count(*) FROM lineage_edge) "
    "+ (SELECT count(*) FROM client_binding) + (SELECT count(*) FROM embedding) "
    "+ (SELECT count(*) FROM working_memory)"
)
DIGEST_OF_CONTENT: Final[str] = (
    "SELECT coalesce(string_agg(line, '|' ORDER BY line), '') FROM ("
    "SELECT id::STRING || body || content_digest || revision::STRING AS line "
    "FROM derived_artifact "
    "UNION ALL SELECT id::STRING || coalesce(text_body, '') FROM ledger "
    "UNION ALL SELECT id::STRING || client_id::STRING || coalesce(superseded_by::STRING, '') "
    "FROM client_binding"
    ") AS shape"
)
COUNT_RUNS: Final[str] = "SELECT count(*) FROM erasure_run"
COUNT_CANDIDATES: Final[str] = "SELECT count(*) FROM erasure_candidate WHERE run_id = %s"
COUNT_DISPOSITIONS: Final[str] = "SELECT count(*) FROM disposition WHERE run_id = %s"
COUNT_BACKUP_RECORDS: Final[str] = "SELECT count(*) FROM backup_record WHERE run_id = %s"
READ_RUN: Final[str] = (
    "SELECT status, phase, t_before, t_after, working_rows_deleted, fencing_generation, "
    "finalised_at, finalisation_result, error_detail, dry_run "
    "FROM erasure_run WHERE id = %s"
)

# The values the placed rows carry. The two markers are distinct enough that a
# substring of one is not a substring of the other, which the rewrite validation
# reads.
ERASED_MARKER: Final[str] = "ACME-INTERNAL"
RETAINED_MARKER: Final[str] = "ZENITH-INTERNAL"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"
DERIVATION_METHOD: Final[str] = "distilled"
REQUESTER: Final[str] = "governance-owner"
JUSTIFICATION: Final[str] = "engagement concluded under a contractual purge obligation"
OTHER_OWNER: Final[str] = "another-worker"
RUN_OWNER: Final[str] = "this-worker"
TEXT_PROVIDER_NAME: Final[str] = "stub-text"
TEXT_MODEL: Final[str] = "stub-text-model"
EMBEDDING_PROVIDER_NAME: Final[str] = "stub-embedding"
EMBEDDING_MODEL: Final[str] = "stub-embedding-model"
BACKUP_TARGET: Final[str] = "s3://operator-owned-bucket/molt"
CCLOUD_BINARY: Final[str] = "ccloud-stub"
CLUSTER_ID: Final[str] = "cluster-stub"

# How long a placed row is retained for, as a duration rather than a stated instant.
RETENTION: Final[timedelta] = timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


@dataclass(slots=True)
class StubTextProvider:
    """A Text_Provider that answers from a rule rather than from a model.

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
    """Deterministic stub vectors: one axis per text, chosen by the text's own digest.

    Deterministic rather than random, so a distance an example depends on is a
    property of the example rather than of a seed, and unit-norm because the schema
    admits no other.
    """

    name: str = EMBEDDING_PROVIDER_NAME
    model_id: str = EMBEDDING_MODEL
    dimensions: int = EMBEDDING_DIMENSION

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One unit vector per text, in the input order."""
        return [stub_vector(text) for text in texts]

    def probe(self) -> ProviderProbe:
        """Report reachability and the declared width the startup gate reads."""
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


@dataclass(slots=True)
class Recorder:
    """Where the progress callback's reports are collected for an example to read."""

    seen: list[PhaseProgress] = field(default_factory=list)

    def __call__(self, progress: PhaseProgress) -> None:
        """Collect one report."""
        self.seen.append(progress)

    @property
    def phases(self) -> list[Phase]:
        """The phases reported, in the order they were reported."""
        return [progress.phase for progress in self.seen]


@dataclass(frozen=True, slots=True)
class Fixture:
    """One tenant pair, a Session, and the identifiers an example asserts against."""

    erased_id: UUID
    erased_slug: str
    retained_id: UUID
    retained_slug: str
    session_id: UUID


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, and a store over it."""

    store: MemoryStore
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

    @property
    def content(self) -> tuple[int, str]:
        """How many content rows stand, and the shape of the ones that carry text.

        Both together, because a count alone would admit a run that deleted one row
        and wrote another, and a digest alone would admit a run that deleted a row
        carrying nothing this statement projects.
        """
        return self.count(COUNT_CONTENT), str(self.one(DIGEST_OF_CONTENT)[0])

    # -- placed rows ------------------------------------------------------

    def client(self, marker: str) -> tuple[UUID, str]:
        """Place one Client carrying one content marker, and report it with its slug."""
        identifier = uuid4()
        slug = f"tenant-{identifier.hex[:12]}"
        self.send(INSERT_CLIENT, (identifier, slug, f"Tenant {slug}", [marker]))
        return identifier, slug

    def session(self, client_id: UUID) -> UUID:
        """Place one Session of a Client and return its identifier."""
        identifier = uuid4()
        self.send(INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
        return identifier

    def event(self, session_id: UUID, client_id: UUID, seq: int, body: str) -> UUID:
        """Place one Event carrying text, with a chain digest nothing verifies here."""
        identifier = uuid4()
        digest = hashlib.sha256(identifier.bytes).hexdigest()
        previous = hashlib.sha256(f"{seq}".encode()).hexdigest()
        self.send(
            INSERT_EVENT,
            (
                identifier,
                session_id,
                client_id,
                seq,
                "assistant_response",
                AGENT_CLI,
                MACHINE_ID,
                body,
                digest,
                previous,
                digest,
            ),
        )
        return identifier

    def artifact(self, owner_client_id: UUID, body: str) -> UUID:
        """Place one Derived_Artifact with a stub vector already recorded for it."""
        identifier = uuid4()
        self.send(
            INSERT_ARTIFACT,
            (
                identifier,
                DerivedArtifactKind.SUMMARY.value,
                owner_client_id,
                body,
                body_digest(body),
                DERIVATION_METHOD,
            ),
        )
        self.send(
            INSERT_EMBEDDING,
            (
                identifier,
                ArtifactKind.DERIVED_ARTIFACT.value,
                owner_client_id,
                EMBEDDING_PROVIDER_NAME,
                EMBEDDING_MODEL,
                EMBEDDING_DIMENSION,
                vector_text(stub_vector(body)),
            ),
        )
        return identifier

    def bind(self, artifact_id: UUID, kind: ArtifactKind, client_id: UUID) -> None:
        """Place one current Attribution_Version for an Artifact and a Client."""
        self.send(INSERT_BINDING, (uuid4(), artifact_id, kind.value, client_id, "marker", 0.9))

    def working(self, session_id: UUID, client_id: UUID, key: str) -> None:
        """Place one Working_Memory row, which a run erases as one set."""
        self.send(INSERT_WORKING, (session_id, client_id, key, '{"held": true}'))

    def fixture(self) -> Fixture:
        """Two tenants and a Session of the retained one."""
        erased_id, erased_slug = self.client(ERASED_MARKER)
        retained_id, retained_slug = self.client(RETAINED_MARKER)
        return Fixture(
            erased_id=erased_id,
            erased_slug=erased_slug,
            retained_id=retained_id,
            retained_slug=retained_slug,
            session_id=self.session(retained_id),
        )

    def corpus(self, fixture: Fixture) -> tuple[UUID, UUID]:
        """One Artifact the erased tenant holds alone, and one two tenants share."""
        sole_body = f"{ERASED_MARKER} the exporter reconciles the invoice ledger nightly"
        sole_id = self.artifact(fixture.erased_id, sole_body)
        self.bind(sole_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)

        blended_id = self.artifact(fixture.retained_id, blended_body())
        self.bind(blended_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
        self.bind(blended_id, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
        return sole_id, blended_id


def stub_vector(text: str) -> tuple[float, ...]:
    """A unit vector on one axis, chosen by the text's digest so it is deterministic."""
    axis = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:2], "big")
    place = axis % EMBEDDING_DIMENSION
    return tuple(1.0 if index == place else 0.0 for index in range(EMBEDDING_DIMENSION))


def body_digest(text: str) -> str:
    """The digest a Derived_Artifact row records for a body."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def blended_body() -> str:
    """A body carrying one line per tenant, so a rewrite has something to remove."""
    return (
        f"{RETAINED_MARKER} the deployment pipeline runs the migration gate first\n"
        f"{ERASED_MARKER} the invoicing exporter reconciles against the ledger\n"
        f"{RETAINED_MARKER} the recall path reads the covering index rather than the row\n"
    )


def redacted_body() -> str:
    """What a well-behaved rewrite answers with: the erased line gone, the rest intact."""
    return (
        f"{RETAINED_MARKER} the deployment pipeline runs the migration gate first\n"
        f"{RETAINED_MARKER} the recall path reads the covering index rather than the row\n"
    )


def example_configuration() -> Configuration:
    """A configuration naming every number an example turns on, none of them defaults.

    Stated rather than defaulted so that a passing example is passing because of the
    values it names, and a changed surface default cannot silently change what is
    being asserted.
    """
    return Configuration(
        environ={
            "MOLT_ERASURE_BATCH_SIZE": "2",
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
            "MOLT_PROCEDURE_RECALL_FLOOR": "0.15",
        },
        file_values={},
    )


def backup_settings() -> BackupSettings:
    """The backup settings an example runs under.

    The target is supplied here rather than read from a key, because the
    configuration surface declares none for it yet.
    """
    return BackupSettings(
        target=BACKUP_TARGET,
        ccloud_binary=CCLOUD_BINARY,
        cluster_id=CLUSTER_ID,
        timeout_seconds=30,
    )


def issued(_statement: str, _parameters: tuple[object, ...]) -> None:
    """A backup statement that is accepted, so the primary path succeeds."""


def refused(_statement: str, _parameters: tuple[object, ...]) -> None:
    """A backup statement the cluster refuses, so no path succeeds."""
    raise RuntimeError("the stub cluster refuses this backup")


def unreachable_runner(vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
    """A control-plane command that answers nothing, which no example should reach."""
    assert vector and timeout_seconds > 0
    raise AssertionError("the fallback path is not entered when the primary path is available")


def seams(
    fixture: Fixture,
    *,
    issuer: StatementIssuer = issued,
    text: StubTextProvider | None = None,
    progress: Recorder | None = None,
) -> EngineSeams:
    """The seams an example runs under: stub provider, stub vectors, no credentials."""
    return EngineSeams(
        configuration=example_configuration(),
        backup=backup_settings(),
        capabilities=CapabilityRecord(),
        supersession=SupersessionContext(
            session_id=fixture.session_id,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=datetime.now(UTC) + RETENTION,
        ),
        text_provider=text if text is not None else StubTextProvider(answer=redacted_body()),
        embedding_provider=StubEmbeddingProvider(),
        issuer=issuer,
        runner=unreachable_runner,
        progress=progress,
        owner=RUN_OWNER,
    )


def request_for(fixture: Fixture, *, dry_run: bool = False) -> ErasureRequest:
    """One erasure request for the erased tenant, under a key of its own."""
    return ErasureRequest(
        client_id=fixture.erased_id,
        requester=REQUESTER,
        justification=JUSTIFICATION,
        idempotency_key=uuid4().hex,
        dry_run=dry_run,
    )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema.

    Every migration is applied because the lease table, the fencing columns, the
    working-row counter, and the restricting evidence references all arrive in later
    generations than the tables a run acts on.
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


# ---------------------------------------------------------------------------
# No lease, no run
# ---------------------------------------------------------------------------


def test_a_run_holding_no_lease_mutates_nothing_and_reports_the_current_owner(
    cluster: Cluster,
) -> None:
    """Ownership is contended before the first mutation, so a refusal leaves no trace."""
    fixture = cluster.fixture()
    cluster.corpus(fixture)
    held = acquire(cluster.store, fixture.erased_id, OTHER_OWNER, uuid4().hex)
    before = cluster.content
    runs_before = cluster.count(COUNT_RUNS)

    with pytest.raises(LeaseNotHeldError) as refusal:
        run_erasure(cluster.store, request_for(fixture), seams(fixture))

    assert OTHER_OWNER in str(refusal.value), "the refusal names the owner that holds erasure"
    assert str(held.generation) in str(refusal.value)
    assert cluster.content == before, "no content row moved"
    assert cluster.count(COUNT_RUNS) == runs_before, "and no run row was opened"


# ---------------------------------------------------------------------------
# The backup gate
# ---------------------------------------------------------------------------


def test_a_backup_failure_aborts_before_any_memory_content_is_touched(cluster: Cluster) -> None:
    """No backup evidence means no deletion, and the run row says where it stopped."""
    fixture = cluster.fixture()
    cluster.corpus(fixture)
    before = cluster.content

    with pytest.raises(BackupFailedError):
        run_erasure(cluster.store, request_for(fixture), seams(fixture, issuer=refused))

    assert cluster.content == before, "every content table is unchanged"
    aborted = cluster.rows(
        "SELECT id, status, phase, error_detail FROM erasure_run WHERE client_id = %s",
        (fixture.erased_id,),
    )
    assert len(aborted) == 1, "the run row exists and says what happened"
    assert aborted[0][1] == RunStatus.ABORTED.value
    assert aborted[0][2] == Phase.SWEEP.value, "the phase reached is recorded"
    assert aborted[0][3], "and so is the detail"
    assert cluster.count(COUNT_CANDIDATES, (aborted[0][0],)) == 0
    assert cluster.count(COUNT_DISPOSITIONS, (aborted[0][0],)) == 0
    assert cluster.count(COUNT_BACKUP_RECORDS, (aborted[0][0],)) == 1, (
        "the failed attempt is itself recorded as evidence"
    )


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


def test_a_dry_run_writes_run_scoped_evidence_and_mutates_no_memory_content(
    cluster: Cluster,
) -> None:
    """Candidates and dispositions are recorded; not one content row is touched."""
    fixture = cluster.fixture()
    cluster.corpus(fixture)
    cluster.working(fixture.session_id, fixture.erased_id, "held-key")
    before = cluster.content
    recorder = Recorder()

    outcome = run_erasure(
        cluster.store,
        request_for(fixture, dry_run=True),
        seams(fixture, progress=recorder),
    )

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.dry_run is True
    assert outcome.certificate_admissible is False, "a dry run certifies nothing"
    assert outcome.run_id is not None
    assert outcome.working_rows_deleted == 0, "the working delete is skipped entirely"
    assert cluster.count(COUNT_CANDIDATES, (outcome.run_id,)) > 0, "the evidence is written"
    assert cluster.count(COUNT_DISPOSITIONS, (outcome.run_id,)) > 0
    assert cluster.content == before, "and no memory content moved"
    assert Phase.RESIDUE in recorder.phases, "progress is reported phase by phase"
    assert Phase.CERTIFICATE in recorder.phases


# ---------------------------------------------------------------------------
# The completed run
# ---------------------------------------------------------------------------


def test_a_completed_run_records_t_after_the_generation_and_the_working_count(
    cluster: Cluster,
) -> None:
    """The three claims a certificate is read from are all on the run row."""
    fixture = cluster.fixture()
    sole_id, blended_id = cluster.corpus(fixture)
    cluster.working(fixture.session_id, fixture.erased_id, "first-key")
    cluster.working(fixture.session_id, fixture.erased_id, "second-key")

    outcome = run_erasure(cluster.store, request_for(fixture), seams(fixture))

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.run_id is not None
    assert outcome.generation is not None and outcome.generation >= 1
    assert outcome.working_rows_deleted == 2, "one aggregate number, not one row each"
    recorded = cluster.one(READ_RUN, (outcome.run_id,))
    assert recorded[0] == RunStatus.COMPLETED.value
    assert recorded[1] == Phase.CERTIFICATE.value
    assert recorded[3] is not None, "t_after is the cluster's own reading"
    assert recorded[2] < recorded[3], "and it follows t_before"
    assert int(recorded[4]) == 2
    assert int(recorded[5]) == outcome.generation, "the finalising generation is on the row"
    assert recorded[6] is not None, "the attempt is marked finalised"
    assert recorded[7] is not None, "and the outcome a repeat returns is recorded"
    assert cluster.count(COUNT_DISPOSITIONS, (outcome.run_id,)) > 0
    assert cluster.count("SELECT count(*) FROM derived_artifact WHERE id = %s", (sole_id,)) == 0, (
        "the sole-bound artifact is gone"
    )
    assert (
        cluster.count("SELECT count(*) FROM derived_artifact WHERE id = %s", (blended_id,)) == 1
    ), "and the blended one survives"
    assert (
        cluster.count(
            "SELECT count(*) FROM client_binding WHERE artifact_id = %s AND client_id = %s "
            "AND superseded_by IS NULL",
            (blended_id, fixture.erased_id),
        )
        == 0
    ), "with the erased tenant's claim closed"


# ---------------------------------------------------------------------------
# The repeated attempt
# ---------------------------------------------------------------------------


def test_a_repeated_run_under_one_key_returns_the_recorded_result_and_mutates_nothing(
    cluster: Cluster,
) -> None:
    """The second call performs no phase at all and answers with what the first recorded."""
    fixture = cluster.fixture()
    cluster.corpus(fixture)
    request = request_for(fixture)

    first = run_erasure(cluster.store, request, seams(fixture))
    settled = cluster.content
    runs = cluster.count(COUNT_RUNS)

    repeated = run_erasure(cluster.store, request, seams(fixture))

    assert repeated.replayed is True
    assert repeated.finalisation is not None
    assert first.finalisation is not None
    assert repeated.finalisation == first.finalisation, "the recorded result comes back unchanged"
    assert repeated.run_id == first.run_id
    assert cluster.count(COUNT_RUNS) == runs, "no second run row was opened"
    assert cluster.content == settled, "and nothing was mutated"


def test_a_rewrite_that_cannot_be_used_falls_to_the_hard_delete(cluster: Cluster) -> None:
    """The fail-closed bias: blended memory is lost rather than erased content left behind."""
    fixture = cluster.fixture()
    _, blended_id = cluster.corpus(fixture)
    unusable = StubTextProvider(answer=f"{RETAINED_MARKER} and {ERASED_MARKER} both remain")

    outcome = run_erasure(cluster.store, request_for(fixture), seams(fixture, text=unusable))

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.fail_closed_rewrites == 1
    assert (
        cluster.count("SELECT count(*) FROM derived_artifact WHERE id = %s", (blended_id,)) == 0
    ), "an unusable rewrite deletes the artifact rather than leaving the content"
    assert unusable.calls >= 1, "and the provider was actually asked"


def test_a_model_that_refuses_still_leaves_the_run_completed(cluster: Cluster) -> None:
    """Provider unavailability is a fail-closed delete, not a failed run."""
    fixture = cluster.fixture()
    cluster.corpus(fixture)

    class Refusing(StubTextProvider):
        """A provider that answers nothing at all."""

        def generate(self, _prompt: Prompt) -> TextResult:
            """Refuse as an unreachable provider would."""
            raise ModelUnavailableError("the stub provider is unreachable")

    outcome = run_erasure(
        cluster.store,
        request_for(fixture),
        seams(fixture, text=Refusing(answer="")),
    )

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.fail_closed_rewrites == 1
