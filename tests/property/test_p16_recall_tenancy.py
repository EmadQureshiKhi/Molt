"""Property 16: a recall result is one the caller may see, carrying the stored provenance.

**Validates: Requirements 13.2, 13.3, 13.4, 9.8**

This property drives a real cluster, and every clause of it says why. The tenancy
admission is a semi-join over unsuperseded Attribution_Versions evaluated inside the
recall statement; the ranking it admits from is a candidate pool the distributed
vector index produced; the provenance each result carries is assembled by that same
statement from a Ledger row or from one level of the Lineage_Graph. None of the
three exists in this process, so a page composed in Python from a dictionary of
stubbed rows would be evidence about the stub. The module is therefore marked to
gate on a reachable instance and is deselected from the credential-free workflow. It
carries `integration` alone and not `instance` beside it: the two name one
prerequisite, and the suite marker already implies it, whereas `instance` is for a
test whose suite marker does not.

The claim has two halves, and the corpus is arranged so neither can hold by accident.

**Every returned Artifact carries a current binding to a Client the caller may
see.** Asserted against the attribution rows rather than against the Artifact's
owner, because those are different claims and only the weaker one is about
ownership. Each corpus therefore plants an Artifact whose owning Client is *not*
permitted and which carries a current binding to one that is, near the front of the
ordering: it must be returned, and its own Client identifier on the answer is one
the caller was never entitled to. A filter written over ownership would drop it, and
a filter written as a join rather than a semi-join would return the doubly-bound
Artifact twice.

**The Session identifier, the machine identifier, the instant, and the outcome on
each result are the ones the stored rows hold.** Read back from the Ledger row and
its Session for an Event, and from the Session the Lineage_Graph reaches for a
Derived_Artifact, because Requirement 13.3 is asked of both kinds and the two reach a
Session by different routes. Every Session of a corpus carries a machine identifier
of its own and an instant of its own, so a result carrying another Artifact's
provenance is visible rather than plausible.

Two things this property refuses to assume.

**That the pool held the corpus.** The recall statement ranks a candidate pool by the
ordering expression alone and admits the permitted rows from that pool, so a page is
the nearest k the caller may see within the pool rather than within the corpus. This
module places eight reusable corpora once, each around an independent query direction.
After the complete bank is present, setup certifies each corpus by two readings: the
cap read proves no other bank entry can crowd its pool, and the candidate-stage read
proves the approximate vector index returned every row of that corpus. Each property
example then selects only an already-certified corpus and varies its permitted profile
and k, with the search beam derived from that query's candidate pool.

**That an unpermitted Client held nothing worth returning.** The nearest slot of
every corpus holds an Artifact no permitted Client is bound to, and its distance is
read back from the cluster and asserted to be strictly below the nearest distance the
page returned. So the page is not merely permitted: something nearer was there and
was left out.

The example budget remains 100 with no per-example deadline. The module fixture pays
the placement and pool-certification cost once for two permission profiles in each of
four corpus-size bands across 2 to 5 Clients. An example performs one recall and the
storage reads needed by the tenancy and provenance assertions; it does not rebuild
50 to 500 Artifacts.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
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
    answered_page,
    build_corpus_bank,
    corpora_with_permissions,
    count_band,
    open_cluster,
    size_band,
)

from molt.models.session import SessionOutcome

pytestmark = pytest.mark.integration

# How many examples the property runs. The reasoning behind the budget is in the
# module docstring.
MAX_EXAMPLES: Final[int] = 100

# The outcome classifications Requirement 13.2 admits, which every result's outcome
# is asserted to be one of.
TERMINAL_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        SessionOutcome.SUCCEEDED.value,
        SessionOutcome.FAILED.value,
        SessionOutcome.ABANDONED.value,
    }
)


@pytest.fixture(scope="module")
def bank(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[CorpusBank]:
    """Place and certify the isolated reusable corpus bank once for this module."""
    for cluster in open_cluster(fresh_schema, database_driver, local_instance_dsn):
        yield build_corpus_bank(cluster)


def kinds_of(corpus: PlacedCorpus, artifact_ids: Sequence[UUID]) -> tuple[list[UUID], list[UUID]]:
    """Split returned identifiers into the Events and the Derived_Artifacts."""
    placed = corpus.by_id
    events = [found for found in artifact_ids if placed[found].plan.kind.is_event]
    derived = [found for found in artifact_ids if not placed[found].plan.kind.is_event]
    return events, derived


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 16: For any corpus and any permitted Client set, every
# returned recall result carries at least one Client_Binding within the permitted
# set, and every result carries the originating Session identifier, machine
# identifier, timestamp, and outcome classification matching the stored Session
# row.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(case=corpora_with_permissions())
def test_every_result_is_permitted_and_carries_the_provenance_storage_holds(
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

    results = answered_page(cluster, corpus)

    assert results, (
        "the page came back empty although the corpus holds artifacts the caller may see"
    )
    returned = [result.artifact_id for result in results]
    placed = corpus.by_id
    assert set(returned) <= set(placed), "the page named an artifact this corpus never placed"

    # Requirements 13.4 and 9.8: admission is by a current binding to a permitted
    # Client, read from the attribution rows rather than from the answer.
    held = cluster.current_bindings(returned)
    permitted = corpus.permitted_clients
    for result in results:
        claims = held.get(result.artifact_id, frozenset())
        assert claims & permitted, (
            f"the artifact {result.artifact_id} was returned holding current bindings to "
            f"{len(claims)} client(s), none of them permitted"
        )

    # Requirements 13.2 and 13.3: the provenance is the stored provenance, read back
    # by the route the Artifact's own kind reaches a Session through.
    events, derived = kinds_of(corpus, returned)
    stored = cluster.stored_provenance(events, derived)
    for result in results:
        provenance = stored.get(result.artifact_id)
        assert provenance is not None, (
            f"the artifact {result.artifact_id} was returned although no stored row "
            "resolves its originating session"
        )
        assert result.session_id == provenance.session_id
        assert result.machine_id == provenance.machine_id
        assert result.occurred_at == provenance.occurred_at
        assert result.outcome == provenance.outcome
        assert result.outcome in TERMINAL_OUTCOMES, (
            f"the artifact {result.artifact_id} was returned with the outcome "
            f"{result.outcome}, which is no terminal classification"
        )

    # An Artifact admitted by attribution rather than by ownership really is on the
    # page, and the Client identifier it carries is one the caller was never
    # entitled to, so nothing above passed by testing ownership twice.
    admitted = corpus.at(plan.admitted_by_binding)
    assert admitted.artifact_id in set(returned), (
        f"the artifact {admitted.artifact_id}, owned by a client the caller may not see "
        "and bound to one it may, is absent from the page"
    )
    assert admitted.owner_client not in permitted
    carried = next(result for result in results if result.artifact_id == admitted.artifact_id)
    assert carried.client_id == admitted.owner_client
    assert carried.client_id not in permitted

    # The nearest Artifact of the corpus belongs to nobody the caller may see, and
    # it is nearer than everything the page returned, so the filter really excluded
    # something rather than there being nothing to exclude.
    decoy = corpus.at(plan.decoy)
    assert not decoy.bound_clients & permitted
    assert decoy.artifact_id not in set(returned)
    nearest = min(result.distance for result in results)
    hidden = cluster.distance_of(corpus, decoy.artifact_id)
    assert hidden < nearest, (
        f"the unpermitted near neighbour sits at {hidden} where the nearest returned "
        f"result sits at {nearest}, so this example had no unpermitted neighbour ahead "
        "of the page"
    )

    unowned = sum(1 for result in results if result.client_id not in permitted)
    doubled = corpus.at(plan.doubly_bound)
    has_double_permission = len(plan.permitted) > 1
    if has_double_permission:
        assert len(doubled.bound_clients & permitted) == 2
        assert doubled.artifact_id in set(returned), (
            "the artifact with two permitted current bindings did not reach the page, "
            "so this example could not distinguish a semi-join from a duplicate-producing join"
        )
    event(f"results={count_band(len(results))}")
    event(f"results admitted by attribution alone={count_band(unowned)}")
    event(f"events on the page={count_band(len(events))}")
    event(f"derived artifacts on the page={count_band(len(derived))}")
    event(f"double-permission case={has_double_permission}")
    event(f"doubly bound artifact returned={doubled.artifact_id in set(returned)}")
    event(f"page filled to the bound={len(results) == plan.limit}")
