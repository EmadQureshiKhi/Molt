"""The pending sweep excludes by the stored vector rather than by the state flag.

This module asserts the one claim the sweep cannot make on its own and the unit
suite cannot check: that an Artifact leaves the drain's work list the moment a
vector is stored for it, whatever its state column still says.

Why the column is not enough. Migration 007 revokes `UPDATE ON TABLE ledger` from
every role, so an Event's `embedding_state` is fixed by the statement that
appended it and no role can ever clear it. A sweep reading that column alone
would return every Event that has ever been embedded, on every pass, for as long
as the row exists: a drain would pay a provider call per already-embedded Event
in every fresh container, and the unembedded-coverage count a certificate reports
would name Artifacts that are fully embedded. Exempting the column from the
revocation is the remedy this design refuses, because an editable Ledger row is
what the append-only guarantee and the hash chain exist to rule out. So each
branch of the sweep asks whether an Embedding row stands for the Artifact, and
that question is what these examples put to a cluster.

Four findings, and the fifth is the refusal that goes with them.

An Event whose Ledger row still reads `pending` but which has a stored vector is
not swept, and the row is read back to show the flag really is still `pending`,
so the exclusion is attributable to the vector and to nothing else.

A Derived_Artifact in the same position is not swept either, on the same terms:
its state column is left untouched and the vector alone moves it out. The two
branches therefore mean the same thing as each other, which the column reading
did not.

An Event with no stored vector is still swept, so the existence term excludes the
embedded rows without excluding the work.

The sweep stays index-served. Each branch is selected by that table's partial
index over the pending rows, which is what supplies the ascending creation order
a fair drain takes, and the existence term is an anti lookup-join into the
Embedding table's uniqueness index rather than a read of the Embedding table
itself. One consequence of the anti-join is asserted only by its absence here:
rows can now be filtered after the scan, so the row bound can no longer be
pushed into the scans, and no claim about a scan-level limit is made.

Storing the same vector twice is refused by a class of its own carrying the three
values that identify the stored row, and that class is a `StoreError`, so a
caller catching the family still catches it. That is the distinction a drain acts
on: this refusal settles an Artifact, while an unreachable cluster leaves it owed.

Every migration is applied, because the sweep spans two tables that arrive in
different generations and the Embedding table arrives in a third.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.errors import EmbeddingAlreadyStoredError, StoreError
from molt.models.artifact import (
    EMBEDDING_DIMENSION,
    ArtifactKind,
    DerivedArtifact,
    DerivedArtifactKind,
)
from molt.models.event import EmbeddingState
from molt.store import Connection, MemoryStore
from molt.store.embeddings import (
    PENDING_STATE,
    SELECT_PENDING_STATEMENT,
    EmbeddingWrite,
    pending_artifacts,
    vector_text,
    write_derived_artifact,
    write_embedding,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places directly, every column bound. It owns no Client
# insert and no Ledger append, so those rows are placed here rather than through
# a module under test.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION_ROW: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, started_at) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_LEDGER_ROW: Final[str] = (
    "INSERT INTO ledger (id, session_id, client_id, seq, category, occurred_at, "
    "recorded_at, agent_cli, machine_id, payload, content_digest, prev_chain_digest, "
    "chain_digest, embedding_state, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s, %s, %s, %s, %s)"
)

# The plan corpus, placed in three bulk statements with every column bound.
#
# A partial index over the pending rows is only worth selecting when the pending
# rows are a small share of the table, and an anti-join is only worth serving by a
# lookup when the Embedding table is larger than the set being looked up. Both
# conditions hold on a deployed cluster and neither holds on a table holding a
# handful of rows, so the plan a deployment would produce is only visible once the
# settled rows exist.
#
# Every Ledger row here shares one Session, which the two uniqueness constraints
# on that table permit as long as the sequence number and the predecessor digest
# differ per row, and both do.
INSERT_EVENTS_BULK: Final[str] = (
    "INSERT INTO ledger (id, session_id, client_id, seq, category, occurred_at, "
    "recorded_at, agent_cli, machine_id, payload, content_digest, prev_chain_digest, "
    "chain_digest, embedding_state, expires_at) "
    "SELECT unnest(%s::UUID[]), %s, %s, unnest(%s::INT[]), 'tool_call', %s, %s, "
    "'agent', 'machine', '{}'::JSONB, unnest(%s::STRING[]), unnest(%s::STRING[]), "
    "unnest(%s::STRING[]), %s, %s"
)
INSERT_ARTIFACTS_BULK: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, revision, created_at, updated_at, embedding_state, expires_at) "
    "SELECT unnest(%s::UUID[]), 'summary', %s, 'a distilled body', "
    "unnest(%s::STRING[]), 'distil', 1, %s, %s, %s, %s"
)
# Many Embedding rows sharing one bound vector. The Artifact column of this table
# is polymorphic and so carries no reference, which is what lets a plan corpus be
# built from vectors alone.
INSERT_EMBEDDINGS_BULK: Final[str] = (
    "INSERT INTO embedding (artifact_id, artifact_kind, client_id, provider, model_id, "
    "vec, expires_at) SELECT unnest(%s::UUID[]), %s, %s, %s, %s, %s::VECTOR, %s"
)

# The reads that show a state column was never moved, which is what makes the
# exclusion attributable to the stored vector.
SELECT_LEDGER_STATE: Final[str] = "SELECT embedding_state FROM ledger WHERE id = %s"
SELECT_ARTIFACT_STATE: Final[str] = "SELECT embedding_state FROM derived_artifact WHERE id = %s"
COUNT_EMBEDDINGS: Final[str] = "SELECT count(*) FROM embedding WHERE artifact_id = %s"

# What the plan must say. Each branch is selected by that table's partial index
# over the pending rows, and the existence term is an anti lookup-join into the
# uniqueness index, whose leading columns are the Artifact and its kind. The plan
# is lowercased before the comparison, so these are the lowercase spellings.
LEDGER_PENDING_INDEX: Final[str] = "ledger@ledger_pending_embedding"
DERIVED_PENDING_INDEX: Final[str] = "derived_artifact@derived_pending_embedding"
PARTIAL_INDEX: Final[str] = "(partial index)"
ANTI_LOOKUP_JOIN: Final[str] = "lookup join (anti)"
EMBEDDING_UNIQUENESS_INDEX: Final[str] = "embedding@embedding_unique_per_model"

# How the plan names any access to the Embedding table. Exactly two appear, and
# both are the uniqueness index above, which is how a read of the Embedding table
# itself is excluded: a plan that scanned the base table or any other index over
# it would meet every other assertion here while costing a drain a table read per
# pass. The two counts together are the finding; neither alone is.
EMBEDDING_ACCESS: Final[str] = "table: embedding@"

# How many branches the sweep has, and so how many of each plan feature it must
# show: one partial index scan, one anti lookup-join, and one Embedding access per
# embeddable kind.
SWEEP_BRANCHES: Final[int] = 2

# The provider and the model every vector this module stores records.
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# One unit vector of the fixed width. These examples ask which rows the sweep
# returns rather than how near two vectors are, so one vector serves every row
# and the uniqueness constraint is what keeps the repeated write meaningful.
UNIT_VECTOR: Final[tuple[float, ...]] = tuple([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1))

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# How many settled rows of each kind the plan corpus holds, and how many rows are
# left owing a vector within it. The share matters rather than the counts: the
# partial indexes are worth selecting because the owed rows are a small fraction
# of each table, and the anti-join is worth serving by a lookup because the
# Embedding table is far larger than the set looked up in it.
PLAN_SETTLED_ROWS: Final[int] = 500
PLAN_PENDING_ROWS: Final[int] = 8

# The row bound the sweep is read with here. It is far above the number of rows
# this module places, so an example that expects a row to be absent is reading an
# exclusion rather than the far side of a page boundary.
SWEEP_LIMIT: Final[int] = 1000

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def scalar(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> object:
    """Read one column of one row on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    assert row is not None
    return row[0]


@dataclass(frozen=True, slots=True)
class Corpus:
    """A schema holding every migration, a store over it, and the rows to place."""

    store: MemoryStore
    connection: DriverConnection
    schema: str

    def tenant(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:10]}", "Tenant", "eu"),
        )
        return identifier

    def event(self, client_id: UUID, *, recorded_at: datetime = MOMENT, seq: int = 1) -> UUID:
        """Place one Ledger row directly, at the state an append records.

        The append is direct because no role holds `UPDATE` on this table, so the
        state this statement writes is the state the row carries for as long as it
        exists, and that permanence is the whole subject of this module.
        """
        session_id = uuid4()
        send(
            self.connection,
            INSERT_SESSION_ROW,
            (session_id, client_id, "agent", "machine", recorded_at),
        )
        identifier = uuid4()
        send(
            self.connection,
            INSERT_LEDGER_ROW,
            (
                identifier,
                session_id,
                client_id,
                seq,
                "tool_call",
                recorded_at,
                recorded_at,
                "agent",
                "machine",
                '{"tool":"read"}',
                digest_of(f"content-{identifier}"),
                digest_of(f"previous-{identifier}"),
                digest_of(f"chain-{identifier}"),
                EmbeddingState.PENDING.value,
                recorded_at + RETENTION,
            ),
        )
        return identifier

    def artifact(self, client_id: UUID, *, created_at: datetime = MOMENT) -> DerivedArtifact:
        """A Derived_Artifact record owing a vector, not yet written."""
        identifier = uuid4()
        return DerivedArtifact(
            id=identifier,
            kind=DerivedArtifactKind.SUMMARY,
            owner_client_id=client_id,
            body="a distilled body",
            content_digest=digest_of(f"artifact-{identifier}"),
            derivation_method="distil",
            revision=1,
            created_at=created_at,
            updated_at=created_at,
            redacted_at=None,
            embedding_state=EmbeddingState.PENDING,
            expires_at=created_at + RETENTION,
            procedure_confidence=None,
        )

    def pending_artifact(self, client_id: UUID, *, created_at: datetime = MOMENT) -> UUID:
        """Write one Derived_Artifact owing a vector and return its identifier."""
        record = self.artifact(client_id, created_at=created_at)
        write_derived_artifact(self.store, record)
        return record.id

    def vector_for(self, artifact_id: UUID, kind: ArtifactKind, client_id: UUID) -> EmbeddingWrite:
        """The Embedding write standing for one Artifact of one kind."""
        return EmbeddingWrite(
            artifact_id=artifact_id,
            artifact_kind=kind,
            client_id=client_id,
            provider=PROVIDER,
            model_id=MODEL,
            vec=UNIT_VECTOR,
            expires_at=MOMENT + RETENTION,
        )

    def store_vector(self, artifact_id: UUID, kind: ArtifactKind, client_id: UUID) -> UUID:
        """Store one vector on its own, which is the path a drain takes.

        Nothing here moves a state column: the drain's write is the Embedding row
        and the sweep is what reconciles the column with it.
        """
        return write_embedding(self.store, self.vector_for(artifact_id, kind, client_id))

    def swept_for(self, client_id: UUID) -> list[UUID]:
        """The Artifacts the sweep returns for one tenant, in the order it returned them."""
        return [
            found.artifact_id
            for found in pending_artifacts(self.store, limit=SWEEP_LIMIT)
            if found.client_id == client_id
        ]

    def plan_corpus(self, client_id: UUID, settled: int, owed: int) -> None:
        """Fill both swept tables so the shares a plan is chosen by are realistic.

        The settled Events record `not_required`, the settled Derived_Artifacts
        record `embedded` and carry a stored vector each, and a few rows of each
        kind are left owing. Nothing here is content: these rows exist so that the
        pending rows are a small share of each table and the Embedding table is
        larger than the set the anti-join looks up in it, which is the situation a
        deployed cluster is in and the only situation in which the plan under test
        is the plan the cluster picks.
        """
        session_id = uuid4()
        send(
            self.connection,
            INSERT_SESSION_ROW,
            (session_id, client_id, "agent", "machine", MOMENT),
        )
        self._events_bulk(client_id, session_id, 1, settled, EmbeddingState.NOT_REQUIRED)
        self._events_bulk(client_id, session_id, settled + 1, owed, EmbeddingState.PENDING)
        embedded = self._artifacts_bulk(client_id, settled, EmbeddingState.EMBEDDED)
        send(
            self.connection,
            INSERT_EMBEDDINGS_BULK,
            (
                list(embedded),
                ArtifactKind.DERIVED_ARTIFACT.value,
                client_id,
                PROVIDER,
                MODEL,
                vector_text(UNIT_VECTOR),
                MOMENT + RETENTION,
            ),
        )
        self._artifacts_bulk(client_id, owed, EmbeddingState.PENDING)

    def _events_bulk(
        self,
        client_id: UUID,
        session_id: UUID,
        first_seq: int,
        count: int,
        state: EmbeddingState,
    ) -> tuple[UUID, ...]:
        """Place many Ledger rows at one state, in one statement."""
        identifiers = [uuid4() for _ in range(count)]
        send(
            self.connection,
            INSERT_EVENTS_BULK,
            (
                identifiers,
                session_id,
                client_id,
                list(range(first_seq, first_seq + count)),
                MOMENT,
                MOMENT,
                [digest_of(f"content-{identifier}") for identifier in identifiers],
                [digest_of(f"previous-{identifier}") for identifier in identifiers],
                [digest_of(f"chain-{identifier}") for identifier in identifiers],
                state.value,
                MOMENT + RETENTION,
            ),
        )
        return tuple(identifiers)

    def _artifacts_bulk(
        self,
        client_id: UUID,
        count: int,
        state: EmbeddingState,
    ) -> tuple[UUID, ...]:
        """Place many Derived_Artifact rows at one state, in one statement."""
        identifiers = [uuid4() for _ in range(count)]
        send(
            self.connection,
            INSERT_ARTIFACTS_BULK,
            (
                identifiers,
                client_id,
                [digest_of(f"artifact-{identifier}") for identifier in identifiers],
                MOMENT,
                MOMENT,
                state.value,
                MOMENT + RETENTION,
            ),
        )
        return tuple(identifiers)

    def plan_of(self, statement: str, params: tuple[object, ...]) -> str:
        """The plan the cluster reports for one statement, as one lowercase block.

        The prefix is a literal and the statement is the module's own literal; every
        value stays a bound parameter, so nothing a caller supplied reaches
        statement text even here.
        """
        with self.connection.cursor() as cursor:
            cursor.execute("EXPLAIN " + statement, params)
            rows = cursor.fetchall()
        return "\n".join(" ".join(str(column) for column in row) for row in rows).lower()

    def collect_statistics(self) -> None:
        """Collect table statistics, so a plan reflects selectivity rather than absence.

        A table whose statistics have never been collected is planned against a
        default guess. Collecting is what makes the plan the one a deployed
        cluster would produce.
        """
        with self.connection.cursor() as cursor:
            cursor.execute("ANALYZE ledger")
            cursor.execute("ANALYZE derived_artifact")
            cursor.execute("ANALYZE embedding")


@pytest.fixture(scope="module")
def corpus(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Corpus]:
    """Apply every migration, then build a store bound to that schema."""
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
        yield Corpus(store=store, connection=fresh_schema, schema=schema)


# ---------------------------------------------------------------------------
# The stored vector, not the frozen flag
# ---------------------------------------------------------------------------


def test_an_event_with_a_stored_vector_is_not_swept(corpus: Corpus) -> None:
    """The flag is still `pending` and the Event is out of the sweep anyway.

    Both halves are asserted together on purpose. The flag read is what makes the
    exclusion attributable to the stored vector: a sweep that had somehow cleared
    the column would pass the second assertion and fail the first, and a sweep
    reading the column alone would pass the first and fail the second.
    """
    client_id = corpus.tenant()
    event_id = corpus.event(client_id)
    corpus.store_vector(event_id, ArtifactKind.EVENT, client_id)

    swept = corpus.swept_for(client_id)

    assert scalar(corpus.connection, SELECT_LEDGER_STATE, (event_id,)) == PENDING_STATE
    assert scalar(corpus.connection, COUNT_EMBEDDINGS, (event_id,)) == 1
    assert event_id not in swept
    assert swept == []


def test_a_derived_artifact_with_a_stored_vector_is_not_swept(corpus: Corpus) -> None:
    """The same exclusion on the same terms, so the two branches mean one thing.

    The state column is deliberately left where the write put it, even though
    this table's guard would admit a transition, because a Derived_Artifact whose
    column was moved would be excluded by the flag and would say nothing about
    the existence term.
    """
    client_id = corpus.tenant()
    artifact_id = corpus.pending_artifact(client_id)
    corpus.store_vector(artifact_id, ArtifactKind.DERIVED_ARTIFACT, client_id)

    swept = corpus.swept_for(client_id)

    assert scalar(corpus.connection, SELECT_ARTIFACT_STATE, (artifact_id,)) == PENDING_STATE
    assert scalar(corpus.connection, COUNT_EMBEDDINGS, (artifact_id,)) == 1
    assert artifact_id not in swept
    assert swept == []


def test_an_event_with_no_stored_vector_is_still_swept(corpus: Corpus) -> None:
    """The existence term excludes the embedded rows and not the work.

    An exclusion that went too far would be invisible in the two examples above,
    because both of them expect an empty sweep. This one is what tells a sweep
    that answers correctly apart from a sweep that answers nothing.
    """
    client_id = corpus.tenant()
    owed_event = corpus.event(client_id)
    owed_artifact = corpus.pending_artifact(client_id, created_at=MOMENT + timedelta(minutes=1))
    embedded_event = corpus.event(client_id, recorded_at=MOMENT + timedelta(minutes=2), seq=2)
    corpus.store_vector(embedded_event, ArtifactKind.EVENT, client_id)

    swept = corpus.swept_for(client_id)

    assert swept == [owed_event, owed_artifact]
    assert embedded_event not in swept
    assert scalar(corpus.connection, COUNT_EMBEDDINGS, (owed_event,)) == 0


# ---------------------------------------------------------------------------
# The plan the drain's cost rests on
# ---------------------------------------------------------------------------


def test_the_sweep_is_served_by_the_partial_indexes_and_an_anti_join(corpus: Corpus) -> None:
    """Both partial indexes select the rows, and the existence term is an anti-join.

    The partial indexes matter because they are what supplies the ascending
    creation order the sweep is bounded by, so a drain takes the oldest owed
    vectors rather than an arbitrary sample. The anti lookup-join matters because
    the existence term is asked of every row the indexes select, and asking it of
    the Embedding base table would cost a drain a table read per pass.

    Nothing is asserted here about the row bound reaching the scans. It no longer
    does: rows can now be filtered after the scan, so the limit cannot be applied
    at scan level. That is a real consequence of the existence term and it is
    recorded here rather than asserted against.
    """
    client_id = corpus.tenant()
    corpus.plan_corpus(client_id, PLAN_SETTLED_ROWS, PLAN_PENDING_ROWS)
    corpus.collect_statistics()

    plan = corpus.plan_of(SELECT_PENDING_STATEMENT, (SWEEP_LIMIT,))

    assert LEDGER_PENDING_INDEX in plan, plan
    assert DERIVED_PENDING_INDEX in plan, plan
    assert plan.count(PARTIAL_INDEX) == SWEEP_BRANCHES, plan
    assert plan.count(ANTI_LOOKUP_JOIN) == SWEEP_BRANCHES, plan
    assert plan.count(EMBEDDING_UNIQUENESS_INDEX) == SWEEP_BRANCHES, plan
    assert plan.count(EMBEDDING_ACCESS) == SWEEP_BRANCHES, plan


# ---------------------------------------------------------------------------
# The refusal a drain acts on
# ---------------------------------------------------------------------------


def test_a_repeated_vector_is_refused_by_a_class_of_its_own(corpus: Corpus) -> None:
    """The second write is refused, and the refusal says which row is already held.

    A drain reaching this has learned that the work is done rather than that the
    cluster could not be written to, which is the difference between settling an
    Artifact and leaving it owed. The three carried values are an identifier and
    the two names of the service that produced the vector, so none of them is
    memory content.
    """
    client_id = corpus.tenant()
    event_id = corpus.event(client_id)
    corpus.store_vector(event_id, ArtifactKind.EVENT, client_id)

    with pytest.raises(EmbeddingAlreadyStoredError) as refusal:
        corpus.store_vector(event_id, ArtifactKind.EVENT, client_id)

    assert refusal.value.artifact_id == event_id
    assert refusal.value.provider == PROVIDER
    assert refusal.value.model_id == MODEL
    assert isinstance(refusal.value, StoreError)
    assert scalar(corpus.connection, COUNT_EMBEDDINGS, (event_id,)) == 1


def test_the_refusal_is_still_caught_by_the_family_base(corpus: Corpus) -> None:
    """An existing `except StoreError` call site keeps working unchanged.

    The narrower class is what a drain branches on, and widening the taxonomy is
    only safe if the handlers written before it existed still catch the failure.
    """
    client_id = corpus.tenant()
    artifact_id = corpus.pending_artifact(client_id)
    corpus.store_vector(artifact_id, ArtifactKind.DERIVED_ARTIFACT, client_id)

    with pytest.raises(StoreError, match="already stored"):
        corpus.store_vector(artifact_id, ArtifactKind.DERIVED_ARTIFACT, client_id)

    assert scalar(corpus.connection, COUNT_EMBEDDINGS, (artifact_id,)) == 1
