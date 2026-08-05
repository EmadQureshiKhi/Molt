"""Property 35: threshold monotonicity, a rectangular grid, and an analysis that reads.

**Validates: Requirements 48.2, 48.3, 48.5, 48.6, 48.8**

This property runs entirely in this process, and that is the right shape for it
rather than a concession. Every clause is about the analyser's own arithmetic —
which retained distances a pair counts, which pairs describe no band at all, which
calls the shape of the search makes unreachable — and none of it is about how a
cluster plans a statement. The analysis reaches the corpus through one injected
walk, so what runs here is the analysis that ships, driven over a corpus this
module places exactly.

**The corpus is the memory content, and it is hashed either side of the run.** The
placed Artifact rows, Embedding rows, Lineage_Edges, and Client_Bindings are held
in one immutable value, and the digest helper hashes each table's canonical
rendering. The neighbour stub answers from that value and holds no way to change
it, so a digest that moved would mean the analysis had found a way to write
through a seam that offers no write. The digests are compared before and after
every example, which is the byte-identical clause stated over the tables the
property names.

**No provider is reachable, and the stub proves the claim is not vacuous.** An
Adjudicator stub is wired into the walk, and it calls a text provider stub for
every candidate of a batch it is handed. The analysis searches with both
thresholds at the grid's widest review value, so every sighting lands in the
auto-inclusion band and no batch is ever dispatched: the provider records nothing.
A control walk at a narrow auto-inclusion threshold over the same corpus and the
same stubs *does* reach the provider, which is what makes the zero a fact about
the analysis rather than about a stub nobody could have called.

**Vectors are placed, not embedded.** A candidate stands at a chosen cosine
distance from a query direction by blending two fixed basis directions, so every
distance in the expectations is a number this module chose. The stub search
applies the ceiling, the ordering, and the bound exactly as the store's own
neighbour query does.

**Grids are drawn in both orders on purpose.** A pair's two thresholds are drawn
from the unit interval and then either ordered or reversed, so the inapplicable
branch is exercised by construction rather than by luck, and the drawn grid holds
between one and thirty-six pairs so the single-search-then-count shape is asserted
at grid sizes above the default's.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid5

from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.erase.residue import (
    AdjudicationBatch,
    Classification,
    QueryArtifact,
    ResidueDetector,
    ResiduePolicy,
    ResidueReport,
    Verdict,
)
from molt.erase.sensitivity import (
    INAPPLICABLE_REASON,
    GroundTruth,
    SearchBounds,
    SensitivityReport,
    ThresholdGrid,
    ThresholdPair,
    analyse,
)
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind
from molt.store.embeddings import Neighbour

# How many examples the property runs.
MAX_EXAMPLES: Final[int] = 100

# The bounds of one contaminated corpus.
MIN_QUERIES: Final[int] = 2
MAX_QUERIES: Final[int] = 3
MIN_FRAGMENTS: Final[int] = 4
MAX_FRAGMENTS: Final[int] = 12
MIN_SWEPT: Final[int] = 1
MAX_SWEPT: Final[int] = 3

# The bounds of one drawn grid. One pair is the smallest grid that is still a
# grid, and thirty-six is above the twenty-five pairs of the default.
MIN_PAIRS: Final[int] = 1
MAX_PAIRS: Final[int] = 36

# How far the query directions are rotated apart, so a fragment is sighted by
# several query Artifacts at genuinely different distances.
QUERY_SEPARATION: Final[float] = 0.04

# The fixed basis indices a placement blends, so no example depends on a seed.
_QUERY_AXIS: Final[int] = 0
_ORTHOGONAL_AXIS: Final[int] = 1
_ROTATION_AXIS: Final[int] = 2

# The distances fragments are planted at. They straddle the whole unit interval,
# because a drawn pair's thresholds are drawn from the whole unit interval.
_PLACEMENTS: Final[tuple[float, ...]] = (0.02, 0.08, 0.18, 0.27, 0.36, 0.48, 0.62, 0.81)

# The bounds the one retained search runs under. Both are above what any example
# plants, so nothing is lost to truncation and the property is about the bands.
TOP_K: Final[int] = 64
EXCERPT_CHARACTERS: Final[int] = 400

# How much a threshold is raised by in the monotonicity check, and the ceiling a
# raised threshold is held under so it stays a cosine distance in the interval.
RAISE_BY: Final[float] = 0.05
UNIT_CEILING: Final[float] = 1.0

# The auto-inclusion threshold the control walk runs at, low enough that the
# fragments above it form a review band the Adjudicator is dispatched for.
CONTROL_AUTO_THRESHOLD: Final[float] = 0.01

# The namespace identifiers are derived in, so an example names the same rows on
# a replay and a failure is reproducible.
_NAMESPACE: Final[UUID] = UUID("00000000-0000-0000-0000-000000000035")

# The two kinds that carry embeddable text, sorted so a draw is reproducible.
_EMBEDDABLE: Final[tuple[ArtifactKind, ...]] = (
    ArtifactKind.DERIVED_ARTIFACT,
    ArtifactKind.EVENT,
)

# The memory-content tables the digest helper covers, which are the four the
# property names.
_TABLES: Final[tuple[str, ...]] = ("artifact", "embedding", "lineage_edge", "client_binding")


def _identifier(label: str) -> UUID:
    """A reproducible identifier for one placed row."""
    return uuid5(_NAMESPACE, label)


def _unit(components: Mapping[int, float]) -> tuple[float, ...]:
    """A unit vector of the fixed width from a sparse set of components."""
    scale = math.sqrt(sum(value * value for value in components.values()))
    return tuple(components.get(index, 0.0) / scale for index in range(EMBEDDING_DIMENSION))


def _query_direction(ordinal: int) -> tuple[float, ...]:
    """The direction of one query Artifact, a small rotation from the query axis."""
    return _unit({_QUERY_AXIS: 1.0, _ROTATION_AXIS: QUERY_SEPARATION * ordinal})


def _placed_at(distance: float) -> tuple[float, ...]:
    """A unit vector standing at exactly one cosine distance from the query axis."""
    cosine = 1.0 - distance
    completing = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return _unit({_QUERY_AXIS: cosine, _ORTHOGONAL_AXIS: completing})


def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """The cosine distance between two unit vectors, computed here rather than read."""
    return 1.0 - sum(a * b for a, b in zip(left, right, strict=True))


# ---------------------------------------------------------------------------
# The memory content, and its digest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlantedArtifact:
    """One Artifact of a contaminated corpus, and where it was placed."""

    artifact_id: UUID
    artifact_kind: ArtifactKind
    owner_client_id: UUID
    vector: tuple[float, ...]
    distance: float
    swept: bool


@dataclass(frozen=True, slots=True)
class MemoryContent:
    """Every memory-content row one example places, held immutably.

    This is what the neighbour stub reads and what the digest helper hashes. It
    exposes no way to change a row, which is the in-process form of the privilege
    fact the analyser relies on against a cluster.
    """

    artifacts: tuple[tuple[str, str, str], ...]
    embeddings: tuple[tuple[str, str], ...]
    lineage_edges: tuple[tuple[str, str], ...]
    client_bindings: tuple[tuple[str, str], ...]

    @classmethod
    def over(cls, planted: Sequence[PlantedArtifact]) -> MemoryContent:
        """Place one Artifact row, Embedding row, edge, and binding per Artifact."""
        return cls(
            artifacts=tuple(
                (str(row.artifact_id), str(row.artifact_kind), str(row.owner_client_id))
                for row in planted
            ),
            embeddings=tuple(
                (str(row.artifact_id), ",".join(f"{value:.17g}" for value in row.vector))
                for row in planted
            ),
            lineage_edges=tuple(
                (str(row.artifact_id), str(_identifier("parent"))) for row in planted
            ),
            client_bindings=tuple(
                (str(row.artifact_id), str(row.owner_client_id)) for row in planted
            ),
        )

    def rows_of(self, table: str) -> tuple[tuple[str, ...], ...]:
        """One table's rows, named so the digest helper covers each table by name."""
        if table == "artifact":
            return self.artifacts
        if table == "embedding":
            return self.embeddings
        if table == "lineage_edge":
            return self.lineage_edges
        return self.client_bindings


def table_digests(content: MemoryContent) -> Mapping[str, str]:
    """Hash every memory-content table, one digest per table.

    Rendering is canonical and byte-oriented: rows in placement order, columns
    separated by a byte no identifier or rendered vector holds, so two digests
    agree exactly when the bytes of the table agree.
    """
    digests: dict[str, str] = {}
    for table in _TABLES:
        digest = hashlib.sha256()
        for row in content.rows_of(table):
            digest.update(b"\x00".join(column.encode() for column in row))
            digest.update(b"\x1e")
        digests[table] = digest.hexdigest()
    return digests


# ---------------------------------------------------------------------------
# One contaminated corpus paired with one grid
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContaminatedCorpus:
    """Query Artifacts, planted fragments, an already-swept set, and the placed rows."""

    client_id: UUID
    queries: tuple[QueryArtifact, ...]
    fragments: tuple[PlantedArtifact, ...]
    swept: tuple[PlantedArtifact, ...]

    @property
    def corpus(self) -> tuple[PlantedArtifact, ...]:
        """Every Artifact the neighbour query may return, swept ones included."""
        return self.fragments + self.swept

    @property
    def swept_ids(self) -> frozenset[UUID]:
        """The explicit candidate set, which the residue set is disjoint from."""
        return frozenset(row.artifact_id for row in self.swept)

    @property
    def content(self) -> MemoryContent:
        """The memory-content rows this corpus stands for."""
        return MemoryContent.over(self.corpus)

    @property
    def ground_truth(self) -> GroundTruth:
        """The planted-fragment mapping, which is what a recovered count is drawn from."""
        return GroundTruth.from_mapping(
            {row.artifact_id: row.owner_client_id for row in self.fragments}
        )

    def nearest_distance(self, planted: PlantedArtifact) -> float:
        """The smallest distance any query Artifact stands from one Artifact."""
        return min(_cosine_distance(planted.vector, query.vector) for query in self.queries)


@dataclass(frozen=True, slots=True)
class Example:
    """One drawn corpus paired with one drawn grid."""

    corpus: ContaminatedCorpus
    grid: ThresholdGrid


@st.composite
def contaminated_corpora(draw: st.DrawFn) -> ContaminatedCorpus:
    """Draw a corpus of stub vectors placed across the unit interval.

    The query Artifacts are a small rotation apart, so a fragment is sighted at
    several distances and the retained distance is the smallest of them. The
    already-swept Artifacts are placed nearest of all, so a grid that failed to
    exclude them would be dominated by them at every pair.
    """
    client_id = _identifier("client")
    query_count = draw(st.integers(min_value=MIN_QUERIES, max_value=MAX_QUERIES))
    queries = tuple(
        QueryArtifact(
            artifact_id=_identifier(f"query-{ordinal}"),
            artifact_kind=draw(st.sampled_from(sorted(_EMBEDDABLE))),
            text_length=1000 - ordinal,
            excerpt=f"the query artifact excerpt {ordinal}",
            vector=_query_direction(ordinal),
        )
        for ordinal in range(query_count)
    )
    distances = draw(
        st.lists(
            st.sampled_from(_PLACEMENTS),
            min_size=MIN_FRAGMENTS,
            max_size=MAX_FRAGMENTS,
        )
    )
    fragments = tuple(
        PlantedArtifact(
            artifact_id=_identifier(f"fragment-{ordinal}"),
            artifact_kind=draw(st.sampled_from(sorted(_EMBEDDABLE))),
            owner_client_id=_identifier(f"owner-{ordinal % 2}"),
            vector=_placed_at(distance),
            distance=distance,
            swept=False,
        )
        for ordinal, distance in enumerate(distances)
    )
    swept_count = draw(st.integers(min_value=MIN_SWEPT, max_value=MAX_SWEPT))
    swept = tuple(
        PlantedArtifact(
            artifact_id=_identifier(f"swept-{ordinal}"),
            artifact_kind=draw(st.sampled_from(sorted(_EMBEDDABLE))),
            owner_client_id=client_id,
            vector=_placed_at(0.001 * (ordinal + 1)),
            distance=0.001 * (ordinal + 1),
            swept=True,
        )
        for ordinal in range(swept_count)
    )
    return ContaminatedCorpus(
        client_id=client_id,
        queries=queries,
        fragments=fragments,
        swept=swept,
    )


@st.composite
def threshold_grids(draw: st.DrawFn) -> Example:
    """Draw a corpus paired with a grid of one to thirty-six pairs.

    Each pair draws two values from the unit interval and then either orders them
    or reverses them. The reversed order is the inapplicable branch, so it is
    drawn rather than hoped for, and both orders appear in the same grid.
    """
    corpus = draw(contaminated_corpora())
    pair_count = draw(st.integers(min_value=MIN_PAIRS, max_value=MAX_PAIRS))
    pairs: list[ThresholdPair] = []
    for _ in range(pair_count):
        first = draw(st.floats(min_value=0.0, max_value=UNIT_CEILING, allow_nan=False))
        second = draw(st.floats(min_value=0.0, max_value=UNIT_CEILING, allow_nan=False))
        reversed_order = draw(st.booleans())
        low, high = min(first, second), max(first, second)
        auto, review = (high, low) if reversed_order else (low, high)
        pairs.append(ThresholdPair(auto_include_threshold=auto, review_threshold=review))
    return Example(corpus=corpus, grid=ThresholdGrid(pairs=tuple(pairs)))


# ---------------------------------------------------------------------------
# The seams the analysis is driven through
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ForbiddenTextProvider:
    """A text provider that records every call, so a call that must not happen shows.

    It answers rather than raising, because a raise would make the no-call clause
    pass for the wrong reason: the control walk below calls it deliberately, and a
    provider that could not answer would prove nothing about the analysis.
    """

    calls: list[str] = field(default_factory=list)

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return "the stub judged this candidate"


@dataclass(slots=True)
class StubAdjudicator:
    """An Adjudicator that calls the text provider once per candidate of a batch."""

    provider: ForbiddenTextProvider
    batches: list[AdjudicationBatch] = field(default_factory=list)

    def adjudicate(self, batch: AdjudicationBatch) -> tuple[Verdict, ...]:
        self.batches.append(batch)
        verdicts: list[Verdict] = []
        for row in batch.candidates:
            reasoning = self.provider.generate(batch.query_artifact.excerpt)
            verdicts.append(
                Verdict(
                    artifact_id=row.artifact_id,
                    classification=Classification.INCLUDE,
                    included=True,
                    decision_reason="adjudicated_include",
                    model_id="stub-model",
                    prompt_digest="0" * 64,
                    reasoning=reasoning,
                )
            )
        return tuple(verdicts)


@dataclass(slots=True)
class StubNeighbours:
    """The neighbour query, answered from the placed Embedding rows.

    The ceiling, the ordering, and the bound are applied exactly as the store's
    own statement applies them. Nothing here writes, and the content it reads is
    an immutable value, so the walk has no seam to mutate through.
    """

    content: MemoryContent
    corpus: tuple[PlantedArtifact, ...]
    client_id: UUID
    pages: int = 0

    def __call__(
        self, query: QueryArtifact, max_cosine: float, limit: int
    ) -> tuple[Neighbour, ...]:
        self.pages += 1
        rows = [
            Neighbour(
                artifact_id=planted.artifact_id,
                artifact_kind=planted.artifact_kind,
                client_id=planted.owner_client_id,
                cosine_distance=_cosine_distance(planted.vector, query.vector),
            )
            for planted in self.corpus
            if _cosine_distance(planted.vector, query.vector) <= max_cosine
        ]
        rows.sort(key=lambda row: (row.cosine_distance, str(row.artifact_id)))
        return tuple(rows[:limit])


@dataclass(slots=True)
class StubAntiJoin:
    """The SQL anti-join, answering which identifiers the candidate set does not hold."""

    swept: frozenset[UUID]

    def __call__(self, ids: Sequence[UUID]) -> frozenset[UUID]:
        return frozenset(identifier for identifier in ids if identifier not in self.swept)


@dataclass(slots=True)
class StubWalk:
    """The one seam the analysis reaches the corpus through, counted and recorded."""

    corpus: ContaminatedCorpus
    neighbours: StubNeighbours
    adjudicator: StubAdjudicator
    calls: list[ResiduePolicy] = field(default_factory=list)

    def __call__(self, policy: ResiduePolicy) -> ResidueReport:
        self.calls.append(policy)
        detector = ResidueDetector(
            policy,
            neighbours=self.neighbours,
            uncandidated_ids=StubAntiJoin(swept=self.corpus.swept_ids),
            adjudicator=self.adjudicator,
            recorder=None,
        )
        return detector.detect(self.corpus.queries, candidate_ids=self.corpus.swept_ids)


def _walk_over(corpus: ContaminatedCorpus) -> tuple[StubWalk, ForbiddenTextProvider]:
    """Build the walk over one corpus and hold on to the provider it could reach."""
    provider = ForbiddenTextProvider()
    neighbours = StubNeighbours(
        content=corpus.content,
        corpus=corpus.corpus,
        client_id=corpus.client_id,
    )
    walk = StubWalk(
        corpus=corpus,
        neighbours=neighbours,
        adjudicator=StubAdjudicator(provider=provider),
    )
    return walk, provider


def _bounds(corpus: ContaminatedCorpus) -> SearchBounds:
    """The bounds every search of an example runs under, above what it plants."""
    return SearchBounds(
        query_limit=len(corpus.queries),
        top_k=TOP_K,
        excerpt_characters=EXCERPT_CHARACTERS,
    )


def _raised(grid: ThresholdGrid) -> ThresholdGrid:
    """The same grid with both thresholds of every pair raised, held in the interval."""
    return ThresholdGrid(
        pairs=tuple(
            ThresholdPair(
                auto_include_threshold=min(UNIT_CEILING, pair.auto_include_threshold + RAISE_BY),
                review_threshold=min(UNIT_CEILING, pair.review_threshold + RAISE_BY),
            )
            for pair in grid.pairs
        )
    )


def _analyse(corpus: ContaminatedCorpus, grid: ThresholdGrid) -> SensitivityReport:
    """Run the analysis over one corpus and grid with a fresh set of stubs."""
    walk, _ = _walk_over(corpus)
    return analyse(grid, walk=walk, bounds=_bounds(corpus), ground_truth=corpus.ground_truth)


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 35: For any corpus with explicit Client_Bindings and planted
# unlabelled fragments, paired with any Threshold_Grid, raising either threshold of a
# pair yields a candidate count no lower than the original pair's count, every pair
# whose auto-inclusion threshold exceeds its review threshold is reported as
# inapplicable rather than evaluated, no Text_Provider call is made for any candidate,
# and every Artifact row, Embedding row, Lineage_Edge, and Client_Binding is
# byte-identical after the analysis to its state before.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(example=threshold_grids())
def test_threshold_grids_are_monotone_and_the_analysis_is_pure(example: Example) -> None:
    corpus, grid = example.corpus, example.grid
    before = table_digests(corpus.content)
    walk, provider = _walk_over(corpus)

    event(f"pairs={len(grid.pairs)}")
    event(f"queries={len(corpus.queries)}")
    event(f"fragments={len(corpus.fragments)}")
    event(f"inapplicable={sum(1 for pair in grid.pairs if not pair.applicable)}")

    report = analyse(
        grid,
        walk=walk,
        bounds=_bounds(corpus),
        ground_truth=corpus.ground_truth,
    )
    after = table_digests(corpus.content)

    # Requirement 48.5 and Property 35: nothing about the memory content moved. The
    # digests cover every table the property names, byte for byte.
    assert after == before, (
        "a memory-content table digest changed across the analysis, so the read-only "
        "path wrote something"
    )
    assert set(before) == set(_TABLES), "the digest helper did not cover every table"

    # Requirement 48.6: no candidate reached the Text_Provider. The Adjudicator was
    # wired in and would have called it, so the zero is a fact about the analysis.
    assert provider.calls == [], (
        f"the analysis reached the text provider {len(provider.calls)} times, and it "
        "must reach it for no candidate"
    )
    assert walk.adjudicator.batches == [], "the analysis dispatched an adjudication batch"

    # One search per query Artifact, once, whatever the grid's size: every pair is
    # answered by counting against the one retained set.
    assert len(walk.calls) == 1, (
        f"the analysis ran {len(walk.calls)} residue walks for {len(grid.pairs)} pairs "
        "rather than one"
    )
    assert walk.neighbours.pages == len(corpus.queries), (
        f"the analysis asked {walk.neighbours.pages} pages for {len(corpus.queries)} "
        "query artifacts"
    )
    assert walk.calls[0].review_threshold == grid.widest_review_threshold
    assert report.searches == len(corpus.queries)

    # Requirement 48.8: the grid stays rectangular. Every drawn pair has an outcome,
    # in grid order, and a reversed pair carries the reason and no counts at all.
    assert len(report.outcomes) == len(grid.pairs)
    for pair, outcome in zip(grid.pairs, report.outcomes, strict=True):
        assert outcome.pair == pair
        if pair.applicable:
            assert outcome.applicable, "an applicable pair was reported as inapplicable"
            continue
        assert not outcome.applicable, (
            f"the pair {pair.auto_include_threshold} over {pair.review_threshold} was "
            "evaluated although its auto-inclusion threshold exceeds its review threshold"
        )
        assert outcome.inapplicable_reason == INAPPLICABLE_REASON
        assert outcome.candidate_count is None
        assert outcome.auto_included_count is None
        assert outcome.referred_count is None
        assert outcome.recovered_count is None

    # Requirement 48.3: every applicable pair's three counts agree with the retained
    # distances, recomputed here rather than read off the report.
    distances = tuple(row.cosine_distance for row in report.retained)
    planted = corpus.ground_truth.fragment_ids
    for outcome in report.applicable_outcomes:
        expected = sum(1 for value in distances if value <= outcome.review_threshold)
        expected_auto = sum(1 for value in distances if value <= outcome.auto_include_threshold)
        assert outcome.candidate_count == expected
        assert outcome.auto_included_count == expected_auto
        assert outcome.referred_count == expected - expected_auto
        recovered = sum(
            1
            for row in report.retained
            if row.artifact_id in planted and row.cosine_distance <= outcome.review_threshold
        )
        assert outcome.recovered_count == recovered, (
            "the recovered planted-fragment count disagrees with the retained set"
        )

    # The retained set is disjoint from the explicit set, so no count is inflated by
    # an Artifact the sweep already holds.
    assert not {row.artifact_id for row in report.retained} & corpus.swept_ids

    # Requirement 48.2 and Property 35: raising either threshold never lowers the
    # candidate count. Asserted both within the drawn grid, where one pair dominates
    # another, and against a second analysis of the same grid raised by a margin.
    for lower in report.applicable_outcomes:
        for higher in report.applicable_outcomes:
            if (
                higher.auto_include_threshold >= lower.auto_include_threshold
                and higher.review_threshold >= lower.review_threshold
            ):
                assert (higher.candidate_count or 0) >= (lower.candidate_count or 0), (
                    "a pair with both thresholds at least as high reported fewer candidates"
                )

    raised_grid = _raised(grid)
    raised = _analyse(corpus, raised_grid)
    for pair, outcome in zip(grid.pairs, report.outcomes, strict=True):
        raised_outcome = raised.outcome_for(
            ThresholdPair(
                auto_include_threshold=min(UNIT_CEILING, pair.auto_include_threshold + RAISE_BY),
                review_threshold=min(UNIT_CEILING, pair.review_threshold + RAISE_BY),
            )
        )
        if not outcome.applicable or not raised_outcome.applicable:
            continue
        assert (raised_outcome.candidate_count or 0) >= (outcome.candidate_count or 0), (
            "raising both thresholds lowered the candidate count"
        )
        assert (raised_outcome.recovered_count or 0) >= (outcome.recovered_count or 0), (
            "raising both thresholds recovered fewer planted fragments"
        )

    # The no-call clause is not vacuous: the same stubs, walked at a narrow
    # auto-inclusion threshold, do reach the provider for the review band.
    control_walk, control_provider = _walk_over(corpus)
    control = control_walk(
        ResiduePolicy(
            auto_include_threshold=CONTROL_AUTO_THRESHOLD,
            review_threshold=UNIT_CEILING,
            query_limit=len(corpus.queries),
            top_k=TOP_K,
            excerpt_characters=EXCERPT_CHARACTERS,
        )
    )
    referred = sum(
        1 for finding in control.findings if finding.cosine_distance > CONTROL_AUTO_THRESHOLD
    )
    assert len(control_provider.calls) == referred, (
        "the control walk did not reach the provider for its review band, so the "
        "no-call assertion above would hold for the wrong reason"
    )
    assert table_digests(corpus.content) == before
