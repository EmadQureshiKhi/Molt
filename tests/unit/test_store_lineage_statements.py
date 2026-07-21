"""Unit tests for the lineage statements, the guard, and the two traversals.

Nothing here opens a socket. A scripted cursor answers each statement from a
script and keeps what it was sent, so the claims below are asserted by reading
the statements the module produced. The claims that need a cluster to be
meaningful, that the reachability check really refuses a cycle and that the join
really refuses an absent parent, are asserted in the instance-backed module.

Five properties of the shape are checked here.

The guard travels inside the inserting statement. The reachability query, the
existence join, and the insert are one statement, so the read set and the write
are one and the same transaction's, which is what makes the guard survive
concurrency rather than merely describe an intention.

A refusal costs one follow-up statement, and that statement runs inside the same
transaction as the insert it disambiguates. No commit happens on the refused
path, and the follow-up is sent after the insert and before the transaction is
abandoned.

The two causes of a refusal are told apart rather than collapsed. An absent
parent is the missing-parent error and everything else the guard could have
declined for is the cycle error, and the difference is decided by what the
follow-up check found.

Both traversals deduplicate and neither is bounded by a row limit. `UNION`
appears and `UNION ALL` does not, and no traversal statement carries a limit,
because a closure truncated at some number of rows is not a closure.

Every caller-supplied value is bound. Each seed set is one bound array, the
insert binds ten parameters and interpolates none, and a traversal over no seed
sends no statement at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.errors import LineageCycleError, MissingParentError, StoreError
from molt.models.artifact import ArtifactKind
from molt.store import Connection, MemoryStore
from molt.store.lineage import (
    EDGE_UNIQUE_CONSTRAINT,
    FOREIGN_KEY_VIOLATION_STATE,
    INSERT_EDGE_STATEMENT,
    PARENT_EXISTS_STATEMENT,
    SELECT_ANCESTORS_STATEMENT,
    SELECT_DESCENDANTS_STATEMENT,
    UNIQUE_VIOLATION_STATE,
    LineageNode,
    ParentRef,
    ancestors_of,
    descendants_of,
    insert_lineage_edge,
    insert_lineage_edges,
)
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
)

# The two traversal statements, so the deduplication and bounding claims are
# asserted over the whole set rather than over the one a test happened to call.
TRAVERSAL_STATEMENTS: Final[tuple[str, ...]] = (
    SELECT_DESCENDANTS_STATEMENT,
    SELECT_ANCESTORS_STATEMENT,
)

# The seed form both traversals share: one bound array, expanded in the statement.
SEED_FRAGMENT: Final[str] = "SELECT unnest(%s::UUID[]) AS node"

# How many parameters the guarded insert binds.
INSERT_PARAMETER_COUNT: Final[int] = 10

# The derivation method every parent reference in this module carries.
METHOD: Final[str] = "distil"


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


def parent(identifier: UUID, kind: ArtifactKind = ArtifactKind.DERIVED_ARTIFACT) -> ParentRef:
    """A parent reference of one kind, carrying this module's derivation method."""
    return ParentRef(parent_id=identifier, parent_kind=kind, derivation_method=METHOD)


# ---------------------------------------------------------------------------
# The shape of the guarded insert
# ---------------------------------------------------------------------------


def test_the_guard_travels_inside_the_inserting_statement() -> None:
    """Reachability, existence, and the write are one statement."""
    assert INSERT_EDGE_STATEMENT.startswith("WITH RECURSIVE reachable AS (")
    assert "SELECT id FROM artifact_ref WHERE id = %s AND kind = %s" in INSERT_EDGE_STATEMENT
    assert "NOT EXISTS (SELECT 1 FROM reachable WHERE node = %s::UUID)" in INSERT_EDGE_STATEMENT
    assert "%s::UUID != %s::UUID" in INSERT_EDGE_STATEMENT
    assert "INSERT INTO lineage_edge" in INSERT_EDGE_STATEMENT
    assert INSERT_EDGE_STATEMENT.endswith("RETURNING id")
    assert "UNION ALL" not in INSERT_EDGE_STATEMENT
    assert INSERT_EDGE_STATEMENT.count("%s") == INSERT_PARAMETER_COUNT


def test_the_insert_binds_every_value_it_writes() -> None:
    """Ten bound parameters, no identifier and no value in statement text."""
    child = uuid4()
    ancestor = uuid4()
    written = uuid4()
    script = Script(answers=[Answer("INSERT INTO lineage_edge", ((written,),))])

    assert insert_lineage_edge(build_store(script), child, parent(ancestor)) == written

    bound = script.parameters_of(INSERT_EDGE_STATEMENT)
    assert bound == (
        child,
        ancestor,
        ArtifactKind.DERIVED_ARTIFACT.value,
        child,
        ancestor,
        ancestor,
        child,
        ancestor,
        ArtifactKind.DERIVED_ARTIFACT.value,
        METHOD,
    )


def test_the_insert_runs_in_one_serializable_transaction() -> None:
    """The write reaches the cluster through the serializable wrapper alone."""
    script = Script(answers=[Answer("INSERT INTO lineage_edge", ((uuid4(),),))])

    insert_lineage_edge(build_store(script), uuid4(), parent(uuid4()))

    statements = script.statements
    assert BEGIN_STATEMENT in statements
    assert SERIALIZABLE_STATEMENT in statements
    assert statements.index(SERIALIZABLE_STATEMENT) < statements.index(INSERT_EDGE_STATEMENT)
    assert statements.index(INSERT_EDGE_STATEMENT) < statements.index(COMMIT_STATEMENT)


def test_every_edge_of_one_child_is_written_in_one_transaction() -> None:
    """One transaction, one insert per parent, in the order the caller gave them."""
    child = uuid4()
    first = uuid4()
    second = uuid4()
    script = Script(
        answers=[
            Answer("INSERT INTO lineage_edge", ((uuid4(),),)),
            Answer("INSERT INTO lineage_edge", ((uuid4(),),)),
        ]
    )

    written = insert_lineage_edges(
        build_store(script), child, (parent(first), parent(second, ArtifactKind.EVENT))
    )

    assert len(written) == 2
    statements = script.statements
    assert statements.count(BEGIN_STATEMENT) == 1
    assert statements.count(INSERT_EDGE_STATEMENT) == 2
    inserts = [params for query, params in script.sent if query == INSERT_EDGE_STATEMENT]
    assert inserts[0] is not None
    assert inserts[1] is not None
    assert inserts[0][1] == first
    assert inserts[1][1] == second
    assert inserts[1][2] == ArtifactKind.EVENT.value


# ---------------------------------------------------------------------------
# Telling the two refusals apart
# ---------------------------------------------------------------------------


def test_a_refused_insert_asks_one_follow_up_inside_the_same_transaction() -> None:
    """No commit, one existence check, sent after the insert and before the abandon."""
    script = Script(answers=[Answer("INSERT INTO lineage_edge", ())])

    with pytest.raises(MissingParentError):
        insert_lineage_edge(build_store(script), uuid4(), parent(uuid4()))

    statements = script.statements
    assert statements.count(PARENT_EXISTS_STATEMENT) == 1
    assert COMMIT_STATEMENT not in statements
    assert statements.index(INSERT_EDGE_STATEMENT) < statements.index(PARENT_EXISTS_STATEMENT)
    assert statements.index(PARENT_EXISTS_STATEMENT) < statements.index(ROLLBACK_STATEMENT)


def test_an_absent_parent_is_the_missing_parent_error() -> None:
    """The follow-up found no parent, so the absent parent is what is reported."""
    ancestor = uuid4()
    script = Script(
        answers=[
            Answer("INSERT INTO lineage_edge", ()),
            Answer("FROM artifact_ref", ()),
        ]
    )

    with pytest.raises(MissingParentError, match="no Artifact holds"):
        insert_lineage_edge(build_store(script), uuid4(), parent(ancestor, ArtifactKind.SESSION))

    assert script.parameters_of(PARENT_EXISTS_STATEMENT) == (
        ancestor,
        ArtifactKind.SESSION.value,
    )


def test_a_present_parent_makes_the_refusal_a_cycle() -> None:
    """The follow-up found the parent, so the guard is what declined."""
    script = Script(
        answers=[
            Answer("INSERT INTO lineage_edge", ()),
            Answer("FROM artifact_ref", ((1,),)),
        ]
    )

    with pytest.raises(LineageCycleError, match="cyclic"):
        insert_lineage_edge(build_store(script), uuid4(), parent(uuid4()))


def test_a_repeated_edge_is_reported_as_already_recorded() -> None:
    """The unique pair is a named refusal rather than a driver failure leaking out."""
    script = Script(
        answers=[
            Answer(
                "INSERT INTO lineage_edge",
                error=DriverFailureError(UNIQUE_VIOLATION_STATE, EDGE_UNIQUE_CONSTRAINT),
            )
        ]
    )

    with pytest.raises(StoreError, match="already recorded"):
        insert_lineage_edge(build_store(script), uuid4(), parent(uuid4()))


def test_an_absent_child_is_reported_as_a_missing_reference() -> None:
    """The child column carries a reference, and its violation names the child."""
    script = Script(
        answers=[
            Answer(
                "INSERT INTO lineage_edge",
                error=DriverFailureError(FOREIGN_KEY_VIOLATION_STATE, "lineage_edge_child_fk"),
            )
        ]
    )

    with pytest.raises(MissingParentError, match="no Derived_Artifact"):
        insert_lineage_edge(build_store(script), uuid4(), parent(uuid4()))


def test_a_failure_this_module_does_not_name_propagates_untouched() -> None:
    """A refusal with no name here is not renamed into something it is not."""
    script = Script(
        answers=[Answer("INSERT INTO lineage_edge", error=DriverFailureError("42601", None))]
    )

    with pytest.raises(DriverFailureError):
        insert_lineage_edge(build_store(script), uuid4(), parent(uuid4()))


# ---------------------------------------------------------------------------
# The traversals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("statement", TRAVERSAL_STATEMENTS)
def test_a_traversal_deduplicates_and_carries_no_row_bound(statement: str) -> None:
    """`UNION` rather than `UNION ALL`, and no limit on a closure."""
    assert " UNION " in statement
    assert "UNION ALL" not in statement
    assert "LIMIT" not in statement.upper()
    assert statement.startswith("WITH RECURSIVE seed AS (")
    assert SEED_FRAGMENT in statement
    assert statement.count("%s") == 1


def test_the_descendant_traversal_walks_the_parent_column() -> None:
    """Each step joins on the parent column, which is the index for this direction."""
    assert "JOIN seed AS s ON e.parent_id = s.node" in SELECT_DESCENDANTS_STATEMENT
    assert "JOIN descendants AS d ON e.parent_id = d.node" in SELECT_DESCENDANTS_STATEMENT


def test_the_ancestor_traversal_walks_the_child_column_and_carries_the_kind() -> None:
    """The mirror direction, with the polymorphic kind selected alongside the node."""
    assert "JOIN seed AS s ON e.child_id = s.node" in SELECT_ANCESTORS_STATEMENT
    assert "JOIN ancestors AS a ON e.child_id = a.node" in SELECT_ANCESTORS_STATEMENT
    assert SELECT_ANCESTORS_STATEMENT.endswith("SELECT node, kind FROM ancestors")


def test_a_traversal_sends_one_statement_with_one_bound_array() -> None:
    """Many seeds cost one statement, and a repeated seed is collapsed."""
    first = uuid4()
    second = uuid4()
    reached = uuid4()
    script = Script(answers=[Answer("FROM descendants", ((reached,),))])

    assert descendants_of(build_store(script), [first, second, first]) == (reached,)

    bound = script.parameters_of(SELECT_DESCENDANTS_STATEMENT)
    assert bound is not None
    assert bound[0] == [first, second], "the seed array is bound, deduplicated, and in order"
    assert isinstance(bound[0], list), "an array parameter is bound as a list"


def test_a_traversal_frames_no_transaction() -> None:
    """A read needs no explicit transaction, so none is opened for one."""
    script = Script(answers=[Answer("FROM ancestors", ())])

    ancestors_of(build_store(script), [uuid4()])

    assert BEGIN_STATEMENT not in script.statements


def test_a_traversal_over_no_seed_sends_no_statement() -> None:
    """An empty seed set reaches nothing, and asking costs no round trip."""
    script = Script()
    store = build_store(script)

    assert descendants_of(store, []) == ()
    assert ancestors_of(store, []) == ()
    assert SELECT_DESCENDANTS_STATEMENT not in script.statements
    assert SELECT_ANCESTORS_STATEMENT not in script.statements


def test_ancestor_rows_decode_to_nodes_carrying_their_kind() -> None:
    """Each ancestor arrives with the kind of the table that holds it."""
    event_id = uuid4()
    session_id = uuid4()
    script = Script(
        answers=[
            Answer(
                "FROM ancestors",
                ((event_id, "event"), (session_id, "session")),
            )
        ]
    )

    reached = ancestors_of(build_store(script), [uuid4()])

    assert reached == (
        LineageNode(id=event_id, kind=ArtifactKind.EVENT),
        LineageNode(id=session_id, kind=ArtifactKind.SESSION),
    )


def test_a_row_of_the_wrong_width_is_refused() -> None:
    """A statement and its decoder cannot drift apart silently."""
    script = Script(answers=[Answer("FROM ancestors", ((uuid4(),),))])

    with pytest.raises(StoreError, match="column"):
        ancestors_of(build_store(script), [uuid4()])


# ---------------------------------------------------------------------------
# The parent reference
# ---------------------------------------------------------------------------


def test_a_parent_reference_refuses_a_kind_no_artifact_can_be_a_parent_of() -> None:
    """An Embedding is derived content and is never a lineage parent."""
    with pytest.raises(ValueError, match="lineage parent"):
        ParentRef(
            parent_id=uuid4(),
            parent_kind=ArtifactKind.EMBEDDING,
            derivation_method=METHOD,
        )


def test_a_parent_reference_requires_the_derivation_method() -> None:
    """The method that produced the child is stored on every edge."""
    with pytest.raises(ValueError, match="derivation method"):
        ParentRef(
            parent_id=uuid4(),
            parent_kind=ArtifactKind.DERIVED_ARTIFACT,
            derivation_method="",
        )
