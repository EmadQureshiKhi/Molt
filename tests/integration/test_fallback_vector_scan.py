"""The exact-scan fallback, chosen by the capability record and visible when chosen.

`test_capability_probes.py` compares the two statement forms against each other
over one corpus, with a ceiling and without, and over tenancy cases a narrower or
wider fallback would show in. Those comparisons drive the forms directly, by
naming which one to send. This module asks the two questions that surround them,
both of which are about the choice rather than about the forms.

**Is the choice actually driven by the record, and is the record read the way the
capability module says it reads?** A row saying the index is absent is a cluster
that was asked and said no, and that is what a fallback exists for. No row at all
is a cluster nobody asked, which is not evidence of absence: every capability in
this design is present on the delivered tier, so an unprobed record must leave the
primary path in place rather than degrade on the strength of a missing row. Those
two cases are one line apart in the module that decides, and they are the
difference between a deployment running index-served and a deployment quietly
running a scan.

**Is taking the fallback visible?** Requirement 10.11 asks for a
`store.vector_index_unavailable` measurement, because a tier running on the scan is
otherwise merely slower, and slower is what an operator notices last. So the
measurement is asserted where it is emitted: once per query answered by the scan,
and not at all for a query answered the primary way. That is the observability half
of the graceful degradation Requirement 32.1 asks for, and no other suite reads
this counter.

The equality of the two answers is then asserted once more here, but through
`nearest` and with a cosine ceiling, so what is exercised is the whole path from
the record to the rows: on this cluster the index exists whatever the record says,
so a record reporting it absent is the one condition under which both answers can
be compared over the same corpus at the same instant.
"""

from __future__ import annotations

import hashlib
import math
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
from molt.store.capability import VECTOR_INDEX, Capability, CapabilityRecord
from molt.store.embeddings import (
    VECTOR_INDEX_UNAVAILABLE_METRIC,
    EmbeddingWrite,
    index_served,
    insert_artifact_with_embedding,
    nearest,
)
from molt.store.migrate import apply_migrations
from molt.telemetry import Telemetry
from molt.telemetry import current as current_telemetry
from molt.telemetry import reset as reset_telemetry

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
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)

# The provider and the model every row this module writes records.
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# The angles the corpus places vectors at, ascending in distance from the query
# vector, so an ordering claim is a claim about known values.
ANGLES: Final[tuple[float, ...]] = (0.1, 0.35, 0.6, 0.9, 1.2, 1.5)

# An angle whose distance admits the nearer part of the corpus and excludes the
# rest, so a ceiling that was ignored would show as a longer result.
CEILING_ANGLE: Final[float] = 0.7

# How many results each query asks for: more than the corpus holds under the
# ceiling, so the ceiling rather than the row bound is what shortens the answer.
PAGE: Final[int] = 10

# The counter key the fallback measurement is held under. It carries no
# dimensions, so the combination is the name and an empty tuple.
FALLBACK_COUNTER: Final[tuple[str, tuple[tuple[str, str], ...]]] = (
    VECTOR_INDEX_UNAVAILABLE_METRIC,
    (),
)

# The two records the choice is driven by, plus the record of a cluster nobody
# asked. The last one is empty rather than absent-valued, because that is what a
# store hands out before any probe has run.
INDEX_PRESENT: Final[CapabilityRecord] = CapabilityRecord(
    (Capability(VECTOR_INDEX, available=True),)
)
INDEX_ABSENT: Final[CapabilityRecord] = CapabilityRecord(
    (Capability(VECTOR_INDEX, available=False),)
)
UNPROBED: Final[CapabilityRecord] = CapabilityRecord()

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
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
            send(
                self.connection,
                INSERT_BINDING,
                (uuid4(), record.id, "derived_artifact", owner, "scope", 0.9, MOMENT),
            )
        return tuple(record.id for record in records)

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
def corpus(cluster: Cluster) -> tuple[UUID, dict[float, UUID]]:
    """One tenant holding one attributed Embedding per angle."""
    owner = cluster.tenant()
    placed = cluster.place(owner, ANGLES)
    return owner, dict(zip(ANGLES, placed, strict=True))


@pytest.fixture
def reported(cluster: Cluster) -> Iterator[Callable[[CapabilityRecord], None]]:
    """Establish the record the path choice reads, and put back what was held."""
    held = cluster.store.known_capabilities()
    try:
        yield cluster.store.prime_capabilities
    finally:
        cluster.store.prime_capabilities(held)


@pytest.fixture
def measurements() -> Iterator[Telemetry]:
    """A fresh in-process telemetry instance, so a counter reading is this example's.

    The instance is discarded afterwards as well as beforehand, so no example here
    leaves a counter behind for another to read.
    """
    reset_telemetry()
    try:
        yield current_telemetry()
    finally:
        reset_telemetry()


def fallbacks(measured: Telemetry) -> float:
    """How many times the fallback measurement has been emitted on this instance."""
    return measured.counters().get(FALLBACK_COUNTER, 0.0)


# ---------------------------------------------------------------------------
# The choice, and how the record is read
# ---------------------------------------------------------------------------


def test_a_record_reporting_the_index_absent_chooses_the_scan_and_says_so(
    cluster: Cluster,
    reported: Callable[[CapabilityRecord], None],
    measurements: Telemetry,
) -> None:
    """A cluster that was asked and said no is what a fallback is for, and it is recorded."""
    reported(INDEX_ABSENT)

    assert index_served(cluster.store) is False
    assert fallbacks(measurements) == 1.0


def test_a_record_reporting_the_index_present_leaves_the_primary_path_unmeasured(
    cluster: Cluster,
    reported: Callable[[CapabilityRecord], None],
    measurements: Telemetry,
) -> None:
    """Nothing degraded, so nothing is recorded as degraded."""
    reported(INDEX_PRESENT)

    assert index_served(cluster.store) is True
    assert fallbacks(measurements) == 0.0


def test_an_unprobed_record_leaves_the_primary_path_in_place(
    cluster: Cluster,
    reported: Callable[[CapabilityRecord], None],
    measurements: Telemetry,
) -> None:
    """A missing row is a cluster nobody asked, which is not evidence of an absent index.

    This is the case the two above do not cover and the one a deployment meets
    first: a component that has not probed yet, or a reading role not granted the
    read. Degrading on it would put the delivered cluster on the scan for no
    reason and record nothing worth reading.
    """
    reported(UNPROBED)

    assert cluster.store.known_capabilities().probed(VECTOR_INDEX) is False
    assert index_served(cluster.store) is True
    assert fallbacks(measurements) == 0.0


def test_the_measurement_is_emitted_once_for_each_query_the_scan_answers(
    cluster: Cluster,
    corpus: tuple[UUID, dict[float, UUID]],
    reported: Callable[[CapabilityRecord], None],
    measurements: Telemetry,
) -> None:
    """The counter follows queries rather than the record, so a tier's load is visible.

    A record read once and held would make a single measurement at start-up easy
    to mistake for a tier running on the scan briefly. What Requirement 10.11 needs
    is the count of queries answered that way, which is what this asserts.
    """
    owner, _ = corpus
    reported(INDEX_ABSENT)

    for _ in range(3):
        assert nearest(cluster.store, vector_at(0.0), permitted_clients=[owner], limit=PAGE)

    assert fallbacks(measurements) == 3.0


# ---------------------------------------------------------------------------
# The two answers, driven end to end by the record
# ---------------------------------------------------------------------------


def test_the_record_driven_paths_agree_on_ordering_and_on_the_ceiling(
    cluster: Cluster,
    corpus: tuple[UUID, dict[float, UUID]],
    reported: Callable[[CapabilityRecord], None],
) -> None:
    """Same corpus, same instant, same rows, whichever path the record chose.

    Both answers are taken through the store's own query rather than by naming a
    form, so what is compared is the path from the record to the rows. The ceiling
    is part of it because a fallback that computed distances correctly and admitted
    them by a different rule would rank alike and answer differently.
    """
    owner, by_angle = corpus
    ceiling = expected_distance(CEILING_ANGLE)
    admitted = [by_angle[angle] for angle in ANGLES if expected_distance(angle) <= ceiling]
    assert 0 < len(admitted) < len(ANGLES), "the ceiling should admit some of the corpus"

    reported(INDEX_PRESENT)
    served = nearest(
        cluster.store,
        vector_at(0.0),
        permitted_clients=[owner],
        limit=PAGE,
        max_cosine=ceiling,
    )
    reported(INDEX_ABSENT)
    scanned = nearest(
        cluster.store,
        vector_at(0.0),
        permitted_clients=[owner],
        limit=PAGE,
        max_cosine=ceiling,
    )

    assert [neighbour.artifact_id for neighbour in served] == admitted
    assert served == scanned
