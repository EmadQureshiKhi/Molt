"""The recall page against a live instance: is it index-served, and is it correct?

The recall query is the one read in this system that sits on a person's critical
path, and it is built out of a compromise this cluster forces. Any predicate on a
column other than the vector takes the plan off the distributed vector index, and
the tenancy admission is exactly such a predicate. The statement therefore ranks a
candidate pool by the ordering expression alone and admits from that pool, which
buys the index and gives up completeness beyond the pool's horizon. This module
asserts both halves of that: the plan really does use the index, and the answer is
really restricted to what the caller may see.

Five claims, in the order they build on each other.

**The candidate stage is served by the distributed vector index.** Asserted from
the plan rather than from the statement's shape, because the shape is only evidence
if the planner agrees with it. If a future cluster inlined the stage and pushed the
tenancy admission into it, this is the assertion that would fail, and the failure
would be the whole point.

**Every returned Artifact carries a permitted binding.** A second tenant's corpus
sits in the same schema, close to the query vector, and never appears.

**The provenance on each result is the stored provenance.** The Session identifier,
the machine identifier, the instant, and the outcome are read back from the rows
they came from and compared, so Requirement 13.2 and 13.3 are asserted against
storage rather than against the shape of the answer.

**A Learned_Procedure below the configured floor is excluded from the page and
retained in the database.** Both halves, because exclusion from recall is not a
soft delete: erasure still has to reach the row.

**The two forms agree.** The exact-scan fallback answers the same page as the
index-served form over the same corpus, so a tier without the index answers the
same question more slowly rather than a different question quickly.

The recall Event and the retrieval record are asserted here as well, because both
are writes and a write is only real once a cluster has taken it. The retrieval is
asserted twice over: once through an injected recorder, which is what a caller
holding its own transaction discipline supplies, and once through the default,
which is the Confidence_Tracker's own write and the only one that discharges the
requirement for a caller that supplies nothing.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.models.artifact import (
    EMBEDDING_DIMENSION,
    ArtifactKind,
    DerivedArtifact,
    DerivedArtifactKind,
)
from molt.models.event import EmbeddingState, Event, EventCategory
from molt.models.session import Session, SessionOutcome
from molt.recall import RecallEngine
from molt.store import Connection, Cursor, MemoryStore
from molt.store.capability import VECTOR_INDEX, Capability, CapabilityRecord
from molt.store.chain import LedgerAppend, append
from molt.store.embeddings import (
    DEFAULT_EXCERPT_CHARACTERS,
    RECALL_STATEMENT,
    EmbeddingWrite,
    RecallRow,
    select_recall_page,
    vector_text,
    write_derived_artifact,
    write_embedding,
)
from molt.store.lineage import ParentRef, insert_lineage_edge
from molt.store.migrate import apply_migrations
from molt.store.sessions import upsert_session

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes the fixtures make, parameterised in full. This module owns no
# Client insert and no attribution write, so those rows are placed here.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction, retention_interval) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) VALUES (%s, %s, %s, %s, 'scope', 1.0, %s)"
)

# The reads the assertions make against storage rather than against the answer.
COUNT_PROCEDURE: Final[str] = (
    "SELECT count(*) FROM derived_artifact WHERE id = %s AND procedure_confidence < %s"
)
SELECT_RECALL_EVENTS: Final[str] = (
    "SELECT payload FROM ledger WHERE session_id = %s AND category = 'recall'"
)
COUNT_RETRIEVALS: Final[str] = (
    "SELECT count(*) FROM procedure_retrieval WHERE procedure_id = %s AND session_id = %s"
)

# What the plan calls the vector index search, the index it names when it takes
# it, and the partial current-version index the tenancy admission is served from.
# The plan is lowercased before the comparison, so these are the lowercase
# spellings.
VECTOR_SEARCH: Final[str] = "vector search"
VECTOR_INDEX_NAME: Final[str] = "embedding@embedding_vec_idx"
CURRENT_BINDING_INDEX: Final[str] = "client_binding@binding_current_unique"

# The provider and the model every embedding row this module writes records.
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# The machine each tenant's Session records, distinct so a result's provenance is
# checkable rather than coincidentally right.
PERMITTED_MACHINE: Final[str] = "permitted-machine"
OTHER_MACHINE: Final[str] = "other-machine"
AGENT_CLI: Final[str] = "a-coding-agent"

# The floor under test, and the three standings placed either side of it.
RECALL_FLOOR: Final[float] = 0.15
STRONG_CONFIDENCE: Final[float] = 0.90
WEAK_CONFIDENCE: Final[float] = 0.40
BELOW_FLOOR_CONFIDENCE: Final[float] = 0.05

# How many decoy vectors the unpermitted tenant holds. Larger than the page, and
# placed nearer the query than most of the permitted corpus, so a tenancy filter
# that failed would be visible in the answer rather than merely possible.
DECOY_COUNT: Final[int] = 24

# How many results each example reads.
PAGE: Final[int] = 6

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


def unit_vector(label: str) -> tuple[float, ...]:
    """A reproducible unit vector of the fixed width, derived from a label.

    Every component carries part of the vector, so the corpus occupies the width
    the column declares and a ranking over it is not a restatement of an angle
    chosen here. The same label always yields the same vector, so no example
    depends on a random draw.
    """
    needed = EMBEDDING_DIMENSION * 4
    blocks: list[bytes] = []
    counter = 0
    while sum(len(block) for block in blocks) < needed:
        blocks.append(hashlib.sha256(label.encode() + counter.to_bytes(8, "big")).digest())
        counter += 1
    raw = struct.unpack(f">{EMBEDDING_DIMENSION}i", b"".join(blocks)[:needed])
    scaled = [value / 2147483648.0 for value in raw]
    norm = math.sqrt(math.fsum(component * component for component in scaled))
    return tuple(component / norm for component in scaled)


def blended(
    first: tuple[float, ...],
    second: tuple[float, ...],
    weight: float,
) -> tuple[float, ...]:
    """A unit vector between two others, so a corpus can be placed at chosen distances."""
    mixed = [a * (1.0 - weight) + b * weight for a, b in zip(first, second, strict=True)]
    norm = math.sqrt(math.fsum(component * component for component in mixed))
    return tuple(component / norm for component in mixed)


class StubQueryEmbedder:
    """An embedding surface answering the same vector the corpus was placed around."""

    def __init__(self, vector: tuple[float, ...]) -> None:
        self._vector = vector
        self.asked: list[str] = []

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Answer the fixed query vector once per text, recording what was asked."""
        self.asked.extend(texts)
        return [self._vector for _ in texts]


@dataclass(slots=True)
class RecordedRetrievals:
    """The retrieval records the Confidence_Tracker seam was handed."""

    seen: list[tuple[UUID, UUID]]

    def record(self, procedure_id: UUID, session_id: UUID) -> None:
        """Keep one retrieval, as the tracker's own recorder would write one."""
        self.seen.append((procedure_id, session_id))


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


@dataclass(frozen=True, slots=True)
class Placed:
    """The corpus one tenant may see, and the identities the assertions name."""

    query: tuple[float, ...]
    permitted_client: UUID
    other_client: UUID
    permitted_session: UUID
    other_session: UUID
    event_artifact: UUID
    strong_procedure: UUID
    weak_procedure: UUID
    excluded_procedure: UUID
    permitted_ids: frozenset[UUID]


class Corpus:
    """A schema holding every migration, a store over it, and the writes placed in it."""

    def __init__(self, store: MemoryStore, connection: DriverConnection) -> None:
        self.store = store
        self.connection = connection

    # -- placement -------------------------------------------------------

    def tenant(self, slug: str) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (identifier, slug, "Tenant", "eu", RETENTION),
        )
        return identifier

    def session(self, client_id: UUID, machine_id: str, outcome: SessionOutcome) -> UUID:
        """Place one Session for a tenant, at a stated terminal outcome."""
        record = Session(
            id=uuid4(),
            client_id=client_id,
            agent_cli=AGENT_CLI,
            machine_id=machine_id,
            team_id=None,
            attribution={"principal": "a-principal"},
            workspace_path="/workspace",
            started_at=MOMENT,
            ended_at=MOMENT + timedelta(minutes=1),
            outcome=outcome,
            parent_session_id=None,
            spawning_event_id=None,
            depth=0,
            tool_call_count=0,
            model_request_count=0,
            error_count=0,
            token_count=0,
            cost_usd=Decimal("0.000000"),
            halted=False,
            halted_at=None,
            halt_reason=None,
            halt_rule_id=None,
        )
        upsert_session(self.store, record)
        return record.id

    def attribute(self, artifact_id: UUID, kind: ArtifactKind, client_id: UUID) -> None:
        """Attribute one Artifact to one tenant as a current Attribution_Version."""
        send(
            self.connection,
            INSERT_BINDING,
            (uuid4(), artifact_id, kind.value, client_id, MOMENT),
        )

    def event(
        self,
        session_id: UUID,
        client_id: UUID,
        machine_id: str,
        text: str,
        vector: tuple[float, ...],
        *,
        attribute_to: UUID | None = None,
    ) -> UUID:
        """Append one Event, give it a vector, and attribute it."""
        event = Event(
            id=uuid4(),
            session_id=session_id,
            client_id=client_id,
            category=EventCategory.TOOL_CALL,
            occurred_at=MOMENT,
            agent_cli=AGENT_CLI,
            machine_id=machine_id,
            parent_event_id=None,
            payload={"tool": "a-tool"},
            redacted=False,
            text_body=text,
        )
        append(
            self.store,
            LedgerAppend(
                event=event,
                expires_at=MOMENT + RETENTION,
                embedding_state=EmbeddingState.PENDING,
            ),
        )
        write_embedding(
            self.store,
            EmbeddingWrite(
                artifact_id=event.id,
                artifact_kind=ArtifactKind.EVENT,
                client_id=client_id,
                provider=PROVIDER,
                model_id=MODEL,
                vec=vector,
                expires_at=MOMENT + RETENTION,
            ),
        )
        self.attribute(event.id, ArtifactKind.EVENT, attribute_to or client_id)
        return event.id

    def procedure(
        self,
        session_id: UUID,
        client_id: UUID,
        confidence: float,
        vector: tuple[float, ...],
        label: str,
    ) -> UUID:
        """Write one Learned_Procedure, link it to its Session, and attribute it."""
        record = DerivedArtifact(
            id=uuid4(),
            kind=DerivedArtifactKind.LEARNED_PROCEDURE,
            owner_client_id=client_id,
            body=f"the procedure distilled as {label}",
            content_digest=digest_of(label),
            derivation_method="distil",
            revision=1,
            created_at=MOMENT,
            updated_at=MOMENT,
            redacted_at=None,
            embedding_state=EmbeddingState.EMBEDDED,
            expires_at=MOMENT + RETENTION,
            procedure_confidence=confidence,
        )
        write_derived_artifact(
            self.store,
            record,
            embedding=EmbeddingWrite(
                artifact_id=record.id,
                artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                client_id=client_id,
                provider=PROVIDER,
                model_id=MODEL,
                vec=vector,
                expires_at=MOMENT + RETENTION,
            ),
        )
        insert_lineage_edge(
            self.store,
            record.id,
            ParentRef(
                parent_id=session_id,
                parent_kind=ArtifactKind.SESSION,
                derivation_method="distil",
            ),
        )
        self.attribute(record.id, ArtifactKind.DERIVED_ARTIFACT, client_id)
        return record.id

    # -- reads the assertions make --------------------------------------

    def plan_of(self, statement: str, params: tuple[object, ...]) -> str:
        """The plan the cluster produces for one statement, lowercased."""
        with self.connection.cursor() as cursor:
            cursor.execute("EXPLAIN " + statement, params)
            rows = cursor.fetchall()
        return "\n".join(" ".join(str(column) for column in row) for row in rows).lower()

    def scalar(self, statement: str, params: tuple[object, ...]) -> object:
        """The first column of the one row a read returns."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            row = cursor.fetchone()
        assert row is not None
        return row[0]

    def rows_of(self, statement: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        """Every row a read returns."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            return list(cursor.fetchall())

    def collect_statistics(self) -> None:
        """Collect table statistics, so the planner sees the corpus rather than a guess."""
        with self.connection.cursor() as cursor:
            cursor.execute("ANALYZE embedding")
            cursor.execute("ANALYZE client_binding")
            cursor.execute("ANALYZE derived_artifact")


@pytest.fixture(scope="module")
def corpus(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Corpus]:
    """Apply every migration, then build a store whose connections see that schema."""
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
        yield Corpus(store=store, connection=fresh_schema)


@pytest.fixture(scope="module")
def placed(corpus: Corpus) -> Placed:
    """Two tenants' content, placed once and searched by every example here.

    The permitted tenant holds one Event and three Learned_Procedures, two of them
    at one distance so the standing decides their order and one below the floor. The
    unpermitted tenant holds a crowd of vectors placed nearer the query than any of
    those, so a page that leaked would leak visibly.
    """
    query = unit_vector("the intended action")
    away = unit_vector("something else entirely")

    permitted_client = corpus.tenant(f"permitted-{uuid4().hex[:8]}")
    other_client = corpus.tenant(f"other-{uuid4().hex[:8]}")
    permitted_session = corpus.session(
        permitted_client, PERMITTED_MACHINE, SessionOutcome.SUCCEEDED
    )
    other_session = corpus.session(other_client, OTHER_MACHINE, SessionOutcome.FAILED)

    event_artifact = corpus.event(
        permitted_session,
        permitted_client,
        PERMITTED_MACHINE,
        "the earlier attempt at the same action",
        blended(query, away, 0.10),
    )
    tied = blended(query, away, 0.30)
    strong = corpus.procedure(
        permitted_session, permitted_client, STRONG_CONFIDENCE, tied, "the trusted procedure"
    )
    weak = corpus.procedure(
        permitted_session, permitted_client, WEAK_CONFIDENCE, tied, "the doubted procedure"
    )
    excluded = corpus.procedure(
        permitted_session,
        permitted_client,
        BELOW_FLOOR_CONFIDENCE,
        blended(query, away, 0.05),
        "the discredited procedure",
    )

    for step in range(DECOY_COUNT):
        corpus.event(
            other_session,
            other_client,
            OTHER_MACHINE,
            f"another tenant's attempt {step}",
            blended(query, away, 0.02 + (step * 0.0005)),
        )
    corpus.collect_statistics()

    return Placed(
        query=query,
        permitted_client=permitted_client,
        other_client=other_client,
        permitted_session=permitted_session,
        other_session=other_session,
        event_artifact=event_artifact,
        strong_procedure=strong,
        weak_procedure=weak,
        excluded_procedure=excluded,
        permitted_ids=frozenset({event_artifact, strong, weak}),
    )


# The candidate pool every example reads with, stated here so the plan assertion
# and the page assertions bind the same value.
POOL: Final[int] = 256


def page_of(
    corpus: Corpus,
    placed: Placed,
    *,
    index_served: bool = True,
) -> tuple[tuple[RecallRow, ...], int]:
    """The recall page and the exclusion tally, in one of the two statement forms."""

    def body(cursor: Cursor) -> tuple[tuple[RecallRow, ...], int]:
        return select_recall_page(
            cursor,
            placed.query,
            permitted_clients=[placed.permitted_client],
            limit=PAGE,
            recall_floor=RECALL_FLOOR,
            candidate_pool=POOL,
            index_served=index_served,
        )

    return corpus.store.read(body)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_the_candidate_stage_of_the_recall_page_is_served_by_the_vector_index(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The staging exists to keep the index, so the plan is what proves it kept it.

    The tenancy admission reads the candidate stage's output rather than the
    Embedding table, so the ordering stays the only restriction on the scan the
    index answers. A planner that inlined the stage and pushed the admission into
    it would take the plan off the index, and this assertion is what would say so.

    Three readings are taken together. The plan holds a vector search node, that
    node names the distributed vector index, and the count it searches for is the
    candidate pool the caller sized, so the pool really is the bound the index was
    asked to satisfy rather than a filter applied afterwards. The admission is then
    read from the partial index over current Attribution_Versions, which is the
    stage the tenancy term belongs to and the reason it is not in the vector one.
    """
    rendered = vector_text(placed.query)
    excerpt = DEFAULT_EXCERPT_CHARACTERS
    plan = corpus.plan_of(
        RECALL_STATEMENT,
        (
            rendered,
            rendered,
            POOL,
            [placed.permitted_client],
            excerpt,
            excerpt,
            RECALL_FLOOR,
            PAGE,
        ),
    )

    assert VECTOR_SEARCH in plan, plan
    assert VECTOR_INDEX_NAME in plan, plan
    assert f"target count: {POOL}" in plan, plan
    assert CURRENT_BINDING_INDEX in plan, plan


# ---------------------------------------------------------------------------
# Tenancy, provenance, and the floor
# ---------------------------------------------------------------------------


def test_every_returned_artifact_carries_a_permitted_binding(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """A crowd of nearer vectors belonging to another tenant never reaches the page."""
    rows, _ = page_of(corpus, placed)

    assert rows
    assert {row.artifact_id for row in rows} <= placed.permitted_ids
    assert all(row.client_id == placed.permitted_client for row in rows)


def test_each_result_carries_the_provenance_the_stored_rows_hold(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The Session, the machine, the instant, and the outcome are storage's own."""
    rows, _ = page_of(corpus, placed)

    for row in rows:
        assert row.session_id == placed.permitted_session
        assert row.machine_id == PERMITTED_MACHINE
        assert row.occurred_at == MOMENT
        assert row.outcome == SessionOutcome.SUCCEEDED.value
        assert row.excerpt


def test_the_page_is_ordered_by_distance_then_by_standing_then_by_identifier(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """Two procedures at one distance are separated by the standing, higher first."""
    rows, _ = page_of(corpus, placed)

    distances = [row.cosine_distance for row in rows]
    assert distances == sorted(distances)
    ranked = [row.artifact_id for row in rows]
    assert ranked.index(placed.strong_procedure) < ranked.index(placed.weak_procedure)


def test_a_procedure_below_the_floor_is_absent_from_the_page_and_still_stored(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """Exclusion from recall is not a soft delete: the row stays inside erasure's reach."""
    rows, excluded = page_of(corpus, placed)

    assert placed.excluded_procedure not in {row.artifact_id for row in rows}
    assert excluded == 1
    retained = corpus.scalar(COUNT_PROCEDURE, (placed.excluded_procedure, RECALL_FLOOR))
    assert isinstance(retained, int)
    assert retained == 1


def test_the_floor_does_not_shorten_the_page_it_only_changes_who_fills_it(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The excluded procedure is nearest of all, and the page is still full.

    It sits closer to the query than every admitted result, so a floor applied
    after truncation would have taken a position and given nothing back. Applied
    inside the predicate it takes none.
    """
    rows, _ = page_of(corpus, placed)

    assert len(rows) == len(placed.permitted_ids)


# ---------------------------------------------------------------------------
# The two forms
# ---------------------------------------------------------------------------


def test_the_exact_scan_form_answers_the_same_page_as_the_index_served_form(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """A tier without the index answers the same question more slowly, not another one."""
    served, served_excluded = page_of(corpus, placed)
    scanned, scanned_excluded = page_of(corpus, placed, index_served=False)

    assert [row.artifact_id for row in scanned] == [row.artifact_id for row in served]
    assert [row.cosine_distance for row in scanned] == [row.cosine_distance for row in served]
    assert scanned_excluded == served_excluded


def test_the_engine_takes_the_fallback_form_when_the_record_reports_no_index(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The choice is a recorded probe result, so priming the record changes the form."""
    corpus.store.prime_capabilities(CapabilityRecord((Capability(VECTOR_INDEX, available=False),)))
    try:
        engine = RecallEngine(
            corpus.store,
            StubQueryEmbedder(placed.query),
            recall_floor=RECALL_FLOOR,
        )
        results = engine.recall(
            "the intended action",
            PAGE,
            permitted=[placed.permitted_client],
        )
    finally:
        corpus.store.prime_capabilities(CapabilityRecord(()))

    assert {result.artifact_id for result in results} == placed.permitted_ids


# ---------------------------------------------------------------------------
# What the answer owes
# ---------------------------------------------------------------------------


def test_the_engine_appends_one_recall_event_and_records_each_procedure_retrieval(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The Event and the retrieval records are writes, so a cluster has to have taken them."""
    session_id = corpus.session(
        placed.permitted_client, PERMITTED_MACHINE, SessionOutcome.SUCCEEDED
    )
    retrievals = RecordedRetrievals(seen=[])
    embedder = StubQueryEmbedder(placed.query)
    engine = RecallEngine(
        corpus.store,
        embedder,
        recall_floor=RECALL_FLOOR,
        retrievals=retrievals.record,
    )

    results = engine.recall("the intended action", PAGE, session_id=session_id)

    assert embedder.asked == ["the intended action"]
    assert {result.artifact_id for result in results} == placed.permitted_ids
    appended = corpus.rows_of(SELECT_RECALL_EVENTS, (session_id,))
    assert len(appended) == 1
    payload = appended[0][0]
    assert isinstance(payload, dict)
    identifiers = payload["artifact_ids"]
    distances = payload["distances"]
    assert isinstance(identifiers, list)
    assert isinstance(distances, list)
    assert payload["query_text"] == "the intended action"
    assert set(identifiers) == {str(found) for found in placed.permitted_ids}
    assert len(distances) == len(results)
    assert payload["floor_exclusions"] == 1
    assert sorted(retrievals.seen) == sorted(
        [(placed.strong_procedure, session_id), (placed.weak_procedure, session_id)]
    )


def test_an_engine_given_no_recorder_writes_each_retrieval_through_the_tracker(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The seam's default is the Confidence_Tracker's own write rather than silence.

    A caller supplying no recorder still owes Requirement 49.3, so the rows are
    read back from the table the tracker writes. The Event that was also returned
    holds no standing and so is asked about too: a retrieval of anything but a
    Learned_Procedure would be a row the schema's own equivalence forbids.
    """
    session_id = corpus.session(
        placed.permitted_client, PERMITTED_MACHINE, SessionOutcome.SUCCEEDED
    )
    engine = RecallEngine(
        corpus.store,
        StubQueryEmbedder(placed.query),
        recall_floor=RECALL_FLOOR,
    )

    results = engine.recall("the intended action", PAGE, session_id=session_id)

    assert {result.artifact_id for result in results} == placed.permitted_ids
    for procedure in (placed.strong_procedure, placed.weak_procedure):
        assert corpus.scalar(COUNT_RETRIEVALS, (procedure, session_id)) == 1
    assert corpus.scalar(COUNT_RETRIEVALS, (placed.event_artifact, session_id)) == 0


def test_a_query_from_a_session_of_another_tenant_sees_that_tenants_content_only(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The tenancy comes from the stored Session row, which is what a body cannot name."""
    engine = RecallEngine(
        corpus.store,
        StubQueryEmbedder(placed.query),
        recall_floor=RECALL_FLOOR,
    )

    results = engine.recall("the intended action", PAGE, session_id=placed.other_session)

    assert results
    assert not {result.artifact_id for result in results} & placed.permitted_ids
    assert all(result.client_id == placed.other_client for result in results)
