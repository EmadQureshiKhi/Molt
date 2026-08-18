"""Phase two of erasure: the Residue_Detector's semantic pass.

Phase one selects what a Client's own labels name. This phase looks for the
content those labels missed, by asking the corpus which Artifacts sit close to
the ones already selected. Six claims carry the module, and each is arranged so a
caller cannot lose it by forgetting something.

**The query Artifacts are the longest text in the candidate set, by kind.** A
short Artifact ranks its neighbours by too little evidence, so the selection is
by descending text length within kind up to the configured limit. Kind leads the
order so the bound is spent across both text-bearing kinds rather than filled
entirely by whichever kind happens to hold the longest bodies.

**The neighbour question is the store's own, asked at the review threshold.** The
ceiling is the review threshold, so nothing beyond the band this phase reasons
about ever reaches this process, and the ordering is the one the delivered vector
index serves. The query is the store's neighbour query rather than a second
statement that would have to be kept in step with it.

**The candidate set is excluded in SQL as well as in the loop.** The anti-join
statement below asks the cluster which of a page of neighbours the candidate set
does not already hold, and the loop then re-checks membership against the set it
carries. Neither check is redundant: the SQL term is what makes disjointness hold
when the in-memory set is incomplete, and the loop term is what makes it hold
when a candidate landed after the page was read.

**A candidate reached from several query Artifacts keeps its smallest distance.**
Findings are keyed by Artifact within a run, the conflict update keeps the lesser
distance and carries the nearer row's band and reason with it, and inclusion is
disjunctive. Replaying the phase therefore converges on the same rows rather than
on whichever query Artifact was walked last.

**Nothing below the auto-inclusion threshold is ever adjudicated, and grouping
happens before dispatch.** The walk is two passes for exactly that reason. The
first pass reads every page and settles the smallest distance per Artifact; the
second pass bands the settled distances and dispatches the review band grouped by
query Artifact, one batch per group. A one-pass walk would have adjudicated a
candidate in the review band of one query Artifact that a later query Artifact
placed under the auto-inclusion threshold, which is a provider call this phase
promises not to make. Grouping by query Artifact is what lets the Adjudicator
reuse one Stable_Prefix across a batch instead of interleaving prefixes.

**The Adjudicator is a seam, and an absent one fails closed.** It arrives as an
injected callable defaulting to None rather than as an import, so this phase is
drivable by a stub and the two modules stay independently testable. A review-band
candidate with no Adjudicator present is included, with the fail-closed reason and
the adjudicated flag false, because an under-inclusive erasure breaks the
contractual claim while an over-inclusive one costs memory utility.

**The read-only mode mutates nothing.** The CLI residue verb, the
Sensitivity_Analyzer, and the tool server's residue tool all run this same walk
with recording suppressed, so they read the corpus and report bands and distances
without writing a row of any kind.

**A recorded finding goes through the fence, and a recording pass cannot forget
it.** A `residue_candidate` row is evidence about the run rather than memory
content, and the fence's obligation covers evidence for the reason the run's other
evidence writes carry it: a worker whose lease was taken over must not leave
findings behind that a later run or a certificate would then account for. So the
recording seam presents the generation its worker holds, read inside the finding
write's own transaction, and a superseded worker records nothing. The generation is
optional on the entry point and not on the recording path: a caller with no lease
is a real caller here — the read-only exposures below have none and would have to
invent one — but a caller that records is refused before the first read when it
names no fence, so the omission cannot pass as a pass that simply found nothing.

Both thresholds come from the configuration surface rather than from constants
here, because an operator overrides them per run and a second spelling of a
default is a second place a change can fail to reach.

Every statement is a whole module-level literal composed from named terms, every
caller-supplied value is a bound parameter, and no identifier is interpolated.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from molt.config.resolve import Configuration
from molt.errors import StoreError
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind
from molt.store import Cursor, MemoryStore
from molt.store.embeddings import (
    COSINE_CEILING,
    COSINE_FLOOR,
    Neighbour,
    index_served,
    select_nearest,
)
from molt.store.fencing import FIRST_GENERATION, fenced
from molt.telemetry import Severity, log, metric

__all__ = [
    "ADJUDICATION_BATCHES_METRIC",
    "AUTO_INCLUDE_REASON",
    "AUTO_INCLUDE_THRESHOLD_KEY",
    "COMPONENT",
    "EXCERPT_BUDGET_KEY",
    "FAIL_CLOSED_METRIC",
    "FAIL_CLOSED_REASON",
    "FILTER_UNCANDIDATED_STATEMENT",
    "INSERT_FINDING_STATEMENT",
    "MAX_QUERY_LIMIT",
    "MAX_TOP_K",
    "QUERY_LIMIT_KEY",
    "RESIDUE_CANDIDATES_METRIC",
    "REVIEW_THRESHOLD_KEY",
    "SELECT_QUERY_ARTIFACTS_STATEMENT",
    "TOP_K_KEY",
    "AdjudicationBatch",
    "Adjudicator",
    "CandidateFilter",
    "Classification",
    "FindingFence",
    "NeighbourSearch",
    "QueryArtifact",
    "ResidueBand",
    "ResidueDetector",
    "ResidueFinding",
    "ResiduePolicy",
    "ResidueReport",
    "Verdict",
    "detect_residue",
    "record_finding",
    "residue_report",
    "select_query_artifacts",
    "store_candidate_filter",
    "store_neighbour_search",
    "store_recorder",
    "uncandidated",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "erase"

# The configuration keys the two thresholds and the two bounds resolve from. They
# are named here and read through the configuration surface rather than restated
# as numbers, so an operator's override reaches this phase.
AUTO_INCLUDE_THRESHOLD_KEY: Final[str] = "MOLT_AUTO_INCLUDE_THRESHOLD"
REVIEW_THRESHOLD_KEY: Final[str] = "MOLT_REVIEW_THRESHOLD"
QUERY_LIMIT_KEY: Final[str] = "MOLT_RESIDUE_QUERY_LIMIT"
TOP_K_KEY: Final[str] = "MOLT_RESIDUE_TOP_K"
EXCERPT_BUDGET_KEY: Final[str] = "MOLT_ADJUDICATION_PREFIX_BUDGET_BYTES"

# The ceilings a resolved bound may not exceed. A limit is a bound on work rather
# than a preference, so an override past these is refused rather than honoured.
MAX_QUERY_LIMIT: Final[int] = 1000
MAX_TOP_K: Final[int] = 1000

# The reasons this phase records without an Adjudicator's help. Every other reason
# arrives on a Verdict.
AUTO_INCLUDE_REASON: Final[str] = "below_auto_include_threshold"
FAIL_CLOSED_REASON: Final[str] = "adjudication_unavailable_fail_closed"

# The measurements this phase emits. The fail-closed count is the one that has to
# be visible: a fail-closed inclusion looks exactly like an adjudicated one in the
# stored row apart from a flag, and it is the count of them that tells an operator
# the provider was not answering.
#
# That count is emitted under the *table's* name for a fail-closed adjudication
# rather than a residue-specific spelling of it. A fail-closed inclusion recorded
# here and a fail-closed verdict recorded by the Adjudicator are the same
# condition seen from two call sites, and a deployment alarming on the declared
# name would otherwise miss every one of them raised on this path.
RESIDUE_CANDIDATES_METRIC: Final[str] = "erasure.residue_candidates"
ADJUDICATION_BATCHES_METRIC: Final[str] = "erasure.residue_adjudication_batches"
FAIL_CLOSED_METRIC: Final[str] = "erasure.adjudication_fail_closed"

# How many columns each row shape carries, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_QUERY_ROW_WIDTH: Final[int] = 5
_IDENTIFIER_ROW_WIDTH: Final[int] = 1

# The brackets and separator a stored vector's text form carries, which is the
# form the projection below returns it in.
_VECTOR_OPEN: Final[str] = "["
_VECTOR_CLOSE: Final[str] = "]"
_VECTOR_SEPARATOR: Final[str] = ","

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The query-Artifact selection, over the two kinds of the candidate set that carry
# text, joined to the stored vector each one ranks its neighbours by.
#
# The two branches ask the same question of two tables: an Event's text is the
# Ledger's body and a Derived_Artifact's is its own, and both reach a vector
# through the Embedding table keyed by the Artifact and its kind. A candidate with
# no stored vector is absent rather than returned with a hole, because a query
# Artifact with no vector cannot rank anything; the sweep records the count of
# those separately on the run row.
#
# The ordering is descending text length within kind, with the identifier as the
# tie-break so the order is total and a replay selects the same Artifacts. The
# excerpt is truncated by the cluster under the configured budget, so a body far
# above it never crosses the wire.
_QUERY_EVENT_TERM: Final[str] = (
    "SELECT c.artifact_id, c.artifact_kind, "
    "length(coalesce(l.text_body, '')) AS text_length, "
    "left(coalesce(l.text_body, ''), %s) AS excerpt, m.vec::STRING AS rendered "
    "FROM erasure_candidate AS c "
    "JOIN ledger AS l ON l.id = c.artifact_id "
    "JOIN embedding AS m ON m.artifact_id = c.artifact_id AND m.artifact_kind = 'event' "
    "WHERE c.run_id = %s AND c.artifact_kind = 'event' "
    "AND coalesce(l.text_body, '') <> '' "
)
_QUERY_DERIVED_TERM: Final[str] = (
    "SELECT c.artifact_id, c.artifact_kind, length(d.body) AS text_length, "
    "left(d.body, %s) AS excerpt, m.vec::STRING AS rendered "
    "FROM erasure_candidate AS c "
    "JOIN derived_artifact AS d ON d.id = c.artifact_id "
    "JOIN embedding AS m ON m.artifact_id = c.artifact_id "
    "AND m.artifact_kind = 'derived_artifact' "
    "WHERE c.run_id = %s AND c.artifact_kind = 'derived_artifact' AND d.body <> ''"
)
_QUERY_PROJECTION: Final[str] = (
    "SELECT artifact_id, artifact_kind, text_length, excerpt, rendered FROM ("
)
_QUERY_UNION_TERM: Final[str] = "UNION ALL "
_QUERY_ORDERING_TERM: Final[str] = (
    ") AS q ORDER BY q.artifact_kind ASC, q.text_length DESC, q.artifact_id ASC LIMIT %s"
)
SELECT_QUERY_ARTIFACTS_STATEMENT: Final[str] = (
    _QUERY_PROJECTION
    + _QUERY_EVENT_TERM
    + _QUERY_UNION_TERM
    + _QUERY_DERIVED_TERM
    + _QUERY_ORDERING_TERM
)

# The anti-join. A page of neighbour identifiers travels in as one bound array and
# the cluster answers which of them the candidate set does not hold, so the
# disjointness of the residue set from the explicit set is asserted by the cluster
# over committed rows rather than by a set this process assembled.
FILTER_UNCANDIDATED_STATEMENT: Final[str] = (
    "SELECT n.artifact_id FROM unnest(%s::UUID[]) AS n (artifact_id) "
    "WHERE NOT EXISTS ("
    "SELECT 1 FROM erasure_candidate AS c "
    "WHERE c.run_id = %s AND c.artifact_id = n.artifact_id)"
)

# The finding write, keyed by the run and the Artifact so a candidate reached from
# several query Artifacts holds one row.
#
# The conflict update keeps the smallest distance and carries the nearer row's
# evidence with it, because a band, a query Artifact, and a reason describe one
# distance and mixing them across two would record a decision nothing took.
# Inclusion is the one disjunctive column: an Artifact any query Artifact included
# stays included, which is the fail-closed direction.
_NEARER: Final[str] = "excluded.cosine_distance < residue_candidate.cosine_distance"
_CARRIED_COLUMNS: Final[tuple[str, ...]] = (
    "query_artifact_id",
    "artifact_kind",
    "band",
    "adjudicated",
    "model_id",
    "prompt_digest",
    "classification",
    "reasoning",
    "decision_reason",
)
_CARRY_TERM: Final[str] = ", ".join(
    f"{column} = CASE WHEN {_NEARER} THEN excluded.{column} ELSE residue_candidate.{column} END"
    for column in _CARRIED_COLUMNS
)
INSERT_FINDING_STATEMENT: Final[str] = (
    "INSERT INTO residue_candidate ("
    "run_id, artifact_id, artifact_kind, query_artifact_id, cosine_distance, band, "
    "adjudicated, model_id, prompt_digest, classification, reasoning, included, "
    "decision_reason) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (run_id, artifact_id) DO UPDATE SET " + _CARRY_TERM + ", "
    "cosine_distance = least(residue_candidate.cosine_distance, excluded.cosine_distance), "
    "included = residue_candidate.included OR excluded.included"
)

_QUERY_LABEL: Final[str] = "residue_query_artifacts"
_FINDING_LABEL: Final[str] = "residue_finding"


# ---------------------------------------------------------------------------
# What a finding is
# ---------------------------------------------------------------------------


class ResidueBand(StrEnum):
    """Which side of the auto-inclusion threshold a candidate's distance fell on.

    The two values are the two the stored check constraint admits, so a band this
    module records is a band the row can hold.
    """

    AUTO_INCLUDE = "auto_include"
    REVIEW = "review"


class Classification(StrEnum):
    """What an Adjudicator concluded about one candidate."""

    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True, slots=True)
class ResiduePolicy:
    """The two thresholds and the two bounds one residue pass runs under.

    Held as a value rather than read key by key at each use, so the values a run
    used are one object the run row and the certificate can both be written from.
    """

    auto_include_threshold: float
    review_threshold: float
    query_limit: int
    top_k: int
    excerpt_characters: int

    def __post_init__(self) -> None:
        for name, value in (
            ("the auto-inclusion threshold", self.auto_include_threshold),
            ("the review threshold", self.review_threshold),
        ):
            if not math.isfinite(value) or not COSINE_FLOOR <= value <= COSINE_CEILING:
                raise ValueError(f"{name} must be a cosine distance in range")
        if self.review_threshold < self.auto_include_threshold:
            raise ValueError("the review threshold must not be below the auto-inclusion threshold")
        for name, bound, ceiling in (
            ("the query limit", self.query_limit, MAX_QUERY_LIMIT),
            ("the neighbour bound", self.top_k, MAX_TOP_K),
        ):
            if bound < 1 or bound > ceiling:
                raise ValueError(f"{name} must be a usable bound")
        if self.excerpt_characters < 1:
            raise ValueError("the excerpt budget must admit at least one character")

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> ResiduePolicy:
        """Resolve the policy from the configuration surface.

        Every value is read through the surface rather than defaulted here, which
        is what makes both thresholds operator-overridable in one place.
        """
        return cls(
            auto_include_threshold=configuration.number(AUTO_INCLUDE_THRESHOLD_KEY),
            review_threshold=configuration.number(REVIEW_THRESHOLD_KEY),
            query_limit=configuration.integer(QUERY_LIMIT_KEY),
            top_k=configuration.integer(TOP_K_KEY),
            excerpt_characters=configuration.integer(EXCERPT_BUDGET_KEY),
        )

    def band_for(self, distance: float) -> ResidueBand:
        """Which band one settled distance falls in.

        At or below the auto-inclusion threshold is the auto band, which is where
        the threshold comparison is decided and the only place it is decided.
        """
        if distance <= self.auto_include_threshold:
            return ResidueBand.AUTO_INCLUDE
        return ResidueBand.REVIEW


@dataclass(frozen=True, slots=True)
class QueryArtifact:
    """One Artifact of the candidate set that ranks its neighbours.

    Attributes:
        artifact_id: The Artifact the page is ranked around.
        artifact_kind: Which table the Artifact lives in.
        text_length: The whole body's length, which is what selected it.
        excerpt: The body truncated to the configured budget, which is what the
            Adjudicator's Stable_Prefix is built from.
        vector: The stored unit vector the neighbour query ranks by.
    """

    artifact_id: UUID
    artifact_kind: ArtifactKind
    text_length: int
    excerpt: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    """One Adjudicator conclusion about one review-band candidate.

    The evidence fields are the ones the stored row carries, so a verdict is
    recorded rather than summarised. `adjudicated` is false on the fail-closed
    path, where the classification is an inclusion nothing judged.
    """

    artifact_id: UUID
    classification: Classification
    included: bool
    decision_reason: str
    adjudicated: bool = True
    model_id: str | None = None
    prompt_digest: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class AdjudicationBatch:
    """Every review-band candidate sharing one query Artifact, dispatched together.

    The batch is the unit of dispatch rather than the candidate, which is what
    lets the Adjudicator build one Stable_Prefix and reuse it: the excerpt of the
    query Artifact is in the batch, and nothing that varies per candidate is.
    """

    query_artifact: QueryArtifact
    candidates: tuple[Neighbour, ...]
    distances: Mapping[UUID, float]


class Adjudicator(Protocol):
    """The one call this phase makes on the Adjudicator.

    Narrow on purpose, and reached as an injected value rather than an import.
    This phase decides which candidates need judging and in what groups; the
    Adjudicator decides what the model is asked and how the answer is read. Stated
    as a protocol, the two are separately testable and this module is drivable by
    a stub.
    """

    def adjudicate(self, batch: AdjudicationBatch) -> Sequence[Verdict]:
        """Return one verdict per candidate of the batch, in any order."""
        ...


@dataclass(frozen=True, slots=True)
class ResidueFinding:
    """One recorded residue candidate: its distance, its band, and why it was decided.

    `within_auto_include` and `within_review` are the threshold comparisons
    themselves rather than something a reader recomputes from the distance and a
    threshold it has to find, because the comparison is what the decision was
    taken on and the thresholds move per run.
    """

    artifact_id: UUID
    artifact_kind: ArtifactKind
    query_artifact_id: UUID
    cosine_distance: float
    band: ResidueBand
    within_auto_include: bool
    within_review: bool
    included: bool
    decision_reason: str
    adjudicated: bool = False
    model_id: str | None = None
    prompt_digest: str | None = None
    classification: Classification | None = None
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class ResidueReport:
    """What one residue pass concluded, and under which thresholds.

    The policy travels with the findings because the certificate states the values
    the run actually used, and a report read without them cannot say what a band
    meant.
    """

    findings: tuple[ResidueFinding, ...]
    policy: ResiduePolicy
    query_artifact_ids: tuple[UUID, ...]
    read_only: bool
    adjudication_batches: int

    @property
    def included_ids(self) -> frozenset[UUID]:
        """Every Artifact this pass extends the candidate set with."""
        return frozenset(finding.artifact_id for finding in self.findings if finding.included)

    @property
    def candidate_ids(self) -> frozenset[UUID]:
        """Every Artifact this pass recorded, included or not."""
        return frozenset(finding.artifact_id for finding in self.findings)


@dataclass(frozen=True, slots=True)
class FindingFence:
    """The tenant and the generation each recorded finding presents to the fence.

    Carried as one value rather than as two parameters because the fence needs both
    of them in the write's own transaction, and a caller that supplied one without
    the other would read as a fenced write while guarding nothing. The generation
    travels with the run rather than being read per finding, since the fence reads
    the current generation inside each write's transaction and compares it against
    this one; a caller that has been superseded therefore learns so at the write.

    Attributes:
        client_id: The tenant whose current lease the finding write is checked
            against.
        generation: The generation the recording worker believes it holds.
    """

    client_id: UUID
    generation: int

    def __post_init__(self) -> None:
        """Refuse a fence whose generation names no lease that was ever granted."""
        if self.generation < FIRST_GENERATION:
            raise ValueError(
                f"a presented fencing generation is at least {FIRST_GENERATION}, "
                "so nothing below it names a lease a finding could be recorded under"
            )


# The three seams the walk reaches storage through. Each is a callable rather than
# a store handle so the walk is drivable with stubs, and each has a store-backed
# builder below so the wired-up path is the same walk.
NeighbourSearch = Callable[[QueryArtifact, float, int], Sequence[Neighbour]]
CandidateFilter = Callable[[Sequence[UUID]], frozenset[UUID]]
FindingRecorder = Callable[[ResidueFinding], None]


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


class ResidueDetector:
    """The semantic pass: two walks over the pages, then one dispatch per group.

    Held as an object because everything it needs resolves once per run — the
    policy, the two storage seams, the Adjudicator, and whether anything is
    recorded — and a caller then supplies the query Artifacts and nothing else.
    """

    __slots__ = ("_adjudicator", "_filter", "_policy", "_recorder", "_search")

    def __init__(
        self,
        policy: ResiduePolicy,
        *,
        neighbours: NeighbourSearch,
        uncandidated_ids: CandidateFilter,
        adjudicator: Adjudicator | None = None,
        recorder: FindingRecorder | None = None,
    ) -> None:
        """Build the pass.

        Args:
            policy: The thresholds and bounds, resolved from configuration.
            neighbours: The neighbour query, asked per query Artifact at a
                ceiling and a bound.
            uncandidated_ids: The SQL anti-join, answering which of a page the
                candidate set does not hold.
            adjudicator: The Adjudicator, or None. None is the read-only and
                not-yet-wired case, and a review-band candidate then takes the
                fail-closed path rather than being silently excluded.
            recorder: Where a finding is written, or None for a pass that records
                nothing, which is the read-only mode.
        """
        self._policy = policy
        self._search = neighbours
        self._filter = uncandidated_ids
        self._adjudicator = adjudicator
        self._recorder = recorder

    @property
    def policy(self) -> ResiduePolicy:
        """The thresholds and bounds this pass runs under."""
        return self._policy

    @property
    def read_only(self) -> bool:
        """Whether this pass writes nothing at all."""
        return self._recorder is None

    def detect(
        self,
        query_artifacts: Sequence[QueryArtifact],
        *,
        candidate_ids: Iterable[UUID] = (),
    ) -> ResidueReport:
        """Walk every query Artifact's neighbours and decide each candidate once.

        The in-memory candidate set is a second exclusion rather than the only
        one: the pages have already been anti-joined against the stored set by
        the seam, and this set catches a candidate that landed after a page was
        read.
        """
        excluded = frozenset(candidate_ids)
        settled = self._settle(query_artifacts, excluded)
        findings, batches = self._decide(settled, query_artifacts)
        for finding in findings:
            if self._recorder is not None:
                self._recorder(finding)
        metric(RESIDUE_CANDIDATES_METRIC, float(len(findings)))
        return ResidueReport(
            findings=findings,
            policy=self._policy,
            query_artifact_ids=tuple(query.artifact_id for query in query_artifacts),
            read_only=self.read_only,
            adjudication_batches=batches,
        )

    # -- pass one: the smallest distance per Artifact --------------------

    def _settle(
        self,
        query_artifacts: Sequence[QueryArtifact],
        excluded: frozenset[UUID],
    ) -> dict[UUID, tuple[Neighbour, UUID]]:
        """The nearest sighting of each Artifact, across every query Artifact.

        Keyed by Artifact, so an Artifact reached from three query Artifacts is
        decided once, at its smallest distance, by the query Artifact that found
        it there. Deciding per sighting instead would have banded the same
        Artifact twice and adjudicated it at a distance a later page bettered.
        """
        settled: dict[UUID, tuple[Neighbour, UUID]] = {}
        for query in query_artifacts:
            page = self._search(query, self._policy.review_threshold, self._policy.top_k)
            admitted = self._filter([row.artifact_id for row in page])
            for row in page:
                if row.artifact_id == query.artifact_id:
                    continue
                if row.artifact_id not in admitted or row.artifact_id in excluded:
                    continue
                held = settled.get(row.artifact_id)
                if held is None or row.cosine_distance < held[0].cosine_distance:
                    settled[row.artifact_id] = (row, query.artifact_id)
        return settled

    # -- pass two: band, group, dispatch ---------------------------------

    def _decide(
        self,
        settled: Mapping[UUID, tuple[Neighbour, UUID]],
        query_artifacts: Sequence[QueryArtifact],
    ) -> tuple[tuple[ResidueFinding, ...], int]:
        """Band every settled distance and decide the review band in groups."""
        auto: list[ResidueFinding] = []
        review: dict[UUID, list[Neighbour]] = {}
        distances: dict[UUID, float] = {}
        for neighbour, query_id in settled.values():
            distances[neighbour.artifact_id] = neighbour.cosine_distance
            if self._policy.band_for(neighbour.cosine_distance) is ResidueBand.AUTO_INCLUDE:
                auto.append(self._auto_finding(neighbour, query_id))
            else:
                review.setdefault(query_id, []).append(neighbour)

        by_id = {query.artifact_id: query for query in query_artifacts}
        decided: list[ResidueFinding] = list(auto)
        batches = 0
        for query_id, candidates in review.items():
            ordered = tuple(sorted(candidates, key=_page_order))
            decided.extend(self._review_findings(by_id[query_id], ordered, distances))
            batches += 1
        return tuple(sorted(decided, key=_finding_order)), batches

    def _auto_finding(self, neighbour: Neighbour, query_id: UUID) -> ResidueFinding:
        """One candidate the auto-inclusion band admits, with no provider call.

        Nothing is dispatched from here and nothing can be: the Adjudicator is
        reached from the review branch alone, which is what makes the promise
        that a candidate at or below the threshold costs no call structural.
        """
        return ResidueFinding(
            artifact_id=neighbour.artifact_id,
            artifact_kind=neighbour.artifact_kind,
            query_artifact_id=query_id,
            cosine_distance=neighbour.cosine_distance,
            band=ResidueBand.AUTO_INCLUDE,
            within_auto_include=True,
            within_review=True,
            included=True,
            decision_reason=AUTO_INCLUDE_REASON,
        )

    def _review_findings(
        self,
        query: QueryArtifact,
        candidates: tuple[Neighbour, ...],
        distances: Mapping[UUID, float],
    ) -> tuple[ResidueFinding, ...]:
        """One batch of review-band candidates, dispatched together and recorded.

        A batch with no Adjudicator, or one whose verdicts do not cover every
        candidate, fills the gap with the fail-closed inclusion rather than
        dropping the candidate, and the measurement is emitted so the gap is
        visible.
        """
        verdicts: dict[UUID, Verdict] = {}
        if self._adjudicator is not None:
            batch = AdjudicationBatch(
                query_artifact=query,
                candidates=candidates,
                distances={row.artifact_id: distances[row.artifact_id] for row in candidates},
            )
            for returned in self._adjudicator.adjudicate(batch):
                verdicts[returned.artifact_id] = returned
            metric(ADJUDICATION_BATCHES_METRIC)

        findings: list[ResidueFinding] = []
        missing = 0
        for row in candidates:
            found = verdicts.get(row.artifact_id)
            if found is None:
                missing += 1
            findings.append(self._review_finding(row, query.artifact_id, found))
        if missing:
            metric(FAIL_CLOSED_METRIC, float(missing))
            log(
                Severity.WARNING,
                COMPONENT,
                "residue candidates were included without adjudication",
                query_artifact_id=str(query.artifact_id),
                candidate_count=missing,
            )
        return tuple(findings)

    def _review_finding(
        self,
        neighbour: Neighbour,
        query_id: UUID,
        verdict: Verdict | None,
    ) -> ResidueFinding:
        """One review-band candidate, from its verdict or from the fail-closed path."""
        if verdict is None:
            return ResidueFinding(
                artifact_id=neighbour.artifact_id,
                artifact_kind=neighbour.artifact_kind,
                query_artifact_id=query_id,
                cosine_distance=neighbour.cosine_distance,
                band=ResidueBand.REVIEW,
                within_auto_include=False,
                within_review=True,
                included=True,
                decision_reason=FAIL_CLOSED_REASON,
                adjudicated=False,
            )
        return ResidueFinding(
            artifact_id=neighbour.artifact_id,
            artifact_kind=neighbour.artifact_kind,
            query_artifact_id=query_id,
            cosine_distance=neighbour.cosine_distance,
            band=ResidueBand.REVIEW,
            within_auto_include=False,
            within_review=True,
            included=verdict.included,
            decision_reason=verdict.decision_reason,
            adjudicated=verdict.adjudicated,
            model_id=verdict.model_id,
            prompt_digest=verdict.prompt_digest,
            classification=verdict.classification,
            reasoning=verdict.reasoning,
        )


def _page_order(neighbour: Neighbour) -> tuple[float, str]:
    """The order a batch's candidates are dispatched in, total by construction."""
    return (neighbour.cosine_distance, str(neighbour.artifact_id))


def _finding_order(finding: ResidueFinding) -> tuple[float, str]:
    """The order findings are reported in: nearest first, identifier to settle ties."""
    return (finding.cosine_distance, str(finding.artifact_id))


# ---------------------------------------------------------------------------
# The store-backed seams
# ---------------------------------------------------------------------------


def select_query_artifacts(
    cursor: Cursor,
    run_id: UUID,
    *,
    limit: int,
    excerpt_characters: int,
) -> tuple[QueryArtifact, ...]:
    """The candidate set's longest text-bearing Artifacts, on a caller's cursor."""
    cursor.execute(
        SELECT_QUERY_ARTIFACTS_STATEMENT,
        (excerpt_characters, run_id, excerpt_characters, run_id, limit),
    )
    return tuple(_query_artifact_of(row) for row in cursor.fetchall())


def uncandidated(cursor: Cursor, run_id: UUID, ids: Sequence[UUID]) -> frozenset[UUID]:
    """Which of a page of neighbours the candidate set does not already hold."""
    if not ids:
        return frozenset()
    cursor.execute(FILTER_UNCANDIDATED_STATEMENT, (list(dict.fromkeys(ids)), run_id))
    return frozenset(_as_uuid(_column(row, 0, _IDENTIFIER_ROW_WIDTH)) for row in cursor.fetchall())


def record_finding(cursor: Cursor, run_id: UUID, finding: ResidueFinding) -> None:
    """Write one finding, keeping the smallest distance when the row already stands."""
    cursor.execute(
        INSERT_FINDING_STATEMENT,
        (
            run_id,
            finding.artifact_id,
            str(finding.artifact_kind),
            finding.query_artifact_id,
            finding.cosine_distance,
            str(finding.band),
            finding.adjudicated,
            finding.model_id,
            finding.prompt_digest,
            None if finding.classification is None else str(finding.classification),
            finding.reasoning,
            finding.included,
            finding.decision_reason,
        ),
    )


def store_neighbour_search(
    store: MemoryStore,
    *,
    permitted_clients: Sequence[UUID],
) -> NeighbourSearch:
    """The neighbour seam, answered by the store's own neighbour query.

    The permitted set is every Client of the fleet rather than the erased one
    alone, because residue is by definition content the erased Client's labels do
    not name. Which form of the query is sent is the store's recorded probe
    result, read once here rather than per query Artifact.
    """
    served = index_served(store)

    def search(query: QueryArtifact, max_cosine: float, limit: int) -> Sequence[Neighbour]:
        def body(cursor: Cursor) -> tuple[Neighbour, ...]:
            return select_nearest(
                cursor,
                query.vector,
                permitted_clients=permitted_clients,
                limit=limit,
                max_cosine=max_cosine,
                index_served=served,
            )

        return store.read(body)

    return search


def store_candidate_filter(store: MemoryStore, run_id: UUID) -> CandidateFilter:
    """The anti-join seam, answered by the cluster over committed candidate rows."""

    def admitted(ids: Sequence[UUID]) -> frozenset[UUID]:
        def body(cursor: Cursor) -> frozenset[UUID]:
            return uncandidated(cursor, run_id, ids)

        return store.read(body)

    return admitted


def store_recorder(store: MemoryStore, run_id: UUID, fence: FindingFence) -> FindingRecorder:
    """The recording seam, one finding to a fenced transaction.

    The transaction is the fenced one rather than a plain serializable one. A
    finding is evidence about the run, so the generation read runs on this write's
    own cursor ahead of the insert, and a worker whose lease was taken over records
    no row at all rather than a row a later run would account for. The refusal
    propagates out of the walk, which is what ends the run that owned it.
    """

    def write(finding: ResidueFinding) -> None:
        def body(cursor: Cursor) -> None:
            record_finding(cursor, run_id, finding)

        fenced(store, fence.client_id, fence.generation, body, label=_FINDING_LABEL)

    return write


def detect_residue(
    store: MemoryStore,
    run_id: UUID,
    policy: ResiduePolicy,
    *,
    permitted_clients: Sequence[UUID],
    fence: FindingFence | None = None,
    adjudicator: Adjudicator | None = None,
    read_only: bool = False,
) -> ResidueReport:
    """Run phase two against a stored run row.

    Args:
        store: The cluster the corpus lives in.
        run_id: The Erasure_Run the candidate set and the findings belong to. A
            read-only pass names a synthetic run row, which is what the CLI verb
            and the tool server hold.
        policy: The thresholds and bounds, resolved from configuration.
        permitted_clients: The Clients the neighbour query may return content
            for, which is the whole fleet on the erasure path.
        fence: The tenant and the generation each recorded finding presents, which
            a recording pass names and a read-only pass has no use for. Optional
            because a caller holding no lease is a real caller — the read-only
            exposures below hold none — and required in effect wherever anything
            is recorded, by the refusal below.
        adjudicator: The Adjudicator, or None to take the fail-closed path for
            every review-band candidate.
        read_only: True suppresses recording entirely, so the pass reads the
            corpus and writes nothing.

    Raises:
        ValueError: The pass records findings and named no fence, so its writes
            would guard nothing. Refused before the corpus is read at all, rather
            than allowed to record unfenced evidence.
    """
    recorder: FindingRecorder | None = None
    if not read_only:
        if fence is None:
            raise ValueError(
                "a residue pass that records findings presents the generation its "
                "worker holds, so a recording pass names the fence it writes behind"
            )
        recorder = store_recorder(store, run_id, fence)

    def query_body(cursor: Cursor) -> tuple[QueryArtifact, ...]:
        return select_query_artifacts(
            cursor,
            run_id,
            limit=policy.query_limit,
            excerpt_characters=policy.excerpt_characters,
        )

    queries = store.read(query_body)
    detector = ResidueDetector(
        policy,
        neighbours=store_neighbour_search(store, permitted_clients=permitted_clients),
        uncandidated_ids=store_candidate_filter(store, run_id),
        adjudicator=adjudicator,
        recorder=recorder,
    )
    report = detector.detect(queries)
    log(
        Severity.INFO,
        COMPONENT,
        "residue detection completed",
        run_id=str(run_id),
        read_only=read_only,
        query_artifacts=len(queries),
        candidates=len(report.findings),
    )
    return report


def residue_report(
    store: MemoryStore,
    run_id: UUID,
    policy: ResiduePolicy,
    *,
    permitted_clients: Sequence[UUID],
    adjudicator: Adjudicator | None = None,
) -> ResidueReport:
    """The read-only exposure of the same path, which mutates no memory content.

    This is what the CLI residue verb, the Sensitivity_Analyzer, and the tool
    server's residue tool call. It is the same walk with recording suppressed
    rather than a second implementation, so a reported band and a recorded band
    cannot come to mean different things.

    No fence is named and none is needed: none of these three callers holds an
    erasure lease, and because recording is suppressed there is no write for a
    generation to guard. That is the whole reason the fence is optional rather than
    demanded of every caller.
    """
    return detect_residue(
        store,
        run_id,
        policy,
        permitted_clients=permitted_clients,
        adjudicator=adjudicator,
        read_only=True,
    )


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------


def _query_artifact_of(row: Sequence[object]) -> QueryArtifact:
    """Build one query Artifact from a selected row."""
    return QueryArtifact(
        artifact_id=_as_uuid(_column(row, 0, _QUERY_ROW_WIDTH)),
        artifact_kind=ArtifactKind(_as_str(_column(row, 1, _QUERY_ROW_WIDTH))),
        text_length=_as_count(_column(row, 2, _QUERY_ROW_WIDTH)),
        excerpt=_as_str(_column(row, 3, _QUERY_ROW_WIDTH)),
        vector=_as_vector(_column(row, 4, _QUERY_ROW_WIDTH)),
    )


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a residue row carried {len(row)} columns rather than {width}")
    return row[index]


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a column whose type is not the one the schema declares."""
    return StoreError(f"a residue row carried {type(value).__name__} rather than {expected}")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_count(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a count")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a count")


def _as_vector(value: object) -> tuple[float, ...]:
    """Decode the text form the vector projection returns.

    The store renders a vector to text on the way in and this is the way back,
    which is needed because the neighbour query ranks against a vector held in
    this process. The width is checked here rather than trusted, so a stored row
    of another width is refused before it is ranked against.
    """
    text = _as_str(value).strip()
    if not text.startswith(_VECTOR_OPEN) or not text.endswith(_VECTOR_CLOSE):
        raise _unexpected(value, "a rendered vector")
    body = text[len(_VECTOR_OPEN) : -len(_VECTOR_CLOSE)]
    try:
        components = tuple(float(part) for part in body.split(_VECTOR_SEPARATOR))
    except ValueError as error:
        raise _unexpected(value, "a rendered vector") from error
    if len(components) != EMBEDDING_DIMENSION:
        raise StoreError(
            f"a stored vector carried {len(components)} components rather than "
            f"{EMBEDDING_DIMENSION}"
        )
    return components
