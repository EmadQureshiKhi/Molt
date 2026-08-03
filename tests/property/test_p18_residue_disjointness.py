"""Property 18: the residue set is disjoint from the explicit set and recovers what it must.

**Validates: Requirements 17.2, 17.3, 17.4, 17.7**

This property runs entirely in this process, and that is the right shape for it
rather than a concession. Every clause of the property is about the phase's own
arithmetic — which band a settled distance falls in, which Artifact a page is
excluded by, which candidates reach a provider and which never may — and none of
it is about how a cluster plans a statement. The two places the phase touches
storage are injected callables, so the walk under test is the walk that ships,
driven by a corpus this module places exactly.

**No provider is called anywhere in the loop.** Vectors are built by blending two
fixed basis directions, so a candidate's cosine distance from a query direction is
a number this module chose rather than one a model produced: for a unit query
direction `q` and a unit direction `w` orthogonal to it, the vector
`(1 - d) q + sqrt(1 - (1 - d)^2) w` stands at cosine distance exactly `d` from `q`.
The stub neighbour search then computes the distance the same way the cluster would,
applies the review ceiling, orders ascending, and truncates to the bound, so the
page the phase sees has the shape the store's own neighbour query returns. The
Adjudicator is a stub that records every batch it is handed, which is how the clause
about calls that must not happen is observable at all.

**The query directions are near-parallel on purpose.** Each example places two or
three query Artifacts a small rotation apart, so a planted fragment is seen by more
than one of them at genuinely different distances. That is what exercises the
smallest-distance rule, and it is what makes the no-call clause a real
constraint rather than a vacuous one: a fragment sighted in the review band of one
query Artifact and below the auto-inclusion threshold of another must not be
adjudicated, and a one-pass walk would have adjudicated it.

**Every threshold comparison in the expectations is computed from the vectors.** The
expected minimum distance per fragment is recomputed here from the placed vectors
rather than read off the report, so an assertion cannot be satisfied by the phase
agreeing with itself.

**The explicit candidate set is planted nearest.** The contaminated corpora put the
already-swept Artifacts at the smallest distances of all, ahead of every fragment, so
disjointness is asserted against rows that would otherwise dominate every page. Both
exclusions are exercised: the anti-join stub answers over the whole set, and the
in-memory set is passed to the walk as well.

Both thresholds are drawn per example and reach the phase as a resolved policy, never
as a literal inside the walk, so an example whose bands sit in different places is
still the same code under test.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid5

from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.erase.residue import (
    AUTO_INCLUDE_REASON,
    AdjudicationBatch,
    Classification,
    QueryArtifact,
    ResidueBand,
    ResidueDetector,
    ResidueFinding,
    ResiduePolicy,
    Verdict,
)
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind
from molt.store.embeddings import Neighbour

# How many examples the property runs, and the bounds of one contaminated corpus.
MAX_EXAMPLES: Final[int] = 100
MIN_QUERIES: Final[int] = 2
MAX_QUERIES: Final[int] = 3
MIN_FRAGMENTS: Final[int] = 4
MAX_FRAGMENTS: Final[int] = 12
MIN_SWEPT: Final[int] = 1
MAX_SWEPT: Final[int] = 3

# The thresholds an example may draw. Every pair is ordered and every pair leaves
# a review band wide enough for a fragment to be placed inside it.
AUTO_THRESHOLDS: Final[tuple[float, ...]] = (0.10, 0.15, 0.20, 0.25)
REVIEW_THRESHOLDS: Final[tuple[float, ...]] = (0.35, 0.45, 0.55)

# How far the query directions are rotated apart. Small enough that every query
# Artifact sees the same neighbourhood, large enough that the distances it sees
# them at differ.
QUERY_SEPARATION: Final[float] = 0.04

# The basis directions a placement blends. The query plane and the offsets are
# fixed indices of the fixed vector width, so no example depends on a drawn seed.
_QUERY_AXIS: Final[int] = 0
_ORTHOGONAL_AXIS: Final[int] = 1
_ROTATION_AXIS: Final[int] = 2

# The neighbour bound and the query-Artifact bound the policy carries. Both are
# above what any example plants, so nothing is lost to a bound and the property is
# about the bands rather than about truncation.
TOP_K: Final[int] = 64
QUERY_LIMIT: Final[int] = MAX_QUERIES
EXCERPT_CHARACTERS: Final[int] = 400

# The namespace identifiers are derived in, so an example's Artifacts are named
# reproducibly and a failure names the same Artifact on a replay.
_NAMESPACE: Final[UUID] = UUID("00000000-0000-0000-0000-000000000018")

# Where a fragment is placed relative to the two thresholds. These are the five
# positions the property is stated over, and every example plants from this ladder
# so both boundaries are straddled rather than approached.
_PLACEMENTS: Final[tuple[str, ...]] = (
    "far_below_auto",
    "at_auto",
    "just_above_auto",
    "just_below_review",
    "beyond_review",
)

# How far a placement sits from the threshold it is stated against. Comfortably
# above floating-point noise at this width, and well inside the narrowest band any
# drawn threshold pair leaves.
_MARGIN: Final[float] = 0.02


# The two kinds that carry embeddable text, sorted so a draw is reproducible.
_EMBEDDABLE: Final[tuple[ArtifactKind, ...]] = (
    ArtifactKind.DERIVED_ARTIFACT,
    ArtifactKind.EVENT,
)


def _identifier(label: str) -> UUID:
    """A reproducible identifier for one placed Artifact."""
    return uuid5(_NAMESPACE, label)


def _unit(components: dict[int, float]) -> tuple[float, ...]:
    """A unit vector of the fixed width from a sparse set of components."""
    scale = math.sqrt(sum(value * value for value in components.values()))
    return tuple(components.get(index, 0.0) / scale for index in range(EMBEDDING_DIMENSION))


def _query_direction(ordinal: int) -> tuple[float, ...]:
    """The direction of one query Artifact, a small rotation from the query axis."""
    return _unit({_QUERY_AXIS: 1.0, _ROTATION_AXIS: QUERY_SEPARATION * ordinal})


def _placed_at(distance: float) -> tuple[float, ...]:
    """A unit vector standing at exactly one cosine distance from the query axis.

    The cosine of the angle is one minus the distance, so the component along the
    query axis is that cosine and the component along an orthogonal axis is what
    completes the unit length.
    """
    cosine = 1.0 - distance
    completing = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return _unit({_QUERY_AXIS: cosine, _ORTHOGONAL_AXIS: completing})


def _cosine_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """The cosine distance between two unit vectors, computed here rather than read."""
    return 1.0 - sum(a * b for a, b in zip(left, right, strict=True))


# ---------------------------------------------------------------------------
# What one contaminated corpus is
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlantedArtifact:
    """One Artifact of a contaminated corpus, and where it was placed."""

    artifact_id: UUID
    artifact_kind: ArtifactKind
    vector: tuple[float, ...]
    placement: str
    swept: bool


@dataclass(frozen=True, slots=True)
class ContaminatedCorpus:
    """Query Artifacts, planted fragments, an already-swept set, and the thresholds."""

    policy: ResiduePolicy
    queries: tuple[QueryArtifact, ...]
    fragments: tuple[PlantedArtifact, ...]
    swept: tuple[PlantedArtifact, ...]

    @property
    def swept_ids(self) -> frozenset[UUID]:
        """The explicit candidate set, which the residue set must be disjoint from."""
        return frozenset(planted.artifact_id for planted in self.swept)

    @property
    def corpus(self) -> tuple[PlantedArtifact, ...]:
        """Every Artifact the neighbour query may return, swept ones included."""
        return self.fragments + self.swept

    def nearest_distance(self, planted: PlantedArtifact) -> float:
        """The smallest distance any query Artifact stands from one Artifact."""
        return min(_cosine_distance(planted.vector, query.vector) for query in self.queries)

    def within(self, planted: PlantedArtifact, ceiling: float) -> bool:
        """Whether any query Artifact sees this Artifact inside a ceiling."""
        return self.nearest_distance(planted) <= ceiling


def _distance_for(placement: str, policy: ResiduePolicy) -> float:
    """The distance one placement stands at, stated against the drawn thresholds."""
    if placement == "far_below_auto":
        return max(0.0, policy.auto_include_threshold - 2.0 * _MARGIN)
    if placement == "at_auto":
        return policy.auto_include_threshold
    if placement == "just_above_auto":
        return policy.auto_include_threshold + _MARGIN
    if placement == "just_below_review":
        return policy.review_threshold - _MARGIN
    return policy.review_threshold + 2.0 * _MARGIN


@st.composite
def contaminated_corpora(draw: st.DrawFn) -> ContaminatedCorpus:
    """Draw a corpus of stub vectors placed at distances straddling both thresholds.

    The thresholds come first, because every placement is stated relative to them.
    The query Artifacts follow, a small rotation apart so a fragment is sighted at
    several distances. The fragments are drawn from the placement ladder, which is
    what puts Artifacts on both sides of both thresholds and on the auto-inclusion
    boundary itself. The already-swept Artifacts are placed nearest of all, so a
    page that failed to exclude them would be dominated by them.
    """
    policy = ResiduePolicy(
        auto_include_threshold=draw(st.sampled_from(AUTO_THRESHOLDS)),
        review_threshold=draw(st.sampled_from(REVIEW_THRESHOLDS)),
        query_limit=QUERY_LIMIT,
        top_k=TOP_K,
        excerpt_characters=EXCERPT_CHARACTERS,
    )
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

    placements = draw(
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
            vector=_placed_at(_distance_for(placement, policy)),
            placement=placement,
            swept=False,
        )
        for ordinal, placement in enumerate(placements)
    )

    swept_count = draw(st.integers(min_value=MIN_SWEPT, max_value=MAX_SWEPT))
    swept = tuple(
        PlantedArtifact(
            artifact_id=_identifier(f"swept-{ordinal}"),
            artifact_kind=draw(st.sampled_from(sorted(_EMBEDDABLE))),
            vector=_placed_at(_MARGIN * (ordinal + 1) / 10.0),
            placement="swept",
            swept=True,
        )
        for ordinal in range(swept_count)
    )
    return ContaminatedCorpus(policy=policy, queries=queries, fragments=fragments, swept=swept)


# ---------------------------------------------------------------------------
# The seams the walk is driven through
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubNeighbours:
    """The neighbour query, answered from placed vectors with no cluster and no model.

    The ceiling, the ordering, and the bound are applied here exactly as the store's
    own statement applies them, so the page the walk receives has the shape it has in
    production.
    """

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
                client_id=self.client_id,
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
    asked: int = 0

    def __call__(self, ids: Sequence[UUID]) -> frozenset[UUID]:
        self.asked += 1
        return frozenset(identifier for identifier in ids if identifier not in self.swept)


@dataclass(slots=True)
class StubAdjudicator:
    """An Adjudicator that records every batch and every candidate it was handed.

    It answers deterministically by identifier parity rather than by distance, so an
    exclusion is not a restatement of the band and the recorded reason is the
    verdict's rather than the phase's.
    """

    batches: list[AdjudicationBatch] = field(default_factory=list)

    @property
    def seen(self) -> frozenset[UUID]:
        """Every candidate any batch carried."""
        return frozenset(row.artifact_id for batch in self.batches for row in batch.candidates)

    def adjudicate(self, batch: AdjudicationBatch) -> tuple[Verdict, ...]:
        self.batches.append(batch)
        verdicts: list[Verdict] = []
        for row in batch.candidates:
            include = row.artifact_id.int % 2 == 0
            label = Classification.INCLUDE if include else Classification.EXCLUDE
            verdicts.append(
                Verdict(
                    artifact_id=row.artifact_id,
                    classification=label,
                    included=include,
                    decision_reason=f"adjudicated_{label.value}",
                    model_id="stub-model",
                    prompt_digest="0" * 64,
                    reasoning="the stub judged this candidate",
                )
            )
        return tuple(verdicts)


def _run(corpus: ContaminatedCorpus) -> tuple[ResidueDetector, StubAdjudicator, StubNeighbours]:
    """Build the walk over one corpus and hold on to the stubs it was driven by."""
    neighbours = StubNeighbours(corpus=corpus.corpus, client_id=_identifier("client"))
    adjudicator = StubAdjudicator()
    detector = ResidueDetector(
        corpus.policy,
        neighbours=neighbours,
        uncandidated_ids=StubAntiJoin(swept=corpus.swept_ids),
        adjudicator=adjudicator,
    )
    return detector, adjudicator, neighbours


def _by_id(findings: tuple[ResidueFinding, ...]) -> dict[UUID, ResidueFinding]:
    """The findings keyed by Artifact, which is what the property asserts per Artifact."""
    return {finding.artifact_id: finding for finding in findings}


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 18: For any corpus with explicit Client_Bindings and planted
# unlabelled fragments, the residue candidate set and the explicit sweep candidate set
# are disjoint, their union contains every planted fragment whose cosine distance is at
# most the auto-inclusion threshold, every candidate at or below the auto-inclusion
# threshold is included without an Adjudicator call, and every recorded candidate
# carries a distance, a band, and a decision reason.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(corpus=contaminated_corpora())
def test_the_residue_set_is_disjoint_and_recovers_what_it_must(
    corpus: ContaminatedCorpus,
) -> None:
    policy = corpus.policy
    detector, adjudicator, neighbours = _run(corpus)

    event(f"queries={len(corpus.queries)}")
    event(f"fragments={len(corpus.fragments)}")
    event(f"auto threshold={policy.auto_include_threshold}")
    event(f"review threshold={policy.review_threshold}")
    for planted in corpus.fragments:
        event(f"placement={planted.placement}")

    report = detector.detect(corpus.queries, candidate_ids=corpus.swept_ids)
    findings = _by_id(report.findings)

    assert neighbours.pages == len(corpus.queries), (
        "the walk asked one page per query artifact, so a differing count means a "
        "query artifact was skipped or asked twice"
    )

    # Requirement 17.3: the two candidate sets are disjoint, and the swept Artifacts
    # were the nearest content in the corpus, so nothing excluded them by accident.
    assert not report.candidate_ids & corpus.swept_ids, (
        "the residue set holds an artifact the explicit sweep already selected"
    )

    # Requirement 17.2 and 17.7: every fragment inside the auto-inclusion threshold is
    # recovered, is banded auto-inclusion, and is included with the threshold reason.
    for planted in corpus.fragments:
        nearest = corpus.nearest_distance(planted)
        if nearest > policy.auto_include_threshold:
            continue
        finding = findings.get(planted.artifact_id)
        assert finding is not None, (
            f"a fragment at distance {nearest} was not recovered although the "
            f"auto-inclusion threshold is {policy.auto_include_threshold}"
        )
        assert finding.band is ResidueBand.AUTO_INCLUDE, (
            f"a fragment at distance {nearest} was banded {finding.band.value}"
        )
        assert finding.included, "a fragment inside the auto-inclusion threshold was excluded"
        assert finding.decision_reason == AUTO_INCLUDE_REASON, (
            "an auto-included fragment carries a reason other than the threshold's"
        )
        assert not finding.adjudicated, "an auto-included fragment was marked adjudicated"

    # Requirement 17.4: nothing at or below the auto-inclusion threshold reached the
    # Adjudicator, whichever query artifact first sighted it further away.
    for artifact_id in adjudicator.seen:
        finding = findings[artifact_id]
        assert finding.cosine_distance > policy.auto_include_threshold, (
            f"an artifact at distance {finding.cosine_distance} was adjudicated although "
            f"the auto-inclusion threshold is {policy.auto_include_threshold}"
        )
    assert adjudicator.seen == {
        finding.artifact_id for finding in report.findings if finding.band is ResidueBand.REVIEW
    }, "the review band and the set of adjudicated artifacts disagree"

    # Every call sharing a Stable_Prefix was issued together: one batch per query
    # Artifact at most, and no Artifact carried in two batches.
    assert len(adjudicator.batches) == report.adjudication_batches
    dispatched = [row.artifact_id for batch in adjudicator.batches for row in batch.candidates]
    assert len(dispatched) == len(set(dispatched)), "a candidate was dispatched twice"
    assert len({batch.query_artifact.artifact_id for batch in adjudicator.batches}) == len(
        adjudicator.batches
    ), "two batches carried the same query artifact rather than being issued together"

    # Requirement 17.7: every recorded candidate carries a distance, a band, a
    # threshold comparison, an inclusion, and a decision reason.
    for finding in report.findings:
        assert math.isfinite(finding.cosine_distance)
        assert 0.0 <= finding.cosine_distance <= policy.review_threshold, (
            "a candidate was recorded outside the ceiling the page was read at"
        )
        assert finding.band in (ResidueBand.AUTO_INCLUDE, ResidueBand.REVIEW)
        assert finding.decision_reason, "a candidate was recorded with no decision reason"
        assert finding.within_review is True
        assert finding.within_auto_include is (finding.band is ResidueBand.AUTO_INCLUDE)
        assert isinstance(finding.included, bool)
        assert finding.query_artifact_id in {query.artifact_id for query in corpus.queries}

    # Nothing beyond the review threshold was recorded at all, which is the ceiling
    # the page was read at rather than a filter applied afterwards.
    for planted in corpus.fragments:
        if not corpus.within(planted, policy.review_threshold):
            assert planted.artifact_id not in findings, (
                "an artifact beyond the review threshold was recorded as a candidate"
            )

    # The distance recorded is the smallest any query artifact sighted, not the first.
    for planted in corpus.fragments:
        finding = findings.get(planted.artifact_id)
        if finding is None:
            continue
        assert finding.cosine_distance == min(
            _cosine_distance(planted.vector, query.vector) for query in corpus.queries
        ), "a candidate was recorded at a distance some query artifact bettered"
