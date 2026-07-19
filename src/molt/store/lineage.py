"""The lineage graph: one guarded insert and the two closure traversals.

Edges point child to parent. A child is always a Derived_Artifact, which the
schema enforces with a reference; a parent is any of the three kinds the
`artifact_ref` view spans, which the schema cannot enforce with a reference at
all. Four claims are load-bearing here, and each is arranged so a caller cannot
lose it by forgetting something.

**The acyclicity guard is part of the inserting statement, not a check before
it.** The insert carries a recursive reachability query over the descendants of
the child it is about to write, and it writes nothing when the proposed parent is
among them. Reading the reachable set and writing the edge are therefore one
statement and one read-write set, which is what makes the guard hold under
concurrency: two inserts that would jointly close a cycle each read the set the
other writes into, so SERIALIZABLE aborts one, the retry re-reads a set that now
holds the committed edge, and the second edge is refused rather than admitted.
A guard implemented as a select followed by an insert would let both commit.

**Parent existence is enforced by a join rather than by a reference.** The parent
identifier is polymorphic across the three Artifact kinds, so no foreign key can
describe it. The insert joins `artifact_ref`, which produces no row for an
identifier no Artifact holds, so the insert writes nothing. The alternative of
three nullable typed columns with three references was rejected in the design
because the descendant traversal would then need a three-way `COALESCE` in its
join predicate, and that defeats the index the traversal is served by. Neither an
Embedding nor a disposable working row appears in the view, so neither can become
a lineage parent, and that too is structural rather than checked here.

**A refusal returns no row, and the two causes are told apart inside the same
transaction.** Zero rows means the guard, the join, or both declined. One
follow-up existence check against the same view distinguishes them: an absent
parent raises the missing-parent error, and anything else raises the cycle error.
The check runs on the transaction's own cursor, so it reads exactly the state the
insert read, and a parent some concurrent transaction inserted without committing
cannot make the answer disagree with the write.

**A traversal deduplicates, and it is not bounded by a row limit.** Both
recursive terms combine with `UNION` rather than `UNION ALL`, so a diamond is
walked once per node instead of once per path, and the traversal terminates even
if an edge ever escaped the guard. Neither traversal takes a row bound: a closure
truncated at some number of rows is not a closure, and the erasure sweep that
selects derived content by lineage would silently leave rows behind. What holds
the time bound instead is the index per direction, `lineage_by_parent` for the
descendant walk and `lineage_by_child` for the ancestor walk, so each recursive
step is a lookup join rather than a scan.

Every statement is a whole module-level literal, every caller-supplied value is a
bound parameter, and no identifier is interpolated anywhere. Both traversals are
seeded from one bound array, so a caller asking about a thousand roots sends one
statement rather than a thousand.

Nothing here frames a transaction of its own beyond what the store's serializable
wrapper provides, and every function that writes is also available in a form
taking a caller's cursor, because an edge is written in the same transaction as
the Derived_Artifact it describes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from molt.errors import LineageCycleError, MissingParentError, StoreError
from molt.models.artifact import LINEAGE_PARENT_KINDS, ArtifactKind
from molt.store import Cursor, MemoryStore

__all__ = [
    "CHILD_REFERENCE_CONSTRAINTS",
    "COMPONENT",
    "EDGE_UNIQUE_CONSTRAINT",
    "FOREIGN_KEY_VIOLATION_STATE",
    "INSERT_EDGE_STATEMENT",
    "PARENT_EXISTS_STATEMENT",
    "SELECT_ANCESTORS_STATEMENT",
    "SELECT_DESCENDANTS_STATEMENT",
    "UNIQUE_VIOLATION_STATE",
    "LineageNode",
    "ParentRef",
    "ancestors_of",
    "descendants_of",
    "insert_edge",
    "insert_edges",
    "insert_lineage_edge",
    "insert_lineage_edges",
    "select_ancestors",
    "select_descendants",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The states the cluster reports for the two write failures this module names.
# They are read off the failure rather than inferred from a type, because the
# driver is imported lazily and its exception classes are not nameable here.
FOREIGN_KEY_VIOLATION_STATE: Final[str] = "23503"
UNIQUE_VIOLATION_STATE: Final[str] = "23505"

# The attribute names a driver may carry the state under, matching the pair the
# transaction wrapper reads.
_STATE_ATTRIBUTES: Final[tuple[str, ...]] = ("sqlstate", "pgcode")

# The reference on the child column, under both names it has held: the one the
# second migration generation gave it when it re-created the constraint with a
# cascading delete, and the one the cluster generated for the original.
CHILD_REFERENCE_CONSTRAINTS: Final[frozenset[str]] = frozenset(
    {"lineage_edge_child_fk", "lineage_edge_child_id_fkey"}
)

# The constraint making the pair of child and parent unique, so a restatement of
# an edge already recorded is reported as that rather than as something else.
EDGE_UNIQUE_CONSTRAINT: Final[str] = "lineage_edge_unique"

# How many columns each row shape carries, checked before a row is read so a
# statement and its decoder cannot drift apart silently.
_NODE_ROW_WIDTH: Final[int] = 1
_ANCESTOR_ROW_WIDTH: Final[int] = 2

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The guarded insert. The recursive term walks the descendants of the child, so
# `reachable` is everything the child can already reach in the parent-to-child
# direction; a proposed parent found in that set would close a cycle. The
# existence term is the join that stands in for the reference the polymorphic
# parent column cannot carry. The guard refuses the self edge explicitly as well,
# which the schema's own check also refuses, because the shortest cycle must be
# refused by the same statement that refuses the longer ones rather than only by
# a constraint the disambiguation would then have to read a state off.
#
# Both terms are cross-joined into the inserting select, so the insert writes one
# row when the guard and the existence check both produce a row, and no row
# otherwise. Zero rows is the refusal, and it is not an error state: the
# transaction stays healthy and the follow-up check runs on the same cursor.
INSERT_EDGE_STATEMENT: Final[str] = (
    "WITH RECURSIVE reachable AS ("
    "SELECT child_id AS node FROM lineage_edge WHERE parent_id = %s "
    "UNION "
    "SELECT e.child_id FROM lineage_edge AS e "
    "JOIN reachable AS r ON e.parent_id = r.node"
    "), "
    "parent_exists AS ("
    "SELECT id FROM artifact_ref WHERE id = %s AND kind = %s"
    "), "
    "guard AS ("
    "SELECT 1 AS ok WHERE %s::UUID != %s::UUID "
    "AND NOT EXISTS (SELECT 1 FROM reachable WHERE node = %s::UUID)"
    ") "
    "INSERT INTO lineage_edge (child_id, parent_id, parent_kind, derivation_method) "
    "SELECT %s, %s, %s, %s FROM guard, parent_exists "
    "RETURNING id"
)

# The one follow-up check a refusal is disambiguated with. It asks the same view
# the insert joined, on the same cursor and inside the same transaction, so the
# answer describes the state the insert saw.
PARENT_EXISTS_STATEMENT: Final[str] = "SELECT 1 FROM artifact_ref WHERE id = %s AND kind = %s"

# The descendant closure, seeded from one bound array. Each recursive step joins
# the edge table on the parent column, which `lineage_by_parent` serves, so a
# step is a lookup rather than a scan. The seed rows are not part of the result:
# the answer is what the roots reach, and a caller holding the roots already has
# them.
SELECT_DESCENDANTS_STATEMENT: Final[str] = (
    "WITH RECURSIVE seed AS ("
    "SELECT unnest(%s::UUID[]) AS node"
    "), "
    "descendants AS ("
    "SELECT e.child_id AS node FROM lineage_edge AS e "
    "JOIN seed AS s ON e.parent_id = s.node "
    "UNION "
    "SELECT e.child_id FROM lineage_edge AS e "
    "JOIN descendants AS d ON e.parent_id = d.node"
    ") "
    "SELECT node FROM descendants"
)

# The mirror traversal, seeded the same way and served by `lineage_by_child`. It
# carries the parent kind alongside the identifier, because a parent is
# polymorphic and a caller reading an ancestor needs to know which table holds
# it. The derivation method is on the edge rather than on the node, so it is not
# selected here: two edges into one ancestor may name two methods, and reporting
# one of them would be a choice the traversal has no basis to make.
SELECT_ANCESTORS_STATEMENT: Final[str] = (
    "WITH RECURSIVE seed AS ("
    "SELECT unnest(%s::UUID[]) AS node"
    "), "
    "ancestors AS ("
    "SELECT e.parent_id AS node, e.parent_kind AS kind FROM lineage_edge AS e "
    "JOIN seed AS s ON e.child_id = s.node "
    "UNION "
    "SELECT e.parent_id, e.parent_kind FROM lineage_edge AS e "
    "JOIN ancestors AS a ON e.child_id = a.node"
    ") "
    "SELECT node, kind FROM ancestors"
)

# The labels the transactions of this module appear under in a log record and in
# the note an exhausted retry attaches.
_INSERT_LABEL: Final[str] = "lineage_insert"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParentRef:
    """One parent an edge names, with the derivation that produced the child.

    The derivation method travels with the parent rather than with the child
    because it is a property of the edge: one Derived_Artifact may be reached
    from one parent by summarising and from another by generalising, and the
    schema stores the method per edge for exactly that reason.

    The kind is checked here against the three kinds a parent may have, so an
    Embedding named as a parent is refused before a statement is sent rather
    than by producing no join row and being reported as an absent parent.
    """

    parent_id: UUID
    parent_kind: ArtifactKind
    derivation_method: str

    def __post_init__(self) -> None:
        kind = ArtifactKind(self.parent_kind)
        if kind not in LINEAGE_PARENT_KINDS:
            raise ValueError("a lineage parent is an event, a session, or a derived artifact")
        if not self.derivation_method:
            raise ValueError("a lineage edge carries the derivation method that produced the child")


@dataclass(frozen=True, slots=True)
class LineageNode:
    """One Artifact an ancestor traversal reached, with the kind that holds it."""

    id: UUID
    kind: ArtifactKind


# ---------------------------------------------------------------------------
# The guarded insert
# ---------------------------------------------------------------------------


def insert_edge(cursor: Cursor, child_id: UUID, parent: ParentRef) -> UUID:
    """Write one edge on a caller's cursor, refusing a cycle and an absent parent.

    This is the form an Artifact write composes: Requirement 11.2 puts the edges
    of a Derived_Artifact in the same transaction as the Artifact itself, so the
    caller frames the transaction and hands its cursor here.

    Args:
        cursor: The cursor the caller's transaction is running on.
        child_id: The Derived_Artifact the edge points from.
        parent: The Artifact the edge points to, its kind, and the derivation
            method that produced the child from it.

    Returns:
        The identifier of the edge that was written.

    Raises:
        LineageCycleError: The edge would have made the graph cyclic, the self
            edge included. Nothing was written.
        MissingParentError: The parent identifier and kind name no Artifact, or
            the child identifier names no Derived_Artifact. Nothing was written.
        StoreError: The edge is already recorded, so the pair the schema holds
            unique would have been repeated.
    """
    kind = ArtifactKind(parent.parent_kind).value
    try:
        cursor.execute(
            INSERT_EDGE_STATEMENT,
            (
                child_id,
                parent.parent_id,
                kind,
                child_id,
                parent.parent_id,
                parent.parent_id,
                child_id,
                parent.parent_id,
                kind,
                parent.derivation_method,
            ),
        )
    except Exception as error:
        translated = _translated(error, child_id, parent)
        if translated is None:
            raise
        raise translated from error
    row = cursor.fetchone()
    if row is not None:
        return _as_uuid(_column(row, 0, _NODE_ROW_WIDTH))
    raise _refusal(cursor, child_id, parent)


def insert_edges(
    cursor: Cursor,
    child_id: UUID,
    parents: Iterable[ParentRef],
) -> tuple[UUID, ...]:
    """Write one edge per parent on a caller's cursor, in the order given.

    Every edge goes through the same guarded statement, so an edge that would
    close a cycle against an edge this same transaction wrote a moment earlier is
    refused by the reachability set that already holds it. The
    first refusal raises, and because the caller's transaction is abandoned by
    the wrapper on a raised failure, either every edge of the Artifact lands or
    none does.
    """
    return tuple(insert_edge(cursor, child_id, parent) for parent in parents)


def insert_lineage_edge(store: MemoryStore, child_id: UUID, parent: ParentRef) -> UUID:
    """Write one edge in a transaction of its own.

    The transaction is framed by the store's serializable wrapper, so the bounded
    jittered retry of a conflict is inherited rather than restated here. That
    retry is what makes the guard hold under concurrency: the conflicting attempt
    runs again and re-reads a reachable set that now holds the edge that won.
    """

    def body(opened: Cursor) -> UUID:
        return insert_edge(opened, child_id, parent)

    return store.in_serializable(body, label=_INSERT_LABEL)


def insert_lineage_edges(
    store: MemoryStore,
    child_id: UUID,
    parents: Sequence[ParentRef],
) -> tuple[UUID, ...]:
    """Write every edge of one child in a single transaction.

    A caller with no Artifact write to compose into uses this. A caller writing
    the Artifact uses `insert_edges` on its own cursor instead, because the edges
    belong in the Artifact's transaction rather than in one of their own.
    """

    def body(opened: Cursor) -> tuple[UUID, ...]:
        return insert_edges(opened, child_id, parents)

    return store.in_serializable(body, label=_INSERT_LABEL)


def _refusal(cursor: Cursor, child_id: UUID, parent: ParentRef) -> StoreError:
    """The failure a zero-row insert stands for, told apart by one existence check.

    The check runs on the transaction's own cursor, which is what makes the
    answer describe the state the insert read rather than a later state. An
    absent parent is the missing-parent error; everything else the guard could
    have declined for is a cycle, the self edge included.
    """
    kind = ArtifactKind(parent.parent_kind).value
    cursor.execute(PARENT_EXISTS_STATEMENT, (parent.parent_id, kind))
    if cursor.fetchone() is None:
        return MissingParentError(
            f"the lineage edge from {child_id} names the parent {parent.parent_id} of kind "
            f"{kind}, which no Artifact holds, so nothing was written"
        )
    return LineageCycleError(
        f"the lineage edge from {child_id} to {parent.parent_id} would have made the "
        "lineage graph cyclic, so nothing was written"
    )


def _translated(
    error: BaseException,
    child_id: UUID,
    parent: ParentRef,
) -> StoreError | None:
    """The failure to raise for a driver refusal this module has a name for.

    A state this module does not name returns nothing and the original failure
    propagates untouched, so a conflict still reaches the retry wrapper's own
    handling and a constraint failure is never renamed into something it is not.
    """
    state = _state_of(error)
    if state == FOREIGN_KEY_VIOLATION_STATE:
        if _constraint_of(error) in CHILD_REFERENCE_CONSTRAINTS:
            return MissingParentError(
                f"the lineage edge names the child {child_id}, which no Derived_Artifact "
                "holds, so nothing was written"
            )
        return MissingParentError(
            f"the lineage edge from {child_id} to {parent.parent_id} named a row that does "
            "not exist, so nothing was written"
        )
    if state == UNIQUE_VIOLATION_STATE and _constraint_of(error) == EDGE_UNIQUE_CONSTRAINT:
        return StoreError(
            f"the lineage edge from {child_id} to {parent.parent_id} is already recorded, "
            "and the pair of child and parent is held unique, so nothing was written"
        )
    return None


def _state_of(error: BaseException) -> str | None:
    """The state a driver failure carries, or None when it carries none."""
    for attribute in _STATE_ATTRIBUTES:
        state = getattr(error, attribute, None)
        if isinstance(state, str):
            return state
    return None


def _constraint_of(error: BaseException) -> str | None:
    """The constraint a driver failure names, or None when it names none."""
    diagnostic: object = getattr(error, "diag", None)
    if diagnostic is None:
        return None
    name: object = getattr(diagnostic, "constraint_name", None)
    return name if isinstance(name, str) else None


# ---------------------------------------------------------------------------
# The traversals
# ---------------------------------------------------------------------------


def select_descendants(cursor: Cursor, roots: Iterable[UUID]) -> tuple[UUID, ...]:
    """Every Artifact reachable from the roots in the parent-to-child direction.

    The roots are sent as one bound array, so the cost of asking about many roots
    is one statement. An empty set of roots reaches nothing, and that answer is
    returned without a round trip rather than by sending an empty array.

    The result holds no root and no duplicate: the recursive terms combine with
    `UNION`, so a node reachable by several paths appears once, which is what
    bounds the walk on a diamond.
    """
    seeds = _seeds(roots)
    if not seeds:
        return ()
    cursor.execute(SELECT_DESCENDANTS_STATEMENT, (seeds,))
    return tuple(_as_uuid(_column(row, 0, _NODE_ROW_WIDTH)) for row in cursor.fetchall())


def select_ancestors(cursor: Cursor, artifact_ids: Iterable[UUID]) -> tuple[LineageNode, ...]:
    """Every Artifact the named Artifacts were derived from, however deep.

    The mirror of the descendant walk, seeded the same way and deduplicating the
    same way. Each result carries the kind the edge recorded for it, because a
    parent may be an Event, a Session, or another Derived_Artifact and a caller
    reading one needs to know which.
    """
    seeds = _seeds(artifact_ids)
    if not seeds:
        return ()
    cursor.execute(SELECT_ANCESTORS_STATEMENT, (seeds,))
    return tuple(_node_of(row) for row in cursor.fetchall())


def descendants_of(store: MemoryStore, roots: Iterable[UUID]) -> tuple[UUID, ...]:
    """Run the descendant closure on a leased connection, framing no transaction."""

    def body(opened: Cursor) -> tuple[UUID, ...]:
        return select_descendants(opened, roots)

    return store.read(body)


def ancestors_of(store: MemoryStore, artifact_ids: Iterable[UUID]) -> tuple[LineageNode, ...]:
    """Run the ancestor closure on a leased connection, framing no transaction."""

    def body(opened: Cursor) -> tuple[LineageNode, ...]:
        return select_ancestors(opened, artifact_ids)

    return store.read(body)


# ---------------------------------------------------------------------------
# Parameters and row decoding
# ---------------------------------------------------------------------------


def _seeds(identifiers: Iterable[UUID]) -> list[UUID]:
    """The seed array to bind, with repeated identifiers collapsed.

    A list rather than a tuple because a list is what the driver renders as an
    array. Duplicates are dropped here rather than left to the traversal, since
    a repeated root would seed the same walk twice for nothing.
    """
    return list(dict.fromkeys(identifiers))


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _node_of(row: Sequence[object]) -> LineageNode:
    """Build one ancestor from a selected row."""
    return LineageNode(
        id=_as_uuid(_column(row, 0, _ANCESTOR_ROW_WIDTH)),
        kind=ArtifactKind(_as_str(row[1])),
    )


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a value whose type is not the one the schema declares.

    The type is named and the value is not, because a column of this schema may
    hold memory content and a message naming the fault belongs in a log record
    while the content does not.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


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
