"""The separated ground truth, and recovery by vector similarity alone.

The whole point of planting contamination the way the seed does is that finding it
takes a distance rather than a match. This module asserts both halves of that.

**The answer is not in the cluster.** The mapping is a file, nothing in the
database references it, and no seeded row carries the owning tenant for a planted
fragment. A detector that could read the mapping would be reading the answer rather
than finding it, which is why the separation is asserted rather than assumed.

**A keyword search finds nothing.** Every revealing token of every owning tenant is
searched for, case-insensitively, across the stored text of every row that is not
that tenant's own. The count must be zero: there is no label to match on.

**A nearest-neighbour search finds everything.** The query vectors are drawn from
each tenant's *own* stored content, read out of the cluster under that tenant's
scope, exactly as the residue phase draws them. The mapping is opened only at the
end, to check an answer that was produced without it. The neighbour query is the
store's own, so what is exercised is the delivered search rather than a search
written here.

**Validates: Requirements 28.5, 28.6, 36.14, 48.4**
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID

import pytest

from molt.seed.contaminate import GroundTruth, load_ground_truth, plant_contamination
from molt.seed.corpora import SeedVolumes, domain_of
from molt.seed.generator import SeedResult, generate
from molt.seed.vectors import seed_vector
from molt.store import Connection, MemoryStore
from molt.store.embeddings import nearest
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

SEED: Final[int] = 8135

VOLUMES: Final[SeedVolumes] = SeedVolumes(
    clients=4,
    sessions=6,
    events=90,
    subagent_sessions_depth_two=1,
    subagent_sessions_depth_three=1,
    blended_artifacts=2,
    planted_fragments=3,
    working_rows_per_session=1,
)

# The tenant's own stored content, which is where a residue query is drawn from.
OWN_TEXTS: Final[str] = (
    "SELECT id, text_body FROM ledger "
    "WHERE client_id = %s AND text_body IS NOT NULL ORDER BY seq ASC LIMIT %s"
)

# The keyword search, over every row that is not the named tenant's own. This is
# the search an explicit sweep for a tenant would amount to if a planted fragment
# carried a label, and it is required to find nothing.
KEYWORD_SEARCH: Final[str] = (
    "SELECT count(*) FROM ledger WHERE client_id <> %s AND text_body ILIKE %s"
)
ARTIFACT_KEYWORD_SEARCH: Final[str] = (
    "SELECT count(*) FROM derived_artifact WHERE owner_client_id <> %s AND body ILIKE %s"
)

# Whether anything stored references the mapping file. Nothing may.
MAPPING_REFERENCE_SEARCH: Final[str] = (
    "SELECT count(*) FROM ledger WHERE text_body ILIKE %s OR payload::STRING ILIKE %s"
)
MAPPING_ARTIFACT_SEARCH: Final[str] = "SELECT count(*) FROM derived_artifact WHERE body ILIKE %s"

# Any current binding naming a tenant for one Artifact.
CURRENT_BINDINGS: Final[str] = (
    "SELECT count(*) FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NULL"
)

# How many of a tenant's own rows a residue search draws queries from, and how many
# neighbours each query asks for. Both are bounds a real sweep also carries.
QUERY_LIMIT: Final[int] = 40
NEIGHBOUR_LIMIT: Final[int] = 50

DriverConnection = Any


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

    def count(self, statement: str, params: tuple[object, ...]) -> int:
        """The number one counting statement reports."""
        produced = self.rows(statement, params)
        assert len(produced) == 1
        return int(produced[0][0])


@dataclass(frozen=True, slots=True)
class Seeded:
    """One generation, its planted contamination, and where the mapping was written."""

    result: SeedResult
    truth: GroundTruth
    mapping_path: Path


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
        connection: Connection = opened
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


@pytest.fixture(scope="module")
def seeded(cluster: Cluster, tmp_path_factory: pytest.TempPathFactory) -> Seeded:
    """Generate one corpus and plant the contamination into it."""
    result = generate(cluster.store, seed=SEED, volumes=VOLUMES)
    mapping = tmp_path_factory.mktemp("ground-truth") / "ground_truth.json"
    truth = plant_contamination(cluster.store, result, volumes=VOLUMES, path=mapping)
    return Seeded(result=result, truth=truth, mapping_path=mapping)


def recovered_by_similarity(cluster: Cluster, seeded: Seeded) -> dict[str, set[UUID]]:
    """Search, per tenant, for that tenant's content sitting in another tenant's scope.

    The queries are the tenant's own stored rows and the permitted scope is every
    other tenant, which is the residue phase's shape: an Artifact the tenant is
    bound to is not residue, and an Artifact nobody else may see is not reachable.
    The mapping is not consulted anywhere in here.
    """
    found: dict[str, set[UUID]] = {}
    for client in seeded.result.clients:
        others = [other.id for other in seeded.result.clients if other.id != client.id]
        hits: set[UUID] = set()
        for _identifier, text in cluster.rows(OWN_TEXTS, (client.id, QUERY_LIMIT)):
            for neighbour in nearest(
                cluster.store,
                seed_vector(str(text)),
                permitted_clients=others,
                limit=NEIGHBOUR_LIMIT,
            ):
                hits.add(neighbour.artifact_id)
        found[client.slug] = hits
    return found


# ---------------------------------------------------------------------------
# The separation
# ---------------------------------------------------------------------------


def test_the_ground_truth_is_stored_outside_the_memory_tables(
    cluster: Cluster,
    seeded: Seeded,
) -> None:
    """The mapping is a file, and nothing stored refers to it."""
    assert seeded.mapping_path.is_file()
    assert seeded.mapping_path.read_text(encoding="utf-8").strip()

    for pattern in (f"%{seeded.mapping_path.name}%", "%ground_truth%"):
        assert cluster.count(MAPPING_REFERENCE_SEARCH, (pattern, pattern)) == 0
        assert cluster.count(MAPPING_ARTIFACT_SEARCH, (pattern,)) == 0


def test_the_mapping_reads_back_as_it_was_written(seeded: Seeded) -> None:
    """A reader outside this process gets the same mapping, which is what a report reads."""
    reloaded = load_ground_truth(seeded.mapping_path)

    assert reloaded.seed == seeded.truth.seed
    assert reloaded.host_event_ids == seeded.truth.host_event_ids
    assert len(reloaded.fragments) == VOLUMES.planted_fragments


def test_no_planted_fragment_is_bound_to_the_tenant_that_owns_it(
    cluster: Cluster,
    seeded: Seeded,
) -> None:
    """The explicit sweep cannot reach a planted fragment, because no binding names the owner."""
    for fragment in seeded.truth.fragments:
        owner = seeded.result.client_of(fragment.owner_client_slug)

        assert cluster.count(CURRENT_BINDINGS, (fragment.host_event_id, owner.id)) == 0


# ---------------------------------------------------------------------------
# What a keyword search can and cannot do
# ---------------------------------------------------------------------------


def test_a_keyword_search_over_the_contaminated_content_finds_nothing(
    cluster: Cluster,
    seeded: Seeded,
) -> None:
    """No revealing token of any tenant appears in any other tenant's stored content."""
    for client in seeded.result.clients:
        for token in domain_of(client.slug).owner_tokens:
            pattern = f"%{token}%"

            assert cluster.count(KEYWORD_SEARCH, (client.id, pattern)) == 0, (
                f"a keyword search for {token!r} reached another tenant's content"
            )
            assert cluster.count(ARTIFACT_KEYWORD_SEARCH, (client.id, pattern)) == 0


# ---------------------------------------------------------------------------
# What a nearest-neighbour search can
# ---------------------------------------------------------------------------


def test_a_nearest_neighbour_search_recovers_every_planted_fragment(
    cluster: Cluster,
    seeded: Seeded,
) -> None:
    """Similarity alone reaches every planted fragment the mapping records."""
    found = recovered_by_similarity(cluster, seeded)

    missing = [
        fragment.fragment_index
        for fragment in seeded.truth.fragments
        if fragment.host_event_id not in found[fragment.owner_client_slug]
    ]

    assert not missing, f"similarity did not recover the planted fragments {missing}"


def test_the_recovered_fragment_is_nearer_than_the_hosts_own_content(
    cluster: Cluster,
    seeded: Seeded,
) -> None:
    """A planted fragment is the nearest thing in the host's scope to the owner's own copy.

    The query is the owner's own stored copy, read under the owner's scope, so the
    ranking is a real semantic match rather than a lookup: the planted row carries
    the owner's idiom and the host's rows do not.
    """
    for fragment in seeded.truth.fragments:
        owner = seeded.result.client_of(fragment.owner_client_slug)
        host = seeded.result.client_of(fragment.host_client_slug)
        origin = [
            str(text)
            for identifier, text in cluster.rows(OWN_TEXTS, (owner.id, QUERY_LIMIT))
            if UUID(str(identifier)) == fragment.origin_event_id
        ]
        assert origin, "the owner's own copy of the fragment was not stored"

        ranked = nearest(
            cluster.store,
            seed_vector(origin[0]),
            permitted_clients=[host.id],
            limit=NEIGHBOUR_LIMIT,
        )

        assert ranked, "the neighbour query returned nothing inside the host's scope"
        assert ranked[0].artifact_id == fragment.host_event_id
        assert ranked[0].cosine_distance < 0.5
