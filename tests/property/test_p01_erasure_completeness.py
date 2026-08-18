"""Property 1: an erasure run leaves nothing of the erased Client and explains everything.

**Validates: Requirements 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 18.1, 18.4, 10.9**

Completeness is the one claim of an erasure that cannot be established in this
process. Every phase of the sweep is a set-based statement whose whole meaning is
which rows a cluster's own planner reaches: a recursive lineage walk, a partial
index over unsuperseded attribution versions, an anti-join against a candidate set.
A stub standing in for any of those would be asserting that the stub agrees with
itself. So this property runs against a live instance, and the three clauses it
carries are all read back out of stored rows.

Four decisions shape it.

**The graph is generated and placed, the run is the shipped one.** `memory_graphs()`
draws the shape the design names — several Clients, Sessions with spawn trees,
Events across the categories, Derived_Artifacts with parents and derivation depth,
a fraction of them Learned_Procedures whose Procedure_Confidence straddles the
recall floor, Attribution_Versions that are current and Attribution_Versions that
were superseded away, and a fraction of Artifacts left in the pending embedding
state — and every row of it is inserted before `run_erasure` is called with no
phase replaced.

**No provider is reached and no cluster is backed up.** Vectors are deterministic
functions of an Artifact's own text, so residue distances are a property of the
example rather than of a model. The Text_Provider answers rewrites from a table
this module built while it placed the bodies, so a well-behaved rewrite is
computable and a body the table does not know falls closed — which is the shipped
behaviour rather than an error. The backup statement issuer accepts, and the
control-plane runner is never reached.

**The clauses are asserted against three different tables.** That no current
Attribution_Version of the erased Client remains is read from `client_binding`
under the same unsuperseded predicate the sweep selects by. That every swept
candidate carries exactly one Disposition with a known selection reason is read by
joining `erasure_candidate` to `disposition` and counting, so a candidate disposed
twice fails as loudly as one disposed not at all. That a pending-embedding Artifact
is selected like any other is read by comparing the candidate set against the rows
still in that state, and against the aggregate the sweep recorded on the run row.

**Each example starts from an empty namespace.** The schema is truncated between
examples rather than reused, because the residue phase searches the whole fleet and
a hundred examples' accumulated rows would make the last example a different
workload from the first.

The example budget is deliberately small. Every example places a graph, acquires a
lease, and drives a full run through several transactions against a real instance,
so the cost per example is round trips rather than arithmetic, and the shrinking a
larger budget would buy is not worth minutes of wall clock.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from molt.backup import BackupSettings, CommandResult
from molt.config.resolve import Configuration
from molt.erase.engine import (
    SEMANTIC_RESIDUE_REASON,
    EngineSeams,
    ErasureRequest,
    RunStatus,
    run_erasure,
)
from molt.erase.sweep import SWEEP_REASONS
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind, DerivedArtifactKind
from molt.models.event import EmbeddingState
from molt.providers import Prompt, ProviderProbe, TextResult
from molt.store import Connection, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.store.capability import CapabilityRecord
from molt.store.embeddings import vector_text
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.instance

# How many graphs the property is asserted over. Small on purpose: an example is a
# whole run against a live instance rather than a call into this process.
MAX_EXAMPLES: Final[int] = 25

# The bounds of one generated graph. Below the design's own bounds, because the
# design's bounds describe the generator's shape and a live run pays for every row
# in round trips; every structural feature the property depends on — a spawn tree,
# a derivation chain, a blended Artifact, a superseded version — is still reachable
# inside these.
MIN_CLIENTS: Final[int] = 2
MAX_CLIENTS: Final[int] = 3
MIN_SESSIONS: Final[int] = 1
MAX_SESSIONS: Final[int] = 3
MAX_EVENTS_PER_SESSION: Final[int] = 3
MAX_ARTIFACTS: Final[int] = 4
MAX_PARENTS: Final[int] = 2
MAX_WORKING_ROWS: Final[int] = 2

# The Client whose content is erased. Always the first, so a failing example names
# the same tenant on a replay and every other Client is a retained one.
ERASED_ORDINAL: Final[int] = 0

# The content marker each generated Client carries. No marker is a substring of
# another, which the rewrite validation reads: a body that lost one marker must not
# be read as having lost a different one.
MARKERS: Final[tuple[str, ...]] = ("ALPHA-INTERNAL", "BRAVO-INTERNAL", "CHARLIE-INTERNAL")

# The Event categories a generated Event is drawn from, one per broad class the
# schema admits, so an example is not a run over one category repeated.
CATEGORIES: Final[tuple[str, ...]] = (
    "user_prompt",
    "assistant_response",
    "tool_call",
    "file_write",
    "decision",
)

# The Derived_Artifact kinds a generated Artifact is drawn from. The procedure kind
# is present because the sweep reaches a below-floor procedure by a statement of its
# own, and that statement is only exercised when one is placed.
ARTIFACT_KINDS: Final[tuple[str, ...]] = (
    DerivedArtifactKind.SUMMARY.value,
    DerivedArtifactKind.BEHAVIORAL_BASELINE.value,
    DerivedArtifactKind.LEARNED_PROCEDURE.value,
)

# The embedding states a generated Artifact is drawn from. Both are here because
# the third clause of the property is precisely that the pending one is no shelter.
EMBEDDING_STATES: Final[tuple[str, ...]] = (
    EmbeddingState.EMBEDDED.value,
    EmbeddingState.PENDING.value,
)

# The recall floor this run is configured with, and the two standings a generated
# Learned_Procedure is drawn from: one comfortably below the floor, one comfortably
# above it, so both arms of the strict comparison are reached.
RECALL_FLOOR: Final[str] = "0.15"
PROCEDURE_STANDINGS: Final[tuple[float, ...]] = (0.05, 0.60)

# What the placed rows carry beyond their generated parts.
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"
DERIVATION_METHOD: Final[str] = "distilled"
BINDING_METHODS: Final[tuple[str, ...]] = ("scope", "inherited", "marker")
REQUESTER: Final[str] = "governance-owner"
JUSTIFICATION: Final[str] = "engagement concluded under a contractual purge obligation"
RUN_OWNER: Final[str] = "this-worker"
TEXT_PROVIDER_NAME: Final[str] = "stub-text"
TEXT_MODEL: Final[str] = "stub-text-model"
EMBEDDING_PROVIDER_NAME: Final[str] = "stub-embedding"
EMBEDDING_MODEL: Final[str] = "stub-embedding-model"
BACKUP_TARGET: Final[str] = "s3://operator-owned-bucket.invalid/molt"
CCLOUD_BINARY: Final[str] = "control-plane-command"
CLUSTER_ID: Final[str] = "00000000-0000-0000-0000-000000000000"

# How long a placed row is retained for, as a duration rather than a stated instant.
RETENTION: Final[timedelta] = timedelta(days=90)

# Every selection reason a candidate may lawfully carry: the five the explicit
# sweep records and the one the semantic phase adds.
KNOWN_REASONS: Final[frozenset[str]] = frozenset(SWEEP_REASONS) | {SEMANTIC_RESIDUE_REASON}

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# How one example's namespace is emptied before the next one places its graph. One
# statement, because every content table and every evidence table descends from the
# tenant table by reference, so truncating that one reaches all of them.
RESET_STATEMENT: Final[str] = "TRUNCATE client CASCADE"

INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, content_markers) VALUES (%s, %s, %s, %s)"
)
INSERT_ROOT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
INSERT_SPAWNED_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, parent_session_id, depth) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
INSERT_EVENT: Final[str] = (
    "INSERT INTO ledger ("
    "id, session_id, client_id, seq, category, agent_cli, machine_id, payload, text_body, "
    "content_digest, prev_chain_digest, chain_digest, embedding_state, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::JSONB, %s, %s, %s, %s, %s, "
    "now() + INTERVAL '90 days')"
)
INSERT_ARTIFACT: Final[str] = (
    "INSERT INTO derived_artifact ("
    "id, kind, owner_client_id, body, content_digest, derivation_method, embedding_state, "
    "procedure_confidence, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + INTERVAL '90 days')"
)
INSERT_EDGE: Final[str] = (
    "INSERT INTO lineage_edge (id, child_id, parent_id, parent_kind, derivation_method) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, confidence) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
# A version that was superseded away, which is a statement about the past and must
# neither widen the sweep nor narrow it. The closure is total, so the end of
# validity and the successor reference are written together.
INSERT_CLOSED_BINDING: Final[str] = (
    "INSERT INTO client_binding ("
    "id, artifact_id, artifact_kind, client_id, method, confidence, valid_to, superseded_by) "
    "VALUES (%s, %s, %s, %s, %s, %s, now(), %s)"
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

# What the three clauses are read from.
COUNT_CURRENT_BINDINGS: Final[str] = (
    "SELECT count(*) FROM client_binding WHERE client_id = %s AND superseded_by IS NULL"
)
SELECT_CANDIDATES: Final[str] = (
    "SELECT artifact_id, artifact_kind, selection_reason FROM erasure_candidate WHERE run_id = %s"
)
# One row per candidate with the number of Dispositions naming it, so a candidate
# disposed twice and a candidate never disposed are the same assertion.
SELECT_DISPOSITION_TALLY: Final[str] = (
    "SELECT c.artifact_id, count(d.id), "
    "coalesce(string_agg(DISTINCT d.selection_reason, ','), '') "
    "FROM erasure_candidate AS c LEFT JOIN disposition AS d "
    "ON d.run_id = c.run_id AND d.artifact_id = c.artifact_id "
    "WHERE c.run_id = %s GROUP BY c.artifact_id"
)
SELECT_DISPOSITION_REASONS: Final[str] = (
    "SELECT disposition, reason, selection_reason FROM disposition WHERE run_id = %s"
)
SELECT_UNEMBEDDED_COUNT: Final[str] = "SELECT unembedded_count FROM erasure_run WHERE id = %s"
COUNT_SURVIVING_PENDING: Final[str] = (
    "SELECT count(*) FROM derived_artifact AS d "
    "JOIN client_binding AS b ON b.artifact_id = d.id AND b.superseded_by IS NULL "
    "WHERE b.client_id = %s AND d.embedding_state = %s"
)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What one drawn graph is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    """One Event of one Session: its category and whether it awaits a vector."""

    category: str
    pending: bool


@dataclass(frozen=True, slots=True)
class PlannedSession:
    """One Session, its owning Client, and the Session it was spawned from."""

    client: int
    parent: int | None
    events: tuple[PlannedEvent, ...]


@dataclass(frozen=True, slots=True)
class PlannedArtifact:
    """One Derived_Artifact: its kind, its parents, and who is attributed with it.

    Attributes:
        owner: The Client ordinal the row itself belongs to.
        kind: The Derived_Artifact kind, which decides whether a standing is carried.
        standing: The Procedure_Confidence, present exactly for the procedure kind.
        pending: Whether the Artifact is left awaiting a vector.
        parents: The ordinals of earlier Artifacts it derives from, so the graph is
            acyclic by construction and derivation depth grows with the ordinal.
        bound: The Client ordinals holding a current Attribution_Version.
        closed: The Client ordinals holding only a superseded version, which is a
            claim about the past and no live claim at all.
    """

    owner: int
    kind: str
    standing: float | None
    pending: bool
    parents: tuple[int, ...]
    bound: tuple[int, ...]
    closed: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MemoryGraph:
    """One generated memory graph, before any of it has been placed."""

    clients: int
    sessions: tuple[PlannedSession, ...]
    artifacts: tuple[PlannedArtifact, ...]
    working_rows: int

    @property
    def blended(self) -> int:
        """How many Artifacts the erased Client shares a current claim on."""
        return sum(
            1
            for artifact in self.artifacts
            if ERASED_ORDINAL in artifact.bound and len(artifact.bound) > 1
        )

    @property
    def pending_artifacts(self) -> int:
        """How many Artifacts were left awaiting a vector."""
        return sum(1 for artifact in self.artifacts if artifact.pending)


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


@st.composite
def memory_graphs(draw: st.DrawFn) -> MemoryGraph:
    """Draw the memory graph an erasure run acts on.

    Clients come first because every later draw is stated in their ordinals. The
    Sessions form a spawn forest by drawing each parent from the Sessions already
    drawn, which keeps depth bounded by the ordinal and the forest acyclic without a
    rejection. The Artifacts do the same for derivation, so a chain of them is
    reachable and a cycle is not.

    Attribution is drawn as two disjoint sets per Artifact: the Clients holding a
    current version and the Clients holding only a superseded one. Drawing them
    disjointly is what makes the superseded arm meaningful — a Client that appears
    in both would be selected by its current version whatever the history said, and
    the sweep's unsuperseded predicate would never be tested.
    """
    clients = draw(st.integers(min_value=MIN_CLIENTS, max_value=MAX_CLIENTS))
    ordinals = range(clients)

    sessions: list[PlannedSession] = []
    for index in range(draw(st.integers(min_value=MIN_SESSIONS, max_value=MAX_SESSIONS))):
        parent = draw(st.none() | st.integers(min_value=0, max_value=max(index - 1, 0)))
        events = tuple(
            PlannedEvent(
                category=draw(st.sampled_from(CATEGORIES)),
                pending=draw(st.booleans()),
            )
            for _ in range(draw(st.integers(min_value=0, max_value=MAX_EVENTS_PER_SESSION)))
        )
        sessions.append(
            PlannedSession(
                client=draw(st.sampled_from(sorted(ordinals))),
                parent=None if index == 0 else parent,
                events=events,
            )
        )

    artifacts: list[PlannedArtifact] = []
    for index in range(draw(st.integers(min_value=0, max_value=MAX_ARTIFACTS))):
        kind = draw(st.sampled_from(ARTIFACT_KINDS))
        parents = (
            draw(
                st.lists(
                    st.integers(min_value=0, max_value=index - 1),
                    min_size=1,
                    max_size=min(MAX_PARENTS, index),
                    unique=True,
                )
            )
            if index > 0
            else []
        )
        bound = draw(
            st.lists(st.sampled_from(sorted(ordinals)), min_size=1, max_size=clients, unique=True)
        )
        # Drawn from the complement rather than filtered against it, so the two sets
        # are disjoint by construction and no example is rejected to make them so.
        available = sorted(set(ordinals) - set(bound))
        closed = (
            draw(
                st.lists(
                    st.sampled_from(available),
                    max_size=len(available),
                    unique=True,
                )
            )
            if available
            else []
        )
        artifacts.append(
            PlannedArtifact(
                owner=draw(st.sampled_from(sorted(ordinals))),
                kind=kind,
                standing=(
                    draw(st.sampled_from(PROCEDURE_STANDINGS))
                    if kind == DerivedArtifactKind.LEARNED_PROCEDURE.value
                    else None
                ),
                pending=draw(st.sampled_from(EMBEDDING_STATES)) == EmbeddingState.PENDING.value,
                parents=tuple(parents),
                bound=tuple(bound),
                closed=tuple(closed),
            )
        )

    return MemoryGraph(
        clients=clients,
        sessions=tuple(sessions),
        artifacts=tuple(artifacts),
        working_rows=draw(st.integers(min_value=0, max_value=MAX_WORKING_ROWS)),
    )


# ---------------------------------------------------------------------------
# Deterministic stubs
# ---------------------------------------------------------------------------


def stub_vector(text: str) -> tuple[float, ...]:
    """A unit vector on one axis, chosen by the text's digest so it is deterministic.

    One axis per text rather than a dense direction, so two different texts are
    almost always orthogonal and the residue phase's review band is entered only
    where an example genuinely plants a near neighbour. That keeps a hundred runs
    free of adjudication round trips without weakening the phase: the same walk runs,
    over a corpus whose distances this module chose.
    """
    axis = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:2], "big")
    place = axis % EMBEDDING_DIMENSION
    return tuple(1.0 if index == place else 0.0 for index in range(EMBEDDING_DIMENSION))


def body_digest(text: str) -> str:
    """The digest a Derived_Artifact row records for a body."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StubTextProvider:
    """A Text_Provider that answers a rewrite from a table rather than from a model.

    The table is built while the bodies are placed, so the correct output of a
    rewrite is computable here and the property does not depend on a model. A body
    the table does not know is answered with text that carries no marker at all,
    which the rewrite validation refuses and the run then handles by its own
    fail-closed path — the shipped behaviour rather than a fault.
    """

    answers: dict[str, str] = field(default_factory=dict)
    calls: int = 0

    name: str = TEXT_PROVIDER_NAME
    model_id: str = TEXT_MODEL
    supports_prompt_cache: bool = False

    def generate(self, prompt: Prompt) -> TextResult:
        """Answer with the replacement for whichever known body the prompt carries."""
        self.calls += 1
        answer = ""
        for body, replacement in self.answers.items():
            if body in prompt.variable_suffix:
                answer = replacement
                break
        return TextResult(
            text=answer,
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
    """Deterministic stub vectors, one per text, with no provider call anywhere."""

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
    """A backup statement the cluster accepts, so the primary path succeeds."""


def unreachable_runner(vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
    """A control-plane command no example reaches, because the primary path is available."""
    assert vector and timeout_seconds > 0
    raise AssertionError("the fallback backup path is not entered when the primary one works")


def example_configuration() -> Configuration:
    """A configuration naming every number a run turns on, none of them a default."""
    return Configuration(
        environ={
            "MOLT_ERASURE_BATCH_SIZE": "25",
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
            "MOLT_PROCEDURE_RECALL_FLOOR": RECALL_FLOOR,
        },
        file_values={},
    )


def backup_settings() -> BackupSettings:
    """The backup settings a run resolves against, naming a target that resolves nowhere."""
    return BackupSettings(
        target=BACKUP_TARGET,
        ccloud_binary=CCLOUD_BINARY,
        cluster_id=CLUSTER_ID,
        timeout_seconds=30,
    )


# ---------------------------------------------------------------------------
# The placed graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Placed:
    """One graph as it now stands in the schema, with the identifiers it was given."""

    clients: tuple[UUID, ...]
    sessions: tuple[UUID, ...]
    artifacts: tuple[UUID, ...]
    governance_session: UUID
    rewrites: dict[str, str]

    @property
    def erased(self) -> UUID:
        """The Client whose content this run erases."""
        return self.clients[ERASED_ORDINAL]


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

    def count(self, statement: str, params: tuple[object, ...] | None = None) -> int:
        """The single number one counting statement reports."""
        produced = self.rows(statement, params)
        assert len(produced) == 1, "a counting statement reported no single row"
        return int(produced[0][0])

    def reset(self) -> None:
        """Empty the namespace, so every example starts from the same state."""
        self.send(RESET_STATEMENT)

    def place(self, graph: MemoryGraph) -> Placed:
        """Insert every row of one generated graph and report the identifiers used."""
        clients = tuple(uuid4() for _ in range(graph.clients))
        for ordinal, identifier in enumerate(clients):
            slug = f"tenant-{identifier.hex[:12]}"
            self.send(
                INSERT_CLIENT,
                (identifier, slug, f"Tenant {slug}", [MARKERS[ordinal]]),
            )

        # A Session of a retained Client, which the closing attribution Event of a
        # surgical redaction is recorded within. Owned by a retained Client because a
        # Session of the erased one is itself swept.
        governance = uuid4()
        self.send(INSERT_ROOT_SESSION, (governance, clients[1], AGENT_CLI, MACHINE_ID))

        sessions: list[UUID] = []
        for planned in graph.sessions:
            identifier = uuid4()
            if planned.parent is None:
                self.send(
                    INSERT_ROOT_SESSION,
                    (identifier, clients[planned.client], AGENT_CLI, MACHINE_ID),
                )
            else:
                self.send(
                    INSERT_SPAWNED_SESSION,
                    (
                        identifier,
                        clients[planned.client],
                        AGENT_CLI,
                        MACHINE_ID,
                        sessions[planned.parent],
                        planned.parent + 1,
                    ),
                )
            sessions.append(identifier)
            for ordinal, planned_event in enumerate(planned.events):
                self._event(
                    identifier,
                    clients[planned.client],
                    ordinal + 1,
                    planned_event,
                )

        rewrites: dict[str, str] = {}
        artifacts: list[UUID] = []
        for index, planned_artifact in enumerate(graph.artifacts):
            identifier = uuid4()
            body = _body_for(planned_artifact)
            rewrites[body] = _rewritten(planned_artifact)
            self.send(
                INSERT_ARTIFACT,
                (
                    identifier,
                    planned_artifact.kind,
                    clients[planned_artifact.owner],
                    body,
                    body_digest(body),
                    DERIVATION_METHOD,
                    (
                        EmbeddingState.PENDING.value
                        if planned_artifact.pending
                        else EmbeddingState.EMBEDDED.value
                    ),
                    planned_artifact.standing,
                ),
            )
            artifacts.append(identifier)
            for parent in planned_artifact.parents:
                self.send(
                    INSERT_EDGE,
                    (
                        uuid4(),
                        identifier,
                        artifacts[parent],
                        ArtifactKind.DERIVED_ARTIFACT.value,
                        DERIVATION_METHOD,
                    ),
                )
            for ordinal in planned_artifact.bound:
                self.send(
                    INSERT_BINDING,
                    (
                        uuid4(),
                        identifier,
                        ArtifactKind.DERIVED_ARTIFACT.value,
                        clients[ordinal],
                        BINDING_METHODS[ordinal % len(BINDING_METHODS)],
                        0.9,
                    ),
                )
            for ordinal in planned_artifact.closed:
                self.send(
                    INSERT_CLOSED_BINDING,
                    (
                        uuid4(),
                        identifier,
                        ArtifactKind.DERIVED_ARTIFACT.value,
                        clients[ordinal],
                        BINDING_METHODS[ordinal % len(BINDING_METHODS)],
                        0.5,
                        uuid4(),
                    ),
                )
            if not planned_artifact.pending:
                self.send(
                    INSERT_EMBEDDING,
                    (
                        identifier,
                        ArtifactKind.DERIVED_ARTIFACT.value,
                        clients[planned_artifact.owner],
                        EMBEDDING_PROVIDER_NAME,
                        EMBEDDING_MODEL,
                        EMBEDDING_DIMENSION,
                        vector_text(stub_vector(body)),
                    ),
                )
            assert index + 1 == len(artifacts)

        for ordinal in range(graph.working_rows):
            self.send(
                INSERT_WORKING,
                (governance, clients[ERASED_ORDINAL], f"held-{ordinal}", '{"held": true}'),
            )

        return Placed(
            clients=clients,
            sessions=tuple(sessions),
            artifacts=tuple(artifacts),
            governance_session=governance,
            rewrites=rewrites,
        )

    def _event(self, session_id: UUID, client_id: UUID, seq: int, planned: PlannedEvent) -> None:
        """Place one Event, with a chain digest nothing in this property verifies."""
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
                planned.category,
                AGENT_CLI,
                MACHINE_ID,
                f"{planned.category} recorded under sequence {seq}",
                digest,
                previous,
                digest,
                (
                    EmbeddingState.PENDING.value
                    if planned.pending
                    else EmbeddingState.NOT_REQUIRED.value
                ),
            ),
        )


def _segment(ordinal: int) -> str:
    """One Client's own labelled line of a shared body, of a length no other differs from."""
    return f"{MARKERS[ordinal]} the exporter reconciles the ledger for this tenant nightly"


def _body_for(planned: PlannedArtifact) -> str:
    """A body carrying one labelled line per attributed Client, so a rewrite is divisible."""
    ordinals = planned.bound if planned.bound else (planned.owner,)
    return "\n".join(_segment(ordinal) for ordinal in sorted(ordinals))


def _rewritten(planned: PlannedArtifact) -> str:
    """What a well-behaved rewrite answers with: the erased line gone, the rest intact."""
    remaining = [ordinal for ordinal in sorted(planned.bound) if ordinal != ERASED_ORDINAL]
    return "\n".join(_segment(ordinal) for ordinal in remaining)


def _pending_events(graph: MemoryGraph) -> int:
    """How many Events of the erased Client's Sessions were left awaiting a vector.

    Those Events are swept because their Session is, so they are part of the count
    the run row carries and the lower bound this property states has to include them.
    """
    return sum(
        1
        for session in graph.sessions
        if session.client == ERASED_ORDINAL
        for planned in session.events
        if planned.pending
    )


def seams(placed: Placed) -> EngineSeams:
    """The seams one run is driven through: stub provider, stub vectors, no credentials."""
    return EngineSeams(
        configuration=example_configuration(),
        backup=backup_settings(),
        capabilities=CapabilityRecord(),
        supersession=SupersessionContext(
            session_id=placed.governance_session,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=datetime.now(UTC) + RETENTION,
        ),
        text_provider=StubTextProvider(answers=dict(placed.rewrites)),
        embedding_provider=StubEmbeddingProvider(),
        issuer=issued,
        runner=unreachable_runner,
        owner=RUN_OWNER,
    )


def request_for(placed: Placed) -> ErasureRequest:
    """One erasure request for the erased Client, under a key of its own."""
    return ErasureRequest(
        client_id=placed.erased,
        requester=REQUESTER,
        justification=JUSTIFICATION,
        idempotency_key=uuid4().hex,
    )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema."""
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
        return cast(Connection, opened)

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 1: For any memory graph of Sessions, Events,
# Derived_Artifacts, Lineage_Edges, and Client_Bindings over 2 to 5 Clients with
# derivation depth up to 4, after an Erasure_Run for Client C the set of Artifacts
# carrying a Client_Binding for C is empty, every candidate selected by any of the five
# sweep paths has exactly one recorded Disposition carrying a known selection reason,
# and Artifacts in the pending-embedding state are selected like any other.
@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(graph=memory_graphs())
def test_a_run_erases_every_current_attribution_and_explains_every_candidate(
    cluster: Cluster,
    graph: MemoryGraph,
) -> None:
    cluster.reset()
    placed = cluster.place(graph)

    event(f"clients={graph.clients}")
    event(f"sessions={len(graph.sessions)}")
    event(f"artifacts={len(graph.artifacts)}")
    event(f"blended artifacts={graph.blended}")
    event(f"pending artifacts={graph.pending_artifacts}")
    event(f"working rows={graph.working_rows}")

    outcome = run_erasure(cluster.store, request_for(placed), seams(placed))

    assert outcome.status is RunStatus.COMPLETED, (
        f"the run did not complete: {outcome.error_detail}"
    )
    assert outcome.run_id is not None
    run_id = outcome.run_id

    # Requirements 16.2 through 16.7 and 18.1: nothing the erased Client holds a
    # live claim on survives, whichever statement of the sweep reached it.
    assert cluster.count(COUNT_CURRENT_BINDINGS, (placed.erased,)) == 0, (
        "a current attribution version still names the erased client"
    )

    # Requirement 18.4: every candidate is accounted for exactly once, and the
    # reason it was selected under is one the sweep or the semantic phase records.
    candidates = cluster.rows(SELECT_CANDIDATES, (run_id,))
    selected = {UUID(str(row[0])) for row in candidates}
    for _, _, reason in candidates:
        assert str(reason) in KNOWN_REASONS, f"a candidate carries the reason {reason}"

    tally = cluster.rows(SELECT_DISPOSITION_TALLY, (run_id,))
    assert len(tally) == len(candidates), "the candidate set and the tally disagree in size"
    for artifact_id, disposed, reasons in tally:
        assert int(disposed) == 1, (
            f"the candidate {artifact_id} carries {int(disposed)} dispositions where one was read"
        )
        assert str(reasons) in KNOWN_REASONS, (
            f"a disposition of {artifact_id} carries the selection reason {reasons}"
        )

    for disposition, reason, selection_reason in cluster.rows(
        SELECT_DISPOSITION_REASONS, (run_id,)
    ):
        assert str(disposition), "a disposition was recorded with no outcome"
        assert str(reason), "a disposition was recorded with no reason"
        assert str(selection_reason) in KNOWN_REASONS

    # Requirement 10.9: an Artifact awaiting its vector is selected like any other.
    # Asserted against the candidate set, which is evidence and survives the run,
    # rather than against the Artifact rows the run has since removed: the pending
    # Artifacts the erased Client is attributed with are compared to the embedded
    # ones, and both sets have to be selected whole.
    pending = EmbeddingState.PENDING.value
    attributed = {
        placed.artifacts[index]: planned.pending
        for index, planned in enumerate(graph.artifacts)
        if ERASED_ORDINAL in planned.bound
    }
    awaiting = {identifier for identifier, is_pending in attributed.items() if is_pending}
    embedded = {identifier for identifier, is_pending in attributed.items() if not is_pending}
    assert awaiting <= selected, (
        "an artifact awaiting its embedding was not selected although the client is bound to it"
    )
    assert embedded <= selected, "an embedded artifact of the erased client was not selected"
    assert cluster.count(COUNT_SURVIVING_PENDING, (placed.erased, pending)) == 0, (
        "an artifact awaiting its embedding survived with a live claim on it"
    )

    # And how many were in that state is on the record rather than inferred, because
    # a certificate omitting it would overstate what the semantic phase could reach.
    assert outcome.sweep is not None
    recorded = cluster.count(SELECT_UNEMBEDDED_COUNT, (run_id,))
    assert recorded == outcome.sweep.unembedded_count, (
        "the reported pending-embedding count and the recorded one disagree"
    )
    assert recorded >= len(awaiting) + _pending_events(graph), (
        "the recorded pending-embedding count is below the rows known to be in that state"
    )
