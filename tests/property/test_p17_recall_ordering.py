"""Property 17: the page is one ordering, no longer than asked for, and holds no repeat.

**Validates: Requirements 13.1, 10.7**

This property drives a real cluster, and the reason is that the ordering is the
cluster's. The distances are computed by the vector operator, the ranking is a window
function over a candidate pool the distributed vector index produced, and the
truncation is a bound inside the same statement. A page assembled in Python and then
sorted in Python would be a demonstration that Python sorts. The module is therefore
marked to gate on a reachable instance and is deselected from the credential-free
workflow. It carries `integration` alone and not `instance` beside it: the two name
one prerequisite, and the suite marker already implies it, whereas `instance` is for
a test whose suite marker does not.

It draws the same corpora the tenancy property does, from the generator both share,
because the two claims are about one page and a page that satisfied one over a corpus
and the other over a different corpus would leave the interesting case — the one
where filtering and ordering interact — untested. What this module adds to that
corpus is nothing: the ties are already in it, planted, for the reason below.

Four clauses, and the third is why the corpus is built the way it is.

**The distances are non-decreasing.** Over the whole page, whatever mixture of kinds
and tenants filled it.

**The page holds at most k results and no Artifact twice.** The bound is the caller's
own. The repetition clause is not idle: the tenancy admission is a semi-join over
attribution rows, and an Artifact bound to two permitted Clients at once is what a
join in its place would return twice. Every multi-permission bank entry plants one,
and the property asserts that entry reached the page before using it to test the
no-repetition claim.

**At equal distance the more-trusted Learned_Procedure comes first, and the Artifact
identifier settles what the standing does not.** Exact ties do not arise from drawing
distances at random, so they are constructed: every reusable corpus places all three
tie shapes on rungs of its distance ladder, giving each group one exact distance.
Distinct standings leave the order to confidence, identical standings leave it to
the identifier, and a procedure beside an Artifact with no standing exercises nulls
last. The expected order is computed independently of the statement under test.

**Every result carries an identifier, a Client identifier, and a distance in the
range a cosine distance occupies**, which is Requirement 10.7 asked of the recall
projection rather than of the neighbour query alone.

Two things this property refuses to assume. Setup certifies after the entire reusable
bank is placed that each corpus alone occupies its cap and that the approximate
candidate stage contains all of it. Each example then selects a certified corpus,
varies k, and applies that query's own candidate-pool beam. The property also refuses
to assume a tie reached the page: every example requires at least one planted group
to arrive whole.

The example budget remains 100 with no per-example deadline. The module fixture pays
the placement and completeness checks once for eight corpora; each example performs
one recall and assertions over the selected corpus.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from types import ModuleType
from typing import Final
from uuid import UUID

import pytest
from hypothesis import event, given, settings
from tests.property.recall_corpora import (
    CorpusBank,
    CorpusCase,
    DriverConnection,
    PlacedCorpus,
    TieGroup,
    answered_page,
    build_corpus_bank,
    corpora_with_permissions,
    count_band,
    open_cluster,
    size_band,
    tie_order_key,
)

from molt.recall import Recalled
from molt.store.embeddings import COSINE_CEILING, COSINE_FLOOR

pytestmark = pytest.mark.integration

# How many examples the property runs. The reasoning behind the budget is in the
# module docstring.
MAX_EXAMPLES: Final[int] = 100


@pytest.fixture(scope="module")
def bank(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[CorpusBank]:
    """Place and certify the isolated reusable corpus bank once for this module."""
    for cluster in open_cluster(fresh_schema, database_driver, local_instance_dsn):
        yield build_corpus_bank(cluster)


def expected_group_order(corpus: PlacedCorpus, group: TieGroup) -> tuple[UUID, ...]:
    """The order the Artifacts of one tie group ought to appear in.

    Descending standing with an absent standing last, then the Artifact identifier
    ascending, computed from the drawn group rather than from the page it is compared
    against.
    """
    members = [corpus.at(position) for position in group.positions]
    ordered = sorted(
        members,
        key=lambda placed: tie_order_key(placed.plan.confidence, placed.artifact_id),
    )
    return tuple(placed.artifact_id for placed in ordered)


def observed_group_order(
    group: TieGroup,
    corpus: PlacedCorpus,
    positions: Mapping[UUID, int],
) -> tuple[UUID, ...]:
    """The members of one tie group that reached the page, in the order they did."""
    present = [
        corpus.at(position).artifact_id
        for position in group.positions
        if corpus.at(position).artifact_id in positions
    ]
    return tuple(sorted(present, key=lambda found: positions[found]))


def distances_of(
    group: TieGroup,
    corpus: PlacedCorpus,
    by_id: Mapping[UUID, Recalled],
) -> tuple[float, ...]:
    """The distances the page reported for the members of one tie group."""
    return tuple(
        by_id[corpus.at(position).artifact_id].distance
        for position in group.positions
        if corpus.at(position).artifact_id in by_id
    )


def group_report(shapes: Sequence[TieGroup], whole: int) -> str:
    """What the tie groups of one example contributed, for the coverage record."""
    return f"{whole} of {len(shapes)} tie group(s) arrived whole"


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 17: For any query text against any corpus, the returned
# cosine distances are non-decreasing, the result count is at most k, and no
# Artifact appears twice.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(case=corpora_with_permissions())
def test_the_page_is_ordered_bounded_and_free_of_repetition(
    bank: CorpusBank, case: CorpusCase
) -> None:
    corpus = bank.select(case)
    cluster = bank.cluster
    plan = corpus.plan

    event(f"corpus={size_band(plan.size)}")
    event(f"clients={plan.client_count}")
    event(f"permitted={len(plan.permitted)} of {plan.client_count}")
    event(f"permission profile={case.permission_profile}")
    event(f"query direction={case.band}-{case.permission_profile}")
    event(f"asked={plan.limit}")
    event(f"tie groups={len(plan.groups)}")
    event(f"tied artifacts={count_band(plan.tie_members)}")
    for group in plan.groups:
        event(f"tie shape={group.shape.value}")

    results = answered_page(cluster, corpus)

    assert results, (
        "the page came back empty although the corpus holds artifacts the caller may see"
    )

    # Requirement 13.1: ascending cosine distance, over the whole page.
    distances = [result.distance for result in results]
    assert distances == sorted(distances), (
        f"the page reported the distances {distances}, which are not non-decreasing"
    )

    # The caller's own bound, and one row per Artifact. The second clause is where
    # the doubly bound Artifact matters: a tenancy admission written as a join
    # rather than as a semi-join would return it once per permitted binding.
    assert len(results) <= plan.limit, (
        f"the page holds {len(results)} result(s) where at most {plan.limit} were asked for"
    )
    returned = [result.artifact_id for result in results]
    assert len(returned) == len(set(returned)), "the page returned an artifact more than once"
    has_double_permission = len(plan.permitted) > 1
    if has_double_permission:
        doubled = corpus.at(plan.doubly_bound)
        assert len(doubled.bound_clients & corpus.permitted_clients) == 2
        assert doubled.artifact_id in set(returned), (
            "the artifact with two permitted current bindings did not reach the page, "
            "so this example could not distinguish a semi-join from a duplicate-producing join"
        )
    event(f"double-permission case={has_double_permission}")

    # Requirement 10.7: each result carries the identifier, the Client identifier,
    # and a distance inside the range a cosine distance occupies.
    placed = corpus.by_id
    for result in results:
        assert result.artifact_id in placed
        assert result.client_id == placed[result.artifact_id].owner_client
        assert COSINE_FLOOR <= result.distance <= COSINE_CEILING, (
            f"the artifact {result.artifact_id} was returned at the distance "
            f"{result.distance}, outside the range a cosine distance occupies"
        )

    # The tie-break, asserted where an exact tie was planted. A group that arrived
    # whole is one whose whole order the page decided, and its members are asserted
    # to sit at one distance as well as in one order: an order that held because the
    # distances differed would say nothing about the tie-break.
    positions = {result.artifact_id: index for index, result in enumerate(results)}
    by_id = {result.artifact_id: result for result in results}
    whole = 0
    for group in plan.groups:
        observed = observed_group_order(group, corpus, positions)
        if len(observed) < 2:
            continue
        reported = distances_of(group, corpus, by_id)
        assert len(set(reported)) == 1, (
            f"the {group.size} artifacts placed on one distance were reported at "
            f"{sorted(set(reported))}, so this group is no tie"
        )
        expected = tuple(
            found for found in expected_group_order(corpus, group) if found in positions
        )
        assert observed == expected, (
            f"the {group.shape.value} tie group arrived in the order {observed} where the "
            f"standing and the identifier make it {expected}"
        )
        if len(observed) == group.size:
            whole += 1

    assert whole, (
        f"no planted tie group reached the page whole out of {len(plan.groups)}, so this "
        "example asserted nothing about the tie-break"
    )

    event(group_report(plan.groups, whole))
    event(f"results={count_band(len(results))}")
    event(f"page filled to the bound={len(results) == plan.limit}")
