"""The capability probes and the two neighbour forms against a live instance.

The unit module asserts the shape of what the probes send and record. This one
asserts the four things only a cluster can answer.

The zone-configuration probe measures this cluster's own collection interval. The
proof is a comparison against the same interval read by an independent route, in
the configuration's other rendering, so an agreement here is an agreement with
the cluster rather than with a pattern matching itself. The recorded row is then
read back through the historical read module, which owns the form that count is
written in and refuses every other form: the contract between the probe that
writes and the module that reads is a live round trip here rather than two
assertions about one constant.

The index-definition probe records what this cluster reports about the vector
index. What it records is the operator class the cluster names, which is the fact
that makes write-time unit normalisation load-bearing, and not a claim about any
query plan.

The backup probe asks this cluster to plan a self-managed backup and records
whether it would. It costs no data movement, creates no job, and leaves nothing
behind, and the target it planned against carries credentials in its query
parameters, so the row it wrote is read back out of the cluster and searched for
them.

The two neighbour forms answer the same question. Both are run over one corpus of
vectors placed at known angles, and their results are compared to each other row
for row, ordering included, with a ceiling and without, and over tenancy cases
chosen so a fallback narrower or wider than the primary path would show: content
attributed to a Client that does not own it, and content attributed to two
permitted Clients at once.
"""

from __future__ import annotations

import hashlib
import math
import re
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
from molt.store.capability import (
    GC_HORIZON_SECONDS,
    SELF_MANAGED_BACKUP,
    VECTOR_INDEX,
    Capability,
    CapabilityRecord,
    capabilities,
    probe_platform,
)
from molt.store.embeddings import (
    NEAREST_SCAN_STATEMENT,
    NEAREST_STATEMENT,
    EmbeddingWrite,
    Neighbour,
    insert_artifact_with_embedding,
    nearest,
    select_nearest,
    vector_text,
)
from molt.store.historical import GcHorizon, gc_horizon
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes and reads the fixtures make, parameterised in full. This module
# owns no Client insert and no attribution write, so those rows are placed here
# rather than through a module under test.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
SELECT_CAPABILITY_ROW: Final[str] = (
    "SELECT available, detail, checked_at FROM capability WHERE name = %s"
)

# The collection interval read by the route the probe does not use. The zone
# configuration has two renderings and the probe reads the statement one, so the
# other is what an independent reading comes from.
ZONE_YAML_QUERY: Final[str] = (
    "SELECT raw_config_yaml FROM crdb_internal.zones WHERE target = 'RANGE default'"
)
_YAML_INTERVAL: Final[re.Pattern[str]] = re.compile(r"ttlseconds:\s*(?P<seconds>\d+)", re.ASCII)

# A backup target of the shape an operator configures, carrying a credential in
# its query parameters exactly as one may. Nothing the cluster stores may hold it.
BACKUP_SCHEME: Final[str] = "s3"
TARGET_MARKER: Final[str] = "a-key-that-must-not-be-recorded"
BACKUP_TARGET: Final[str] = (
    f"{BACKUP_SCHEME}://operator-owned/molt?AWS_ACCESS_KEY_ID={TARGET_MARKER}"
)

# The operator class this platform reports for the index, and the ordering that
# class serves, which is why every vector is unit length before it is written.
REPORTED_OPERATOR_CLASS: Final[str] = "vector_l2_ops"

# The provider and the model every row this module writes records.
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# The angles the corpus places vectors at, ascending in distance from the query
# vector, so an ordering claim is a claim about known values.
ANGLES: Final[tuple[float, ...]] = (0.1, 0.3, 0.6, 1.0, 1.4)

# A ceiling that admits the nearer half of the corpus and excludes the rest.
CEILING_ANGLE: Final[float] = 0.7

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes in width."""
    return hashlib.sha256(label.encode()).hexdigest()


def vector_at(angle: float) -> tuple[float, ...]:
    """A unit vector at a known angle from the query vector of these examples."""
    components = [0.0] * EMBEDDING_DIMENSION
    components[0] = math.cos(angle)
    components[1] = math.sin(angle)
    return tuple(components)


def expected_distance(angle: float) -> float:
    """The cosine distance from the query vector for a vector at an angle."""
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
class Cluster:
    """A schema holding every migration, a store over it, and a corpus factory."""

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

    def bind(self, artifact_id: UUID, client_id: UUID) -> UUID:
        """Attribute one Artifact to one tenant as a current Attribution_Version."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_BINDING,
            (identifier, artifact_id, "derived_artifact", client_id, "scope", 0.9, MOMENT),
        )
        return identifier

    def place(self, owner: UUID, angles: tuple[float, ...]) -> tuple[UUID, ...]:
        """Write one attributed Artifact and Embedding per angle, in one transaction."""
        records = [self._artifact(owner) for _ in angles]

        def body(cursor: Cursor) -> None:
            for record, angle in zip(records, angles, strict=True):
                insert_artifact_with_embedding(
                    cursor,
                    record,
                    EmbeddingWrite(
                        artifact_id=record.id,
                        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                        client_id=owner,
                        provider=PROVIDER,
                        model_id=MODEL,
                        vec=vector_at(angle),
                        expires_at=MOMENT + RETENTION,
                    ),
                )

        self.store.in_serializable(body)
        for record in records:
            self.bind(record.id, owner)
        return tuple(record.id for record in records)

    def measured_interval(self) -> int:
        """The collection interval, read by the rendering the probe does not read."""
        rendered = scalar(self.connection, ZONE_YAML_QUERY, ())
        assert isinstance(rendered, str)
        found = _YAML_INTERVAL.search(rendered)
        assert found is not None, "the zone configuration should name a collection interval"
        return int(found.group("seconds"), 10)

    def _artifact(self, owner: UUID) -> DerivedArtifact:
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
            embedding_state=EmbeddingState.EMBEDDED,
            expires_at=MOMENT + RETENTION,
            procedure_confidence=None,
        )


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


@pytest.fixture(scope="module")
def probed(cluster: Cluster) -> CapabilityRecord:
    """Run the probes once against this schema and hold what they produced."""
    return probe_platform(cluster.store, backup_target=BACKUP_TARGET)


@pytest.fixture(scope="module")
def corpus(cluster: Cluster) -> tuple[UUID, dict[float, UUID]]:
    """One tenant holding one attributed Embedding per angle."""
    owner = cluster.tenant()
    placed = cluster.place(owner, ANGLES)
    return owner, dict(zip(ANGLES, placed, strict=True))


# ---------------------------------------------------------------------------
# The zone-configuration probe
# ---------------------------------------------------------------------------


def test_the_horizon_probe_records_the_interval_this_cluster_reports(
    cluster: Cluster,
    probed: CapabilityRecord,
) -> None:
    """The recorded count agrees with the interval read by the other rendering."""
    measured = cluster.measured_interval()

    assert probed.of(GC_HORIZON_SECONDS) == Capability(
        GC_HORIZON_SECONDS,
        available=True,
        detail=str(measured),
    )


@pytest.mark.usefixtures("probed")
def test_the_recorded_horizon_is_read_back_by_the_module_that_owns_the_form(
    cluster: Cluster,
) -> None:
    """The probe writes the one form the historical read parses, proved by reading it."""
    measured = cluster.measured_interval()

    assert gc_horizon(cluster.store) == GcHorizon(seconds=measured)


@pytest.mark.usefixtures("probed")
def test_the_horizon_row_carries_a_reading_instant_from_the_cluster(cluster: Cluster) -> None:
    """A capability row records when the cluster was asked, and the cluster says when."""
    with cluster.connection.cursor() as opened:
        opened.execute(SELECT_CAPABILITY_ROW, (GC_HORIZON_SECONDS,))
        row = opened.fetchone()

    assert row is not None
    assert row[0] is True
    assert isinstance(row[2], datetime)
    assert row[2].tzinfo is not None, "the reading instant carries a zone"


# ---------------------------------------------------------------------------
# The index-definition probe
# ---------------------------------------------------------------------------


def test_the_index_probe_records_the_operator_class_this_cluster_reports(
    probed: CapabilityRecord,
) -> None:
    """The index is present here, and the class it serves is the cluster's own report."""
    assert probed.vector_index is True
    assert probed.vector_index_operator_class == REPORTED_OPERATOR_CLASS


def test_the_probed_record_is_the_one_the_store_holds_afterwards(
    cluster: Cluster,
    probed: CapabilityRecord,
) -> None:
    """The record read once and held is the record the probes produced."""
    assert cluster.store.known_capabilities() == probed
    assert capabilities(cluster.store) == probed


# ---------------------------------------------------------------------------
# The backup probe
# ---------------------------------------------------------------------------


def test_the_backup_probe_records_the_self_managed_path_this_cluster_admits(
    probed: CapabilityRecord,
) -> None:
    """The cluster plans a user-issued backup, so the self-managed path is the path."""
    assert probed.self_managed_backup is True
    assert probed.detail(SELF_MANAGED_BACKUP) == BACKUP_SCHEME


@pytest.mark.usefixtures("probed")
def test_the_stored_backup_row_holds_the_scheme_and_no_part_of_the_target(
    cluster: Cluster,
) -> None:
    """A target may carry a credential, so the row that outlives the probe holds none."""
    with cluster.connection.cursor() as opened:
        opened.execute(SELECT_CAPABILITY_ROW, (SELF_MANAGED_BACKUP,))
        row = opened.fetchone()

    assert row is not None
    assert row[0] is True
    detail = row[1]
    assert detail == BACKUP_SCHEME
    assert isinstance(detail, str)
    assert TARGET_MARKER not in detail
    assert BACKUP_TARGET not in detail


# ---------------------------------------------------------------------------
# The two neighbour forms, over one corpus
# ---------------------------------------------------------------------------


def both_forms(
    cluster: Cluster,
    *,
    permitted: list[UUID],
    limit: int,
    max_cosine: float | None = None,
) -> tuple[tuple[Neighbour, ...], tuple[Neighbour, ...]]:
    """Run the index-served form and the exact-scan form over the same corpus."""

    def body(cursor: Cursor) -> tuple[tuple[Neighbour, ...], tuple[Neighbour, ...]]:
        served = select_nearest(
            cursor,
            vector_at(0.0),
            permitted_clients=permitted,
            limit=limit,
            max_cosine=max_cosine,
            index_served=True,
        )
        scanned = select_nearest(
            cursor,
            vector_at(0.0),
            permitted_clients=permitted,
            limit=limit,
            max_cosine=max_cosine,
            index_served=False,
        )
        return served, scanned

    return cluster.store.read(body)


def test_the_two_forms_return_the_same_rows_in_the_same_order(
    cluster: Cluster,
    corpus: tuple[UUID, dict[float, UUID]],
) -> None:
    """The fallback is the same question answered by a scan, so it ranks alike."""
    owner, by_angle = corpus

    served, scanned = both_forms(cluster, permitted=[owner], limit=len(ANGLES))

    assert [neighbour.artifact_id for neighbour in served] == [by_angle[angle] for angle in ANGLES]
    assert served == scanned
    for neighbour, angle in zip(scanned, ANGLES, strict=True):
        assert neighbour.cosine_distance == pytest.approx(expected_distance(angle), abs=1e-5)


def test_the_two_forms_honour_the_same_cosine_ceiling(
    cluster: Cluster,
    corpus: tuple[UUID, dict[float, UUID]],
) -> None:
    """One ceiling, one admitted set, whichever form computed the distances."""
    owner, by_angle = corpus
    ceiling = expected_distance(CEILING_ANGLE)
    admitted = [by_angle[angle] for angle in ANGLES if expected_distance(angle) <= ceiling]

    served, scanned = both_forms(
        cluster,
        permitted=[owner],
        limit=len(ANGLES),
        max_cosine=ceiling,
    )

    assert [neighbour.artifact_id for neighbour in served] == admitted
    assert served == scanned


def test_the_two_forms_cut_the_result_at_the_same_row_bound(
    cluster: Cluster,
    corpus: tuple[UUID, dict[float, UUID]],
) -> None:
    """A bound of two is the two nearest under both forms, not two arbitrary rows."""
    owner, by_angle = corpus

    served, scanned = both_forms(cluster, permitted=[owner], limit=2)

    assert [neighbour.artifact_id for neighbour in served] == [
        by_angle[ANGLES[0]],
        by_angle[ANGLES[1]],
    ]
    assert served == scanned


def test_the_two_forms_admit_content_a_permitted_client_does_not_own(
    cluster: Cluster,
) -> None:
    """Attribution decides visibility in both forms, so neither is the narrower."""
    owner = cluster.tenant()
    reader = cluster.tenant()
    (artifact_id,) = cluster.place(owner, (0.2,))
    cluster.bind(artifact_id, reader)

    served, scanned = both_forms(cluster, permitted=[reader], limit=10)

    assert artifact_id in {neighbour.artifact_id for neighbour in served}
    assert served == scanned


def test_the_two_forms_return_content_bound_to_two_permitted_clients_once(
    cluster: Cluster,
) -> None:
    """The exact scan's tenancy term is set-valued, so no row arrives twice."""
    owner = cluster.tenant()
    second = cluster.tenant()
    (artifact_id,) = cluster.place(owner, (0.25,))
    cluster.bind(artifact_id, second)

    served, scanned = both_forms(cluster, permitted=[owner, second], limit=10)

    reached = [neighbour.artifact_id for neighbour in served]
    assert reached.count(artifact_id) == 1
    assert served == scanned


def test_a_record_reporting_the_index_absent_puts_the_query_on_the_scan(
    cluster: Cluster,
    corpus: tuple[UUID, dict[float, UUID]],
) -> None:
    """The whole path is driven by the record, and it answers what the index path did."""
    owner, _ = corpus
    held = cluster.store.known_capabilities()
    try:
        cluster.store.prime_capabilities(
            CapabilityRecord((Capability(VECTOR_INDEX, available=True),))
        )
        served = nearest(cluster.store, vector_at(0.0), permitted_clients=[owner], limit=3)

        cluster.store.prime_capabilities(
            CapabilityRecord((Capability(VECTOR_INDEX, available=False),))
        )
        scanned = nearest(cluster.store, vector_at(0.0), permitted_clients=[owner], limit=3)
    finally:
        cluster.store.prime_capabilities(held)

    assert served == scanned
    assert len(served) == 3


def test_both_forms_are_statements_this_cluster_plans(cluster: Cluster) -> None:
    """Neither form is a shape that only reads well in the module's own text."""
    assert NEAREST_STATEMENT != NEAREST_SCAN_STATEMENT
    rendered = vector_text(vector_at(0.0))
    clients: list[UUID] = [uuid4()]
    planned = (
        (NEAREST_STATEMENT, (rendered, clients, None, rendered, None, rendered, 5)),
        (NEAREST_SCAN_STATEMENT, (rendered, clients, 10, None, rendered, None, rendered, 5)),
    )

    with cluster.connection.cursor() as opened:
        for statement, parameters in planned:
            opened.execute(f"EXPLAIN {statement}", parameters)
            assert opened.fetchall()
