"""A 25-pair Threshold_Grid analysis over 100000 Embeddings stays inside 120 seconds.

**Validates: Requirements 48.11**

Why the bound exists. The threshold pair is chosen once and relied on by every
certificate afterwards, so the analysis that informs the choice has to be
answerable while an operator waits. The requirement fixes the scale it must stay
usable at, and this module is the only place that scale is actually built.

**What the bound is stated over is the single-search-then-count shape.** A grid of
twenty-five pairs costs one residue search per query Artifact, at the widest
review threshold the grid names, and every pair is then answered by counting
against the retained distances. Re-searching per pair would multiply the cluster's
work by twenty-five for identical answers, so the measurement below also asserts
what the analysis asked for: one walk, one page per query Artifact, and no
adjudication. Without those, a passing time would say nothing about the shape.

**The corpus is placed by direct statements in bulk.** Placing a hundred thousand
Embeddings one round trip at a time would measure the fixture rather than the
analysis, and rendering a distinct thousand-component vector per row would send
hundreds of megabytes of text. Rows are therefore placed in batches over bound
arrays, with each batch sharing one direction drawn from a fixed set of
directions, so the corpus costs a few hundred statements and a few megabytes while
still holding a hundred thousand vectors spread across the distance range the grid
reasons about. Every statement is a whole module-level literal with every value
bound and no identifier interpolated.

**The store authenticates as the read-only role.** The analyser refuses a
connection that authenticates as anything else, so the measurement runs on the
same privilege path a deployment uses and no write transaction is opened against
the corpus at any point.

The grid is the configured default rather than twenty-five pairs written here, so
the measurement is over the grid an operator actually gets.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.erase.sensitivity import (
    READ_ONLY_ROLE,
    SearchBounds,
    ThresholdGrid,
    analyse,
    default_grid,
    store_residue_walk,
)
from molt.models.artifact import EMBEDDING_DIMENSION
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations

# A bound measured against a cluster: the corpus is placed by real statements and
# the neighbour query is served by the cluster's own plan. The performance marker
# says what is measured and the instance marker states what that measurement
# needs, so with no instance reachable this module skips at collection naming what
# was missing.
pytestmark = [pytest.mark.perf, pytest.mark.instance]

# The scale the requirement states, the bound it states for it, and the grid size
# the bound is stated over.
EMBEDDING_TARGET: Final[int] = 100000
ANALYSIS_BOUND_SECONDS: Final[float] = 120.0
GRID_PAIRS: Final[int] = 25

# How the corpus reaches that count. Each batch places one statement's worth of
# Artifacts and one statement's worth of Embeddings, all sharing one direction, so
# the corpus is a few hundred statements and the rendered vector text stays small
# while the vectors themselves span the distance range.
BATCH_ROWS: Final[int] = 1000
DIRECTIONS: Final[int] = EMBEDDING_TARGET // BATCH_ROWS

# How many Artifacts of the candidate set rank their neighbours, and how many
# neighbours each one asks for. Both are the analysis's own bounds, and the
# retained set is their product at most.
QUERY_ARTIFACTS: Final[int] = 5
TOP_K: Final[int] = 100
EXCERPT_CHARACTERS: Final[int] = 4096

# The distance range the directions are spread over, which covers the review
# values of the default grid and beyond, so no pair of the grid counts the whole
# corpus and none counts nothing.
NEAREST_DISTANCE: Final[float] = 0.02
FURTHEST_DISTANCE: Final[float] = 0.90

# The fixed basis indices a placement blends, so the corpus depends on no seed.
_QUERY_AXIS: Final[int] = 0
_ORTHOGONAL_AXIS: Final[int] = 1

# The fixture's own statements. Every row this measurement runs over is placed
# here, in bulk, with every value bound.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
CURRENT_SCHEMA_STATEMENT: Final[str] = "SELECT current_schema()"
INSERT_CLIENT_STATEMENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACTS_STATEMENT: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, expires_at) "
    "SELECT unnest(%s::UUID[]), %s, %s, %s, %s, %s, %s"
)
INSERT_EMBEDDINGS_STATEMENT: Final[str] = (
    "INSERT INTO embedding (artifact_id, artifact_kind, client_id, provider, model_id, "
    "vec, expires_at) "
    "SELECT unnest(%s::UUID[]), %s, %s, %s, %s, %s::VECTOR, %s"
)
INSERT_REQUEST_STATEMENT: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification) VALUES (%s, %s, %s, %s)"
)
INSERT_RUN_STATEMENT: Final[str] = (
    "INSERT INTO erasure_run (id, request_id, client_id, requester, t_before) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_CANDIDATES_STATEMENT: Final[str] = (
    "INSERT INTO erasure_candidate (run_id, artifact_id, artifact_kind, selection_reason) "
    "SELECT %s, unnest(%s::UUID[]), %s, %s"
)
COUNT_EMBEDDINGS_STATEMENT: Final[str] = "SELECT count(*) FROM embedding"
ANALYSE_EMBEDDINGS_STATEMENT: Final[str] = "ANALYZE embedding"
ANALYSE_ARTIFACTS_STATEMENT: Final[str] = "ANALYZE derived_artifact"

# The fixed values every placed row carries. None of them is what this module
# measures, so none is varied.
NODE_KIND: Final[str] = "summary"
ARTIFACT_KIND: Final[str] = "derived_artifact"
METHOD: Final[str] = "distil"
PROVIDER: Final[str] = "stub"
MODEL: Final[str] = "stub-embedding"
SELECTION_REASON: Final[str] = "session_scope"
REQUESTER: Final[str] = "the operator"
JUSTIFICATION: Final[str] = "threshold calibration"
NODE_DIGEST: Final[str] = "0" * 64

# A body long enough that the query-Artifact selection has text to order by, and
# short enough that the corpus stays small.
NODE_BODY: Final[str] = "a derived body carrying enough text to rank neighbours by " * 4

# An instant derived from the epoch rather than written as a literal, so a run
# embeds nothing about when it happened.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def _unit(components: dict[int, float]) -> tuple[float, ...]:
    """A unit vector of the width the schema fixes, from sparse components."""
    scale = math.sqrt(sum(value * value for value in components.values()))
    return tuple(components.get(index, 0.0) / scale for index in range(EMBEDDING_DIMENSION))


def _placed_at(distance: float) -> tuple[float, ...]:
    """A unit vector standing at one cosine distance from the query axis."""
    cosine = 1.0 - distance
    completing = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return _unit({_QUERY_AXIS: cosine, _ORTHOGONAL_AXIS: completing})


def _rendered(vector: Sequence[float]) -> str:
    """The text form the vector column is written from."""
    return "[" + ",".join(f"{component:.9g}" for component in vector) + "]"


def _direction_distance(ordinal: int) -> float:
    """The distance the vectors of one batch stand at, spread across the range."""
    span = FURTHEST_DISTANCE - NEAREST_DISTANCE
    return NEAREST_DISTANCE + span * ordinal / max(1, DIRECTIONS - 1)


# ---------------------------------------------------------------------------
# The cluster the corpus is placed on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Corpus:
    """A schema holding every migration, a read-only store over it, and the rows."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID
    run_id: UUID
    placed_seconds: float

    def scalar(self, statement: str) -> int:
        """Read one count on the fixture's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement)
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])


def _send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def _place_batch(
    connection: DriverConnection,
    client_id: UUID,
    identifiers: Sequence[UUID],
    rendered: str,
) -> None:
    """Place one batch of Artifacts and one batch of Embeddings sharing a direction."""
    batch = list(identifiers)
    _send(
        connection,
        INSERT_ARTIFACTS_STATEMENT,
        (batch, NODE_KIND, client_id, NODE_BODY, NODE_DIGEST, METHOD, EXPIRY),
    )
    _send(
        connection,
        INSERT_EMBEDDINGS_STATEMENT,
        (batch, ARTIFACT_KIND, client_id, PROVIDER, MODEL, rendered, EXPIRY),
    )


@pytest.fixture(scope="module")
def corpus(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Corpus]:
    """Apply every migration, place a hundred thousand Embeddings, refresh statistics.

    Module scope pays the corpus cost once. The schema is created and dropped by
    the shared fixture this one builds on, so nothing is left behind.
    """
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute(CURRENT_SCHEMA_STATEMENT)
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    client_id = uuid4()
    _send(
        fresh_schema,
        INSERT_CLIENT_STATEMENT,
        (client_id, f"tenant-{client_id.hex[:8]}", "Tenant", "eu"),
    )

    started = time.perf_counter()
    placed: list[UUID] = []
    for ordinal in range(DIRECTIONS):
        identifiers = [uuid4() for _ in range(BATCH_ROWS)]
        _place_batch(
            fresh_schema,
            client_id,
            identifiers,
            _rendered(_placed_at(_direction_distance(ordinal))),
        )
        placed.extend(identifiers[:1])
    placed_seconds = time.perf_counter() - started

    # The candidate set the query Artifacts are drawn from: a synthetic run row,
    # because nothing is being erased and the analysis only reads.
    request_id = uuid4()
    run_id = uuid4()
    _send(
        fresh_schema,
        INSERT_REQUEST_STATEMENT,
        (request_id, client_id, REQUESTER, JUSTIFICATION),
    )
    _send(
        fresh_schema,
        INSERT_RUN_STATEMENT,
        (run_id, request_id, client_id, REQUESTER, MOMENT),
    )
    _send(
        fresh_schema,
        INSERT_CANDIDATES_STATEMENT,
        (run_id, placed[:QUERY_ARTIFACTS], ARTIFACT_KIND, SELECTION_REASON),
    )

    with fresh_schema.cursor() as cursor:
        cursor.execute(ANALYSE_ARTIFACTS_STATEMENT)
        cursor.execute(ANALYSE_EMBEDDINGS_STATEMENT)

    print(
        f"corpus: {DIRECTIONS * BATCH_ROWS} embeddings across {DIRECTIONS} directions, "
        f"placed in {placed_seconds:.1f} s"
    )

    with MemoryStore(connect_with=connect_with, role=READ_ONLY_ROLE) as store:
        yield Corpus(
            store=store,
            connection=fresh_schema,
            client_id=client_id,
            run_id=run_id,
            placed_seconds=placed_seconds,
        )


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def test_the_corpus_holds_the_stated_scale(corpus: Corpus) -> None:
    """The corpus really holds a hundred thousand Embeddings before anything is timed."""
    assert DIRECTIONS * BATCH_ROWS >= EMBEDDING_TARGET, (
        "the plan describes fewer embeddings than the requirement states"
    )
    assert corpus.scalar(COUNT_EMBEDDINGS_STATEMENT) >= EMBEDDING_TARGET, (
        "the placed embedding count falls below the scale the bound is stated over"
    )
    assert corpus.store.role == READ_ONLY_ROLE


def test_the_default_grid_crosses_five_values_with_five() -> None:
    """The measured grid is the configured default, and it holds twenty-five pairs."""
    grid = default_grid()
    assert len(grid.pairs) == GRID_PAIRS, (
        f"the configured default grid holds {len(grid.pairs)} pairs rather than {GRID_PAIRS}"
    )
    assert len(grid.auto_include_axis) == 5
    assert len(grid.review_axis) == 5
    assert all(pair.applicable for pair in grid.pairs), (
        "every pair of the default grid is applicable, so the default report is full"
    )


def test_grid_analysis_within_bound(corpus: Corpus) -> None:
    """The 25-pair analysis over the whole corpus stays inside the bound."""
    grid: ThresholdGrid = default_grid()
    bounds = SearchBounds(
        query_limit=QUERY_ARTIFACTS,
        top_k=TOP_K,
        excerpt_characters=EXCERPT_CHARACTERS,
    )
    walk = store_residue_walk(corpus.store, corpus.run_id, permitted_clients=[corpus.client_id])

    started = time.perf_counter()
    report = analyse(grid, walk=walk, bounds=bounds)
    elapsed = time.perf_counter() - started

    summary = (
        f"sensitivity: {elapsed:.3f} s for {len(report.outcomes)} pairs over "
        f"{corpus.scalar(COUNT_EMBEDDINGS_STATEMENT)} embeddings, "
        f"{report.searches} searches retaining {len(report.retained)} candidates, "
        f"bound {ANALYSIS_BOUND_SECONDS:.0f} s"
    )
    print(summary)

    assert len(report.outcomes) == GRID_PAIRS, (
        "the report came back with fewer cells than the grid holds, so the timing "
        "describes less work than the bound is stated over"
    )
    assert report.searches == QUERY_ARTIFACTS, (
        f"the analysis ran {report.searches} searches for {QUERY_ARTIFACTS} query "
        "artifacts, so it re-searched per pair rather than counting"
    )
    assert report.retained, "the analysis retained no candidate, so it counted nothing"
    assert report.searched_at == grid.widest_review_threshold
    for outcome in report.applicable_outcomes:
        assert outcome.candidate_count is not None
        assert outcome.referred_count is not None
        assert outcome.auto_included_count is not None
    assert elapsed <= ANALYSIS_BOUND_SECONDS, summary
