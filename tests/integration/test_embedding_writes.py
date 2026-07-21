"""Embedding writes, the pending sweep, and the neighbour query against a live instance.

The unit module asserts the shape of the statements. This one asserts the five
things only a cluster can answer.

The Artifact row and its Embedding row are one transaction. A vector the schema
refuses leaves no Artifact behind, which is the claim a caller cannot check by
reading the module: the rollback is the cluster's.

The width and the unit-norm checks refuse before a statement is sent, and the
column refuses the width a second time. Both halves matter: the write-time check
is what keeps the ordering and the thresholds reconciled, and the column is what
makes a vector arriving by some other path impossible rather than merely
detectable.

The pending sweep spans Events and Derived_Artifacts and returns them oldest
first, which is what makes a bounded drain fair rather than arbitrary.

The neighbour query orders by the distance it projects, admits only Artifacts an
unsuperseded Attribution_Version binds to a permitted Client, and honours the
cosine ceiling. Each of those is compared against a distance computed
independently in Python over vectors placed at known angles.

The plans are read back as well. Every table access the neighbour statement makes
is a seek into a bounded span rather than a read of a table, and the ordering
expression on its own is served by the distributed vector index. Those are two
separate findings and both are asserted, because on this cluster the tenancy
term is applied by a semi-join over the attribution index rather than inside the
vector search: the index serves the ordering, and the tenant covering indexes
bound what the ordering is applied to.

Every migration is applied, because the attribution closure columns the tenancy
term reads arrive with the second generation.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.errors import StoreError
from molt.models.artifact import (
    EMBEDDING_DIMENSION,
    ArtifactKind,
    DerivedArtifact,
    DerivedArtifactKind,
)
from molt.models.event import EmbeddingState
from molt.store import Connection, Cursor, MemoryStore
from molt.store.embeddings import (
    NEAREST_STATEMENT,
    EmbeddingWrite,
    insert_artifact_with_embedding,
    mark_embedding_state,
    nearest,
    pending_artifacts,
    vector_text,
    write_derived_artifact,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes and reads the fixtures make, parameterised in full. This module
# owns no Client insert, no Ledger append, and no attribution write, so those
# rows are placed here rather than through the module under test.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION_ROW: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, started_at) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_LEDGER_ROW: Final[str] = (
    "INSERT INTO ledger (id, session_id, client_id, seq, category, occurred_at, "
    "recorded_at, agent_cli, machine_id, payload, content_digest, prev_chain_digest, "
    "chain_digest, embedding_state, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s, %s, %s, %s, %s)"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
# Many attribution rows in one statement, every column of every row bound. The
# artifact column of this table is polymorphic and so carries no reference, which
# is what lets a plan corpus be built from attribution rows alone.
INSERT_BINDINGS_BULK: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) "
    "SELECT unnest(%s::UUID[]), unnest(%s::UUID[]), 'derived_artifact', "
    "unnest(%s::UUID[]), 'scope', 0.9, %s"
)
CLOSE_BINDING: Final[str] = (
    "UPDATE client_binding SET valid_to = %s, superseded_by = %s WHERE id = %s"
)
# Many Embedding rows in one statement, sharing one bound vector. The artifact
# column of this table is polymorphic and carries no reference either, so these
# rows are a corpus rather than content.
INSERT_EMBEDDINGS_BULK: Final[str] = (
    "INSERT INTO embedding (artifact_id, artifact_kind, client_id, provider, model_id, "
    "vec, expires_at) "
    "SELECT unnest(%s::UUID[]), 'derived_artifact', unnest(%s::UUID[]), %s, %s, "
    "%s::VECTOR, %s"
)
INSERT_RAW_EMBEDDING: Final[str] = (
    "INSERT INTO embedding (artifact_id, artifact_kind, client_id, provider, model_id, "
    "vec, expires_at) VALUES (%s, %s, %s, %s, %s, %s::VECTOR, %s)"
)
SELECT_EMBEDDING_ROW: Final[str] = (
    "SELECT artifact_kind, client_id, provider, model_id, dimension, normalised "
    "FROM embedding WHERE artifact_id = %s"
)
SELECT_ARTIFACT_STATE: Final[str] = "SELECT embedding_state FROM derived_artifact WHERE id = %s"
COUNT_ARTIFACTS: Final[str] = "SELECT count(*) FROM derived_artifact WHERE id = %s"
COUNT_EMBEDDINGS: Final[str] = "SELECT count(*) FROM embedding WHERE artifact_id = %s"

# The ordering expression on its own, which is what the distributed vector index
# serves. It is read from the module's own statement rather than restated, so a
# change to the ordering there reaches this assertion too.
ORDERING_FRAGMENT: Final[str] = "ORDER BY e.vec <-> %s::VECTOR"
ORDERING_ONLY_STATEMENT: Final[str] = (
    "SELECT e.artifact_id FROM embedding AS e ORDER BY e.vec <-> %s::VECTOR LIMIT %s"
)

# The index the tenancy term seeks into, and the index the lookup back into the
# vectors is served by.
BINDING_INDEX: Final[str] = "binding_by_client"
EMBEDDING_ARTIFACT_INDEX: Final[str] = "embedding_by_artifact"

# What the plan calls the vector index search, and what it says when it read a
# whole table instead of seeking into it. The plan is lowercased before the
# comparison, so these are the lowercase spellings.
VECTOR_SEARCH: Final[str] = "vector search"
FULL_SCAN: Final[str] = "full scan"
BOUNDED_SPAN: Final[str] = "spans: [/"

# The provider and the model every row this module writes records.
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# How many other tenants the plan corpus attributes content to, and how many rows
# each of them holds. A tenant restriction is only worth seeking for when it is
# selective, so the plan a deployed cluster would produce is only visible once the
# other tenants exist; the plan corpus is what supplies them.
PLAN_TENANTS: Final[int] = 24
PLAN_ROWS_PER_TENANT: Final[int] = 40

# How many vectors the tenant the plan is read for holds.
PLAN_ROWS_UNDER_TEST: Final[int] = 20

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


def vector_at(angle: float) -> tuple[float, ...]:
    """A unit vector at a known angle from the query vector of these examples.

    Two components carry the whole of the vector, so the cosine distance from the
    base vector is one minus the cosine of the angle and is known exactly rather
    than measured. Every other component is zero, which keeps the norm at one.
    """
    components = [0.0] * EMBEDDING_DIMENSION
    components[0] = math.cos(angle)
    components[1] = math.sin(angle)
    return tuple(components)


def expected_distance(angle: float) -> float:
    """The cosine distance from the base vector for a vector at an angle."""
    return 1.0 - math.cos(angle)


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def scalar(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> object:
    """Read one column of one row on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    assert row is not None
    return row[0]


@dataclass(frozen=True, slots=True)
class Corpus:
    """A schema holding every migration, a store over it, and a tenant factory."""

    store: MemoryStore
    connection: DriverConnection
    schema: str

    def tenant(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:10]}", "Tenant", "eu"),
        )
        return identifier

    def artifact(
        self,
        client_id: UUID,
        *,
        created_at: datetime = MOMENT,
        state: EmbeddingState = EmbeddingState.PENDING,
        identifier: UUID | None = None,
    ) -> DerivedArtifact:
        """A Derived_Artifact record for one tenant, not yet written."""
        chosen = uuid4() if identifier is None else identifier
        return DerivedArtifact(
            id=chosen,
            kind=DerivedArtifactKind.SUMMARY,
            owner_client_id=client_id,
            body="a distilled body",
            content_digest=digest_of(f"artifact-{chosen}"),
            derivation_method="distil",
            revision=1,
            created_at=created_at,
            updated_at=created_at,
            redacted_at=None,
            embedding_state=state,
            expires_at=created_at + RETENTION,
            procedure_confidence=None,
        )

    def embedding(
        self,
        artifact_id: UUID,
        client_id: UUID,
        angle: float,
    ) -> EmbeddingWrite:
        """An Embedding write for one Artifact, placed at a known angle."""
        return EmbeddingWrite(
            artifact_id=artifact_id,
            artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=client_id,
            provider=PROVIDER,
            model_id=MODEL,
            vec=vector_at(angle),
            expires_at=MOMENT + RETENTION,
        )

    def bind(self, artifact_id: UUID, client_id: UUID) -> UUID:
        """Attribute one Artifact to one tenant as a current Attribution_Version."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_BINDING,
            (identifier, artifact_id, "derived_artifact", client_id, "scope", 0.9, MOMENT),
        )
        return identifier

    def event(self, client_id: UUID, *, recorded_at: datetime, seq: int) -> UUID:
        """Place one pending Ledger row directly and return its identifier."""
        session_id = uuid4()
        send(
            self.connection,
            INSERT_SESSION_ROW,
            (session_id, client_id, "agent", "machine", MOMENT),
        )
        identifier = uuid4()
        send(
            self.connection,
            INSERT_LEDGER_ROW,
            (
                identifier,
                session_id,
                client_id,
                seq,
                "tool_call",
                recorded_at,
                recorded_at,
                "agent",
                "machine",
                '{"tool":"read"}',
                digest_of(f"content-{identifier}"),
                digest_of(f"previous-{identifier}"),
                digest_of(f"chain-{identifier}"),
                EmbeddingState.PENDING.value,
                recorded_at + RETENTION,
            ),
        )
        return identifier

    def place(self, client_id: UUID, angles: tuple[float, ...]) -> tuple[UUID, ...]:
        """Write one attributed Artifact and Embedding per angle, in one transaction."""
        records = [self.artifact(client_id) for _ in angles]

        def body(cursor: Cursor) -> None:
            for record, angle in zip(records, angles, strict=True):
                insert_artifact_with_embedding(
                    cursor,
                    record,
                    self.embedding(record.id, client_id, angle),
                )

        self.store.in_serializable(body)
        for record in records:
            self.bind(record.id, client_id)
        return tuple(record.id for record in records)

    def other_tenants_content(self, tenants: int, per_tenant: int) -> None:
        """Give many other tenants attributed vectors, in two bulk statements.

        A tenant restriction is only worth seeking for when it is selective, and a
        corpus is only worth seeking into when the tenant's slice of it is small.
        Both conditions hold on a deployed cluster and neither holds on a table
        holding one tenant's rows, so the plan a deployment would produce is only
        visible once the other tenants exist.

        These rows need no Artifact behind them: neither the attribution table's
        artifact column nor the Embedding table's carries a reference, because both
        are polymorphic across the Artifact kinds. One vector value is shared by
        every row, which keeps the corpus cheap to place and is invisible to every
        other example here, since none of them permits these tenants.
        """
        bindings: list[UUID] = []
        artifacts: list[UUID] = []
        owners: list[UUID] = []
        for _ in range(tenants):
            client_id = self.tenant()
            for _ in range(per_tenant):
                bindings.append(uuid4())
                artifacts.append(uuid4())
                owners.append(client_id)
        send(self.connection, INSERT_BINDINGS_BULK, (bindings, artifacts, owners, MOMENT))
        send(
            self.connection,
            INSERT_EMBEDDINGS_BULK,
            (artifacts, owners, PROVIDER, MODEL, vector_text(vector_at(2.0)), MOMENT + RETENTION),
        )

    def plan_of(self, statement: str, params: tuple[object, ...]) -> str:
        """The plan the cluster reports for one statement, as one lowercase block.

        The prefix is a literal and the statement is the module's own literal; every
        value stays a bound parameter, so nothing a caller supplied reaches
        statement text even here.
        """
        with self.connection.cursor() as cursor:
            cursor.execute("EXPLAIN " + statement, params)
            rows = cursor.fetchall()
        return "\n".join(" ".join(str(column) for column in row) for row in rows).lower()

    def collect_statistics(self) -> None:
        """Collect table statistics, so a plan reflects selectivity rather than absence.

        A table whose statistics have never been collected is planned against a
        default guess, and the guess for a freshly created table is that no
        restriction is selective. Collecting is what makes the plan the one a
        deployed cluster would produce.
        """
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
    """Apply every migration, then build a store bound to that schema."""
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
        yield Corpus(store=store, connection=fresh_schema, schema=schema)


# ---------------------------------------------------------------------------
# The one transaction
# ---------------------------------------------------------------------------


def test_the_artifact_and_its_embedding_land_together(corpus: Corpus) -> None:
    """Both rows exist, and the Embedding row records what produced the vector."""
    client_id = corpus.tenant()
    record = corpus.artifact(client_id)

    written = write_derived_artifact(
        corpus.store,
        record,
        embedding=corpus.embedding(record.id, client_id, 0.0),
    )

    assert written.artifact_id == record.id
    assert written.embedding_id is not None
    assert written.embedding_state is EmbeddingState.EMBEDDED
    assert scalar(corpus.connection, SELECT_ARTIFACT_STATE, (record.id,)) == "embedded"
    with corpus.connection.cursor() as cursor:
        cursor.execute(SELECT_EMBEDDING_ROW, (record.id,))
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == ArtifactKind.DERIVED_ARTIFACT.value
    assert row[1] == client_id
    assert row[2] == PROVIDER
    assert row[3] == MODEL
    assert row[4] == EMBEDDING_DIMENSION
    assert row[5] is True


def test_a_refused_embedding_leaves_no_artifact_row(corpus: Corpus) -> None:
    """The rollback is the cluster's, and it takes the Artifact with it.

    The refusal is the one a re-embedding under the same provider and model runs
    into: one vector per Artifact per provider-and-model pair is held unique. The
    conflicting row is placed first, so the Artifact insert succeeds and the
    Embedding insert is what fails, which is the ordering that makes the
    atomicity claim meaningful.
    """
    client_id = corpus.tenant()
    record = corpus.artifact(client_id)
    send(
        corpus.connection,
        INSERT_RAW_EMBEDDING,
        (
            record.id,
            ArtifactKind.DERIVED_ARTIFACT.value,
            client_id,
            PROVIDER,
            MODEL,
            vector_text(vector_at(0.0)),
            MOMENT + RETENTION,
        ),
    )

    with pytest.raises(StoreError, match="already stored"):
        write_derived_artifact(
            corpus.store,
            record,
            embedding=corpus.embedding(record.id, client_id, 0.5),
        )

    assert scalar(corpus.connection, COUNT_ARTIFACTS, (record.id,)) == 0
    assert scalar(corpus.connection, COUNT_EMBEDDINGS, (record.id,)) == 1


# ---------------------------------------------------------------------------
# The write-time checks, and the column that restates one of them
# ---------------------------------------------------------------------------


def test_a_vector_of_another_width_is_refused_twice_over(corpus: Corpus) -> None:
    """The module refuses before sending, and the column refuses a vector anyway."""
    client_id = corpus.tenant()
    record = corpus.artifact(client_id)
    narrow = tuple([1.0] + [0.0] * 511)

    with pytest.raises(ValueError, match="component"):
        corpus.store.write_derived_artifact(
            record,
            embedding=EmbeddingWrite(
                artifact_id=record.id,
                artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                client_id=client_id,
                provider=PROVIDER,
                model_id=MODEL,
                vec=narrow,
                expires_at=MOMENT + RETENTION,
            ),
        )
    with pytest.raises(Exception, match=r"1024"):
        send(
            corpus.connection,
            INSERT_RAW_EMBEDDING,
            (
                record.id,
                ArtifactKind.DERIVED_ARTIFACT.value,
                client_id,
                PROVIDER,
                MODEL,
                vector_text(narrow),
                MOMENT + RETENTION,
            ),
        )

    assert scalar(corpus.connection, COUNT_ARTIFACTS, (record.id,)) == 0
    assert scalar(corpus.connection, COUNT_EMBEDDINGS, (record.id,)) == 0


def test_a_vector_that_is_not_unit_length_is_refused_at_write_time(corpus: Corpus) -> None:
    """L2 ordering is cosine ordering over unit vectors and over nothing else."""
    client_id = corpus.tenant()
    record = corpus.artifact(client_id)
    stretched = tuple(3.0 * component for component in vector_at(0.0))

    with pytest.raises(ValueError, match="unit length"):
        EmbeddingWrite(
            artifact_id=record.id,
            artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=client_id,
            provider=PROVIDER,
            model_id=MODEL,
            vec=stretched,
            expires_at=MOMENT + RETENTION,
        )

    assert scalar(corpus.connection, COUNT_ARTIFACTS, (record.id,)) == 0


# ---------------------------------------------------------------------------
# The pending sweep
# ---------------------------------------------------------------------------


def test_the_pending_sweep_spans_both_kinds_oldest_first(corpus: Corpus) -> None:
    """Events and Derived_Artifacts alike, in ascending creation order."""
    client_id = corpus.tenant()
    first_event = corpus.event(client_id, recorded_at=MOMENT, seq=1)
    second_event = corpus.event(client_id, recorded_at=MOMENT + timedelta(minutes=3), seq=1)
    older = corpus.artifact(client_id, created_at=MOMENT + timedelta(minutes=1))
    newer = corpus.artifact(client_id, created_at=MOMENT + timedelta(minutes=5))
    write_derived_artifact(corpus.store, older)
    write_derived_artifact(corpus.store, newer)

    swept = [
        pending.artifact_id
        for pending in pending_artifacts(corpus.store, limit=1000)
        if pending.client_id == client_id
    ]

    assert swept == [first_event, older.id, second_event, newer.id]


def test_an_artifact_written_with_a_vector_is_not_swept(corpus: Corpus) -> None:
    """A row owing nothing is not work, and the sweep is a list of work owed."""
    client_id = corpus.tenant()
    (embedded,) = corpus.place(client_id, (0.0,))
    pending = corpus.artifact(client_id, created_at=MOMENT + timedelta(minutes=1))
    write_derived_artifact(corpus.store, pending)

    swept = [
        found.artifact_id
        for found in pending_artifacts(corpus.store, limit=1000)
        if found.client_id == client_id
    ]

    assert swept == [pending.id]
    assert embedded not in swept


def test_a_state_transition_moves_a_row_out_of_the_sweep(corpus: Corpus) -> None:
    """The three states work can be in, and the sweep follows the state column."""
    client_id = corpus.tenant()
    record = corpus.artifact(client_id)
    write_derived_artifact(corpus.store, record)

    assert (
        mark_embedding_state(corpus.store, record.id, client_id, EmbeddingState.FAILED)
        is EmbeddingState.FAILED
    )

    swept = [
        found.artifact_id
        for found in pending_artifacts(corpus.store, limit=1000)
        if found.client_id == client_id
    ]
    assert swept == []
    assert scalar(corpus.connection, SELECT_ARTIFACT_STATE, (record.id,)) == "failed"


def test_a_state_transition_for_another_tenant_matches_nothing(corpus: Corpus) -> None:
    """Holding an identifier is not authority over the row it names."""
    client_id = corpus.tenant()
    other = corpus.tenant()
    record = corpus.artifact(client_id)
    write_derived_artifact(corpus.store, record)

    assert mark_embedding_state(corpus.store, record.id, other, EmbeddingState.EMBEDDED) is None
    assert scalar(corpus.connection, SELECT_ARTIFACT_STATE, (record.id,)) == "pending"


# ---------------------------------------------------------------------------
# The neighbour query
# ---------------------------------------------------------------------------


def test_the_closest_come_first_with_the_distance_they_were_ranked_by(corpus: Corpus) -> None:
    """The ordering the cluster performed matches the distance it projected."""
    client_id = corpus.tenant()
    angles = (1.2, 0.3, 0.9, 0.0, 0.6)
    placed = corpus.place(client_id, angles)
    by_angle = dict(zip(angles, placed, strict=True))

    found = nearest(corpus.store, vector_at(0.0), permitted_clients=[client_id], limit=5)

    assert [neighbour.artifact_id for neighbour in found] == [
        by_angle[angle] for angle in sorted(angles)
    ]
    distances = [neighbour.cosine_distance for neighbour in found]
    assert distances == sorted(distances)
    for neighbour, angle in zip(found, sorted(angles), strict=True):
        assert neighbour.cosine_distance == pytest.approx(expected_distance(angle), abs=1e-5)
        assert neighbour.artifact_kind is ArtifactKind.DERIVED_ARTIFACT
        assert neighbour.client_id == client_id


def test_the_result_count_is_at_most_the_bound(corpus: Corpus) -> None:
    """A page of k results is at most k, and it is the k closest."""
    client_id = corpus.tenant()
    angles = (0.2, 0.4, 0.6, 0.8, 1.0)
    placed = corpus.place(client_id, angles)
    by_angle = dict(zip(angles, placed, strict=True))

    found = nearest(corpus.store, vector_at(0.0), permitted_clients=[client_id], limit=2)

    assert [neighbour.artifact_id for neighbour in found] == [by_angle[0.2], by_angle[0.4]]


def test_the_cosine_ceiling_excludes_the_more_distant_rows(corpus: Corpus) -> None:
    """The ceiling sits in the predicate, so a page is not shortened after the fact."""
    client_id = corpus.tenant()
    angles = (0.1, 0.2, 1.4)
    placed = corpus.place(client_id, angles)
    by_angle = dict(zip(angles, placed, strict=True))
    ceiling = expected_distance(0.5)

    found = nearest(
        corpus.store,
        vector_at(0.0),
        permitted_clients=[client_id],
        limit=10,
        max_cosine=ceiling,
    )

    assert {neighbour.artifact_id for neighbour in found} == {by_angle[0.1], by_angle[0.2]}
    assert all(neighbour.cosine_distance <= ceiling for neighbour in found)


def test_another_tenants_vectors_are_not_reachable(corpus: Corpus) -> None:
    """The tenancy term is what makes a permitted set an authority rather than a hint."""
    mine = corpus.tenant()
    theirs = corpus.tenant()
    (ours,) = corpus.place(mine, (0.1,))
    (foreign,) = corpus.place(theirs, (0.0,))

    found = nearest(corpus.store, vector_at(0.0), permitted_clients=[mine], limit=10)

    reached = {neighbour.artifact_id for neighbour in found}
    assert ours in reached
    assert foreign not in reached


def test_a_superseded_attribution_no_longer_reaches_the_vector(corpus: Corpus) -> None:
    """Only an unsuperseded Attribution_Version admits an Artifact to a tenant's search.

    The supersession is the ordinary one: an Artifact first attributed to one
    tenant is re-attributed to another, the successor version is written, and the
    prior version is closed against it. The vector then answers for the successor's
    tenant and stops answering for the predecessor's.
    """
    first = corpus.tenant()
    second = corpus.tenant()
    (artifact_id,) = corpus.place(first, (0.2,))
    prior = scalar(
        corpus.connection,
        "SELECT id FROM client_binding WHERE artifact_id = %s AND client_id = %s",
        (artifact_id, first),
    )
    successor = corpus.bind(artifact_id, second)
    send(corpus.connection, CLOSE_BINDING, (MOMENT + timedelta(minutes=1), successor, prior))

    as_first = nearest(corpus.store, vector_at(0.0), permitted_clients=[first], limit=10)
    as_second = nearest(corpus.store, vector_at(0.0), permitted_clients=[second], limit=10)

    assert artifact_id not in {neighbour.artifact_id for neighbour in as_first}
    assert artifact_id in {neighbour.artifact_id for neighbour in as_second}


def test_a_caller_permitted_no_client_reaches_nothing(corpus: Corpus) -> None:
    """An empty permitted set is answered without a statement and without rows."""
    corpus.place(corpus.tenant(), (0.0,))

    assert nearest(corpus.store, vector_at(0.0), permitted_clients=[]) == ()


# ---------------------------------------------------------------------------
# The plans the bounds rest on
# ---------------------------------------------------------------------------


def test_the_neighbour_statement_seeks_into_bounded_spans(corpus: Corpus) -> None:
    """Every table access is a seek into a bounded span rather than a table read.

    The tenancy term is served by the attribution index keyed on the tenant, and
    the vectors are reached through the per-artifact index on the Embedding table.
    Both are named here rather than merely asserting the absence of a full scan,
    because a plan that read either table whole would meet every other assertion
    in this module and still miss the latency bound.
    """
    client_id = corpus.tenant()
    corpus.place(
        client_id,
        tuple(0.01 * (step + 1) for step in range(PLAN_ROWS_UNDER_TEST)),
    )
    corpus.other_tenants_content(PLAN_TENANTS, PLAN_ROWS_PER_TENANT)
    corpus.collect_statistics()
    rendered = vector_text(vector_at(0.0))

    plan = corpus.plan_of(
        NEAREST_STATEMENT,
        (rendered, [client_id], None, rendered, None, rendered, 5),
    )

    assert BINDING_INDEX in plan, plan
    assert EMBEDDING_ARTIFACT_INDEX in plan, plan
    assert BOUNDED_SPAN in plan, plan
    assert FULL_SCAN not in plan, plan


def test_the_ordering_expression_is_served_by_the_distributed_vector_index(
    corpus: Corpus,
) -> None:
    """The index serves the ordering the module's statement asks for.

    This is asserted over the ordering on its own rather than over the whole
    statement, because on this cluster the tenancy term is applied as a semi-join
    over the attribution index rather than inside the vector search: the search
    node appears when the ordering is the only restriction. The two plans together
    are what the design's claim rests on, so both are read back.
    """
    corpus.place(corpus.tenant(), (0.05, 0.1))

    plan = corpus.plan_of(ORDERING_ONLY_STATEMENT, (vector_text(vector_at(0.0)), 5))

    assert ORDERING_FRAGMENT in NEAREST_STATEMENT, "the ordering under test is the module's own"
    assert ORDERING_FRAGMENT in ORDERING_ONLY_STATEMENT
    assert VECTOR_SEARCH in plan, plan
    assert FULL_SCAN not in plan, plan
