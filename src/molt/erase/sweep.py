"""Phase one of an erasure run: the explicit sweep, as six set-based statements.

The sweep answers one question — which stored rows does this tenant's content
reach — and it answers it entirely inside the cluster. No candidate identifier
crosses the wire, so the cost of sweeping a hundred thousand Artifacts is six
statements rather than a hundred thousand round trips, and a crash mid-sweep
leaves a partially populated candidate set that the same statements rebuild
unchanged when they run again.

Six claims shape the statements here, and each is arranged so a caller cannot
lose it by forgetting something.

**Every candidate carries the reason it was selected, and the reason is the
statement that selected it.** The candidate table admits six reason values and
this module writes five of them; the sixth belongs to the semantic phase that
extends the set afterwards. A reason is what makes a disposition explicable
later: a verifier reading the evidence can see that an Artifact was reached
because a Session scope covered it, or because a lineage edge led to it, rather
than having to re-derive the sweep to find out.

**Binding selection is resolved through the current-attribution query, never
through the version history.** Attribution is an immutable version history: a
binding that was superseded away from this tenant is a statement about the past
and not a live claim, and a binding superseded *towards* this tenant is the live
claim whatever the earlier versions said. So the binding statement admits exactly
the rows whose successor reference is null. That predicate is what makes a
superseded version neither widen the sweep — an Artifact this tenant used to be
attributed with is not swept — nor narrow it, since the current version is
selected regardless of how many closed versions stand behind it.

**A Learned_Procedure below the recall floor is reached explicitly rather than
inherited from recall.** Recall excludes such a procedure and the store retains
it, which is exactly the shape in which content goes missing from an erasure: the
sweep that reused recall's predicate would leave behind the procedures recall had
already stopped showing. So the floor is read from the configured policy and bound
as a parameter, and the statement selects below-floor procedures of this tenant
directly. It overlaps the binding statement by construction and the conflict
clause absorbs the overlap; the overlap is the cheap direction of the error, and
absence would not be recoverable from the evidence.

**Order is part of the meaning.** The lineage statement seeds from the candidate
set the earlier statements have already written, and the embedding statement seeds
from everything before it, so "descendants of everything selected" and
"embeddings of everything selected" are facts about the set rather than about one
statement's output. Running the six in any other order would produce a smaller
set and no error.

**A pending Embedding never hides an Artifact, and how many there are is on the
record.** Selection is by identity and by lineage rather than by the presence of a
vector, so an Artifact still waiting to be embedded is swept like any other. Their
count is recorded on the run row, because a certificate that omitted it would
overstate what the semantic phase could have found: no vector means no
nearest-neighbour comparison, and the number of Artifacts in that state is the
measure of that gap.

**Every statement is replayable.** Each insert names no conflict target and does
nothing on conflict, so a serialization conflict that re-runs the whole
transaction body rewrites the same rows to the same values. The run-session
capture behaves the same way, and the count update is idempotent because it
assigns a value the statement computes rather than an increment.

Every statement is a whole module-level literal, every caller-supplied value is a
bound parameter, and no identifier and no domain value is ever interpolated: the
Artifact kinds, the selection reasons, the procedure kind, and the pending
embedding state all travel as parameters drawn from the models. Nothing here
frames a transaction of its own beyond what the store's serializable wrapper
provides, and the phase marker the run moves to next is the orchestration's to
write rather than this module's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from molt.confidence import ConfidencePolicy
from molt.errors import StoreError
from molt.models.artifact import ArtifactKind, DerivedArtifactKind
from molt.models.event import EmbeddingState
from molt.store import Cursor, MemoryStore

__all__ = [
    "COMPONENT",
    "COUNT_BY_REASON_QUERY",
    "INSERT_BELOW_FLOOR_PROCEDURES_STATEMENT",
    "INSERT_CURRENT_BINDINGS_STATEMENT",
    "INSERT_LINEAGE_DESCENDANTS_STATEMENT",
    "INSERT_RUN_SESSIONS_STATEMENT",
    "INSERT_SCOPED_EVENTS_STATEMENT",
    "INSERT_SCOPED_SESSIONS_STATEMENT",
    "INSERT_SELECTED_EMBEDDINGS_STATEMENT",
    "REASON_CLIENT_BINDING",
    "REASON_EMBEDDING_OF_SELECTED",
    "REASON_EVENT_OF_SCOPED_SESSION",
    "REASON_LINEAGE_DESCENDANT",
    "REASON_SESSION_SCOPE",
    "RECORD_UNEMBEDDED_COUNT_STATEMENT",
    "SWEEP_REASONS",
    "SweepCounts",
    "SweepResult",
    "count_run_sessions",
    "record_unembedded_count",
    "run_sweep",
    "select_reason_counts",
    "sweep",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "erase"

# The five reasons the explicit sweep records. The sixth value the candidate table
# admits is the semantic phase's and is deliberately absent here, so a candidate
# this module wrote is distinguishable from one adjudication admitted.
REASON_SESSION_SCOPE: Final[str] = "session_scope"
REASON_EVENT_OF_SCOPED_SESSION: Final[str] = "event_of_scoped_session"
REASON_CLIENT_BINDING: Final[str] = "client_binding"
REASON_LINEAGE_DESCENDANT: Final[str] = "lineage_descendant"
REASON_EMBEDDING_OF_SELECTED: Final[str] = "embedding_of_selected"

# In the order the statements run, which is the order the set is built in: a later
# reason's statement reads the rows the earlier ones wrote.
SWEEP_REASONS: Final[tuple[str, ...]] = (
    REASON_SESSION_SCOPE,
    REASON_EVENT_OF_SCOPED_SESSION,
    REASON_CLIENT_BINDING,
    REASON_LINEAGE_DESCENDANT,
    REASON_EMBEDDING_OF_SELECTED,
)

# The kinds and the states the statements bind rather than state. Each is quoted
# from the model that owns it, so the values this module sends are the values the
# schema's own checks name.
_SESSION_KIND: Final[str] = ArtifactKind.SESSION.value
_EVENT_KIND: Final[str] = ArtifactKind.EVENT.value
_DERIVED_KIND: Final[str] = ArtifactKind.DERIVED_ARTIFACT.value
_EMBEDDING_KIND: Final[str] = ArtifactKind.EMBEDDING.value
_PROCEDURE_KIND: Final[str] = DerivedArtifactKind.LEARNED_PROCEDURE.value
_PENDING_EMBEDDING: Final[str] = EmbeddingState.PENDING.value

# How many columns each row shape carries, checked before a row is read so a
# statement and its decoder cannot drift apart silently.
_REASON_ROW_WIDTH: Final[int] = 2
_COUNT_ROW_WIDTH: Final[int] = 1

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The Sessions the tenant owns. A Session carries no content digest of its own --
# what it holds is the Events beneath it -- so the digest column is left absent
# rather than filled with something derived.
INSERT_SCOPED_SESSIONS_STATEMENT: Final[str] = (
    "INSERT INTO erasure_candidate "
    "(run_id, artifact_id, artifact_kind, content_digest, selection_reason) "
    "SELECT %s, s.id, %s, NULL, %s FROM session AS s WHERE s.client_id = %s "
    "ON CONFLICT DO NOTHING"
)

# The Events of those Sessions. The scope is the Session's tenant rather than the
# Event's own denormalised tenant column, because the reason being recorded is
# that the containing Session was swept; an Event whose own column names this
# tenant while its Session does not is reached by the binding statement instead,
# under the reason that actually explains it.
INSERT_SCOPED_EVENTS_STATEMENT: Final[str] = (
    "INSERT INTO erasure_candidate "
    "(run_id, artifact_id, artifact_kind, content_digest, selection_reason) "
    "SELECT %s, e.id, %s, e.content_digest, %s "
    "FROM ledger AS e JOIN session AS s ON s.id = e.session_id "
    "WHERE s.client_id = %s "
    "ON CONFLICT DO NOTHING"
)

# Every Artifact whose current attribution names the tenant, whatever its kind.
# The null-successor predicate is the current-attribution query: a version that
# was superseded is history and cannot widen the sweep, and the current version is
# admitted however long the history behind it. The digest is read from the derived
# table by identifier and is absent for a kind that table does not hold, which is
# correct rather than lossy -- an Event's digest is recorded by the statement above
# and a Session and an Embedding carry none.
INSERT_CURRENT_BINDINGS_STATEMENT: Final[str] = (
    "INSERT INTO erasure_candidate "
    "(run_id, artifact_id, artifact_kind, content_digest, selection_reason) "
    "SELECT %s, b.artifact_id, b.artifact_kind, "
    "(SELECT d.content_digest FROM derived_artifact AS d WHERE d.id = b.artifact_id), %s "
    "FROM client_binding AS b "
    "WHERE b.client_id = %s AND b.superseded_by IS NULL "
    "ON CONFLICT DO NOTHING"
)

# The below-floor Learned_Procedures, reached explicitly. The floor is bound from
# the configured policy, so the value this statement compares against is the value
# the recall path excludes by rather than a number written twice. The comparison is
# strict, matching the exclusion it mirrors: a procedure standing exactly at the
# floor is not below it.
INSERT_BELOW_FLOOR_PROCEDURES_STATEMENT: Final[str] = (
    "INSERT INTO erasure_candidate "
    "(run_id, artifact_id, artifact_kind, content_digest, selection_reason) "
    "SELECT %s, d.id, %s, d.content_digest, %s "
    "FROM derived_artifact AS d "
    "JOIN client_binding AS b ON b.artifact_id = d.id AND b.superseded_by IS NULL "
    "WHERE d.kind = %s AND b.client_id = %s AND d.procedure_confidence < %s "
    "ON CONFLICT DO NOTHING"
)

# The lineage descendants of everything selected so far. The roots term reads the
# candidate set this transaction has already written, which is why this statement
# runs fourth rather than first. Both recursive terms combine with UNION, so a
# diamond is walked once per node instead of once per path and the traversal
# terminates even on a graph that somehow holds a cycle. Each step joins the edge
# table on its parent column, which the parent-direction index serves.
INSERT_LINEAGE_DESCENDANTS_STATEMENT: Final[str] = (
    "INSERT INTO erasure_candidate "
    "(run_id, artifact_id, artifact_kind, content_digest, selection_reason) "
    "WITH RECURSIVE roots AS ("
    "SELECT artifact_id AS node FROM erasure_candidate WHERE run_id = %s"
    "), "
    "descendants AS ("
    "SELECT e.child_id AS node FROM lineage_edge AS e JOIN roots AS r ON e.parent_id = r.node "
    "UNION "
    "SELECT e.child_id FROM lineage_edge AS e JOIN descendants AS d ON e.parent_id = d.node"
    ") "
    "SELECT %s, d.node, %s, a.content_digest, %s "
    "FROM descendants AS d JOIN derived_artifact AS a ON a.id = d.node "
    "ON CONFLICT DO NOTHING"
)

# The Embeddings of everything selected, joined by the artifact identifier the
# per-artifact embedding index serves. This statement runs last because the set it
# reads has to be complete: an Embedding of a lineage descendant is only reachable
# once that descendant is in the set.
INSERT_SELECTED_EMBEDDINGS_STATEMENT: Final[str] = (
    "INSERT INTO erasure_candidate "
    "(run_id, artifact_id, artifact_kind, content_digest, selection_reason) "
    "SELECT %s, em.id, %s, NULL, %s FROM embedding AS em "
    "JOIN erasure_candidate AS c ON c.run_id = %s AND c.artifact_id = em.artifact_id "
    "ON CONFLICT DO NOTHING"
)

# The chain tip of every Session the sweep touched, captured for the certificate.
# The tip is the highest sequence number's row, taken by ranking within the
# Session, and the row count is the same window's cardinality, so one pass over
# the Session's Events yields both and a verifier re-deriving the chain knows how
# far it should have got.
INSERT_RUN_SESSIONS_STATEMENT: Final[str] = (
    "INSERT INTO run_session (run_id, session_id, terminal_chain_digest, terminal_seq, row_count) "
    "SELECT %s, t.session_id, t.chain_digest, t.seq, t.row_count FROM ("
    "SELECT e.session_id, e.chain_digest, e.seq, "
    "count(*) OVER (PARTITION BY e.session_id) AS row_count, "
    "row_number() OVER (PARTITION BY e.session_id ORDER BY e.seq DESC) AS rn "
    "FROM ledger AS e WHERE e.session_id IN ("
    "SELECT DISTINCT session_id FROM ledger WHERE id IN ("
    "SELECT artifact_id FROM erasure_candidate "
    "WHERE run_id = %s AND artifact_kind = %s))"
    ") AS t WHERE t.rn = 1 "
    "ON CONFLICT DO NOTHING"
)

# How many swept Artifacts hold no vector yet, recorded on the run row. Two
# counting terms rather than one join over a union, because each term is a lookup
# by primary key into the one table that holds that kind, and the two kinds that
# carry an embedding state are exactly these two. The assignment is absolute
# rather than incremental, so re-running the statement lands on the same number.
RECORD_UNEMBEDDED_COUNT_STATEMENT: Final[str] = (
    "UPDATE erasure_run SET unembedded_count = ("
    "SELECT count(*) FROM erasure_candidate AS c "
    "JOIN derived_artifact AS d ON d.id = c.artifact_id "
    "WHERE c.run_id = %s AND d.embedding_state = %s"
    ") + ("
    "SELECT count(*) FROM erasure_candidate AS c "
    "JOIN ledger AS e ON e.id = c.artifact_id "
    "WHERE c.run_id = %s AND e.embedding_state = %s"
    ") WHERE id = %s RETURNING unembedded_count"
)

# The per-reason cardinality of the candidate set, which is how a caller learns
# what each statement contributed without any identifier crossing the wire.
COUNT_BY_REASON_QUERY: Final[str] = (
    "SELECT selection_reason, count(*) FROM erasure_candidate WHERE run_id = %s "
    "GROUP BY selection_reason ORDER BY selection_reason"
)

# How many Sessions the run recorded a chain tip for.
COUNT_RUN_SESSIONS_QUERY: Final[str] = "SELECT count(*) FROM run_session WHERE run_id = %s"

# The label the sweep transaction appears under in a log record and in the note an
# exhausted retry attaches.
_SWEEP_LABEL: Final[str] = "erasure_sweep"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepCounts:
    """How many candidates each selection reason accounts for.

    Every reason the explicit sweep can record is present, zero included, so a
    caller reading a report never has to decide what an absent reason means. The
    semantic phase's reason is deliberately not a field here: this shape describes
    what phase one selected.
    """

    session_scope: int
    event_of_scoped_session: int
    client_binding: int
    lineage_descendant: int
    embedding_of_selected: int

    @property
    def total(self) -> int:
        """How many candidates the explicit sweep selected in all."""
        return (
            self.session_scope
            + self.event_of_scoped_session
            + self.client_binding
            + self.lineage_descendant
            + self.embedding_of_selected
        )

    def for_reason(self, reason: str) -> int:
        """How many candidates one reason accounts for.

        Raises:
            ValueError: The reason is not one the explicit sweep records.
        """
        if reason not in SWEEP_REASONS:
            raise ValueError("the explicit sweep records only its own five selection reasons")
        counted: int = getattr(self, reason)
        return counted


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What phase one selected, as the numbers a run row and a certificate carry.

    Attributes:
        run_id: The run the candidate set belongs to.
        client_id: The tenant whose content was swept.
        counts: The per-reason cardinality of the candidate set.
        sessions_recorded: How many Sessions a chain tip was captured for.
        unembedded_count: How many swept Artifacts hold no vector yet, as
            recorded on the run row.
        recall_floor: The floor the below-floor procedure selection applied, kept
            so a report states the value that was in force rather than the value
            in force when the report is read.
    """

    run_id: UUID
    client_id: UUID
    counts: SweepCounts
    sessions_recorded: int
    unembedded_count: int
    recall_floor: float


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def sweep(
    cursor: Cursor,
    run_id: UUID,
    client_id: UUID,
    *,
    recall_floor: float,
) -> SweepResult:
    """Populate one run's candidate set on a caller's cursor, in statement order.

    This is the composable form the run skeleton uses: phase one is one
    SERIALIZABLE transaction of the run, and the caller that owns the run frames
    it and hands its cursor here.

    The statements run in the order the set is built in, and the order is not
    interchangeable: the lineage traversal seeds from the rows the first four
    statements wrote, and the embedding selection seeds from everything before it.

    Args:
        cursor: The cursor the caller's transaction is running on.
        run_id: The run whose candidate set is being populated.
        client_id: The tenant whose content is being swept.
        recall_floor: The standing below which a Learned_Procedure is excluded
            from recall and therefore has to be reached explicitly here.

    Returns:
        The per-reason counts, the number of Sessions whose chain tip was
        captured, and the pending-embedding count written to the run row.

    Raises:
        StoreError: The run identifier names no run, so there is no row to record
            the pending-embedding count on and nothing was completed.
    """
    cursor.execute(
        INSERT_SCOPED_SESSIONS_STATEMENT,
        (run_id, _SESSION_KIND, REASON_SESSION_SCOPE, client_id),
    )
    cursor.execute(
        INSERT_SCOPED_EVENTS_STATEMENT,
        (run_id, _EVENT_KIND, REASON_EVENT_OF_SCOPED_SESSION, client_id),
    )
    cursor.execute(
        INSERT_CURRENT_BINDINGS_STATEMENT,
        (run_id, REASON_CLIENT_BINDING, client_id),
    )
    cursor.execute(
        INSERT_BELOW_FLOOR_PROCEDURES_STATEMENT,
        (
            run_id,
            _DERIVED_KIND,
            REASON_CLIENT_BINDING,
            _PROCEDURE_KIND,
            client_id,
            recall_floor,
        ),
    )
    cursor.execute(
        INSERT_LINEAGE_DESCENDANTS_STATEMENT,
        (run_id, run_id, _DERIVED_KIND, REASON_LINEAGE_DESCENDANT),
    )
    cursor.execute(
        INSERT_SELECTED_EMBEDDINGS_STATEMENT,
        (run_id, _EMBEDDING_KIND, REASON_EMBEDDING_OF_SELECTED, run_id),
    )
    cursor.execute(INSERT_RUN_SESSIONS_STATEMENT, (run_id, run_id, _EVENT_KIND))
    return SweepResult(
        run_id=run_id,
        client_id=client_id,
        counts=select_reason_counts(cursor, run_id),
        sessions_recorded=count_run_sessions(cursor, run_id),
        unembedded_count=record_unembedded_count(cursor, run_id),
        recall_floor=recall_floor,
    )


def run_sweep(
    store: MemoryStore,
    run_id: UUID,
    client_id: UUID,
    *,
    policy: ConfidencePolicy | None = None,
) -> SweepResult:
    """Run phase one in one SERIALIZABLE transaction, retrying a conflict.

    The transaction is framed by the store's own wrapper, so the bounded jittered
    retry is inherited rather than restated here, and the body is replayable
    because every statement in it is.

    The floor comes from the configured policy rather than from a constant of this
    module, and a caller sweeping several tenants resolves the policy once and
    hands it in, so a batch costs one resolution rather than one per run.
    """
    floor = (ConfidencePolicy.from_configuration() if policy is None else policy).recall_floor

    def body(cursor: Cursor) -> SweepResult:
        return sweep(cursor, run_id, client_id, recall_floor=floor)

    return store.in_serializable(body, label=_SWEEP_LABEL)


# ---------------------------------------------------------------------------
# What the sweep selected
# ---------------------------------------------------------------------------


def select_reason_counts(cursor: Cursor, run_id: UUID) -> SweepCounts:
    """The per-reason cardinality of one run's candidate set.

    A reason no statement contributed to reads as zero rather than as an absent
    key. The semantic phase's reason is skipped rather than refused, so this same
    read is usable after that phase has extended the set.
    """
    cursor.execute(COUNT_BY_REASON_QUERY, (run_id,))
    counted: dict[str, int] = {}
    for row in cursor.fetchall():
        reason = _as_text(_column(row, 0, _REASON_ROW_WIDTH))
        if reason in SWEEP_REASONS:
            counted[reason] = _as_count(row[1])
    return SweepCounts(
        session_scope=counted.get(REASON_SESSION_SCOPE, 0),
        event_of_scoped_session=counted.get(REASON_EVENT_OF_SCOPED_SESSION, 0),
        client_binding=counted.get(REASON_CLIENT_BINDING, 0),
        lineage_descendant=counted.get(REASON_LINEAGE_DESCENDANT, 0),
        embedding_of_selected=counted.get(REASON_EMBEDDING_OF_SELECTED, 0),
    )


def count_run_sessions(cursor: Cursor, run_id: UUID) -> int:
    """How many Sessions this run captured a chain tip for."""
    cursor.execute(COUNT_RUN_SESSIONS_QUERY, (run_id,))
    row = cursor.fetchone()
    if row is None:
        raise StoreError("the run session count reported no row, so it is unknown")
    return _as_count(_column(row, 0, _COUNT_ROW_WIDTH))


def record_unembedded_count(cursor: Cursor, run_id: UUID) -> int:
    """Record how many swept Artifacts hold no vector yet, and report the number.

    Written inside the sweep's own transaction rather than afterwards, because the
    number describes the candidate set this transaction produced and a number
    written separately could describe a set some later phase had extended.

    Raises:
        StoreError: The identifier names no run, so the count has nowhere to be
            recorded and the sweep's evidence would be incomplete.
    """
    cursor.execute(
        RECORD_UNEMBEDDED_COUNT_STATEMENT,
        (run_id, _PENDING_EMBEDDING, run_id, _PENDING_EMBEDDING, run_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            "the pending-embedding count was not recorded because no erasure run "
            "carries that identifier"
        )
    return _as_count(_column(row, 0, _COUNT_ROW_WIDTH))


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a value whose type is not the one the schema declares.

    The type is named and the value is not, because a column reached by these
    statements may hold memory content and a message naming the fault belongs in a
    log record while the content does not.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_count(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a count")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a count")
