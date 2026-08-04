"""Property 20: erasing a Client twice is erasing it once.

**Validates: Requirements 16.2, 16.3, 16.4, 16.5, 16.6, 18.1**

The first run of an erasure is where the interesting work happens. The second run
is where the interesting failures live. A sweep that selected by a predicate the
first run did not fully settle would select again; a disposition path that deleted
by identifier rather than by current binding would delete a survivor; a working
tier counted rather than emptied would report a second count. Every one of those
is invisible from the first run alone and obvious from the second.

Four decisions shape what is generated and what is asserted.

**The second run is a genuinely new run, not a replay.** It carries an
idempotency key of its own, so the engine takes the lease again, opens a run row
again, and performs every phase again. The replayed-attempt path — same key, same
recorded result — is a different claim tested elsewhere; asserting idempotence
through it would assert that the engine can recognise a key, which is not what
this property is about.

**Idempotence is asserted against a digest of every memory-content table.** Each
of the seven tables a run may mutate is hashed whole, row by row and column by
column, by rendering each row to its canonical object form and hashing the sorted
rendering. That is what makes "changes no memory-content row" checkable in the
strong sense: a closed binding, a bumped revision, a re-inserted embedding, or a
deleted lineage edge all move the bytes of some table, whichever column carries
them.

**The touched-Artifact list is read from the second run's own evidence.** A
Disposition naming anything other than a retention is a record that the run
mutated that Artifact, so the list of them is the run's own answer to what it
touched, read back with a statement of this module's own rather than taken from a
returned count. The returned counts are asserted beside it, so a run whose tally
and whose evidence disagree fails rather than passing on whichever of the two the
property happened to read.

**Every example makes the first run do something.** At least one Artifact the
erased tenant is solely bound to is always placed, and the first run is asserted
to have deleted it, so the second run's emptiness is emptiness after a real
erasure rather than after a run that selected nothing.

The example budget is deliberately small: each example places a corpus and
performs two full passes over it against a live instance, and the corpus varies in
how many rows each table holds rather than in which branch of the pass is taken.
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
# object rendering, so a column no projection names is still covered, and the rows
# are ordered by their own rendering so the digest does not depend on the order
# the cluster happened to return them in.
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
# mutates content in these seven and nowhere else, so this mapping is what "no
# memory-content row changed" means for the second run.
CONTENT_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("session", DIGEST_SESSION),
    ("ledger", DIGEST_LEDGER),
    ("derived_artifact", DIGEST_DERIVED_ARTIFACT),
    ("lineage_edge", DIGEST_LINEAGE_EDGE),
    ("client_binding", DIGEST_CLIENT_BINDING),
    ("embedding", DIGEST_EMBEDDING),
    ("working_memory", DIGEST_WORKING_MEMORY),
)

# The value a Disposition carries for an Artifact a run decided to leave alone.
# Every other value records a mutation, which is what makes the statement below
# the run's own list of the Artifacts it touched.
RETAINED_DISPOSITION: Final[str] = "retained"

# The evidence this module reads back. The touched list is the second run's own
# record of what it mutated, read with a statement of this module's own rather
# than taken from a returned count.
SELECT_TOUCHED: Final[str] = (
    "SELECT artifact_id, disposition FROM disposition "
    "WHERE run_id = %s AND disposition <> %s ORDER BY artifact_id ASC"
)
SELECT_CANDIDATE_IDS: Final[str] = (
    "SELECT artifact_id FROM erasure_candidate WHERE run_id = %s ORDER BY artifact_id ASC"
)
COUNT_CURRENT_BINDINGS: Final[str] = (
    "SELECT count(*) FROM client_binding WHERE client_id = %s AND superseded_by IS NULL"
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
# The stubs the runs reach the world through
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubTextProvider:
    """A Text_Provider that answers from a rule rather than from a model.

    The call count is what makes the second run's emptiness legible: a run with no
    blended Artifact left to rewrite reaches the provider for nothing.
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

    The counts vary rather than the shapes, because the property is about what a
    second run finds left: a tenant whose content spanned several tables and one
    whose content spanned few both have to leave the second run nothing to do.
    """

    sole_artifacts: int
    blended: bool
    erased_events: int
    retained_events: int
    working_rows: int
    linked: bool


def corpora() -> st.SearchStrategy[Corpus]:
    """Draw a corpus that places rows in each of the seven memory-content tables.

    At least one solely-bound Artifact is always placed, so the first run always
    has something to delete and the second run's emptiness is emptiness after a
    real erasure. The blended Artifact is a flag rather than a count because one
    is enough to reach the surgical branch, and the stub rewriter answers for one
    body.
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
        """Place one Working_Memory row, which the first run erases as one set."""
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
    the working-row counter arrive in later generations than the tables a run
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
    """The seams a run runs under: stub provider, stub vectors, no credentials."""
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


def request_for(placed: Placed) -> ErasureRequest:
    """One erasure request for the erased tenant, under a key of its own.

    The key is fresh on every call, which is what makes the second run a second
    run rather than a replay of the first: the engine recognises a repeated key
    and returns the recorded result without performing a phase, and that path
    would make the idempotence clause a statement about key recognition instead
    of about the phases.
    """
    return ErasureRequest(
        client_id=placed.erased_id,
        requester=REQUESTER,
        justification=JUSTIFICATION,
        idempotency_key=uuid4().hex,
    )


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


def differing(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    """The memory-content tables whose bytes moved between two readings."""
    return tuple(table for table, _ in CONTENT_TABLES if before[table] != after[table])


def candidates(cluster: Cluster, run_id: UUID) -> frozenset[UUID]:
    """Every Artifact one run's sweep and residue pass selected."""
    return frozenset(
        row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))
        for row in cluster.rows(SELECT_CANDIDATE_IDS, (run_id,))
    )


def touched(cluster: Cluster, run_id: UUID) -> tuple[tuple[UUID, str], ...]:
    """The Artifacts one run's own evidence says it mutated, and how.

    A Disposition recording a retention is a decision to leave an Artifact alone,
    so it is excluded here: everything that remains is a record that the run
    changed the Artifact it names.
    """
    return tuple(
        (row[0] if isinstance(row[0], UUID) else UUID(str(row[0])), str(row[1]))
        for row in cluster.rows(SELECT_TOUCHED, (run_id, RETAINED_DISPOSITION))
    )


# Feature: molt, Property 20: For any Client and any corpus, a second Erasure_Run
# performed for that same Client after a completed first run changes no
# memory-content row and produces an empty touched-Artifact list.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(corpus=corpora())
def test_a_second_run_for_the_same_client_changes_nothing_and_touches_nothing(
    cluster: Cluster, corpus: Corpus
) -> None:
    placed = cluster.place(corpus)
    text = StubTextProvider(answer=redacted_body())

    event(f"solely bound artifacts={corpus.sole_artifacts}")
    event(f"blended artifact placed={corpus.blended}")
    event(f"erased events={corpus.erased_events}")
    event(f"retained events={corpus.retained_events}")
    event(f"working rows={corpus.working_rows}")
    event(f"lineage edge placed={corpus.linked}")

    first = run_erasure(cluster.store, request_for(placed), seams(placed, text))
    settled = cluster.digests()

    # Requirements 16.2 to 16.6 and 18.1: the first run really erased, so the
    # second run's emptiness is emptiness after an erasure rather than after a run
    # that selected nothing. Both the tally and the evidence are read, because a
    # run whose two accounts disagree is a run neither of them describes.
    assert first.status is RunStatus.COMPLETED
    assert first.run_id is not None
    assert first.deleted >= MIN_SOLE_ARTIFACTS, (
        "the first run deleted nothing, so nothing was erased to be idempotent about"
    )
    assert len(touched(cluster, first.run_id)) == first.deleted + first.redacted, (
        "the first run's tally and its own dispositions disagree about what it touched"
    )
    assert cluster.count(COUNT_CURRENT_BINDINGS, (placed.erased_id,)) == 0, (
        "the erased tenant still holds a current binding after the first run"
    )
    event(f"first run deleted={first.deleted}")
    event(f"first run redacted={first.redacted}")
    event(f"first run working rows deleted={first.working_rows_deleted}")

    second = run_erasure(cluster.store, request_for(placed), seams(placed, text))
    after_second = cluster.digests()

    # The second run is a run, not a recognised repeat: it took the lease again,
    # opened a run row of its own, and performed every phase.
    assert second.replayed is False, "the second call was answered from the recorded result"
    assert second.status is RunStatus.COMPLETED
    assert second.run_id is not None and second.run_id != first.run_id
    assert second.generation is not None and first.generation is not None
    assert second.generation > first.generation, (
        "the second run did not take ownership again, so it performed no phase"
    )

    # Requirement 18.1: the touched-Artifact list of the second run is empty. Read
    # from its own evidence, so a run that mutated an Artifact without recording it
    # fails the digest clause below and a run that recorded one without mutating it
    # fails here.
    assert touched(cluster, second.run_id) == (), (
        f"the second run touched {touched(cluster, second.run_id)}"
    )
    assert second.deleted == 0, "the second run deleted an Artifact the first had already erased"
    assert second.redacted == 0, "the second run rewrote an Artifact a second time"
    assert second.fail_closed_rewrites == 0, "the second run fell closed on a rewrite"
    assert second.working_rows_deleted == 0, "the second run deleted a working row again"

    # The sweep is scoped by the tenant as well as by its current bindings, so a
    # second run legitimately selects the tenant's surviving Sessions again. What
    # it may not do is select anything the first run already disposed of, which is
    # the selection-side half of idempotence.
    assert not candidates(cluster, second.run_id) & {
        artifact_id for artifact_id, _ in touched(cluster, first.run_id)
    }, "the second run selected an Artifact the first run had already disposed of"

    # Requirements 16.2 to 16.6: and no memory-content row changed, in any of the
    # seven tables a run may mutate, so the second run cost the retained tenant
    # nothing either.
    assert differing(settled, after_second) == (), (
        f"the second run mutated {differing(settled, after_second)}"
    )
