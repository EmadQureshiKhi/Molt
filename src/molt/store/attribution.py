"""Attribution as a bitemporal version history: two ordered writes and two reads.

A Client_Binding is not a row that gets edited. It is an Attribution_Version: an
immutable statement that a named Artifact holds a named Client's data, carrying a
validity start, a validity end that is null while the version is current, and a
superseding reference that is null while the version is current. The question an
auditor asks — when did you first attribute this Artifact to my Client, and what
has changed since — is unanswerable against a row that gets overwritten and
answerable by construction against a history.

Six shapes here are load-bearing.

**A supersession is two ordered statements inside one transaction, never one.**
The cluster refuses a single statement that mutates one table twice unless both
mutations are inserts, and closing a version is an update while writing its
successor is an insert, so no arrangement of common table expressions makes one
statement out of them. The order is fixed: the closing statement runs first and
names the successor's identifier, which the caller generated before either
statement ran, and the insert of the successor follows. Reversing the two would
leave two rows with a null superseding reference for one pair, which the partial
unique index refuses, so the wrong order fails at the database rather than
producing an ambiguous history.

**Integrity comes from the transaction rather than from a constraint.** The
closing statement writes a superseding reference to a row the following statement
has not yet inserted. The cluster checks each foreign key per statement and
implements no deferred checking, so a self-referencing constraint on that column
could not survive the ordering; the reference therefore carries none, and both
statements commit together or neither does, so no committed state exists in which
it dangles. This is the same reasoning already applied to the Disposition's
Artifact identifier and to a checkpoint's Session identifier.

**The current-version uniqueness is partial, so a history accumulates.** The
index constrains only the rows whose superseding reference is null, so many
closed versions accumulate for one Artifact and Client pair while exactly one
version stays current among them. Both facts the governance claim needs, an
accumulating history and a single unambiguous current claim, are therefore
enforced by the database rather than followed by the writing code.

**The greater-confidence rule is evaluated by the cluster against the closed
version's own confidence.** The closing statement returns the confidence it
closed, and the successor's insert writes the greater of that value and the
submitted one, so a repeated detection never lowers a claim and exactly one
current version per pair holds the maximum confidence submitted. A submission
carrying the same method and no greater confidence supersedes nothing at all and
leaves the current version untouched, which is what keeps a repeated write from
growing the history for no reason.

**No supersession is silent.** One Ledger Event naming the Artifact, the Client,
the closed version, and its successor is appended on the same cursor inside the
same transaction, so the attribution history is part of the episodic record and
inherits the hash chain.

**A removal is a closure rather than a delete.** Withdrawing a binding closes the
current version and writes a terminal marker version whose validity interval is
empty and whose superseding reference names the version whose removal it records.
Because the interval is half-open, an empty interval contains no instant, so the
marker is returned by neither read form and the pair holds no current version
afterwards — the binding is gone from every operational read while the history
still says that it was removed rather than that it never existed.

The two read forms are the current-attribution query, which every operational
read uses, and the as-of-attribution query over the half-open validity interval:
inclusive at the start, exclusive at the end, so a supersession instant belongs to
exactly one version and no instant returns two versions for one Client. The
earliest-version query the Erasure_Certificate reads is here as well, and it is
read before any disposition runs, because a hard delete removes the rows it reads.

Every statement is a whole module-level literal, every caller-supplied value is a
bound parameter, and no identifier is interpolated anywhere. The only mutation of a
stored version any statement here
performs is the closure, which writes the validity end and the superseding
reference and nothing else, matching the columns the database-side guard permits a
non-administrative role to write; a statement that would change an immutable
column is refused by that guard and reported as a restatement.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from molt.errors import AttributionImmutableError, StoreError
from molt.models.artifact import ArtifactKind, require_unit_interval
from molt.models.binding import BindingMethod
from molt.models.event import Event, EventCategory, JsonObject, require_aware
from molt.store import Cursor, MemoryStore
from molt.store.chain import LedgerAppend, append_in_transaction
from molt.telemetry import metric

__all__ = [
    "ATTRIBUTION_AS_OF_QUERY",
    "CLOSE_CURRENT_VERSION_STATEMENT",
    "COMPONENT",
    "CURRENT_ATTRIBUTION_QUERY",
    "CURRENT_PAIR_QUERY",
    "CURRENT_UNIQUE_INDEX",
    "CURRENT_VERSION_PREDICATE",
    "FIRST_ATTRIBUTION_QUERY",
    "IMMUTABILITY_GUARD_MESSAGE",
    "INSERT_ERASURE_MARKER_STATEMENT",
    "INSERT_SUCCESSOR_STATEMENT",
    "INSERT_VERSION_STATEMENT",
    "PRIMARY_KEY_CONSTRAINTS",
    "RAISED_EXCEPTION_STATE",
    "SUPERSESSION_METRIC",
    "SUPERSESSION_REASON",
    "UNIQUE_VIOLATION_STATE",
    "WITHDRAWAL_REASON",
    "AttributionOutcome",
    "AttributionSubmission",
    "AttributionWrite",
    "ClosedVersion",
    "CurrentVersion",
    "FirstAttribution",
    "PairVersion",
    "SupersessionContext",
    "VersionAsOf",
    "attribution_as_of",
    "close_current_version",
    "current_attribution",
    "current_pair_version",
    "first_attributions",
    "record_attribution",
    "remove_attribution",
    "select_attribution_as_of",
    "select_current_attribution",
    "select_first_attributions",
    "withdraw_attribution",
    "write_attribution",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The measurement emitted once per supersession, so a history that is churning is
# visible rather than only discoverable by reading rows.
SUPERSESSION_METRIC: Final[str] = "attribution.supersessions"

# The canonical term for *this version is the current claim for its pair*, named
# once so that the partial unique index, both write paths, and every tenancy
# filter in the layer are held to mean exactly the same thing by it.
#
# Each statement still carries the predicate as part of its own text rather than
# being assembled around this constant, for two reasons that pull the same way: a
# statement composed around a name is no longer a whole literal, and building
# statement text by concatenating a literal with a name is the shape the security
# lint rule refuses, because it is indistinguishable from building a statement
# around a value. Agreement is therefore asserted rather than shared: the unit
# suite checks that every current-form statement in the layer, this module's three
# and the two tenancy terms of the neighbour query, carries this exact predicate.
CURRENT_VERSION_PREDICATE: Final[str] = "superseded_by IS NULL"

# The states the cluster reports for the two refusals this module names. They are
# read off the failure rather than inferred from a type, because the driver is
# imported lazily and its exception classes are not nameable here.
UNIQUE_VIOLATION_STATE: Final[str] = "23505"
RAISED_EXCEPTION_STATE: Final[str] = "P0001"

# The attribute names a driver may carry the state under, matching the pair the
# transaction wrapper reads.
_STATE_ATTRIBUTES: Final[tuple[str, ...]] = ("sqlstate", "pgcode")

# The uniqueness over the primary key, under both names a cluster reports it by. A
# violation of it means an identifier a stored version already holds was written
# again, which is a restatement of that version rather than a new one.
PRIMARY_KEY_CONSTRAINTS: Final[frozenset[str]] = frozenset({"primary", "client_binding_pkey"})

# The uniqueness admitting one current version per Artifact and Client pair.
CURRENT_UNIQUE_INDEX: Final[str] = "binding_current_unique"

# What the database-side guard says when a statement changes an immutable column.
# The guard is the enforcement; matching its own words is how its refusal is
# reported as immutability rather than as an unnamed database fault.
IMMUTABILITY_GUARD_MESSAGE: Final[str] = "a stored attribution version is immutable"

# How many columns each row shape carries, checked before a row is read so a
# statement and its decoder cannot drift apart silently.
_PAIR_ROW_WIDTH: Final[int] = 3
_CLOSED_ROW_WIDTH: Final[int] = 6
_WRITTEN_ROW_WIDTH: Final[int] = 3
_CURRENT_ROW_WIDTH: Final[int] = 5
_AS_OF_ROW_WIDTH: Final[int] = 6
_FIRST_ROW_WIDTH: Final[int] = 3

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The first write for a pair. The validity start is the cluster's own transaction
# timestamp rather than a caller's reading, so the interval a history is ordered
# by comes from one clock; the detection instant is the caller's, because when the
# detector concluded something is an observation rather than a storage fact.
#
# Nothing here resolves a conflict. The partial unique index is what admits this
# row, and a pair that already holds a current version is refused by it rather
# than quietly updated, because updating is the thing this whole design replaces.
INSERT_VERSION_STATEMENT: Final[str] = (
    "INSERT INTO client_binding ("
    "id, artifact_id, artifact_kind, client_id, method, confidence, detected_at, valid_from) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
    "RETURNING id, confidence, valid_from"
)

# Statement one of a supersession: close the current version of one pair, naming
# the successor's generated identifier. The assignment names the validity end and
# the superseding reference and no other column, which is exactly the pair the
# guard leaves writable, so this statement is the only mutation of a stored
# version the module performs.
#
# The predicate restricts to the current version, so a closed version cannot be
# closed again and a history cannot be rewritten by closing one twice. No matching
# row means the pair holds no current version, which the caller reads as *this is
# a first write* rather than as a failure.
#
# What comes back is what the successor's insert and the Ledger Event both need:
# the closed identifier, the kind the pair was recorded under, the method and the
# confidence the closed version carried, and the interval it now holds.
CLOSE_CURRENT_VERSION_STATEMENT: Final[str] = (
    "UPDATE client_binding SET valid_to = now(), superseded_by = %s "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NULL "
    "RETURNING id, artifact_kind, method, confidence, valid_from, valid_to"
)

# Statement two of a supersession: insert the successor, carrying the greater of
# the submitted confidence and the confidence the closing statement returned. The
# comparison is the cluster's, evaluated over two bound values, so the rule is
# part of the write rather than an arithmetic a caller could skip.
INSERT_SUCCESSOR_STATEMENT: Final[str] = (
    "INSERT INTO client_binding ("
    "id, artifact_id, artifact_kind, client_id, method, confidence, detected_at, valid_from) "
    "VALUES (%s, %s, %s, %s, %s, greatest(%s::FLOAT8, %s::FLOAT8), %s, now()) "
    "RETURNING id, confidence, valid_from"
)

# The terminal marker a removal writes. Its validity interval is empty, both ends
# being the same transaction timestamp, so the half-open containment predicate
# admits it at no instant and neither read form returns it. Its superseding
# reference names the version whose removal it records, because closure is total
# in this schema and no later version exists; that reference and the empty
# interval together are what distinguish a withdrawn binding from a superseded
# one, since an ordinary supersession always leaves a current version behind.
#
# The method and the confidence are the closed version's own, so the last row of
# the history states what was removed rather than restating it as something else.
INSERT_ERASURE_MARKER_STATEMENT: Final[str] = (
    "INSERT INTO client_binding ("
    "id, artifact_id, artifact_kind, client_id, method, confidence, detected_at, "
    "valid_from, valid_to, superseded_by) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s) "
    "RETURNING id, confidence, valid_from"
)

# The decision read: the current version of one pair, which is what says whether a
# submission is a first write, a supersession, or nothing at all. It reads inside
# the caller's transaction, so the state it reports is the state the write acts
# on, and under SERIALIZABLE a concurrent supersession of the same pair conflicts
# on it rather than racing past it.
CURRENT_PAIR_QUERY: Final[str] = (
    "SELECT id, method, confidence FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NULL"
)

# The current-attribution query, which is the form every operational read of
# bindings takes: the sweep, the disposition join, the recall filter, the
# nearest-neighbour filter, and the tool-server filter alike. Only versions
# carrying no superseding reference are returned, so a Client whose claim was
# closed no longer reaches the Artifact.
CURRENT_ATTRIBUTION_QUERY: Final[str] = (
    "SELECT id, client_id, method, confidence, valid_from FROM client_binding "
    "WHERE artifact_id = %s AND superseded_by IS NULL ORDER BY client_id"
)

# The as-of-attribution query, which is what an auditor reads. The interval is
# half-open, so the instant a supersession happened belongs to the successor
# alone and no instant returns two versions for one Client. The projection and
# the predicate are both served by the covering index over one Artifact's
# versions, which is what holds the bound for an Artifact carrying a long history.
ATTRIBUTION_AS_OF_QUERY: Final[str] = (
    "SELECT id, client_id, method, confidence, valid_from, valid_to FROM client_binding "
    "WHERE artifact_id = %s AND valid_from <= %s AND (valid_to IS NULL OR valid_to > %s) "
    "ORDER BY client_id"
)

# What the Erasure_Certificate records per touched Artifact: when the Client was
# first attributed to it and how that first attribution was concluded. The
# earliest version is the honest answer to *when did you first hold this*, and its
# method says *how you concluded it*, since a marker detection at the earliest
# instant is a materially different admission from an inherited one.
#
# This is read before any disposition runs, because a hard delete removes the rows
# it reads from. The ordering is stated so that two runs over one Artifact set
# report in one order rather than in whichever the scan produced.
FIRST_ATTRIBUTION_QUERY: Final[str] = (
    "SELECT artifact_id, min(valid_from) AS first_attributed_at, "
    "(array_agg(method ORDER BY valid_from))[1] AS first_method "
    "FROM client_binding WHERE client_id = %s AND artifact_id = ANY (%s::UUID[]) "
    "GROUP BY artifact_id ORDER BY artifact_id"
)

# The labels the transactions of this module appear under in a log record and in
# the note an exhausted retry attaches.
_WRITE_LABEL: Final[str] = "attribution_write"
_WITHDRAW_LABEL: Final[str] = "attribution_withdraw"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class AttributionOutcome(StrEnum):
    """What a write did to the history of one Artifact and Client pair."""

    INSERTED = "inserted"
    UNCHANGED = "unchanged"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class AttributionSubmission:
    """One detection result, as the Binding_Detector submits it.

    A submission carries no identifier, no validity interval, and no superseding
    reference: those describe a stored version rather than a detection, and the
    write path derives each of them. What it does carry is immutable once stored,
    which is why a differing submission produces a further version instead of a
    change to this one.
    """

    artifact_id: UUID
    artifact_kind: ArtifactKind
    client_id: UUID
    method: BindingMethod
    confidence: float
    detected_at: datetime

    def __post_init__(self) -> None:
        ArtifactKind(self.artifact_kind)
        BindingMethod(self.method)
        require_unit_interval(self.confidence, "an attribution confidence value")
        require_aware(self.detected_at, "an attribution detection timestamp")


@dataclass(frozen=True, slots=True)
class SupersessionContext:
    """The Session context the supersession Event is recorded within.

    The payload of that Event is built here rather than taken from a caller,
    because what it must name is a requirement rather than a preference. What a
    caller supplies is the context no store module can know: which Session
    observed the change, which command and machine were acting, and how long the
    row is retained for.
    """

    session_id: UUID
    agent_cli: str
    machine_id: str
    expires_at: datetime
    parent_event_id: UUID | None = None

    def __post_init__(self) -> None:
        require_aware(self.expires_at, "a supersession Event expiry")


@dataclass(frozen=True, slots=True)
class PairVersion:
    """The current version of one pair, as the decision read returns it."""

    id: UUID
    method: BindingMethod
    confidence: float


@dataclass(frozen=True, slots=True)
class ClosedVersion:
    """The version a closing statement closed, and the interval it now holds."""

    id: UUID
    artifact_kind: ArtifactKind
    method: BindingMethod
    confidence: float
    valid_from: datetime
    valid_to: datetime


@dataclass(frozen=True, slots=True)
class AttributionWrite:
    """What one attribution write committed.

    Attributes:
        version_id: The version the write produced: the first version, the
            successor, or the terminal marker of a withdrawal. On an unchanged
            write it is the current version that was left alone.
        confidence: The confidence the current version now carries. On a
            withdrawal it is the confidence of the version that was closed.
        outcome: Which of the four things the write did.
        superseded_id: The version that was closed, or None when none was.
        event_id: The Ledger Event the supersession appended, or None when the
            write superseded nothing and so recorded nothing.
    """

    version_id: UUID
    confidence: float
    outcome: AttributionOutcome
    superseded_id: UUID | None = None
    event_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CurrentVersion:
    """One current version of an Artifact, as the current-attribution query reads it."""

    id: UUID
    client_id: UUID
    method: BindingMethod
    confidence: float
    valid_from: datetime


@dataclass(frozen=True, slots=True)
class VersionAsOf:
    """One version whose validity interval contains an instant.

    The validity end is carried because an auditor reading an instant in the past
    is being told which interval the answer came from, and a null end is the
    reading *and it still holds*.
    """

    id: UUID
    client_id: UUID
    method: BindingMethod
    confidence: float
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True, slots=True)
class FirstAttribution:
    """When a Client was first attributed to an Artifact, and how it was concluded."""

    artifact_id: UUID
    first_attributed_at: datetime
    first_method: BindingMethod


# What the supersession Event says the supersession was for. Both values are
# supersessions of a version; the distinction is whether a detector changed its
# mind or an erasure withdrew the claim, which is worth recording because only one
# of the two leaves the pair with a current version afterwards.
SUPERSESSION_REASON: Final[str] = "detection_change"
WITHDRAWAL_REASON: Final[str] = "erasure"


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------


def record_attribution(
    cursor: Cursor,
    submission: AttributionSubmission,
    *,
    context: SupersessionContext,
    version_id: UUID | None = None,
) -> AttributionWrite:
    """Record one detection result on a caller's cursor, superseding when it differs.

    Bindings are written in the transaction that writes the Artifact they describe,
    so this takes the caller's cursor rather than framing a transaction of its own.
    What it does depends on what the pair already holds, and the decision read runs
    on the same cursor so the state it reports is the state the write acts on.

    A pair holding no current version takes the plain insert path. A pair holding
    one whose method matches and whose confidence is at least the submitted value
    is left exactly as it is, because a repeated detection that says nothing new
    should not grow a history. Anything else is a supersession: the current version
    is closed naming the successor's identifier, the successor is inserted carrying
    the greater of the two confidences, and one Ledger Event recording the change
    is appended on this same cursor.

    Args:
        cursor: The cursor the caller's transaction is running on.
        submission: The detection result to record.
        context: The Session context the supersession Event is recorded within.
        version_id: The identifier the new version will carry. A caller may
            generate it, which is what lets the closing statement name a row that
            does not exist yet; one is generated here when none is given.

    Returns:
        What the write did, the version that is now current, and the Ledger Event
        a supersession appended.

    Raises:
        AttributionImmutableError: The write would have restated a stored version
            rather than superseded it, either because the identifier presented for
            the new version is one a stored version already holds, or because the
            database-side guard refused a change to an immutable column.
        StoreError: A statement produced no row where one was required.
    """
    prior = current_pair_version(cursor, submission.artifact_id, submission.client_id)
    if prior is None:
        return _insert_first_version(cursor, submission, version_id=_chosen(version_id))
    if _is_restatement(prior, submission):
        return AttributionWrite(
            version_id=prior.id,
            confidence=prior.confidence,
            outcome=AttributionOutcome.UNCHANGED,
        )
    return _supersede(
        cursor,
        submission,
        context=context,
        successor_id=_chosen(version_id, prior=prior),
    )


def withdraw_attribution(
    cursor: Cursor,
    artifact_id: UUID,
    client_id: UUID,
    *,
    context: SupersessionContext,
    marker_id: UUID | None = None,
) -> AttributionWrite | None:
    """Remove a Client's claim on an Artifact by closing it rather than deleting it.

    This is the form the surgical redaction of an Artifact uses. The current
    version is closed and a terminal marker version is written whose validity
    interval is empty, so the pair holds no current version afterwards and every
    operational read stops returning the Client, while the history still records
    that the claim was withdrawn. One Ledger Event naming both versions is appended
    on the same cursor, as for any other supersession.

    A pair that holds no current version is left alone and reported as such, which
    is what makes a repeated erasure of the same Artifact idempotent rather than a
    failure.

    Args:
        cursor: The cursor the caller's transaction is running on.
        artifact_id: The Artifact whose binding is withdrawn.
        client_id: The Client whose claim is withdrawn.
        context: The Session context the supersession Event is recorded within.
        marker_id: The identifier the terminal marker will carry, generated by the
            caller when it wants to hold it; one is generated here when none is
            given.

    Returns:
        What the withdrawal committed, or None when the pair held no current
        version and nothing was written.
    """
    marker = marker_id if marker_id is not None else uuid4()
    closed = close_current_version(cursor, artifact_id, client_id, successor_id=marker)
    if closed is None:
        return None
    written = _send(
        cursor,
        INSERT_ERASURE_MARKER_STATEMENT,
        (
            marker,
            artifact_id,
            closed.artifact_kind.value,
            client_id,
            closed.method.value,
            closed.confidence,
            closed.valid_to,
            closed.id,
        ),
        required="the terminal marker of a withdrawn attribution",
    )
    event_id = _append_supersession_event(
        cursor,
        artifact_id=artifact_id,
        client_id=client_id,
        superseded_id=closed.id,
        superseding_id=marker,
        occurred_at=closed.valid_to,
        reason=WITHDRAWAL_REASON,
        context=context,
    )
    metric(SUPERSESSION_METRIC)
    return AttributionWrite(
        version_id=written.id,
        confidence=written.confidence,
        outcome=AttributionOutcome.WITHDRAWN,
        superseded_id=closed.id,
        event_id=event_id,
    )


def close_current_version(
    cursor: Cursor,
    artifact_id: UUID,
    client_id: UUID,
    *,
    successor_id: UUID,
) -> ClosedVersion | None:
    """Send the first statement of a supersession and report what it closed.

    The successor's identifier is written into a row whose successor does not exist
    yet, which is the whole reason the column carries no reference. No matching row
    means the pair holds no current version, and that is a reading rather than a
    failure: the caller either takes the first-write path or reports that there was
    nothing to withdraw.
    """
    cursor.execute(CLOSE_CURRENT_VERSION_STATEMENT, (successor_id, artifact_id, client_id))
    row = cursor.fetchone()
    if row is None:
        return None
    return ClosedVersion(
        id=_as_uuid(_column(row, 0, _CLOSED_ROW_WIDTH)),
        artifact_kind=ArtifactKind(_as_text(row[1])),
        method=BindingMethod(_as_text(row[2])),
        confidence=_as_float(row[3]),
        valid_from=_as_moment(row[4]),
        valid_to=_as_moment(row[5]),
    )


def current_pair_version(cursor: Cursor, artifact_id: UUID, client_id: UUID) -> PairVersion | None:
    """The current version of one Artifact and Client pair, or None when there is none.

    Read on the caller's own cursor, so a decision taken from it is taken inside
    the transaction that acts on it: a concurrent supersession of the same pair
    writes into this read set, and SERIALIZABLE aborts one of the two rather than
    letting both write a current version.
    """
    cursor.execute(CURRENT_PAIR_QUERY, (artifact_id, client_id))
    row = cursor.fetchone()
    if row is None:
        return None
    return PairVersion(
        id=_as_uuid(_column(row, 0, _PAIR_ROW_WIDTH)),
        method=BindingMethod(_as_text(row[1])),
        confidence=_as_float(row[2]),
    )


def write_attribution(
    store: MemoryStore,
    submission: AttributionSubmission,
    *,
    context: SupersessionContext,
    version_id: UUID | None = None,
) -> AttributionWrite:
    """Record one detection result in one SERIALIZABLE transaction of its own.

    A caller writing the Artifact uses the cursor form instead, because the
    bindings of an Artifact belong in that Artifact's transaction. This form is for
    a caller with no transaction to compose into, and it inherits the bounded
    jittered retry: a conflict re-runs the decision read against the state that
    won, so the second attempt supersedes the version that committed rather than
    the one it first saw.
    """

    def body(opened: Cursor) -> AttributionWrite:
        return record_attribution(opened, submission, context=context, version_id=version_id)

    return store.in_serializable(body, label=_WRITE_LABEL)


def remove_attribution(
    store: MemoryStore,
    artifact_id: UUID,
    client_id: UUID,
    *,
    context: SupersessionContext,
    marker_id: UUID | None = None,
) -> AttributionWrite | None:
    """Withdraw one Client's claim on one Artifact in a transaction of its own."""

    def body(opened: Cursor) -> AttributionWrite | None:
        return withdraw_attribution(
            opened,
            artifact_id,
            client_id,
            context=context,
            marker_id=marker_id,
        )

    return store.in_serializable(body, label=_WITHDRAW_LABEL)


# ---------------------------------------------------------------------------
# The two write paths, and what tells them apart
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _WrittenVersion:
    """What an insert of a version returned: its identifier and what it holds."""

    id: UUID
    confidence: float
    valid_from: datetime


def _insert_first_version(
    cursor: Cursor,
    submission: AttributionSubmission,
    *,
    version_id: UUID,
) -> AttributionWrite:
    """Write the first version of a pair, admitted by the partial unique index.

    No Ledger Event is appended here. A first attribution is not a supersession,
    and the Artifact's own capture Event is what records that the Artifact and its
    bindings were written; an Event per first binding would say the same thing
    twice.
    """
    written = _send(
        cursor,
        INSERT_VERSION_STATEMENT,
        (
            version_id,
            submission.artifact_id,
            ArtifactKind(submission.artifact_kind).value,
            submission.client_id,
            BindingMethod(submission.method).value,
            submission.confidence,
            submission.detected_at,
        ),
        required="the first attribution version of a pair",
    )
    return AttributionWrite(
        version_id=written.id,
        confidence=written.confidence,
        outcome=AttributionOutcome.INSERTED,
    )


def _supersede(
    cursor: Cursor,
    submission: AttributionSubmission,
    *,
    context: SupersessionContext,
    successor_id: UUID,
) -> AttributionWrite:
    """Close the current version, insert the successor, and record the change.

    The order is the point. The closing statement runs first and names an
    identifier no row holds yet; the successor's insert follows and is what makes
    that reference real, inside the one transaction both statements commit in.
    Reversing them would leave two current versions for the pair, which the partial
    unique index refuses.
    """
    closed = close_current_version(
        cursor,
        submission.artifact_id,
        submission.client_id,
        successor_id=successor_id,
    )
    if closed is None:
        raise StoreError(
            "the current attribution version of the pair was closed by another "
            "transaction between the decision read and the supersession, so nothing was written"
        )
    written = _send(
        cursor,
        INSERT_SUCCESSOR_STATEMENT,
        (
            successor_id,
            submission.artifact_id,
            ArtifactKind(submission.artifact_kind).value,
            submission.client_id,
            BindingMethod(submission.method).value,
            submission.confidence,
            closed.confidence,
            submission.detected_at,
        ),
        required="the successor attribution version",
    )
    event_id = _append_supersession_event(
        cursor,
        artifact_id=submission.artifact_id,
        client_id=submission.client_id,
        superseded_id=closed.id,
        superseding_id=successor_id,
        occurred_at=closed.valid_to,
        reason=SUPERSESSION_REASON,
        context=context,
    )
    metric(SUPERSESSION_METRIC)
    return AttributionWrite(
        version_id=written.id,
        confidence=written.confidence,
        outcome=AttributionOutcome.SUPERSEDED,
        superseded_id=closed.id,
        event_id=event_id,
    )


def _is_restatement(prior: PairVersion, submission: AttributionSubmission) -> bool:
    """Whether a submission says nothing the current version does not already say.

    Same method and no greater confidence is the repeated-write case: the current
    version already holds the maximum confidence submitted for the pair, so
    superseding it would produce a successor identical in every stored field and
    lengthen the history by a row that records no change.
    """
    return prior.method is BindingMethod(submission.method) and (
        submission.confidence <= prior.confidence
    )


def _chosen(version_id: UUID | None, *, prior: PairVersion | None = None) -> UUID:
    """The identifier a new version will carry, refusing one a stored version holds.

    Presenting the identifier of the version being superseded is a restatement of
    that version rather than a supersession of it, and it is refused here rather
    than at the database, because the closing statement would otherwise write a
    superseding reference to the row it just closed.
    """
    if version_id is None:
        return uuid4()
    if prior is not None and version_id == prior.id:
        raise AttributionImmutableError(
            f"the attribution version {version_id} is stored and immutable, so superseding "
            "it writes a further version rather than restating that one"
        )
    return version_id


def _append_supersession_event(
    cursor: Cursor,
    *,
    artifact_id: UUID,
    client_id: UUID,
    superseded_id: UUID,
    superseding_id: UUID,
    occurred_at: datetime,
    reason: str,
    context: SupersessionContext,
) -> UUID:
    """Append the one Event a supersession records, on the transaction's own cursor.

    The append goes through the chain's single-statement form on this cursor, so
    the Event's sequence number and digests are derived inside the same transaction
    the two attribution statements are in, and the Event commits with them or not
    at all. The instant it records is the closing statement's own validity end,
    which is the instant the supersession happened rather than a reading taken
    afterwards.
    """
    payload: JsonObject = {
        "artifact_id": str(artifact_id),
        "client_id": str(client_id),
        "superseded_version_id": str(superseded_id),
        "superseding_version_id": str(superseding_id),
        "reason": reason,
    }
    event = Event(
        id=uuid4(),
        session_id=context.session_id,
        client_id=client_id,
        category=EventCategory.ATTRIBUTION_SUPERSEDED,
        occurred_at=occurred_at,
        agent_cli=context.agent_cli,
        machine_id=context.machine_id,
        parent_event_id=context.parent_event_id,
        payload=payload,
        redacted=False,
        text_body=None,
    )
    appended = append_in_transaction(
        cursor,
        LedgerAppend(event=event, expires_at=context.expires_at),
    )
    return appended.event_id


def _send(
    cursor: Cursor,
    statement: str,
    parameters: tuple[object, ...],
    *,
    required: str,
) -> _WrittenVersion:
    """Send one version insert, translating a refusal this module has a name for."""
    try:
        cursor.execute(statement, parameters)
    except Exception as error:
        translated = _translated(error)
        if translated is None:
            raise
        raise translated from error
    row = cursor.fetchone()
    if row is None:
        raise StoreError(f"{required} was not written, so no version identifier came back")
    return _WrittenVersion(
        id=_as_uuid(_column(row, 0, _WRITTEN_ROW_WIDTH)),
        confidence=_as_float(row[1]),
        valid_from=_as_moment(row[2]),
    )


def _translated(error: BaseException) -> StoreError | None:
    """The failure to raise for a refusal this module names, or None for any other.

    Two refusals are named. The guard refuses a statement that would change an
    immutable column, and its own words are what identify it, since it reports the
    state every raised database exception reports. The primary key refuses an
    identifier a stored version already holds, which is the same restatement
    arriving by another route. A refusal of the partial uniqueness is a different
    thing and says so: two current versions for one pair means a concurrent write
    landed, not that anything was restated.
    """
    state = _state_of(error)
    if state == RAISED_EXCEPTION_STATE and IMMUTABILITY_GUARD_MESSAGE in str(error):
        return AttributionImmutableError(
            "a stored attribution version is immutable and may only be closed, so the "
            "restatement was refused and nothing was written"
        )
    if state == UNIQUE_VIOLATION_STATE:
        constraint = _constraint_of(error)
        if constraint in PRIMARY_KEY_CONSTRAINTS:
            return AttributionImmutableError(
                "the identifier presented for a new attribution version is one a stored "
                "version already holds, so the write would have restated it"
            )
        if constraint == CURRENT_UNIQUE_INDEX:
            return StoreError(
                "the Artifact and Client pair already holds a current attribution version, "
                "so a second current version was refused and nothing was written"
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
# The reads
# ---------------------------------------------------------------------------


def select_current_attribution(cursor: Cursor, artifact_id: UUID) -> tuple[CurrentVersion, ...]:
    """Every current version of one Artifact, ordered by Client.

    This is the form every operational read of bindings takes. A version whose
    claim was closed is absent from it, whether it was closed by a later detection
    or by an erasure, so a Client that no longer holds the Artifact does not reach
    it through any path built on this.
    """
    cursor.execute(CURRENT_ATTRIBUTION_QUERY, (artifact_id,))
    return tuple(_current_of(row) for row in cursor.fetchall())


def select_attribution_as_of(
    cursor: Cursor,
    artifact_id: UUID,
    at: datetime,
) -> tuple[VersionAsOf, ...]:
    """Every version of one Artifact whose validity interval contains an instant.

    The interval is half-open: a version is returned when its validity start is at
    or before the instant and its validity end is absent or strictly after it. So
    the instant a supersession happened belongs to the successor alone, and one
    Client contributes at most one version to any answer.
    """
    moment = require_aware(at, "an as-of attribution instant")
    cursor.execute(ATTRIBUTION_AS_OF_QUERY, (artifact_id, moment, moment))
    return tuple(_as_of_of(row) for row in cursor.fetchall())


def select_first_attributions(
    cursor: Cursor,
    client_id: UUID,
    artifact_ids: Iterable[UUID],
) -> tuple[FirstAttribution, ...]:
    """When each Artifact was first attributed to one Client, and how.

    The certificate reads this before any disposition runs, because a hard delete
    removes the rows it reads from. The Artifacts travel as one bound array, so a
    run touching a thousand of them asks once, and an empty set is answered without
    a round trip rather than by sending an empty array.
    """
    wanted = list(dict.fromkeys(artifact_ids))
    if not wanted:
        return ()
    cursor.execute(FIRST_ATTRIBUTION_QUERY, (client_id, wanted))
    return tuple(_first_of(row) for row in cursor.fetchall())


def current_attribution(store: MemoryStore, artifact_id: UUID) -> tuple[CurrentVersion, ...]:
    """Read one Artifact's current attribution on a leased connection."""

    def body(opened: Cursor) -> tuple[CurrentVersion, ...]:
        return select_current_attribution(opened, artifact_id)

    return store.read(body)


def attribution_as_of(
    store: MemoryStore,
    artifact_id: UUID,
    at: datetime,
) -> tuple[VersionAsOf, ...]:
    """Read one Artifact's attribution as it stood at an instant."""

    def body(opened: Cursor) -> tuple[VersionAsOf, ...]:
        return select_attribution_as_of(opened, artifact_id, at)

    return store.read(body)


def first_attributions(
    store: MemoryStore,
    client_id: UUID,
    artifact_ids: Iterable[UUID],
) -> tuple[FirstAttribution, ...]:
    """Read the earliest attribution of each Artifact to one Client."""

    def body(opened: Cursor) -> tuple[FirstAttribution, ...]:
        return select_first_attributions(opened, client_id, artifact_ids)

    return store.read(body)


# ---------------------------------------------------------------------------
# Row narrowing
# ---------------------------------------------------------------------------


def _current_of(row: Sequence[object]) -> CurrentVersion:
    """Build one current version from a selected row."""
    return CurrentVersion(
        id=_as_uuid(_column(row, 0, _CURRENT_ROW_WIDTH)),
        client_id=_as_uuid(row[1]),
        method=BindingMethod(_as_text(row[2])),
        confidence=_as_float(row[3]),
        valid_from=_as_moment(row[4]),
    )


def _as_of_of(row: Sequence[object]) -> VersionAsOf:
    """Build one as-of version from a selected row."""
    return VersionAsOf(
        id=_as_uuid(_column(row, 0, _AS_OF_ROW_WIDTH)),
        client_id=_as_uuid(row[1]),
        method=BindingMethod(_as_text(row[2])),
        confidence=_as_float(row[3]),
        valid_from=_as_moment(row[4]),
        valid_to=None if row[5] is None else _as_moment(row[5]),
    )


def _first_of(row: Sequence[object]) -> FirstAttribution:
    """Build one earliest-attribution answer from a selected row."""
    return FirstAttribution(
        artifact_id=_as_uuid(_column(row, 0, _FIRST_ROW_WIDTH)),
        first_attributed_at=_as_moment(row[1]),
        first_method=BindingMethod(_as_text(row[2])),
    )


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a value whose type is not the one the schema declares.

    The type is named and the value is not, because a column of this table names a
    tenant and a message naming the fault belongs in a log record while the tenant
    does not.
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


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        raise _unexpected(value, "a confidence value")
    if isinstance(value, (int, float)):
        return float(value)
    raise _unexpected(value, "a confidence value")


def _as_moment(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise _unexpected(value, "an instant")
    return require_aware(value, "a stored attribution timestamp")
