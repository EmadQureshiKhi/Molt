"""Property 2: an erasure leaves everything it was not asked about exactly as it was.

**Validates: Requirements 18.2, 18.4, 18.8**

Property 1 asks whether an erasure removed enough. This one asks the opposite
question, which is the harder half of the same claim: whether it removed too much.
A sweep that widens by lineage, a residue pass that reaches across the fleet, and a
batched delete that binds arrays of identifiers are all places where one Artifact
too many could be carried into the deletion set. A run that erased the tenant
completely and took a neighbouring tenant's summary with it would satisfy every
completeness assertion and still be wrong.

Five decisions shape what is generated and what is asserted.

**The graph is drawn by the generator Property 1 uses.** `memory_graphs()` is
imported from the module that already owns it rather than restated here, so the two
properties are claims about one generated input space. What the drawn counts size is
the corpus one example places: how many Derived_Artifacts stand, how many of them
carry a current claim for the erased tenant, and how many Events the retained
tenant's Session holds. Every Artifact past the bound count is bound to the retained
tenant alone, and those are the rows this property is about.

**Preservation is asserted on the content digest, the body, and the revision
together.** A digest alone would admit a row whose body was replaced by content
digesting the same way; a body alone would admit a row rewritten to itself and
counted as a revision. Reading all three from the stored row before and after is
what makes *unchanged* mean unchanged rather than merely *still present*.

**One untouched candidate is placed deliberately rather than hoped for.** A lineage
edge runs from an Artifact the erased tenant holds alone to an Artifact the retained
tenant holds alone, so the descendant arm of the sweep pulls a row into the candidate
set that the decision table must then leave alone. Without it the second clause of
this property would be vacuous on most examples: an unbound Artifact that never
became a candidate is preserved by never having been considered, which is not the
claim. With it, every example carries at least one candidate the run examined, chose
not to touch, and had to account for.

**The accounting is read from the Disposition table, not from the returned counts.**
Requirement 18.8 is about evidence: a candidate left alone carries a retained
Disposition naming why. So the assertion reads the rows the run wrote, and requires
the reason to be a non-empty value rather than merely present, because an empty
reason is an unexplained retention.

**Nothing here supplies a credential.** The backup statement, the control-plane
command, the Text_Provider, and the Embedding_Provider are all the stubs the
integration module already defines, imported rather than rewritten, so the run
performs its real transactions against a real schema with every outside call
answered in process.

The example budget is 25 with no per-example deadline. Every example performs a
whole erasure run against a live schema, so the cost per example is a transaction
sequence rather than a function call, and a deadline would assert something about
the machine rather than about the code.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any, Final
from uuid import UUID

import pytest
from hypothesis import event, given, settings
from tests.integration.test_erasure_engine import (
    ERASED_MARKER,
    RETAINED_MARKER,
    RUN_OWNER,
    SEARCH_PATH_STATEMENT,
    Cluster,
    Fixture,
    blended_body,
    request_for,
    seams,
)
from tests.property.test_p27_backup_path_agreement import MemoryGraph, memory_graphs

from molt.config.resolve import Configuration
from molt.erase.disposition import BINDING_ABSENT_REASON, DispositionKind
from molt.erase.engine import EngineSeams, RunStatus, run_erasure
from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The example budget. Modest on purpose: an example is a whole run against a
# schema, not a call.
MAX_EXAMPLES: Final[int] = 25

# The statement the module's own schema name is read from, so the connections the
# store opens see the same tables the fixture created.
CURRENT_SCHEMA_STATEMENT: Final[str] = "SELECT current_schema()"

# The lineage edge that makes the descendant arm of the sweep reach an Artifact the
# erased tenant holds no claim on. Placed here rather than through the store's own
# writer because what is wanted is the edge itself, not the cycle guard around it.
INSERT_EDGE_STATEMENT: Final[str] = (
    "INSERT INTO lineage_edge (child_id, parent_id, parent_kind, derivation_method) "
    "VALUES (%s, %s, %s, %s)"
)

# The stored shape of every Derived_Artifact an example placed that carries no
# current claim for the erased tenant. Three columns rather than one, because
# *unchanged* has to mean the body, its digest, and its revision together.
SELECT_UNBOUND_ARTIFACTS: Final[str] = (
    "SELECT d.id, d.content_digest, d.body, d.revision, d.redacted_at IS NULL "
    "FROM derived_artifact AS d WHERE d.id = ANY (%s::UUID[]) AND NOT EXISTS ("
    "SELECT 1 FROM client_binding AS b WHERE b.artifact_id = d.id AND b.client_id = %s "
    "AND b.superseded_by IS NULL) ORDER BY d.id"
)

# The same reading for the Events the retained tenant's Session holds.
SELECT_UNBOUND_EVENTS: Final[str] = (
    "SELECT e.id, e.content_digest, coalesce(e.text_body, '') FROM ledger AS e "
    "WHERE e.id = ANY (%s::UUID[]) AND NOT EXISTS ("
    "SELECT 1 FROM client_binding AS b WHERE b.artifact_id = e.id AND b.client_id = %s "
    "AND b.superseded_by IS NULL) ORDER BY e.id"
)

# What the sweep and the residue pass selected, and what the decision table then
# recorded for each of them.
SELECT_RUN_CANDIDATES: Final[str] = (
    "SELECT artifact_id, selection_reason FROM erasure_candidate WHERE run_id = %s"
)
SELECT_RUN_DISPOSITIONS: Final[str] = (
    "SELECT artifact_id, disposition, reason, post_digest FROM disposition WHERE run_id = %s"
)

# The values the placed rows carry. The retained lines name the retained tenant's
# marker alone, so no rewrite validation has any reason to touch them.
KEPT_LINE: Final[str] = "the deployment pipeline reads the covering index"
SOLE_LINE: Final[str] = "the invoicing exporter reconciles against the ledger"
DESCENDANT_LINE: Final[str] = "the distilled summary the parent artifact produced"
PARENT_KIND: Final[str] = ArtifactKind.DERIVED_ARTIFACT.value

# The first sequence number a Session's Ledger admits, which the schema holds above
# zero, so a graph drawing no rows still numbers its first Event validly.
FIRST_SEQUENCE_NUMBER: Final[int] = 1
DERIVATION_METHOD: Final[str] = "distilled"

# The residue search this property runs under. Narrower than the integration
# module's, because the schema is shared across examples and the neighbour search is
# the one phase whose cost grows with what every earlier example left behind.
# Every number an example turns on, stated rather than defaulted, so a passing
# example passes because of the values it names. The two residue numbers are the
# only ones that differ from the integration module's, and they differ downward.
NARROWED_ENVIRON: Final[dict[str, str]] = {
    "MOLT_ERASURE_BATCH_SIZE": "2",
    "MOLT_LEASE_INTERVAL_SECONDS": "60",
    "MOLT_AUTO_INCLUDE_THRESHOLD": "0.20",
    "MOLT_REVIEW_THRESHOLD": "0.45",
    "MOLT_RESIDUE_QUERY_LIMIT": "2",
    "MOLT_RESIDUE_TOP_K": "4",
    "MOLT_ADJUDICATION_PREFIX_BUDGET_BYTES": "4096",
    "MOLT_MINIMUM_CACHEABLE_PREFIX_BYTES": "16384",
    "MOLT_REWRITE_LENGTH_RATIO_MIN": "0.25",
    "MOLT_RETENTION_DEFAULT_INTERVAL": "90 days",
    "MOLT_LEASE_OWNER": RUN_OWNER,
    "MOLT_PROCEDURE_RECALL_FLOOR": "0.15",
}

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


@dataclass(frozen=True, slots=True)
class Corpus:
    """What one example placed, split by whether the erased tenant claims it.

    Attributes:
        bound: Artifacts carrying a current claim for the erased tenant, which the
            run is entitled to delete or rewrite.
        unbound: Artifacts carrying no such claim, which is the set this property
            is about.
        events: Events of the retained tenant's Session.
        descendant: The unbound Artifact the lineage arm of the sweep reaches, which
            is the candidate every example requires the run to account for.
    """

    bound: tuple[UUID, ...]
    unbound: tuple[UUID, ...]
    events: tuple[UUID, ...]
    descendant: UUID


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """One Derived_Artifact as the preservation reading reports it."""

    artifact_id: UUID
    content_digest: str
    body: str
    revision: int
    unredacted: bool


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One Event as the preservation reading reports it."""

    artifact_id: UUID
    content_digest: str
    text: str


def _as_uuid(value: object) -> UUID:
    """Narrow a stored identifier, refusing nothing and inventing nothing."""
    return value if isinstance(value, UUID) else UUID(str(value))


def artifact_state(
    cluster: Cluster, identifiers: tuple[UUID, ...], erased_id: UUID
) -> dict[UUID, StoredArtifact]:
    """Read every placed Artifact that carries no current claim for the erased tenant."""
    return {
        _as_uuid(row[0]): StoredArtifact(
            artifact_id=_as_uuid(row[0]),
            content_digest=str(row[1]),
            body=str(row[2]),
            revision=int(row[3]),
            unredacted=bool(row[4]),
        )
        for row in cluster.rows(SELECT_UNBOUND_ARTIFACTS, (list(identifiers), erased_id))
    }


def event_state(
    cluster: Cluster, identifiers: tuple[UUID, ...], erased_id: UUID
) -> dict[UUID, StoredEvent]:
    """Read every placed Event that carries no current claim for the erased tenant."""
    return {
        _as_uuid(row[0]): StoredEvent(
            artifact_id=_as_uuid(row[0]),
            content_digest=str(row[1]),
            text=str(row[2]),
        )
        for row in cluster.rows(SELECT_UNBOUND_EVENTS, (list(identifiers), erased_id))
    }


def place_corpus(cluster: Cluster, fixture: Fixture, graph: MemoryGraph) -> Corpus:
    """Place the corpus the drawn graph sizes, plus the one candidate every example needs.

    The drawn counts are read as sizes rather than as contents: how many Artifacts
    stand, how many of them the erased tenant claims, and how many Events the
    retained tenant's Session holds. Every Artifact past the claimed count belongs
    to the retained tenant alone.
    """
    sizes = dict(graph.rows)
    total = sizes["derived_artifact"]
    claimed = min(sizes["client_binding"], total)
    bound: list[UUID] = []
    unbound: list[UUID] = []

    for index in range(total):
        if index < claimed:
            blended = index % 2 == 1
            body = blended_body() if blended else f"{ERASED_MARKER} {SOLE_LINE} {index}"
            owner = fixture.retained_id if blended else fixture.erased_id
            identifier = cluster.artifact(owner, body)
            cluster.bind(identifier, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
            if blended:
                cluster.bind(identifier, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
            bound.append(identifier)
        else:
            identifier = cluster.artifact(
                fixture.retained_id, f"{RETAINED_MARKER} {KEPT_LINE} {index}"
            )
            cluster.bind(identifier, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
            unbound.append(identifier)

    # The deliberate untouched candidate: a parent the erased tenant holds alone,
    # and a child the retained tenant holds alone. The sweep reaches the child by
    # descent, so the decision table has to account for a row it must not touch.
    parent_id = cluster.artifact(fixture.erased_id, f"{ERASED_MARKER} {SOLE_LINE} parent")
    cluster.bind(parent_id, ArtifactKind.DERIVED_ARTIFACT, fixture.erased_id)
    bound.append(parent_id)
    descendant = cluster.artifact(fixture.retained_id, f"{RETAINED_MARKER} {DESCENDANT_LINE}")
    cluster.bind(descendant, ArtifactKind.DERIVED_ARTIFACT, fixture.retained_id)
    unbound.append(descendant)
    cluster.send(INSERT_EDGE_STATEMENT, (descendant, parent_id, PARENT_KIND, DERIVATION_METHOD))

    events = tuple(
        cluster.event(
            fixture.session_id,
            fixture.retained_id,
            index + FIRST_SEQUENCE_NUMBER,
            f"{RETAINED_MARKER} {KEPT_LINE} {index}",
        )
        for index in range(sizes["ledger"])
    )
    return Corpus(
        bound=tuple(bound),
        unbound=tuple(unbound),
        events=events,
        descendant=descendant,
    )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store whose connections see that schema.

    Every migration is applied because the lease table, the fencing columns, and the
    Disposition evidence columns all arrive in later generations than the tables a
    run acts on. Module scope keeps that cost paid once: examples are isolated from
    each other by a tenant pair of their own rather than by a schema of their own,
    and rows an earlier example left standing are content a later run must also
    preserve.
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

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


def narrowed_seams(fixture: Fixture) -> EngineSeams:
    """The integration module's seams with the residue search held to a few queries.

    The residue pass is the one phase whose cost grows with everything every earlier
    example left standing in the shared schema, and this property is not a claim
    about how wide that search is. Narrowing it keeps an example a bounded amount of
    work while leaving the phase itself in the run, so residue candidates the erased
    tenant holds no claim on are still selected and still have to be accounted for.
    """
    return replace(
        seams(fixture),
        configuration=Configuration(environ=NARROWED_ENVIRON, file_values={}),
    )


def count_band(count: int) -> str:
    """How many of something an example carried, for the coverage record."""
    if count == 0:
        return "none"
    if count == 1:
        return "1"
    if count <= 4:
        return "2-4"
    return "5+"


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 2: For any memory graph as in Property 1, for every Artifact
# carrying no Client_Binding for the erased Client C before the run, the content digest
# after the run equals the content digest before the run, and every candidate the run
# left unchanged carries a `retained` Disposition with a non-empty reason.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(graph=memory_graphs())
def test_an_erasure_preserves_every_artifact_it_holds_no_claim_on(
    cluster: Cluster, graph: MemoryGraph
) -> None:
    fixture = cluster.fixture()
    corpus = place_corpus(cluster, fixture, graph)
    before_artifacts = artifact_state(cluster, corpus.unbound, fixture.erased_id)
    before_events = event_state(cluster, corpus.events, fixture.erased_id)
    assert corpus.descendant in before_artifacts, (
        "the deliberate untouched candidate carries no claim for the erased tenant"
    )

    outcome = run_erasure(cluster.store, request_for(fixture), narrowed_seams(fixture))

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.run_id is not None
    candidates = {
        _as_uuid(row[0]): str(row[1])
        for row in cluster.rows(SELECT_RUN_CANDIDATES, (outcome.run_id,))
    }
    dispositions = {
        _as_uuid(row[0]): (str(row[1]), str(row[2]), row[3])
        for row in cluster.rows(SELECT_RUN_DISPOSITIONS, (outcome.run_id,))
    }
    untouched = [identifier for identifier in before_artifacts if identifier in candidates]

    event(f"artifacts the erased tenant claimed={count_band(len(corpus.bound))}")
    event(f"artifacts it did not={count_band(len(before_artifacts))}")
    event(f"events placed={count_band(len(corpus.events))}")
    event(f"unbound artifacts the run selected={count_band(len(untouched))}")
    event(f"dispositions written={count_band(len(dispositions))}")

    # Requirements 18.2 and 18.4: every Artifact the erased tenant held no current
    # claim on stands exactly as it stood, in all three of the columns a rewrite or
    # a revision would move.
    after_artifacts = artifact_state(cluster, corpus.unbound, fixture.erased_id)
    assert set(after_artifacts) == set(before_artifacts), (
        "an artifact the erased tenant held no claim on was removed by the run"
    )
    for identifier, before in before_artifacts.items():
        assert after_artifacts[identifier] == before, (
            f"artifact {identifier} carried no claim for the erased tenant and moved: "
            f"{before} became {after_artifacts[identifier]}"
        )

    # The same claim for the Events of the retained tenant's Session, which the
    # scoped arms of the sweep have no reason to reach and must not.
    after_events = event_state(cluster, corpus.events, fixture.erased_id)
    assert after_events == before_events, "an event of the retained tenant's session moved"

    # Requirement 18.8: a candidate the run examined and left alone is accounted
    # for, with a reason that says something.
    assert untouched, "no unbound artifact was selected, so this example asserts nothing"
    for identifier in untouched:
        assert identifier in dispositions, (
            f"candidate {identifier} was selected and carries no disposition"
        )
        disposition, reason, post_digest = dispositions[identifier]
        assert disposition == DispositionKind.RETAINED.value, (
            f"candidate {identifier} carries no claim for the erased tenant and was "
            f"recorded as {disposition}"
        )
        assert reason.strip(), f"the retention of {identifier} records no reason"
        # A retention records the digest it found as the digest it left, so where
        # the candidate row carried one it has to be the unchanged one.
        assert (
            post_digest is None or str(post_digest) == before_artifacts[identifier].content_digest
        ), f"the retention of {identifier} records a digest other than the one it left"

    # The reason the descendant carries is the one the decision table names for an
    # absent claim, rather than any reason at all.
    assert dispositions[corpus.descendant][1] == BINDING_ABSENT_REASON
