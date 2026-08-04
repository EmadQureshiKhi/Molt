"""Phase three: what happens to each candidate Artifact, decided and then written.

The sweep and the residue phase produce a candidate set. This module turns that set
into one decision per Artifact and performs the two mutations those decisions call
for: a batched hard delete, and a per-Artifact surgical redaction. Both write
through the fence, so a worker that lost the lease mid-run records no disposition.

Seven claims carry the module.

**Classification is one statement over stored state, not a Python view of it.** The
candidate set is joined to the Attribution_Versions that are current, and the counts
that decide the outcome — how many other Clients still hold a current claim, and
whether the erased Client still holds one — come back from the cluster. A decision
taken from an in-memory picture of bindings would be a decision taken against state
that has since moved; taken from this statement it is a decision the same
transaction boundary can be reasoned about.

**The join admits current versions only.** A binding that was closed by an earlier
run, or withdrawn by this one, is history rather than a claim, so it neither keeps
an Artifact alive nor counts toward the blended case. That is the same
current-version predicate every operational read in the store layer applies, which
is what makes a re-run of an erasure sweep an empty set rather than a repeat.

**An Event is not divisible.** A blended Derived_Artifact can be rewritten because
its body is one document several Clients contributed to. An Event body is a single
recorded act of one Client's Session; other Clients appear on it only by
inheritance, so there is nothing to redact and the whole row goes. The reason
recorded says exactly that, because *hard deleted although other Clients were
bound* is a claim an auditor will want an explanation of.

**Ownership of a Session and of its Events comes from the Session's tenant column,
not from an attribution row.** A Session records which Client it ran for on the row
itself and carries no binding at all, and an Event of that Session inherits the
same ownership without needing one either. The sweep selects both on exactly that
basis and stamps the selection reason saying so. Reading the claim only out of
`client_binding` would therefore contradict the selection that produced the
candidate: the sweep would say *this row is the tenant's* and the decision table
would answer *no Client holds a claim on it*, and the tenant's own Sessions would
survive an erasure that was asked to remove them. So a candidate carrying either
session-scope reason is treated as claimed by the erased tenant whatever the
binding join returned, and the two paths agree.

**The batch delete captures its evidence before it destroys it.** The pre-deletion
digest and the bound slugs come from the classification statement and are written
onto the Disposition rows in the same transaction as the deletes, because once the
rows are gone that evidence exists nowhere else. The deletes are ordered dependents
first so no reference is violated part-way through, the decisions themselves are
ordered so that every Event is removed no later than the Session it was recorded in
and no batch boundary can fall between the two the wrong way round, and the batch
size is read from the configuration surface rather than fixed here.

**The surgical transaction carries an optimistic guard and holds no model call.**
The update matches on the pre-redaction digest, so a body that changed between the
rewrite call and this transaction updates zero rows and the caller re-reads and
re-rewrites rather than overwriting a change it never saw. The replacement text and
its vector are both produced before the transaction opens.

**The binding removal is recorded as history rather than as a hole.** The erased
Client's claim on a redacted Artifact is closed through the attribution
supersession, which writes a terminal version and one Ledger Event, rather than
deleted. Every other Client's current binding is untouched, which is the whole
substance of the surgical claim. A hard delete removes bindings outright instead,
because the Artifact they describe no longer exists to be attributed.

**Both digests are retained and no pre-redaction body is stored.** The Disposition
row holds the digest before and the digest after, the binding slugs before and
after, the count-only structural summary a rewrite produced, and the writing owner's
fencing generation. It holds no body, and this module sends no statement that would
copy one anywhere. The summary is two whole numbers rather than a text diff for that
reason, and it is absent on the two paths that rewrote nothing: a hard delete
removed the body and a retention left it untouched, so neither has segments to
report and a zero would overstate.

Every statement is a whole module-level literal with bound parameters, and no
identifier or domain value is ever interpolated into statement text.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from molt.config.resolve import Configuration, load_configuration
from molt.erase.rewriter import FAIL_CLOSED_REASON, Replacement, StructuralDiff
from molt.erase.sweep import REASON_EVENT_OF_SCOPED_SESSION, REASON_SESSION_SCOPE
from molt.errors import StoreError
from molt.models.artifact import ArtifactKind
from molt.store import Cursor, MemoryStore
from molt.store.attribution import SupersessionContext, withdraw_attribution
from molt.store.embeddings import EmbeddingWrite, insert_embedding
from molt.store.fencing import fenced_disposition
from molt.telemetry import Severity, log, metric

__all__ = [
    "BATCH_SIZE_KEY",
    "BINDING_ABSENT_REASON",
    "CLASSIFICATION_QUERY",
    "COMPONENT",
    "DELETE_BINDINGS_STATEMENT",
    "DELETE_DERIVED_STATEMENT",
    "DELETE_EDGES_STATEMENT",
    "DELETE_EMBEDDINGS_STATEMENT",
    "DELETE_EVENTS_STATEMENT",
    "DELETE_SESSIONS_STATEMENT",
    "DISPOSITION_METRIC",
    "EVENT_NOT_DIVISIBLE_REASON",
    "INSERT_DISPOSITION_STATEMENT",
    "ORPHANED_PARENT_EDGE_STATEMENT",
    "REDACT_BODY_STATEMENT",
    "REMOVE_REDACTED_EMBEDDING_STATEMENT",
    "REWRITTEN_REASON",
    "SOLE_BINDING_REASON",
    "Candidate",
    "Decision",
    "DispositionKind",
    "RedactionRecord",
    "RunOwnership",
    "SurgicalWrite",
    "batches_of",
    "classify",
    "decide",
    "decisions_for",
    "fail_closed_delete",
    "hard_delete",
    "read_candidates",
    "retain",
    "surgical_redaction",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "erase"

# The measurement each written Disposition is counted by. The one dimension is the
# disposition itself, which the schema holds to three values, so the billable
# cardinality is bounded by construction; the tenant and the Artifact are unbounded
# and stay in the log record.
DISPOSITION_METRIC: Final[str] = "erasure.dispositions"

# The configuration surface key the delete batch size is read from.
BATCH_SIZE_KEY: Final[str] = "MOLT_ERASURE_BATCH_SIZE"

# The reasons this module records, one per row of the decision table. They are
# values rather than prose because a certificate reader and the console both group
# by them.
SOLE_BINDING_REASON: Final[str] = "sole_client_binding"
EVENT_NOT_DIVISIBLE_REASON: Final[str] = "event_not_divisible"
REWRITTEN_REASON: Final[str] = "blended_artifact_rewritten"
BINDING_ABSENT_REASON: Final[str] = "binding_already_absent"

# The kinds an Artifact of the candidate set can carry, and the two that cannot be
# partially attributed. A Derived_Artifact body is one document several Clients
# contributed to; an Event and a Session are one Client's own recorded act.
_INDIVISIBLE_KINDS: Final[frozenset[ArtifactKind]] = frozenset(
    {ArtifactKind.EVENT, ArtifactKind.SESSION}
)

# The two selection reasons the sweep records when the erased tenant's ownership was
# read off the Session's own tenant column rather than out of an attribution row. The
# constants are imported from the sweep that writes them rather than restated here, so
# the selection and the decision cannot drift apart on a spelling.
_SESSION_SCOPED_REASONS: Final[frozenset[str]] = frozenset(
    {REASON_SESSION_SCOPE, REASON_EVENT_OF_SCOPED_SESSION}
)

# How many columns the classification statement returns, checked before a row is
# decoded so the statement and its decoder cannot drift apart silently.
_CANDIDATE_ROW_WIDTH: Final[int] = 7

# The labels the transactions of this module appear under in a log record and in
# the note an exhausted retry attaches.
_DELETE_LABEL: Final[str] = "disposition_hard_delete"
_REDACT_LABEL: Final[str] = "disposition_surgical"

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# Classification. The join to the attribution history admits current versions
# alone, so a closed claim neither keeps an Artifact alive nor counts toward the
# blended case, and the join is outer so an Artifact whose every binding is already
# closed still comes back — that Artifact is a retention with a reason rather than
# an absence.
#
# The two counts are what the decision table reads. The slug array is the
# pre-decision binding evidence a Disposition records, filtered so an Artifact with
# no current binding reports an empty array rather than an array holding one
# absence, and coalesced because a filtered aggregate over no rows is itself
# absent.
#
# The ordering is stated so two runs over one candidate set classify in the same
# order rather than in whichever order the scan produced.
CLASSIFICATION_QUERY: Final[str] = (
    "SELECT c.artifact_id, c.artifact_kind, c.selection_reason, c.content_digest, "
    "count(b.client_id) FILTER (WHERE b.client_id != %s) AS other_client_count, "
    "count(b.client_id) FILTER (WHERE b.client_id = %s) AS erased_client_count, "
    "coalesce(array_agg(cl.slug) FILTER (WHERE cl.slug IS NOT NULL), ARRAY[]::STRING[]) "
    "AS binding_slugs "
    "FROM erasure_candidate AS c "
    "LEFT JOIN client_binding AS b "
    "ON b.artifact_id = c.artifact_id AND b.superseded_by IS NULL "
    "LEFT JOIN client AS cl ON cl.id = b.client_id "
    "WHERE c.run_id = %s "
    "GROUP BY c.artifact_id, c.artifact_kind, c.selection_reason, c.content_digest "
    "ORDER BY c.artifact_id"
)

# The ordered batch delete. Dependents first, then the Artifact rows, so no
# reference is violated part-way through the transaction. Each statement binds one
# array of identifiers rather than being sent per row, which is what keeps a batch
# one round trip per table instead of one per Artifact.
DELETE_EMBEDDINGS_STATEMENT: Final[str] = (
    "DELETE FROM embedding WHERE artifact_id = ANY (%s::UUID[])"
)
DELETE_EDGES_STATEMENT: Final[str] = (
    "DELETE FROM lineage_edge WHERE child_id = ANY (%s::UUID[]) OR parent_id = ANY (%s::UUID[])"
)
DELETE_BINDINGS_STATEMENT: Final[str] = (
    "DELETE FROM client_binding WHERE artifact_id = ANY (%s::UUID[])"
)
DELETE_DERIVED_STATEMENT: Final[str] = "DELETE FROM derived_artifact WHERE id = ANY (%s::UUID[])"
DELETE_EVENTS_STATEMENT: Final[str] = "DELETE FROM ledger WHERE id = ANY (%s::UUID[])"
DELETE_SESSIONS_STATEMENT: Final[str] = "DELETE FROM session WHERE id = ANY (%s::UUID[])"

# The Disposition row, which is evidence rather than state: the digests either side
# of the decision, the binding slugs either side of it, the count-only summary of
# what a rewrite dropped and kept, and the generation of the owner that wrote it.
# The conflict resolution is what makes a replayed batch a no-op rather than a
# refusal, since the transaction wrapper may run a body again.
#
# The two segment counts are bound as absent on every path but the surgical one. A
# hard delete removed the body and a retention left it alone, so neither summarises
# a rewrite, and a zero there would claim a rewrite that dropped nothing rather
# than the fact that no rewrite happened.
INSERT_DISPOSITION_STATEMENT: Final[str] = (
    "INSERT INTO disposition ("
    "run_id, artifact_id, artifact_kind, disposition, reason, selection_reason, "
    "pre_digest, post_digest, bindings_before, bindings_after, "
    "removed_segments, retained_segments, fencing_generation) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (run_id, artifact_id) DO NOTHING"
)

# The surgical update, with the optimistic guard as its second predicate. Matching
# on the pre-redaction digest is what makes a concurrent body change a zero-row
# update rather than a silent overwrite of content this rewrite never saw. The
# embedding state returns to pending because the vector standing for the old body is
# removed in the same transaction and the replacement is written from the caller's
# own vector.
#
# The owning tenant comes back because the replacement vector's row records a
# tenant, and reading it here rather than taking it from a caller means the vector
# cannot be attributed to a Client the row does not belong to.
REDACT_BODY_STATEMENT: Final[str] = (
    "UPDATE derived_artifact "
    "SET body = %s, content_digest = %s, revision = revision + 1, updated_at = now(), "
    "redacted_at = now(), embedding_state = 'pending' "
    "WHERE id = %s AND content_digest = %s "
    "RETURNING revision, owner_client_id"
)

REMOVE_REDACTED_EMBEDDING_STATEMENT: Final[str] = "DELETE FROM embedding WHERE artifact_id = %s"

# Edge removal for parents the erased Client alone holds a current claim on. The
# grouping is over the current versions of each parent, and the two conditions
# together say *this parent is the erased Client's and nobody else's*: keeping the
# edge would leave a redacted Artifact pointing at lineage that phase three is about
# to remove, which is a dangling parent reference rather than provenance.
#
# Parents bound to another Client as well are left alone, because the edge is still
# true for that Client.
ORPHANED_PARENT_EDGE_STATEMENT: Final[str] = (
    "DELETE FROM lineage_edge WHERE child_id = %s AND parent_id IN ("
    "SELECT b.artifact_id FROM client_binding AS b WHERE b.superseded_by IS NULL "
    "GROUP BY b.artifact_id "
    "HAVING count(*) FILTER (WHERE b.client_id != %s) = 0 "
    "AND count(*) FILTER (WHERE b.client_id = %s) > 0)"
)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class DispositionKind(StrEnum):
    """What phase three did to one Artifact, in the values the schema admits."""

    HARD_DELETE = "hard_delete"
    SURGICAL_REDACTION = "surgical_redaction"
    RETAINED = "retained"


@dataclass(frozen=True, slots=True)
class RunOwnership:
    """The run, the tenant being erased, and the generation every write presents.

    The generation travels with the run rather than being read per write, because
    the fence reads the current generation inside each write's own transaction and
    compares it against this one. A caller that has been superseded therefore
    learns so at the write rather than at a check it took earlier.
    """

    run_id: UUID
    client_id: UUID
    slug: str
    generation: int

    def __post_init__(self) -> None:
        """Refuse ownership that names no tenant slug the evidence can record."""
        if not self.slug:
            raise ValueError(
                "a disposition records the erased Client's slug, so ownership names it"
            )


@dataclass(frozen=True, slots=True)
class Candidate:
    """One candidate Artifact as the classification statement reports it."""

    artifact_id: UUID
    artifact_kind: ArtifactKind
    selection_reason: str
    content_digest: str | None
    other_client_count: int
    erased_client_count: int
    binding_slugs: tuple[str, ...]

    @property
    def blended(self) -> bool:
        """Whether a Client other than the erased one still holds a current claim."""
        return self.other_client_count > 0

    @property
    def scoped(self) -> bool:
        """Whether the sweep selected this candidate from the Session's tenant column.

        A Session carries the Client it ran for on the row and holds no binding, and an
        Event of that Session inherits the same ownership without needing one. Both are
        selected on that basis and stamped with the reason saying so, so the erased
        tenant's claim on such a candidate is established by the selection itself rather
        than by the binding join.
        """
        return self.selection_reason in _SESSION_SCOPED_REASONS


@dataclass(frozen=True, slots=True)
class Decision:
    """What the decision table concluded for one candidate."""

    candidate: Candidate
    disposition: DispositionKind
    reason: str

    @property
    def artifact_id(self) -> UUID:
        """The Artifact this decision is about."""
        return self.candidate.artifact_id


@dataclass(frozen=True, slots=True)
class SurgicalWrite:
    """Everything one surgical transaction needs, all produced before it opens.

    The vector is a constructor value rather than something this module obtains,
    because no model call may happen inside the transaction: a call held open across
    a transaction would hold locks for the duration of a network round trip to a
    provider.
    """

    decision: Decision
    replacement: Replacement
    vector: tuple[float, ...]
    embedding_provider: str
    embedding_model_id: str
    expires_at: datetime
    context: SupersessionContext
    embedding_client_id: UUID | None = None

    def __post_init__(self) -> None:
        """Refuse a write that is not the surgical case, or that has no prior digest."""
        if self.decision.disposition is not DispositionKind.SURGICAL_REDACTION:
            raise ValueError("a surgical write applies to a surgical redaction decision alone")
        if self.decision.candidate.content_digest is None:
            raise ValueError(
                "the optimistic guard matches on the pre-redaction digest, "
                "so a candidate carrying none cannot be redacted"
            )

    @property
    def pre_digest(self) -> str:
        """The digest the optimistic guard matches on."""
        digest = self.decision.candidate.content_digest
        if digest is None:  # pragma: no cover - refused at construction
            raise ValueError("a surgical write carries a pre-redaction digest")
        return digest


@dataclass(frozen=True, slots=True)
class RedactionRecord:
    """What one surgical transaction committed.

    Attributes:
        artifact_id: The Artifact that was rewritten.
        revision: The revision the row now carries.
        pre_digest: The digest the body carried before the rewrite.
        post_digest: The digest it carries now. Both are retained; neither body is.
        bindings_before: The slugs holding a current claim before the withdrawal.
        bindings_after: The slugs holding one after it, which is the same set less
            the erased Client.
        embedding_id: The replacement vector's row.
        withdrawn_version_id: The terminal attribution version the withdrawal
            wrote, which is what makes the removal history rather than a hole.
    """

    artifact_id: UUID
    revision: int
    pre_digest: str
    post_digest: str
    bindings_before: tuple[str, ...]
    bindings_after: tuple[str, ...]
    embedding_id: UUID
    withdrawn_version_id: UUID | None


# ---------------------------------------------------------------------------
# Classification and the decision table
# ---------------------------------------------------------------------------


def read_candidates(cursor: Cursor, ownership: RunOwnership) -> tuple[Candidate, ...]:
    """Send the classification statement on a caller's cursor.

    Taking the cursor rather than framing a transaction is what lets a caller
    classify inside the transaction that will act on the result, so the counts the
    decision turns on are part of that transaction's read set.
    """
    cursor.execute(
        CLASSIFICATION_QUERY,
        (ownership.client_id, ownership.client_id, ownership.run_id),
    )
    return tuple(_candidate_of(row) for row in cursor.fetchall())


def classify(store: MemoryStore, ownership: RunOwnership) -> tuple[Candidate, ...]:
    """The candidate set of one run, classified against the current bindings."""
    return store.read(lambda cursor: read_candidates(cursor, ownership))


def decide(candidate: Candidate, *, exclusion_reason: str | None = None) -> Decision:
    """Apply the decision table to one classified candidate.

    The order of the tests is the order of the table, and it is load-bearing: an
    Artifact the erased Client no longer holds a claim on is retained whatever its
    other bindings say, because there is nothing left to erase from it, and an
    Artifact the adjudication excluded is retained before any deletion is
    considered.

    The no-claim retention reads the binding join, so it applies only where a binding
    is what ownership was ever recorded as. A Session records its tenant on the row
    itself and carries no binding, and an Event of that Session inherits that
    ownership rather than restating it, which is exactly the basis the sweep selected
    both on. Retaining those on an absent binding would have the decision table deny
    the claim the selection asserted, and the erased tenant's own Sessions would
    outlive the run. A session-scoped candidate therefore passes this test and falls
    to the rows below: unblended it is the sole claim and goes as such, and blended
    it is an indivisible body and goes as that. Neither needs a reason of its own,
    because the reason each already carries is the true description of it.

    Args:
        candidate: The classified candidate.
        exclusion_reason: The reason the residue adjudication excluded this
            candidate, when it did. An excluded candidate is retained with that
            reason recorded and no mutation performed.

    Returns:
        The disposition and the reason, ready to be written.
    """
    if exclusion_reason is not None:
        return Decision(candidate, DispositionKind.RETAINED, exclusion_reason)
    if candidate.erased_client_count == 0 and not candidate.scoped:
        return Decision(candidate, DispositionKind.RETAINED, BINDING_ABSENT_REASON)
    if not candidate.blended:
        return Decision(candidate, DispositionKind.HARD_DELETE, SOLE_BINDING_REASON)
    if candidate.artifact_kind in _INDIVISIBLE_KINDS:
        return Decision(candidate, DispositionKind.HARD_DELETE, EVENT_NOT_DIVISIBLE_REASON)
    return Decision(candidate, DispositionKind.SURGICAL_REDACTION, REWRITTEN_REASON)


def fail_closed_delete(candidate: Candidate) -> Decision:
    """The decision a blended Artifact falls to when no usable rewrite was produced.

    The disposition is the hard delete rather than a retention, which is the bias the
    whole rewrite path is built around: blended memory is lost rather than an erased
    Client's content left in place. The reason is the rewriter's own single collapsed
    value, so the evidence names the fail-closed path rather than an ordinary sole
    binding.
    """
    return Decision(candidate, DispositionKind.HARD_DELETE, FAIL_CLOSED_REASON)


def decisions_for(
    candidates: Sequence[Candidate],
    *,
    exclusions: dict[UUID, str] | None = None,
) -> tuple[Decision, ...]:
    """The decision table applied across a classified candidate set."""
    excluded = exclusions if exclusions is not None else {}
    return tuple(
        decide(candidate, exclusion_reason=excluded.get(candidate.artifact_id))
        for candidate in candidates
    )


# ---------------------------------------------------------------------------
# The batched hard delete
# ---------------------------------------------------------------------------


def batches_of(
    decisions: Sequence[Decision],
    *,
    size: int | None = None,
    configuration: Configuration | None = None,
) -> Iterator[tuple[Decision, ...]]:
    """Split decisions into transaction-sized batches, sized from configuration.

    The size is a configured number rather than a constant here, because how much
    work one transaction should carry is a property of the deployment's contention
    rather than of this module.
    """
    bound = size if size is not None else _batch_size(configuration)
    if bound < 1:
        raise ValueError("a delete batch carries at least one artifact")
    for start in range(0, len(decisions), bound):
        yield tuple(decisions[start : start + bound])


def _delete_rank(decision: Decision) -> int:
    """Where one decision sits in the delete order: a Session last, anything else first."""
    return 1 if decision.candidate.artifact_kind is ArtifactKind.SESSION else 0


def _session_last(decisions: Sequence[Decision]) -> tuple[Decision, ...]:
    """Order decisions so that no Session is deleted before an Event of that Session.

    `ledger.session_id` is the one reference of the memory graph an erasure leaves
    enforced, because an Event belonging to a Session that does not exist records
    nothing a reader can follow. A Session therefore may not go before its own
    Events, and batching cuts across Sessions, so without an order a Session can land
    in an earlier batch than Events of it and that batch's delete is refused.

    The sort is stable and its key has two values, so every kind other than the
    Session keeps the relative order it arrived in and the ordering the caller
    established for the rest of the set is untouched.
    """
    return tuple(sorted(decisions, key=_delete_rank))


def hard_delete(
    store: MemoryStore,
    ownership: RunOwnership,
    decisions: Sequence[Decision],
    *,
    batch_size: int | None = None,
    configuration: Configuration | None = None,
) -> int:
    """Delete the Artifacts of the hard-delete decisions, in fenced batches.

    Each batch is one fenced transaction: the fence's generation read runs first, the
    ordered deletes follow, and the Disposition rows land in the same transaction as
    the deletes they are evidence of. That co-location is the point — after the
    delete, the digest and the binding slugs exist nowhere else, so evidence written
    afterwards could not be written at all.

    The decisions are ordered before they are split, and the order is load-bearing
    rather than cosmetic. `ledger.session_id` stays enforced, so a Session may not be
    removed before its own Events; batching cuts across Sessions, so a Session
    arriving ahead of Events of it would land in an earlier transaction and that
    transaction would be refused. Sorting Session-kind decisions last here rather than
    at a call site is what makes the order impossible to bypass: every caller of this
    function gets it, whatever order it presented.

    Args:
        store: The connection surface each batch transaction is framed by.
        ownership: The run, the tenant, and the generation every write presents.
        decisions: The decisions to apply. Anything that is not a hard delete is
            refused rather than skipped, because silently ignoring a decision would
            leave an Artifact unaccounted for.
        batch_size: How many Artifacts one transaction carries, from the
            configuration surface when the caller names none.
        configuration: The surface the batch size is read from.

    Returns:
        How many Artifacts were deleted.

    Raises:
        ValueError: A decision naming another disposition was presented.
        LeaseNotHeldError: The Client holds no current lease. Nothing was written.
        StaleFencingGenerationError: The presented generation is superseded. Nothing
            was written.
    """
    wrong = [
        decision
        for decision in decisions
        if decision.disposition is not DispositionKind.HARD_DELETE
    ]
    if wrong:
        raise ValueError(
            f"{len(wrong)} decision(s) presented to the hard delete name another disposition"
        )
    ordered = _session_last(decisions)
    deleted = 0
    for batch in batches_of(ordered, size=batch_size, configuration=configuration):
        deleted += _delete_batch(store, ownership, batch)
    return deleted


def _delete_batch(
    store: MemoryStore,
    ownership: RunOwnership,
    batch: tuple[Decision, ...],
) -> int:
    """One fenced transaction deleting one batch and recording its evidence."""
    identifiers = [decision.artifact_id for decision in batch]

    def body(cursor: Cursor) -> int:
        cursor.execute(DELETE_EMBEDDINGS_STATEMENT, (identifiers,))
        cursor.execute(DELETE_EDGES_STATEMENT, (identifiers, identifiers))
        cursor.execute(DELETE_BINDINGS_STATEMENT, (identifiers,))
        cursor.execute(DELETE_DERIVED_STATEMENT, (identifiers,))
        cursor.execute(DELETE_EVENTS_STATEMENT, (identifiers,))
        cursor.execute(DELETE_SESSIONS_STATEMENT, (identifiers,))
        for decision in batch:
            _insert_disposition(
                cursor,
                ownership,
                decision,
                post_digest=None,
                bindings_after=(),
            )
        return len(batch)

    written = fenced_disposition(store, ownership.client_id, ownership.generation, body)
    log(
        Severity.INFO,
        COMPONENT,
        "a disposition batch hard-deleted its artifacts and recorded the evidence",
        run_id=str(ownership.run_id),
        client_id=str(ownership.client_id),
        artifact_count=written,
        label=_DELETE_LABEL,
    )
    return written


# ---------------------------------------------------------------------------
# The single surgical transaction
# ---------------------------------------------------------------------------


def surgical_redaction(
    store: MemoryStore,
    ownership: RunOwnership,
    write: SurgicalWrite,
) -> RedactionRecord | None:
    """Apply one validated replacement in one fenced transaction, or report a stale guard.

    The order inside the transaction is fixed. The guarded update comes first, so a
    body that moved since the rewrite stops the transaction before anything else is
    touched. The stale vector is removed, the lineage edges whose parents belong to
    the erased Client alone are removed, the erased Client's claim is closed through
    the attribution supersession, the replacement vector is written, and the
    Disposition row records both digests and both binding sets.

    Args:
        store: The connection surface the transaction is framed by.
        ownership: The run, the tenant, and the generation the write presents.
        write: The validated replacement, its vector, and the Session context the
            supersession Event is recorded within.

    Returns:
        What the transaction committed, or None when the optimistic guard matched no
        row, which means the body changed since the rewrite and the caller should
        re-read and re-rewrite. Nothing was written in that case.

    Raises:
        LeaseNotHeldError: The Client holds no current lease. Nothing was written.
        StaleFencingGenerationError: The presented generation is superseded. Nothing
            was written.
        StoreError: A statement produced no row where one was required.
    """

    def body(cursor: Cursor) -> RedactionRecord | None:
        return _redact(cursor, ownership, write)

    record = fenced_disposition(store, ownership.client_id, ownership.generation, body)
    if record is None:
        log(
            Severity.WARNING,
            COMPONENT,
            "a surgical redaction matched no row, so the body changed since the rewrite",
            run_id=str(ownership.run_id),
            artifact_id=str(write.decision.artifact_id),
            label=_REDACT_LABEL,
        )
    return record


def _redact(
    cursor: Cursor,
    ownership: RunOwnership,
    write: SurgicalWrite,
) -> RedactionRecord | None:
    """The body of the surgical transaction, in the order the design fixes."""
    artifact_id = write.decision.artifact_id
    replacement = write.replacement
    cursor.execute(
        REDACT_BODY_STATEMENT,
        (replacement.text, replacement.digest, artifact_id, write.pre_digest),
    )
    updated = cursor.fetchone()
    if updated is None:
        return None
    revision = _as_int(updated[0])
    owner_client_id = _as_uuid(updated[1])

    cursor.execute(REMOVE_REDACTED_EMBEDDING_STATEMENT, (artifact_id,))
    cursor.execute(
        ORPHANED_PARENT_EDGE_STATEMENT,
        (artifact_id, ownership.client_id, ownership.client_id),
    )
    withdrawn = withdraw_attribution(
        cursor,
        artifact_id,
        ownership.client_id,
        context=write.context,
    )
    embedding_id = insert_embedding(
        cursor,
        EmbeddingWrite(
            artifact_id=artifact_id,
            artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=write.embedding_client_id
            if write.embedding_client_id is not None
            else owner_client_id,
            provider=write.embedding_provider,
            model_id=write.embedding_model_id,
            vec=write.vector,
            expires_at=write.expires_at,
        ),
    )
    bindings_before = write.decision.candidate.binding_slugs
    bindings_after = tuple(slug for slug in bindings_before if slug != ownership.slug)
    _insert_disposition(
        cursor,
        ownership,
        write.decision,
        post_digest=replacement.digest,
        bindings_after=bindings_after,
        diff=replacement.diff,
    )
    return RedactionRecord(
        artifact_id=artifact_id,
        revision=revision,
        pre_digest=write.pre_digest,
        post_digest=replacement.digest,
        bindings_before=bindings_before,
        bindings_after=bindings_after,
        embedding_id=embedding_id,
        withdrawn_version_id=None if withdrawn is None else withdrawn.version_id,
    )


# ---------------------------------------------------------------------------
# Retention, which mutates nothing and still records why
# ---------------------------------------------------------------------------


def retain(
    store: MemoryStore,
    ownership: RunOwnership,
    decision: Decision,
) -> None:
    """Record a retained candidate, with the reason and no mutation at all.

    A retention is written through the same fence as a deletion, because it is
    evidence about the run and a superseded owner may record none.
    """
    if decision.disposition is not DispositionKind.RETAINED:
        raise ValueError("a retention records a retained decision alone")

    def body(cursor: Cursor) -> None:
        _insert_disposition(
            cursor,
            ownership,
            decision,
            post_digest=decision.candidate.content_digest,
            bindings_after=decision.candidate.binding_slugs,
        )

    fenced_disposition(store, ownership.client_id, ownership.generation, body)


# ---------------------------------------------------------------------------
# The one disposition write, and row decoding
# ---------------------------------------------------------------------------


def _insert_disposition(
    cursor: Cursor,
    ownership: RunOwnership,
    decision: Decision,
    *,
    post_digest: str | None,
    bindings_after: tuple[str, ...],
    diff: StructuralDiff | None = None,
) -> None:
    """Write one Disposition row on the caller's cursor, carrying the generation.

    Every disposition of every path goes through here, so the fencing generation is
    on every row by construction rather than by each caller remembering it, and the
    two segment counts travel as one value or as nothing.

    Args:
        cursor: The cursor of the transaction the mutation this records happened in.
        ownership: The run, the tenant, and the generation the row carries.
        decision: What was decided, and the candidate it was decided about.
        post_digest: The digest the body carries after the decision, absent where
            the body is gone.
        bindings_after: The slugs holding a current claim after the decision.
        diff: The count-only summary of what a rewrite dropped and kept, absent on
            the two paths that performed no rewrite.
    """
    candidate = decision.candidate
    cursor.execute(
        INSERT_DISPOSITION_STATEMENT,
        (
            ownership.run_id,
            candidate.artifact_id,
            candidate.artifact_kind.value,
            decision.disposition.value,
            decision.reason,
            candidate.selection_reason,
            candidate.content_digest,
            post_digest,
            list(candidate.binding_slugs),
            list(bindings_after),
            None if diff is None else diff.removed_segments,
            None if diff is None else diff.retained_segments,
            ownership.generation,
        ),
    )
    metric(DISPOSITION_METRIC, disposition=decision.disposition.value)


def _batch_size(configuration: Configuration | None) -> int:
    """The configured number of Artifacts one delete transaction carries."""
    resolved = configuration if configuration is not None else load_configuration()
    return resolved.integer(BATCH_SIZE_KEY)


def _candidate_of(row: Sequence[object]) -> Candidate:
    """Build one classified candidate from a stored row, refusing every other shape."""
    if len(row) != _CANDIDATE_ROW_WIDTH:
        raise StoreError(
            f"the classification returned {len(row)} column(s) "
            f"where {_CANDIDATE_ROW_WIDTH} are read"
        )
    return Candidate(
        artifact_id=_as_uuid(row[0]),
        artifact_kind=ArtifactKind(_as_text(row[1])),
        selection_reason=_as_text(row[2]),
        content_digest=None if row[3] is None else _as_text(row[3]),
        other_client_count=_as_int(row[4]),
        erased_client_count=_as_int(row[5]),
        binding_slugs=_as_slugs(row[6]),
    )


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a value whose type is not the one the schema declares.

    The type is named and the value is not, because a message reaches a log record
    and the values passing through here are memory content.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a whole number")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a whole number")


def _as_slugs(value: object) -> tuple[str, ...]:
    """The binding slug array, ordered so two runs record it identically."""
    if isinstance(value, (list, tuple)):
        return tuple(sorted(_as_text(item) for item in value))
    raise _unexpected(value, "an array of tenant slugs")
