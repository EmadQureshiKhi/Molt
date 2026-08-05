"""The Sensitivity_Analyzer: what each threshold pair would have swept.

The threshold pair is the one tuning decision that changes what an erasure
covers, and it is chosen once and then relied on by every certificate afterwards.
This module exists so the choice is made against a measured consequence rather
than against the plausibility of a default. Five claims carry it, and each is
arranged so a caller cannot lose it by forgetting something.

**One search, then counting.** The residue walk runs once, at the *widest* review
threshold the grid names, and every candidate it sights is retained with its
cosine distance. Every grid pair is then answered by counting against that one
retained set. Re-searching per pair would multiply the cluster's work by the size
of the grid for answers that are identical, which is why the retained set is the
subject of this module and the walk is a seam it drives once.

**Nothing is adjudicated, so no candidate costs a model call.** The walk is asked
for a policy whose auto-inclusion threshold equals its review threshold, so every
sighting falls in the auto-inclusion band and the review branch of the walk is
never entered. The adjudication-referred figure is then arithmetic over the
retained distances and a pair's two band boundaries: a count of candidates a pair
*would* refer, computed without referring one. The report is refused outright if
the walk comes back having dispatched a batch, so the promise is checked rather
than asserted.

**Purity is a privilege fact.** No statement here writes, no write transaction is
opened, and the store-backed walk refuses a store whose connection does not
authenticate as the read-only role. The walk is the residue module's own
read-only exposure with recording suppressed, so a reported band and a recorded
band cannot come to mean different things, and a report that arrived from a
recording pass is refused.

**An inapplicable pair is reported rather than skipped.** A pair whose
auto-inclusion threshold exceeds its review threshold describes no band, and it
carries the reason instead of counts. Skipping it would leave a hole where the
console renders a cell, so the grid stays rectangular and a reader sees which
cells are meaningless instead of finding blanks.

**Calibration, not decision replay.** The report says what each pair would select
from the corpus as it stands. It replays no recorded adjudication and re-asks
nothing, because a past run's reasoning is evidence about a corpus that erasure
has since changed rather than a function that can be re-evaluated.

Both grid axes, all three search bounds, and the ground-truth path are read
through the configuration surface rather than restated here, so an operator's
override reaches the analysis in one place.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from molt.config.resolve import ConfigError, Configuration, load_configuration
from molt.erase.residue import (
    EXCERPT_BUDGET_KEY,
    QUERY_LIMIT_KEY,
    TOP_K_KEY,
    ResiduePolicy,
    ResidueReport,
    residue_report,
)
from molt.errors import StoreError
from molt.models.artifact import ArtifactKind
from molt.store import READER_ROLE_NAMES as STORE_READER_ROLE_NAMES
from molt.store import MemoryStore
from molt.store.embeddings import COSINE_CEILING, COSINE_FLOOR
from molt.telemetry import Severity, log, metric

__all__ = [
    "AUTO_THRESHOLDS_KEY",
    "COMPONENT",
    "GROUND_TRUTH_KEY",
    "INAPPLICABLE_REASON",
    "READ_ONLY_ROLE",
    "REVIEW_THRESHOLDS_KEY",
    "SENSITIVITY_CANDIDATES_METRIC",
    "SENSITIVITY_PAIRS_METRIC",
    "GroundTruth",
    "PairOutcome",
    "ResidueWalk",
    "RetainedCandidate",
    "SearchBounds",
    "SensitivityReport",
    "ThresholdGrid",
    "ThresholdPair",
    "analyse",
    "analyse_client",
    "configured_ground_truth",
    "default_grid",
    "load_ground_truth",
    "retained_from",
    "store_residue_walk",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "erase"

# The configuration keys the two grid axes and the ground-truth mapping resolve
# from. The five auto-inclusion values and the five review values live in the
# surface's defaults, so the 25-pair default grid is one crossing of them here
# rather than a second spelling of ten numbers.
AUTO_THRESHOLDS_KEY: Final[str] = "MOLT_SENSITIVITY_AUTO_THRESHOLDS"
REVIEW_THRESHOLDS_KEY: Final[str] = "MOLT_SENSITIVITY_REVIEW_THRESHOLDS"
GROUND_TRUTH_KEY: Final[str] = "MOLT_SENSITIVITY_GROUND_TRUTH"

# The role a store's connection must authenticate as for the analysis to run. The
# grants the migrations apply give it `SELECT` and nothing else, which is what makes
# the no-mutation claim a privilege fact rather than a discipline.
#
# Both spellings are admitted, and the set is the verifier's own rather than a second
# one stated here. The migrations create the role as `molt_reader` while a
# configuration and a test both commonly name it `reader`, so a check against one
# spelling alone refuses the very deployment the grants were written for. That was a
# live defect: the analysis was unreachable in any provisioned deployment.
READER_ROLE_NAMES: Final[frozenset[str]] = STORE_READER_ROLE_NAMES

# The short spelling, kept for the label a page renders and a test names.
READ_ONLY_ROLE: Final[str] = "reader"

# The reason an inapplicable pair carries. A pair whose auto-inclusion threshold
# exceeds its review threshold describes no band: everything the pair would sight
# is already inside the threshold that decides inclusion outright.
INAPPLICABLE_REASON: Final[str] = "auto_include_threshold_above_review_threshold"

# The measurements the analysis emits: how many pairs were answered and how large
# the one retained set they were all answered from was.
SENSITIVITY_PAIRS_METRIC: Final[str] = "erasure.sensitivity_pairs"
SENSITIVITY_CANDIDATES_METRIC: Final[str] = "erasure.sensitivity_candidates"

# The fields the ground-truth mapping is read through. Both accepted shapes are
# named here rather than guessed at each use: an object carrying an array of
# planted fragments, or a flat object of fragment identifier to owning Client.
FRAGMENTS_FIELD: Final[str] = "planted_fragments"
FRAGMENT_FIELD: Final[str] = "artifact_id"
OWNER_FIELD: Final[str] = "owner_client_id"


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThresholdPair:
    """One cell of the grid: an auto-inclusion threshold and a review threshold.

    Both values are cosine distances, and neither is checked against the other
    here. A pair whose auto-inclusion threshold exceeds its review threshold is a
    pair an operator may legitimately ask about, and the answer is that it is
    inapplicable, which is a reported outcome rather than a rejected input.
    """

    auto_include_threshold: float
    review_threshold: float

    def __post_init__(self) -> None:
        for name, value in (
            ("an auto-inclusion threshold", self.auto_include_threshold),
            ("a review threshold", self.review_threshold),
        ):
            if not math.isfinite(value) or not COSINE_FLOOR <= value <= COSINE_CEILING:
                raise ValueError(f"{name} must be a cosine distance in range")

    @property
    def applicable(self) -> bool:
        """Whether this pair leaves a band the residue phase could reason about."""
        return self.auto_include_threshold <= self.review_threshold


@dataclass(frozen=True, slots=True)
class ThresholdGrid:
    """Every pair one analysis answers, in the order the console renders them.

    The order is the grid's own: auto-inclusion values lead and review values
    follow, so the sequence of outcomes reads row by row down the rendered table
    without the view having to sort anything.
    """

    pairs: tuple[ThresholdPair, ...]

    def __post_init__(self) -> None:
        if not self.pairs:
            raise ValueError("a threshold grid must name at least one pair")

    @classmethod
    def from_axes(
        cls,
        auto_include_thresholds: Sequence[float],
        review_thresholds: Sequence[float],
    ) -> ThresholdGrid:
        """Cross the two axes into one rectangular grid, rows before columns."""
        if not auto_include_thresholds or not review_thresholds:
            raise ValueError("a threshold grid needs a value on each axis")
        return cls(
            pairs=tuple(
                ThresholdPair(auto_include_threshold=auto, review_threshold=review)
                for auto in auto_include_thresholds
                for review in review_thresholds
            )
        )

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> ThresholdGrid:
        """Cross the two configured axes, which is where the default grid comes from."""
        return cls.from_axes(
            auto_include_thresholds=configuration.number_list(AUTO_THRESHOLDS_KEY),
            review_thresholds=configuration.number_list(REVIEW_THRESHOLDS_KEY),
        )

    @property
    def widest_review_threshold(self) -> float:
        """The ceiling the one retained search is run at.

        The widest review threshold of any pair, applicable or not, so no pair can
        ask about a candidate the single search did not sight.
        """
        return max(pair.review_threshold for pair in self.pairs)

    @property
    def auto_include_axis(self) -> tuple[float, ...]:
        """The distinct auto-inclusion values, ascending: the rendered rows."""
        return tuple(sorted({pair.auto_include_threshold for pair in self.pairs}))

    @property
    def review_axis(self) -> tuple[float, ...]:
        """The distinct review values, ascending: the rendered columns."""
        return tuple(sorted({pair.review_threshold for pair in self.pairs}))


def default_grid(configuration: Configuration | None = None) -> ThresholdGrid:
    """The configured grid, which defaults to the 25-pair crossing of the two axes.

    The five auto-inclusion values and the five review values are the surface's
    defaults for the two axis keys, so this is a crossing of configured numbers
    rather than a second place ten numbers are written down.
    """
    resolved = load_configuration() if configuration is None else configuration
    return ThresholdGrid.from_configuration(resolved)


@dataclass(frozen=True, slots=True)
class SearchBounds:
    """The three bounds the one retained search runs under.

    Held apart from the thresholds because the thresholds are what the grid
    varies and these are what it holds fixed: the same bounds answer every pair,
    and they are the residue phase's own configured bounds rather than a second
    set this module invents.
    """

    query_limit: int
    top_k: int
    excerpt_characters: int

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> SearchBounds:
        """Resolve the three bounds through the configuration surface."""
        return cls(
            query_limit=configuration.integer(QUERY_LIMIT_KEY),
            top_k=configuration.integer(TOP_K_KEY),
            excerpt_characters=configuration.integer(EXCERPT_BUDGET_KEY),
        )

    def policy_at(self, ceiling: float) -> ResiduePolicy:
        """The policy the one search runs under, at a ceiling and with no band.

        Both thresholds are the ceiling, so every sighting falls in the
        auto-inclusion band and the walk's review branch is unreachable. That is
        what makes the no-provider-call promise structural: the analysis cannot
        reach an Adjudicator through a policy of this shape.
        """
        return ResiduePolicy(
            auto_include_threshold=ceiling,
            review_threshold=ceiling,
            query_limit=self.query_limit,
            top_k=self.top_k,
            excerpt_characters=self.excerpt_characters,
        )


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """The planted cross-client fragments and the Client each one belongs to.

    The Seed_Generator records it in a file separate from the seeded content,
    which is why it is read here as a path rather than as a query: the mapping is
    knowledge about the corpus that the corpus deliberately does not carry.
    """

    planted: tuple[tuple[UUID, UUID], ...]

    @property
    def fragment_ids(self) -> frozenset[UUID]:
        """Every planted fragment, which is what a recovered count is drawn from."""
        return frozenset(fragment for fragment, _ in self.planted)

    @classmethod
    def from_mapping(cls, owners: Mapping[UUID, UUID]) -> GroundTruth:
        """Build the mapping from fragment identifiers to owning Client identifiers."""
        return cls(planted=tuple(sorted(owners.items(), key=lambda pair: str(pair[0]))))


def load_ground_truth(path: Path) -> GroundTruth:
    """Read the ground-truth mapping from its file.

    Two shapes are accepted, because the mapping is a small file an operator may
    also hand-write: an object carrying an array of planted fragments, each naming
    a fragment and its owning Client, or a flat object of fragment identifier to
    owning Client identifier. Anything else is refused with the path named, rather
    than reported as an absent mapping, because a malformed file an operator meant
    to supply must not silently become a report with no recovered column.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"the ground-truth mapping at {path} could not be read") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"the ground-truth mapping at {path} is not valid JSON") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"the ground-truth mapping at {path} is not an object")
    listed = raw.get(FRAGMENTS_FIELD)
    if listed is None:
        return GroundTruth.from_mapping(_flat_owners(raw, path))
    if not isinstance(listed, list):
        raise ConfigError(f"the ground-truth mapping at {path} lists no planted fragments")
    return GroundTruth.from_mapping(_listed_owners(listed, path))


def configured_ground_truth(configuration: Configuration) -> GroundTruth | None:
    """The configured mapping where one is available, and nothing where it is not.

    An unset path and a path naming no file are both the absent case, because the
    recovered-fragment column is reported *where* ground truth is available and a
    deployment holding no seeded corpus has none.
    """
    path = configuration.optional_path(GROUND_TRUTH_KEY)
    if path is None or not path.is_file():
        return None
    return load_ground_truth(path)


def _flat_owners(raw: Mapping[str, object], path: Path) -> dict[UUID, UUID]:
    """Decode the flat shape: fragment identifier to owning Client identifier."""
    owners: dict[UUID, UUID] = {}
    for fragment, owner in raw.items():
        owners[_identifier(fragment, path)] = _identifier(owner, path)
    return owners


def _listed_owners(listed: Sequence[object], path: Path) -> dict[UUID, UUID]:
    """Decode the array shape, where each entry names a fragment and its Client."""
    owners: dict[UUID, UUID] = {}
    for entry in listed:
        if not isinstance(entry, dict):
            raise ConfigError(f"the ground-truth mapping at {path} holds a malformed entry")
        owners[_identifier(entry.get(FRAGMENT_FIELD), path)] = _identifier(
            entry.get(OWNER_FIELD), path
        )
    return owners


def _identifier(value: object, path: Path) -> UUID:
    """One identifier of the mapping, refused rather than coerced when unusable."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ConfigError(
                f"the ground-truth mapping at {path} names something other than an identifier"
            ) from error
    raise ConfigError(
        f"the ground-truth mapping at {path} names something other than an identifier"
    )


# ---------------------------------------------------------------------------
# What one analysis reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetainedCandidate:
    """One candidate the single search sighted, kept with the distance it sighted at.

    This is the whole subject of the module: every pair of the grid is answered by
    counting these against two boundaries, so the distance is retained rather than
    the band it fell in under the search's own ceiling.
    """

    artifact_id: UUID
    artifact_kind: ArtifactKind
    query_artifact_id: UUID
    cosine_distance: float


@dataclass(frozen=True, slots=True)
class PairOutcome:
    """What one pair would have selected, or why it could not have selected anything.

    An inapplicable pair carries the reason and no counts at all. That is the
    observable difference between a pair that was answered and a pair that was
    reported: the counts are absent rather than zero, so a reader cannot mistake
    a meaningless cell for an empty one.
    """

    auto_include_threshold: float
    review_threshold: float
    candidate_count: int | None
    auto_included_count: int | None
    referred_count: int | None
    recovered_count: int | None = None
    inapplicable_reason: str | None = None

    def __post_init__(self) -> None:
        candidates = self.candidate_count
        included = self.auto_included_count
        referred = self.referred_count
        if self.inapplicable_reason is not None:
            if candidates is not None or included is not None or referred is not None:
                raise ValueError("an inapplicable pair carries no counts")
            if self.recovered_count is not None:
                raise ValueError("an inapplicable pair recovers no fragment")
            return
        if candidates is None or included is None or referred is None:
            raise ValueError("an applicable pair carries every count")
        if included + referred != candidates:
            raise ValueError("the two bands of a pair must partition its candidates")

    @property
    def applicable(self) -> bool:
        """Whether this pair was answered rather than reported as inapplicable."""
        return self.inapplicable_reason is None

    @property
    def pair(self) -> ThresholdPair:
        """The pair this outcome answers, for a caller keying a rendered grid."""
        return ThresholdPair(
            auto_include_threshold=self.auto_include_threshold,
            review_threshold=self.review_threshold,
        )


@dataclass(frozen=True, slots=True)
class SensitivityReport:
    """Every pair's outcome, and the one retained set they were all answered from.

    The retained set travels with the outcomes because it is the evidence behind
    every count: a reader can recompute any cell from the distances rather than
    taking the arithmetic on trust.
    """

    grid: ThresholdGrid
    outcomes: tuple[PairOutcome, ...]
    retained: tuple[RetainedCandidate, ...]
    searched_at: float
    query_artifact_ids: tuple[UUID, ...]
    ground_truth_available: bool

    @property
    def searches(self) -> int:
        """How many residue searches the analysis cost: one per query Artifact."""
        return len(self.query_artifact_ids)

    @property
    def applicable_outcomes(self) -> tuple[PairOutcome, ...]:
        """The pairs that were answered, in grid order."""
        return tuple(outcome for outcome in self.outcomes if outcome.applicable)

    @property
    def inapplicable_outcomes(self) -> tuple[PairOutcome, ...]:
        """The pairs that were reported as inapplicable, in grid order."""
        return tuple(outcome for outcome in self.outcomes if not outcome.applicable)

    def outcome_for(self, pair: ThresholdPair) -> PairOutcome:
        """The outcome of one pair, which is what a rendered cell reads."""
        for outcome in self.outcomes:
            if outcome.pair == pair:
                return outcome
        raise KeyError("the grid holds no such threshold pair")


# ---------------------------------------------------------------------------
# Counting against the retained set
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tally:
    """The retained distances, ascending, with the planted count running beside them.

    Held sorted with a prefix count so a pair costs two searches over a sorted
    sequence rather than a walk of every candidate. Twenty-five pairs over a
    retained set of thousands is then arithmetic, which is the point of retaining
    the set at all.
    """

    distances: tuple[float, ...]
    planted_prefix: tuple[int, ...]
    ground_truth_available: bool

    @classmethod
    def over(
        cls,
        retained: Sequence[RetainedCandidate],
        ground_truth: GroundTruth | None,
    ) -> _Tally:
        """Sort the retained distances once and run the planted count along them."""
        planted = frozenset() if ground_truth is None else ground_truth.fragment_ids
        ordered = sorted(retained, key=lambda row: (row.cosine_distance, str(row.artifact_id)))
        prefix: list[int] = [0]
        for row in ordered:
            prefix.append(prefix[-1] + (1 if row.artifact_id in planted else 0))
        return cls(
            distances=tuple(row.cosine_distance for row in ordered),
            planted_prefix=tuple(prefix),
            ground_truth_available=ground_truth is not None,
        )

    def within(self, ceiling: float) -> int:
        """How many retained candidates stand at or inside one ceiling."""
        return bisect_right(self.distances, ceiling)

    def planted_within(self, ceiling: float) -> int | None:
        """How many planted fragments stand inside one ceiling, where truth is known."""
        if not self.ground_truth_available:
            return None
        return self.planted_prefix[self.within(ceiling)]

    def outcome_for(self, pair: ThresholdPair) -> PairOutcome:
        """Answer one pair by counting, or report it as inapplicable.

        The referred count is the band between the two thresholds and is computed
        from the distances alone. Nothing is dispatched to reach it, which is what
        makes a 25-pair grid cost no model spend.
        """
        if not pair.applicable:
            return PairOutcome(
                auto_include_threshold=pair.auto_include_threshold,
                review_threshold=pair.review_threshold,
                candidate_count=None,
                auto_included_count=None,
                referred_count=None,
                inapplicable_reason=INAPPLICABLE_REASON,
            )
        candidates = self.within(pair.review_threshold)
        auto_included = self.within(pair.auto_include_threshold)
        return PairOutcome(
            auto_include_threshold=pair.auto_include_threshold,
            review_threshold=pair.review_threshold,
            candidate_count=candidates,
            auto_included_count=auto_included,
            referred_count=candidates - auto_included,
            recovered_count=self.planted_within(pair.review_threshold),
        )


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------

# The one seam the analysis reaches the corpus through: a residue walk asked for
# one policy and answering with one report. A callable rather than a store handle,
# so the counting is drivable by a stub and the store-backed form below is the
# same analysis.
ResidueWalk = Callable[[ResiduePolicy], ResidueReport]


def retained_from(report: ResidueReport) -> tuple[RetainedCandidate, ...]:
    """Every candidate one walk sighted, with the distance it was sighted at.

    The walk's own report is the source rather than a second search, and the two
    refusals here are the analysis's purity checks: a report from a recording pass
    means rows were written, and a report carrying an adjudication batch means a
    provider was called for a candidate. Both are conditions this module promises
    cannot happen, so both are refused instead of being reported.
    """
    if not report.read_only:
        raise StoreError("the sensitivity analysis received a residue pass that recorded findings")
    if report.adjudication_batches:
        raise StoreError("the sensitivity analysis received a residue pass that adjudicated")
    return tuple(
        RetainedCandidate(
            artifact_id=finding.artifact_id,
            artifact_kind=finding.artifact_kind,
            query_artifact_id=finding.query_artifact_id,
            cosine_distance=finding.cosine_distance,
        )
        for finding in report.findings
    )


def analyse(
    grid: ThresholdGrid,
    *,
    walk: ResidueWalk,
    bounds: SearchBounds,
    ground_truth: GroundTruth | None = None,
) -> SensitivityReport:
    """Answer every pair of the grid from one residue search.

    Args:
        grid: The pairs to answer, which need not all be applicable.
        walk: The residue walk, asked once for the widest ceiling the grid names.
        bounds: The query, neighbour, and excerpt bounds the search runs under.
        ground_truth: The planted-fragment mapping where one is available, which
            is what adds the recovered column to every applicable pair.

    Returns:
        One outcome per pair, in grid order, and the retained set behind them.
    """
    ceiling = grid.widest_review_threshold
    report = walk(bounds.policy_at(ceiling))
    retained = retained_from(report)
    tally = _Tally.over(retained, ground_truth)
    outcomes = tuple(tally.outcome_for(pair) for pair in grid.pairs)

    metric(SENSITIVITY_PAIRS_METRIC, float(len(outcomes)))
    metric(SENSITIVITY_CANDIDATES_METRIC, float(len(retained)))
    log(
        Severity.INFO,
        COMPONENT,
        "threshold sensitivity analysis completed",
        pairs=len(outcomes),
        inapplicable=sum(1 for outcome in outcomes if not outcome.applicable),
        candidates=len(retained),
        searched_at=ceiling,
        query_artifacts=len(report.query_artifact_ids),
        ground_truth=ground_truth is not None,
    )
    return SensitivityReport(
        grid=grid,
        outcomes=outcomes,
        retained=retained,
        searched_at=ceiling,
        query_artifact_ids=report.query_artifact_ids,
        ground_truth_available=ground_truth is not None,
    )


def store_residue_walk(
    store: MemoryStore,
    run_id: UUID,
    *,
    permitted_clients: Sequence[UUID],
) -> ResidueWalk:
    """The store-backed walk: the residue module's read-only exposure, no Adjudicator.

    The role is checked before a statement is sent rather than after, because the
    point of the check is that the connection cannot write: a store authenticating
    as anything but the read-only role is refused here instead of being trusted to
    behave. No Adjudicator is passed, so no candidate can reach the Text_Provider
    even if a policy of another shape were somehow handed to the walk.
    """
    if store.role not in READER_ROLE_NAMES:
        raise StoreError(
            f"the sensitivity analysis runs as the read-only role, named as one of "
            f"{', '.join(sorted(READER_ROLE_NAMES))}, and this connection "
            f"authenticates as {store.role or 'an unnamed role'}"
        )

    def walk(policy: ResiduePolicy) -> ResidueReport:
        return residue_report(
            store,
            run_id,
            policy,
            permitted_clients=permitted_clients,
            adjudicator=None,
        )

    return walk


def analyse_client(
    store: MemoryStore,
    run_id: UUID,
    *,
    permitted_clients: Sequence[UUID],
    configuration: Configuration,
    grid: ThresholdGrid | None = None,
    ground_truth: GroundTruth | None = None,
) -> SensitivityReport:
    """The wired-up analysis: the configured grid, bounds, and mapping over a cluster.

    Args:
        store: The cluster the corpus lives in, connected as the read-only role.
        run_id: The candidate-set row the query Artifacts are drawn from, which is
            a synthetic row on this path because nothing is being erased.
        permitted_clients: The Clients the neighbour query may answer for, which
            is the whole fleet, because residue is content the erased Client's own
            labels do not name.
        configuration: The surface both grid axes, the three bounds, and the
            ground-truth path are read through.
        grid: A grid to answer instead of the configured default.
        ground_truth: A mapping to use instead of the configured one.
    """
    resolved_grid = default_grid(configuration) if grid is None else grid
    resolved_truth = (
        configured_ground_truth(configuration) if ground_truth is None else ground_truth
    )
    return analyse(
        resolved_grid,
        walk=store_residue_walk(store, run_id, permitted_clients=permitted_clients),
        bounds=SearchBounds.from_configuration(configuration),
        ground_truth=resolved_truth,
    )
