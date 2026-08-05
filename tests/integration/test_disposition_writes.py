"""Phase three against a live instance: what each disposition actually does to rows.

The unit surface can assert the decision table and the shape of every statement. Only
a cluster can answer the five claims this module is about, and each of them is a claim
about stored rows rather than about a returned value.

**An Artifact only the erased Client holds a claim on leaves nothing behind.** The
row, its vectors, and its lineage edges in both directions are gone, and the
Disposition that survives it carries the digest and the binding slugs the row held
before it went, because after the delete that evidence exists nowhere else.

**A blended Artifact survives with exactly one binding closed.** The erased Client's
claim is closed rather than deleted, so the attribution history still says the claim
was withdrawn, and every other Client's current claim is untouched. That asymmetry is
the whole substance of the surgical case.

**Both digests are recorded and no pre-redaction body is stored anywhere.** The
Disposition holds the digest before and the digest after; the original text appears in
no column of any table that could hold it, which is asserted by looking rather than by
trusting the writing path.

**A rewrite that fails validation falls to the hard delete.** The fail-closed bias is
that blended memory is lost rather than an erased Client's content left in place, and
the reason recorded names that path rather than an ordinary sole binding.

**The optimistic guard refuses a body that moved.** A concurrent change between the
rewrite call and the transaction updates no row, and the transaction commits nothing.

**Validates: Requirements 18.1, 18.2, 18.4, 18.5, 18.6, 18.7, 18.8, 43.1, 44.7**
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.erase.disposition import (
    BINDING_ABSENT_REASON,
    EVENT_NOT_DIVISIBLE_REASON,
    REWRITTEN_REASON,
    SOLE_BINDING_REASON,
    Candidate,
    DispositionKind,
    RunOwnership,
    SurgicalWrite,
    classify,
    decide,
    fail_closed_delete,
    hard_delete,
    retain,
    surgical_redaction,
)
from molt.erase.rewriter import (
    FAIL_CLOSED_REASON,
    ClientIdentity,
    RatioBand,
    RewriteRequest,
    body_digest,
    rewrite,
)
from molt.errors import ModelUnavailableError
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind, DerivedArtifactKind
from molt.providers import Prompt, ProviderProbe, TextResult
from molt.store import Connection, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.store.embeddings import vector_text
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places directly. Phase three owns no Client insert, no Artifact
# insert, and no candidate insert, so everything a disposition acts on is placed here.
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
INSERT_LEASE: Final[str] = (
    "INSERT INTO erasure_lease (id, client_id, owner, generation, idempotency_key, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, now() + INTERVAL '1 hour')"
)
INSERT_REQUEST: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification) VALUES (%s, %s, %s, %s)"
)
INSERT_RUN: Final[str] = (
    "INSERT INTO erasure_run ("
    "id, request_id, client_id, requester, t_before, phase, fencing_generation, lease_id) "
    "VALUES (%s, %s, %s, %s, now(), 'disposition', %s, %s)"
)
INSERT_CANDIDATE: Final[str] = (
    "INSERT INTO erasure_candidate (run_id, artifact_id, artifact_kind, content_digest, "
    "selection_reason) VALUES (%s, %s, %s, %s, %s)"
)

# What every claim about stored rows is read from.
COUNT_ARTIFACT: Final[str] = "SELECT count(*) FROM derived_artifact WHERE id = %s"
COUNT_EVENT: Final[str] = "SELECT count(*) FROM ledger WHERE id = %s"
COUNT_EMBEDDINGS: Final[str] = "SELECT count(*) FROM embedding WHERE artifact_id = %s"
COUNT_EDGES: Final[str] = "SELECT count(*) FROM lineage_edge WHERE child_id = %s OR parent_id = %s"
COUNT_BINDING_ROWS: Final[str] = "SELECT count(*) FROM client_binding WHERE artifact_id = %s"
COUNT_CLOSED_BINDINGS: Final[str] = (
    "SELECT count(*) FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NOT NULL"
)
READ_CURRENT_SLUGS: Final[str] = (
    "SELECT coalesce(array_agg(cl.slug ORDER BY cl.slug), ARRAY[]::STRING[]) "
    "FROM client_binding AS b JOIN client AS cl ON cl.id = b.client_id "
    "WHERE b.artifact_id = %s AND b.superseded_by IS NULL"
)
READ_ARTIFACT: Final[str] = (
    "SELECT body, content_digest, revision, redacted_at, embedding_state "
    "FROM derived_artifact WHERE id = %s"
)
READ_DISPOSITION: Final[str] = (
    "SELECT disposition, reason, selection_reason, pre_digest, post_digest, "
    "bindings_before, bindings_after, fencing_generation "
    "FROM disposition WHERE run_id = %s AND artifact_id = %s"
)
COUNT_DISPOSITIONS: Final[str] = "SELECT count(*) FROM disposition WHERE run_id = %s"

# Whether a body appears anywhere at all. Every column of every table that can hold
# artifact text, provider text, or evidence text is counted in one statement, so the
# claim is *no row of any table* rather than *no row of the table we thought of*. The
# array columns are joined to text because a slug array could otherwise hide a body
# the writing path put there by mistake.
COUNT_BODY_OCCURRENCES: Final[str] = (
    "SELECT (SELECT count(*) FROM derived_artifact WHERE body LIKE %s) "
    "+ (SELECT count(*) FROM ledger "
    "WHERE coalesce(text_body, '') LIKE %s OR payload::STRING LIKE %s) "
    "+ (SELECT count(*) FROM session WHERE attribution::STRING LIKE %s) "
    "+ (SELECT count(*) FROM disposition "
    "WHERE reason LIKE %s OR selection_reason LIKE %s "
    "OR coalesce(pre_digest, '') LIKE %s OR coalesce(post_digest, '') LIKE %s "
    "OR array_to_string(bindings_before, ' ') LIKE %s "
    "OR array_to_string(bindings_after, ' ') LIKE %s) "
    "+ (SELECT count(*) FROM erasure_candidate WHERE selection_reason LIKE %s) "
    "+ (SELECT count(*) FROM residue_candidate "
    "WHERE coalesce(reasoning, '') LIKE %s OR coalesce(classification, '') LIKE %s) "
    "+ (SELECT count(*) FROM erasure_run WHERE coalesce(error_detail, '') LIKE %s) "
    "+ (SELECT count(*) FROM erasure_certificate WHERE payload::STRING LIKE %s) "
    "+ (SELECT count(*) FROM audit_log_snapshot WHERE records::STRING LIKE %s) "
    "+ (SELECT count(*) FROM working_memory WHERE value::STRING LIKE %s)"
)
_BODY_SEARCH_PARAMETERS: Final[int] = 17

# The values the placed rows carry. The two marker strings are what the rewrite
# validation reads, so they are distinct enough that a substring of one is not a
# substring of the other.
ERASED_MARKER: Final[str] = "ACME-INTERNAL"
RETAINED_MARKER: Final[str] = "ZENITH-INTERNAL"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"
DERIVATION_METHOD: Final[str] = "distilled"
OWNER: Final[str] = "stub-worker"
REQUESTER: Final[str] = "governance-owner"
JUSTIFICATION: Final[str] = "engagement concluded under a contractual purge obligation"
PROVIDER_NAME: Final[str] = "stub-text"
TEXT_MODEL: Final[str] = "stub-text-model"
EMBEDDING_PROVIDER: Final[str] = "stub-embedding"
EMBEDDING_MODEL: Final[str] = "stub-embedding-model"

# The ratio band every example applies. It is not the surface default, so an admitted
# replacement cannot be admitted by coincidence with a number this codebase holds.
EXAMPLE_RATIO_MINIMUM: Final[float] = 0.25

# The generation every fenced write in this module presents, matching the lease each
# example places.
LEASE_GENERATION: Final[int] = 1

# How long a placed row is retained for. A duration rather than an instant, because a
# stated instant would be a timestamp literal.
RETENTION: Final[timedelta] = timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture rather
# than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


@dataclass(slots=True)
class StubTextProvider:
    """A Text_Provider that answers from a rule rather than from a model.

    Constructed per example, because what makes a rewrite acceptable or unacceptable is
    the whole point of each example and a shared double would let one example decide
    another's answer. The three capability fields are settable rather than read-only
    because the protocol declares them as variables, and a frozen shape would satisfy
    the runtime and not the declared contract.
    """

    answer: str
    name: str = PROVIDER_NAME
    model_id: str = TEXT_MODEL
    supports_prompt_cache: bool = False
    fails: bool = False

    def generate(self, prompt: Prompt) -> TextResult:
        """Answer the prompt with the configured text, or refuse as a provider would."""
        assert prompt.stable_prefix, "a rewrite prompt carries fixed instructions"
        if self.fails:
            raise ModelUnavailableError("the stub provider refuses to answer")
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


@dataclass(frozen=True, slots=True)
class Fixture:
    """One run's worth of placed rows: two tenants, a lease, a request, and a run."""

    erased_id: UUID
    erased_slug: str
    retained_id: UUID
    retained_slug: str
    session_id: UUID
    run_id: UUID

    @property
    def ownership(self) -> RunOwnership:
        """The ownership every fenced write in an example presents."""
        return RunOwnership(
            run_id=self.run_id,
            client_id=self.erased_id,
            slug=self.erased_slug,
            generation=LEASE_GENERATION,
        )

    @property
    def context(self) -> SupersessionContext:
        """The Session context the closing supersession's Ledger Event is recorded within."""
        return SupersessionContext(
            session_id=self.session_id,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=datetime.now(UTC) + RETENTION,
        )


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

    def one(self, statement: str, params: tuple[object, ...]) -> tuple[Any, ...]:
        """The single row a statement is expected to produce."""
        produced = self.rows(statement, params)
        assert len(produced) == 1, f"the statement produced {len(produced)} rows where one was read"
        return produced[0]

    def count(self, statement: str, params: tuple[object, ...]) -> int:
        """The number one counting statement reports."""
        return int(self.one(statement, params)[0])

    def occurrences_of(self, needle: str) -> int:
        """How many rows of any body-bearing column hold this text."""
        pattern = f"%{needle}%"
        return int(self.one(COUNT_BODY_OCCURRENCES, (pattern,) * _BODY_SEARCH_PARAMETERS)[0])

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
        """Place one Ledger Event carrying text, with a chain digest nothing verifies here."""
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
        """Place one Derived_Artifact with a vector already recorded for it."""
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
                EMBEDDING_PROVIDER,
                EMBEDDING_MODEL,
                EMBEDDING_DIMENSION,
                vector_text(unit_vector()),
            ),
        )
        return identifier

    def bind(self, artifact_id: UUID, kind: ArtifactKind, client_id: UUID) -> UUID:
        """Place one current Attribution_Version for an Artifact and a Client."""
        identifier = uuid4()
        self.send(INSERT_BINDING, (identifier, artifact_id, kind.value, client_id, "marker", 0.9))
        return identifier

    def edge(self, child_id: UUID, parent_id: UUID, parent_kind: ArtifactKind) -> None:
        """Place one lineage edge from a parent of any kind to a Derived_Artifact."""
        self.send(INSERT_EDGE, (uuid4(), child_id, parent_id, parent_kind.value, DERIVATION_METHOD))

    def candidate(self, run_id: UUID, artifact_id: UUID, kind: ArtifactKind, digest: str) -> None:
        """Place one candidate row, as the explicit sweep would have."""
        self.send(INSERT_CANDIDATE, (run_id, artifact_id, kind.value, digest, "client_binding"))

    def run_for(self, client_id: UUID) -> UUID:
        """Place a lease, a request, and a run for one tenant, and return the run."""
        lease_id = uuid4()
        self.send(INSERT_LEASE, (lease_id, client_id, OWNER, LEASE_GENERATION, uuid4().hex))
        request_id = uuid4()
        self.send(INSERT_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION))
        run_id = uuid4()
        self.send(
            INSERT_RUN,
            (run_id, request_id, client_id, REQUESTER, LEASE_GENERATION, lease_id),
        )
        return run_id

    def fixture(self) -> Fixture:
        """Two tenants, a Session, and a run under a current lease held at generation one."""
        erased_id, erased_slug = self.client(ERASED_MARKER)
        retained_id, retained_slug = self.client(RETAINED_MARKER)
        return Fixture(
            erased_id=erased_id,
            erased_slug=erased_slug,
            retained_id=retained_id,
            retained_slug=retained_slug,
            session_id=self.session(retained_id),
            run_id=self.run_for(erased_id),
        )


def unit_vector() -> tuple[float, ...]:
    """A vector of the schema's width whose norm is one, so the write is admitted."""
    return (1.0, *(0.0 for _ in range(EMBEDDING_DIMENSION - 1)))


def example_band() -> RatioBand:
    """The length band every example admits inside, read from a configuration."""
    return RatioBand.from_configuration(
        Configuration(
            environ={"MOLT_REWRITE_LENGTH_RATIO_MIN": str(EXAMPLE_RATIO_MINIMUM)},
            file_values={},
        )
    )


def blended_body(erased_marker: str, retained_marker: str) -> str:
    """A body carrying one line per tenant, so a rewrite has something to remove."""
    return (
        f"{retained_marker} the deployment pipeline runs the migration gate first\n"
        f"{erased_marker} the invoicing exporter reconciles against the ledger\n"
        f"{retained_marker} the recall path reads the covering index rather than the row\n"
    )


def redacted_body(retained_marker: str) -> str:
    """What a well-behaved rewrite answers with: the erased line gone, the rest intact."""
    return (
        f"{retained_marker} the deployment pipeline runs the migration gate first\n"
        f"{retained_marker} the recall path reads the covering index rather than the row\n"
    )


def identities(fixture: Fixture) -> tuple[ClientIdentity, tuple[ClientIdentity, ...]]:
    """The erased identity and the retained identities the validation reads."""
    erased = ClientIdentity(
        client_id=fixture.erased_id,
        slug=fixture.erased_slug,
        display_name=f"Tenant {fixture.erased_slug}",
        content_markers=(ERASED_MARKER,),
    )
    retained = (
        ClientIdentity(
            client_id=fixture.retained_id,
            slug=fixture.retained_slug,
            display_name=f"Tenant {fixture.retained_slug}",
            content_markers=(RETAINED_MARKER,),
        ),
    )
    return erased, retained


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema.

    Every migration is applied because the fence's generation column, the lease table,
    and the restricting evidence references all arrive in later generations than the
    tables a disposition acts on.
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


def classified(cluster: Cluster, fixture: Fixture, artifact_id: UUID) -> Candidate:
    """The one classified candidate an example is about, read from the cluster."""
    found = [
        candidate
        for candidate in classify(cluster.store, fixture.ownership)
        if candidate.artifact_id == artifact_id
    ]
    assert len(found) == 1, "the candidate set names this artifact exactly once"
    return found[0]


def example_configuration() -> Configuration:
    """A configuration naming a batch size that is not the surface default."""
    return Configuration(environ={"MOLT_ERASURE_BATCH_SIZE": "2"}, file_values={})


# ---------------------------------------------------------------------------
# The hard delete
# ---------------------------------------------------------------------------


def test_an_artifact_bound_only_to_the_erased_client_is_hard_deleted(cluster: Cluster) -> None:
    """The row, its vectors, its edges in both directions, and its bindings all go."""
    fixture = cluster.fixture()
    body = f"{ERASED_MARKER} the exporter reconciles the invoice ledger nightly"
    artifact_id = cluster.artifact(fixture.erased_id, body)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
    cluster.edge(artifact_id, fixture.session_id, ArtifactKind.SESSION)
    descendant_id = cluster.artifact(fixture.retained_id, f"{RETAINED_MARKER} a later summary")
    cluster.edge(descendant_id, artifact_id, ArtifactKind.DERIVED_ARTIFACT)
    cluster.candidate(fixture.run_id, artifact_id, ArtifactKind.DERIVED_ARTIFACT, body_digest(body))

    decision = decide(classified(cluster, fixture, artifact_id))
    deleted = hard_delete(
        cluster.store,
        fixture.ownership,
        [decision],
        configuration=example_configuration(),
    )

    assert decision.disposition is DispositionKind.HARD_DELETE
    assert decision.reason == SOLE_BINDING_REASON
    assert deleted == 1
    assert cluster.count(COUNT_ARTIFACT, (artifact_id,)) == 0
    assert cluster.count(COUNT_EMBEDDINGS, (artifact_id,)) == 0
    assert cluster.count(COUNT_EDGES, (artifact_id, artifact_id)) == 0
    assert cluster.count(COUNT_BINDING_ROWS, (artifact_id,)) == 0
    assert cluster.count(COUNT_ARTIFACT, (descendant_id,)) == 1, (
        "another tenant's artifact is not carried away by the edge removal"
    )

    recorded = cluster.one(READ_DISPOSITION, (fixture.run_id, artifact_id))
    assert recorded[0] == DispositionKind.HARD_DELETE.value
    assert recorded[1] == SOLE_BINDING_REASON
    assert recorded[3] == body_digest(body), "the pre-deletion digest survives the row"
    assert recorded[4] is None
    assert list(recorded[5]) == [fixture.erased_slug], "the bound slugs survive the row"
    assert int(recorded[7]) == LEASE_GENERATION


def test_a_blended_event_is_hard_deleted_because_an_event_is_not_divisible(
    cluster: Cluster,
) -> None:
    """An Event body is one Client's own recorded act, so there is nothing to redact."""
    fixture = cluster.fixture()
    body = f"{ERASED_MARKER} and {RETAINED_MARKER} appear in one recorded response"
    event_id = cluster.event(fixture.session_id, fixture.erased_id, 1, body)
    cluster.bind(event_id, ArtifactKind.EVENT, fixture.erased_id)
    cluster.bind(event_id, ArtifactKind.EVENT, fixture.retained_id)
    cluster.candidate(fixture.run_id, event_id, ArtifactKind.EVENT, body_digest(body))

    decision = decide(classified(cluster, fixture, event_id))
    hard_delete(
        cluster.store,
        fixture.ownership,
        [decision],
        configuration=example_configuration(),
    )

    assert decision.reason == EVENT_NOT_DIVISIBLE_REASON
    assert cluster.count(COUNT_EVENT, (event_id,)) == 0
    recorded = cluster.one(READ_DISPOSITION, (fixture.run_id, event_id))
    assert recorded[0] == DispositionKind.HARD_DELETE.value
    assert sorted(recorded[5]) == sorted([fixture.erased_slug, fixture.retained_slug])


# ---------------------------------------------------------------------------
# The surgical redaction
# ---------------------------------------------------------------------------


def test_a_blended_artifact_survives_with_only_the_erased_binding_closed(
    cluster: Cluster,
) -> None:
    """The row stays, the erased claim is closed as history, other claims are untouched."""
    fixture = cluster.fixture()
    body = blended_body(ERASED_MARKER, RETAINED_MARKER)
    artifact_id = cluster.artifact(fixture.retained_id, body)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
    cluster.candidate(fixture.run_id, artifact_id, ArtifactKind.DERIVED_ARTIFACT, body_digest(body))
    erased, retained = identities(fixture)
    decision = decide(classified(cluster, fixture, artifact_id))

    replacement = rewrite(
        StubTextProvider(answer=redacted_body(RETAINED_MARKER)),
        RewriteRequest(artifact_id=artifact_id, body=body, erased=erased, retained=retained),
        band=example_band(),
    )
    record = surgical_redaction(
        cluster.store,
        fixture.ownership,
        SurgicalWrite(
            decision=decision,
            replacement=replacement,
            vector=unit_vector(),
            embedding_provider=EMBEDDING_PROVIDER,
            embedding_model_id=EMBEDDING_MODEL,
            expires_at=datetime.now(UTC) + RETENTION,
            context=fixture.context,
        ),
    )

    assert decision.disposition is DispositionKind.SURGICAL_REDACTION
    assert record is not None
    stored = cluster.one(READ_ARTIFACT, (artifact_id,))
    assert stored[0] == replacement.text
    assert stored[1] == replacement.digest
    assert int(stored[2]) == 2, "a rewrite is a revision of the row rather than a replacement of it"
    assert stored[3] is not None, "the row records that it was redacted"
    assert stored[4] == "pending", "the replacement owes a vector under the run's own provider"
    assert list(cluster.one(READ_CURRENT_SLUGS, (artifact_id,))[0]) == [fixture.retained_slug]
    assert cluster.count(COUNT_CLOSED_BINDINGS, (artifact_id, fixture.erased_id)) >= 1, (
        "the withdrawal is recorded as history rather than as a hole"
    )
    assert cluster.count(COUNT_EMBEDDINGS, (artifact_id,)) == 1

    recorded = cluster.one(READ_DISPOSITION, (fixture.run_id, artifact_id))
    assert recorded[0] == DispositionKind.SURGICAL_REDACTION.value
    assert recorded[1] == REWRITTEN_REASON
    assert sorted(recorded[5]) == sorted([fixture.erased_slug, fixture.retained_slug])
    assert list(recorded[6]) == [fixture.retained_slug]
    assert int(recorded[7]) == LEASE_GENERATION


def test_both_digests_are_recorded_and_the_pre_redaction_body_is_stored_nowhere(
    cluster: Cluster,
) -> None:
    """The evidence is two digests, and the original text is in no row of any table."""
    fixture = cluster.fixture()
    needle = "QUARTERLY-RECONCILIATION-NOTE"
    body = (
        f"{RETAINED_MARKER} the migration gate runs before the deployment completes\n"
        f"{ERASED_MARKER} {needle} closes the invoicing period against the ledger\n"
        f"{RETAINED_MARKER} the recall path reads the covering index rather than the row\n"
    )
    artifact_id = cluster.artifact(fixture.retained_id, body)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
    cluster.candidate(fixture.run_id, artifact_id, ArtifactKind.DERIVED_ARTIFACT, body_digest(body))
    erased, retained = identities(fixture)
    assert cluster.occurrences_of(needle) == 1, "the original is stored once before the rewrite"

    replacement = rewrite(
        StubTextProvider(answer=redacted_body(RETAINED_MARKER)),
        RewriteRequest(artifact_id=artifact_id, body=body, erased=erased, retained=retained),
        band=example_band(),
    )
    record = surgical_redaction(
        cluster.store,
        fixture.ownership,
        SurgicalWrite(
            decision=decide(classified(cluster, fixture, artifact_id)),
            replacement=replacement,
            vector=unit_vector(),
            embedding_provider=EMBEDDING_PROVIDER,
            embedding_model_id=EMBEDDING_MODEL,
            expires_at=datetime.now(UTC) + RETENTION,
            context=fixture.context,
        ),
    )

    assert record is not None
    assert record.pre_digest == body_digest(body)
    assert record.post_digest == body_digest(replacement.text)
    recorded = cluster.one(READ_DISPOSITION, (fixture.run_id, artifact_id))
    assert recorded[3] == body_digest(body)
    assert recorded[4] == body_digest(replacement.text)
    assert cluster.occurrences_of(needle) == 0, "no table holds the pre-redaction body"
    assert cluster.occurrences_of(ERASED_MARKER) == 0, "and none holds the erased marker either"


def test_a_body_that_moved_since_the_rewrite_updates_nothing(cluster: Cluster) -> None:
    """The optimistic guard is what stops a rewrite overwriting a change it never saw."""
    fixture = cluster.fixture()
    body = blended_body(ERASED_MARKER, RETAINED_MARKER)
    artifact_id = cluster.artifact(fixture.retained_id, body)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
    cluster.candidate(fixture.run_id, artifact_id, ArtifactKind.DERIVED_ARTIFACT, body_digest(body))
    erased, retained = identities(fixture)
    decision = decide(classified(cluster, fixture, artifact_id))
    replacement = rewrite(
        StubTextProvider(answer=redacted_body(RETAINED_MARKER)),
        RewriteRequest(artifact_id=artifact_id, body=body, erased=erased, retained=retained),
        band=example_band(),
    )
    write = SurgicalWrite(
        decision=decision,
        replacement=replacement,
        vector=unit_vector(),
        embedding_provider=EMBEDDING_PROVIDER,
        embedding_model_id=EMBEDDING_MODEL,
        expires_at=datetime.now(UTC) + RETENTION,
        context=fixture.context,
    )
    assert surgical_redaction(cluster.store, fixture.ownership, write) is not None

    repeated = surgical_redaction(cluster.store, fixture.ownership, write)

    assert repeated is None, "the digest the guard matches on is no longer the stored one"
    assert cluster.one(READ_ARTIFACT, (artifact_id,))[1] == replacement.digest
    assert int(cluster.one(READ_ARTIFACT, (artifact_id,))[2]) == 2, "the row moved once, not twice"


# ---------------------------------------------------------------------------
# The fail-closed path, and the retention
# ---------------------------------------------------------------------------


def test_a_rewrite_validation_failure_falls_back_to_a_hard_delete(cluster: Cluster) -> None:
    """A model that answers badly is unavailability, and the reason recorded says so."""
    fixture = cluster.fixture()
    body = blended_body(ERASED_MARKER, RETAINED_MARKER)
    artifact_id = cluster.artifact(fixture.retained_id, body)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
    cluster.candidate(fixture.run_id, artifact_id, ArtifactKind.DERIVED_ARTIFACT, body_digest(body))
    erased, retained = identities(fixture)
    candidate = classified(cluster, fixture, artifact_id)
    assert decide(candidate).disposition is DispositionKind.SURGICAL_REDACTION

    with pytest.raises(ModelUnavailableError, match="ratio band"):
        rewrite(
            StubTextProvider(answer="done"),
            RewriteRequest(artifact_id=artifact_id, body=body, erased=erased, retained=retained),
            band=example_band(),
        )
    fallback = fail_closed_delete(candidate)
    hard_delete(
        cluster.store,
        fixture.ownership,
        [fallback],
        configuration=example_configuration(),
    )

    assert cluster.count(COUNT_ARTIFACT, (artifact_id,)) == 0
    assert cluster.count(COUNT_EMBEDDINGS, (artifact_id,)) == 0
    recorded = cluster.one(READ_DISPOSITION, (fixture.run_id, artifact_id))
    assert recorded[0] == DispositionKind.HARD_DELETE.value
    assert recorded[1] == FAIL_CLOSED_REASON
    assert sorted(recorded[5]) == sorted([fixture.erased_slug, fixture.retained_slug])


def test_an_artifact_the_erased_client_no_longer_holds_is_retained_with_a_reason(
    cluster: Cluster,
) -> None:
    """A candidate with no current claim for the erased Client is evidence, not a mutation."""
    fixture = cluster.fixture()
    body = f"{RETAINED_MARKER} the covering index answers the tenancy filter"
    artifact_id = cluster.artifact(fixture.retained_id, body)
    cluster.bind(artifact_id, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
    cluster.candidate(fixture.run_id, artifact_id, ArtifactKind.DERIVED_ARTIFACT, body_digest(body))

    decision = decide(classified(cluster, fixture, artifact_id))
    retain(cluster.store, fixture.ownership, decision)

    assert decision.disposition is DispositionKind.RETAINED
    assert decision.reason == BINDING_ABSENT_REASON
    assert cluster.count(COUNT_ARTIFACT, (artifact_id,)) == 1
    assert cluster.one(READ_ARTIFACT, (artifact_id,))[0] == body
    recorded = cluster.one(READ_DISPOSITION, (fixture.run_id, artifact_id))
    assert recorded[0] == DispositionKind.RETAINED.value
    assert list(recorded[5]) == [fixture.retained_slug]
    assert list(recorded[6]) == [fixture.retained_slug]
