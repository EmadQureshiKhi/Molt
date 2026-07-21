"""Unit tests for the embedding statements, the write-time checks, and the states.

Nothing here opens a socket. A scripted cursor answers each statement from a
script and keeps what it was sent, so every claim below is asserted by reading
the statements the module produced. The claims that need a cluster to mean
anything, that the neighbour statement is served by an index over a bounded span
and that the ordering the cluster performs really matches the distance it
projects, are asserted in the instance-backed module.

Six properties of the shape are checked here.

Every caller-supplied value is bound and no identifier is interpolated. The
vector travels as bound text with the cast written into the statement, the
permitted Clients travel as one bound array however many of them there are, and
no statement text carries a value a caller supplied.

The Artifact row and its Embedding row land in one transaction. One
`BEGIN`, the Artifact insert, the Embedding insert in that order, one `COMMIT`.

The width and unit-norm checks happen before anything is sent. A vector of the
wrong width, a vector holding a component that is not a finite number, and a
vector whose norm is away from one are each refused with no statement sent at
all, on the write path and on the query path alike.

The embedding state is derived from whether a vector accompanied the row rather
than read off the record, and a record claiming an embedded state with no vector
is refused.

The neighbour statement is the statement the design states: the tenancy `EXISTS`
restricted to unsuperseded Attribution_Versions, the optional ceiling in the same
predicate, the ordering by L2 distance, and the projected cosine distance.

The sweep spans both embeddable kinds, ascends by creation instant, and carries a
bound.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.errors import StoreError
from molt.models.artifact import (
    EMBEDDING_DIMENSION,
    ArtifactKind,
    DerivedArtifact,
    DerivedArtifactKind,
)
from molt.models.event import EmbeddingState
from molt.store import Connection, MemoryStore
from molt.store.embeddings import (
    DEFAULT_NEIGHBOUR_LIMIT,
    DEFAULT_PENDING_LIMIT,
    EMBEDDING_UNIQUE_CONSTRAINT,
    INSERT_ARTIFACT_STATEMENT,
    INSERT_EMBEDDING_STATEMENT,
    MARK_STATE_STATEMENT,
    NEAREST_STATEMENT,
    PENDING_STATE,
    SELECT_PENDING_STATEMENT,
    UNIQUE_VIOLATION_STATE,
    ArtifactWrite,
    EmbeddingWrite,
    Neighbour,
    PendingArtifact,
    mark_embedding_state,
    nearest,
    pending_artifacts,
    vector_text,
    write_derived_artifact,
    write_embedding,
)
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    SERIALIZABLE_STATEMENT,
)

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# The provider and model every row in this module records.
PROVIDER: Final[str] = "stub-provider"
MODEL: Final[str] = "stub-model"

# A digest-shaped value for the column the schema fixes at 64 characters.
DIGEST: Final[str] = "a" * 64

# How many parameters each statement binds.
ARTIFACT_PARAMETER_COUNT: Final[int] = 13
EMBEDDING_PARAMETER_COUNT: Final[int] = 9
NEAREST_PARAMETER_COUNT: Final[int] = 7


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()
    error: Exception | None = None


@dataclass(slots=True)
class Script:
    """The answers a connection hands out, consumed in the order they match."""

    answers: list[Answer] = field(default_factory=list)
    sent: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    armed: tuple[tuple[object, ...], ...] = ()

    @property
    def statements(self) -> list[str]:
        """Every statement the script was sent, in order."""
        return [query for query, _ in self.sent]

    def parameters_of(self, statement: str) -> tuple[object, ...] | None:
        """The bound parameters of the one occurrence of a statement."""
        matches = [params for query, params in self.sent if query == statement]
        assert len(matches) == 1, f"the statement should have been sent once, not {len(matches)}"
        return matches[0]

    def take(self, query: str) -> Answer | None:
        """The next answer matching a statement, removed from the script."""
        for index, answer in enumerate(self.answers):
            if answer.fragment in query:
                return self.answers.pop(index)
        return None


class ScriptedCursor:
    """A cursor answering from a script and recording what it was sent."""

    def __init__(self, script: Script) -> None:
        self._script = script
        self.released = False

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then raise or arm rows as the script says."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        if answer is None:
            self._script.armed = ()
            return None
        if answer.error is not None:
            raise answer.error
        self._script.armed = answer.rows
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or None when the statement armed none."""
        rows = self._script.armed
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._script.armed)

    def close(self) -> None:
        """Mark this cursor released."""
        self.released = True


class ScriptedConnection:
    """A connection handing out scripted cursors over one shared script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Open a recording cursor over this connection's script."""
        return ScriptedCursor(self.script)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True


class DriverFailureError(Exception):
    """A driver failure carrying the state and the constraint a driver reports."""

    def __init__(self, sqlstate: str, constraint_name: str | None) -> None:
        super().__init__("the statement was refused")
        self.sqlstate = sqlstate
        self.diag = _Diagnostic(constraint_name)


class _Diagnostic:
    """The diagnostic attribute a driver failure carries the constraint under."""

    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


# ---------------------------------------------------------------------------
# Vectors under test
# ---------------------------------------------------------------------------


def unit_vector(offset: int = 0) -> tuple[float, ...]:
    """A unit-length vector of the fixed width, with its mass on one component."""
    components = [0.0] * EMBEDDING_DIMENSION
    components[offset % EMBEDDING_DIMENSION] = 1.0
    return tuple(components)


def spread_vector() -> tuple[float, ...]:
    """A unit-length vector with every component contributing equally."""
    component = 1.0 / math.sqrt(EMBEDDING_DIMENSION)
    return tuple([component] * EMBEDDING_DIMENSION)


def embedding_of(
    artifact_id: UUID,
    client_id: UUID,
    *,
    vec: tuple[float, ...] | None = None,
    kind: ArtifactKind = ArtifactKind.DERIVED_ARTIFACT,
) -> EmbeddingWrite:
    """An Embedding write for one Artifact of one tenant."""
    return EmbeddingWrite(
        artifact_id=artifact_id,
        artifact_kind=kind,
        client_id=client_id,
        provider=PROVIDER,
        model_id=MODEL,
        vec=unit_vector() if vec is None else vec,
        expires_at=EXPIRY,
    )


def artifact_of(
    artifact_id: UUID,
    client_id: UUID,
    *,
    state: EmbeddingState = EmbeddingState.PENDING,
) -> DerivedArtifact:
    """A Derived_Artifact record at a stated embedding state."""
    return DerivedArtifact(
        id=artifact_id,
        kind=DerivedArtifactKind.SUMMARY,
        owner_client_id=client_id,
        body="a distilled body",
        content_digest=DIGEST,
        derivation_method="distil",
        revision=1,
        created_at=MOMENT,
        updated_at=MOMENT,
        redacted_at=None,
        embedding_state=state,
        expires_at=EXPIRY,
        procedure_confidence=None,
    )


# ---------------------------------------------------------------------------
# The write-time checks
# ---------------------------------------------------------------------------


def test_a_vector_of_another_width_is_refused_before_anything_is_sent() -> None:
    """The column holds one width, and a vector of another cannot be stored at all."""
    with pytest.raises(ValueError, match="component"):
        embedding_of(uuid4(), uuid4(), vec=(1.0,))


def test_a_vector_that_is_not_unit_length_is_refused() -> None:
    """L2 ordering and cosine ordering coincide on unit vectors and nowhere else."""
    doubled = tuple(2.0 * component for component in unit_vector())

    with pytest.raises(ValueError, match="unit length"):
        embedding_of(uuid4(), uuid4(), vec=doubled)


def test_a_vector_holding_a_value_that_is_not_a_number_is_refused() -> None:
    """A component that is not finite has no distance to anything."""
    broken = list(unit_vector())
    broken[1] = math.inf

    with pytest.raises(ValueError, match="finite"):
        embedding_of(uuid4(), uuid4(), vec=tuple(broken))


def test_a_vector_scaled_within_the_tolerance_is_accepted() -> None:
    """Floating-point error in a provider's own scaling is not a refusal."""
    spread = spread_vector()
    norm = math.sqrt(math.fsum(component * component for component in spread))

    assert abs(norm - 1.0) <= 1e-9
    assert embedding_of(uuid4(), uuid4(), vec=spread).dimension == EMBEDDING_DIMENSION


def test_only_an_embeddable_kind_carries_an_embedding() -> None:
    """A Session holds no embeddable text, so no vector stands for one."""
    with pytest.raises(ValueError, match="event or a derived artifact"):
        embedding_of(uuid4(), uuid4(), kind=ArtifactKind.SESSION)


def test_a_row_records_the_provider_and_the_model_that_produced_it() -> None:
    """A model identifier alone does not say which service produced the vector."""
    with pytest.raises(ValueError, match="provider"):
        EmbeddingWrite(
            artifact_id=uuid4(),
            artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=uuid4(),
            provider="",
            model_id=MODEL,
            vec=unit_vector(),
            expires_at=EXPIRY,
        )


def test_a_query_vector_is_held_to_the_same_checks_with_no_statement_sent() -> None:
    """The ordering is a cosine ordering only when both sides are unit vectors."""
    script = Script()

    with pytest.raises(ValueError, match="unit length"):
        nearest(build_store(script), [0.0] * EMBEDDING_DIMENSION, permitted_clients=[uuid4()])

    assert NEAREST_STATEMENT not in script.statements


# ---------------------------------------------------------------------------
# The one transaction
# ---------------------------------------------------------------------------


def test_the_artifact_and_its_embedding_are_written_by_one_transaction() -> None:
    """One transaction, the Artifact first, and the vector inside it."""
    artifact_id = uuid4()
    client_id = uuid4()
    embedding_id = uuid4()
    script = Script(
        answers=[
            Answer("INSERT INTO derived_artifact", ((artifact_id, "embedded"),)),
            Answer("INSERT INTO embedding", ((embedding_id, MOMENT),)),
        ]
    )

    written = write_derived_artifact(
        build_store(script),
        artifact_of(artifact_id, client_id),
        embedding=embedding_of(artifact_id, client_id),
    )

    assert written == ArtifactWrite(
        artifact_id=artifact_id,
        embedding_id=embedding_id,
        embedding_state=EmbeddingState.EMBEDDED,
    )
    statements = script.statements
    assert statements.count(BEGIN_STATEMENT) == 1
    assert SERIALIZABLE_STATEMENT in statements
    assert statements.index(INSERT_ARTIFACT_STATEMENT) < statements.index(
        INSERT_EMBEDDING_STATEMENT
    )
    assert statements.index(INSERT_EMBEDDING_STATEMENT) < statements.index(COMMIT_STATEMENT)


def test_the_artifact_write_binds_every_value_and_interpolates_none() -> None:
    """Thirteen bound parameters, and the derived state among them."""
    artifact_id = uuid4()
    client_id = uuid4()
    script = Script(answers=[Answer("INSERT INTO derived_artifact", ((artifact_id, "pending"),))])

    write_derived_artifact(build_store(script), artifact_of(artifact_id, client_id))

    assert INSERT_ARTIFACT_STATEMENT.count("%s") == ARTIFACT_PARAMETER_COUNT
    assert script.parameters_of(INSERT_ARTIFACT_STATEMENT) == (
        artifact_id,
        "summary",
        client_id,
        "a distilled body",
        DIGEST,
        "distil",
        1,
        MOMENT,
        MOMENT,
        None,
        EmbeddingState.PENDING.value,
        EXPIRY,
        None,
    )


def test_the_embedding_write_binds_the_vector_as_text_with_the_cast_in_the_statement() -> None:
    """The vector is a bound value, the cast is statement text, and both are asserted."""
    artifact_id = uuid4()
    client_id = uuid4()
    script = Script(answers=[Answer("INSERT INTO embedding", ((uuid4(), MOMENT),))])

    write_embedding(build_store(script), embedding_of(artifact_id, client_id))

    assert "%s::VECTOR" in INSERT_EMBEDDING_STATEMENT
    assert INSERT_EMBEDDING_STATEMENT.count("%s") == EMBEDDING_PARAMETER_COUNT
    assert script.parameters_of(INSERT_EMBEDDING_STATEMENT) == (
        artifact_id,
        ArtifactKind.DERIVED_ARTIFACT.value,
        client_id,
        PROVIDER,
        MODEL,
        EMBEDDING_DIMENSION,
        True,
        vector_text(unit_vector()),
        EXPIRY,
    )


def test_a_repeated_vector_for_one_provider_and_model_is_reported_as_stored() -> None:
    """The pair the schema holds unique is a named refusal rather than a leak."""
    script = Script(
        answers=[
            Answer(
                "INSERT INTO embedding",
                error=DriverFailureError(UNIQUE_VIOLATION_STATE, EMBEDDING_UNIQUE_CONSTRAINT),
            )
        ]
    )

    with pytest.raises(StoreError, match="already stored"):
        write_embedding(build_store(script), embedding_of(uuid4(), uuid4()))


def test_a_failure_this_module_does_not_name_propagates_untouched() -> None:
    """A refusal with no name here is not renamed into something it is not."""
    script = Script(
        answers=[Answer("INSERT INTO embedding", error=DriverFailureError("42601", None))]
    )

    with pytest.raises(DriverFailureError):
        write_embedding(build_store(script), embedding_of(uuid4(), uuid4()))


# ---------------------------------------------------------------------------
# The derived state
# ---------------------------------------------------------------------------


def test_a_vector_makes_the_recorded_state_embedded_whatever_was_declared() -> None:
    """The state describes what the write did, not what the caller believed."""
    artifact_id = uuid4()
    client_id = uuid4()
    script = Script(
        answers=[
            Answer("INSERT INTO derived_artifact", ((artifact_id, "embedded"),)),
            Answer("INSERT INTO embedding", ((uuid4(), MOMENT),)),
        ]
    )

    write_derived_artifact(
        build_store(script),
        artifact_of(artifact_id, client_id, state=EmbeddingState.FAILED),
        embedding=embedding_of(artifact_id, client_id),
    )

    bound = script.parameters_of(INSERT_ARTIFACT_STATEMENT)
    assert bound is not None
    assert bound[10] == EmbeddingState.EMBEDDED.value


def test_a_record_claiming_an_embedded_state_with_no_vector_is_refused() -> None:
    """That state would assert an Embedding row the transaction is not writing."""
    artifact_id = uuid4()
    script = Script()

    with pytest.raises(ValueError, match="together with the vector"):
        write_derived_artifact(
            build_store(script),
            artifact_of(artifact_id, uuid4(), state=EmbeddingState.EMBEDDED),
        )

    assert INSERT_ARTIFACT_STATEMENT not in script.statements


def test_a_row_owing_no_vector_keeps_the_absent_state() -> None:
    """A row carrying no embeddable text does not begin owing a vector."""
    artifact_id = uuid4()
    script = Script(
        answers=[Answer("INSERT INTO derived_artifact", ((artifact_id, "not_required"),))]
    )

    written = write_derived_artifact(
        build_store(script),
        artifact_of(artifact_id, uuid4(), state=EmbeddingState.NOT_REQUIRED),
    )

    assert written.embedding_state is EmbeddingState.NOT_REQUIRED
    assert written.embedding_id is None


def test_a_vector_naming_another_artifact_is_refused_and_nothing_is_written() -> None:
    """An Embedding is written for the Artifact it represents and no other."""
    script = Script()

    with pytest.raises(ValueError, match="no other"):
        write_derived_artifact(
            build_store(script),
            artifact_of(uuid4(), uuid4()),
            embedding=embedding_of(uuid4(), uuid4()),
        )

    assert INSERT_ARTIFACT_STATEMENT not in script.statements
    assert INSERT_EMBEDDING_STATEMENT not in script.statements


def test_a_vector_naming_another_tenant_is_refused() -> None:
    """Tenancy travels with the Artifact, so a vector cannot re-attribute one."""
    artifact_id = uuid4()
    script = Script()

    with pytest.raises(ValueError, match="tenant"):
        write_derived_artifact(
            build_store(script),
            artifact_of(artifact_id, uuid4()),
            embedding=embedding_of(artifact_id, uuid4()),
        )

    assert INSERT_ARTIFACT_STATEMENT not in script.statements
    assert INSERT_EMBEDDING_STATEMENT not in script.statements


# ---------------------------------------------------------------------------
# The state transition
# ---------------------------------------------------------------------------


def test_the_transition_names_the_state_column_alone_and_is_scoped_by_tenant() -> None:
    """One column written, and a row of another tenant matches nothing."""
    artifact_id = uuid4()
    client_id = uuid4()
    script = Script(answers=[Answer("UPDATE derived_artifact", (("failed",),))])

    state = mark_embedding_state(build_store(script), artifact_id, client_id, EmbeddingState.FAILED)

    assert state is EmbeddingState.FAILED
    assert MARK_STATE_STATEMENT.count("SET") == 1
    assert "owner_client_id = %s" in MARK_STATE_STATEMENT
    assert script.parameters_of(MARK_STATE_STATEMENT) == (
        EmbeddingState.FAILED.value,
        artifact_id,
        client_id,
    )


def test_a_transition_to_the_absent_state_is_refused() -> None:
    """An obligation to hold a vector is not withdrawn by an update."""
    script = Script()

    with pytest.raises(ValueError, match="owed, present, or unobtainable"):
        mark_embedding_state(build_store(script), uuid4(), uuid4(), EmbeddingState.NOT_REQUIRED)

    assert MARK_STATE_STATEMENT not in script.statements


def test_a_transition_matching_no_row_reports_nothing_rather_than_a_state() -> None:
    """A caller learns the write matched nothing instead of assuming it landed."""
    script = Script(answers=[Answer("UPDATE derived_artifact", ())])

    assert (
        mark_embedding_state(build_store(script), uuid4(), uuid4(), EmbeddingState.EMBEDDED) is None
    )


# ---------------------------------------------------------------------------
# The pending sweep
# ---------------------------------------------------------------------------


def test_the_sweep_spans_both_embeddable_kinds_and_ascends_by_creation() -> None:
    """Two branches, one order, and the state spelled as the enumeration spells it."""
    assert PENDING_STATE == "pending"
    assert SELECT_PENDING_STATEMENT.count("embedding_state = 'pending'") == 2
    assert "FROM ledger" in SELECT_PENDING_STATEMENT
    assert "FROM derived_artifact" in SELECT_PENDING_STATEMENT
    assert "UNION ALL" in SELECT_PENDING_STATEMENT
    assert SELECT_PENDING_STATEMENT.endswith("ORDER BY created_at ASC, id ASC LIMIT %s")
    assert SELECT_PENDING_STATEMENT.count("%s") == 1


def test_the_sweep_frames_no_transaction_and_binds_its_bound() -> None:
    """A read needs no explicit transaction, and the row bound is a parameter."""
    event_id = uuid4()
    artifact_id = uuid4()
    client_id = uuid4()
    later = MOMENT + timedelta(minutes=5)
    script = Script(
        answers=[
            Answer(
                "AS pending",
                (
                    (event_id, "event", client_id, MOMENT),
                    (artifact_id, "derived_artifact", client_id, later),
                ),
            )
        ]
    )

    swept = pending_artifacts(build_store(script), limit=25)

    assert swept == (
        PendingArtifact(
            artifact_id=event_id,
            artifact_kind=ArtifactKind.EVENT,
            client_id=client_id,
            created_at=MOMENT,
        ),
        PendingArtifact(
            artifact_id=artifact_id,
            artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=client_id,
            created_at=later,
        ),
    )
    assert BEGIN_STATEMENT not in script.statements
    assert script.parameters_of(SELECT_PENDING_STATEMENT) == (25,)


def test_a_sweep_bound_outside_the_admitted_range_is_refused() -> None:
    """No caller asks for an unbounded scan of a corpus, or for no rows at all."""
    script = Script()

    with pytest.raises(ValueError, match="at least one row"):
        pending_artifacts(build_store(script), limit=0)
    with pytest.raises(ValueError, match="may not exceed"):
        pending_artifacts(build_store(script), limit=10001)

    assert SELECT_PENDING_STATEMENT not in script.statements


# ---------------------------------------------------------------------------
# The neighbour statement
# ---------------------------------------------------------------------------


def test_the_neighbour_statement_is_the_statement_the_design_states() -> None:
    """Tenancy inside the predicate, L2 ordering, and a projected cosine distance."""
    assert "(e.vec <=> %s::VECTOR) AS cosine_distance" in NEAREST_STATEMENT
    assert "WHERE EXISTS (" in NEAREST_STATEMENT
    assert "b.client_id = ANY (%s::UUID[])" in NEAREST_STATEMENT
    assert "b.superseded_by IS NULL" in NEAREST_STATEMENT
    assert "(%s::FLOAT8 IS NULL OR (e.vec <=> %s::VECTOR) <= %s::FLOAT8)" in NEAREST_STATEMENT
    assert "ORDER BY e.vec <-> %s::VECTOR" in NEAREST_STATEMENT
    assert NEAREST_STATEMENT.endswith("LIMIT %s")
    assert NEAREST_STATEMENT.count("%s") == NEAREST_PARAMETER_COUNT


def test_the_neighbour_query_binds_the_vector_the_clients_and_the_bound() -> None:
    """One statement, seven bound parameters, and no rendered list among them."""
    first = uuid4()
    second = uuid4()
    found = uuid4()
    script = Script(answers=[Answer("FROM embedding AS e", ((found, "event", first, 0.25),))])

    neighbours = nearest(
        build_store(script),
        unit_vector(3),
        permitted_clients=[first, second, first],
        limit=5,
        max_cosine=0.45,
    )

    assert neighbours == (
        Neighbour(
            artifact_id=found,
            artifact_kind=ArtifactKind.EVENT,
            client_id=first,
            cosine_distance=0.25,
        ),
    )
    rendered = vector_text(unit_vector(3))
    assert script.parameters_of(NEAREST_STATEMENT) == (
        rendered,
        [first, second],
        0.45,
        rendered,
        0.45,
        rendered,
        5,
    )


def test_no_ceiling_binds_nothing_and_admits_every_distance() -> None:
    """One statement serves the bounded search and the unbounded one alike."""
    client_id = uuid4()
    script = Script(answers=[Answer("FROM embedding AS e", ())])

    assert nearest(build_store(script), unit_vector(), permitted_clients=[client_id]) == ()

    bound = script.parameters_of(NEAREST_STATEMENT)
    assert bound is not None
    assert bound[2] is None
    assert bound[4] is None


def test_a_ceiling_outside_the_distance_range_is_refused() -> None:
    """A cosine distance lies between zero and two, so a ceiling does too."""
    script = Script()

    with pytest.raises(ValueError, match="cosine ceiling"):
        nearest(
            build_store(script),
            unit_vector(),
            permitted_clients=[uuid4()],
            max_cosine=2.5,
        )

    assert NEAREST_STATEMENT not in script.statements


def test_a_caller_permitted_no_client_sees_nothing_without_a_round_trip() -> None:
    """An empty permitted set admits no row, and proving that costs no statement."""
    script = Script()

    assert nearest(build_store(script), unit_vector(), permitted_clients=[]) == ()
    assert NEAREST_STATEMENT not in script.statements


def test_a_neighbour_row_of_the_wrong_width_is_refused() -> None:
    """A statement and its decoder cannot drift apart silently."""
    script = Script(answers=[Answer("FROM embedding AS e", ((uuid4(), "event"),))])

    with pytest.raises(StoreError, match="column"):
        nearest(build_store(script), unit_vector(), permitted_clients=[uuid4()])


def test_the_store_delegates_the_embedding_surface_to_this_module() -> None:
    """The design names these as store methods, and each one delegates and nothing more.

    The owning module is imported inside each method body, so the import direction
    stays submodule to store; what is asserted here is that the statements the
    delegating method reaches the cluster with are this module's own, and that the
    default bounds are the ones this module declares rather than restated numbers.
    """
    artifact_id = uuid4()
    client_id = uuid4()
    script = Script(
        answers=[
            Answer("INSERT INTO derived_artifact", ((artifact_id, "pending"),)),
            Answer("INSERT INTO embedding", ((uuid4(), MOMENT),)),
            Answer("UPDATE derived_artifact", (("embedded",),)),
            Answer("AS pending", ()),
            Answer("FROM embedding AS e", ()),
        ]
    )
    store = build_store(script)

    store.write_derived_artifact(artifact_of(artifact_id, client_id))
    store.write_embedding(embedding_of(artifact_id, client_id))
    store.mark_embedding_state(artifact_id, client_id, EmbeddingState.EMBEDDED)
    store.pending_artifacts()
    store.nearest(unit_vector(), permitted_clients=[client_id])

    sent = script.statements
    assert INSERT_ARTIFACT_STATEMENT in sent
    assert INSERT_EMBEDDING_STATEMENT in sent
    assert MARK_STATE_STATEMENT in sent
    assert script.parameters_of(SELECT_PENDING_STATEMENT) == (DEFAULT_PENDING_LIMIT,)
    bound = script.parameters_of(NEAREST_STATEMENT)
    assert bound is not None
    assert bound[6] == DEFAULT_NEIGHBOUR_LIMIT


def test_a_read_bound_beyond_the_ceiling_is_refused() -> None:
    """The neighbour bound is a parameter with a ceiling of its own."""
    script = Script()

    with pytest.raises(ValueError, match="may not exceed"):
        nearest(build_store(script), unit_vector(), permitted_clients=[uuid4()], limit=1001)

    assert NEAREST_STATEMENT not in script.statements
