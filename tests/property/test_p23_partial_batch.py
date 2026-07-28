"""Property 23: a batch mixing well-formed and malformed records loses neither.

**Validates: Requirements 5.6**

A hook writes records one at a time and a spool replays whatever it holds, so a
batch arriving at the Collector is an ordinary place for one truncated write, one
field of the wrong type, and one blank separator to sit beside four perfectly good
records. The promise is arithmetic: every well-formed record is kept, every
malformed one is counted under the reason its own defect implies, and the two
counts add up to the batch size with nothing left over and nothing counted twice.

Five decisions shape what is generated and where it is asserted.

**The counting identity is asserted where it is decidable without a cluster.** The
reading is a pure function of the body, so `read_records` carries the whole of the
counting claim: how many records the batch held, which of them became Events, and
which reason each rejection was filed under. What a transaction then does with
those Events is a promise about a transaction, and the Collector integration suite
asserts it against a real instance. Driving the handler here as well is still
worth it, because the envelope is where an operator reads the counts, and a store
that refuses every connection is enough to exercise it.

**With a refusing store the two handler arms are different answers, and the
property asserts each for what it can carry.** A batch holding no well-formed
record reaches no transaction, so it is answered with a 200 whose `accepted`,
`rejected`, and `rejections` fields are the reading's own counting restated on the
wire, and the connection counter proves nothing was reached for. A batch holding
one well-formed record does reach the transaction, where the refusal is
unreachability rather than a request fault, so it is answered with a 503 and the
counter is the witness that the well-formed records got that far. Asserting a 200
with a positive accepted count would need a cluster, so that is not attempted.

**Every record's expected outcome is decided by the generator, not read off the
reading.** Each drawn record carries the Event it ought to become, or the reason it
ought to be rejected under, decided from the defect that was applied to it. The
truncated arm is the one place a defect implies two different reasons: a cut inside
a multi-byte character leaves bytes that are not valid text, and any other cut
leaves a proper prefix of one compact JSON object, which is never a complete JSON
value. Variant zero of that arm cuts one byte into the multi-byte character the
payload carries, so the unreadable reason is reached by construction rather than by
luck.

**A blank line is a separator, so it is counted as neither.** Every generated
record is terminated with the separator, which is how the capture side writes them,
so the text after the final separator is empty in every example. The blank arm puts
further empty and whitespace-only lines in the middle. The batch size the property
expects is the count of records that carry something, and the accounting is asserted
against that count rather than against the number of lines.

**Oversized means long, and long is not a rejection class of its own.** The only
size bound in this design is the per-request one and it is applied before the body
is split, so a line longer than the bound cannot appear inside an accepted request
and a long line inside one is judged by parse and validate like any other. The long
arm therefore appends a two-kibibyte filler to the payload and appears in all three
outcomes — long and well formed, long and truncated, long and wrongly typed —
rather than reaching for the delivered bound. The worst example is then two hundred
long records, which is a body just under half a mebibyte and a peak of about three
mebibytes across the reading and the handler drive together, where two hundred
records at the delivered request bound would have been a gigabyte. The request
bound itself is crossed for real in the two explicit cases at the foot of the
module.

The example budget is 100 with no per-example deadline. A one-record batch and a
two-hundred-record batch of long records differ in cost by more than two orders of
magnitude, and a deadline would fail the large end for being large rather than for
being wrong.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.capture.signing import AUTHORIZATION_HEADER, BEARER_SCHEME
from molt.collector.handler import Collector, Invocation
from molt.collector.routes import (
    DEFAULT_MAX_BODY_BYTES,
    EVENTS_PATH,
    RECORD_SEPARATOR,
    Headers,
    RejectionReason,
    read_records,
    split_records,
)
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.errors import StoreError
from molt.models.event import Event, EventCategory, JsonObject, JsonValue, serialise_event
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.store import Connection, MemoryStore

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

# How many records one batch carries. The upper end is the count the plan names.
MIN_RECORDS: Final[int] = 1
MAX_RECORDS: Final[int] = 200

# The statuses this module reads off a response.
OK: Final[int] = 200
TOO_LARGE: Final[int] = 413
UNAVAILABLE: Final[int] = 503

# The five fields every ingest envelope carries.
ENVELOPE_FIELDS: Final[tuple[str, ...]] = (
    "accepted",
    "rejected",
    "halted",
    "halt_reason",
    "pending_approvals",
)

# The cold-start values the Collector under test is built with. The bound is the
# delivered one, so no example is refused on its size; the two explicit cases at
# the foot of the module state their own bound.
BEARER_VALUE: Final[str] = "a-collector-bearer-value"
TIMEOUT_MS: Final[int] = 1000
SMALL_BOUND: Final[int] = 512

# The identity every generated record carries. The Session identifiers are drawn
# from a pair, so a batch ordinarily carries records of two Sessions and the
# handler's grouping is exercised rather than bypassed.
AGENT_CLI: Final[str] = "an-agent-under-test"
MACHINE_ID: Final[str] = "machine-under-test"
SESSION_IDS: Final[tuple[UUID, ...]] = (UUID(int=(1 << 100) + 1), UUID(int=(1 << 100) + 2))

# The instant every generated record is placed at, read from a fixed offset rather
# than from the host so no run embeds a reading of the machine it ran on.
FIXED_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# The payload text every record carries, and the encoding of the one character in
# it that occupies more than a byte. A record cut one byte into that character is
# not valid text, which is how the unreadable reason is reached deliberately.
MULTIBYTE_CHARACTER: Final[str] = "\u00e9"
MULTIBYTE_SEQUENCE: Final[bytes] = MULTIBYTE_CHARACTER.encode("utf-8")
COMMAND_TEXT: Final[str] = f"npm run caf{MULTIBYTE_CHARACTER}"

# How long the filler on a long record is, and what it is made of. Two kibibytes
# exercises the long-line path at a cost two hundred records can be drawn at; the
# request bound is a different subject and is crossed in the explicit cases below.
LONG_FILLER_CHARACTERS: Final[int] = 2048
FILLER_CHARACTER: Final[str] = "x"

# The separators a compact document is rendered with, so a generated record is the
# same shape the canonical wire form is.
COMPACT: Final[tuple[str, str]] = (",", ":")

# The fields the Event shape wants as text, and the values of other types that are
# substituted into them. `payload` and `redacted` are deliberately absent: a JSON
# object is a perfectly good payload and a boolean is a perfectly good `redacted`,
# so substituting those would not be a defect in every combination.
WRONG_TYPE_FIELDS: Final[tuple[str, ...]] = (
    "agent_cli",
    "category",
    "client_id",
    "id",
    "machine_id",
    "occurred_at",
    "parent_event_id",
    "session_id",
    "text_body",
)
WRONG_TYPE_VALUES: Final[tuple[JsonValue, ...]] = (
    7,
    4.5,
    ["one", "two"],
    {"nested": "value"},
    None,
)

# How many variants one record plan is drawn across. The count is the size of the
# wrongly typed cross product, so every field is paired with every value equally
# often, and the other arms decompose the same number their own way.
VARIANT_COUNT: Final[int] = len(WRONG_TYPE_FIELDS) * len(WRONG_TYPE_VALUES)

# The forms a blank record takes: nothing at all between two separators, a run of
# spaces, and a tab.
BLANK_FORMS: Final[tuple[bytes, ...]] = (b"", b"   ", b"\t")


# ---------------------------------------------------------------------------
# What a generated batch is made of
# ---------------------------------------------------------------------------


class RecordKind(StrEnum):
    """The five kinds of record one batch mixes.

    The first three are the ordinary shapes: a record as the capture side writes
    it, a record the writer stopped part-way through, and a record carrying a
    field of the wrong type. `BLANK` is the separator case, which is a record in
    neither count. `LONG` is the long-line case, which is not a rejection class at
    all: a long record is well formed, truncated, or wrongly typed like any other,
    and this kind exists to carry each of those three at length.
    """

    VALID = "valid"
    TRUNCATED = "truncated"
    WRONG_TYPE = "wrong type"
    BLANK = "blank"
    LONG = "long"


class Defect(StrEnum):
    """What was done to a record, which is what its expected outcome follows from."""

    NONE = "none"
    TRUNCATED = "truncated"
    WRONG_TYPE = "wrong type"
    BLANK = "blank"


# How the long arm distributes its variants over the three outcomes a long record
# can have, so a long line is drawn well formed as often as it is drawn defective.
LONG_DEFECTS: Final[tuple[Defect, ...]] = (Defect.NONE, Defect.TRUNCATED, Defect.WRONG_TYPE)

# The kinds that carry a defect. A batch drawn from these alone holds no
# well-formed record at all, which is the one arm the handler answers with the
# envelope rather than with unreachability.
DEFECTIVE_KINDS: Final[tuple[RecordKind, ...]] = (
    RecordKind.TRUNCATED,
    RecordKind.WRONG_TYPE,
    RecordKind.BLANK,
    RecordKind.LONG,
)

# How often a batch is drawn from the defective kinds alone. Drawn from a weighted
# pool rather than by a coin, because a wholly malformed batch is one stated arm
# that needs reaching rather than half the budget, and a freely drawn batch of two
# hundred records holds a well-formed record with near certainty.
DEFECTIVE_SHARE: Final[tuple[bool, ...]] = (True, False, False, False)


@dataclass(frozen=True, slots=True)
class Plan:
    """One record before it is rendered: its kind and which variant of it was drawn."""

    kind: RecordKind
    variant: int


@dataclass(frozen=True, slots=True)
class Record:
    """One generated record, with what reading it ought to produce.

    Attributes:
        line: The bytes this record occupies between two separators.
        kind: Which of the five kinds produced it, for the coverage record.
        defect: What was done to it, for the coverage record.
        accepted: The Event it ought to become, or None when it is not well formed.
        reason: The reason it ought to be rejected under, or None when it is well
            formed or is a separator rather than a record.
    """

    line: bytes
    kind: RecordKind
    defect: Defect
    accepted: Event | None
    reason: RejectionReason | None

    @property
    def counted(self) -> bool:
        """Whether this record counts towards the batch size at all."""
        return self.accepted is not None or self.reason is not None

    @property
    def outcome(self) -> str:
        """What ought to become of this record, for the coverage record."""
        if self.accepted is not None:
            return "accepted"
        if self.reason is not None:
            return f"rejected as {self.reason}"
        return "separator"


@dataclass(frozen=True, slots=True)
class Batch:
    """One drawn batch: its records, and the accounting they add up to."""

    records: tuple[Record, ...]

    @property
    def body(self) -> bytes:
        """The batch body, with every record terminated by the separator.

        Termination rather than joining is what the capture side does, so the text
        after the final separator is empty in every example and the batch size is
        the count of records that carry something.
        """
        return b"".join(item.line + RECORD_SEPARATOR for item in self.records)

    @property
    def counted(self) -> int:
        """How many records this batch carries, separators excluded."""
        return sum(1 for item in self.records if item.counted)

    @property
    def blanks(self) -> int:
        """How many lines of this body are separators rather than records."""
        return len(self.records) - self.counted

    @property
    def accepted(self) -> tuple[Event, ...]:
        """The Events this batch ought to be read as, in the order they arrived."""
        return tuple(item.accepted for item in self.records if item.accepted is not None)

    @property
    def reasons(self) -> Counter[RejectionReason]:
        """How many rejections this batch ought to report under each reason."""
        return Counter(item.reason for item in self.records if item.reason is not None)

    @property
    def breakdown(self) -> JsonObject:
        """The rejection breakdown an envelope ought to carry for this batch."""
        return {str(reason): count for reason, count in self.reasons.items()}


# ---------------------------------------------------------------------------
# Rendering one record
# ---------------------------------------------------------------------------


def well_formed_event(index: int, session_id: UUID, *, filler: int = 0) -> Event:
    """One Event of the shape the capture side transmits.

    The identifier is derived from the record's position rather than drawn, so a
    failing example replays to the same batch and the accepted Events of one batch
    are distinguishable from one another.
    """
    return Event(
        id=UUID(int=index + 1),
        session_id=session_id,
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=FIXED_INSTANT,
        agent_cli=AGENT_CLI,
        machine_id=MACHINE_ID,
        parent_event_id=None,
        payload={"command": COMMAND_TEXT + FILLER_CHARACTER * filler},
        redacted=False,
        text_body=None,
    )


def truncation_of(blob: bytes, variant: int) -> int:
    """Where a truncated record is cut, in bytes.

    Variant zero cuts one byte into the multi-byte character the payload carries,
    so a record that is not valid text is reached by construction rather than by
    luck. Every other variant spreads the cut over the whole record, and the range
    keeps at least the first byte and drops at least the last, so every drawn cut
    really does truncate.
    """
    if variant == 0:
        return blob.index(MULTIBYTE_SEQUENCE) + 1
    return 1 + variant * (len(blob) - 2) // (VARIANT_COUNT - 1)


def reason_of(line: bytes) -> RejectionReason:
    """Which reason a truncated record's own defect implies.

    Bytes that are not valid text are unreadable. Anything else here is a proper
    prefix of one compact JSON object, and a proper prefix of a JSON object is
    never a complete JSON value, so it is unparsed. Neither branch consults the
    reading under test: the first is the stated definition of unreadable and the
    second is a fact about what the generator produced.
    """
    try:
        line.decode("utf-8")
    except UnicodeDecodeError:
        return RejectionReason.UNREADABLE
    return RejectionReason.UNPARSED


def wrongly_typed(blob: bytes, variant: int) -> bytes:
    """One serialisation with a text field replaced by a value of another type.

    The record is still one JSON object, which is what makes it invalid rather than
    unparsed: it is a document the Event shape refuses rather than a document no
    reader can decode.
    """
    decoded: object = json.loads(blob.decode("utf-8"))
    assert isinstance(decoded, dict)
    fields: JsonObject = {str(name): value for name, value in decoded.items()}
    fields[WRONG_TYPE_FIELDS[variant % len(WRONG_TYPE_FIELDS)]] = WRONG_TYPE_VALUES[
        variant // len(WRONG_TYPE_FIELDS) % len(WRONG_TYPE_VALUES)
    ]
    return json.dumps(fields, separators=COMPACT, ensure_ascii=False).encode("utf-8")


def defect_of(plan: Plan) -> Defect:
    """What is done to one record, given the kind and variant that were drawn."""
    if plan.kind is RecordKind.VALID:
        return Defect.NONE
    if plan.kind is RecordKind.TRUNCATED:
        return Defect.TRUNCATED
    if plan.kind is RecordKind.WRONG_TYPE:
        return Defect.WRONG_TYPE
    if plan.kind is RecordKind.BLANK:
        return Defect.BLANK
    return LONG_DEFECTS[plan.variant % len(LONG_DEFECTS)]


def realise(index: int, plan: Plan) -> Record:
    """Render one drawn plan into bytes, with the outcome the plan implies."""
    defect = defect_of(plan)
    if defect is Defect.BLANK:
        return Record(
            line=BLANK_FORMS[plan.variant % len(BLANK_FORMS)],
            kind=plan.kind,
            defect=defect,
            accepted=None,
            reason=None,
        )

    filler = LONG_FILLER_CHARACTERS if plan.kind is RecordKind.LONG else 0
    record = well_formed_event(
        index,
        SESSION_IDS[plan.variant % len(SESSION_IDS)],
        filler=filler,
    )
    blob = serialise_event(record).encode("utf-8")

    if defect is Defect.NONE:
        return Record(line=blob, kind=plan.kind, defect=defect, accepted=record, reason=None)
    if defect is Defect.TRUNCATED:
        line = blob[: truncation_of(blob, plan.variant)]
        return Record(
            line=line,
            kind=plan.kind,
            defect=defect,
            accepted=None,
            reason=reason_of(line),
        )
    return Record(
        line=wrongly_typed(blob, plan.variant),
        kind=plan.kind,
        defect=defect,
        accepted=None,
        reason=RejectionReason.INVALID,
    )


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def defective_variants() -> st.SearchStrategy[int]:
    """Draw a variant that leaves a long record defective.

    The long arm spreads its variants over the three outcomes a long line can have,
    one of which is well formed. A batch that must hold no well-formed record steps
    past that one variant rather than filtering it out, so no draw is spent on an
    example that is then discarded.
    """
    return st.integers(min_value=0, max_value=VARIANT_COUNT - 1).map(
        lambda variant: variant if variant % len(LONG_DEFECTS) else variant + 1
    )


def record_plans(*, well_formed: bool) -> st.SearchStrategy[Plan]:
    """Draw one record: which kind it is, and which variant of it.

    Two draws per record and nothing else. The variant is one integer rather than a
    field, a value, and an offset drawn separately, because a two-hundred-record
    batch drawn at four values per record spends its entropy budget on the batch
    rather than on the shapes; each arm decomposes the one integer its own way.
    """
    if not well_formed:
        return st.builds(Plan, st.sampled_from(DEFECTIVE_KINDS), defective_variants())
    return st.builds(
        Plan,
        st.sampled_from(RecordKind),
        st.integers(min_value=0, max_value=VARIANT_COUNT - 1),
    )


@st.composite
def batches(draw: st.DrawFn) -> Batch:
    """Draw one batch of 1 to 200 records mixing all five kinds.

    The size is drawn as a number before the records are drawn, rather than left to
    a list's own sizing, because the long batches are the interesting end: a
    two-hundred-record batch is where every kind appears at once, and a list
    generator concentrates near its lower bound.

    A quarter of the batches are drawn from the defective kinds alone. That is what
    makes the handler's envelope arm ordinary: with a refusing store, only a batch
    yielding no Event at all is answered with the counts rather than with
    unreachability, and a batch of this length otherwise almost always yields one.
    """
    well_formed = not draw(st.sampled_from(DEFECTIVE_SHARE))
    size = draw(st.integers(min_value=MIN_RECORDS, max_value=MAX_RECORDS))
    plans = [draw(record_plans(well_formed=well_formed)) for _ in range(size)]
    return Batch(records=tuple(realise(index, plan) for index, plan in enumerate(plans)))


# ---------------------------------------------------------------------------
# The harness: a Collector over a store that refuses every connection
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefusedConnections:
    """A connection factory that refuses, counting how often it was asked.

    The count is the witness for what reached the transaction: a batch whose
    records were all malformed leaves it at zero, and a batch carrying one
    well-formed record moves it to one.
    """

    attempts: int = 0

    def open(self) -> Connection:
        """Record the attempt and refuse it."""
        self.attempts += 1
        raise StoreError("this property reaches no cluster")


def build_collector(
    factory: RefusedConnections,
    *,
    maximum: int = DEFAULT_MAX_BODY_BYTES,
) -> Collector:
    """A Collector over a refusing store, with the signature seam accepting.

    The verifier is injected rather than loaded, because what a signature does with
    a request is another property's subject and all this one needs is for a signed
    route to proceed to the reading.
    """

    def accept(headers: object, body: bytes) -> None:
        assert isinstance(body, bytes)
        assert headers is not None

    return Collector(
        configuration=Configuration(
            environ={
                "MOLT_COLLECTOR_MAX_BODY_BYTES": str(maximum),
                "MOLT_DB_STATEMENT_TIMEOUT_MS": str(TIMEOUT_MS),
            }
        ),
        store=MemoryStore(connect_with=factory.open, statement_timeout_ms=TIMEOUT_MS),
        bearer=Credential(
            BEARER_VALUE,
            source_name="MOLT_COLLECTOR_TOKEN",
            source=CredentialSource.ENVIRONMENT,
        ),
        ingress=accept,
    )


def posted(body: bytes) -> Invocation:
    """One authenticated batch request, as the transport would deliver it."""
    return Invocation(
        method="POST",
        path=EVENTS_PATH,
        headers=Headers({AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {BEARER_VALUE}"}),
        body_text=body.decode("utf-8"),
        base64_encoded=False,
    )


# ---------------------------------------------------------------------------
# The coverage record
# ---------------------------------------------------------------------------


def size_band(size: int) -> str:
    """Which part of the size range a batch sits in, for the coverage record."""
    if size == MIN_RECORDS:
        return "one record"
    if size < MAX_RECORDS // 2:
        return "under a hundred records"
    return "a hundred records or more"


def record_coverage(batch: Batch) -> None:
    """Report what one example covered, so the arms can be seen to be reached."""
    event(f"batch size={size_band(len(batch.records))}")
    event(f"carries a well-formed record={bool(batch.accepted)}")
    event(f"carries a separator={batch.blanks > 0}")
    for item in batch.records:
        event(f"record kind={item.kind}")
        event(f"record outcome={item.outcome}")


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 23: For any batch mixing well-formed and malformed
# records, the persisted record count equals the well-formed record count and the
# reported accepted and rejected counts sum to the batch size.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(batch=batches())
def test_a_batch_mixing_well_formed_and_malformed_records_accounts_for_every_one(
    batch: Batch,
) -> None:
    record_coverage(batch)

    read = read_records(batch.body)

    # The batch size is the count of records that carry something, so the trailing
    # separator every well-formed batch ends with, and every blank line inside it,
    # count as neither accepted nor rejected.
    assert read.records == batch.counted, (
        f"the reading reports {read.records} record(s) where the batch carries "
        f"{batch.counted}, with {batch.blanks} separator(s) among its "
        f"{len(batch.records)} line(s)"
    )
    assert len(split_records(batch.body)) == batch.counted
    assert read.records == len(batch.records) - batch.blanks

    # Requirement 5.6, the counting identity: the two counts add up to the batch
    # size, with nothing left over and nothing counted twice.
    assert len(read.events) + len(read.rejections) == read.records

    # Every well-formed record is kept, and kept as the Event it was written from,
    # in the order it arrived. The expectation is the batch's own record of what it
    # generated rather than anything the reading reported.
    assert read.events == batch.accepted, (
        f"the reading kept {len(read.events)} record(s) where the batch carries "
        f"{len(batch.accepted)} well-formed one(s)"
    )

    # Each rejection is filed under the reason its own record's defect implies, so a
    # batch whose reasons were miscategorised fails here rather than passing on the
    # strength of the total alone.
    assert Counter(read.rejections) == batch.reasons, (
        f"the reading reports {dict(Counter(read.rejections))} where the batch's "
        f"defects imply {dict(batch.reasons)}"
    )

    # The same batch through the handler. A store that refuses every connection
    # splits the two arms: a batch holding no well-formed record reaches no
    # transaction and is answered with the envelope, and a batch holding one does
    # reach the transaction, where a refused connection is unreachability.
    factory = RefusedConnections()
    answer = build_collector(factory).ingest(batch.body)

    if batch.accepted:
        assert answer.status == UNAVAILABLE
        assert factory.attempts == 1, (
            f"{factory.attempts} connection(s) were asked for by a batch carrying "
            f"{len(batch.accepted)} well-formed record(s)"
        )
        return

    assert answer.status == OK
    assert factory.attempts == 0
    for name in ENVELOPE_FIELDS:
        assert name in answer.document
    assert answer.document["accepted"] == 0
    assert answer.document["rejected"] == read.records
    assert answer.document.get("rejections", {}) == batch.breakdown


# ---------------------------------------------------------------------------
# The request bound, crossed for real
# ---------------------------------------------------------------------------


def long_record_body() -> bytes:
    """A one-record batch whose record is longer than the small bound.

    The record is well formed, which is the point: what refuses it in the first
    case below is its length against the request bound, and nothing about its
    shape.
    """
    record = well_formed_event(0, SESSION_IDS[0], filler=SMALL_BOUND * 8)
    body = serialise_event(record).encode("utf-8") + RECORD_SEPARATOR
    assert len(body) > SMALL_BOUND
    return body


def test_a_record_longer_than_the_request_bound_is_never_split_from_the_body() -> None:
    """The bound is applied before the split, so no reading of it happens at all.

    This is the other half of *there is no per-record bound*: the property loop
    reads long records inside an accepted request, and what stops a line longer
    than the request bound from ever appearing there is this refusal.
    """
    factory = RefusedConnections()
    collector = build_collector(factory, maximum=SMALL_BOUND)

    answer = collector.serve(posted(long_record_body()))

    assert answer.status == TOO_LARGE
    assert factory.attempts == 0


def test_a_long_record_inside_an_accepted_request_reaches_the_transaction() -> None:
    """Under the delivered bound the same record is judged by parse and validate.

    The connection counter is what says it got that far: with a refusing store the
    answer is unreachability, which is only reachable by a batch that yielded a
    well-formed record for the transaction to attempt.
    """
    body = long_record_body()
    factory = RefusedConnections()
    collector = build_collector(factory)

    read = read_records(body)
    answer = collector.serve(posted(body))

    assert len(read.events) == 1
    assert read.rejections == ()
    assert answer.status == UNAVAILABLE
    assert factory.attempts == 1
