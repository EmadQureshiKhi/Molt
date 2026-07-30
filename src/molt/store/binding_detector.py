"""The Binding_Detector: three kinds of evidence, one claim per Client, one history.

An Artifact reaches the store with three independent reasons to believe a Client's
data is in it. The Session it was produced under belongs to a Client, which is the
most direct claim there is and carries confidence 1.0. Every Client holding a
current claim on a parent Artifact has a claim on content derived from that parent,
inherited at the parent's own confidence. And a Client whose configured content
markers appear in the Artifact's text has a claim the text itself evidences,
carried at 0.9. Requirement 12 asks for all three, Requirement 12.6 asks for them
in the Artifact's own transaction, and Requirement 43.3 asks that a detection
disagreeing with what is stored produce a further version rather than a change to
one.

Four decisions shape this module.

**Every write goes through the attribution module and nothing here writes a
statement against the binding table.** A detection is submitted, and the write
path decides: a pair holding no current version takes the plain insert, a
submission saying nothing new leaves the current version untouched, and anything
else closes the current version naming its successor and inserts the successor
carrying the greater of the two confidences. So the maximum-confidence rule is
evaluated by the cluster against the *unsuperseded* version rather than against
whatever this module last observed, and no detection can overwrite a stored claim
because no statement here can.

**Detections for one Artifact and Client pair are collapsed into one submission
before any of them is written.** The three kinds collide readily: the owning
Client is frequently a marker match as well, and an inherited claim on the same
Client is ordinary. Submitting each separately would be correct about confidence,
since the greater-confidence rule is commutative, and wrong about method, since
the last submission to differ is the one whose method the current version carries
— the stored claim would then record whichever kind happened to be submitted last.
Collapsing takes the greatest confidence and, where two kinds agree on confidence,
the more direct kind by a fixed precedence: scope, then marker, then inherited. The
result does not depend on the order detections were produced in, which is asserted
rather than asserted-by-inspection.

**Inheritance reads the direct parents and never walks the lineage graph.**
Requirement 12.3 names the parents of a Derived_Artifact rather than its
ancestors, and the closure comes out the same: a parent's own current bindings
already include everything that parent inherited when it was stored, so a child
inheriting from its parents inherits transitively by induction. That is also what
bounds the read. One statement over one bound array of parent identifiers costs the
same for a chain a thousand deep as for a chain of one, whereas an unbounded
recursive traversal inside a write transaction would put a cost proportional to the
whole ancestry on the transaction that writes a single Artifact, and would hold a
read set proportional to it under SERIALIZABLE. The inherited confidence for a
Client bound to several parents is the greatest of those parents' current
confidences, aggregated by the cluster in that one statement, so a child never
carries a claim stronger than the parent evidence it came from and Property 15's
monotonicity holds edge by edge.

**Marker matching prefers a missed match to a false one.** The comparison is
case-insensitive, and both edges of a match must sit at a token boundary: the
start or end of the text, a character that is not a letter or a digit, a change of
case between adjacent letters, or a letter belonging to a script that draws no
case distinction. So a marker `acme` is found in `acme-payments`, in `ACME.`, and
in `acmePayments`, and is not found in `acmecorp` or in `acme2`. The governance
error in each direction is real and they are not symmetric. A marker that
over-matches binds an Artifact to a Client with no claim on it, and that binding is
load-bearing in two places at once: the recall tenancy filter would show one
tenant's content to another, and an erasure for the over-matched Client would
select content belonging to somebody else. Nothing downstream would catch either,
because a binding is exactly the evidence those paths trust. A marker that
under-matches leaves residue, which is the failure this system exists to find — but
it is not left unattended: the Residue_Detector's semantic pass is the second
detector for precisely this content, its findings arrive under the `residue`
method, and the supersession path admits a stronger later claim for a pair at any
time without rewriting anything. Precision here is therefore recoverable and
over-matching is not, so the boundary rule is the deliberate choice, and an
operator needing a broader form configures the broader marker.

Marker comparison happens in this process rather than in a predicate. A marker is
tenant-supplied text, and a statement matching it server-side would either need
that text inside a pattern, where its own metacharacters would change what the
pattern means, or a containment test too coarse for the boundary rule above. The
two reads this module does perform bind every value, interpolate nothing, and are
whole module-level literals, as every statement of the layer is.

Nothing here frames a transaction of its own beyond the store's serializable
wrapper. The cursor form is the one a caller writing an Artifact composes, because
Requirement 12.6 puts the bindings in that Artifact's transaction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from molt.errors import StoreError
from molt.models.artifact import ArtifactKind, ArtifactRef, require_unit_interval
from molt.models.binding import BindingMethod
from molt.models.event import require_aware
from molt.store import Cursor, MemoryStore
from molt.store.attribution import (
    AttributionSubmission,
    AttributionWrite,
    SupersessionContext,
    record_attribution,
)

__all__ = [
    "COMPONENT",
    "MARKER_CLIENTS_QUERY",
    "MARKER_CONFIDENCE",
    "METHOD_PRECEDENCE",
    "PARENT_BINDINGS_QUERY",
    "SCOPE_CONFIDENCE",
    "BoundClient",
    "Detection",
    "DetectionRequest",
    "MarkerClient",
    "WrittenBinding",
    "bindings_for",
    "collapse",
    "inherited_detections",
    "marker_clients",
    "marker_detections",
    "marker_in_text",
    "parent_bindings",
    "record_bindings",
    "scope_detection",
    "write_bindings",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The two confidences the requirement fixes. Scope is certain: the Session the
# Artifact was produced under belongs to exactly one Client and the store knows
# which. A marker match is strong evidence about content rather than a fact about
# provenance, so it is held just below certainty and is therefore superseded by a
# scope claim for the same pair rather than competing with one.
SCOPE_CONFIDENCE: Final[float] = 1.0
MARKER_CONFIDENCE: Final[float] = 0.9

# How directly each kind claims the Artifact, most direct first. This orders two
# detections that agree on confidence, and it is the whole reason the stored method
# does not depend on the order detections were produced in. Scope is the Session's
# own tenancy, a marker is evidence in this Artifact's own text, an inherited claim
# is evidence about a different Artifact, and a residue finding is a later
# semantic conclusion about content the markers did not name.
METHOD_PRECEDENCE: Final[tuple[BindingMethod, ...]] = (
    BindingMethod.SCOPE,
    BindingMethod.MARKER,
    BindingMethod.INHERITED,
    BindingMethod.RESIDUE,
)

# How many columns each row shape carries, checked before a row is read so a
# statement and its decoder cannot drift apart silently.
_MARKER_ROW_WIDTH: Final[int] = 2
_BOUND_ROW_WIDTH: Final[int] = 2

# The label the transaction of this module appears under in a log record and in
# the note an exhausted retry attaches.
_WRITE_LABEL: Final[str] = "binding_detection"


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# Every Client that has configured at least one content marker, with its markers.
# The roster of tenants is the smallest table in the schema and this reads it in
# primary-key order, so the cost is one bounded statement per Artifact rather than
# one per Client or one per marker. A Client with no marker is excluded by the
# predicate rather than returned and skipped, because the empty array is the
# ordinary case and carrying it would make the answer mostly nothing.
#
# The markers travel back as data and are compared in this process. Matching them
# server-side would put tenant-supplied text into a pattern, where the text's own
# metacharacters would decide what the pattern matched.
MARKER_CLIENTS_QUERY: Final[str] = (
    "SELECT id, content_markers FROM client WHERE array_length(content_markers, 1) > 0 ORDER BY id"
)

# Every Client holding a current claim on any of the named parents, with the
# greatest confidence any of those claims carries. Three things about it matter.
#
# It is the current-attribution form: only versions carrying no superseding
# reference are read, so a claim a later detection closed, or an erasure withdrew,
# is not inherited by anything written afterwards. It reads on the caller's own
# cursor, inside the Artifact's transaction, so what the child inherits is the
# state the child's own write acts on and a concurrent supersession of a parent's
# claim conflicts on this read rather than racing past it. And the aggregate is the
# cluster's: a Client bound to several parents at several confidences yields one
# row at the greatest of them, so the answer is one row per Client however many
# parents the Artifact has.
PARENT_BINDINGS_QUERY: Final[str] = (
    "SELECT client_id, max(confidence) AS confidence FROM client_binding "
    "WHERE artifact_id = ANY (%s::UUID[]) AND superseded_by IS NULL "
    "GROUP BY client_id ORDER BY client_id"
)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarkerClient:
    """One Client's configured content markers, as the marker read returns them.

    Markers live on the Client row rather than in the configuration surface, which
    Requirement 12.4 settles by making them per-Client: a global configuration key
    would be a second source of truth for the same fact, and the two would
    disagree the first time a tenant's markers changed without a redeployment.

    A marker that is empty or holds nothing but whitespace is carried here as the
    row holds it and refused by the comparison, which is where it matters: such a
    marker occurs in every text, and matching it would bind every Artifact ever
    written to this Client.
    """

    client_id: UUID
    markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BoundClient:
    """One Client's current claim on the parents, at the greatest confidence found."""

    client_id: UUID
    confidence: float

    def __post_init__(self) -> None:
        require_unit_interval(self.confidence, "an inherited attribution confidence value")


@dataclass(frozen=True, slots=True)
class Detection:
    """One conclusion about one Client, before anything is written.

    A detection carries no Artifact and no instant. Those belong to the request
    the detections were produced for and to the submission the write path builds,
    and repeating them per detection would let one Artifact's detections disagree
    about which Artifact they describe.
    """

    client_id: UUID
    method: BindingMethod
    confidence: float

    def __post_init__(self) -> None:
        BindingMethod(self.method)
        require_unit_interval(self.confidence, "a detected attribution confidence value")


@dataclass(frozen=True, slots=True)
class DetectionRequest:
    """One Artifact to detect bindings for, and the three sources of evidence.

    Attributes:
        artifact: The Artifact the bindings will describe.
        text: The Artifact's text, or None for an Artifact carrying none. A text
            that is absent yields no marker detection and no failure: an Artifact
            with no text evidences nothing about markers.
        scope_client_id: The Client owning the Session the Artifact was produced
            under, which is the scope claim.
        parents: The direct parents of a Derived_Artifact. Ancestors are not
            named: a parent's current bindings already carry what that parent
            inherited, so the closure follows by induction.
    """

    artifact: ArtifactRef
    scope_client_id: UUID
    text: str | None = None
    parents: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        ArtifactKind(self.artifact.kind)

    @property
    def parent_ids(self) -> tuple[UUID, ...]:
        """The parent identifiers, repeated ones collapsed and order preserved."""
        return tuple(dict.fromkeys(parent.id for parent in self.parents))


@dataclass(frozen=True, slots=True)
class WrittenBinding:
    """One detection and what the attribution write path did with it."""

    detection: Detection
    write: AttributionWrite


# ---------------------------------------------------------------------------
# The three kinds
# ---------------------------------------------------------------------------


def scope_detection(request: DetectionRequest) -> Detection:
    """The scope claim, which every Artifact carries exactly one of.

    It is unconditional. An Artifact was produced under a Session, that Session
    belongs to a Client, and no evidence is weighed to conclude it, which is why
    it is the one detection carrying certainty.
    """
    return Detection(
        client_id=request.scope_client_id,
        method=BindingMethod.SCOPE,
        confidence=SCOPE_CONFIDENCE,
    )


def parent_bindings(cursor: Cursor, parent_ids: Iterable[UUID]) -> tuple[BoundClient, ...]:
    """Every Client currently claiming any of the named parents, greatest first claim.

    One statement over one bound array, sent on the caller's own cursor. An empty
    set of parents is answered without a round trip rather than by sending an empty
    array, which is the ordinary case for an Artifact that derives from nothing.
    """
    wanted = list(dict.fromkeys(parent_ids))
    if not wanted:
        return ()
    cursor.execute(PARENT_BINDINGS_QUERY, (wanted,))
    return tuple(_bound_of(row) for row in cursor.fetchall())


def inherited_detections(
    cursor: Cursor,
    parent_ids: Iterable[UUID],
) -> tuple[Detection, ...]:
    """One inherited detection per Client bound to any parent, at the parent confidence.

    The confidence is the parent's own rather than a value chosen here, so nothing
    is claimed about derived content more strongly than about the content it came
    from. That is the monotonicity Property 15 rests on: the Client set can only
    grow along an inheritance edge, and the confidence cannot rise across one
    unless another kind of evidence supplies a higher value for the same pair.
    """
    return tuple(
        Detection(
            client_id=bound.client_id,
            method=BindingMethod.INHERITED,
            confidence=bound.confidence,
        )
        for bound in parent_bindings(cursor, parent_ids)
    )


def marker_clients(cursor: Cursor) -> tuple[MarkerClient, ...]:
    """Every Client that has configured content markers, with those markers."""
    cursor.execute(MARKER_CLIENTS_QUERY)
    return tuple(_marker_of(row) for row in cursor.fetchall())


def marker_detections(cursor: Cursor, text: str | None) -> tuple[Detection, ...]:
    """One marker detection per Client whose markers appear in the Artifact's text.

    An Artifact with no text, or with text holding nothing but whitespace, yields
    nothing here and reads no Client row: there is no evidence to weigh, and asking
    would cost a round trip per Artifact for an answer that cannot matter.
    """
    if text is None or not text.strip():
        return ()
    return tuple(
        Detection(
            client_id=candidate.client_id,
            method=BindingMethod.MARKER,
            confidence=MARKER_CONFIDENCE,
        )
        for candidate in marker_clients(cursor)
        if any(marker_in_text(text, marker) for marker in candidate.markers)
    )


def marker_in_text(text: str, marker: str) -> bool:
    """Whether a configured marker occurs in a text at a token boundary.

    Case is ignored and the marker is compared as literal text, never as a
    pattern, so a marker holding a metacharacter matches that character rather
    than whatever the character would have meant. Both edges of an occurrence must
    sit at a boundary, which is what keeps `acme` out of `acmecorp` while leaving
    it in `acme-payments` and in `acmePayments`.
    """
    needle = marker.strip()
    if not needle:
        return False
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    return any(
        _boundary_before(text, found.start()) and _boundary_after(text, found.end())
        for found in pattern.finditer(text)
    )


def _boundary_before(text: str, index: int) -> bool:
    """Whether the left edge of an occurrence begins a token."""
    if index == 0:
        return True
    before = text[index - 1]
    if not _joins(before):
        return True
    return before.islower() and text[index].isupper()


def _boundary_after(text: str, index: int) -> bool:
    """Whether the right edge of an occurrence ends a token."""
    if index == len(text):
        return True
    after = text[index]
    if not _joins(after):
        return True
    return text[index - 1].islower() and after.isupper()


def _joins(character: str) -> bool:
    """Whether a character continues a token rather than delimiting one.

    A letter or a digit continues one, with one exception: a letter from a script
    drawing no case distinction delimits, because such scripts do not separate
    words with the characters this rule would otherwise wait for, and treating
    them as joining would leave a marker beside them permanently undetected.
    """
    if not character.isalnum():
        return False
    return not (character.isalpha() and character.lower() == character.upper())


# ---------------------------------------------------------------------------
# Collapsing, and what makes it order-independent
# ---------------------------------------------------------------------------


def collapse(detections: Iterable[Detection]) -> tuple[Detection, ...]:
    """One detection per Client, the strongest, ordered by Client.

    Two detections for one Client are resolved by confidence, and where the
    confidences agree by how directly the kind claims the Artifact. Both terms are
    total and neither consults arrival order, so the result is the same set
    whatever order the detections were produced in — which is the property that
    keeps the stored method from recording which kind happened to be submitted
    last.

    Nothing is dropped: a Client appearing at all appears in the result, so the
    Client set of the collapsed detections is the union of the Client sets of the
    three kinds.
    """
    strongest: dict[UUID, Detection] = {}
    for detection in detections:
        held = strongest.get(detection.client_id)
        if held is None or _outranks(detection, held):
            strongest[detection.client_id] = detection
    return tuple(sorted(strongest.values(), key=lambda item: str(item.client_id)))


def _outranks(candidate: Detection, held: Detection) -> bool:
    """Whether one detection is the stronger claim of two about one Client."""
    if candidate.confidence != held.confidence:
        return candidate.confidence > held.confidence
    return _precedence(candidate.method) < _precedence(held.method)


def _precedence(method: BindingMethod) -> int:
    """How directly a method claims the Artifact, lower being more direct."""
    return METHOD_PRECEDENCE.index(BindingMethod(method))


def bindings_for(cursor: Cursor, request: DetectionRequest) -> tuple[Detection, ...]:
    """Every binding one Artifact carries, one per Client, read on the caller's cursor.

    The three kinds are produced and then collapsed, so a Client that is the scope
    owner and a marker match and bound to a parent contributes one claim rather
    than three, and the claim it contributes is the strongest of them. Both reads
    run inside the caller's transaction, which is what makes the inherited claims
    the ones the parents hold at the instant the Artifact is written.
    """
    return collapse(
        (
            scope_detection(request),
            *inherited_detections(cursor, request.parent_ids),
            *marker_detections(cursor, request.text),
        )
    )


# ---------------------------------------------------------------------------
# The write path, which is the attribution module's
# ---------------------------------------------------------------------------


def write_bindings(
    cursor: Cursor,
    request: DetectionRequest,
    *,
    context: SupersessionContext,
    detected_at: datetime,
) -> tuple[WrittenBinding, ...]:
    """Detect and record one Artifact's bindings on the caller's own cursor.

    This is the form the Artifact's write composes: Requirement 12.6 puts the
    bindings in the same transaction as the Artifact they describe, so the caller
    frames the transaction and hands its cursor here, and the Artifact and its
    bindings commit together or not at all.

    Each collapsed detection is submitted to the attribution write path, which
    inserts where the pair holds no current version, supersedes where the method or
    the confidence differs, and leaves the current version untouched where the
    submission says nothing new. No statement here writes the binding table, so no
    detection can overwrite a stored claim and the maximum-confidence rule stays
    the cluster's arithmetic over the unsuperseded version.

    Args:
        cursor: The cursor the caller's transaction is running on.
        request: The Artifact and the three sources of evidence about it.
        context: The Session context a supersession Event is recorded within.
        detected_at: When the detection was concluded, which is an observation
            rather than a storage fact and so comes from the caller.

    Returns:
        One record per Client, in Client order, pairing the detection with what the
        write did to that pair's history.
    """
    moment = require_aware(detected_at, "a binding detection timestamp")
    kind = ArtifactKind(request.artifact.kind)
    written: list[WrittenBinding] = []
    for detection in bindings_for(cursor, request):
        submission = AttributionSubmission(
            artifact_id=request.artifact.id,
            artifact_kind=kind,
            client_id=detection.client_id,
            method=detection.method,
            confidence=detection.confidence,
            detected_at=moment,
        )
        written.append(
            WrittenBinding(
                detection=detection,
                write=record_attribution(cursor, submission, context=context),
            )
        )
    return tuple(written)


def record_bindings(
    store: MemoryStore,
    request: DetectionRequest,
    *,
    context: SupersessionContext,
    detected_at: datetime,
) -> tuple[WrittenBinding, ...]:
    """Detect and record one Artifact's bindings in one SERIALIZABLE transaction.

    For a caller with no Artifact write to compose into: a re-detection over
    content already stored, or a backfill. A caller writing the Artifact uses the
    cursor form instead. The bounded jittered retry is inherited from the store's
    wrapper, so a conflict re-runs both reads and every decision taken from them
    against the state that won.
    """

    def body(opened: Cursor) -> tuple[WrittenBinding, ...]:
        return write_bindings(opened, request, context=context, detected_at=detected_at)

    return store.in_serializable(body, label=_WRITE_LABEL)


# ---------------------------------------------------------------------------
# Row narrowing
# ---------------------------------------------------------------------------


def _marker_of(row: Sequence[object]) -> MarkerClient:
    """Build one Client's marker set from a selected row."""
    return MarkerClient(
        client_id=_as_uuid(_column(row, 0, _MARKER_ROW_WIDTH)),
        markers=_as_markers(row[1]),
    )


def _bound_of(row: Sequence[object]) -> BoundClient:
    """Build one inherited claim from a selected row."""
    return BoundClient(
        client_id=_as_uuid(_column(row, 0, _BOUND_ROW_WIDTH)),
        confidence=_as_float(row[1]),
    )


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a value whose type is not the one the schema declares.

    The type is named and the value is not, because a column read here names a
    tenant or holds a tenant's own marker text, and a message naming the fault
    belongs in a log record while neither of those does.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        raise _unexpected(value, "a confidence value")
    if isinstance(value, (int, float)):
        return float(value)
    raise _unexpected(value, "a confidence value")


def _as_markers(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(_as_text(item) for item in value)
    raise _unexpected(value, "a marker array")


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")
