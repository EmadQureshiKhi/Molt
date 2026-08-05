"""The explicit sweep against a live instance: reasons, attribution, and the counts.

The statements of phase one are set-based and server-side, so almost nothing about
them can be asserted without a cluster. This module asserts the five things only a
cluster can answer.

**Every selection reason is on the record.** A candidate with no reason is a
disposition nobody can explain afterwards, so each of the five statements is
checked by reading back the reason its rows carry rather than by counting rows in
aggregate.

**A superseded attribution neither widens the sweep nor narrows it.** Two
Artifacts carry a version history: one whose live claim moved away from the erased
tenant, and one whose live claim moved towards it. The first must be absent and
the second present, and both are read back from the candidate set. This is the
claim the whole current-attribution predicate exists for, and it is the one that
fails silently if the predicate is ever dropped.

**A pending Embedding hides nothing.** An Artifact still waiting for a vector is
swept like any other, and the number of such Artifacts is read back off the run
row rather than from what the call returned.

**A Learned_Procedure below the recall floor is reached.** Exclusion from recall
is not a soft delete, so the below-floor procedure is asserted present. The
dedicated statement is also driven on its own against an empty run, so its
predicate is observable rather than masked by the binding statement that overlaps
it.

**The sweep is replayable.** Running phase one twice over the same run leaves the
same candidate set, which is what makes a serialization retry safe.

**Validates: Requirements 10.9, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 21.9,
43.6, 49.11**
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.confidence import (
    FAILURE_DELTA_KEY,
    INITIAL_KEY,
    RECALL_FLOOR_KEY,
    SUCCESS_DELTA_KEY,
    ConfidencePolicy,
)
from molt.config.resolve import Configuration
from molt.erase.sweep import (
    INSERT_BELOW_FLOOR_PROCEDURES_STATEMENT,
    REASON_CLIENT_BINDING,
    REASON_EMBEDDING_OF_SELECTED,
    REASON_EVENT_OF_SCOPED_SESSION,
    REASON_LINEAGE_DESCENDANT,
    REASON_SESSION_SCOPE,
    SweepResult,
    run_sweep,
)
from molt.models.artifact import ArtifactKind, DerivedArtifactKind
from molt.models.event import EmbeddingState
from molt.store import Connection, Cursor, MemoryStore
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The fixed vector width the schema declares, and the digest width it checks.
VECTOR_WIDTH: Final[int] = 1024
DIGEST_WIDTH: Final[int] = 64

# The rows this module places directly. The sweep owns no insert of its own: it
# selects over content other components write, so the world each example sweeps is
# built here.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name) VALUES (%s, %s, 'Stub workspace')"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) "
    "VALUES (%s, %s, 'stub', 'stub-host')"
)
INSERT_EVENT: Final[str] = (
    "INSERT INTO ledger (id, session_id, client_id, seq, category, agent_cli, machine_id, "
    "payload, content_digest, prev_chain_digest, chain_digest, embedding_state, expires_at) "
    "VALUES (%s, %s, %s, %s, 'user_prompt', 'stub', 'stub-host', '{}'::JSONB, %s, %s, %s, %s, "
    "now() + INTERVAL '3600 seconds')"
)
INSERT_ARTIFACT: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, embedding_state, procedure_confidence, expires_at) "
    "VALUES (%s, %s, %s, 'body', %s, 'stub', %s, %s, now() + INTERVAL '3600 seconds')"
)
INSERT_EDGE: Final[str] = (
    "INSERT INTO lineage_edge (child_id, parent_id, parent_kind, derivation_method) "
    "VALUES (%s, %s, %s, 'stub')"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, confidence) "
    "VALUES (%s, %s, %s, %s, 'scope', 1.0)"
)
CLOSE_BINDING: Final[str] = (
    "UPDATE client_binding SET valid_to = now(), superseded_by = %s WHERE id = %s"
)
INSERT_EMBEDDING: Final[str] = (
    "INSERT INTO embedding (id, artifact_id, artifact_kind, client_id, provider, model_id, vec, "
    "expires_at) VALUES (%s, %s, %s, %s, 'stub', 'stub-model', %s::VECTOR, "
    "now() + INTERVAL '3600 seconds')"
)
INSERT_REQUEST: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification) "
    "VALUES (%s, %s, 'stub-operator', 'stub justification')"
)
INSERT_RUN: Final[str] = (
    "INSERT INTO erasure_run (id, request_id, client_id, requester, t_before) "
    "VALUES (%s, %s, %s, 'stub-operator', now())"
)

# What every claim about the recorded candidate set is read from.
READ_REASON: Final[str] = (
    "SELECT selection_reason FROM erasure_candidate WHERE run_id = %s AND artifact_id = %s"
)
READ_DIGEST: Final[str] = (
    "SELECT content_digest FROM erasure_candidate WHERE run_id = %s AND artifact_id = %s"
)
COUNT_CANDIDATES: Final[str] = "SELECT count(*) FROM erasure_candidate WHERE run_id = %s"
COUNT_BY_REASON: Final[str] = (
    "SELECT count(*) FROM erasure_candidate WHERE run_id = %s AND selection_reason = %s"
)
READ_UNEMBEDDED: Final[str] = "SELECT unembedded_count FROM erasure_run WHERE id = %s"
READ_RUN_SESSION: Final[str] = (
    "SELECT terminal_chain_digest, terminal_seq, row_count FROM run_session "
    "WHERE run_id = %s AND session_id = %s"
)
COUNT_RUN_SESSIONS: Final[str] = "SELECT count(*) FROM run_session WHERE run_id = %s"

# The policy every example applies. The floor is not the surface default, so a
# procedure selected below it cannot have been selected by a number this codebase
# happens to hold.
EXAMPLE_FLOOR: Final[float] = 0.5
EXAMPLE_INITIAL: Final[float] = 0.4
EXAMPLE_SUCCESS_DELTA: Final[float] = 0.06
EXAMPLE_FAILURE_DELTA: Final[float] = 0.11

# The two standings the example procedures carry, one either side of the floor.
BELOW_FLOOR_STANDING: Final[float] = 0.1
ABOVE_FLOOR_STANDING: Final[float] = 0.9

# How many Events the swept Session holds, and which of them is left unembedded.
EVENTS_PER_SESSION: Final[int] = 2

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver
# installed.
DriverConnection = Any


def digest_of(*parts: object) -> str:
    """A digest of the shape the ledger's own check admits, derived from identifiers."""
    material = "|".join(str(part) for part in parts).encode()
    return hashlib.sha256(material).hexdigest()


def unit_vector() -> str:
    """A vector literal of the fixed width, carrying unit length."""
    components = ["0"] * VECTOR_WIDTH
    components[0] = "1"
    return "[" + ",".join(components) + "]"


def example_policy() -> ConfidencePolicy:
    """The policy every example applies, read from a configuration naming four values."""
    return ConfidencePolicy.from_configuration(
        Configuration(
            environ={
                INITIAL_KEY: str(EXAMPLE_INITIAL),
                SUCCESS_DELTA_KEY: str(EXAMPLE_SUCCESS_DELTA),
                FAILURE_DELTA_KEY: str(EXAMPLE_FAILURE_DELTA),
                RECALL_FLOOR_KEY: str(EXAMPLE_FLOOR),
            },
            file_values={},
        )
    )


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

    def send(self, statement: str, params: tuple[object, ...] | None = None) -> None:
        """Send one statement whose rows nothing reads."""
        self.rows(statement, params)

    def one(self, statement: str, params: tuple[object, ...]) -> tuple[Any, ...] | None:
        """The single row a statement produced, or None when it produced none."""
        produced = self.rows(statement, params)
        assert len(produced) <= 1, f"the statement produced {len(produced)} rows where one was read"
        return produced[0] if produced else None

    def count(self, statement: str, params: tuple[object, ...]) -> int:
        """The number one counting statement reports."""
        produced = self.one(statement, params)
        assert produced is not None
        return int(produced[0])

    def reason_of(self, run_id: UUID, artifact_id: UUID) -> str | None:
        """The reason one Artifact was selected for, or None when it was not selected."""
        produced = self.one(READ_REASON, (run_id, artifact_id))
        return None if produced is None else str(produced[0])

    # -- placing the world -------------------------------------------------

    def client(self) -> UUID:
        """One tenant of this module's own, so a sweep over it disturbs no other."""
        identifier = uuid4()
        self.send(INSERT_CLIENT, (identifier, f"tenant-{identifier.hex[:12]}"))
        return identifier

    def session(self, client_id: UUID) -> UUID:
        """One Session owned by a tenant."""
        identifier = uuid4()
        self.send(INSERT_SESSION, (identifier, client_id))
        return identifier

    def event(
        self,
        session_id: UUID,
        client_id: UUID,
        seq: int,
        *,
        embedding_state: EmbeddingState = EmbeddingState.NOT_REQUIRED,
    ) -> UUID:
        """One Event of a Session, chained to its predecessor's digest."""
        identifier = uuid4()
        self.send(
            INSERT_EVENT,
            (
                identifier,
                session_id,
                client_id,
                seq,
                digest_of(identifier),
                digest_of(session_id, seq - 1),
                digest_of(session_id, seq),
                embedding_state.value,
            ),
        )
        return identifier

    def artifact(
        self,
        client_id: UUID,
        *,
        kind: DerivedArtifactKind = DerivedArtifactKind.SUMMARY,
        embedding_state: EmbeddingState = EmbeddingState.EMBEDDED,
        standing: float | None = None,
    ) -> UUID:
        """One Derived_Artifact, with a standing only for the procedure kind."""
        identifier = uuid4()
        self.send(
            INSERT_ARTIFACT,
            (
                identifier,
                kind.value,
                client_id,
                digest_of(identifier),
                embedding_state.value,
                standing,
            ),
        )
        return identifier

    def procedure(self, client_id: UUID, standing: float) -> UUID:
        """One Learned_Procedure at a chosen standing, bound to its tenant."""
        identifier = self.artifact(
            client_id,
            kind=DerivedArtifactKind.LEARNED_PROCEDURE,
            standing=standing,
        )
        self.bind(identifier, ArtifactKind.DERIVED_ARTIFACT, client_id)
        return identifier

    def edge(self, child_id: UUID, parent_id: UUID, parent_kind: ArtifactKind) -> None:
        """One lineage edge from a child to the Artifact it was derived from."""
        self.send(INSERT_EDGE, (child_id, parent_id, parent_kind.value))

    def bind(self, artifact_id: UUID, kind: ArtifactKind, client_id: UUID) -> UUID:
        """One current attribution version naming a tenant."""
        identifier = uuid4()
        self.send(INSERT_BINDING, (identifier, artifact_id, kind.value, client_id))
        return identifier

    def supersede(
        self,
        artifact_id: UUID,
        kind: ArtifactKind,
        *,
        was: UUID,
        now_is: UUID,
    ) -> UUID:
        """Move one Artifact's live attribution from one tenant to another.

        The successor is written first and the predecessor closed against it,
        because closure is total in the schema: a version carrying a validity end
        carries a successor too.
        """
        predecessor = self.bind(artifact_id, kind, was)
        successor = self.bind(artifact_id, kind, now_is)
        self.send(CLOSE_BINDING, (successor, predecessor))
        return successor

    def embedding(self, artifact_id: UUID, kind: ArtifactKind, client_id: UUID) -> UUID:
        """One vector standing for an Artifact."""
        identifier = uuid4()
        self.send(
            INSERT_EMBEDDING,
            (identifier, artifact_id, kind.value, client_id, unit_vector()),
        )
        return identifier

    def run(self, client_id: UUID) -> UUID:
        """One erasure run of a tenant, with the request it answers."""
        request_id = uuid4()
        run_id = uuid4()
        self.send(INSERT_REQUEST, (request_id, client_id))
        self.send(INSERT_RUN, (run_id, request_id, client_id))
        return run_id


@dataclass(frozen=True, slots=True)
class World:
    """One tenant's content, a second tenant's content, and a run over the first.

    Every example sweeps this same shape, because the claims are about which rows a
    sweep reaches and they are only meaningful against a world holding rows it must
    not reach.
    """

    erased_client: UUID
    retained_client: UUID
    run_id: UUID
    session_id: UUID
    other_session_id: UUID
    events: tuple[UUID, ...]
    other_event_id: UUID
    bound_artifact: UUID
    descendant: UUID
    embedding_id: UUID
    below_floor_procedure: UUID
    above_floor_procedure: UUID
    moved_away: UUID
    moved_in: UUID
    retained_artifact: UUID


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


@pytest.fixture
def world(cluster: Cluster) -> World:
    """Place one erased tenant's content beside a retained tenant's."""
    erased = cluster.client()
    retained = cluster.client()

    session_id = cluster.session(erased)
    events = tuple(
        cluster.event(
            session_id,
            erased,
            seq,
            embedding_state=(
                EmbeddingState.PENDING if seq == EVENTS_PER_SESSION else EmbeddingState.EMBEDDED
            ),
        )
        for seq in range(1, EVENTS_PER_SESSION + 1)
    )

    other_session_id = cluster.session(retained)
    other_event_id = cluster.event(other_session_id, retained, 1)

    # Bound by a current attribution, and still owed a vector: the sweep reaches it
    # by identity rather than by the presence of one.
    bound_artifact = cluster.artifact(erased, embedding_state=EmbeddingState.PENDING)
    cluster.bind(bound_artifact, ArtifactKind.DERIVED_ARTIFACT, erased)

    # Reached only through lineage: no attribution of its own names the tenant.
    descendant = cluster.artifact(retained)
    cluster.edge(descendant, bound_artifact, ArtifactKind.DERIVED_ARTIFACT)

    embedding_id = cluster.embedding(bound_artifact, ArtifactKind.DERIVED_ARTIFACT, erased)

    below_floor_procedure = cluster.procedure(erased, BELOW_FLOOR_STANDING)
    above_floor_procedure = cluster.procedure(erased, ABOVE_FLOOR_STANDING)

    moved_away = cluster.artifact(retained)
    cluster.supersede(
        moved_away,
        ArtifactKind.DERIVED_ARTIFACT,
        was=erased,
        now_is=retained,
    )
    moved_in = cluster.artifact(retained)
    cluster.supersede(
        moved_in,
        ArtifactKind.DERIVED_ARTIFACT,
        was=retained,
        now_is=erased,
    )

    retained_artifact = cluster.artifact(retained)
    cluster.bind(retained_artifact, ArtifactKind.DERIVED_ARTIFACT, retained)

    return World(
        erased_client=erased,
        retained_client=retained,
        run_id=cluster.run(erased),
        session_id=session_id,
        other_session_id=other_session_id,
        events=events,
        other_event_id=other_event_id,
        bound_artifact=bound_artifact,
        descendant=descendant,
        embedding_id=embedding_id,
        below_floor_procedure=below_floor_procedure,
        above_floor_procedure=above_floor_procedure,
        moved_away=moved_away,
        moved_in=moved_in,
        retained_artifact=retained_artifact,
    )


def swept(cluster: Cluster, world: World) -> SweepResult:
    """Run phase one over the world with the example policy."""
    return run_sweep(
        cluster.store,
        world.run_id,
        world.erased_client,
        policy=example_policy(),
    )


# ---------------------------------------------------------------------------
# Every selection reason is on the record
# ---------------------------------------------------------------------------


def test_each_selection_reason_is_recorded_against_the_rows_it_selected(
    cluster: Cluster,
    world: World,
) -> None:
    """Five statements, five reasons, each read back off the rows it wrote."""
    result = swept(cluster, world)

    assert cluster.reason_of(world.run_id, world.session_id) == REASON_SESSION_SCOPE
    for event_id in world.events:
        assert cluster.reason_of(world.run_id, event_id) == REASON_EVENT_OF_SCOPED_SESSION
    assert cluster.reason_of(world.run_id, world.bound_artifact) == REASON_CLIENT_BINDING
    assert cluster.reason_of(world.run_id, world.descendant) == REASON_LINEAGE_DESCENDANT
    assert cluster.reason_of(world.run_id, world.embedding_id) == REASON_EMBEDDING_OF_SELECTED

    assert result.counts.session_scope == 1
    assert result.counts.event_of_scoped_session == EVENTS_PER_SESSION
    assert result.counts.lineage_descendant == 1
    assert result.counts.embedding_of_selected == 1
    assert result.counts.total == cluster.count(COUNT_CANDIDATES, (world.run_id,))
    for reason in (
        REASON_SESSION_SCOPE,
        REASON_EVENT_OF_SCOPED_SESSION,
        REASON_CLIENT_BINDING,
        REASON_LINEAGE_DESCENDANT,
        REASON_EMBEDDING_OF_SELECTED,
    ):
        assert result.counts.for_reason(reason) == cluster.count(
            COUNT_BY_REASON, (world.run_id, reason)
        )


def test_another_tenants_rows_are_left_alone(cluster: Cluster, world: World) -> None:
    """A sweep is scoped by ownership and by attribution, so nothing else is reached."""
    swept(cluster, world)

    assert cluster.reason_of(world.run_id, world.other_session_id) is None
    assert cluster.reason_of(world.run_id, world.other_event_id) is None
    assert cluster.reason_of(world.run_id, world.retained_artifact) is None


def test_the_event_digest_travels_onto_the_candidate_row(cluster: Cluster, world: World) -> None:
    """A candidate carries the digest of the content it names, so a certificate can quote it."""
    swept(cluster, world)

    recorded = cluster.one(READ_DIGEST, (world.run_id, world.events[0]))
    assert recorded is not None
    assert str(recorded[0]) == digest_of(world.events[0])


# ---------------------------------------------------------------------------
# The current-attribution predicate
# ---------------------------------------------------------------------------


def test_a_superseded_attribution_neither_widens_nor_narrows_the_sweep(
    cluster: Cluster,
    world: World,
) -> None:
    """History is not a live claim, and a live claim stands whatever precedes it."""
    swept(cluster, world)

    assert cluster.reason_of(world.run_id, world.moved_away) is None, (
        "an attribution superseded away from the tenant must not widen the sweep"
    )
    assert cluster.reason_of(world.run_id, world.moved_in) == REASON_CLIENT_BINDING, (
        "an attribution superseded towards the tenant is the live claim and must be swept"
    )


# ---------------------------------------------------------------------------
# Pending embeddings
# ---------------------------------------------------------------------------


def test_pending_embedding_artifacts_are_selected_and_counted_on_the_run(
    cluster: Cluster,
    world: World,
) -> None:
    """Selection is by identity, and the size of the gap is recorded rather than implied."""
    result = swept(cluster, world)

    assert cluster.reason_of(world.run_id, world.bound_artifact) == REASON_CLIENT_BINDING
    assert result.unembedded_count == 2, (
        "one Derived_Artifact and one Event of the swept set are still owed a vector"
    )
    assert cluster.count(READ_UNEMBEDDED, (world.run_id,)) == result.unembedded_count


# ---------------------------------------------------------------------------
# The recall floor
# ---------------------------------------------------------------------------


def test_a_procedure_below_the_recall_floor_is_included(cluster: Cluster, world: World) -> None:
    """Exclusion from recall is not a soft delete, so the sweep still reaches it."""
    swept(cluster, world)

    assert cluster.reason_of(world.run_id, world.below_floor_procedure) == REASON_CLIENT_BINDING
    assert cluster.reason_of(world.run_id, world.above_floor_procedure) == REASON_CLIENT_BINDING


def test_the_floor_statement_selects_the_below_floor_procedure_on_its_own(
    cluster: Cluster,
    world: World,
) -> None:
    """Driven alone against an empty run, so the predicate is observable rather than masked."""
    policy = example_policy()

    def body(cursor: Cursor) -> None:
        cursor.execute(
            INSERT_BELOW_FLOOR_PROCEDURES_STATEMENT,
            (
                world.run_id,
                ArtifactKind.DERIVED_ARTIFACT.value,
                REASON_CLIENT_BINDING,
                DerivedArtifactKind.LEARNED_PROCEDURE.value,
                world.erased_client,
                policy.recall_floor,
            ),
        )

    cluster.store.in_serializable(body)

    assert cluster.reason_of(world.run_id, world.below_floor_procedure) == REASON_CLIENT_BINDING
    assert cluster.reason_of(world.run_id, world.above_floor_procedure) is None
    assert cluster.count(COUNT_CANDIDATES, (world.run_id,)) == 1


# ---------------------------------------------------------------------------
# The chain tip capture, and replay
# ---------------------------------------------------------------------------


def test_the_chain_tip_of_every_touched_session_is_captured(
    cluster: Cluster,
    world: World,
) -> None:
    """One row per swept Session, carrying the highest sequence number and its digest."""
    result = swept(cluster, world)

    recorded = cluster.one(READ_RUN_SESSION, (world.run_id, world.session_id))
    assert recorded is not None
    assert str(recorded[0]) == digest_of(world.session_id, EVENTS_PER_SESSION)
    assert int(recorded[1]) == EVENTS_PER_SESSION
    assert int(recorded[2]) == EVENTS_PER_SESSION
    assert cluster.one(READ_RUN_SESSION, (world.run_id, world.other_session_id)) is None
    assert result.sessions_recorded == cluster.count(COUNT_RUN_SESSIONS, (world.run_id,)) == 1


def test_replaying_the_sweep_leaves_the_same_candidate_set(
    cluster: Cluster,
    world: World,
) -> None:
    """Every statement is replayable, which is what makes a serialization retry safe."""
    first = swept(cluster, world)
    second = swept(cluster, world)

    assert second == first
    assert cluster.count(COUNT_CANDIDATES, (world.run_id,)) == first.counts.total
    assert cluster.count(COUNT_RUN_SESSIONS, (world.run_id,)) == 1
