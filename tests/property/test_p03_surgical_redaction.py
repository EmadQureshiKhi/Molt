"""Property 3: a surgical redaction keeps the row, keeps the other tenants, and keeps nothing else.

**Validates: Requirements 18.2, 18.3, 18.4, 18.5, 18.6**

This is the hard claim of the whole erasure path. A blended Derived_Artifact carries
several tenants' content in one body, so erasing one tenant from it is a rewrite of
a surviving row rather than the deletion of a row. Four things therefore have to
hold at once, and a wrong implementation can satisfy three of them:

**The row survives.** A delete would satisfy every erasure claim and destroy the
retained tenants' memory, which is the failure mode the surgical path exists to
avoid.

**The current binding set is exactly the original set less the erased tenant.**
Not fewer, because a rewrite that dropped a retained tenant's claim has erased
somebody who asked for nothing; and not more, because a rewrite that left the
erased tenant's claim standing has recorded an erasure that did not happen. The
erased tenant's claim is closed as history rather than deleted, so the row is still
there and no longer current.

**Both digests are recorded.** The Disposition carries the digest the body had
before and the digest it has now, because those two values are the only evidence a
certificate can offer that a body changed, given that neither body is stored.

**The pre-redaction body appears in no row of any table.** This is the assertion
the property is really for, and it is the one that is easy to write weakly. A
rewrite path may keep the old body somewhere perfectly innocently: a stored diff,
a residue snippet, a certificate payload, a superseded revision row, an audit
snapshot. Every one of those is a leak of exactly the content the run certified was
gone. So the check here is not a spot check of the artifact row. It is a scan of
every base table in the schema, whole row cast to text, looking for the
pre-redaction body and for each erased segment line in it, with the table named in
the failure. A guard beside it reads the schema's own table list and refuses to run
if the scan does not cover every table, so a table added by a later migration
cannot silently fall outside the claim.

Five decisions shape what is generated and what is run.

**The generator is `blended_artifacts()` and it labels segments per tenant.** One
body is assembled from lines, each line owned by one tenant and marked with that
tenant's own content marker. The erased tenant owns at least one line and every
retained tenant owns at least one, because a retained tenant whose marker the
original never carried is not a tenant the rewrite can be asked to preserve. The
interleaving is drawn, so the erased tenant's lines are not always at one end.

**The rewriter is a stub that reads its own prompt.** It does not receive the
answer from the example. It parses the erased tenant's names out of the prompt's
variable suffix, takes the document out of it, and returns the document with every
line naming those names dropped. That is the honest rewrite, produced generically,
so the prompt shape is exercised rather than asserted about and the answer is a
function of the drawn body rather than a constant the example supplied.

**The whole run is executed, not just the surgical transaction.** `run_erasure`
performs the lease, the backup gate, the sweep, the residue phase, the dispositions
and the certificate, and any one of those phases could be the one that writes the
leak this property looks for. Running the phase in isolation would scan a schema no
other phase had touched.

**Two more Artifacts stand beside the blended one in every example.** One bound to
the erased tenant alone, which must be gone, and one bound to a retained tenant
alone, whose body and digest must be untouched. They are what makes the
preservation clause a comparison rather than a tautology.

**The length band is set wide and both ends are named.** The drawn blend may leave
the erased tenant owning most of the body, and the honest rewrite of that body is
short. The band is a separate concern with a property of its own, so it is opened
here to admit every honest rewrite; a narrow band would turn a drawn shape into a
fail-closed hard delete and the surgical path would go unexercised for that
example.

The example budget is 25 with no per-example deadline. One example is a real
erasure run against a live schema plus a scan of every table in it, so the cost per
example is on the order of a second; the budget buys every interleaving and every
tenant count the generator reaches, and a larger one would buy repetition.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.backup import BackupSettings, CommandResult
from molt.config.resolve import Configuration
from molt.erase.disposition import REWRITTEN_REASON, DispositionKind
from molt.erase.engine import EngineSeams, ErasureRequest, RunStatus, run_erasure
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind, DerivedArtifactKind
from molt.providers import Prompt, ProviderProbe, TextResult
from molt.store import Connection, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.store.capability import CapabilityRecord
from molt.store.embeddings import vector_text
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The example budget, and the shape of a drawn blend. At least one erased segment
# and at least one retained tenant, because anything less is not a blended Artifact
# and takes the hard-delete arm instead.
MAX_EXAMPLES: Final[int] = 25
MIN_RETAINED_CLIENTS: Final[int] = 1
MAX_RETAINED_CLIENTS: Final[int] = 3
MIN_SEGMENTS_PER_CLIENT: Final[int] = 1
MAX_SEGMENTS_PER_CLIENT: Final[int] = 3

# The index the erased tenant occupies in a drawn segment's owner field. Negative so
# it can never collide with a retained tenant's position.
ERASED_OWNER: Final[int] = -1

# The phrases a segment's text is drawn from. Content-free on purpose: what a
# segment says is irrelevant to every clause, and what matters is that each line is
# unique, so the index is part of the line and the marker is per tenant.
PHRASES: Final[tuple[str, ...]] = (
    "the exporter reconciles against the ledger",
    "the deployment gate runs the migration first",
    "the recall path reads the covering index",
    "the collector spools before it acknowledges",
    "the sweep batches its deletes by configuration",
)

# The labels the rewrite prompt's variable suffix carries. Restated here rather than
# imported from the module under test, so the stub parses the shape the design fixes
# rather than whatever that module currently happens to emit.
ERASED_LABEL: Final[str] = "Organisation to remove:"
RETAINED_LABEL: Final[str] = "Organisations to keep:"
DOCUMENT_LABEL: Final[str] = "Document:"
MENTION_SEPARATOR: Final[str] = ", "

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places for itself. The engine owns no tenant insert and no
# Artifact insert, so every row a run acts on is placed here.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, content_markers) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
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

# What the clauses are read from.
READ_ARTIFACT: Final[str] = (
    "SELECT body, content_digest, revision, redacted_at FROM derived_artifact WHERE id = %s"
)
COUNT_ARTIFACT: Final[str] = "SELECT count(*) FROM derived_artifact WHERE id = %s"
READ_BINDINGS: Final[str] = (
    "SELECT client_id, superseded_by, valid_to FROM client_binding WHERE artifact_id = %s"
)
READ_DISPOSITION: Final[str] = (
    "SELECT disposition, reason, pre_digest, post_digest, bindings_before, bindings_after, "
    "removed_segments, retained_segments FROM disposition "
    "WHERE run_id = %s AND artifact_id = %s"
)

# The table-list guard. The scan below names its tables one at a time, because a
# statement is a whole literal here and an identifier is never interpolated into
# one; this reads the schema's own list so a table a later migration adds cannot
# fall outside the scan unnoticed.
BASE_TABLES_QUERY: Final[str] = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = current_schema() AND table_type = %s ORDER BY table_name"
)
BASE_TABLE_TYPE: Final[str] = "BASE TABLE"

# The scan. Every base table of the schema, each row cast whole to text, so a
# column this module has never heard of is searched along with the ones it has. The
# source name comes back with the count, so a leak names the table it leaked into
# rather than merely existing.
SCAN_EVERY_ROW_STATEMENT: Final[str] = (
    "SELECT source, count(*) FROM ("
    "SELECT 'approval_queue' AS source, t::STRING AS row_text FROM approval_queue AS t "
    "UNION ALL SELECT 'audit_log_snapshot', t::STRING FROM audit_log_snapshot AS t "
    "UNION ALL SELECT 'backup_record', t::STRING FROM backup_record AS t "
    "UNION ALL SELECT 'capability', t::STRING FROM capability AS t "
    "UNION ALL SELECT 'checkpoint_session', t::STRING FROM checkpoint_session AS t "
    "UNION ALL SELECT 'client', t::STRING FROM client AS t "
    "UNION ALL SELECT 'client_binding', t::STRING FROM client_binding AS t "
    "UNION ALL SELECT 'derived_artifact', t::STRING FROM derived_artifact AS t "
    "UNION ALL SELECT 'disposition', t::STRING FROM disposition AS t "
    "UNION ALL SELECT 'embedding', t::STRING FROM embedding AS t "
    "UNION ALL SELECT 'erasure_candidate', t::STRING FROM erasure_candidate AS t "
    "UNION ALL SELECT 'erasure_certificate', t::STRING FROM erasure_certificate AS t "
    "UNION ALL SELECT 'erasure_lease', t::STRING FROM erasure_lease AS t "
    "UNION ALL SELECT 'erasure_request', t::STRING FROM erasure_request AS t "
    "UNION ALL SELECT 'erasure_run', t::STRING FROM erasure_run AS t "
    "UNION ALL SELECT 'ledger', t::STRING FROM ledger AS t "
    "UNION ALL SELECT 'ledger_checkpoint', t::STRING FROM ledger_checkpoint AS t "
    "UNION ALL SELECT 'lineage_edge', t::STRING FROM lineage_edge AS t "
    "UNION ALL SELECT 'policy_match', t::STRING FROM policy_match AS t "
    "UNION ALL SELECT 'policy_rule', t::STRING FROM policy_rule AS t "
    "UNION ALL SELECT 'procedure_confidence_change', t::STRING "
    "FROM procedure_confidence_change AS t "
    "UNION ALL SELECT 'procedure_outcome', t::STRING FROM procedure_outcome AS t "
    "UNION ALL SELECT 'procedure_retrieval', t::STRING FROM procedure_retrieval AS t "
    "UNION ALL SELECT 'residue_candidate', t::STRING FROM residue_candidate AS t "
    "UNION ALL SELECT 'run_session', t::STRING FROM run_session AS t "
    "UNION ALL SELECT 'schema_migration', t::STRING FROM schema_migration AS t "
    "UNION ALL SELECT 'session', t::STRING FROM session AS t "
    "UNION ALL SELECT 'watcher_watermark', t::STRING FROM watcher_watermark AS t "
    "UNION ALL SELECT 'working_memory', t::STRING FROM working_memory AS t"
    ") AS every_row WHERE strpos(row_text, %s) > 0 GROUP BY source ORDER BY source"
)

# The tables the statement above names, so the guard compares two sets rather than
# a set against a promise.
SCANNED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "approval_queue",
        "audit_log_snapshot",
        "backup_record",
        "capability",
        "checkpoint_session",
        "client",
        "client_binding",
        "derived_artifact",
        "disposition",
        "embedding",
        "erasure_candidate",
        "erasure_certificate",
        "erasure_lease",
        "erasure_request",
        "erasure_run",
        "ledger",
        "ledger_checkpoint",
        "lineage_edge",
        "policy_match",
        "policy_rule",
        "procedure_confidence_change",
        "procedure_outcome",
        "procedure_retrieval",
        "residue_candidate",
        "run_session",
        "schema_migration",
        "session",
        "watcher_watermark",
        "working_memory",
    }
)

# The values every placed row carries.
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "property-machine"
DERIVATION_METHOD: Final[str] = "distilled"
BINDING_METHOD: Final[str] = "marker"
BINDING_CONFIDENCE: Final[float] = 0.9
REQUESTER: Final[str] = "governance-owner"
JUSTIFICATION: Final[str] = "engagement concluded under a contractual purge obligation"
RUN_OWNER: Final[str] = "this-worker"
TEXT_PROVIDER_NAME: Final[str] = "stub-rewriter"
TEXT_MODEL: Final[str] = "stub-rewrite-model"
EMBEDDING_PROVIDER_NAME: Final[str] = "stub-embedding"
EMBEDDING_MODEL: Final[str] = "stub-embedding-model"
BACKUP_TARGET: Final[str] = "s3://operator-owned-bucket.invalid/molt"
CCLOUD_BINARY: Final[str] = "ccloud-stub"
CLUSTER_ID: Final[str] = "cluster-stub"

# How long a placed row is retained for, as a duration rather than a stated instant,
# and the instant every derived reading is taken from.
RETENTION: Final[timedelta] = timedelta(days=90)
FIXED_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What a drawn example is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    """One labelled line of a blended body: whose it is, and what it says.

    The index is part of the rendered line so that no two segments of one body are
    the same text. That matters for the leak scan: a needle that also appeared in a
    line the rewrite legitimately kept would report a leak that is not one.
    """

    owner: int
    index: int
    phrase: str

    @property
    def erased(self) -> bool:
        """Whether this segment belongs to the tenant being erased."""
        return self.owner == ERASED_OWNER

    def line(self, markers: Sequence[str]) -> str:
        """The rendered line, marked with its owner's own content marker."""
        return f"{markers[self.owner]} note {self.index:02d} {self.phrase}"


@dataclass(frozen=True, slots=True)
class Blend:
    """A drawn blended body: how many tenants retain a claim, and the segments.

    Attributes:
        retained_count: How many tenants other than the erased one hold a claim.
        segments: The labelled lines, in the order they appear in the body.
    """

    retained_count: int
    segments: tuple[Segment, ...]

    @property
    def erased_segments(self) -> tuple[Segment, ...]:
        """The lines the erasure must remove."""
        return tuple(segment for segment in self.segments if segment.erased)

    @property
    def retained_segments(self) -> tuple[Segment, ...]:
        """The lines every other tenant's claim rests on."""
        return tuple(segment for segment in self.segments if not segment.erased)


def marker_of(owner: int) -> str:
    """The content marker one owner's lines carry.

    Zero-padded and suffixed so that no marker is a substring of another: an erased
    marker that was a prefix of a retained one would make the rewriter's own
    still-names-the-tenant check fire on an honest answer.
    """
    if owner == ERASED_OWNER:
        return "TENANT-ERASED-MARK"
    return f"TENANT-{owner:02d}-MARK"


def markers_for(retained_count: int) -> tuple[str, ...]:
    """The marker table a segment renders through, indexed by owner.

    Built so that index -1 selects the erased tenant's marker, which is what lets a
    segment render itself without knowing which arm it is on.
    """
    return (*(marker_of(owner) for owner in range(retained_count)), marker_of(ERASED_OWNER))


def body_of(blend: Blend) -> str:
    """The pre-redaction body of a drawn blend, one segment per line."""
    markers = markers_for(blend.retained_count)
    return "".join(f"{segment.line(markers)}\n" for segment in blend.segments)


def digest_of(text: str) -> str:
    """The digest a Derived_Artifact row records for a body.

    Restated here rather than imported, so the digest clause compares the stored
    value against the definition the requirement states rather than against the
    function the write path used.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


@st.composite
def blended_artifacts(draw: st.DrawFn) -> Blend:
    """Draw one blended body as per-tenant labelled segments.

    Every tenant of the blend owns between one and three lines, the erased tenant
    included, and the interleaving is a drawn permutation rather than a
    concatenation. Both matter: a tenant owning no line is a tenant the rewrite
    cannot be asked to preserve, and a body whose erased lines are always adjacent
    at one end would never exercise a rewrite that has to remove lines from the
    middle.
    """
    retained_count = draw(
        st.integers(min_value=MIN_RETAINED_CLIENTS, max_value=MAX_RETAINED_CLIENTS)
    )
    owners: list[int] = []
    for owner in (ERASED_OWNER, *range(retained_count)):
        count = draw(
            st.integers(min_value=MIN_SEGMENTS_PER_CLIENT, max_value=MAX_SEGMENTS_PER_CLIENT)
        )
        owners.extend([owner] * count)
    ordered = draw(st.permutations(owners))
    phrases = draw(
        st.lists(
            st.sampled_from(PHRASES),
            min_size=len(ordered),
            max_size=len(ordered),
        )
    )
    segments = tuple(
        Segment(owner=owner, index=index, phrase=phrase)
        for index, (owner, phrase) in enumerate(zip(ordered, phrases, strict=True))
    )
    return Blend(retained_count=retained_count, segments=segments)


# ---------------------------------------------------------------------------
# The stub rewriter, which reads its own prompt
# ---------------------------------------------------------------------------


def mentions_and_document(suffix: str) -> tuple[tuple[str, ...], str]:
    """Split a rewrite prompt's variable suffix into the erased names and the document.

    Parsed rather than supplied, so the stub answers a function of the body it was
    actually given. A suffix that does not carry the three labels the design fixes
    is a failure of the prompt shape and is reported as one here.
    """
    assert suffix.startswith(ERASED_LABEL), "a rewrite prompt names the organisation to remove"
    assert RETAINED_LABEL in suffix, "a rewrite prompt names the organisations to keep"
    head, _, document = suffix.partition(f"{DOCUMENT_LABEL}\n")
    assert document, "a rewrite prompt carries the document under its own label"
    erased_line = head.split("\n", 1)[0].removeprefix(ERASED_LABEL).strip()
    mentions = tuple(name.strip() for name in erased_line.split(MENTION_SEPARATOR) if name.strip())
    assert mentions, "a rewrite prompt names at least one mention of the erased organisation"
    return mentions, document


def honest_rewrite(document: str, mentions: Sequence[str]) -> str:
    """The document with every line naming the erased tenant dropped, and no other change.

    This is what a well-behaved model would answer for a body of labelled segments:
    the retained lines exactly as they stood, in the order they stood, and nothing
    of the removed ones.
    """
    kept = [
        line
        for line in document.splitlines()
        if not any(name.casefold() in line.casefold() for name in mentions)
    ]
    return "".join(f"{line}\n" for line in kept)


@dataclass(slots=True)
class StubRewriter:
    """A Text_Provider that redacts by reading the prompt it was handed.

    The capability fields are settable because the provider protocol declares them
    as variables, and a frozen shape would satisfy the runtime and not the contract.
    """

    name: str = TEXT_PROVIDER_NAME
    model_id: str = TEXT_MODEL
    supports_prompt_cache: bool = False
    documents: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)

    def generate(self, prompt: Prompt) -> TextResult:
        """Answer with the honest rewrite of the document the prompt carries."""
        assert prompt.stable_prefix, "every prompt this design sends carries a stable prefix"
        mentions, document = mentions_and_document(prompt.variable_suffix)
        answer = honest_rewrite(document, mentions)
        self.documents.append(document)
        self.answers.append(answer)
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


# ---------------------------------------------------------------------------
# Deterministic vectors, dense so that no two texts are near neighbours
# ---------------------------------------------------------------------------


def stub_vector(text: str, dimensions: int = EMBEDDING_DIMENSION) -> tuple[float, ...]:
    """A reproducible unit vector for one text, dense rather than one-hot.

    Dense on purpose: a one-hot stub places two texts either at distance zero or at
    the maximum, so a digest collision would make two unrelated bodies exact
    neighbours and pull an Artifact into the residue set for reasons that have
    nothing to do with the property. A dense pseudo-random direction puts every
    pair near cosine distance one, which is what keeps the residue phase quiet.
    """
    raw = hashlib.sha256(text.encode("utf-8")).digest()
    stream = b"".join(
        hashlib.sha256(raw + counter.to_bytes(8, "big")).digest()
        for counter in range((dimensions * 4 // 32) + 1)
    )
    components = [
        value / 2147483648.0 for value in struct.unpack(f">{dimensions}i", stream[: dimensions * 4])
    ]
    norm = math.sqrt(math.fsum(component * component for component in components))
    if norm == 0.0:  # pragma: no cover - unreachable for any digest-derived direction
        return tuple(1.0 if index == 0 else 0.0 for index in range(dimensions))
    return tuple(component / norm for component in components)


@dataclass(slots=True)
class StubEmbeddingProvider:
    """An embedding provider that computes vectors instead of calling a model."""

    name: str = EMBEDDING_PROVIDER_NAME
    model_id: str = EMBEDDING_MODEL
    dimensions: int = EMBEDDING_DIMENSION

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One unit vector per text, in the input order."""
        return [stub_vector(text, self.dimensions) for text in texts]

    def probe(self) -> ProviderProbe:
        """Report reachability and the declared width the startup gate reads."""
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


# ---------------------------------------------------------------------------
# The cluster the run is executed against
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tenant:
    """One placed tenant: its identifier, its names, and the marker its lines carry."""

    client_id: UUID
    slug: str
    display_name: str
    marker: str

    @property
    def mentions(self) -> tuple[str, ...]:
        """Every name whose presence in a body means this tenant is still named."""
        return (self.slug, self.display_name, self.marker)


@dataclass(frozen=True, slots=True)
class Placed:
    """The rows one example placed, and what the clauses are stated about.

    Attributes:
        erased: The tenant the run erases.
        retained: The tenants whose claims must survive the rewrite untouched.
        session_id: The Session a supersession Event is recorded within.
        blended_id: The Artifact the surgical redaction applies to.
        sole_id: The Artifact the erased tenant holds alone, which must be gone.
        untouched_id: An Artifact a retained tenant holds alone, which must not move.
        body: The pre-redaction body of the blended Artifact.
    """

    erased: Tenant
    retained: tuple[Tenant, ...]
    session_id: UUID
    blended_id: UUID
    sole_id: UUID
    untouched_id: UUID
    body: str

    @property
    def erased_mentions(self) -> tuple[str, ...]:
        """The erased tenant's names, which the honest rewrite drops every line naming."""
        return self.erased.mentions


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and this module's own reads."""

    store: MemoryStore
    connection: DriverConnection

    # -- statement plumbing -----------------------------------------------

    def rows(
        self,
        statement: str,
        params: tuple[object, ...] | None = None,
    ) -> list[tuple[Any, ...]]:
        """Send one parameterised statement on this module's own connection."""
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

    # -- placed rows ------------------------------------------------------

    def tenant(self, marker: str) -> Tenant:
        """Place one tenant carrying one content marker."""
        identifier = uuid4()
        slug = f"tenant-{identifier.hex[:12]}"
        display_name = f"Tenant {slug}"
        self.send(INSERT_CLIENT, (identifier, slug, display_name, [marker]))
        return Tenant(
            client_id=identifier,
            slug=slug,
            display_name=display_name,
            marker=marker,
        )

    def session(self, client_id: UUID) -> UUID:
        """Place one Session of a tenant and return its identifier."""
        identifier = uuid4()
        self.send(INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
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
                digest_of(body),
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
        """Place one current Attribution_Version for an Artifact and a tenant."""
        self.send(
            INSERT_BINDING,
            (
                uuid4(),
                artifact_id,
                ArtifactKind.DERIVED_ARTIFACT.value,
                client_id,
                BINDING_METHOD,
                BINDING_CONFIDENCE,
            ),
        )

    def place(self, blend: Blend) -> Placed:
        """Place one drawn blend, plus the two Artifacts the preservation clause reads."""
        erased = self.tenant(marker_of(ERASED_OWNER))
        retained = tuple(self.tenant(marker_of(owner)) for owner in range(blend.retained_count))
        body = body_of(blend)

        blended_id = self.artifact(retained[0].client_id, body)
        self.bind(blended_id, erased.client_id)
        for tenant in retained:
            self.bind(blended_id, tenant.client_id)

        sole_body = f"{erased.marker} sole holding {blended_id.hex[:8]}\n"
        sole_id = self.artifact(erased.client_id, sole_body)
        self.bind(sole_id, erased.client_id)

        untouched_body = f"{retained[0].marker} untouched holding {blended_id.hex[:8]}\n"
        untouched_id = self.artifact(retained[0].client_id, untouched_body)
        self.bind(untouched_id, retained[0].client_id)

        return Placed(
            erased=erased,
            retained=retained,
            session_id=self.session(retained[0].client_id),
            blended_id=blended_id,
            sole_id=sole_id,
            untouched_id=untouched_id,
            body=body,
        )

    # -- reads the clauses are stated over --------------------------------

    def current_bindings(self, artifact_id: UUID) -> frozenset[UUID]:
        """The tenants holding a current claim on one Artifact."""
        return frozenset(
            _as_uuid(row[0])
            for row in self.rows(READ_BINDINGS, (artifact_id,))
            if row[1] is None and row[2] is None
        )

    def closed_bindings(self, artifact_id: UUID) -> frozenset[UUID]:
        """The tenants whose claim on one Artifact stands as history rather than as current."""
        return frozenset(
            _as_uuid(row[0])
            for row in self.rows(READ_BINDINGS, (artifact_id,))
            if row[1] is not None or row[2] is not None
        )

    def leaks_of(self, needle: str) -> dict[str, int]:
        """Every table holding a row whose text contains the needle, with the row count.

        One statement over every base table of the schema, each row cast whole to
        text, so a column no assertion here names is searched along with the ones
        that are named.
        """
        return {str(row[0]): int(row[1]) for row in self.rows(SCAN_EVERY_ROW_STATEMENT, (needle,))}

    def base_tables(self) -> frozenset[str]:
        """Every base table the schema holds, read from the schema's own catalogue."""
        return frozenset(str(row[0]) for row in self.rows(BASE_TABLES_QUERY, (BASE_TABLE_TYPE,)))


def _as_uuid(value: object) -> UUID:
    """Narrow a stored identifier, refusing anything else."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _as_slugs(value: object) -> frozenset[str]:
    """Narrow a stored slug array to a set of names."""
    assert isinstance(value, list), "a binding slug column holds an array"
    return frozenset(str(item) for item in cast(list[object], value))


# ---------------------------------------------------------------------------
# The seams a run executes under
# ---------------------------------------------------------------------------


def example_configuration() -> Configuration:
    """A configuration naming every number a run turns on, none of them defaults.

    The ratio band is opened at both ends for the reason the module docstring gives:
    the drawn blend decides how much of the body the honest rewrite removes, and a
    band narrow enough to refuse a legitimately short answer would send the example
    down the fail-closed hard delete instead of the surgical path.
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
            "MOLT_REWRITE_LENGTH_RATIO_MIN": "0.05",
            "MOLT_REWRITE_LENGTH_RATIO_MAX": "3.0",
            "MOLT_RETENTION_DEFAULT_INTERVAL": "90 days",
            "MOLT_LEASE_OWNER": RUN_OWNER,
            "MOLT_PROCEDURE_RECALL_FLOOR": "0.15",
        },
        file_values={},
    )


def backup_settings() -> BackupSettings:
    """The backup settings a run executes under, naming a target that resolves nowhere."""
    return BackupSettings(
        target=BACKUP_TARGET,
        ccloud_binary=CCLOUD_BINARY,
        cluster_id=CLUSTER_ID,
        timeout_seconds=30,
    )


def accepted(_statement: str, _parameters: tuple[object, ...]) -> None:
    """A backup statement the cluster accepts, so the primary path succeeds."""


def unreachable_runner(vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
    """A control-plane command no example reaches, because the primary path answers."""
    assert vector and timeout_seconds > 0
    raise AssertionError("the fallback path is not entered when the primary path is available")


def seams(placed: Placed, rewriter: StubRewriter) -> EngineSeams:
    """The seams a run executes under: the stub rewriter, stub vectors, no credentials."""
    return EngineSeams(
        configuration=example_configuration(),
        backup=backup_settings(),
        capabilities=CapabilityRecord(),
        supersession=SupersessionContext(
            session_id=placed.session_id,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=FIXED_INSTANT + RETENTION,
        ),
        text_provider=rewriter,
        embedding_provider=StubEmbeddingProvider(),
        issuer=accepted,
        runner=unreachable_runner,
        progress=None,
        owner=RUN_OWNER,
    )


def request_for(placed: Placed) -> ErasureRequest:
    """One erasure request for the erased tenant, under an idempotency key of its own."""
    return ErasureRequest(
        client_id=placed.erased.client_id,
        requester=REQUESTER,
        justification=JUSTIFICATION,
        idempotency_key=uuid4().hex,
        dry_run=False,
    )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema.

    Every migration is applied because the lease table, the fencing columns, and the
    two segment-count columns a surgical Disposition records all arrive in later
    generations than the tables a run acts on. Module scope keeps the schema cost
    paid once: examples are isolated by tenants and Artifacts of their own.
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
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 3: For any blended Derived_Artifact carrying labelled
# segments of several Clients, an Erasure_Run for one of those Clients leaves the
# Artifact row in place, leaves the current Attribution_Version set equal to the
# original set less the erased Client, records both the pre-redaction and the
# post-redaction content digest on the Disposition, and leaves the pre-redaction
# body present in no row of any table.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(blend=blended_artifacts())
def test_a_surgical_redaction_keeps_the_row_the_other_tenants_and_none_of_the_old_body(
    cluster: Cluster, blend: Blend
) -> None:
    covered = cluster.base_tables()
    assert covered == SCANNED_TABLES, (
        "the leak scan does not cover every base table of the schema; "
        f"unscanned: {sorted(covered - SCANNED_TABLES)}, "
        f"absent: {sorted(SCANNED_TABLES - covered)}"
    )

    placed = cluster.place(blend)
    rewriter = StubRewriter()
    before_untouched = cluster.one(READ_ARTIFACT, (placed.untouched_id,))
    original_bindings = cluster.current_bindings(placed.blended_id)
    expected_original = frozenset(
        {placed.erased.client_id, *(tenant.client_id for tenant in placed.retained)}
    )
    assert original_bindings == expected_original, (
        "the placed bindings are the set the clause is stated against"
    )

    outcome = run_erasure(cluster.store, request_for(placed), seams(placed, rewriter))

    event(f"retained tenants={blend.retained_count}")
    event(f"erased segments={len(blend.erased_segments)}")
    event(f"retained segments={len(blend.retained_segments)}")
    event(f"erased segment leads the body={blend.segments[0].erased}")
    event(f"erased segment closes the body={blend.segments[-1].erased}")

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.run_id is not None
    assert outcome.fail_closed_rewrites == 0, (
        "the drawn blend fell to the fail-closed delete, so the surgical path went unexercised"
    )
    assert rewriter.documents == [placed.body], (
        "the rewrite was asked exactly once, about the body that was placed"
    )

    # Requirement 18.2 and 18.3: the row survives, rewritten rather than removed.
    assert cluster.count(COUNT_ARTIFACT, (placed.blended_id,)) == 1, (
        "the blended artifact was deleted rather than rewritten"
    )
    stored = cluster.one(READ_ARTIFACT, (placed.blended_id,))
    stored_body = str(stored[0])
    expected_body = honest_rewrite(placed.body, placed.erased_mentions)
    assert stored_body == expected_body, "the stored body is not the rewrite that was validated"
    assert stored_body != placed.body, "the body did not change at all"
    assert str(stored[1]) == digest_of(stored_body), "the recorded digest is not of the stored body"
    assert int(stored[2]) > 1, "a rewritten row carries a later revision"
    assert stored[3] is not None, "a rewritten row records when it was redacted"

    # Requirements 18.4 and 18.5: exactly the erased tenant's claim is gone, and it
    # is closed as history rather than removed outright.
    surviving = cluster.current_bindings(placed.blended_id)
    assert surviving == expected_original - {placed.erased.client_id}, (
        "the current binding set is not the original set less the erased tenant"
    )
    assert placed.erased.client_id in cluster.closed_bindings(placed.blended_id), (
        "the erased tenant's claim was deleted rather than closed as history"
    )

    # Requirement 18.6: both digests, both binding sets, and the count-only summary.
    recorded = cluster.one(READ_DISPOSITION, (outcome.run_id, placed.blended_id))
    assert str(recorded[0]) == DispositionKind.SURGICAL_REDACTION.value
    assert str(recorded[1]) == REWRITTEN_REASON
    assert str(recorded[2]) == digest_of(placed.body), "the pre-redaction digest is not recorded"
    assert str(recorded[3]) == digest_of(stored_body), "the post-redaction digest is not recorded"
    assert recorded[2] != recorded[3], "both digests are recorded and they differ"
    assert _as_slugs(recorded[4]) == frozenset(
        {placed.erased.slug, *(tenant.slug for tenant in placed.retained)}
    )
    assert _as_slugs(recorded[5]) == frozenset(tenant.slug for tenant in placed.retained)
    assert int(recorded[6]) == len(blend.erased_segments), "the removed segment count is wrong"
    assert int(recorded[7]) == len(blend.retained_segments), "the retained segment count is wrong"

    # The two Artifacts beside the blended one, so preservation is a comparison.
    assert cluster.count(COUNT_ARTIFACT, (placed.sole_id,)) == 0, (
        "the artifact the erased tenant held alone survived"
    )
    assert cluster.one(READ_ARTIFACT, (placed.untouched_id,)) == before_untouched, (
        "an artifact bound to no erased tenant was modified"
    )

    # The clause this property is really for: the pre-redaction body, and every
    # erased line of it, is present in no row of any table.
    assert cluster.leaks_of(placed.body) == {}, (
        "the pre-redaction body survives somewhere in the schema"
    )
    markers = markers_for(blend.retained_count)
    for segment in blend.erased_segments:
        assert cluster.leaks_of(segment.line(markers)) == {}, (
            "a segment of the erased tenant's content survives somewhere in the schema"
        )
    for segment in blend.retained_segments:
        assert "derived_artifact" in cluster.leaks_of(segment.line(markers)), (
            "a retained tenant's own segment was removed by the redaction"
        )
