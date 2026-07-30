"""Reusable database corpora shared by Recall Properties 16 and 17.

`corpora_with_permissions()` varies a bank selection, a permitted subset, and k
across 100 Hypothesis examples without repeating expensive placement. Each property
module owns an isolated eight-entry bank: two permission profiles in each of four
size bands across 2 to 5 Clients. Every entry includes Events, Summaries,
Learned_Procedures, both storage kinds, all tie shapes, binding-only admission, and
a nearer unpermitted neighbor; entries with multi-Client permission sets also carry
the double binding that detects duplicate-producing joins.

Six decisions in here are load-bearing.

**The beam follows each query.** The candidate stage is approximate and can omit
near rows at the cluster default. Setup therefore certifies actual candidate-stage
membership once after the complete bank is placed. Every certification and recall
then sets both connections to that query's `candidate_pool_for(k)`, avoiding both
silent omission and a fixed worst-case beam on small pages.

**Every reusable corpus ranks around a direction of its own.** The bank entries
share one schema, and each placement derives fresh query and far directions. Its
whole corpus lies inside a narrow angular cap while other bank entries remain
outside it. The one-time horizon and candidate-stage reads certify both facts after
the final placement, when all competing rows are already present.

Fresh identifiers rather than drawn seeds make direction collisions unavailable:
Hypothesis may revisit a bank entry freely, but it never causes another placement
or grows the table during the property run.

**Distances are placed by slot rather than drawn.** A slot is a rung on a ladder of
blend weights between the two directions, and the vector of a slot is the same
vector for every Artifact on it. That is what makes an exact tie constructible: two
Artifacts on one slot are at one distance by construction rather than by a
coincidence of floating point, so the tie-break the ordering property is about is
reached in every example rather than in the lucky ones. It is also what keeps the
distances distinct everywhere else, so an ordering assertion is about the ordering
rather than about how a sort settled equal keys.

**The nearest three slots are planted, and each plants one thing an assertion needs.**
The nearest slot holds an Artifact no permitted Client is bound to, so every example
really does have an unpermitted near neighbour ahead of everything the caller may
see. The next holds an Artifact whose owning Client is not permitted and which
carries a current binding to a permitted one, so admission by binding rather than by
ownership is exercised rather than assumed to be the same thing. The third holds an
Artifact bound to two permitted Clients when the selected subset has two, which is
the row a tenancy admission written as a join rather than as a semi-join would return
twice. Ties occupy the slots after those, so their members sit inside any admissible
page.

**Only a Learned_Procedure carries a standing, and every standing sits above the
floor.** The schema holds the kind and the confidence in an equivalence, so a
Summary and an Event carry none and the tie-break separates procedures only. The
floor exclusion is a different claim with its own coverage in the integration and
unit suites, and a corpus that drew standings below the floor would shorten pages
for a reason neither property is about, so drawn standings stay above it.

**Rows are placed by this module's own statements, in batches.** The write paths are
covered by their own suites; what these two properties are about is the read. All
eight corpora are inserted once at module setup, in bounded batches, instead of
placing 50 to 500 rows for every one of 100 examples. The
Ledger rows are the one place where that could have meant fabricating something, so
they do not: the sequence numbers, the predecessor digests, and the chain digests
are computed by the chain module's own functions, which leaves each Session holding
a chain that verifies and that a later append continues.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

from hypothesis import strategies as st

from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind
from molt.models.session import SessionOutcome
from molt.recall import MAX_RESULT_LIMIT, Recalled, RecallEngine, candidate_pool_for
from molt.store import Connection, MemoryStore
from molt.store.chain import (
    FIRST_SEQUENCE,
    GENESIS_PREDECESSOR,
    canonical_payload_text,
    chain_digest,
    content_digest_input,
    sha256_hex,
)
from molt.store.embeddings import vector_text
from molt.store.migrate import apply_migrations
from molt.store.retry import (
    DEFAULT_RETRY_POLICY,
    DEFAULT_SLEEP,
    is_serialization_failure,
)

__all__ = [
    "AGENT_CLI",
    "MAX_ARTIFACTS",
    "MAX_CLIENTS",
    "MIN_ARTIFACTS",
    "MIN_CLIENTS",
    "POOL_HORIZON",
    "QUERY_TEXT",
    "RECALL_FLOOR",
    "Cluster",
    "CorpusBank",
    "CorpusCase",
    "CorpusPlan",
    "Horizon",
    "PlacedArtifact",
    "PlacedCorpus",
    "PlannedArtifact",
    "PlannedKind",
    "RecordedRetrievals",
    "StoredProvenance",
    "StubQueryEmbedder",
    "TieGroup",
    "TieShape",
    "answered_page",
    "build_corpus_bank",
    "corpora_with_permissions",
    "count_band",
    "open_cluster",
    "require_uncrowded_pool",
    "size_band",
    "tie_order_key",
    "unit_vector",
]

# How many Embeddings one corpus holds and how many Clients it is spread over.
MIN_ARTIFACTS: Final[int] = 50
MAX_ARTIFACTS: Final[int] = 500
MIN_CLIENTS: Final[int] = 2
MAX_CLIENTS: Final[int] = 5

# How many tie groups one corpus plants and how many Artifacts share one distance.
MIN_TIE_GROUPS: Final[int] = 1
MAX_TIE_GROUPS: Final[int] = 3
MIN_TIE_MEMBERS: Final[int] = 2
MAX_TIE_MEMBERS: Final[int] = 3

# How many slots the planted Artifacts occupy ahead of the tie groups: the
# unpermitted near neighbour, the Artifact admitted by a binding rather than by
# ownership, and the Artifact bound to two permitted Clients at once.
PLANTED_SLOTS: Final[int] = 3

# The floor the properties configure, and the standings a drawn procedure may
# carry. Every value here is above the floor: a procedure below it is excluded
# inside the statement's own predicate, which is a claim with its own coverage and
# not one either of these properties is stated over.
RECALL_FLOOR: Final[float] = 0.15
CONFIDENCE_LADDER: Final[tuple[float, ...]] = (0.2, 0.35, 0.5, 0.65, 0.8, 1.0)

# The blend weight of the nearest slot and the distance between two slots. The
# ladder is bounded so that the farthest Artifact of the largest corpus stays well
# inside the cap the horizon read below is taken at.
NEAREST_WEIGHT: Final[float] = 0.05
WEIGHT_STEP: Final[float] = 0.0011

# The cosine distance the cap is drawn at. Every Artifact of an example sits below
# it, and an Artifact of another example, whose direction was drawn independently
# in a space of this width, sits near the orthogonal distance of 1.0 and so above
# it. The properties assert both halves from the cluster rather than trusting them.
POOL_HORIZON: Final[float] = 0.75

# The query text every example asks, which the stub embedder answers with the
# example's own direction. The text is fixed because the vector is what ranks and
# the text is what the recall Event records.
QUERY_TEXT: Final[str] = "the action about to be taken"

# The command every Session records, and the provider and model every Embedding
# row records.
AGENT_CLI: Final[str] = "a-coding-agent"
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# The instant the corpus is placed at and how long a row is retained for. The
# reading is derived from the epoch rather than written out, so no example carries
# a calendar value, and every Session and every Event is placed at a distinct
# offset from it so a confusion between two rows' provenance is visible.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)
SESSION_SPACING: Final[timedelta] = timedelta(minutes=1)
EVENT_SPACING: Final[timedelta] = timedelta(seconds=1)

# How many rows one batched statement carries. Bounded so that the parameter count
# of the widest row shape stays inside what the wire protocol admits.
BATCH_ROWS: Final[int] = 100

# The bound parameter form of a search path change and of a beam width change, so
# the schema name and the width are values rather than statement text even in a
# fixture. A session setting admits no placeholder in its own syntax, which is why
# both go through the configuration function instead.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
SEARCH_BEAM_STATEMENT: Final[str] = "SELECT set_config('vector_search_beam_size', %s, false)"

# The writes this module makes for itself, one statement per row shape.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction, retention_interval) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, started_at, ended_at, outcome) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
INSERT_LEDGER: Final[str] = (
    "INSERT INTO ledger (id, session_id, client_id, seq, category, occurred_at, recorded_at, "
    "agent_cli, machine_id, payload, redacted, text_body, content_digest, prev_chain_digest, "
    "chain_digest, embedding_state, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, %s, %s, %s, %s, %s, %s)"
)
INSERT_DERIVED: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, revision, created_at, updated_at, embedding_state, expires_at, "
    "procedure_confidence) VALUES (%s, %s, %s, %s, %s, 'distil', 1, %s, %s, 'embedded', %s, %s)"
)
INSERT_LINEAGE: Final[str] = (
    "INSERT INTO lineage_edge (id, child_id, parent_id, parent_kind, derivation_method) "
    "VALUES (%s, %s, %s, 'session', 'distil')"
)
INSERT_EMBEDDING: Final[str] = (
    "INSERT INTO embedding (id, artifact_id, artifact_kind, client_id, provider, model_id, "
    "dimension, normalised, vec, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s::VECTOR, %s)"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, confidence, "
    "valid_from) VALUES (%s, %s, %s, %s, %s, 1.0, %s)"
)

# The reads the assertions make against storage rather than against the answer.
#
# The horizon read is what makes the pool's trade auditable inside a property: it
# counts the rows of the whole table that lie inside the cap and how many of those
# belong to the example, so a property can refuse an example whose candidate pool
# could have been crowded by another example's corpus instead of asserting into it.
HORIZON_QUERY: Final[str] = (
    "SELECT count(*) AS within, "
    "count(*) FILTER (WHERE e.client_id = ANY (%s::UUID[])) AS mine "
    "FROM embedding AS e WHERE (e.vec <=> %s::VECTOR) <= %s::FLOAT8"
)

# The candidate stage of the recall statement, read for its membership alone. The
# text is the recall statement's own candidate term rather than a paraphrase of it
# — the same projection, the same ordering expression, the same bound — because a
# read planned differently would answer a different question than the one the
# guard below asks, which is which rows the ranking actually had to choose from.
POOL_QUERY: Final[str] = (
    "SELECT c.artifact_id FROM ("
    "SELECT e.artifact_id, (e.vec <=> %s::VECTOR) AS cosine_distance "
    "FROM embedding AS e ORDER BY e.vec <-> %s::VECTOR LIMIT %s"
    ") AS c"
)
DISTANCE_QUERY: Final[str] = (
    "SELECT (e.vec <=> %s::VECTOR) FROM embedding AS e WHERE e.artifact_id = %s"
)
CURRENT_BINDINGS_QUERY: Final[str] = (
    "SELECT artifact_id, client_id FROM client_binding "
    "WHERE artifact_id = ANY (%s::UUID[]) AND superseded_by IS NULL"
)
EVENT_PROVENANCE_QUERY: Final[str] = (
    "SELECT l.id, l.session_id, l.machine_id, l.occurred_at, s.outcome "
    "FROM ledger AS l JOIN session AS s ON s.id = l.session_id "
    "WHERE l.id = ANY (%s::UUID[])"
)
DERIVED_PROVENANCE_QUERY: Final[str] = (
    "SELECT d.id, s.id, s.machine_id, s.started_at, s.outcome "
    "FROM derived_artifact AS d "
    "JOIN lineage_edge AS le ON le.child_id = d.id AND le.parent_kind = 'session' "
    "JOIN session AS s ON s.id = le.parent_id "
    "WHERE d.id = ANY (%s::UUID[])"
)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module importable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# The result bound an example draws, and the search width it asks for
# ---------------------------------------------------------------------------


def _minimum_limit(size: int, tie_members: int) -> int:
    """The smallest result bound an example may ask for.

    Two conditions meet here. The pool has to be wider than the corpus, because a
    corpus larger than the pool would be a corpus the page is a horizon into, and
    every clause of both properties would then be asserted against that horizon
    rather than against the corpus. And the page has to be long enough to hold the
    planted Artifacts and every tie member, because a tie whose members fall off
    the end of the page is a tie no assertion can see.
    """
    needed = PLANTED_SLOTS + tie_members
    for candidate in range(1, MAX_RESULT_LIMIT + 1):
        if candidate >= needed and candidate_pool_for(candidate) > size:
            return candidate
    return MAX_RESULT_LIMIT


# How far above that smallest bound a drawn bound may reach, so the examples spread
# over page lengths and pool widths instead of every example asking for the
# narrowest page its own corpus admits.
LIMIT_HEADROOM: Final[int] = 24

# The widest bound any example can ask for, which is the headroom above the largest
# smallest bound there is. The smallest bound rises with the corpus and with the
# tied Artifacts both, so it peaks at the largest corpus carrying the most tie
# members, and the draw below is bounded by this same value rather than by the
# largest bound the engine admits.
MAX_DRAWN_LIMIT: Final[int] = min(
    MAX_RESULT_LIMIT,
    _minimum_limit(MAX_ARTIFACTS, MAX_TIE_GROUPS * MAX_TIE_MEMBERS) + LIMIT_HEADROOM,
)

# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


class PlannedKind(StrEnum):
    """What one Artifact of a drawn corpus is.

    An Event reaches its Session through its own row, a Summary and a
    Learned_Procedure reach one through the Lineage_Graph, and only the procedure
    carries a standing. All three are drawn because the provenance criterion is
    asked of every result and the two routes to a Session are different code.
    """

    EVENT = "event"
    SUMMARY = "summary"
    PROCEDURE = "learned_procedure"

    @property
    def is_event(self) -> bool:
        """Whether this Artifact is a Ledger row rather than a Derived_Artifact."""
        return self is PlannedKind.EVENT

    @property
    def carries_standing(self) -> bool:
        """Whether the schema admits a confidence value for this kind."""
        return self is PlannedKind.PROCEDURE


class TieShape(StrEnum):
    """How the Artifacts sharing one distance differ, and so what decides their order.

    Three shapes, because the ordering has three keys and a tie group is how the
    second and third of them become observable. Separated members are told apart
    by the standing; identical members carry one standing, so the Artifact
    identifier is the only key left; a mixed group holds a procedure beside an
    Artifact carrying no standing at all, which is where the absent value sorting
    last is what puts the procedure first.
    """

    SEPARATED = "separated"
    IDENTICAL = "identical"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class PlannedArtifact:
    """One Artifact of a drawn corpus.

    Attributes:
        owner: The Client owning the Session the Artifact was produced under,
            which is also the Client its scope binding names.
        also_bound: A second Client holding a current binding on the Artifact, or
            None. This is the difference between admission by ownership and
            admission by attribution, and the tenancy claim is about the latter.
        kind: What the Artifact is.
        confidence: The standing, present exactly for a Learned_Procedure.
        slot: Which rung of the distance ladder the Artifact sits on. Two
            Artifacts on one slot carry one vector and so one distance.
        group: Which tie group the Artifact belongs to, or None.
    """

    owner: int
    also_bound: int | None
    kind: PlannedKind
    confidence: float | None
    slot: int
    group: int | None

    @property
    def bound_indices(self) -> tuple[int, ...]:
        """Every Client index holding a current binding on this Artifact."""
        return (self.owner,) if self.also_bound is None else (self.owner, self.also_bound)


@dataclass(frozen=True, slots=True)
class TieGroup:
    """One set of Artifacts placed at one distance, and what decides their order."""

    shape: TieShape
    positions: tuple[int, ...]

    @property
    def size(self) -> int:
        """How many Artifacts share the distance."""
        return len(self.positions)


@dataclass(frozen=True, slots=True)
class CorpusPlan:
    """A corpus of stub Embeddings, the Clients it spreads over, and who may see it.

    Attributes:
        client_count: How many Clients the corpus is spread over.
        permitted: The Client indices the caller may see, never all of them, so
            every example holds content that must not be returned.
        outcomes: The terminal classification each Client's Session reached.
        artifacts: The Artifacts, in the order they are placed.
        limit: The result bound the caller asks for.
        groups: The tie groups, each naming the Artifacts sharing one distance.
    """

    client_count: int
    permitted: tuple[int, ...]
    outcomes: tuple[SessionOutcome, ...]
    artifacts: tuple[PlannedArtifact, ...]
    limit: int
    groups: tuple[TieGroup, ...]

    @property
    def size(self) -> int:
        """How many Embeddings the corpus holds."""
        return len(self.artifacts)

    @property
    def permitted_indices(self) -> frozenset[int]:
        """The permitted Clients as a set, for membership questions."""
        return frozenset(self.permitted)

    @property
    def pool(self) -> int:
        """How many candidates the ranking stage considers for this result bound."""
        return candidate_pool_for(self.limit)

    @property
    def decoy(self) -> int:
        """The Artifact on the nearest slot, which no permitted Client may see."""
        return 0

    @property
    def admitted_by_binding(self) -> int:
        """The Artifact an unpermitted Client owns and a permitted Client is bound to."""
        return 1

    @property
    def doubly_bound(self) -> int:
        """The Artifact two permitted Clients hold a current binding on, if any.

        A corpus whose permitted subset holds one Client cannot have one, because
        two current bindings for one pair are what the partial uniqueness forbids
        and two permitted Clients are what a second permitted binding needs.
        """
        return 2

    def permits(self, position: int) -> bool:
        """Whether any Client the caller may see holds a binding on one Artifact."""
        bound = frozenset(self.artifacts[position].bound_indices)
        return bool(bound & self.permitted_indices)

    @property
    def tie_members(self) -> int:
        """How many Artifacts sit on a shared distance across every group."""
        return sum(group.size for group in self.groups)


def tie_order_key(confidence: float | None, artifact_id: UUID) -> tuple[int, float, str]:
    """The order two Artifacts at one distance are ranked in.

    Descending standing with an absent standing last, then the Artifact identifier
    ascending. Stated here rather than read off the statement under test, because a
    tie assertion that took its expectation from the thing it is asserting about
    would hold whatever that thing did.
    """
    if confidence is None:
        return (1, 0.0, str(artifact_id))
    return (0, -confidence, str(artifact_id))


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def _permitted_subsets(client_count: int) -> st.SearchStrategy[tuple[int, ...]]:
    """Draw a non-empty permitted subset that is never every Client.

    Never every Client, because a corpus whose whole content the caller may see
    says nothing about a filter: the unpermitted Clients are what the tenancy claim
    is stated against, so at least one of them is always there.
    """
    return st.lists(
        st.integers(min_value=0, max_value=client_count - 1),
        min_size=1,
        max_size=client_count - 1,
        unique=True,
    ).map(lambda drawn: tuple(sorted(drawn)))


def _tie_shapes() -> st.SearchStrategy[TieShape]:
    """Draw which key a tie group leaves to decide the order."""
    return st.sampled_from(TieShape)


def _standings(count: int, *, distinct: bool) -> st.SearchStrategy[tuple[float, ...]]:
    """Draw standings for the procedures of one tie group.

    Distinct values leave the ordering to the standing. Repeating one value leaves
    it to the Artifact identifier, which is the key that makes the ordering total,
    so a group asking for it draws one value and gives it to every member.
    """
    if not distinct:
        return st.sampled_from(CONFIDENCE_LADDER).map(lambda value: (value,) * count)
    return st.lists(
        st.sampled_from(CONFIDENCE_LADDER),
        min_size=count,
        max_size=count,
        unique=True,
    ).map(tuple)


def _standing_for(kind: PlannedKind) -> st.SearchStrategy[float | None]:
    """Draw the standing one kind may carry, which is one value or none at all.

    The schema holds the kind and the standing in an equivalence rather than an
    implication, so this is not a convention the generator keeps: a Summary
    carrying a value and a procedure carrying none are both unwritable.
    """
    if kind.carries_standing:
        return st.sampled_from(CONFIDENCE_LADDER)
    return st.none()


@st.composite
def _random_corpus_plan(draw: st.DrawFn) -> CorpusPlan:
    """Draw a corpus of 50 to 500 stub Embeddings across 2 to 5 Clients.

    The Clients come first, because everything later is relative to them: who owns
    an Artifact, who else is bound to it, and who the caller may see. The permitted
    subset is drawn next and is never all of them.

    The tie groups are drawn before the ordinary Artifacts and take the slots just
    behind the three planted ones, so their members sit inside any page the drawn
    bound admits. The ordinary Artifacts then fill the rest of the ladder, one to a
    slot, at distances that are distinct by construction.

    The result bound is drawn last, from the range that keeps the pool wider than
    the corpus and the page long enough to hold what was planted. That coupling is
    what stops the two properties from being asserted against the pool's horizon
    instead of against the corpus.
    """
    client_count = draw(st.integers(min_value=MIN_CLIENTS, max_value=MAX_CLIENTS))
    permitted = draw(_permitted_subsets(client_count))
    outcomes = tuple(
        draw(
            st.lists(
                st.sampled_from(
                    (SessionOutcome.SUCCEEDED, SessionOutcome.FAILED, SessionOutcome.ABANDONED)
                ),
                min_size=client_count,
                max_size=client_count,
            )
        )
    )
    unpermitted = tuple(index for index in range(client_count) if index not in set(permitted))
    size = draw(st.integers(min_value=MIN_ARTIFACTS, max_value=MAX_ARTIFACTS))

    artifacts: list[PlannedArtifact] = []

    def planted(owner: int, also_bound: int | None, slot: int) -> PlannedArtifact:
        """One planted Artifact, whose kind is free and whose standing follows it."""
        kind = draw(st.sampled_from(PlannedKind))
        return PlannedArtifact(
            owner=owner,
            also_bound=also_bound,
            kind=kind,
            confidence=draw(_standing_for(kind)),
            slot=slot,
            group=None,
        )

    # The nearest slot: an Artifact no permitted Client holds a binding on, so
    # every example has an unpermitted near neighbour ahead of the whole page. A
    # second binding is drawn from the other unpermitted Clients, so the Artifact
    # can be bound twice and still be one the caller may not see.
    hidden_owner = draw(st.sampled_from(unpermitted))
    hidden_others = tuple(index for index in unpermitted if index != hidden_owner)
    artifacts.append(
        planted(
            hidden_owner,
            draw(st.sampled_from(hidden_others)) if hidden_others else None,
            0,
        )
    )

    # The next slot: owned by a Client the caller may not see, bound to one it may.
    artifacts.append(
        planted(draw(st.sampled_from(unpermitted)), draw(st.sampled_from(permitted)), 1)
    )

    # The third slot: bound to two permitted Clients where the subset holds two,
    # which is the row an admission written as a join would return twice.
    owner = draw(st.sampled_from(permitted))
    others = tuple(index for index in permitted if index != owner)
    artifacts.append(planted(owner, draw(st.sampled_from(others)) if others else None, 2))

    groups: list[TieGroup] = []
    group_count = draw(st.integers(min_value=MIN_TIE_GROUPS, max_value=MAX_TIE_GROUPS))
    for group in range(group_count):
        shape = draw(_tie_shapes())
        members = draw(st.integers(min_value=MIN_TIE_MEMBERS, max_value=MAX_TIE_MEMBERS))
        procedures = members - 1 if shape is TieShape.MIXED else members
        standings = draw(_standings(procedures, distinct=shape is not TieShape.IDENTICAL))
        positions: list[int] = []
        for member in range(members):
            standing = standings[member] if member < procedures else None
            positions.append(len(artifacts))
            artifacts.append(
                PlannedArtifact(
                    owner=draw(st.sampled_from(permitted)),
                    also_bound=None,
                    kind=PlannedKind.PROCEDURE
                    if standing is not None
                    else draw(st.sampled_from((PlannedKind.EVENT, PlannedKind.SUMMARY))),
                    confidence=standing,
                    slot=PLANTED_SLOTS + group,
                    group=group,
                )
            )
        groups.append(TieGroup(shape=shape, positions=tuple(positions)))

    # The rest of the ladder, one Artifact to a slot.
    slot = PLANTED_SLOTS + group_count
    while len(artifacts) < size:
        kind = draw(st.sampled_from(PlannedKind))
        owner = draw(st.integers(min_value=0, max_value=client_count - 1))
        also = draw(
            st.one_of(
                st.none(),
                st.sampled_from([index for index in range(client_count) if index != owner]),
            )
        )
        artifacts.append(
            PlannedArtifact(
                owner=owner,
                also_bound=also,
                kind=kind,
                confidence=draw(st.sampled_from(CONFIDENCE_LADDER))
                if kind.carries_standing
                else None,
                slot=slot,
                group=None,
            )
        )
        slot += 1

    tie_members = sum(group.size for group in groups)
    floor = _minimum_limit(size, tie_members)
    limit = draw(
        st.integers(min_value=floor, max_value=min(MAX_DRAWN_LIMIT, floor + LIMIT_HEADROOM))
    )

    return CorpusPlan(
        client_count=client_count,
        permitted=permitted,
        outcomes=outcomes,
        artifacts=tuple(artifacts),
        limit=limit,
        groups=tuple(groups),
    )


# ---------------------------------------------------------------------------
# Reusable corpus bank and the selections Hypothesis varies
# ---------------------------------------------------------------------------


BANK_SIZES: Final[tuple[int, ...]] = (75, 175, 325, 450)
BANK_PERMISSION_PROFILES: Final[tuple[tuple[tuple[int, ...], ...], ...]] = (
    ((0,), (1,)),
    ((0,), (1, 2)),
    ((0, 2), (1, 2, 3)),
    ((0, 2), (1, 3, 4)),
)


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One reusable bank selection and the result bound for its query.

    The band and permission profile are separate draws. Their pair selects one
    placed corpus whose query direction and current bindings were fixed when the
    module-scoped bank was built; the limit remains a per-example draw.
    """

    band: int
    permission_profile: int
    limit: int


@dataclass(frozen=True, slots=True)
class CorpusBank:
    """The corpora placed once in one property's isolated module schema."""

    cluster: Cluster
    corpora: tuple[PlacedCorpus, ...]

    def select(self, case: CorpusCase) -> PlacedCorpus:
        """Return one bank corpus with the example's independently drawn limit."""
        offset = case.band * len(BANK_PERMISSION_PROFILES[case.band])
        placed = self.corpora[offset + case.permission_profile]
        return replace(placed, plan=replace(placed.plan, limit=case.limit))


def _bank_plan(
    size: int,
    client_count: int,
    permitted: tuple[int, ...],
    variant: int,
) -> CorpusPlan:
    """Build one deterministic bank corpus carrying every required hard case."""
    permitted_set = frozenset(permitted)
    unpermitted = tuple(index for index in range(client_count) if index not in permitted_set)
    artifacts: list[PlannedArtifact] = []

    def add(
        owner: int,
        also_bound: int | None,
        kind: PlannedKind,
        confidence: float | None,
        slot: int,
        group: int | None = None,
    ) -> None:
        artifacts.append(
            PlannedArtifact(
                owner=owner,
                also_bound=also_bound,
                kind=kind,
                confidence=confidence,
                slot=slot,
                group=group,
            )
        )

    # The first three slots retain the tenancy non-vacuity cases for every bank
    # entry: a nearer excluded row, binding-only admission, and (where possible)
    # two permitted current bindings on one Artifact.
    add(unpermitted[0], None, PlannedKind.EVENT, None, 0)
    add(unpermitted[-1], permitted[0], PlannedKind.SUMMARY, None, 1)
    second_permitted = permitted[1] if len(permitted) > 1 else None
    add(permitted[0], second_permitted, PlannedKind.PROCEDURE, 0.8, 2)

    groups: list[TieGroup] = []
    group_specs = (
        (TieShape.SEPARATED, (0.8, 0.5, 0.2)),
        (TieShape.IDENTICAL, (0.65, 0.65)),
        (TieShape.MIXED, (1.0, None)),
    )
    for group_index, (shape, confidences) in enumerate(group_specs):
        positions: list[int] = []
        for member, confidence in enumerate(confidences):
            positions.append(len(artifacts))
            kind = (
                PlannedKind.PROCEDURE
                if confidence is not None
                else (PlannedKind.EVENT if variant == 0 else PlannedKind.SUMMARY)
            )
            add(
                permitted[(group_index + member) % len(permitted)],
                None,
                kind,
                confidence,
                PLANTED_SLOTS + group_index,
                group_index,
            )
        groups.append(TieGroup(shape=shape, positions=tuple(positions)))

    slot = PLANTED_SLOTS + len(groups)
    kinds = tuple(PlannedKind)
    while len(artifacts) < size:
        position = len(artifacts)
        kind = kinds[(position + variant) % len(kinds)]
        owner = (position + variant) % client_count
        also_bound = (owner + 1 + variant) % client_count if position % 5 == 0 else None
        add(
            owner,
            also_bound,
            kind,
            CONFIDENCE_LADDER[(position + variant) % len(CONFIDENCE_LADDER)]
            if kind.carries_standing
            else None,
            slot,
        )
        slot += 1

    outcomes = tuple(
        (SessionOutcome.SUCCEEDED, SessionOutcome.FAILED, SessionOutcome.ABANDONED)[
            (index + variant) % 3
        ]
        for index in range(client_count)
    )
    tie_members = sum(group.size for group in groups)
    return CorpusPlan(
        client_count=client_count,
        permitted=permitted,
        outcomes=outcomes,
        artifacts=tuple(artifacts),
        limit=_minimum_limit(size, tie_members),
        groups=tuple(groups),
    )


BANK_PLANS: Final[tuple[CorpusPlan, ...]] = tuple(
    _bank_plan(size, band + MIN_CLIENTS, permitted, profile)
    for band, size in enumerate(BANK_SIZES)
    for profile, permitted in enumerate(BANK_PERMISSION_PROFILES[band])
)


@st.composite
def corpora_with_permissions(draw: st.DrawFn) -> CorpusCase:
    """Select a reusable 50 to 500 Embedding corpus, permission subset, and k.

    The bank is placed once per property module, but the 100 examples still vary
    all query inputs that matter here: one of four size/client bands, one of two
    permitted subsets for that band (and therefore its query direction), and the
    page limit. Every selected plan carries all three Artifact kinds, both storage
    kinds, all tie shapes, binding-only admission, and an unpermitted nearer row.
    """
    band = draw(st.integers(min_value=0, max_value=len(BANK_SIZES) - 1))
    permission_profile = draw(
        st.integers(min_value=0, max_value=len(BANK_PERMISSION_PROFILES[band]) - 1)
    )
    plan_index = band * len(BANK_PERMISSION_PROFILES[band]) + permission_profile
    plan = BANK_PLANS[plan_index]
    limit = draw(
        st.integers(
            min_value=plan.limit,
            max_value=min(MAX_DRAWN_LIMIT, plan.limit + LIMIT_HEADROOM),
        )
    )
    return CorpusCase(band=band, permission_profile=permission_profile, limit=limit)


# ---------------------------------------------------------------------------
# The vectors a corpus is placed at
# ---------------------------------------------------------------------------


def unit_vector(label: str) -> tuple[float, ...]:
    """A reproducible unit vector of the fixed width, derived from a label.

    Every component carries part of the vector, so a corpus occupies the width the
    column declares and a ranking over it is not a restatement of an angle chosen
    here. The same label always yields the same vector, so an example that failed
    replays against the same directions it failed on.
    """
    needed = EMBEDDING_DIMENSION * 4
    blocks: list[bytes] = []
    counter = 0
    while sum(len(block) for block in blocks) < needed:
        blocks.append(hashlib.sha256(label.encode() + counter.to_bytes(8, "big")).digest())
        counter += 1
    raw = struct.unpack(f">{EMBEDDING_DIMENSION}i", b"".join(blocks)[:needed])
    scaled = [value / 2147483648.0 for value in raw]
    norm = math.sqrt(math.fsum(component * component for component in scaled))
    return tuple(component / norm for component in scaled)


def blended(
    first: tuple[float, ...],
    second: tuple[float, ...],
    weight: float,
) -> tuple[float, ...]:
    """A unit vector between two others, so a corpus sits at chosen distances.

    The weight moves the result from the first vector toward the second, and the
    cosine distance to the first grows with it. Two Artifacts given one weight get
    one vector, which is how an exact tie is constructed rather than hoped for.
    """
    mixed = [a * (1.0 - weight) + b * weight for a, b in zip(first, second, strict=True)]
    norm = math.sqrt(math.fsum(component * component for component in mixed))
    return tuple(component / norm for component in mixed)


def _slot_weight(slot: int) -> float:
    """The blend weight of one rung of the distance ladder."""
    return NEAREST_WEIGHT + slot * WEIGHT_STEP


class StubQueryEmbedder:
    """An embedding surface answering the direction the corpus was placed around."""

    def __init__(self, vector: tuple[float, ...]) -> None:
        self._vector = vector
        self.asked: list[str] = []

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Answer the example's own query vector once per text, recording the text."""
        self.asked.extend(texts)
        return [self._vector for _ in texts]


@dataclass(slots=True)
class RecordedRetrievals:
    """The retrieval records the Confidence_Tracker seam was handed.

    The seam is filled rather than left at its default because the default is one
    transaction per returned Learned_Procedure, and neither of these properties is
    about that write: the integration suite asserts it through the default and
    through an injected recorder both. Filling it here keeps a hundred examples
    affordable without weakening anything either property states.
    """

    seen: list[tuple[UUID, UUID]]

    def record(self, procedure_id: UUID, session_id: UUID) -> None:
        """Keep one retrieval, as the tracker's own recorder would write one."""
        self.seen.append((procedure_id, session_id))


# ---------------------------------------------------------------------------
# The corpus as it was stored
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlacedArtifact:
    """One Artifact of a placed corpus, with the identities the assertions name."""

    artifact_id: UUID
    plan: PlannedArtifact
    owner_client: UUID
    session_id: UUID
    machine_id: str
    occurred_at: datetime
    outcome: SessionOutcome
    bound_clients: frozenset[UUID]

    @property
    def artifact_kind(self) -> ArtifactKind:
        """The Artifact kind the Embedding row and the recall answer carry."""
        return ArtifactKind.EVENT if self.plan.kind.is_event else ArtifactKind.DERIVED_ARTIFACT


@dataclass(frozen=True, slots=True)
class PlacedCorpus:
    """A drawn corpus as it now sits in the schema, and what a query against it needs.

    Attributes:
        plan: What was drawn.
        query: The direction this example's corpus was placed around, which is
            also what the stub embedder answers the query text with.
        clients: The Client identifiers, by drawn index.
        artifacts: The Artifacts, in the drawn order.
        presented: The permitted Clients the caller names for itself, which is
            every permitted Client but the one the asking Session belongs to. The
            engine unions the two, so the Session contributes a Client the request
            body did not name and the union path is exercised rather than assumed.
        asking_session: The Session the query is made within, whose stored row is
            where the tenancy actually comes from.
    """

    plan: CorpusPlan
    query: tuple[float, ...]
    clients: tuple[UUID, ...]
    artifacts: tuple[PlacedArtifact, ...]
    presented: tuple[UUID, ...]
    asking_session: UUID

    @property
    def permitted_clients(self) -> frozenset[UUID]:
        """Every Client identifier the caller may see content for."""
        return frozenset(self.clients[index] for index in self.plan.permitted)

    @property
    def by_id(self) -> Mapping[UUID, PlacedArtifact]:
        """The placed Artifacts by identifier, for reading an answer back."""
        return {placed.artifact_id: placed for placed in self.artifacts}

    def at(self, position: int) -> PlacedArtifact:
        """The Artifact drawn at one position of the plan."""
        return self.artifacts[position]

    @property
    def permitted_positions(self) -> tuple[int, ...]:
        """Every position whose Artifact the caller may see."""
        return tuple(index for index in range(self.plan.size) if self.plan.permits(index))


@dataclass(frozen=True, slots=True)
class Horizon:
    """What lies inside the cap this example's corpus occupies.

    Attributes:
        within: How many Embeddings of the whole table lie inside the cap.
        mine: How many of those belong to this example's Clients.
    """

    within: int
    mine: int


@dataclass(frozen=True, slots=True)
class StoredProvenance:
    """The provenance of one Artifact, read back from the rows that hold it."""

    session_id: UUID
    machine_id: str
    occurred_at: datetime
    outcome: str


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and this module's own reads."""

    store: MemoryStore
    connection: DriverConnection

    def configure_search(self, candidate_pool: int) -> None:
        """Apply one query's candidate-pool beam to every connection it uses.

        The fixture connection performs the one-time pool certification and the
        store connection performs recall. Configuring both immediately before the
        query avoids both the approximate-search omission seen at the cluster
        default and the large fixed beam that made small pages unnecessarily slow.
        """
        width = str(candidate_pool)
        with self.connection.cursor() as cursor:
            cursor.execute(SEARCH_BEAM_STATEMENT, (width,))
        with self.store.cursor() as cursor:
            cursor.execute(SEARCH_BEAM_STATEMENT, (width,))

    # -- placement -------------------------------------------------------

    def place(self, plan: CorpusPlan) -> PlacedCorpus:
        """Store one drawn corpus and return it with the identities it was given.

        The order is the order the references need: Clients, then their Sessions,
        then the Artifacts, then the Lineage_Edges a Derived_Artifact reaches its
        Session through, then the vectors, then the attribution. Every step is one
        batched statement per row shape rather than one transaction per Artifact,
        for the reason the module docstring gives.
        """
        # The pair of directions this placement ranks around, derived from an
        # identifier of its own rather than from anything drawn: a drawn seed
        # repeats across examples, and two placements sharing one direction is
        # exactly what the cap the horizon read is taken at exists to rule out.
        bearing = uuid4()
        query = unit_vector(f"query direction {bearing}")
        away = unit_vector(f"far direction {bearing}")
        clients = tuple(uuid4() for _ in range(plan.client_count))
        sessions = tuple(uuid4() for _ in range(plan.client_count))
        machines = tuple(
            f"machine-{index}-{identifier.hex[:8]}" for index, identifier in enumerate(clients)
        )
        started = tuple(MOMENT + SESSION_SPACING * index for index in range(len(clients)))

        self._send(
            INSERT_CLIENT,
            [
                (identifier, f"tenant-{identifier.hex[:12]}", "Tenant", "eu", RETENTION)
                for identifier in clients
            ],
        )
        self._send(
            INSERT_SESSION,
            [
                (
                    sessions[index],
                    clients[index],
                    AGENT_CLI,
                    machines[index],
                    started[index],
                    started[index] + SESSION_SPACING,
                    plan.outcomes[index].value,
                )
                for index in range(len(clients))
            ],
        )

        placed = tuple(
            PlacedArtifact(
                artifact_id=uuid4(),
                plan=artifact,
                owner_client=clients[artifact.owner],
                session_id=sessions[artifact.owner],
                machine_id=machines[artifact.owner],
                occurred_at=(
                    MOMENT + EVENT_SPACING * (position + 1)
                    if artifact.kind.is_event
                    else started[artifact.owner]
                ),
                outcome=plan.outcomes[artifact.owner],
                bound_clients=frozenset(clients[index] for index in artifact.bound_indices),
            )
            for position, artifact in enumerate(plan.artifacts)
        )

        self._place_events(placed)
        self._place_derived(placed)
        self._place_vectors(placed, query, away)
        self._place_bindings(placed)

        asking = sessions[plan.permitted[0]]
        presented = tuple(clients[index] for index in plan.permitted[1:])
        return PlacedCorpus(
            plan=plan,
            query=query,
            clients=clients,
            artifacts=placed,
            presented=presented,
            asking_session=asking,
        )

    def _place_events(self, placed: Sequence[PlacedArtifact]) -> None:
        """Append the Ledger rows, one chain per Session, digests and all.

        The sequence numbers and both digests come from the chain module's own
        functions, so each Session holds a chain that verifies and that a later
        append — the recall Event the engine writes — continues from.
        """
        rows: list[tuple[object, ...]] = []
        sequences: dict[UUID, int] = {}
        tips: dict[UUID, str] = {}
        for artifact in placed:
            if not artifact.plan.kind.is_event:
                continue
            session_id = artifact.session_id
            seq = sequences.get(session_id, FIRST_SEQUENCE - 1) + 1
            sequences[session_id] = seq
            previous = tips.get(session_id, GENESIS_PREDECESSOR)
            payload_text = canonical_payload_text({"tool": "a-tool"})
            content = sha256_hex(
                content_digest_input(
                    event_id=artifact.artifact_id,
                    session_id=session_id,
                    client_id=artifact.owner_client,
                    seq=seq,
                    category="tool_call",
                    occurred_at=artifact.occurred_at,
                    agent_cli=AGENT_CLI,
                    machine_id=artifact.machine_id,
                    parent_event_id=None,
                    payload_text=payload_text,
                    redacted=False,
                )
            )
            digest = chain_digest(previous, content)
            tips[session_id] = digest
            rows.append(
                (
                    artifact.artifact_id,
                    session_id,
                    artifact.owner_client,
                    seq,
                    "tool_call",
                    artifact.occurred_at,
                    artifact.occurred_at,
                    AGENT_CLI,
                    artifact.machine_id,
                    payload_text,
                    f"the earlier attempt recorded as {artifact.artifact_id}",
                    content,
                    previous,
                    digest,
                    "pending",
                    MOMENT + RETENTION,
                )
            )
        self._send(INSERT_LEDGER, rows)

    def _place_derived(self, placed: Sequence[PlacedArtifact]) -> None:
        """Write the Derived_Artifacts and the Lineage_Edge each reaches a Session by.

        The edge names the owning Session directly, which is the one level of
        lineage the recall statement's origin stage reads: a Derived_Artifact whose
        parents name neither a Session nor an Event answers no provenance criterion
        and is left out of the page rather than returned with nothing in it.
        """
        rows: list[tuple[object, ...]] = []
        edges: list[tuple[object, ...]] = []
        for artifact in placed:
            if artifact.plan.kind.is_event:
                continue
            body = f"the work distilled as {artifact.artifact_id}"
            rows.append(
                (
                    artifact.artifact_id,
                    artifact.plan.kind.value,
                    artifact.owner_client,
                    body,
                    hashlib.sha256(body.encode()).hexdigest(),
                    artifact.occurred_at,
                    artifact.occurred_at,
                    MOMENT + RETENTION,
                    artifact.plan.confidence,
                )
            )
            edges.append((uuid4(), artifact.artifact_id, artifact.session_id))
        self._send(INSERT_DERIVED, rows)
        self._send(INSERT_LINEAGE, edges)

    def _place_vectors(
        self,
        placed: Sequence[PlacedArtifact],
        query: tuple[float, ...],
        away: tuple[float, ...],
    ) -> None:
        """Write one Embedding per Artifact, at the distance its slot names."""
        rendered: dict[int, str] = {}
        rows: list[tuple[object, ...]] = []
        for artifact in placed:
            slot = artifact.plan.slot
            if slot not in rendered:
                rendered[slot] = vector_text(blended(query, away, _slot_weight(slot)))
            rows.append(
                (
                    uuid4(),
                    artifact.artifact_id,
                    artifact.artifact_kind.value,
                    artifact.owner_client,
                    PROVIDER,
                    MODEL,
                    EMBEDDING_DIMENSION,
                    rendered[slot],
                    MOMENT + RETENTION,
                )
            )
        self._send(INSERT_EMBEDDING, rows)

    def _place_bindings(self, placed: Sequence[PlacedArtifact]) -> None:
        """Attribute each Artifact to the Clients holding a current claim on it."""
        rows: list[tuple[object, ...]] = []
        for artifact in placed:
            owner = artifact.owner_client
            for client_id in sorted(artifact.bound_clients, key=str):
                rows.append(
                    (
                        uuid4(),
                        artifact.artifact_id,
                        artifact.artifact_kind.value,
                        client_id,
                        "scope" if client_id == owner else "marker",
                        MOMENT,
                    )
                )
        self._send(INSERT_BINDING, rows)

    # -- the reads the assertions make -----------------------------------

    def horizon(self, corpus: PlacedCorpus) -> Horizon:
        """How many Embeddings lie inside this example's cap, and how many are its own."""
        row = self._one(
            HORIZON_QUERY,
            (list(corpus.clients), vector_text(corpus.query), POOL_HORIZON),
        )
        return Horizon(within=int(str(row[0])), mine=int(str(row[1])))

    def pooled(self, corpus: PlacedCorpus) -> frozenset[UUID]:
        """Which Artifacts the candidate stage of the recall statement ranked.

        The membership is read rather than reasoned about, because the stage is
        served by the distributed vector index and an index search returns the
        nearest vectors it found rather than the nearest there are.
        """
        rendered = vector_text(corpus.query)
        rows = self._rows(POOL_QUERY, (rendered, rendered, corpus.plan.pool))
        return frozenset(_as_uuid(row[0]) for row in rows)

    def distance_of(self, corpus: PlacedCorpus, artifact_id: UUID) -> float:
        """The cosine distance one stored Embedding sits at from the query."""
        row = self._one(DISTANCE_QUERY, (vector_text(corpus.query), artifact_id))
        return float(str(row[0]))

    def current_bindings(self, artifact_ids: Sequence[UUID]) -> Mapping[UUID, frozenset[UUID]]:
        """The Clients holding a current claim on each of the named Artifacts."""
        held: dict[UUID, set[UUID]] = {}
        for row in self._rows(CURRENT_BINDINGS_QUERY, (list(artifact_ids),)):
            held.setdefault(_as_uuid(row[0]), set()).add(_as_uuid(row[1]))
        return {artifact_id: frozenset(clients) for artifact_id, clients in held.items()}

    def stored_provenance(
        self,
        events: Sequence[UUID],
        derived: Sequence[UUID],
    ) -> Mapping[UUID, StoredProvenance]:
        """The Session provenance of the named Artifacts, read from the stored rows.

        Two statements because the two kinds reach a Session by two routes: an
        Event names its own Session and carries its own machine and instant, while
        a Derived_Artifact reaches one through the Lineage_Graph and takes the
        Session's machine and start instant. Requirement 13.3 is asked of both, so
        both are read back rather than one standing in for the other.
        """
        found: dict[UUID, StoredProvenance] = {}
        reads = (
            (EVENT_PROVENANCE_QUERY, events),
            (DERIVED_PROVENANCE_QUERY, derived),
        )
        for statement, identifiers in reads:
            if not identifiers:
                continue
            for row in self._rows(statement, (list(identifiers),)):
                found[_as_uuid(row[0])] = StoredProvenance(
                    session_id=_as_uuid(row[1]),
                    machine_id=str(row[2]),
                    occurred_at=_as_instant(row[3]),
                    outcome=str(row[4]),
                )
        return found

    # -- sending ---------------------------------------------------------

    def _send(self, statement: str, rows: Sequence[tuple[object, ...]]) -> None:
        """Send one statement once per row, in batches inside one transaction each.

        One transaction per batch rather than one per row: the corpus of a single
        example runs to five hundred rows, and a transaction each would cost more
        than every assertion in both properties put together.

        A batch that the cluster asks to be retried is retried, because this cluster
        runs every transaction at the serializable isolation level and a large insert
        against an indexed vector column is one it does ask about. The schedule is
        the store's own rather than one of this module's invention, which is what
        makes the retries survive the refusal they are for: the ranges of a vector
        index under sustained batch insert refuse in bursts, and immediate attempts
        against a range that is still busy are spent inside the burst rather than
        after it. The store's policy backs off exponentially with jitter between
        attempts, and the failure surfaces once the attempts it permits are spent.
        """
        for begin in range(0, len(rows), BATCH_ROWS):
            batch = rows[begin : begin + BATCH_ROWS]
            if batch:
                self._send_batch(statement, batch)

    def _send_batch(self, statement: str, batch: Sequence[tuple[object, ...]]) -> None:
        """Send one batch, retrying a refused transaction on the store's own schedule."""
        for retry in range(DEFAULT_RETRY_POLICY.attempts):
            try:
                with self.connection.transaction(), self.connection.cursor() as cursor:
                    cursor.executemany(statement, batch)
            except Exception as error:
                spent = retry + 1 == DEFAULT_RETRY_POLICY.attempts
                if spent or not is_serialization_failure(error):
                    raise
                DEFAULT_SLEEP(DEFAULT_RETRY_POLICY.delay(retry))
            else:
                return

    def _rows(self, statement: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        """Every row one parameterised read returns, on the fixture's connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            return list(cursor.fetchall())

    def _one(self, statement: str, params: tuple[object, ...]) -> tuple[object, ...]:
        """The single row one parameterised read returns."""
        rows = self._rows(statement, params)
        assert len(rows) == 1, f"the read returned {len(rows)} row(s) where one was expected"
        return rows[0]


def _as_uuid(value: object) -> UUID:
    """Narrow a stored identifier, refusing anything else."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _as_instant(value: object) -> datetime:
    """Narrow a stored instant, refusing anything else."""
    assert isinstance(value, datetime), "a stored timestamp column returned no instant"
    return value


def build_corpus_bank(cluster: Cluster) -> CorpusBank:
    """Place and certify the reusable bank once in one property's module schema.

    Certification happens only after all eight entries are present, so it proves
    each minimum candidate pool still contains its complete corpus in the final
    bank rather than proving completeness before later placements can compete.
    """
    corpora = tuple(cluster.place(plan) for plan in BANK_PLANS)
    for corpus in corpora:
        require_uncrowded_pool(cluster, corpus)
    return CorpusBank(cluster=cluster, corpora=corpora)


def answered_page(cluster: Cluster, corpus: PlacedCorpus) -> tuple[Recalled, ...]:
    """The page one placed corpus answers, through the engine a caller would use.

    The retrieval seam is filled with a recorder rather than left at its default,
    because the default is one transaction per returned Learned_Procedure and that
    write has its own coverage in the integration suite. Nothing else about the path
    is stubbed: the tenancy comes from the stored Session row, and the permitted set
    the request presents omits that Session's own Client, so the union of the two is
    what the query actually runs with.
    """
    cluster.configure_search(corpus.plan.pool)
    engine = RecallEngine(
        cluster.store,
        StubQueryEmbedder(corpus.query),
        recall_floor=RECALL_FLOOR,
        retrievals=RecordedRetrievals(seen=[]).record,
    )
    return engine.recall(
        QUERY_TEXT,
        corpus.plan.limit,
        permitted=corpus.presented,
        session_id=corpus.asking_session,
    )


def require_uncrowded_pool(cluster: Cluster, corpus: PlacedCorpus) -> Horizon:
    """Refuse to assert about a page whose candidate pool did not hold the corpus.

    The recall statement ranks a candidate pool by the ordering expression alone and
    admits the permitted rows from that pool, so a page is the nearest k the caller
    may see *within the pool* rather than within the corpus. Every clause of both
    properties is stated over the corpus, so each example has to establish that its
    pool held the whole corpus rather than assume it.

    Four readings, and the last is the one that settles it. Every Artifact of the
    drawn corpus is stored and inside the example's own cap; nothing else in the
    table is inside that cap; the rows inside it number fewer than the pool the
    drawn bound produces; and the pool, read back by the candidate stage's own text,
    holds every Artifact the corpus placed.

    The fourth is not implied by the first three, and the difference is the whole
    reason it is here. The candidate stage is served by the distributed vector
    index, and an index search visits a bounded number of partitions and returns
    the nearest vectors it found among them: a pool wider than everything inside the
    cap can still omit rows that are inside it, the nearest rows included. Inferring
    pool membership from cap occupancy would therefore be inferring an exact search
    from a bound, and an example whose nearest rows the search had dropped would go
    on to fail a clause about tenancy or about the tie-break for a reason that is
    neither. The beam width the fixture sets is what makes the reading hold; this
    read is what checks that it did.
    """
    plan = corpus.plan
    cluster.configure_search(plan.pool)
    horizon = cluster.horizon(corpus)
    assert horizon.mine == plan.size, (
        f"{horizon.mine} of this corpus's {plan.size} embedding(s) lie inside the cap, so "
        "the corpus was not placed where the properties take it to be"
    )
    assert horizon.within == horizon.mine, (
        f"{horizon.within - horizon.mine} embedding(s) from another example lie inside this "
        "example's cap, so its candidate pool could hold rows it never placed"
    )
    assert plan.pool > horizon.within, (
        f"the candidate pool of {plan.pool} is no wider than the {horizon.within} row(s) "
        "inside the cap, so the page would be a horizon into the corpus rather than the "
        "corpus"
    )
    pooled = cluster.pooled(corpus)
    dropped = tuple(
        position
        for position, placed in enumerate(corpus.artifacts)
        if placed.artifact_id not in pooled
    )
    assert not dropped, (
        f"the candidate pool of {plan.pool} row(s) left out {len(dropped)} of this corpus's "
        f"{plan.size} embedding(s), the nearest of them at slot {dropped[0]}, so the index "
        "search behind the pool returned the nearest vectors it found rather than the "
        f"nearest there are; the per-query beam of {plan.pool} was not enough for this "
        "reusable corpus"
    )
    return horizon


def size_band(size: int) -> str:
    """Which part of the corpus range an example drew, for the coverage record."""
    if size <= 100:
        return "50-100"
    if size <= 250:
        return "101-250"
    if size <= 400:
        return "251-400"
    return "401-500"


def count_band(count: int) -> str:
    """How often a shape occurred in one example, for the coverage record."""
    if count == 0:
        return "none"
    if count == 1:
        return "1"
    if count <= 4:
        return "2-4"
    return "5+"


def open_cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store whose connections see that schema.

    Every migration is applied because the vector index, the attribution history
    the tenancy admission reads, and the confidence column the tie-break orders by
    each arrive with a different one.

    Module scope is what keeps placement paid once. The eight reusable corpora are
    isolated from the other property by this module's fresh schema and from one
    another by independently derived query directions.

    Search width is deliberately absent here. `answered_page()` and the one-time
    bank certification apply `candidate_pool_for(k)` immediately before each query
    to both the fixture and store connections, so no global high beam leaks across
    examples.
    """
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
