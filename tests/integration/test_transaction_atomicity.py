"""One transaction per Artifact, whatever else lands with it.

Two writes accompany an Artifact in the transaction that writes it. The Embedding
that represents its text (Requirement 10.5), and the Attribution_Versions naming
the Clients whose data it holds (Requirement 12.6). The first half is asserted
live in `test_embedding_writes.py`, where a vector the schema refuses is shown to
take its Artifact with it. Nobody asserts the second half, and it is the half with
the sharper consequence: an Artifact that landed without its attribution is memory
content nobody is recorded as owning, so an erasure sweep for the Client whose data
it holds would not select it and no later read would notice the omission.

So this module writes the Artifact and the attribution rows on one cursor inside
one serializable transaction and asserts three things.

Both rows land, and the attribution row names the Artifact that was written in the
same transaction.

An attribution row the cluster refuses leaves no Artifact behind. Three refusals
are used rather than one, because the rollback has to be the cluster's rather than
a reaction to a particular failure: an absent Client reaches the reference on the
tenant column, a confidence outside the unit interval reaches the range check, and
a repeated Artifact-and-Client pair reaches the uniqueness the schema holds over
that pair. All three arrive as driver failures out of the second statement of a
transaction whose first statement had succeeded.

An Artifact written with its vector and its attribution together is one
transaction across all three tables. The refusal comes last, so both earlier rows
are already written when it arrives, and neither survives it. That is the case the
Artifact-and-Embedding assertion cannot make, because there the refused write is
the second of two rather than the third of three.

**What waits for task 8.1.** The store has no attribution module yet, so the rows
here are placed by parameterised statements of this module rather than through the
surface a caller will use. What that leaves uncovered is everything specific to an
attribution *history* rather than to a row: the two-statement supersession that
closes the current version and inserts its successor in one transaction, the
partial uniqueness over unsuperseded versions that migration 008 puts in place of
the total constraint used here, the rule retaining the greater of the submitted and
prior confidence, and the `attribution_superseded` Ledger Event that must land in
that same transaction. Each of those is a claim about the module that owns them and
belongs with it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
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
    insert_artifact,
    insert_artifact_with_embedding,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes and reads, parameterised in full. The attribution insert is the
# shape the store's own will take, with every column of the row bound, so what is
# exercised is the transaction rather than a convenience.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
SELECT_BINDING_ROW: Final[str] = (
    "SELECT artifact_kind, client_id, method, confidence, valid_to, superseded_by "
    "FROM client_binding WHERE artifact_id = %s"
)
COUNT_ARTIFACTS: Final[str] = "SELECT count(*) FROM derived_artifact WHERE id = %s"
COUNT_BINDINGS: Final[str] = "SELECT count(*) FROM client_binding WHERE artifact_id = %s"
COUNT_EMBEDDINGS: Final[str] = "SELECT count(*) FROM embedding WHERE artifact_id = %s"

# The provider and the model every Embedding row this module writes records.
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# The detection method and the confidence an accepted attribution row carries.
METHOD: Final[str] = "scope"
CONFIDENCE: Final[float] = 0.9

# A confidence outside the closed unit interval the schema admits, and one of the
# two refusals that reaches a check rather than a reference.
IMPOSSIBLE_CONFIDENCE: Final[float] = 1.5

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# The vector every Embedding row here carries: unit length by construction, since
# the write-time check refuses anything else and this module is not about that.
UNIT_FIRST_COMPONENT: Final[float] = 1.0

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


def unit_vector() -> tuple[float, ...]:
    """A unit vector of the fixed width, with one component carrying all of it."""
    components = [0.0] * EMBEDDING_DIMENSION
    components[0] = UNIT_FIRST_COMPONENT
    return tuple(components)


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
class Cluster:
    """A schema holding every migration, a store over it, and a tenant factory."""

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

    def counts(self, artifact_id: UUID) -> tuple[int, int, int]:
        """How many Artifact, Embedding, and attribution rows name one identifier."""
        return (
            int(str(scalar(self.connection, COUNT_ARTIFACTS, (artifact_id,)))),
            int(str(scalar(self.connection, COUNT_EMBEDDINGS, (artifact_id,)))),
            int(str(scalar(self.connection, COUNT_BINDINGS, (artifact_id,)))),
        )


def attribute(
    cursor: Cursor,
    artifact_id: UUID,
    client_id: UUID,
    *,
    confidence: float = CONFIDENCE,
) -> UUID:
    """Write one Attribution_Version on a caller's cursor, every value bound.

    This is the shape the attribution module will take: the row goes on the
    cursor the Artifact was written on, so it belongs to that transaction rather
    than to one of its own.
    """
    identifier = uuid4()
    cursor.execute(
        INSERT_BINDING,
        (
            identifier,
            artifact_id,
            ArtifactKind.DERIVED_ARTIFACT.value,
            client_id,
            METHOD,
            confidence,
            MOMENT,
        ),
    )
    return identifier


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
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
        yield Cluster(store=store, connection=fresh_schema)


# ---------------------------------------------------------------------------
# Both rows land
# ---------------------------------------------------------------------------


def test_an_artifact_and_its_attribution_land_in_one_transaction(cluster: Cluster) -> None:
    """The row and the record of who owns its content commit together.

    The attribution row is read back for its own columns rather than merely
    counted, because an attribution that landed with the wrong Client or the wrong
    kind would satisfy a count and still leave the Artifact outside the sweep for
    the Client whose data it holds. It is also read back as a current version,
    carrying neither a validity end nor a superseding reference, because only a
    current version admits an Artifact to that Client's scope.
    """
    owner = cluster.tenant()
    record = cluster.artifact(owner, state=EmbeddingState.PENDING)

    def body(cursor: Cursor) -> UUID:
        written = insert_artifact(cursor, record, embedding_state=EmbeddingState.PENDING)
        attribute(cursor, written, owner)
        return written

    artifact_id = cluster.store.in_serializable(body)

    assert artifact_id == record.id
    assert cluster.counts(record.id) == (1, 0, 1)
    with cluster.connection.cursor() as opened:
        opened.execute(SELECT_BINDING_ROW, (record.id,))
        row = opened.fetchone()
    assert row is not None
    assert row[0] == ArtifactKind.DERIVED_ARTIFACT.value
    assert row[1] == owner
    assert row[2] == METHOD
    assert row[3] == pytest.approx(CONFIDENCE)
    assert row[4] is None
    assert row[5] is None


def test_every_attribution_of_one_artifact_lands_with_it(cluster: Cluster) -> None:
    """An Artifact holding two Clients' data is attributed to both or to neither.

    One transaction per Artifact rather than one per attribution: a Derived_Artifact
    derived from two tenants' content carries an attribution row per tenant, and a
    partial set of them would understate the scope of an erasure for either.
    """
    first = cluster.tenant()
    second = cluster.tenant()
    record = cluster.artifact(first, state=EmbeddingState.PENDING)

    def body(cursor: Cursor) -> None:
        written = insert_artifact(cursor, record, embedding_state=EmbeddingState.PENDING)
        attribute(cursor, written, first)
        attribute(cursor, written, second)

    cluster.store.in_serializable(body)

    assert cluster.counts(record.id) == (1, 0, 2)


# ---------------------------------------------------------------------------
# A refused attribution takes the Artifact with it
# ---------------------------------------------------------------------------


def refuse_by_absent_client(cursor: Cursor, artifact_id: UUID, _owner: UUID) -> None:
    """Name a Client no row holds, which reaches the reference on the tenant column."""
    attribute(cursor, artifact_id, uuid4())


def refuse_by_impossible_confidence(cursor: Cursor, artifact_id: UUID, owner: UUID) -> None:
    """Carry a confidence outside the unit interval, which reaches the range check."""
    attribute(cursor, artifact_id, owner, confidence=IMPOSSIBLE_CONFIDENCE)


def refuse_by_repeated_pair(cursor: Cursor, artifact_id: UUID, owner: UUID) -> None:
    """State the same Artifact and Client twice, which reaches the unique pair."""
    attribute(cursor, artifact_id, owner)
    attribute(cursor, artifact_id, owner)


# Each refusal with the fault the cluster names it by, so a case that failed for
# some other reason is not read as the case it was written for.
REFUSALS: Final[tuple[tuple[Callable[[Cursor, UUID, UUID], None], str], ...]] = (
    (refuse_by_absent_client, "foreign key"),
    (refuse_by_impossible_confidence, "CHECK constraint"),
    (refuse_by_repeated_pair, "duplicate key value"),
)


@pytest.mark.parametrize(
    ("refuse", "fault"),
    REFUSALS,
    ids=["absent client", "impossible confidence", "repeated pair"],
)
def test_a_refused_attribution_leaves_no_artifact_row(
    cluster: Cluster,
    refuse: Callable[[Cursor, UUID, UUID], None],
    fault: str,
) -> None:
    """The rollback is the cluster's, and it takes the Artifact with it every time.

    Three refusals of three different kinds, so what is asserted is the framing of
    the transaction rather than a reaction to one constraint: a reference, a range
    check, and a uniqueness the schema holds. In each case the Artifact insert has
    already succeeded when the attribution write is refused, which is the ordering
    that makes the claim mean anything.
    """
    owner = cluster.tenant()
    record = cluster.artifact(owner, state=EmbeddingState.PENDING)

    def body(cursor: Cursor) -> None:
        written = insert_artifact(cursor, record, embedding_state=EmbeddingState.PENDING)
        refuse(cursor, written, owner)

    with pytest.raises(Exception, match=fault):
        cluster.store.in_serializable(body)

    assert cluster.counts(record.id) == (0, 0, 0)


# ---------------------------------------------------------------------------
# All three tables, one transaction
# ---------------------------------------------------------------------------


def test_an_artifact_its_vector_and_its_attribution_are_one_transaction(
    cluster: Cluster,
) -> None:
    """Three tables, one commit, and the write path that composes two of them.

    The Artifact and its Embedding go in through the store's own composing write,
    and the attribution follows on the same cursor, which is how a capture path
    assembles all three.
    """
    owner = cluster.tenant()
    record = cluster.artifact(owner, state=EmbeddingState.EMBEDDED)

    def body(cursor: Cursor) -> None:
        written = insert_artifact_with_embedding(
            cursor,
            record,
            EmbeddingWrite(
                artifact_id=record.id,
                artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                client_id=owner,
                provider=PROVIDER,
                model_id=MODEL,
                vec=unit_vector(),
                expires_at=MOMENT + RETENTION,
            ),
        )
        assert written.embedding_id is not None
        attribute(cursor, written.artifact_id, owner)

    cluster.store.in_serializable(body)

    assert cluster.counts(record.id) == (1, 1, 1)


def test_a_refusal_after_the_vector_landed_leaves_neither_row(cluster: Cluster) -> None:
    """The Artifact and the Embedding are both already written when the refusal arrives.

    This is the case the Artifact-and-Embedding assertion cannot make, because
    there the refused write is the second of two. Here two statements have
    succeeded before the third is refused, so what the cluster discards is a
    partial write across two tables rather than a single insert.
    """
    owner = cluster.tenant()
    record = cluster.artifact(owner, state=EmbeddingState.EMBEDDED)

    def body(cursor: Cursor) -> None:
        written = insert_artifact_with_embedding(
            cursor,
            record,
            EmbeddingWrite(
                artifact_id=record.id,
                artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                client_id=owner,
                provider=PROVIDER,
                model_id=MODEL,
                vec=unit_vector(),
                expires_at=MOMENT + RETENTION,
            ),
        )
        attribute(cursor, written.artifact_id, uuid4())

    with pytest.raises(Exception, match="foreign key"):
        cluster.store.in_serializable(body)

    assert cluster.counts(record.id) == (0, 0, 0)
