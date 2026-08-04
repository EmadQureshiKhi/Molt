"""Property 19: a dry run and the residue verb both read memory content and write none of it.

**Validates: Requirements 17.9, 18.11**

Two verbs promise the same thing from two directions. A dry run performs every
phase an erasure performs and records every piece of run-scoped evidence a real
run records, but mutates no memory content. The residue verb runs the same
semantic walk the erasure phase runs, with recording suppressed, and mutates
nothing at all. Both promises are about absence, and absence is the one thing a
return value cannot demonstrate.

Four decisions shape what is generated and what is asserted.

**The claim is asserted against a digest of every memory-content table, not a
count.** A count admits a pass that deleted one row and wrote another, and a
digest of the tables that carry text admits a pass that closed a binding or
bumped a revision in a table the projection skipped. So each of the seven content
tables is hashed whole, row by row and column by column, by rendering each row to
its canonical object form and hashing the sorted rendering. Two digests agree
exactly when the bytes of the table agree, whichever column moved.

**Both verbs are exercised over the same corpus in one example.** The dry run
comes first, because it is what writes the candidate rows the residue verb then
reads its query Artifacts from, and running them in that order is what makes the
residue invocation a real walk over a real candidate set rather than a walk over
nothing. The digest is taken three times — before, between, and after — so a
mutation is attributed to the verb that made it rather than to the pair.

**Every example plants content in all seven tables and asserts the dry run had
something to do.** A pass that mutates nothing because it selected nothing would
satisfy the digest clause vacuously, so each example asserts the dry run wrote
candidate rows and dispositions of its own, and each example places content the
erased tenant is solely bound to, which a real run would delete outright.

**No provider is reached for text and no cluster is reached for a backup.** The
rewrite provider, the embedding provider, the backup statement, and the
control-plane command are all injected stubs, so a dry run costs a schema and a
handful of rows rather than a model call. The stub providers record their calls,
which is how the residue verb's promise to adjudicate nothing is observable.

The example budget is deliberately small. Each example applies no migration but
does place a corpus and perform a full pass over it against a live instance, so
the cost per example is a round trip per placed row plus one pass, and the value
of a hundred examples over twenty-five here is small: the corpus varies in how
many rows each table holds rather than in which branch of the pass is taken.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.backup import BackupSettings, CommandResult
from molt.config.resolve import Configuration
from molt.erase.engine import EngineSeams, ErasureRequest, RunStatus, run_erasure
from molt.erase.residue import ResiduePolicy, residue_report
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind, DerivedArtifactKind
from molt.providers import Prompt, ProviderProbe, TextResult
from molt.store import Connection, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.store.capability import CapabilityRecord
from molt.store.embeddings import vector_text
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# How many examples the property runs, and the bounds of one placed corpus. The
# reasoning behind the budget is in the module docstring.
MAX_EXAMPLES: Final[int] = 25
MIN_SOLE_ARTIFACTS: Final[int] = 1
MAX_SOLE_ARTIFACTS: Final[int] = 2
MIN_EVENTS: Final[int] = 0
MAX_EVENTS: Final[int] = 2
MIN_WORKING_ROWS: Final[int] = 0
MAX_WORKING_ROWS: Final[int] = 2

# The first sequence number a Session's Ledger admits, which the stored check
# fixes above zero.
FIRST_SEQ: Final[int] = 1

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places. The engine owns no tenant insert and no Artifact
# insert, so everything a pass acts on is placed here.
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
INSERT_EDGE: Final[str] = (
    "INSERT INTO lineage_edge (id, child_id, parent_id, parent_kind, derivation_method) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_WORKING: Final[str] = (
    "INSERT INTO working_memory (session_id, client_id, scratch_key, value, expires_at) "
    "VALUES (%s, %s, %s, %s::JSONB, now() + INTERVAL '1 hour')"
)

# One digest statement per memory-content table, each a whole literal of its own.
# Every column of every row travels into the hash by way of the row's canonical
# object rendering, so a column no projection names is still covered, and the
# rows are ordered by their own rendering so the digest does not depend on the
# order the cluster happened to return them in.
DIGEST_SESSION: Final[str] = (
    "SELECT coalesce(string_agg(shape, '|' ORDER BY shape), '') FROM ("
    "SELECT to_jsonb(t)::STRING AS shape FROM session AS t) AS rendered"
)
DIGEST_LEDGER: Final[str] = (
    "SELECT coalesce(string_agg(shape, '|' ORDER BY shape), '') FROM ("
    "SELECT to_jsonb(t)::STRING AS shape FROM ledger AS t) AS rendered"
)
DIGEST_DERIVED_ARTIFACT: Final[str] = (
    "SELECT coalesce(string_agg(shape, '|' ORDER BY shape), '') FROM ("
    "SELECT to_jsonb(t)::STRING AS shape FROM derived_artifact AS t) AS rendered"
)
DIGEST_LINEAGE_EDGE: Final[str] = (
    "SELECT coalesce(string_agg(shape, '|' ORDER BY shape), '') FROM ("
    "SELECT to_jsonb(t)::STRING AS shape FROM lineage_edge AS t) AS rendered"
)
DIGEST_CLIENT_BINDING: Final[str] = (
    "SELECT coalesce(string_agg(shape, '|' ORDER BY shape), '') FROM ("
    "SELECT to_jsonb(t)::STRING AS shape FROM client_binding AS t) AS rendered"
)
DIGEST_EMBEDDING: Final[str] = (
    "SELECT coalesce(string_agg(shape, '|' ORDER BY shape), '') FROM ("
    "SELECT to_jsonb(t)::STRING AS shape FROM embedding AS t) AS rendered"
)
DIGEST_WORKING_MEMORY: Final[str] = (
    "SELECT coalesce(string_agg(shape, '|' ORDER BY shape), '') FROM ("
    "SELECT to_jsonb(t)::STRING AS shape FROM working_memory AS t) AS rendered"
)

# Every memory-content table, paired with the statement that renders it. A run
# mutates content in these seven and nowhere else, so this mapping is what "every
# memory-content table" means for both verbs under test.
CONTENT_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("session", DIGEST_SESSION),
    ("ledger", DIGEST_LEDGER),
    ("derived_artifact", DIGEST_DERIVED_ARTIFACT),
    ("lineage_edge", DIGEST_LINEAGE_EDGE),
    ("client_binding", DIGEST_CLIENT_BINDING),
    ("embedding", DIGEST_EMBEDDING),
    ("working_memory", DIGEST_WORKING_MEMORY),
)

# The run-scoped evidence a dry run is asserted to write, and the residue rows
# the read-only verb is asserted to leave alone.
COUNT_CANDIDATES: Final[str] = "SELECT count(*) FROM erasure_candidate WHERE run_id = %s"
COUNT_DISPOSITIONS: Final[str] = "SELECT count(*) FROM disposition WHERE run_id = %s"
COUNT_RESIDUE: Final[str] = "SELECT count(*) FROM residue_candidate WHERE run_id = %s"

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
RUN_OWNER: Final[str] = "this-worker"
TEXT_PROVIDER_NAME: Final[str] = "stub-text"
TEXT_MODEL: Final[str] = "stub-text-model"
EMBEDDING_PROVIDER_NAME: Final[str] = "stub-embedding"
EMBEDDING_MODEL: Final[str] = "stub-embedding-model"
BACKUP_TARGET: Final[str] = "s3://operator-owned-bucket.invalid/molt"
CCLOUD_BINARY: Final[str] = "ccloud-stub"
CLUSTER_ID: Final[str] = "cluster-stub"

# How long a placed row is retained for, as a duration rather than a stated instant.
RETENTION: Final[timedelta] = timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


# ---------------------------------------------------------------------------
# The stubs the pass reaches the world through
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubTextProvider:
    """A Text_Provider that answers from a rule rather than from a model.

    The call count is what makes the residue verb's promise observable: a
    read-only walk with no Adjudicator wired reaches no provider at all, and a
    dry run reaches it only where a blended Artifact was placed.
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
    """Deterministic stub vectors: one axis per text, chosen by the text's own digest."""

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


def issued(_statement: str, _parameters: tuple[object, ...]) -> None:
    """A backup statement that is accepted, so the primary path succeeds."""


def unreachable_runner(vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
    """A control-plane command that answers nothing, which no example should reach."""
    assert vector and timeout_seconds > 0
    raise AssertionError("the fallback path is not entered when the primary path is available")


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
    """A configuration naming every number an example turns on, none of them defaults."""
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
    """The backup settings an example runs under, naming a target that resolves nowhere."""
    return BackupSettings(
        target=BACKUP_TARGET,
        ccloud_binary=CCLOUD_BINARY,
        cluster_id=CLUSTER_ID,
        timeout_seconds=30,
    )


# ---------------------------------------------------------------------------
# What one drawn corpus is
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Corpus:
    """How many rows of each shape one example places.

    The counts vary rather than the shapes, because the property is about a pass
    that touches nothing whatever it was given: a table holding no row and a table
    holding several both have to come back byte-identical.
    """

    sole_artifacts: int
    blended: bool
    erased_events: int
    retained_events: int
    working_rows: int
    linked: bool


def corpora() -> st.SearchStrategy[Corpus]:
    """Draw a corpus that places rows in each of the seven memory-content tables.

    At least one solely-bound Artifact is always placed, so a real run would
    always have had something to delete and the purity clause is never satisfied
    by a pass that selected nothing. The blended Artifact is a flag rather than a
    count because one is enough to reach the rewrite branch, and the stub rewriter
    answers for one body.
    """
    return st.builds(
        Corpus,
        st.integers(min_value=MIN_SOLE_ARTIFACTS, max_value=MAX_SOLE_ARTIFACTS),
        st.booleans(),
        st.integers(min_value=MIN_EVENTS, max_value=MAX_EVENTS),
        st.integers(min_value=MIN_EVENTS, max_value=MAX_EVENTS),
        st.integers(min_value=MIN_WORKING_ROWS, max_value=MAX_WORKING_ROWS),
        st.booleans(),
    )


@dataclass(frozen=True, slots=True)
class Placed:
    """The tenants, the Sessions, and the Artifacts one example placed."""

    erased_id: UUID
    retained_id: UUID
    erased_session_id: UUID
    retained_session_id: UUID
    sole_ids: tuple[UUID, ...]
    blended_id: UUID | None


# ---------------------------------------------------------------------------
# The cluster the corpus is placed on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the reads of this module."""

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

    def digests(self) -> dict[str, str]:
        """One digest per memory-content table, hashed over every column of every row.

        The rendering is hashed here rather than on the cluster so that what a
        comparison compares is a fixed-width value this module computed, and a
        failure names the table that moved rather than a wall of rendered rows.
        """
        taken: dict[str, str] = {}
        for table, statement in CONTENT_TABLES:
            rendered = str(self.one(statement)[0])
            taken[table] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        return taken

    # -- placed rows ------------------------------------------------------

    def client(self, marker: str) -> UUID:
        """Place one Client carrying one content marker."""
        identifier = uuid4()
        slug = f"tenant-{identifier.hex[:12]}"
        self.send(INSERT_CLIENT, (identifier, slug, f"Tenant {slug}", [marker]))
        return identifier

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

    def bind(self, artifact_id: UUID, client_id: UUID) -> None:
        """Place one current Attribution_Version for an Artifact and a Client."""
        self.send(
            INSERT_BINDING,
            (
                uuid4(),
                artifact_id,
                ArtifactKind.DERIVED_ARTIFACT.value,
                client_id,
                "marker",
                0.9,
            ),
        )

    def edge(self, child_id: UUID, parent_id: UUID) -> None:
        """Place one lineage edge between two placed Artifacts."""
        self.send(
            INSERT_EDGE,
            (
                uuid4(),
                child_id,
                parent_id,
                ArtifactKind.DERIVED_ARTIFACT.value,
                DERIVATION_METHOD,
            ),
        )

    def working(self, session_id: UUID, client_id: UUID, key: str) -> None:
        """Place one Working_Memory row, which a real run erases as one set."""
        self.send(INSERT_WORKING, (session_id, client_id, key, '{"held": true}'))

    def place(self, corpus: Corpus) -> Placed:
        """Place one drawn corpus, spread across every memory-content table."""
        erased_id = self.client(ERASED_MARKER)
        retained_id = self.client(RETAINED_MARKER)
        erased_session_id = self.session(erased_id)
        retained_session_id = self.session(retained_id)

        for ordinal in range(corpus.erased_events):
            self.event(
                erased_session_id,
                erased_id,
                ordinal + FIRST_SEQ,
                f"{ERASED_MARKER} the exporter reconciled batch {ordinal}",
            )
        for ordinal in range(corpus.retained_events):
            self.event(
                retained_session_id,
                retained_id,
                ordinal + FIRST_SEQ,
                f"{RETAINED_MARKER} the pipeline promoted build {ordinal}",
            )

        sole_ids: list[UUID] = []
        for ordinal in range(corpus.sole_artifacts):
            body = f"{ERASED_MARKER} the exporter reconciles the invoice ledger nightly {ordinal}"
            artifact_id = self.artifact(erased_id, body)
            self.bind(artifact_id, erased_id)
            sole_ids.append(artifact_id)

        blended_id: UUID | None = None
        if corpus.blended:
            blended_id = self.artifact(retained_id, blended_body())
            self.bind(blended_id, erased_id)
            self.bind(blended_id, retained_id)

        if corpus.linked:
            child = blended_id if blended_id is not None else sole_ids[-1]
            parent = sole_ids[0]
            if child != parent:
                self.edge(child, parent)

        for ordinal in range(corpus.working_rows):
            self.working(erased_session_id, erased_id, f"held-key-{ordinal}")

        return Placed(
            erased_id=erased_id,
            retained_id=retained_id,
            erased_session_id=erased_session_id,
            retained_session_id=retained_session_id,
            sole_ids=tuple(sole_ids),
            blended_id=blended_id,
        )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema.

    Every migration is applied because the lease table, the fencing columns, and
    the working-row counter arrive in later generations than the tables a pass
    acts on. Module scope keeps the schema cost paid once: examples are isolated
    from each other by a tenant pair of their own rather than by a schema.
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


def seams(placed: Placed, text: StubTextProvider) -> EngineSeams:
    """The seams a pass runs under: stub provider, stub vectors, no credentials."""
    return EngineSeams(
        configuration=example_configuration(),
        backup=backup_settings(),
        capabilities=CapabilityRecord(),
        supersession=SupersessionContext(
            session_id=placed.retained_session_id,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=datetime.now(UTC) + RETENTION,
        ),
        text_provider=text,
        embedding_provider=StubEmbeddingProvider(),
        issuer=issued,
        runner=unreachable_runner,
        owner=RUN_OWNER,
    )


def dry_run_request(placed: Placed) -> ErasureRequest:
    """One dry-run erasure request for the erased tenant, under a key of its own."""
    return ErasureRequest(
        client_id=placed.erased_id,
        requester=REQUESTER,
        justification=JUSTIFICATION,
        idempotency_key=uuid4().hex,
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


def differing(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    """The memory-content tables whose bytes moved between two readings."""
    return tuple(table for table, _ in CONTENT_TABLES if before[table] != after[table])


# Feature: molt, Property 19: For any corpus and any Client, a dry-run Erasure_Run
# and a residue-verb invocation each leave every memory-content table byte-identical
# to what it was before, while the dry run still writes its run-scoped evidence and
# the residue verb writes nothing at all.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(corpus=corpora())
def test_a_dry_run_and_the_residue_verb_leave_every_content_table_identical(
    cluster: Cluster, corpus: Corpus
) -> None:
    placed = cluster.place(corpus)
    text = StubTextProvider(answer=redacted_body())
    before = cluster.digests()

    event(f"solely bound artifacts={corpus.sole_artifacts}")
    event(f"blended artifact placed={corpus.blended}")
    event(f"erased events={corpus.erased_events}")
    event(f"retained events={corpus.retained_events}")
    event(f"working rows={corpus.working_rows}")
    event(f"lineage edge placed={corpus.linked}")

    outcome = run_erasure(cluster.store, dry_run_request(placed), seams(placed, text))
    after_dry_run = cluster.digests()

    # Requirement 18.11: the dry run performed every phase and recorded the
    # evidence a real run records, which is what makes the purity clause below a
    # statement about a pass that did something rather than about a pass that
    # declined to start.
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.dry_run is True
    assert outcome.certificate_admissible is False, "a dry run certifies nothing"
    run_id = outcome.run_id
    assert run_id is not None
    assert cluster.count(COUNT_CANDIDATES, (run_id,)) > 0, (
        "the dry run selected nothing, so its purity would hold for the wrong reason"
    )
    assert cluster.count(COUNT_DISPOSITIONS, (run_id,)) > 0, (
        "the dry run recorded no disposition, so it decided nothing about the corpus"
    )
    assert outcome.working_rows_deleted == 0, "the working delete is skipped entirely"

    # Requirement 18.11: and not one memory-content row moved, in any of the seven
    # tables a real run mutates.
    assert differing(before, after_dry_run) == (), (
        f"the dry run mutated {differing(before, after_dry_run)}"
    )

    # Requirement 17.9: the residue verb is the same walk with recording
    # suppressed, run over the candidate set the dry run recorded, so the corpus it
    # reads is a real one.
    residue_rows = cluster.count(COUNT_RESIDUE, (run_id,))
    calls_before_residue = text.calls
    report = residue_report(
        cluster.store,
        run_id,
        ResiduePolicy.from_configuration(example_configuration()),
        permitted_clients=[placed.erased_id, placed.retained_id],
    )
    after_residue = cluster.digests()

    assert report.read_only is True, "the residue verb ran a pass that was free to record"
    assert report.query_artifact_ids, (
        "the residue verb ranked no query artifact, so it read nothing to be pure about"
    )
    assert differing(after_dry_run, after_residue) == (), (
        f"the residue verb mutated {differing(after_dry_run, after_residue)}"
    )
    assert cluster.count(COUNT_RESIDUE, (run_id,)) == residue_rows, (
        "the read-only walk recorded a residue candidate of its own"
    )
    assert text.calls == calls_before_residue, (
        "the read-only walk reached the text provider, which a pass with no "
        "adjudicator wired may not do"
    )

    # And the whole pair together: what stood before either verb ran stands after
    # both of them did, table by table.
    assert differing(before, after_residue) == (), (
        f"the pair of verbs together mutated {differing(before, after_residue)}"
    )
