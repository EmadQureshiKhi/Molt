"""The Binding_Detector's three kinds, its collapse, and the path its writes take.

Nothing here opens a socket. A recording cursor answers each statement from a
script and keeps what it was sent, so what is asserted is the statements the
detector really produced and the parameters it really bound.

Four claims are made, and each is one the live suite cannot make cheaply.

The three kinds are produced with the confidences the requirement fixes, from the
sources the requirement names: the Session's Client, the current claims on the
direct parents, and the Clients whose configured markers occur in the text.

The marker policy is asserted as a table of decisions rather than as prose. Each
row is a governance judgement about whether one Client may reach one Artifact, so
each is written down and checked.

Collapsing is order-independent. Every permutation of a detection set produces the
same claims, which is what keeps the stored detection method from recording
whichever kind was produced last.

Every write leaves through the attribution module. The statements a whole detection
sends are the layer's own literals and nothing else, a pair holding no current
version takes the plain insert, a differing method closes the current version and
inserts a successor carrying the greater of the two confidences, and a submission
saying nothing new sends no write at all.

**Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 43.3**
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import permutations
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.models.artifact import ArtifactKind, ArtifactRef
from molt.models.binding import BindingMethod
from molt.store.attribution import (
    CLOSE_CURRENT_VERSION_STATEMENT,
    CURRENT_PAIR_QUERY,
    CURRENT_VERSION_PREDICATE,
    INSERT_SUCCESSOR_STATEMENT,
    INSERT_VERSION_STATEMENT,
    AttributionOutcome,
    SupersessionContext,
)
from molt.store.binding_detector import (
    MARKER_CLIENTS_QUERY,
    MARKER_CONFIDENCE,
    METHOD_PRECEDENCE,
    PARENT_BINDINGS_QUERY,
    SCOPE_CONFIDENCE,
    Detection,
    DetectionRequest,
    bindings_for,
    collapse,
    marker_in_text,
    write_bindings,
)
from molt.store.chain import APPEND_STATEMENT

# The one parameter marker the driver substitutes for.
PARAMETER_MARKER: Final[str] = "%s"

# The statements a whole detection may send: this module's two reads, the
# attribution module's decision read and its three write statements, and the
# Ledger append a supersession makes. Anything else would mean the detector wrote
# the binding table itself.
PERMITTED_STATEMENTS: Final[frozenset[str]] = frozenset(
    {
        MARKER_CLIENTS_QUERY,
        PARENT_BINDINGS_QUERY,
        CURRENT_PAIR_QUERY,
        INSERT_VERSION_STATEMENT,
        CLOSE_CURRENT_VERSION_STATEMENT,
        INSERT_SUCCESSOR_STATEMENT,
        APPEND_STATEMENT,
    }
)

# The fragments the script answers by, each occurring in one statement of a driven
# detection. The successor's fragment is looked for first, because the successor's
# insert also carries the plain insert's leading text.
MARKER_READ: Final[str] = "FROM client WHERE array_length"
PARENT_READ: Final[str] = "max(confidence)"
PAIR_READ: Final[str] = "SELECT id, method, confidence FROM client_binding"
SUCCESSOR_WRITE: Final[str] = "greatest("
CLOSURE_WRITE: Final[str] = "UPDATE client_binding SET valid_to"
VERSION_WRITE: Final[str] = "INSERT INTO client_binding"
LEDGER_WRITE: Final[str] = "INSERT INTO ledger"

# An instant with an offset, derived from the epoch so no example depends on when
# it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
CLOSED_AT: Final[datetime] = MOMENT + timedelta(seconds=1)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# The confidences the driven cases carry, each distinct so no expectation about one
# is satisfied by another. The inherited pair are values a parent's own claim could
# hold; the prior value is what a stored version already holds.
PARENT_CONFIDENCE: Final[float] = 0.4
OTHER_PARENT_CONFIDENCE: Final[float] = 0.75
PRIOR_CONFIDENCE: Final[float] = 0.55

# The identifiers every driven case names. The Clients are fixed at module scope so
# the expected Client ordering is the ordering of these values.
ARTIFACT_ID: Final[UUID] = uuid4()
PARENT_ID: Final[UUID] = uuid4()
OTHER_PARENT_ID: Final[UUID] = uuid4()
SCOPE_CLIENT: Final[UUID] = uuid4()
PARENT_CLIENT: Final[UUID] = uuid4()
MARKER_CLIENT: Final[UUID] = uuid4()
SESSION_ID: Final[UUID] = uuid4()
PRIOR_VERSION_ID: Final[UUID] = uuid4()
WRITTEN_VERSION_ID: Final[UUID] = uuid4()

AGENT_CLI: Final[str] = "molt"
MACHINE_ID: Final[str] = "unit-machine"

# A marker and a text it occurs in, used where a case needs a match without the
# match itself being the subject.
MARKER: Final[str] = "acme"
TEXT: Final[str] = "the acme migration failed"

ARTIFACT: Final[ArtifactRef] = ArtifactRef(
    id=ARTIFACT_ID,
    kind=ArtifactKind.DERIVED_ARTIFACT,
    client_id=SCOPE_CLIENT,
)

CONTEXT: Final[SupersessionContext] = SupersessionContext(
    session_id=SESSION_ID,
    agent_cli=AGENT_CLI,
    machine_id=MACHINE_ID,
    expires_at=EXPIRY,
)

# The rows the script answers the writes with, each of the width its statement
# selects.
WRITTEN_ROW: Final[tuple[object, ...]] = (WRITTEN_VERSION_ID, SCOPE_CONFIDENCE, MOMENT)
CLOSED_ROW: Final[tuple[object, ...]] = (
    PRIOR_VERSION_ID,
    ArtifactKind.DERIVED_ARTIFACT.value,
    BindingMethod.INHERITED.value,
    PRIOR_CONFIDENCE,
    MOMENT,
    CLOSED_AT,
)
APPENDED_ROW: Final[tuple[object, ...]] = (1, "b" * 64, "0" * 64, "c" * 64)


# ---------------------------------------------------------------------------
# The recording cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sent:
    """One statement a driven detection sent, with what it bound."""

    statement: str
    parameters: tuple[object, ...] | None


class ScriptedCursor:
    """A cursor answering from a fragment script and keeping what it was sent."""

    def __init__(self, answers: Mapping[str, Sequence[tuple[object, ...]]] | None = None) -> None:
        self._answers = (
            {} if answers is None else {key: tuple(rows) for key, rows in answers.items()}
        )
        self.sent: list[Sent] = []
        self._armed: tuple[tuple[object, ...], ...] = ()

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then arm the rows its fragment answers with."""
        self.sent.append(Sent(query, None if params is None else tuple(params)))
        self._armed = ()
        for fragment, rows in self._answers.items():
            if fragment in query:
                self._armed = rows
                break
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or None when the statement armed none."""
        return self._armed[0] if self._armed else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._armed)

    def close(self) -> None:
        """Release this cursor."""

    @property
    def statements(self) -> list[str]:
        """Every statement this cursor was sent, in order."""
        return [record.statement for record in self.sent]

    def bound_for(self, statement: str) -> tuple[object, ...] | None:
        """The parameters of the one occurrence of a statement."""
        matches = [record.parameters for record in self.sent if record.statement == statement]
        assert len(matches) == 1, f"the statement was sent {len(matches)} time(s), not once"
        return matches[0]


def request(
    *,
    text: str | None = None,
    parents: tuple[ArtifactRef, ...] = (),
) -> DetectionRequest:
    """The detection request every case is driven with, varying two of its fields."""
    return DetectionRequest(
        artifact=ARTIFACT,
        scope_client_id=SCOPE_CLIENT,
        text=text,
        parents=parents,
    )


def parent_ref(identifier: UUID) -> ArtifactRef:
    """One parent of the driven Artifact, owned by the Client bound to it."""
    return ArtifactRef(id=identifier, kind=ArtifactKind.DERIVED_ARTIFACT, client_id=PARENT_CLIENT)


def detection(client_id: UUID, method: BindingMethod, confidence: float) -> Detection:
    """One detection, as a kind produces it."""
    return Detection(client_id=client_id, method=method, confidence=confidence)


def first_write_answers(
    *,
    markers: tuple[tuple[object, ...], ...] = (),
    parents: tuple[tuple[object, ...], ...] = (),
) -> dict[str, Sequence[tuple[object, ...]]]:
    """A script in which every pair holds no current version, so each write inserts."""
    return {
        MARKER_READ: markers,
        PARENT_READ: parents,
        PAIR_READ: (),
        SUCCESSOR_WRITE: (WRITTEN_ROW,),
        VERSION_WRITE: (WRITTEN_ROW,),
    }


# ---------------------------------------------------------------------------
# The two reads
# ---------------------------------------------------------------------------


def test_the_parent_read_is_the_current_form_over_one_bound_array() -> None:
    """Inheritance reads unsuperseded claims alone, for every parent in one statement."""
    assert CURRENT_VERSION_PREDICATE in PARENT_BINDINGS_QUERY
    assert "artifact_id = ANY (%s::UUID[])" in PARENT_BINDINGS_QUERY
    assert "max(confidence)" in PARENT_BINDINGS_QUERY
    assert "GROUP BY client_id" in PARENT_BINDINGS_QUERY
    assert PARENT_BINDINGS_QUERY.endswith("ORDER BY client_id")
    assert PARENT_BINDINGS_QUERY.count(PARAMETER_MARKER) == 1


def test_the_marker_read_binds_nothing_and_omits_clients_holding_no_marker() -> None:
    """The tenant roster is read once per Artifact, and only where markers exist."""
    assert PARAMETER_MARKER not in MARKER_CLIENTS_QUERY
    assert "array_length(content_markers, 1) > 0" in MARKER_CLIENTS_QUERY
    assert MARKER_CLIENTS_QUERY.endswith("ORDER BY id")


# ---------------------------------------------------------------------------
# The marker policy, as a table of governance decisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "marker", "found"),
    [
        ("acme", "acme", True),
        ("the acme migration", "acme", True),
        ("acme-payments", "acme", True),
        ("ACME.", "acme", True),
        ("client=acme;", "AcMe", True),
        ("acmePayments", "acme", True),
        ("acmePayments", "payments", True),
        ("[acme]", "acme", True),
        ("acmecorp", "acme", False),
        ("theacme", "acme", False),
        ("acme2", "acme", False),
        ("9acme", "acme", False),
        ("", "acme", False),
        ("nothing here", "acme", False),
    ],
)
def test_a_marker_is_found_only_at_a_token_boundary(text: str, marker: str, found: bool) -> None:
    """Each row is a decision about whether one Client may reach one Artifact.

    A match inside a longer word would bind an Artifact to a Client with no claim
    on it, and that binding is what the recall filter and the erasure sweep both
    trust, so the boundary rule refuses it. A match at a separator, at a case
    change, or at either end of the text is the Client's own name occurring in the
    text and is admitted.
    """
    assert marker_in_text(text, marker) is found


@pytest.mark.parametrize("marker", ["", " ", "\t\n"])
def test_a_blank_marker_matches_nothing(marker: str) -> None:
    """A marker occurring in every text would bind every Artifact to its Client."""
    assert marker_in_text("any text at all", marker) is False


def test_a_marker_holding_a_metacharacter_is_compared_as_literal_text() -> None:
    """A marker is a tenant's own text, so its punctuation matches itself."""
    assert marker_in_text("the a.me release", "a.me") is True
    assert marker_in_text("the acme release", "a.me") is False


# ---------------------------------------------------------------------------
# Collapsing
# ---------------------------------------------------------------------------


def test_collapse_keeps_the_strongest_claim_per_client_in_every_order() -> None:
    """No permutation of a detection set changes the claims it collapses to.

    Order-independence is the reason detections are collapsed before any of them is
    written. Written separately, the confidence would still come out right, because
    the greater-confidence rule is commutative, and the stored method would record
    whichever kind was submitted last.
    """
    produced = (
        detection(SCOPE_CLIENT, BindingMethod.SCOPE, SCOPE_CONFIDENCE),
        detection(SCOPE_CLIENT, BindingMethod.MARKER, MARKER_CONFIDENCE),
        detection(SCOPE_CLIENT, BindingMethod.INHERITED, PARENT_CONFIDENCE),
        detection(MARKER_CLIENT, BindingMethod.MARKER, MARKER_CONFIDENCE),
    )
    expected = (
        detection(SCOPE_CLIENT, BindingMethod.SCOPE, SCOPE_CONFIDENCE),
        detection(MARKER_CLIENT, BindingMethod.MARKER, MARKER_CONFIDENCE),
    )

    for order in permutations(produced):
        assert collapse(order) == tuple(sorted(expected, key=lambda item: str(item.client_id)))


def test_collapse_prefers_the_more_direct_kind_when_the_confidences_agree() -> None:
    """A tie is broken by how directly the kind claims the Artifact, never by order."""
    scope = detection(SCOPE_CLIENT, BindingMethod.SCOPE, SCOPE_CONFIDENCE)
    inherited = detection(SCOPE_CLIENT, BindingMethod.INHERITED, SCOPE_CONFIDENCE)
    marker = detection(MARKER_CLIENT, BindingMethod.MARKER, MARKER_CONFIDENCE)
    inherited_marker = detection(MARKER_CLIENT, BindingMethod.INHERITED, MARKER_CONFIDENCE)

    assert collapse((scope, inherited)) == (scope,)
    assert collapse((inherited, scope)) == (scope,)
    assert collapse((marker, inherited_marker)) == (marker,)
    assert collapse((inherited_marker, marker)) == (marker,)
    assert (
        METHOD_PRECEDENCE.index(BindingMethod.SCOPE)
        < METHOD_PRECEDENCE.index(BindingMethod.MARKER)
        < METHOD_PRECEDENCE.index(BindingMethod.INHERITED)
    )


def test_a_stronger_inherited_claim_outranks_a_marker_match() -> None:
    """Confidence decides before the kind does, so a certain parent claim wins."""
    marker = detection(MARKER_CLIENT, BindingMethod.MARKER, MARKER_CONFIDENCE)
    inherited = detection(MARKER_CLIENT, BindingMethod.INHERITED, SCOPE_CONFIDENCE)

    assert collapse((marker, inherited)) == (inherited,)
    assert collapse((inherited, marker)) == (inherited,)


# ---------------------------------------------------------------------------
# The three kinds, produced against the scripted cursor
# ---------------------------------------------------------------------------


def test_the_scope_client_is_detected_with_no_text_and_no_parent() -> None:
    """Every Artifact carries the Session's Client at certainty, evidence or none.

    An Artifact with no text and no parent sends no read at all: there is nothing
    to weigh, and a round trip per Artifact for an answer that cannot matter is a
    cost on the write path.
    """
    cursor = ScriptedCursor()

    detected = bindings_for(cursor, request())

    assert detected == (detection(SCOPE_CLIENT, BindingMethod.SCOPE, SCOPE_CONFIDENCE),)
    assert cursor.statements == []


def test_inheritance_carries_the_parent_confidence_for_every_bound_client() -> None:
    """The Client set grows along the edge and the confidence comes from the parent.

    This is the monotonicity Property 15 rests on, asserted at the point it is
    established: every Client claiming a parent is claimed here too, at the
    parent's own confidence rather than at one chosen by the detector.
    """
    cursor = ScriptedCursor(
        {
            PARENT_READ: (
                (PARENT_CLIENT, PARENT_CONFIDENCE),
                (MARKER_CLIENT, OTHER_PARENT_CONFIDENCE),
            )
        }
    )

    detected = bindings_for(cursor, request(parents=(parent_ref(PARENT_ID),)))

    inherited = {
        item.client_id: item for item in detected if item.method is BindingMethod.INHERITED
    }
    assert set(inherited) == {PARENT_CLIENT, MARKER_CLIENT}
    assert inherited[PARENT_CLIENT].confidence == pytest.approx(PARENT_CONFIDENCE)
    assert inherited[MARKER_CLIENT].confidence == pytest.approx(OTHER_PARENT_CONFIDENCE)
    assert {item.client_id for item in detected} >= {PARENT_CLIENT, MARKER_CLIENT}
    assert cursor.bound_for(PARENT_BINDINGS_QUERY) == ([PARENT_ID],)


def test_repeated_parents_are_asked_about_once() -> None:
    """A parent named twice seeds the same read twice for nothing."""
    cursor = ScriptedCursor({PARENT_READ: ()})

    bindings_for(
        cursor,
        request(
            parents=(parent_ref(PARENT_ID), parent_ref(PARENT_ID), parent_ref(OTHER_PARENT_ID))
        ),
    )

    assert cursor.bound_for(PARENT_BINDINGS_QUERY) == ([PARENT_ID, OTHER_PARENT_ID],)


def test_a_marker_match_detects_its_client_at_the_fixed_confidence() -> None:
    """A Client whose marker occurs in the text is claimed just below certainty."""
    cursor = ScriptedCursor({MARKER_READ: ((MARKER_CLIENT, [MARKER, "other"]),)})

    detected = bindings_for(cursor, request(text=TEXT))

    assert detection(MARKER_CLIENT, BindingMethod.MARKER, MARKER_CONFIDENCE) in detected
    assert cursor.statements == [MARKER_CLIENTS_QUERY]


def test_a_client_whose_markers_are_absent_is_not_detected() -> None:
    """The read returns every configured Client; the text decides which are claimed."""
    cursor = ScriptedCursor({MARKER_READ: ((MARKER_CLIENT, ["unrelated"]),)})

    detected = bindings_for(cursor, request(text=TEXT))

    assert {item.client_id for item in detected} == {SCOPE_CLIENT}


def test_an_artifact_with_no_text_reads_no_marker_row() -> None:
    """Whitespace is no evidence, and neither is an absent text."""
    for text in (None, "", "   \n"):
        cursor = ScriptedCursor({MARKER_READ: ((MARKER_CLIENT, [MARKER]),)})
        bindings_for(cursor, request(text=text))
        assert cursor.statements == []


def test_the_scope_owner_and_a_marker_match_collide_into_one_claim() -> None:
    """The owning Client is frequently a marker match, and holds one claim for it."""
    cursor = ScriptedCursor(
        {
            MARKER_READ: ((SCOPE_CLIENT, [MARKER]),),
            PARENT_READ: ((SCOPE_CLIENT, PARENT_CONFIDENCE),),
        }
    )

    detected = bindings_for(cursor, request(text=TEXT, parents=(parent_ref(PARENT_ID),)))

    assert detected == (detection(SCOPE_CLIENT, BindingMethod.SCOPE, SCOPE_CONFIDENCE),)


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------


def test_a_first_detection_for_a_pair_is_a_plain_insert() -> None:
    """A pair holding no current version is written once, and nothing is closed."""
    cursor = ScriptedCursor(first_write_answers())

    written = write_bindings(cursor, request(), context=CONTEXT, detected_at=MOMENT)

    assert [item.write.outcome for item in written] == [AttributionOutcome.INSERTED]
    assert cursor.statements == [CURRENT_PAIR_QUERY, INSERT_VERSION_STATEMENT]

    bound = cursor.bound_for(INSERT_VERSION_STATEMENT)
    assert bound is not None
    assert isinstance(bound[0], UUID), "the new version's identifier is generated for it"
    assert bound[1:] == (
        ARTIFACT_ID,
        ArtifactKind.DERIVED_ARTIFACT.value,
        SCOPE_CLIENT,
        BindingMethod.SCOPE.value,
        SCOPE_CONFIDENCE,
        MOMENT,
    )
    assert written[0].write.version_id == WRITTEN_VERSION_ID


def test_a_differing_method_supersedes_and_the_cluster_compares_the_confidences() -> None:
    """The stored claim is closed and a successor written, never changed in place.

    The successor's insert binds the submitted confidence beside the confidence the
    closing statement returned, so the greater of the two is chosen by the cluster
    against the version that was actually current rather than by this process
    against whatever it last read.
    """
    cursor = ScriptedCursor(
        {
            PAIR_READ: ((PRIOR_VERSION_ID, BindingMethod.INHERITED.value, PRIOR_CONFIDENCE),),
            CLOSURE_WRITE: (CLOSED_ROW,),
            SUCCESSOR_WRITE: (WRITTEN_ROW,),
            LEDGER_WRITE: (APPENDED_ROW,),
        }
    )

    written = write_bindings(cursor, request(), context=CONTEXT, detected_at=MOMENT)

    assert [item.write.outcome for item in written] == [AttributionOutcome.SUPERSEDED]
    assert written[0].write.superseded_id == PRIOR_VERSION_ID
    assert cursor.statements == [
        CURRENT_PAIR_QUERY,
        CLOSE_CURRENT_VERSION_STATEMENT,
        INSERT_SUCCESSOR_STATEMENT,
        APPEND_STATEMENT,
    ]
    assert INSERT_VERSION_STATEMENT not in cursor.statements
    successor = cursor.bound_for(INSERT_SUCCESSOR_STATEMENT)
    assert successor is not None
    assert successor[5:7] == (SCOPE_CONFIDENCE, PRIOR_CONFIDENCE)


def test_a_repeated_detection_saying_nothing_new_writes_nothing() -> None:
    """The current version already holds the maximum, so the history does not grow."""
    cursor = ScriptedCursor(
        {PAIR_READ: ((PRIOR_VERSION_ID, BindingMethod.SCOPE.value, SCOPE_CONFIDENCE),)}
    )

    written = write_bindings(cursor, request(), context=CONTEXT, detected_at=MOMENT)

    assert [item.write.outcome for item in written] == [AttributionOutcome.UNCHANGED]
    assert cursor.statements == [CURRENT_PAIR_QUERY]


def test_every_statement_a_detection_sends_belongs_to_the_layer() -> None:
    """The detector writes no statement against the binding table of its own.

    A whole detection over all three kinds sends this module's two reads and the
    attribution module's own statements, so every write is a version rather than an
    edit by construction rather than by discipline.
    """
    cursor = ScriptedCursor(
        first_write_answers(
            markers=((MARKER_CLIENT, [MARKER]),),
            parents=((PARENT_CLIENT, PARENT_CONFIDENCE),),
        )
    )

    written = write_bindings(
        cursor,
        request(text=TEXT, parents=(parent_ref(PARENT_ID),)),
        context=CONTEXT,
        detected_at=MOMENT,
    )

    assert len(written) == 3
    assert set(cursor.statements) <= PERMITTED_STATEMENTS


def test_the_written_bindings_are_one_per_client_in_client_order() -> None:
    """One claim per Client reaches the write path, in an order that does not vary."""
    cursor = ScriptedCursor(
        first_write_answers(
            markers=((MARKER_CLIENT, [MARKER]), (SCOPE_CLIENT, [MARKER])),
            parents=((PARENT_CLIENT, PARENT_CONFIDENCE), (MARKER_CLIENT, PARENT_CONFIDENCE)),
        )
    )

    written = write_bindings(
        cursor,
        request(text=TEXT, parents=(parent_ref(PARENT_ID),)),
        context=CONTEXT,
        detected_at=MOMENT,
    )

    claims = [(item.detection.client_id, item.detection.method) for item in written]
    assert claims == sorted(claims, key=lambda item: str(item[0]))
    assert dict(claims) == {
        SCOPE_CLIENT: BindingMethod.SCOPE,
        MARKER_CLIENT: BindingMethod.MARKER,
        PARENT_CLIENT: BindingMethod.INHERITED,
    }


def test_the_evidence_order_does_not_change_what_is_submitted() -> None:
    """Reversing the rows the two reads answer with submits exactly the same claims."""
    markers = ((MARKER_CLIENT, [MARKER]), (SCOPE_CLIENT, [MARKER]))
    parents = ((PARENT_CLIENT, PARENT_CONFIDENCE), (MARKER_CLIENT, OTHER_PARENT_CONFIDENCE))

    forward = ScriptedCursor(first_write_answers(markers=markers, parents=parents))
    reversed_rows = ScriptedCursor(
        first_write_answers(markers=tuple(reversed(markers)), parents=tuple(reversed(parents)))
    )

    submitted: list[list[tuple[object, ...] | None]] = []
    for cursor in (forward, reversed_rows):
        write_bindings(
            cursor,
            request(text=TEXT, parents=(parent_ref(PARENT_ID),)),
            context=CONTEXT,
            detected_at=MOMENT,
        )
        submitted.append(
            [
                record.parameters[1:]
                for record in cursor.sent
                if record.statement == INSERT_VERSION_STATEMENT and record.parameters is not None
            ]
        )

    assert submitted[0] == submitted[1]
    assert len(submitted[0]) == 3


def test_a_naive_detection_instant_is_refused_before_anything_is_read() -> None:
    """An instant with no offset has no position on the timeline, so nothing is sent."""
    cursor = ScriptedCursor(first_write_answers())

    with pytest.raises(ValueError, match="timezone aware"):
        write_bindings(
            cursor,
            request(),
            context=CONTEXT,
            detected_at=MOMENT.replace(tzinfo=None),
        )

    assert cursor.statements == []


def test_a_confidence_outside_the_unit_interval_is_refused() -> None:
    """Requirement 12.5 is the closed unit interval, held by the detection itself."""
    for confidence in (-0.1, 1.1):
        with pytest.raises(ValueError, match="closed interval"):
            detection(SCOPE_CLIENT, BindingMethod.MARKER, confidence)
