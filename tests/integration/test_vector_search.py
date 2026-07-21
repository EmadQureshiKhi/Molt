"""Index-served nearest-neighbour search against a live instance: is it exact?

Three neighbouring modules already make claims about this query and none of them
makes the claim here. `test_schema_shape.py` asserts the distributed vector index
exists with the operator class the cluster reports. `test_embedding_writes.py`
asserts that the ordering expression on its own produces the vector search node in
the plan, that every table access of the tenancy-filtered form is a bounded seek,
and that a five-row corpus comes back in ascending distance order.
`test_capability_probes.py` asserts the two statement forms agree with each other.

What none of them asks is whether the ordering the index serves is the *true*
nearest-neighbour ordering. A vector index may answer approximately, and an
approximate answer over five rows is indistinguishable from an exact one: the
five are all returned whichever way the index searched. Requirement 10.3 is what
makes this worth asking, because the recall path and the residue detector both
read the top of this ordering and treat what they get as the nearest content
there is. So this module places a corpus large enough for an approximate answer to
be wrong in, computes the ranking independently in Python from the same vectors,
and compares.

Three claims, in the order they build on each other.

The ordering the index serves is the exact ranking. The vectors come from a
digest-derived stream rather than from two components of a plane, so they occupy
the whole width the column declares and the ranking is not a restatement of an
angle the module chose. The reference ranking is a cosine computation in Python
over every vector this schema holds, sharing nothing with the statement but the
values themselves.

The tenancy-filtered query the recall path actually sends returns that same
ordering restricted to what the tenant may see. This is the bridge between the
ordering the index serves and the query a caller runs, and it is asserted as an
agreement between two results rather than as a claim about a plan: on this cluster
a predicate on another column takes the plan off the vector index, so the filtered
form is a bounded seek and a sort, which is what `test_embedding_writes.py`
reads off the plan and this module does not restate.

A corpus holding Artifacts that owe a vector answers over the ones that have one,
unchanged. That is the search half of Requirement 32.1: an embedding provider
outage costs the affected Artifacts their searchability and costs the query
nothing. The sweep half, which is that those Artifacts are listed as work owed,
belongs to `test_embedding_writes.py` and is not repeated here.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from molt.models.event import EmbeddingState
from molt.store import Connection, Cursor, MemoryStore
from molt.store.embeddings import (
    EmbeddingWrite,
    insert_artifact_with_embedding,
    nearest,
    vector_text,
    write_derived_artifact,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes the fixtures make, parameterised in full. This module owns no
# Client insert and no attribution write, so those rows are placed here rather
# than through a module under test.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
# Many attribution rows in one statement, every column of every row bound. The
# artifact column of this table is polymorphic and so carries no reference, which
# is what lets a corpus be attributed in one round trip.
INSERT_BINDINGS_BULK: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) "
    "SELECT unnest(%s::UUID[]), unnest(%s::UUID[]), 'derived_artifact', "
    "unnest(%s::UUID[]), 'scope', 0.9, %s"
)
INSERT_EMBEDDINGS_BULK: Final[str] = (
    "INSERT INTO embedding (artifact_id, artifact_kind, client_id, provider, model_id, "
    "vec, expires_at) "
    "SELECT unnest(%s::UUID[]), 'derived_artifact', unnest(%s::UUID[]), %s, %s, "
    "unnest(%s::STRING[])::VECTOR, %s"
)

# The ordering expression on its own, which is the shape this cluster serves from
# the distributed vector index. It carries no predicate on any other column,
# because a predicate on another column takes the plan off the index.
ORDERING_ONLY_STATEMENT: Final[str] = (
    "SELECT e.artifact_id FROM embedding AS e ORDER BY e.vec <-> %s::VECTOR LIMIT %s"
)

# The provider and the model every row this module writes records.
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# How many vectors the tenant under test holds, and how many a second tenant
# holds alongside them. An approximate search is only distinguishable from an
# exact one on a corpus holding more vectors than the page it returns, and only
# meaningfully so when the near ones are a small part of the whole.
CORPUS_SIZE: Final[int] = 48
OTHER_CORPUS_SIZE: Final[int] = 32

# How many results each comparison reads. Small relative to the corpus, because
# that is the shape the recall path uses and the shape an approximate answer
# would be wrong in.
PAGE: Final[int] = 8

# How close a projected distance must be to the independently computed one. Both
# sides sum a thousand products, so they agree to rather better than this; the
# tolerance is here to keep the assertion about the ranking rather than about the
# last bit of a float.
DISTANCE_TOLERANCE: Final[float] = 1e-6

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


def unit_vector(label: str) -> tuple[float, ...]:
    """A reproducible unit vector of the fixed width, derived from a label.

    Every component carries part of the vector rather than two of them carrying
    all of it, so the corpus occupies the width the column declares and a ranking
    over it is not a restatement of an angle chosen here. The same label always
    yields the same vector, so no example depends on a random draw.
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


def cosine_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """The cosine distance between two unit vectors, computed independently here.

    One minus the inner product, which is the cosine distance exactly when both
    sides are unit length, and the summation is exact rather than sequential so
    the reference value is not itself an approximation.
    """
    return 1.0 - math.fsum(a * b for a, b in zip(left, right, strict=True))


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


@dataclass(frozen=True, slots=True)
class Corpus:
    """A schema holding every migration, a store over it, and the vectors placed in it."""

    store: MemoryStore
    connection: DriverConnection

    def tenant(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:10]}", "Tenant", "eu"),
        )
        return identifier

    def artifact(self, owner: UUID, *, state: EmbeddingState) -> DerivedArtifact:
        """A Derived_Artifact record for one tenant, not yet written."""
        identifier = uuid4()
        return DerivedArtifact(
            id=identifier,
            kind=DerivedArtifactKind.SUMMARY,
            owner_client_id=owner,
            body="a distilled body",
            content_digest=digest_of(f"artifact-{identifier}"),
            derivation_method="distil",
            revision=1,
            created_at=MOMENT,
            updated_at=MOMENT,
            redacted_at=None,
            embedding_state=state,
            expires_at=MOMENT + RETENTION,
            procedure_confidence=None,
        )

    def place(self, owner: UUID, labels: tuple[str, ...]) -> dict[UUID, tuple[float, ...]]:
        """Write one attributed Artifact and Embedding per label, through the store.

        The Artifacts and their vectors go in through the module under test, in
        one transaction, and the attribution rows follow in one bulk statement,
        because this module owns no attribution write of its own.
        """
        records = [self.artifact(owner, state=EmbeddingState.EMBEDDED) for _ in labels]
        vectors = {
            record.id: unit_vector(label) for record, label in zip(records, labels, strict=True)
        }

        def body(cursor: Cursor) -> None:
            for record in records:
                insert_artifact_with_embedding(
                    cursor,
                    record,
                    EmbeddingWrite(
                        artifact_id=record.id,
                        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                        client_id=owner,
                        provider=PROVIDER,
                        model_id=MODEL,
                        vec=vectors[record.id],
                        expires_at=MOMENT + RETENTION,
                    ),
                )

        self.store.in_serializable(body)
        self.attribute(tuple(vectors), owner)
        return vectors

    def place_bulk(self, owner: UUID, labels: tuple[str, ...]) -> dict[UUID, tuple[float, ...]]:
        """Give one tenant attributed vectors in two bulk statements.

        These rows need no Artifact behind them: neither the attribution table's
        artifact column nor the Embedding table's carries a reference, because
        both are polymorphic across the Artifact kinds. They exist so the corpus
        the index searches is larger than the tenant's own slice of it, which is
        the condition an approximate answer would go wrong under.
        """
        vectors = {uuid4(): unit_vector(label) for label in labels}
        send(
            self.connection,
            INSERT_EMBEDDINGS_BULK,
            (
                list(vectors),
                [owner] * len(vectors),
                PROVIDER,
                MODEL,
                [vector_text(vector) for vector in vectors.values()],
                MOMENT + RETENTION,
            ),
        )
        self.attribute(tuple(vectors), owner)
        return vectors

    def place_pending(self, owner: UUID, count: int) -> tuple[UUID, ...]:
        """Write Artifacts that owe a vector, as an unavailable provider leaves them."""
        placed: list[UUID] = []
        for _ in range(count):
            record = self.artifact(owner, state=EmbeddingState.PENDING)
            written = write_derived_artifact(self.store, record)
            assert written.embedding_id is None
            assert written.embedding_state is EmbeddingState.PENDING
            placed.append(record.id)
        self.attribute(tuple(placed), owner)
        return tuple(placed)

    def attribute(self, artifact_ids: tuple[UUID, ...], owner: UUID) -> None:
        """Attribute many Artifacts to one tenant as current Attribution_Versions."""
        send(
            self.connection,
            INSERT_BINDINGS_BULK,
            (
                [uuid4() for _ in artifact_ids],
                list(artifact_ids),
                [owner] * len(artifact_ids),
                MOMENT,
            ),
        )

    def index_served_order(self, query: tuple[float, ...], page: int) -> tuple[UUID, ...]:
        """The ordering the distributed vector index serves, with no other predicate."""

        def body(cursor: Cursor) -> tuple[UUID, ...]:
            cursor.execute(ORDERING_ONLY_STATEMENT, (vector_text(query), page))
            return tuple(_as_uuid(row[0]) for row in cursor.fetchall())

        return self.store.read(body)

    def collect_statistics(self) -> None:
        """Collect table statistics, so the planner sees the corpus rather than a guess."""
        with self.connection.cursor() as cursor:
            cursor.execute("ANALYZE embedding")
            cursor.execute("ANALYZE client_binding")


def _as_uuid(value: object) -> UUID:
    """One identifier column, read from a row of the fixture's own statement."""
    if isinstance(value, UUID):
        return value
    assert isinstance(value, str)
    return UUID(value)


def reference_order(
    query: tuple[float, ...],
    vectors: dict[UUID, tuple[float, ...]],
    page: int,
) -> tuple[UUID, ...]:
    """The nearest identifiers by an independent ranking, closest first.

    The identifier is the tie-break, so the reference order is total. Nothing
    here is shared with the statement it is compared against beyond the stored
    vectors themselves.
    """
    ranked = sorted(vectors, key=lambda found: (cosine_distance(query, vectors[found]), found.hex))
    return tuple(ranked[:page])


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


@dataclass(frozen=True, slots=True)
class Placed:
    """One tenant's searchable vectors, and every vector the schema holds."""

    owner: UUID
    mine: dict[UUID, tuple[float, ...]]
    everything: dict[UUID, tuple[float, ...]]
    query: tuple[float, ...]


@pytest.fixture(scope="module")
def placed(corpus: Corpus) -> Placed:
    """Two tenants' vectors, placed once and searched by every example here."""
    owner = corpus.tenant()
    other = corpus.tenant()
    mine = corpus.place(owner, tuple(f"under-test-{step}" for step in range(CORPUS_SIZE)))
    theirs = corpus.place_bulk(other, tuple(f"other-{step}" for step in range(OTHER_CORPUS_SIZE)))
    corpus.collect_statistics()
    return Placed(
        owner=owner,
        mine=mine,
        everything={**mine, **theirs},
        query=unit_vector("the query"),
    )


# ---------------------------------------------------------------------------
# The ordering the index serves
# ---------------------------------------------------------------------------


def test_the_index_served_ordering_is_the_exact_nearest_neighbour_ranking(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The index answers exactly, on a corpus an approximate answer would miss in.

    The page is a small part of the corpus and the reference ranking is computed
    in Python over every stored vector, so an index that searched a subset of the
    space and returned a near-enough neighbour would disagree here. Requirement
    10.3 asks for this index because the recall path reads the top of this
    ordering and treats it as the nearest content there is.
    """
    assert len(placed.everything) == CORPUS_SIZE + OTHER_CORPUS_SIZE
    assert len(placed.everything) > PAGE

    served = corpus.index_served_order(placed.query, PAGE)

    assert served == reference_order(placed.query, placed.everything, PAGE)


def test_the_index_served_ordering_is_exact_from_several_query_vectors(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """One query vector could be lucky, so the comparison is made from several."""
    for step in range(4):
        query = unit_vector(f"another query {step}")

        served = corpus.index_served_order(query, PAGE)

        assert served == reference_order(query, placed.everything, PAGE)


# ---------------------------------------------------------------------------
# The query the recall path sends
# ---------------------------------------------------------------------------


def test_the_tenancy_filtered_query_returns_that_ordering_restricted_to_the_tenant(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """The query a caller runs and the ordering the index serves agree on the tenant's slice.

    Two things are asserted together because either alone would be weaker. The
    identifiers are the reference ranking over the tenant's own vectors, so the
    filtered form is neither narrower nor wider than the ordering. And each
    projected distance is the independently computed cosine distance, so the value
    a threshold is compared against is the value the ranking used.
    """
    found = nearest(
        corpus.store,
        placed.query,
        permitted_clients=[placed.owner],
        limit=PAGE,
    )

    assert [neighbour.artifact_id for neighbour in found] == list(
        reference_order(placed.query, placed.mine, PAGE)
    )
    for neighbour in found:
        assert neighbour.cosine_distance == pytest.approx(
            cosine_distance(placed.query, placed.mine[neighbour.artifact_id]),
            abs=DISTANCE_TOLERANCE,
        )


def test_the_tenancy_filtered_page_is_the_head_of_the_ordering_at_every_bound(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """A shorter page is the head of the longer one, rather than a shorter sample of it."""
    longer = nearest(corpus.store, placed.query, permitted_clients=[placed.owner], limit=PAGE * 2)
    shorter = nearest(corpus.store, placed.query, permitted_clients=[placed.owner], limit=PAGE)

    assert len(longer) == PAGE * 2
    assert list(shorter) == list(longer[:PAGE])


# ---------------------------------------------------------------------------
# A corpus that owes vectors
# ---------------------------------------------------------------------------


def test_artifacts_owing_a_vector_leave_the_search_over_the_rest_unchanged(
    corpus: Corpus,
    placed: Placed,
) -> None:
    """An unavailable provider costs those Artifacts their searchability and nothing else.

    The Artifacts land, attributed to the same tenant and inside the same
    permitted set, and they carry no vector. The page the tenant gets back is the
    page it got before they were written, identifier for identifier and distance
    for distance, so the degradation Requirement 32.1 describes is confined to the
    content that owes a vector.
    """
    before = nearest(corpus.store, placed.query, permitted_clients=[placed.owner], limit=PAGE)

    owing = corpus.place_pending(placed.owner, 4)
    after = nearest(corpus.store, placed.query, permitted_clients=[placed.owner], limit=PAGE)

    assert after == before
    assert not set(owing) & {neighbour.artifact_id for neighbour in after}
