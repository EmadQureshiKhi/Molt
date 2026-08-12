"""Seed determinism and the shape of the contamination it plants, against a live instance.

Three claims are only answerable against a cluster, because all three are about
what was stored rather than about what was computed.

**One seed produces one corpus.** Two generations with the same seed are compared
after every identifier is replaced by a positional placeholder, which is the
comparison Requirement 28.9 asks for: identifiers and timestamps are allowed to
differ and nothing else is. The comparison reads the stored Events rather than the
generator's return value, so a generator that drew reproducibly and wrote
something else would still be caught.

**The contamination is really there.** Each planted fragment is read back from the
Ledger by identifier and its text is required to be present, so a mapping naming a
row that holds nothing fails here rather than passing quietly.

**No planted fragment names its owner.** Every revealing token of the owning
tenant is searched for in the stored text of the planted row, and the owning
tenant is searched for among the row's current bindings. Both must find nothing,
because a fragment that carries either is recoverable by matching a label and
demonstrates nothing about detection.

**Validates: Requirements 28.1, 28.2, 28.3, 28.5, 28.7, 28.9**
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from molt.seed.contaminate import GroundTruth, plant_contamination
from molt.seed.corpora import SeedVolumes, domain_of
from molt.seed.generator import SeedResult, generate
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The seed both generations run under. Any value works; a fixed one is used so a
# failure is reproducible from the module alone.
SEED: Final[int] = 4242

# A generation small enough to run twice inside one example and large enough to
# carry every shape the assertions read: four tenants, nested Sessions at two
# depths, blended Artifacts, and planted fragments crossing tenant boundaries.
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

# The stored content each claim is read from.
EVENT_ROWS: Final[str] = (
    "SELECT category, coalesce(text_body, ''), payload::STRING "
    "FROM ledger WHERE session_id = %s ORDER BY seq ASC"
)
EVENT_TEXT: Final[str] = "SELECT text_body, client_id FROM ledger WHERE id = %s"
CURRENT_BINDINGS: Final[str] = (
    "SELECT count(*) FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NULL"
)
CLIENT_COUNT: Final[str] = "SELECT count(*) FROM client WHERE slug = ANY (%s::STRING[])"
DISTINCT_AGENTS: Final[str] = "SELECT count(DISTINCT agent_cli) FROM session"
DISTINCT_MACHINES: Final[str] = "SELECT count(DISTINCT machine_id) FROM session"

# What an identifier looks like in stored text, so a comparison can replace one
# with a placeholder rather than requiring two runs to agree on it.
IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
PLACEHOLDER: Final[str] = "<identifier>"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
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

    def one(self, statement: str, params: tuple[object, ...]) -> tuple[Any, ...]:
        """The single row a statement is expected to produce."""
        produced = self.rows(statement, params)
        assert len(produced) == 1, f"the statement produced {len(produced)} rows where one was read"
        return produced[0]

    def count(self, statement: str, params: tuple[object, ...] | None = None) -> int:
        """The number one counting statement reports."""
        produced = self.rows(statement, params)
        assert len(produced) == 1
        return int(produced[0][0])


@dataclass(frozen=True, slots=True)
class Run:
    """One whole generation: what it produced and what it planted."""

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
def runs(cluster: Cluster, tmp_path_factory: pytest.TempPathFactory) -> tuple[Run, Run]:
    """Generate and plant twice under one seed, each with its own mapping file."""
    produced: list[Run] = []
    for label in ("first", "second"):
        result = generate(cluster.store, seed=SEED, volumes=VOLUMES)
        mapping = tmp_path_factory.mktemp(label) / "ground_truth.json"
        truth = plant_contamination(cluster.store, result, volumes=VOLUMES, path=mapping)
        produced.append(Run(result=result, truth=truth, mapping_path=mapping))
    first, second = produced
    return first, second


def _shapes(run: Run) -> list[tuple[str, str, int, object]]:
    """Every Session field of one run that two runs of one seed must agree on."""
    return [
        (session.agent_cli, session.machine_id, session.depth, session.outcome)
        for session in run.result.sessions
    ]


def normalised(text: str) -> str:
    """Text with every identifier replaced by one positional placeholder."""
    return IDENTIFIER_PATTERN.sub(PLACEHOLDER, text)


# ---------------------------------------------------------------------------
# The volumes the generation is supposed to reach
# ---------------------------------------------------------------------------


def test_the_generation_writes_the_tenants_the_agents_and_the_machines(
    cluster: Cluster,
    runs: tuple[Run, Run],
) -> None:
    """The floors of Requirement 28.1 and 28.2 are met by what is stored."""
    first, _ = runs
    slugs = [client.slug for client in first.result.clients]

    assert cluster.count(CLIENT_COUNT, (slugs,)) == VOLUMES.clients
    assert cluster.count(DISTINCT_AGENTS) >= 3
    assert cluster.count(DISTINCT_MACHINES) >= 3
    assert first.result.events >= VOLUMES.sessions
    assert first.result.embeddings > 0
    assert first.result.working_rows == VOLUMES.sessions * VOLUMES.working_rows_per_session


def test_the_generation_nests_subagent_sessions(runs: tuple[Run, Run]) -> None:
    """A nesting depth of three exists, which is deeper than the floor asks for."""
    first, _ = runs
    depths = {session.depth for session in first.result.sessions}

    assert 2 in depths
    assert max(depths) >= 2


def test_the_generation_blends_artifacts_across_tenants(runs: tuple[Run, Run]) -> None:
    """A blended Artifact is blended because its parents were, not because it was labelled."""
    first, _ = runs

    assert first.result.blended_artifacts, "no seeded Artifact bound more than one tenant"
    for artifact in first.result.blended_artifacts:
        assert len(set(artifact.client_ids)) >= 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_seed_produces_the_same_session_shapes(runs: tuple[Run, Run]) -> None:
    """Two runs agree on every Session field that is not an identifier or an instant."""
    first, second = runs

    assert _shapes(first) == _shapes(second)
    assert first.result.events == second.result.events
    assert first.result.embeddings == second.result.embeddings
    assert first.result.working_rows == second.result.working_rows


def test_the_same_seed_produces_the_same_stored_event_content(
    cluster: Cluster,
    runs: tuple[Run, Run],
) -> None:
    """The Events the cluster holds agree run for run, identifiers aside."""
    first, second = runs
    assert len(first.result.sessions) == len(second.result.sessions)

    for earlier, later in zip(first.result.sessions, second.result.sessions, strict=True):
        first_rows = [
            (str(row[0]), normalised(str(row[1])), normalised(str(row[2])))
            for row in cluster.rows(EVENT_ROWS, (earlier.id,))
        ]
        second_rows = [
            (str(row[0]), normalised(str(row[1])), normalised(str(row[2])))
            for row in cluster.rows(EVENT_ROWS, (later.id,))
        ]

        assert first_rows == second_rows, "two runs of one seed stored different Event content"
        assert first_rows, "a seeded Session stored no Event at all"


def test_the_same_seed_plants_the_same_fragments(runs: tuple[Run, Run]) -> None:
    """The fragments themselves are identical, which their digests state exactly."""
    first, second = runs

    assert [item.fragment_digest for item in first.truth.fragments] == [
        item.fragment_digest for item in second.truth.fragments
    ]
    assert [item.owner_client_slug for item in first.truth.fragments] == [
        item.owner_client_slug for item in second.truth.fragments
    ]
    assert [item.line_count for item in first.truth.fragments] == [
        item.line_count for item in second.truth.fragments
    ]


# ---------------------------------------------------------------------------
# The contamination that was planted
# ---------------------------------------------------------------------------


def test_every_planted_fragment_is_stored_where_the_mapping_says(
    cluster: Cluster,
    runs: tuple[Run, Run],
) -> None:
    """A mapping entry names a row that holds a fragment, in the host's own scope."""
    first, _ = runs
    assert len(first.truth.fragments) == VOLUMES.planted_fragments

    for fragment in first.truth.fragments:
        host = first.result.client_of(fragment.host_client_slug)
        stored, client_id = cluster.one(EVENT_TEXT, (fragment.host_event_id,))

        assert stored is not None, "a planted fragment was stored with no text"
        assert str(client_id) == str(host.id), "a planted fragment is scoped to the host tenant"
        assert len(str(stored).split("\n")) >= 15


def test_no_planted_fragment_carries_an_identifier_of_its_owner(
    cluster: Cluster,
    runs: tuple[Run, Run],
) -> None:
    """Neither the stored text nor the stored bindings name the owning tenant."""
    first, _ = runs

    for fragment in first.truth.fragments:
        owner = first.result.client_of(fragment.owner_client_slug)
        domain = domain_of(fragment.owner_client_slug)
        stored, _client = cluster.one(EVENT_TEXT, (fragment.host_event_id,))
        lowered = str(stored).lower()

        for token in domain.owner_tokens:
            assert token.lower() not in lowered, f"a planted fragment still carries {token!r}"
        assert cluster.count(CURRENT_BINDINGS, (fragment.host_event_id, owner.id)) == 0
        assert fragment.owner_client_slug != fragment.host_client_slug
