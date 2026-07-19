"""Embedding writes, the embedding-state transitions, and the neighbour query.

An Embedding is a fixed-width vector standing for one Artifact's text. Five
claims carry this module, and each is arranged so a caller cannot lose it by
forgetting something.

**An Artifact and its Embedding land in one transaction.** The Artifact
row and the Embedding row that represents it are inserted on the same cursor
inside one serializable transaction, so a corpus never holds a row whose vector
landed without it or a vector whose row did not. The composable form takes the
caller's own cursor, because an Event's Embedding belongs in the transaction that
appended the Event rather than in one of its own.

**The unit-norm and width checks happen before the statement is sent.** The
delivered index orders by L2 distance while every threshold in this design is
expressed as a cosine distance, and those two orderings agree on unit vectors and
disagree otherwise. A vector of another width or another length would therefore
be ranked by one measure and judged by another, so both are refused here rather
than written and reasoned about later. The row carries the width and the
unit-norm assertion as stored columns as well, so a vector that reached the table
through some other path is identifiable from the row instead of merely suspected.

**The embedding state is derived from whether a vector accompanied the row, not
trusted from the caller.** A write carrying a vector records `embedded`; a write
carrying none records what the Artifact declared, which is `pending` for a row
that owes a vector and `not_required` for a row that owes none. A caller
declaring `embedded` with no vector is refused, because that state would assert
an Embedding row that does not exist. The state moves afterwards through one
`UPDATE` naming the state column alone, which is what the erasure path uses when
a re-embedding lands and what the drain uses when a provider will not answer.

**The pending sweep spans both embeddable kinds and ascends by creation.** Events
and Derived_Artifacts each carry their own state column and their own partial
index over the pending rows, so the sweep is one statement over the union of the
two, ordered by creation instant ascending and bounded by a row limit. Ascending
order is what makes a drain fair: the oldest owed vector is the one produced
next, rather than whichever row a scan happened to reach.

**The sweep answers whether a vector is stored, not only what a column says.**
An Event's state column can never be moved, so a sweep reading it alone would
return every Event that has ever been embedded on every pass, and the
unembedded-coverage count of Requirement 10.9 would name Artifacts that are fully
embedded. Each branch therefore carries an existence test against the Embedding
table for the Artifact and its kind, and the state column stays the branch's
leading term so the partial index still selects the rows and still supplies the
order. The alternative remedy, exempting the state column from the Ledger's
`UPDATE` revocation, is refused: an editable Ledger row is what the append-only
guarantee and the hash chain exist to rule out.

**The neighbour query's tenancy filter is part of the predicate rather than
applied afterwards.** The tenancy term admits an Embedding only when an
unsuperseded Attribution_Version binds its Artifact to one of the Clients the
caller presented as a bound array, so a page of k results is k results the caller
may see rather than k rows filtered down to fewer. The optional cosine ceiling
sits in the same predicate for the same reason. The ordering is by L2 distance,
which is what the delivered index serves, while the projected distance is the
cosine value the thresholds are stated in; unit normalisation at write time is
what makes those the same ordering.

**The query has two forms, they are composed from the terms they share, and which
one is sent is a recorded probe result.** The delivered cluster reports a
distributed vector index and serves the ordering from it. A tier that reports no
such index answers the same question by an exact scan over a candidate set the
covering attribution index bounds, with the same projection, the same tenancy
admission, the same ceiling, and the same ordering, so the fallback is the same
question answered more slowly rather than a different question answered quickly.
The two statements are built from one set of module-level terms, which is what
makes that claim structural instead of a promise to keep two literals in step.
The choice is read from the capability record the store already holds rather than
from a cluster version string and rather than from a read of its own, and taking
the fallback emits a measurement, so a tier running on the scan is visible.

A note on the state column and the connecting role. No role holds `UPDATE` on the
Ledger, so an Event's state is fixed by the statement that appended it and this
module sends no statement that would move it. The transition statement names the
Derived_Artifact table alone, and that table's update guard admits the state
column to the erasure path and to the administrative path; the capture path
establishes the state at insert instead. That asymmetry is deliberate rather than
an oversight to be corrected: the append-only Ledger is what the hash chain's
tamper evidence rests on, and exempting one column from the revocation would make
a Ledger row editable in place. The sweep is what reconciles it, by reading the
stored vector rather than the frozen column.

Every statement here is a whole module-level literal, every caller-supplied value
is a bound parameter, and no identifier is ever interpolated. The vector travels
as bound text with the cast written into the statement, so this module needs no
driver-side vector adapter and stays importable with no driver installed. The two
state literals the sweep matches on are written into the statement text because
they are this module's own constants and because they are what the partial
indexes over the pending rows are defined by.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from molt.errors import EmbeddingAlreadyStoredError, StoreError
from molt.models.artifact import (
    EMBEDDABLE_KINDS,
    EMBEDDING_DIMENSION,
    ArtifactKind,
    DerivedArtifact,
    DerivedArtifactKind,
    require_unit_interval,
)
from molt.models.event import EmbeddingState, require_aware
from molt.store import Cursor, MemoryStore
from molt.store.capability import VECTOR_INDEX
from molt.telemetry import Severity, log, metric

__all__ = [
    "COMPONENT",
    "COSINE_CEILING",
    "COSINE_FLOOR",
    "DEFAULT_CANDIDATE_CAP",
    "DEFAULT_EXCERPT_CHARACTERS",
    "DEFAULT_NEIGHBOUR_LIMIT",
    "DEFAULT_PENDING_LIMIT",
    "EMBEDDING_UNIQUE_CONSTRAINT",
    "INSERT_ARTIFACT_STATEMENT",
    "INSERT_EMBEDDING_STATEMENT",
    "MARK_STATE_STATEMENT",
    "MAX_CANDIDATE_CAP",
    "MAX_EXCERPT_CHARACTERS",
    "MAX_NEIGHBOUR_LIMIT",
    "MAX_PENDING_LIMIT",
    "NEAREST_SCAN_STATEMENT",
    "NEAREST_STATEMENT",
    "NORM_TOLERANCE",
    "PENDING_STATE",
    "PRINCIPAL_SCOPE_QUERY",
    "RECALL_SCAN_STATEMENT",
    "RECALL_STATEMENT",
    "SELECT_PENDING_STATEMENT",
    "TRANSITION_STATES",
    "UNIQUE_VIOLATION_STATE",
    "VECTOR_INDEX_UNAVAILABLE_METRIC",
    "ArtifactWrite",
    "EmbeddingWrite",
    "Neighbour",
    "PendingArtifact",
    "PrincipalScope",
    "RecallRow",
    "index_served",
    "insert_artifact",
    "insert_artifact_with_embedding",
    "insert_embedding",
    "mark_embedding_state",
    "mark_state",
    "nearest",
    "pending_artifacts",
    "principal_scope",
    "recall_page",
    "require_unit_vector",
    "select_nearest",
    "select_pending",
    "select_principal_scope",
    "select_recall_page",
    "vector_text",
    "write_derived_artifact",
    "write_embedding",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# How far a vector's L2 norm may sit from one and still be called unit length.
# A provider's own scaling and the scaling performed here both accumulate
# floating-point error, so an exact comparison would refuse vectors that are
# unit length in every sense that matters to the ordering.
NORM_TOLERANCE: Final[float] = 1e-6

# The closed interval a cosine distance occupies, and so the interval a ceiling
# presented by a caller must lie in. A ceiling outside it either admits nothing
# or admits everything, and both are more likely a mistake than an intention.
COSINE_FLOOR: Final[float] = 0.0
COSINE_CEILING: Final[float] = 2.0

# The states a stored Artifact's embedding state may be moved to after it was
# written. The absent state is not among them: a row that owed no vector when it
# was written does not begin owing one later, and a row that did owe one cannot
# have that obligation withdrawn by an update.
TRANSITION_STATES: Final[frozenset[EmbeddingState]] = frozenset(
    {EmbeddingState.PENDING, EmbeddingState.EMBEDDED, EmbeddingState.FAILED}
)

# The state the sweep selects. It appears spelled out in the sweep statement
# rather than bound or concatenated in, because that statement stays one whole
# literal and because the text of the predicate is what both partial indexes over
# the pending rows are defined by. The unit suite asserts the two agree, so the
# spelling cannot drift from the enumeration it stands for.
PENDING_STATE: Final[str] = EmbeddingState.PENDING.value

# How many rows each read returns when a caller names no bound, and the ceiling a
# caller may not ask past. The bound is a parameter of both read statements, so
# no caller can ask for an unbounded scan of a corpus.
DEFAULT_NEIGHBOUR_LIMIT: Final[int] = 10
MAX_NEIGHBOUR_LIMIT: Final[int] = 1000
DEFAULT_PENDING_LIMIT: Final[int] = 100
MAX_PENDING_LIMIT: Final[int] = 10000

# How many attributed Artifacts the exact-scan fallback may consider, and the
# ceiling a caller may not raise that past. The default covers the corpus size
# the latency obligation is stated for, so on a tier without the index the
# fallback answers the same question as the index-served form rather than an
# approximation of it, while the cap still keeps a runaway scan out of the
# critical path.
DEFAULT_CANDIDATE_CAP: Final[int] = 100000
MAX_CANDIDATE_CAP: Final[int] = 1000000

# How much of an Artifact's text one recall result carries, and the ceiling a
# caller may not ask past. The recall answer travels to an agent's context window
# rather than to a report, so a whole body would crowd out the reason the query
# was asked; the bound is a parameter of the statement so the cluster truncates
# rather than this process receiving text it discards.
DEFAULT_EXCERPT_CHARACTERS: Final[int] = 400
MAX_EXCERPT_CHARACTERS: Final[int] = 8192

# The state the cluster reports when a write repeats a value held unique, and the
# constraint that holds one vector per Artifact per provider-and-model pair. The
# state is read off the failure rather than inferred from a type, because the
# driver is imported lazily and its exception classes are not nameable here.
UNIQUE_VIOLATION_STATE: Final[str] = "23505"
EMBEDDING_UNIQUE_CONSTRAINT: Final[str] = "embedding_unique_per_model"

# The attribute names a driver may carry the state under, matching the pair the
# transaction wrapper reads.
_STATE_ATTRIBUTES: Final[tuple[str, ...]] = ("sqlstate", "pgcode")

# How many columns each row shape carries, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_ARTIFACT_ROW_WIDTH: Final[int] = 2
_EMBEDDING_ROW_WIDTH: Final[int] = 2
_STATE_ROW_WIDTH: Final[int] = 1
_PENDING_ROW_WIDTH: Final[int] = 4
_NEIGHBOUR_ROW_WIDTH: Final[int] = 4
_RECALL_ROW_WIDTH: Final[int] = 13
_PRINCIPAL_ROW_WIDTH: Final[int] = 4

# The brackets a vector's text form carries, and the separator between its
# components. The form is the one the vector type parses, and rendering it here
# rather than adapting a sequence in the driver is what keeps this module
# importable with no driver installed.
_VECTOR_OPEN: Final[str] = "["
_VECTOR_CLOSE: Final[str] = "]"
_VECTOR_SEPARATOR: Final[str] = ","

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The Derived_Artifact insert. Every column the caller decides is bound, and the
# embedding state is the derived value rather than the presented one, for the
# reason the module docstring gives.
INSERT_ARTIFACT_STATEMENT: Final[str] = (
    "INSERT INTO derived_artifact ("
    "id, kind, owner_client_id, body, content_digest, derivation_method, revision, "
    "created_at, updated_at, redacted_at, embedding_state, expires_at, procedure_confidence) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "RETURNING id, embedding_state"
)

# The Embedding insert. The identifier and the creation instant are the column
# defaults, so the row's own identity is the cluster's to assign and comes back
# rather than being presented. The width and the unit-norm assertion are bound
# explicitly rather than left to their defaults, because a row asserting them is
# only evidence if the writing statement stated them.
INSERT_EMBEDDING_STATEMENT: Final[str] = (
    "INSERT INTO embedding ("
    "artifact_id, artifact_kind, client_id, provider, model_id, dimension, normalised, "
    "vec, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::VECTOR, %s) "
    "RETURNING id, created_at"
)

# The one state transition, scoped by tenant as every write in this schema is.
# It names the state column alone, which is what the Derived_Artifact update
# guard confines a non-administrative writer to on that column set, and it
# returns the state the row now holds so a caller learns the outcome rather than
# assuming it.
MARK_STATE_STATEMENT: Final[str] = (
    "UPDATE derived_artifact SET embedding_state = %s "
    "WHERE id = %s AND owner_client_id = %s "
    "RETURNING embedding_state"
)

# The pending sweep, over the union of the two kinds that carry embeddable text.
# Each branch is served by that table's partial index over the pending rows, and
# the ordering is ascending by creation instant with the identifier as the
# tie-break, so the order is total and a drain is fair rather than arbitrary.
#
# Each branch carries one further term, and it is there because the state column
# is not the whole truth about whether an Artifact owes a vector. No role holds
# `UPDATE` on the Ledger, so an Event's state column is fixed by the statement
# that appended it and stays `pending` for as long as the row exists, whatever
# vector later landed for it. A sweep reading the column alone therefore returns
# every Event that has ever been embedded, on every pass, for as long as the
# corpus lives: a drain pays a provider call per already-embedded Event in every
# fresh container, and the unembedded-coverage count Requirement 10.9 asks a
# certificate to report names Artifacts that are fully embedded.
#
# The remedy is on this side rather than on the privilege side. Exempting the
# state column from the Ledger's `UPDATE` revocation would make a Ledger row
# editable in place, and the append-only Ledger is what the hash chain's tamper
# evidence rests on, so the column stays frozen and the sweep stops relying on it
# alone. Requirement 10.9 conditions the count on an Artifact *being* in the
# pending-embedding state, and an Artifact a vector is stored for is not in it, so
# each branch asks exactly that: does an Embedding row stand for this Artifact of
# this kind. The two branches then mean the same thing as each other, which the
# column reading did not: a Derived_Artifact leaves the sweep the moment its
# vector lands, and now an Event does too.
#
# The existence test names the Artifact and its kind and no more. The state a row
# carries is one state for the row rather than one per provider, so *owes a
# vector* is answered by whether a vector stands for the Artifact at all; a
# re-embedding under a second provider is a backfill of stored rows rather than
# work this sweep reports, and the uniqueness constraint is what tells a drain
# that has produced one anyway that the pair is already held.
#
# The state column stays the leading term of each branch, so the partial index
# over the pending rows still selects the rows and still supplies the ascending
# order, and the existence test is an anti lookup-join into the uniqueness index
# over the rows that survive it: that index leads on the Artifact and its kind,
# which is exactly the pair the test names, so no index of its own is needed.
#
# The sweep is composed from named terms rather than written as one literal, for
# the reason the neighbour statement below is: every term is this module's own and
# a reader can see that the two branches ask the same question of two tables.
_PENDING_EVENT_TERM: Final[str] = (
    "SELECT l.id, 'event' AS kind, l.client_id, l.recorded_at AS created_at "
    "FROM ledger AS l WHERE l.embedding_state = 'pending' "
    "AND NOT EXISTS (SELECT 1 FROM embedding AS m "
    "WHERE m.artifact_id = l.id AND m.artifact_kind = 'event') "
)
_PENDING_DERIVED_TERM: Final[str] = (
    "SELECT d.id, 'derived_artifact' AS kind, d.owner_client_id AS client_id, d.created_at "
    "FROM derived_artifact AS d WHERE d.embedding_state = 'pending' "
    "AND NOT EXISTS (SELECT 1 FROM embedding AS m "
    "WHERE m.artifact_id = d.id AND m.artifact_kind = 'derived_artifact')"
)
_PENDING_PROJECTION: Final[str] = "SELECT id, kind, client_id, created_at FROM ("
_PENDING_UNION_TERM: Final[str] = "UNION ALL "
_PENDING_ORDERING_TERM: Final[str] = ") AS pending ORDER BY created_at ASC, id ASC LIMIT %s"
SELECT_PENDING_STATEMENT: Final[str] = (
    _PENDING_PROJECTION
    + _PENDING_EVENT_TERM
    + _PENDING_UNION_TERM
    + _PENDING_DERIVED_TERM
    + _PENDING_ORDERING_TERM
)

# The neighbour query, in the two forms a tier can serve it, composed from the
# terms the two forms share rather than written out twice.
#
# What every term does, and why it is where it is:
#
# The projection reports the cosine distance, because that is what every
# threshold in this design is expressed in, while the ordering is by L2 distance,
# because that is what the reported operator class of the index serves. Those are
# the same ordering over unit vectors, which is what the write-time normalisation
# guarantees, so the two forms below rank identically and neither of them has to
# restate the ordering to say so.
#
# The tenancy term admits an Embedding only through an unsuperseded
# Attribution_Version naming one of the Clients the caller presented, so a Client
# whose claim on an Artifact was closed no longer reaches that Artifact's vector,
# and the permitted Clients travel as one bound array rather than as a rendered
# list. The restriction is the attribution module's canonical current-version
# predicate, and the statement sweep of that module asserts that these two forms
# and the current-attribution query all carry the same one, so this filter and
# that query cannot come to mean two different things by *current*.
#
# The two forms express that same admission differently and no more: the
# index-served form asks it as an existence test, and the exact-scan form asks it
# as membership in a bounded set of attributed Artifacts, which is set-valued and
# so admits an Artifact bound to two permitted Clients exactly once, as the
# existence test does.
#
# The ceiling term is written so that binding null admits every distance, which
# is what lets one form serve a bounded search and an unbounded one.
#
# The row cap is the caller's own bound on results and is present in both forms.
# The exact-scan form carries one further bound the index-served form does not
# need: a cap on how many attributed Artifacts the scan may consider, which is
# what keeps the cost of computing a distance per candidate bounded on a tier that
# cannot serve the ordering from an index. Within that cap the two forms answer
# identically, which is the property the fallback exists to preserve; a cap
# applied to the ordered results instead would have made the fallback answer a
# different question from the primary path rather than the same question more
# slowly.
_NEIGHBOUR_PROJECTION: Final[str] = (
    "SELECT e.artifact_id, e.artifact_kind, e.client_id, "
    "(e.vec <=> %s::VECTOR) AS cosine_distance "
    "FROM embedding AS e "
)
_TENANCY_TERM: Final[str] = (
    "WHERE EXISTS ("
    "SELECT 1 FROM client_binding AS b "
    "WHERE b.artifact_id = e.artifact_id "
    "AND b.client_id = ANY (%s::UUID[]) "
    "AND b.superseded_by IS NULL) "
)
_BOUNDED_TENANCY_TERM: Final[str] = (
    "WHERE e.artifact_id IN ("
    "SELECT b.artifact_id FROM client_binding AS b "
    "WHERE b.client_id = ANY (%s::UUID[]) "
    "AND b.superseded_by IS NULL "
    "LIMIT %s) "
)
_CEILING_TERM: Final[str] = "AND (%s::FLOAT8 IS NULL OR (e.vec <=> %s::VECTOR) <= %s::FLOAT8) "
_ORDERING_TERM: Final[str] = "ORDER BY e.vec <-> %s::VECTOR "
_ROW_CAP_TERM: Final[str] = "LIMIT %s"

# The form the delivered cluster serves, and the expected path on it.
NEAREST_STATEMENT: Final[str] = (
    _NEIGHBOUR_PROJECTION + _TENANCY_TERM + _CEILING_TERM + _ORDERING_TERM + _ROW_CAP_TERM
)

# The form for a tier reporting no distributed vector index: the same projection,
# the same tenancy admission, the same ceiling, and the same ordering, over a
# candidate set the covering attribution index bounds.
NEAREST_SCAN_STATEMENT: Final[str] = (
    _NEIGHBOUR_PROJECTION + _BOUNDED_TENANCY_TERM + _CEILING_TERM + _ORDERING_TERM + _ROW_CAP_TERM
)

# The measurement emitted whenever a neighbour query is answered by the exact
# scan, so a tier running on the fallback is visible rather than merely slower.
VECTOR_INDEX_UNAVAILABLE_METRIC: Final[str] = "store.vector_index_unavailable"

# The recall page: the same neighbour question, projected with the provenance an
# agent is answered with, and staged so the ordering the index serves is not lost
# to the tenancy filter.
#
# **Why this is staged rather than one flat statement.** On this cluster any
# predicate on a column other than the vector takes the plan off the distributed
# vector index, and the tenancy admission is exactly such a predicate. The
# neighbour query above accepts that: it applies the tenancy term beside the
# ordering and is answered by a bounded seek and a sort, which is exact and is
# what the residue detector wants. Recall cannot accept it, because recall runs on
# the agent's critical path over the whole fleet's corpus. So the first stage here
# carries the ordering expression and nothing else, which is the one shape the
# index serves, bounded by a candidate pool the caller sizes; every later stage
# reads that stage's output rather than the Embedding table, so no later predicate
# can reach back and change how the first stage was planned.
#
# **What the staging costs, stated rather than hidden.** A page assembled from a
# candidate pool is the true nearest k among the pool rather than among the
# corpus, so a caller permitted a small slice of a large corpus can be answered
# with fewer than k results even though more exist further down the ordering. That
# is the trade the over-fetch factor is chosen against, and the caller is told
# when the pool saturated, so under-recall is visible rather than silent. Nothing
# admitted is ever wrong: every returned row carries a permitted binding, because
# the admission term is the same one the neighbour query uses.
#
# **The two forms differ in one stage and no more.** A tier reporting no vector
# index cannot serve the ordering, so its first stage narrows to attributed
# Artifacts by the covering attribution index before it ranks them, using the same
# bounded tenancy term the exact-scan neighbour query uses. Every stage after that
# is shared text, so the two forms carry the same admission, the same provenance
# joins, the same floor, and the same total ordering by construction.
_RECALL_CANDIDATE_TERM: Final[str] = (
    "WITH candidates AS ("
    "SELECT e.artifact_id, e.artifact_kind, e.client_id, "
    "(e.vec <=> %s::VECTOR) AS cosine_distance "
    "FROM embedding AS e "
)
_RECALL_CANDIDATE_BOUND: Final[str] = "ORDER BY e.vec <-> %s::VECTOR LIMIT %s), "

# The admission stage, which is the neighbour query's own tenancy term over the
# candidate pool rather than over the Embedding table. Aliasing the pool as `e`
# is what lets the term be the same text in both places, so this filter and that
# one cannot come to mean two different things by *a Client may see this*.
_RECALL_ADMISSION_TERM: Final[str] = (
    "permitted AS ("
    "SELECT e.artifact_id, e.artifact_kind, e.client_id, e.cosine_distance "
    "FROM candidates AS e " + _TENANCY_TERM + "), "
)

# The provenance stage: one branch per embeddable kind, because the two kinds
# reach a Session by different routes and Requirement 13.3 asks for the Session
# either way.
#
# An Event names its Session on the row, so its branch is two joins and the
# outcome comes from that Session.
#
# A Derived_Artifact names no Session at all — the table carries an owning Client
# and no Session column — so its origin is resolved through the Lineage_Graph: the
# earliest direct parent that is a Session, or the Session of the earliest direct
# parent that is an Event. The lookup is inner rather than outer on purpose. A
# Derived_Artifact whose direct parents name neither a Session nor an Event cannot
# answer Requirement 13.2 or 13.3, and returning it with three null fields would
# be a result the reader has to discard; leaving it out means the page is filled
# from Artifacts that can answer instead of being quietly shortened. The bound is
# one level of lineage, which is what a Summary or a Learned_Procedure distilled
# from a Session's own Events carries; a chain that reaches a Session only through
# another Derived_Artifact is outside it, and that is a stated limit rather than
# an oversight.
_RECALL_ORIGIN_TERM: Final[str] = (
    "origin AS ("
    "SELECT p.artifact_id, p.artifact_kind, p.client_id, p.cosine_distance, "
    "l.session_id, l.machine_id, l.occurred_at, s.outcome, l.category AS content_kind, "
    "left(coalesce(l.text_body, ''), %s) AS excerpt, NULL::FLOAT8 AS procedure_confidence "
    "FROM permitted AS p "
    "JOIN ledger AS l ON l.id = p.artifact_id "
    "JOIN session AS s ON s.id = l.session_id "
    "WHERE p.artifact_kind = 'event' "
    "UNION ALL "
    "SELECT p.artifact_id, p.artifact_kind, p.client_id, p.cosine_distance, "
    "o.session_id, o.machine_id, o.occurred_at, o.outcome, d.kind AS content_kind, "
    "left(d.body, %s) AS excerpt, d.procedure_confidence "
    "FROM permitted AS p "
    "JOIN derived_artifact AS d ON d.id = p.artifact_id "
    "JOIN LATERAL ("
    "SELECT s.id AS session_id, s.machine_id AS machine_id, "
    "s.started_at AS occurred_at, s.outcome AS outcome "
    "FROM lineage_edge AS le JOIN session AS s ON s.id = le.parent_id "
    "WHERE le.child_id = d.id AND le.parent_kind = 'session' "
    "UNION ALL "
    "SELECT s.id, l.machine_id, l.occurred_at, s.outcome "
    "FROM lineage_edge AS le JOIN ledger AS l ON l.id = le.parent_id "
    "JOIN session AS s ON s.id = l.session_id "
    "WHERE le.child_id = d.id AND le.parent_kind = 'event' "
    "ORDER BY occurred_at ASC, session_id ASC LIMIT 1"
    ") AS o ON true "
    "WHERE p.artifact_kind = 'derived_artifact'"
    "), "
)

# The floor stage. The comparison is a projected column rather than a filter,
# because the count of what the floor excluded is a measurement the caller owes
# and a filter here would have destroyed it. Only a Learned_Procedure carries a
# confidence value at all — the schema holds that as an equivalence with the kind
# — so this term applies to procedures and to nothing else, and an Event or a
# Summary is never below a floor it has no value for.
_RECALL_FLOOR_TERM: Final[str] = (
    "flagged AS ("
    "SELECT o.artifact_id, o.artifact_kind, o.client_id, o.cosine_distance, o.session_id, "
    "o.machine_id, o.occurred_at, o.outcome, o.content_kind, o.excerpt, "
    "o.procedure_confidence, "
    "(o.procedure_confidence IS NOT NULL AND o.procedure_confidence < %s::FLOAT8) "
    "AS below_floor "
    "FROM origin AS o"
    "), "
)

# The ranking stage, which is where the floor stops being a flag and starts being
# an exclusion. The row numbering is partitioned by the flag, so the admitted rows
# are numbered among themselves and a procedure below the floor consumes none of
# the k positions. That is Requirement 49.9 applied inside the statement rather
# than after truncation: the floor cannot shrink a k-result page, it can only
# change which rows fill it.
#
# The order the numbering uses is the whole ordering the design states: ascending
# cosine distance, then descending Procedure_Confidence with absence last, then
# the Artifact identifier. The identifier is what makes it total, so two results
# at one distance with one confidence still have exactly one order.
#
# The exclusion tally is a window sum over the whole stage rather than a count
# taken after filtering, so it counts every excluded procedure and not only the
# ones that would have fitted on the page.
_RECALL_RANKING_TERM: Final[str] = (
    "ranked AS ("
    "SELECT f.artifact_id, f.artifact_kind, f.client_id, f.cosine_distance, f.session_id, "
    "f.machine_id, f.occurred_at, f.outcome, f.content_kind, f.excerpt, "
    "f.procedure_confidence, f.below_floor, "
    "sum(CASE WHEN f.below_floor THEN 1 ELSE 0 END) OVER () AS floor_excluded, "
    "row_number() OVER (PARTITION BY f.below_floor ORDER BY f.cosine_distance ASC, "
    "f.procedure_confidence DESC NULLS LAST, f.artifact_id ASC) AS position "
    "FROM flagged AS f"
    ") "
)

# The page. The admitted rows are the first k of their own numbering, and one
# excluded row rides along carrying the tally.
#
# That one row is here for a reason worth stating, because it looks like an
# accident otherwise. The tally is a column on a row, so a page holding no row
# reports no tally — and a page holding no row is exactly the case where the floor
# mattered most, because it is the case where the floor excluded everything the
# tenant could otherwise have seen. Carrying one flagged row makes the measurement
# observable in that case too. It is filtered out of the answer by the reader
# below, and it is ordered last, so it can neither reach a caller nor disturb the
# ordering of what does.
_RECALL_PAGE_TERM: Final[str] = (
    "SELECT artifact_id, artifact_kind, client_id, cosine_distance, session_id, machine_id, "
    "occurred_at, outcome, content_kind, excerpt, procedure_confidence, below_floor, "
    "floor_excluded FROM ranked "
    "WHERE (NOT below_floor AND position <= %s) OR (below_floor AND position = 1) "
    "ORDER BY below_floor ASC, cosine_distance ASC, procedure_confidence DESC NULLS LAST, "
    "artifact_id ASC"
)

_RECALL_BODY: Final[str] = (
    _RECALL_ADMISSION_TERM
    + _RECALL_ORIGIN_TERM
    + _RECALL_FLOOR_TERM
    + _RECALL_RANKING_TERM
    + _RECALL_PAGE_TERM
)

# The form the delivered cluster serves: the candidate stage carries the ordering
# expression alone, which is the shape the distributed vector index answers.
RECALL_STATEMENT: Final[str] = _RECALL_CANDIDATE_TERM + _RECALL_CANDIDATE_BOUND + _RECALL_BODY

# The form for a tier reporting no distributed vector index: the candidate stage
# narrows to attributed Artifacts first, by the same bounded tenancy term the
# exact-scan neighbour query uses, and every stage after it is the same text.
RECALL_SCAN_STATEMENT: Final[str] = (
    _RECALL_CANDIDATE_TERM + _BOUNDED_TENANCY_TERM + _RECALL_CANDIDATE_BOUND + _RECALL_BODY
)

# The tenancy resolution a recall request cannot supply for itself. The recall
# request body names a Session and no Client, so the Clients a caller may see are
# read from the stored Session row rather than presented: a request can name a
# Session, and the Session's Client is the cluster's own record of whose data that
# Session produces. The Client's retention interval comes back in the same read,
# because the recall Event this path appends needs an expiry and that expiry
# follows from the Client rather than from the query.
PRINCIPAL_SCOPE_QUERY: Final[str] = (
    "SELECT s.client_id, s.agent_cli, s.machine_id, c.retention_interval "
    "FROM session AS s JOIN client AS c ON c.id = s.client_id WHERE s.id = %s"
)

# The labels the transactions of this module appear under in a log record and in
# the note an exhausted retry attaches.
_ARTIFACT_LABEL: Final[str] = "artifact_write"
_EMBEDDING_LABEL: Final[str] = "embedding_write"
_STATE_LABEL: Final[str] = "embedding_state"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbeddingWrite:
    """One Embedding to write, checked for width and unit length on construction.

    Both checks belong here rather than at the statement, because a vector that
    fails either would be ranked by L2 distance and judged by cosine distance,
    and the two agree only on unit vectors of the fixed width. Refusing at
    construction means no statement is sent and no partial write is possible.

    The provider name travels alongside the model identifier because a model
    identifier alone does not say which service produced the vector, and the
    uniqueness constraint spans both, so a corpus embedded across a provider
    change stays distinguishable row by row.
    """

    artifact_id: UUID
    artifact_kind: ArtifactKind
    client_id: UUID
    provider: str
    model_id: str
    vec: tuple[float, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        kind = ArtifactKind(self.artifact_kind)
        if kind not in EMBEDDABLE_KINDS:
            raise ValueError("only an event or a derived artifact carries an embedding")
        if not self.provider:
            raise ValueError("an embedding row records the provider that produced the vector")
        if not self.model_id:
            raise ValueError("an embedding row records the model that produced the vector")
        require_unit_vector(self.vec)
        require_aware(self.expires_at, "an embedding expiry")

    @property
    def dimension(self) -> int:
        """The width the row records, which the checks above have fixed."""
        return len(self.vec)


@dataclass(frozen=True, slots=True)
class ArtifactWrite:
    """What one Artifact-and-Embedding transaction committed.

    The embedding identifier is absent exactly when no vector accompanied the
    Artifact, which is the same condition the recorded state reports, so the two
    fields cannot disagree about whether a vector was written.
    """

    artifact_id: UUID
    embedding_id: UUID | None
    embedding_state: EmbeddingState


@dataclass(frozen=True, slots=True)
class PendingArtifact:
    """One Artifact still owing a vector, as the sweep returns it."""

    artifact_id: UUID
    artifact_kind: ArtifactKind
    client_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Neighbour:
    """One result of the neighbour query, carrying the distance it was ranked by.

    The distance is the cosine value, which is what every threshold in this
    design is expressed in, even though the ordering the cluster performed was by
    L2 distance over the same unit vectors.
    """

    artifact_id: UUID
    artifact_kind: ArtifactKind
    client_id: UUID
    cosine_distance: float


@dataclass(frozen=True, slots=True)
class RecallRow:
    """One row of the recall page, with the provenance a returned Artifact carries.

    Every field but the last three answers a criterion of Requirement 13 directly:
    the distance is the value the ordering used, the Session identifier, the
    machine identifier, and the instant are the originating Session's, and the
    outcome is that Session's own classification as the row stores it.

    The confidence is present for a Learned_Procedure and absent for every other
    kind, which is an equivalence the schema enforces rather than a convention
    this shape hopes for. The exclusion tally is the same value on every row of one
    page, because it describes the page rather than the row.
    """

    artifact_id: UUID
    artifact_kind: ArtifactKind
    client_id: UUID
    cosine_distance: float
    session_id: UUID
    machine_id: str
    occurred_at: datetime
    outcome: str
    content_kind: str
    excerpt: str
    procedure_confidence: float | None
    floor_excluded: int


@dataclass(frozen=True, slots=True)
class PrincipalScope:
    """The Client one Session belongs to, and what an Event for it needs.

    This is the tenancy a recall request resolves to. It comes from the stored
    Session row, so a request cannot widen its own permitted Client set by saying
    so.
    """

    client_id: UUID
    agent_cli: str
    machine_id: str
    retention: timedelta


# ---------------------------------------------------------------------------
# The vector, checked and rendered
# ---------------------------------------------------------------------------


def require_unit_vector(vec: Sequence[float]) -> Sequence[float]:
    """Return a vector unchanged, refusing one the ordering would not hold for.

    Three refusals, in the order a reader would ask about them. A width other
    than the one the column declares cannot be stored at all. A component that is
    not a finite number has no distance to anything, and would make the computed
    norm meaningless rather than merely wrong. A norm away from one by more than
    the tolerance means L2 ordering and cosine ordering part company, so the
    thresholds would stop meaning what a certificate says they mean.
    """
    if len(vec) != EMBEDDING_DIMENSION:
        raise ValueError(
            f"an embedding vector carries {len(vec)} component(s) where the column "
            f"holds exactly {EMBEDDING_DIMENSION}"
        )
    for component in vec:
        if not math.isfinite(component):
            raise ValueError("an embedding vector component must be a finite number")
    norm = math.sqrt(math.fsum(component * component for component in vec))
    if abs(norm - 1.0) > NORM_TOLERANCE:
        raise ValueError(
            f"an embedding vector is scaled to unit length before it is written; this "
            f"one has an L2 norm of {norm}"
        )
    return vec


def vector_text(vec: Sequence[float]) -> str:
    """Render a vector in the text form the vector type parses.

    Each component is rendered in the shortest form that reads back as the same
    number, so the stored vector is the vector that was checked rather than a
    rounding of it.
    """
    return _VECTOR_OPEN + _VECTOR_SEPARATOR.join(repr(float(x)) for x in vec) + _VECTOR_CLOSE


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def insert_embedding(cursor: Cursor, request: EmbeddingWrite) -> UUID:
    """Write one Embedding row on a caller's cursor.

    This is the form an Artifact write composes, and the form an Event's
    Embedding uses: Requirement 10.5 puts the vector in the same transaction as
    the row it represents, so the caller frames the transaction and hands its
    cursor here.

    Args:
        cursor: The cursor the caller's transaction is running on.
        request: The Embedding to write. Its width and unit length were checked
            when it was built, so nothing about the vector is decided here.

    Returns:
        The identifier the cluster assigned the Embedding row.

    Raises:
        EmbeddingAlreadyStoredError: A vector for this Artifact under this
            provider and model is already stored, so the pair the schema holds
            unique would have been repeated. The class is named rather than the
            family base because a caller acts on this refusal differently from
            every other, and the type is what says so.
        StoreError: The insert reported no row.
    """
    kind = ArtifactKind(request.artifact_kind).value
    try:
        cursor.execute(
            INSERT_EMBEDDING_STATEMENT,
            (
                request.artifact_id,
                kind,
                request.client_id,
                request.provider,
                request.model_id,
                request.dimension,
                True,
                vector_text(request.vec),
                request.expires_at,
            ),
        )
    except Exception as error:
        translated = _translated(error, request)
        if translated is None:
            raise
        raise translated from error
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            f"the embedding write for the artifact {request.artifact_id} reported no row, "
            "so no embedding identifier was assigned"
        )
    return _as_uuid(_column(row, 0, _EMBEDDING_ROW_WIDTH))


def insert_artifact(
    cursor: Cursor,
    artifact: DerivedArtifact,
    *,
    embedding_state: EmbeddingState,
) -> UUID:
    """Write one Derived_Artifact row on a caller's cursor, at a stated state.

    The state is a keyword rather than a field read off the record, because the
    caller of this function is the write that knows whether a vector accompanied
    the row, and a state read off the record would be the caller's claim rather
    than what happened.
    """
    require_aware(artifact.created_at, "a derived artifact creation timestamp")
    cursor.execute(
        INSERT_ARTIFACT_STATEMENT,
        (
            artifact.id,
            DerivedArtifactKind(artifact.kind).value,
            artifact.owner_client_id,
            artifact.body,
            artifact.content_digest,
            artifact.derivation_method,
            artifact.revision,
            artifact.created_at,
            artifact.updated_at,
            artifact.redacted_at,
            EmbeddingState(embedding_state).value,
            artifact.expires_at,
            artifact.procedure_confidence,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            f"the write of the derived artifact {artifact.id} reported no row, so nothing "
            "is known to have been stored"
        )
    return _as_uuid(_column(row, 0, _ARTIFACT_ROW_WIDTH))


def insert_artifact_with_embedding(
    cursor: Cursor,
    artifact: DerivedArtifact,
    embedding: EmbeddingWrite | None,
) -> ArtifactWrite:
    """Write a Derived_Artifact and its Embedding on one cursor, in that order.

    The order is not a convention a caller could get wrong: the Embedding row
    names the Artifact, and there is no way to call this that writes the vector
    first. The state written is derived from whether a vector was given, and a
    vector naming another Artifact or another tenant is refused before either
    statement is sent.
    """
    state = _derived_state(artifact, embedding)
    artifact_id = insert_artifact(cursor, artifact, embedding_state=state)
    if embedding is None:
        return ArtifactWrite(artifact_id=artifact_id, embedding_id=None, embedding_state=state)
    embedding_id = insert_embedding(cursor, embedding)
    return ArtifactWrite(
        artifact_id=artifact_id,
        embedding_id=embedding_id,
        embedding_state=state,
    )


def write_derived_artifact(
    store: MemoryStore,
    artifact: DerivedArtifact,
    *,
    embedding: EmbeddingWrite | None = None,
) -> ArtifactWrite:
    """Write a Derived_Artifact and its Embedding in one serializable transaction.

    Either both rows land or neither does, which is the whole of Requirement
    10.5: a corpus that held an Artifact whose vector never arrived would be
    searchable by meaning for some of its content and silently not for the rest.

    Args:
        store: The connection surface the transaction is framed by.
        artifact: The Derived_Artifact to write.
        embedding: The vector standing for that Artifact's text, or None when no
            vector is available yet. Absent, the row is written owing one.

    Returns:
        The identifiers that landed and the embedding state that was recorded.

    Raises:
        ValueError: The Embedding names another Artifact or another tenant, or
            the Artifact declares an embedded state with no vector to support it.
            Nothing was written.
        EmbeddingAlreadyStoredError: A vector for this Artifact under this
            provider and model is already stored. Nothing was written.
    """

    def body(cursor: Cursor) -> ArtifactWrite:
        return insert_artifact_with_embedding(cursor, artifact, embedding)

    return store.in_serializable(body, label=_ARTIFACT_LABEL)


def write_embedding(store: MemoryStore, request: EmbeddingWrite) -> UUID:
    """Write one Embedding in a transaction of its own.

    This is the drain's path: the Artifact was written earlier owing a vector,
    the provider has since answered, and the vector arrives on its own. A caller
    writing the Artifact in the same breath uses `write_derived_artifact`
    instead, because the vector belongs in the Artifact's transaction rather than
    in one of its own.
    """

    def body(cursor: Cursor) -> UUID:
        return insert_embedding(cursor, request)

    return store.in_serializable(body, label=_EMBEDDING_LABEL)


def mark_state(
    cursor: Cursor,
    artifact_id: UUID,
    client_id: UUID,
    state: EmbeddingState,
) -> EmbeddingState | None:
    """Move one Derived_Artifact's embedding state on a caller's cursor.

    The transition is available for the three states work can be in: owed,
    present, and unobtainable. The absent state is refused, because a row that
    owed no vector does not begin owing one later and a row that owed one cannot
    have the obligation withdrawn by an update.

    Returns:
        The state the row now holds, or None when no row matched the identifier
        and the tenant scope.
    """
    chosen = EmbeddingState(state)
    if chosen not in TRANSITION_STATES:
        raise ValueError(
            "an embedding state transition records work as owed, present, or unobtainable"
        )
    cursor.execute(MARK_STATE_STATEMENT, (chosen.value, artifact_id, client_id))
    row = cursor.fetchone()
    if row is None:
        return None
    return EmbeddingState(_as_str(_column(row, 0, _STATE_ROW_WIDTH)))


def mark_embedding_state(
    store: MemoryStore,
    artifact_id: UUID,
    client_id: UUID,
    state: EmbeddingState,
) -> EmbeddingState | None:
    """Move one Derived_Artifact's embedding state in a transaction of its own."""

    def body(cursor: Cursor) -> EmbeddingState | None:
        return mark_state(cursor, artifact_id, client_id, state)

    return store.in_serializable(body, label=_STATE_LABEL)


def _derived_state(artifact: DerivedArtifact, embedding: EmbeddingWrite | None) -> EmbeddingState:
    """The state to write, derived from whether a vector accompanied the row.

    A vector present makes the state `embedded` whatever the record declared, and
    a disagreement is recorded because it means a caller believed something about
    its own write that the write did not do. A vector absent keeps the declared
    state, except that `embedded` is refused: that state would assert an
    Embedding row this transaction is not writing.
    """
    declared = EmbeddingState(artifact.embedding_state)
    if embedding is None:
        if declared is EmbeddingState.EMBEDDED:
            raise ValueError(
                "an artifact recorded as embedded is written together with the vector "
                "that state asserts"
            )
        return declared
    if embedding.artifact_id != artifact.id:
        raise ValueError("an embedding is written for the artifact it represents and no other")
    if embedding.client_id != artifact.owner_client_id:
        raise ValueError("an embedding carries the tenant of the artifact it represents")
    if ArtifactKind(embedding.artifact_kind) is not ArtifactKind.DERIVED_ARTIFACT:
        raise ValueError("an embedding of a derived artifact records that kind")
    if declared is not EmbeddingState.EMBEDDED:
        log(
            Severity.DEBUG,
            COMPONENT,
            "the stored embedding state was derived from the write rather than presented",
            artifact_id=str(artifact.id),
            presented_state=declared.value,
            stored_state=EmbeddingState.EMBEDDED.value,
        )
    return EmbeddingState.EMBEDDED


def _translated(
    error: BaseException,
    request: EmbeddingWrite,
) -> EmbeddingAlreadyStoredError | None:
    """The failure to raise for a driver refusal this module has a name for.

    A state this module does not name returns nothing and the original failure
    propagates untouched, so a conflict still reaches the retry wrapper's own
    handling and a constraint failure is never renamed into something it is not.

    The refusal carries its own class rather than the family base, so a caller
    tells *the vector is already there* from *the cluster could not be written to*
    by the type it caught rather than by reading the message this raises. The
    drain is the caller that needs the distinction, because the first answer
    settles an Artifact and the second leaves it owed.
    """
    if _state_of(error) != UNIQUE_VIOLATION_STATE:
        return None
    if _constraint_of(error) not in (EMBEDDING_UNIQUE_CONSTRAINT, None):
        return None
    return EmbeddingAlreadyStoredError(
        request.artifact_id,
        request.provider,
        request.model_id,
    )


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
# The pending sweep
# ---------------------------------------------------------------------------


def select_pending(
    cursor: Cursor,
    *,
    limit: int = DEFAULT_PENDING_LIMIT,
) -> tuple[PendingArtifact, ...]:
    """Read the Artifacts still owing a vector, oldest first, on a caller's cursor.

    An Artifact owes a vector when its state column says `pending` and no
    Embedding row stands for it. Both terms are needed because an Event's state
    column can never be moved, so the column alone would report an embedded Event
    as owing a vector for as long as the row exists.
    """
    cursor.execute(SELECT_PENDING_STATEMENT, (_bounded(limit, MAX_PENDING_LIMIT),))
    return tuple(_pending_of(row) for row in cursor.fetchall())


def pending_artifacts(
    store: MemoryStore,
    *,
    limit: int = DEFAULT_PENDING_LIMIT,
) -> tuple[PendingArtifact, ...]:
    """Read the pending sweep on a leased connection, framing no transaction.

    The order is ascending by creation instant, so a bounded drain takes the
    oldest owed vectors rather than an arbitrary sample of them, and a row that
    keeps failing cannot starve the rows behind it by being reread first.
    """

    def body(cursor: Cursor) -> tuple[PendingArtifact, ...]:
        return select_pending(cursor, limit=limit)

    return store.read(body)


# ---------------------------------------------------------------------------
# The neighbour query
# ---------------------------------------------------------------------------


def select_nearest(
    cursor: Cursor,
    query_vector: Sequence[float],
    *,
    permitted_clients: Iterable[UUID],
    limit: int = DEFAULT_NEIGHBOUR_LIMIT,
    max_cosine: float | None = None,
    index_served: bool = True,
    candidate_cap: int = DEFAULT_CANDIDATE_CAP,
) -> tuple[Neighbour, ...]:
    """The k closest Embeddings a caller may see, on a caller's cursor.

    The query vector is held to the same width and unit length as a stored one,
    because the ordering the statement asks for is a cosine ordering only when
    both sides of the distance are unit vectors.

    A caller permitted no Client sees nothing, and that answer is returned
    without a round trip rather than by sending an empty array: neither tenancy
    term admits a row for an empty array, so the statement would cost a scan to
    prove what the empty set already says.

    Args:
        cursor: The cursor the read runs on.
        query_vector: The vector to rank against, unit length and of the fixed
            width.
        permitted_clients: The Clients the caller may see content for.
        limit: How many results to return.
        max_cosine: The cosine ceiling admitted, or None for no ceiling.
        index_served: Whether the tier serves the ordering from the distributed
            vector index. False sends the exact-scan form, which answers the same
            question over a bounded candidate set. The choice belongs to the
            caller because it is a recorded probe result, not something this
            statement can discover.
        candidate_cap: How many attributed Artifacts the exact-scan form may
            consider. Unused by the index-served form.
    """
    require_unit_vector(query_vector)
    bound = _bounded(limit, MAX_NEIGHBOUR_LIMIT)
    ceiling = None if max_cosine is None else _bounded_ceiling(max_cosine)
    candidates = _bounded(candidate_cap, MAX_CANDIDATE_CAP)
    clients = list(dict.fromkeys(permitted_clients))
    if not clients:
        return ()
    rendered = vector_text(query_vector)
    if index_served:
        cursor.execute(
            NEAREST_STATEMENT,
            (rendered, clients, ceiling, rendered, ceiling, rendered, bound),
        )
    else:
        cursor.execute(
            NEAREST_SCAN_STATEMENT,
            (rendered, clients, candidates, ceiling, rendered, ceiling, rendered, bound),
        )
    return tuple(_neighbour_of(row) for row in cursor.fetchall())


def nearest(
    store: MemoryStore,
    query_vector: Sequence[float],
    *,
    permitted_clients: Iterable[UUID],
    limit: int = DEFAULT_NEIGHBOUR_LIMIT,
    max_cosine: float | None = None,
    candidate_cap: int = DEFAULT_CANDIDATE_CAP,
) -> tuple[Neighbour, ...]:
    """Run the neighbour query on a leased connection, framing no transaction.

    Which form is sent is decided by the capability record the store already
    holds, so the choice is driven by a probe result and costs no round trip on
    the agent's critical path.
    """
    served = index_served(store)

    def body(cursor: Cursor) -> tuple[Neighbour, ...]:
        return select_nearest(
            cursor,
            query_vector,
            permitted_clients=permitted_clients,
            limit=limit,
            max_cosine=max_cosine,
            index_served=served,
            candidate_cap=candidate_cap,
        )

    return store.read(body)


def select_recall_page(
    cursor: Cursor,
    query_vector: Sequence[float],
    *,
    permitted_clients: Iterable[UUID],
    limit: int = DEFAULT_NEIGHBOUR_LIMIT,
    recall_floor: float,
    candidate_pool: int = DEFAULT_CANDIDATE_CAP,
    excerpt_characters: int = DEFAULT_EXCERPT_CHARACTERS,
    index_served: bool = True,
) -> tuple[tuple[RecallRow, ...], int]:
    """The recall page and the count of procedures the floor excluded, on a cursor.

    One statement answers all of it: the candidate pool the index ranks, the
    tenancy admission over unsuperseded Attribution_Versions, the join to the
    originating Session, the floor, the total ordering, and the truncation to the
    caller's bound.

    A caller permitted no Client is answered with nothing and no round trip, for
    the reason the neighbour query gives: neither tenancy term admits a row for an
    empty array, so the statement would cost a scan to prove what the empty set
    already says.

    Args:
        cursor: The cursor the read runs on.
        query_vector: The vector to rank against, unit length and of the fixed
            width, held to that because the projected distance is a cosine
            distance only when both sides are unit vectors.
        permitted_clients: The Clients the caller may see content for.
        limit: How many results the page holds at most.
        recall_floor: The Procedure_Confidence a Learned_Procedure must reach to
            be admitted. It is applied inside the statement, so a procedure below
            it consumes none of the page's positions.
        candidate_pool: How many Embeddings the ranking stage considers.
        excerpt_characters: How much text each result carries.
        index_served: Whether the tier serves the ordering from the distributed
            vector index. False sends the exact-scan form.

    Returns:
        The page in the ordering the statement produced, and how many
        Learned_Procedures the floor excluded from the admitted set.
    """
    require_unit_vector(query_vector)
    bound = _bounded(limit, MAX_NEIGHBOUR_LIMIT)
    pool = _bounded(candidate_pool, MAX_CANDIDATE_CAP)
    excerpt = _bounded(excerpt_characters, MAX_EXCERPT_CHARACTERS)
    floor = require_unit_interval(recall_floor, "a recall floor")
    clients = list(dict.fromkeys(permitted_clients))
    if not clients:
        return (), 0
    rendered = vector_text(query_vector)
    if index_served:
        cursor.execute(
            RECALL_STATEMENT,
            (rendered, rendered, pool, clients, excerpt, excerpt, floor, bound),
        )
    else:
        cursor.execute(
            RECALL_SCAN_STATEMENT,
            (rendered, clients, pool, rendered, pool, clients, excerpt, excerpt, floor, bound),
        )
    return _recall_page_of(cursor.fetchall())


def recall_page(
    store: MemoryStore,
    query_vector: Sequence[float],
    *,
    permitted_clients: Iterable[UUID],
    limit: int = DEFAULT_NEIGHBOUR_LIMIT,
    recall_floor: float,
    candidate_pool: int = DEFAULT_CANDIDATE_CAP,
    excerpt_characters: int = DEFAULT_EXCERPT_CHARACTERS,
) -> tuple[tuple[RecallRow, ...], int]:
    """Answer the recall page on a leased connection, framing no transaction.

    Which form is sent is decided by the capability record the store already
    holds, so the choice is driven by a probe result and costs no round trip on
    the agent's critical path.
    """
    served = index_served(store)

    def body(cursor: Cursor) -> tuple[tuple[RecallRow, ...], int]:
        return select_recall_page(
            cursor,
            query_vector,
            permitted_clients=permitted_clients,
            limit=limit,
            recall_floor=recall_floor,
            candidate_pool=candidate_pool,
            excerpt_characters=excerpt_characters,
            index_served=served,
        )

    return store.read(body)


def select_principal_scope(cursor: Cursor, session_id: UUID) -> PrincipalScope | None:
    """The Client one Session belongs to, on a caller's cursor.

    Returns:
        The scope, or None when no Session holds that identifier, which a caller
        reads as *this request resolves to no tenancy* rather than as a failure.
    """
    cursor.execute(PRINCIPAL_SCOPE_QUERY, (session_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return PrincipalScope(
        client_id=_as_uuid(_column(row, 0, _PRINCIPAL_ROW_WIDTH)),
        agent_cli=_as_str(row[1]),
        machine_id=_as_str(row[2]),
        retention=_as_interval(row[3]),
    )


def principal_scope(store: MemoryStore, session_id: UUID) -> PrincipalScope | None:
    """Resolve one Session's tenancy on a leased connection, framing no transaction."""

    def body(cursor: Cursor) -> PrincipalScope | None:
        return select_principal_scope(cursor, session_id)

    return store.read(body)


def index_served(store: MemoryStore) -> bool:
    """Whether the neighbour query takes the index-served form on this cluster.

    The fallback is taken only where the capability record reports the index
    probed and absent. A cluster nobody probed is not a cluster known to lack the
    index, and the two forms are the same question either way, so an unprobed
    record leaves the primary path in place rather than degrading on the strength
    of a missing row.

    Taking the fallback is recorded as it is taken: the measurement is what makes
    a tier running on the exact scan visible rather than merely slower.
    """
    if not store.known_capabilities().unavailable(VECTOR_INDEX):
        return True
    metric(VECTOR_INDEX_UNAVAILABLE_METRIC)
    log(
        Severity.WARNING,
        COMPONENT,
        "the capability record reports no distributed vector index, so the neighbour "
        "query is answered by a bounded exact scan",
    )
    return False


# ---------------------------------------------------------------------------
# Parameters and row decoding
# ---------------------------------------------------------------------------


def _bounded(limit: int, ceiling: int) -> int:
    """The row bound to send, refusing one that is not a usable bound."""
    if limit < 1:
        raise ValueError("a read bound must admit at least one row")
    if limit > ceiling:
        raise ValueError(f"a read bound may not exceed {ceiling} rows")
    return limit


def _bounded_ceiling(max_cosine: float) -> float:
    """The cosine ceiling to bind, refusing one outside the distance's range."""
    if not math.isfinite(max_cosine):
        raise ValueError("a cosine ceiling must be a finite number")
    if not COSINE_FLOOR <= max_cosine <= COSINE_CEILING:
        raise ValueError(
            f"a cosine ceiling lies between {COSINE_FLOOR} and {COSINE_CEILING} inclusive"
        )
    return max_cosine


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _pending_of(row: Sequence[object]) -> PendingArtifact:
    """Build one pending Artifact from a swept row."""
    return PendingArtifact(
        artifact_id=_as_uuid(_column(row, 0, _PENDING_ROW_WIDTH)),
        artifact_kind=ArtifactKind(_as_str(row[1])),
        client_id=_as_uuid(row[2]),
        created_at=_as_instant(row[3]),
    )


def _neighbour_of(row: Sequence[object]) -> Neighbour:
    """Build one neighbour from a returned row."""
    return Neighbour(
        artifact_id=_as_uuid(_column(row, 0, _NEIGHBOUR_ROW_WIDTH)),
        artifact_kind=ArtifactKind(_as_str(row[1])),
        client_id=_as_uuid(row[2]),
        cosine_distance=_as_float(row[3]),
    )


def _recall_page_of(rows: Sequence[Sequence[object]]) -> tuple[tuple[RecallRow, ...], int]:
    """Split the returned rows into the page and the exclusion tally.

    The tally rides on every row of one page, so it is read from whichever row is
    present, and the one flagged row the statement carries for that purpose is
    dropped here rather than reaching a caller. A page holding no row at all means
    the tenant had nothing admitted and nothing excluded.
    """
    page: list[RecallRow] = []
    excluded = 0
    for row in rows:
        below = _as_bool(_column(row, 11, _RECALL_ROW_WIDTH))
        excluded = _as_count(row[12])
        if below:
            continue
        page.append(_recall_row_of(row, excluded))
    return tuple(page), excluded


def _recall_row_of(row: Sequence[object], excluded: int) -> RecallRow:
    """Build one recall result from a returned row."""
    return RecallRow(
        artifact_id=_as_uuid(_column(row, 0, _RECALL_ROW_WIDTH)),
        artifact_kind=ArtifactKind(_as_str(row[1])),
        client_id=_as_uuid(row[2]),
        cosine_distance=_as_float(row[3]),
        session_id=_as_uuid(row[4]),
        machine_id=_as_str(row[5]),
        occurred_at=_as_instant(row[6]),
        outcome=_as_str(row[7]),
        content_kind=_as_str(row[8]),
        excerpt=_as_str(row[9]),
        procedure_confidence=None if row[10] is None else _as_float(row[10]),
        floor_excluded=excluded,
    )


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a column whose type is not the one the schema declares.

    The type is named and the value is not, because a column of this schema may
    hold memory content or a vector derived from it, and a message naming the
    fault belongs in a log record while the content does not.
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


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        raise _unexpected(value, "a distance")
    if isinstance(value, float | int):
        return float(value)
    raise _unexpected(value, "a distance")


def _as_instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return require_aware(value, "a selected timestamp")
    raise _unexpected(value, "a timestamp")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise _unexpected(value, "a flag")


def _as_count(value: object) -> int:
    """One aggregate count, which a cluster may answer as an exact decimal."""
    if isinstance(value, bool):
        raise _unexpected(value, "a count")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    raise _unexpected(value, "a count")


def _as_interval(value: object) -> timedelta:
    if isinstance(value, timedelta):
        return value
    raise _unexpected(value, "an interval")
