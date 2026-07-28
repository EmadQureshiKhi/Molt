"""Property 34: a request is accepted only if it is correctly signed and in window.

**Validates: Requirements 47.1, 47.2, 47.4, 47.5, 47.7, 47.8**

The unit suite settles the four rejection causes case by case, the age boundary at
the microsecond, and the header casing. What this property adds is the cross
product: every alteration against every offset class against every body size,
where the interesting failures live. A verifier that read the body before the
headers, that compared a prefix rather than a whole digest, that measured the age
as a signed difference, or that opened its transaction before verifying would pass
several of those cases individually and fail somewhere in the product.

Five decisions shape it.

**Two harnesses, because the two halves of the claim are measured against
different clocks.** The status and the *nothing persisted* half are asserted
through the real `handler.serve`, over a store whose connection factory refuses
and counts, with nothing injected at the signature seam, so the seam the handler
resolves by name is the seam under test and the connection count is the witness:
zero attempts means nothing could have landed. But that path measures the age
against the host's own reading, taken inside the call, so a presented timestamp
placed exactly on the bound is a microsecond stale by the time it is judged. The
verdict half is therefore also asserted through the verification call directly,
with the reading injected, where the offset the generator drew is the age exactly.

**What the host path can and cannot decide is derived rather than assumed.** The
reading the presented timestamp is offset from is taken before the request is
built and a second reading is taken after the answer, so the instant the host
judged the request at is known to lie between them. The age at that instant is
therefore an interval, and the verdict is determined when the whole interval falls
on one side of the bound. It does for every alteration — an altered request is
refused whatever the clock says — and for every offset except the two that sit a
microsecond from the bound in the direction the drift moves them across it. Those
two are asserted through the injected reading, and through the host path they
still assert the load-bearing half: a refusal costs no connection, and a
connection means the request was accepted. Nothing is excused by a tolerance.

**The size band is sampled, and the 5 MiB end is crossed exactly four times.** A
hundred examples at 5 MiB apiece is half a gigabyte of allocation and of keyed
hashing, for a claim that does not vary with length. The property therefore draws
the empty body, a batch of records alone, and a batch padded into the kibibytes,
which peaks in the tens of kibibytes per example; the two 5 MiB boundary cases sit
at the foot of the module, where the request bound is crossed for real.

**The request bound and the signature check are different answers, and the
property says which bodies reach which.** `handler.serve` applies the 5 MiB bound
before it verifies anything, so a body past that bound is a 413 and never a 401.
Every body the property draws is asserted to be inside the bound, so every one of
them reaches the signature check; the 413 answer is asserted where it belongs, on
a body one byte past the bound, and asserted to be 413 whether that body is
correctly signed or not.

**A batch carries several well-formed records, and a mutation really mutates.**
Each non-empty body is a batch of at least two records that read back with no
rejection, so a refused request has a well-formed prefix that a partial write
would have left behind. The mutation arm is checked to have changed the bytes,
which for the empty body means appending rather than replacing: replacing a byte
of nothing is not a mutation, and letting it pass would have turned that arm
silently into the no-alteration arm.

The rejection metric and the log record naming the cause are the unit suite's
subject, so nothing here reads telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

import pytest
from hypothesis import event, example, given, settings
from hypothesis import strategies as st

from molt.capture.hook import batch_body
from molt.capture.signing import (
    AUTHORIZATION_HEADER,
    BEARER_SCHEME,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ingress_timestamp,
    sign_ingress,
)
from molt.collector.handler import Collector, Invocation
from molt.collector.ingress import verify_ingress
from molt.collector.routes import (
    DEFAULT_MAX_BODY_BYTES,
    EVENTS_PATH,
    RECORD_SEPARATOR,
    Headers,
    exceeds_bound,
    read_records,
)
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.errors import IngressRejectedError, StoreError
from molt.models.event import Event, EventCategory, JsonObject
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.store import Connection, MemoryStore

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

# The two credentials, shaped like the values a deployment holds and obviously
# synthetic. Neither name carries a word the credential-shape lint inspects, so no
# call site below needs a suppression in order to pass a literal.
SHARED_VALUE: Final[str] = "an-ingress-shared-value"
BEARER_VALUE: Final[str] = "a-collector-bearer-value"

MACHINE: Final[str] = "machine-under-test"
TOOL: Final[str] = "claude_code"
TIMEOUT_MS: Final[int] = 1000
SESSION_UNDER_TEST: Final[UUID] = UUID("11111111-1111-4111-8111-111111111111")

# The statuses this module distinguishes. A refused caller and an oversized body
# are different answers, and the point of naming both is that no assertion below
# may conflate them.
OK: Final[int] = 200
UNAUTHORISED: Final[int] = 401
TOO_LARGE: Final[int] = 413
UNAVAILABLE: Final[int] = 503

# The one body a refused request carries, and the one an oversized request
# carries. Restated here rather than imported, because what a caller sees is the
# subject rather than a detail to agree with the handler about.
REFUSAL_DOCUMENT: Final[JsonObject] = {"error": "unauthorised"}
OVERSIZED_DOCUMENT: Final[JsonObject] = {"error": "request body too large"}

# The bound a presented timestamp must fall inside, read from the configuration
# surface rather than restated. The Collector built below is given no value for it
# either, so both sides resolve the same default from the same declaration.
MAX_AGE_SECONDS: Final[int] = Configuration(environ={}).integer("MOLT_INGRESS_MAX_AGE_SECONDS")
MICROSECONDS_PER_SECOND: Final[int] = 1000000
MAX_AGE_MICROSECONDS: Final[int] = MAX_AGE_SECONDS * MICROSECONDS_PER_SECOND
BOUND: Final[timedelta] = timedelta(seconds=MAX_AGE_SECONDS)

# The smallest step past the bound the presented form can carry, because the
# canonical timestamp renders microseconds, and how far out the far arm sits.
ONE_MICROSECOND: Final[timedelta] = timedelta(microseconds=1)
FAR_MULTIPLE: Final[int] = 4

# How many records a drawn batch carries. At least two, so every refused batch has
# a well-formed prefix a partial write would have left behind.
MIN_RECORDS: Final[int] = 2
MAX_RECORDS: Final[int] = 4

# The size a padded body is drawn between. Kibibytes rather than mebibytes: the
# claim does not vary with length, and the 5 MiB end is crossed in the explicit
# cases at the foot of the module instead of a hundred times over.
PADDED_MIN: Final[int] = 2048
PADDED_MAX: Final[int] = 8192

# What a mutation replaces a byte with, and what it appends when there is no byte
# to replace. Both stay inside the ASCII range so the mutated body remains
# carriable as transport text; a body that is not text at all is the unit suite's
# subject, and it is the digest rather than the encoding that is under test here.
REPLACEMENTS: Final[tuple[bytes, bytes]] = (b"z", b"y")
EMPTY_BODY_MUTATION: Final[bytes] = b"z" + RECORD_SEPARATOR

# The instant every generated record is placed at, computed at runtime from a
# fixed offset so no example embeds a reading of the machine it ran on. It is
# unrelated to the request timestamp on purpose: the age bound is measured against
# what the request presents, not against what its records observed.
RECORD_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# Where the generated record identifiers start. Stated rather than drawn, so one
# drawn example renders one body however many times it is replayed.
FIRST_RECORD_ID: Final[int] = 0x4001

# How many records the explicit boundary cases carry.
EXPLICIT_RECORDS: Final[int] = 3


class BodyBand(StrEnum):
    """Where a drawn body sits in the size range the property covers.

    The empty body is its own band because it is the one accepted request that
    persists nothing by construction: an accepted batch of no records opens no
    transaction, so acceptance shows there as a 200 rather than as a connection
    attempt.
    """

    EMPTY = "empty"
    RECORDS = "records alone"
    PADDED = "records and padding"


class OffsetClass(StrEnum):
    """How far a presented timestamp sits from the reading, relative to the bound.

    Four classes, each drawn in both directions, so the bound is straddled from
    inside and outside and from ahead of the reading as well as behind it: a
    caller whose clock runs fast is presenting a request that will still be
    replayable once the reading catches up.
    """

    INSIDE = "inside the bound"
    AT_BOUND = "exactly at the bound"
    JUST_BEYOND = "one microsecond beyond the bound"
    FAR_BEYOND = "far beyond the bound"


class Alteration(StrEnum):
    """What was done to a correctly signed request before it was presented."""

    NONE = "no alteration"
    BODY = "body mutation"
    SIGNATURE = "signature mutation"
    NO_TIMESTAMP = "timestamp header removed"
    NO_SIGNATURE = "signature header removed"


# How each of the three selectors is drawn, and how wide the integer behind it is.
#
# A selector drawn as one sampled entry lands on the earliest entries of its pool
# far more often than on the last, because the draw is biased toward the low end of
# its range. The residue of an integer drawn from a wide range is flatter, which is
# also what lets a pool carry a weighting by repeating an entry: the unaltered arm
# appears twice because acceptance is the one outcome needing two dimensions to
# agree at once, and an even split over five arms would spend most of the budget
# reconfirming refusals. Neither device guarantees an arm is reached at all in a
# hundred examples of a product this wide, which is what the pinned examples on the
# property itself are for.
SELECTOR_SPREAD: Final[int] = 9999
BAND_POOL: Final[tuple[BodyBand, ...]] = tuple(BodyBand)
OFFSET_POOL: Final[tuple[OffsetClass, ...]] = tuple(OffsetClass)
ALTERATION_POOL: Final[tuple[Alteration, ...]] = (
    Alteration.NONE,
    Alteration.NONE,
    Alteration.BODY,
    Alteration.SIGNATURE,
    Alteration.NO_TIMESTAMP,
    Alteration.NO_SIGNATURE,
)

# What a pinned example fills in for the dimensions it does not name: a batch of
# the smallest size that still carries a well-formed prefix, a padded body at the
# lower end of the padded band, a mutation of the very first byte, and a fresh
# timestamp for the class that draws its distance from the reading.
PINNED_TOTAL: Final[int] = PADDED_MIN
PINNED_INDEX: Final[int] = 0
PINNED_INSIDE_MICROSECONDS: Final[int] = 0


class HostVerdict(StrEnum):
    """What the host-clock path is known to have decided about one request.

    The undecided value is not a tolerance. It names the two draws whose age
    interval straddles the bound because the host's reading advanced during the
    call, and those two are decided exactly by the injected-reading harness.
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNDECIDED = "undecided at the microsecond"


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """One request as the generator decided it, before any clock is read.

    Attributes:
        band: Which size band the body was drawn from.
        records: How many well-formed records the body carries.
        body: The bytes the signature is computed over.
        sent: The bytes the request carries, which differ from `body` on the body
            mutation arm alone.
        offset: How far the presented timestamp sits from the reading, signed, so
            a negative value is a request presenting a stale timestamp.
        offset_class: Which offset class produced that value.
        ahead: Whether the presented timestamp is ahead of the reading.
        alteration: What was done to the request after it was signed.
    """

    band: BodyBand
    records: int
    body: bytes
    sent: bytes
    offset: timedelta
    offset_class: OffsetClass
    ahead: bool
    alteration: Alteration

    @property
    def in_window(self) -> bool:
        """Whether the age bound covers this offset, taken as an absolute value."""
        return abs(self.offset) <= BOUND

    @property
    def acceptable(self) -> bool:
        """Whether this request ought to be accepted, from the drawn dimensions."""
        return self.alteration is Alteration.NONE and self.in_window


# ---------------------------------------------------------------------------
# Doubles and builders
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefusedConnections:
    """A connection factory that refuses, counting how often it was asked.

    The count is the whole witness. A request refused before the transaction opens
    leaves it at zero, so *nothing persisted* is asserted rather than assumed, and
    a request that reached the transaction leaves it above zero, so acceptance is
    asserted the same way.
    """

    attempts: int = 0

    def open(self) -> Connection:
        """Record the attempt and refuse it."""
        self.attempts += 1
        raise StoreError("this property reaches no cluster")


def build_collector(factory: RefusedConnections) -> Collector:
    """A Collector holding the shared value, with nothing injected at the seam.

    Nothing is attached to the signature seam, so the handler resolves the
    verification call by name and wraps it. That is what makes every assertion
    below a statement about the seam closing rather than about a double.
    """
    store = MemoryStore(connect_with=factory.open, statement_timeout_ms=TIMEOUT_MS)
    return Collector(
        configuration=Configuration(
            environ={
                "MOLT_COLLECTOR_MAX_BODY_BYTES": str(DEFAULT_MAX_BODY_BYTES),
                "MOLT_DB_STATEMENT_TIMEOUT_MS": str(TIMEOUT_MS),
            }
        ),
        store=store,
        bearer=Credential(
            BEARER_VALUE,
            source_name="MOLT_COLLECTOR_TOKEN",
            source=CredentialSource.ENVIRONMENT,
        ),
        ingress_key=Credential(
            SHARED_VALUE,
            source_name="MOLT_INGRESS_SECRET",
            source=CredentialSource.ENVIRONMENT,
        ),
    )


def build_record(index: int) -> Event:
    """One well-formed Event of the shape the capture side transmits."""
    return Event(
        id=UUID(int=FIRST_RECORD_ID + index),
        session_id=SESSION_UNDER_TEST,
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=RECORD_INSTANT,
        agent_cli=TOOL,
        machine_id=MACHINE,
        parent_event_id=None,
        payload={"command": "ls"},
        redacted=False,
        text_body=None,
    )


def records_body(records: int) -> bytes:
    """A batch body carrying that many well-formed records."""
    return batch_body([build_record(index) for index in range(records)])


def padded(base: bytes, total: int) -> bytes:
    """A batch grown to exactly that many bytes by one trailing blank line.

    The padding is a separator rather than a record: a line holding nothing but
    space is dropped by the batch reader, so a padded body carries exactly the
    records the unpadded one did and the well-formed prefix is untouched.
    """
    filler = total - len(base)
    if filler < 1:
        return base
    grown = base + b" " * (filler - 1) + RECORD_SEPARATOR
    assert len(grown) == total
    return grown


def body_of(band: BodyBand, records: int, total: int) -> bytes:
    """The bytes one drawn body carries."""
    if band is BodyBand.EMPTY:
        return b""
    base = records_body(records)
    if band is BodyBand.RECORDS:
        return base
    return padded(base, total)


def mutated_body(body: bytes, offset: int) -> bytes:
    """The same body with one byte changed, or one byte added when there are none.

    The offset is taken as a residue of the length, so the byte it lands on is a
    real one whatever was drawn and the draw does not have to be made against a
    body that has not been built yet.

    An empty body has no byte to replace, and replacing nothing would leave the
    bytes as they were and turn this arm silently into the no-alteration arm. The
    empty case therefore grows the body instead, which is as much a departure from
    what was signed as a replacement is.
    """
    if not body:
        return EMPTY_BODY_MUTATION
    index = offset % len(body)
    first, second = REPLACEMENTS
    replacement = first if body[index : index + 1] != first else second
    return body[:index] + replacement + body[index + 1 :]


def mutated_signature(signature: str) -> str:
    """The same digest with one hex character changed."""
    swapped = "0" if signature[0] != "0" else "1"
    return swapped + signature[1:]


def magnitude_of(offset_class: OffsetClass, inside_microseconds: int) -> timedelta:
    """How far from the reading one offset class places a presented timestamp."""
    if offset_class is OffsetClass.INSIDE:
        return timedelta(microseconds=inside_microseconds)
    if offset_class is OffsetClass.AT_BOUND:
        return BOUND
    if offset_class is OffsetClass.JUST_BEYOND:
        return BOUND + ONE_MICROSECOND
    return BOUND * FAR_MULTIPLE


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def build_request(
    *,
    band: BodyBand,
    records: int,
    total: int,
    alteration: Alteration,
    index: int,
    offset_class: OffsetClass,
    ahead: bool,
    inside_microseconds: int,
) -> SignedRequest:
    """One case, from the eight decisions that make one up.

    Written once and called by both the generator and the pinned examples, so a
    pinned arm is the same kind of case a drawn arm is rather than a second shape
    that happens to look like one.
    """
    body = body_of(band, records, total)
    magnitude = magnitude_of(offset_class, inside_microseconds)
    return SignedRequest(
        band=band,
        records=0 if band is BodyBand.EMPTY else records,
        body=body,
        sent=mutated_body(body, index) if alteration is Alteration.BODY else body,
        offset=magnitude if ahead else -magnitude,
        offset_class=offset_class,
        ahead=ahead,
        alteration=alteration,
    )


def pinned(
    band: BodyBand,
    alteration: Alteration,
    offset_class: OffsetClass,
    *,
    ahead: bool,
) -> SignedRequest:
    """One case naming the three dimensions and taking the rest as stated."""
    return build_request(
        band=band,
        records=MIN_RECORDS,
        total=PINNED_TOTAL,
        alteration=alteration,
        index=PINNED_INDEX,
        offset_class=offset_class,
        ahead=ahead,
        inside_microseconds=PINNED_INSIDE_MICROSECONDS,
    )


def selectors() -> st.SearchStrategy[int]:
    """The wide integer every selector below takes its residue from."""
    return st.integers(min_value=0, max_value=SELECTOR_SPREAD)


@st.composite
def signed_requests(draw: st.DrawFn) -> SignedRequest:
    """Draw one request: a body, a timestamp offset, and an alteration.

    The three dimensions are drawn independently, because the property is about
    their product: an alteration that is only ever tried on a small in-window
    request asserts far less than the same alteration tried on the empty body with
    a timestamp a microsecond past the bound.

    The body is built inside the draw rather than at assertion time, so one drawn
    example renders one body however often it is replayed.
    """
    band = BAND_POOL[draw(selectors()) % len(BAND_POOL)]
    records = draw(st.integers(min_value=MIN_RECORDS, max_value=MAX_RECORDS))
    total = draw(st.integers(min_value=PADDED_MIN, max_value=PADDED_MAX))
    return build_request(
        band=band,
        records=records,
        total=total,
        alteration=ALTERATION_POOL[draw(selectors()) % len(ALTERATION_POOL)],
        index=draw(st.integers(min_value=0, max_value=PADDED_MAX - 1)),
        offset_class=OFFSET_POOL[draw(selectors()) % len(OFFSET_POOL)],
        ahead=draw(st.booleans()),
        inside_microseconds=draw(st.integers(min_value=0, max_value=MAX_AGE_MICROSECONDS - 1)),
    )


# ---------------------------------------------------------------------------
# Presenting one request
# ---------------------------------------------------------------------------


def presented_headers(case: SignedRequest, reading: datetime) -> dict[str, str]:
    """The headers one request presents, signed over the body it was drawn with.

    The bearer header is always correct: the bearer gate sits ahead of the
    signature gate and answers with the same status, so a request failing both
    would prove nothing about the second.
    """
    presented = ingress_timestamp(reading + case.offset)
    signature = sign_ingress(case.body, SHARED_VALUE, presented)
    if case.alteration is Alteration.SIGNATURE:
        signature = mutated_signature(signature)
    fields = {
        AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {BEARER_VALUE}",
        TIMESTAMP_HEADER: presented,
        SIGNATURE_HEADER: signature,
    }
    if case.alteration is Alteration.NO_TIMESTAMP:
        del fields[TIMESTAMP_HEADER]
    if case.alteration is Alteration.NO_SIGNATURE:
        del fields[SIGNATURE_HEADER]
    return fields


def verified(headers: dict[str, str], body: bytes, reading: datetime) -> bool:
    """Whether the verification call accepts one request at an injected reading.

    This is the exact half. The reading the age bound is measured against is the
    one the presented timestamp was offset from, so the age is the drawn offset
    itself and the boundary is decided at the microsecond in both directions.
    """
    try:
        verify_ingress(Headers(headers), body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading)
    except IngressRejectedError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class Served:
    """What driving one request through the whole handler path produced.

    Attributes:
        status: What the caller was answered with.
        document: The body the caller was answered with.
        attempts: How often a connection was asked for, which is zero for every
            request refused before the transaction opens.
        drift: How far the host's reading had advanced by the time the answer was
            in hand, which bounds where the instant the request was judged at
            could have been.
    """

    status: int
    document: JsonObject
    attempts: int
    drift: float


def served(body: bytes, headers: dict[str, str], reading: datetime) -> Served:
    """Drive one request through `handler.serve` and record what it cost."""
    factory = RefusedConnections()
    collector = build_collector(factory)
    answer = collector.serve(
        Invocation(
            method="POST",
            path=EVENTS_PATH,
            headers=Headers(headers),
            body_text=body.decode("utf-8"),
            base64_encoded=False,
        )
    )
    return Served(
        status=answer.status,
        document=answer.document,
        attempts=factory.attempts,
        drift=(datetime.now(UTC) - reading).total_seconds(),
    )


def age_interval(offset: timedelta, drift: float) -> tuple[float, float]:
    """The ages the host could have measured for one presented timestamp.

    The presented timestamp was placed at the reading plus the offset, and the
    instant the host judged it at lies between the reading and the reading plus
    the drift. The age is the absolute difference between the two, so it spans an
    interval whose ends are the two extremes of that window, and whose lowest
    point is zero when the window contains the presented instant itself.
    """
    seconds = offset.total_seconds()
    ends = (abs(seconds), abs(drift - seconds))
    lowest = 0.0 if 0.0 <= seconds <= drift else min(ends)
    return lowest, max(ends)


def host_verdict(case: SignedRequest, drift: float) -> HostVerdict:
    """What the host-clock path is known to have decided, from the drawn case.

    An altered request is refused whatever the clock says, so every alteration arm
    is decided. An unaltered one is decided when the whole age interval falls on
    one side of the bound, which it does except for the two draws sitting a
    microsecond from the bound on the side the drift carries them across.
    """
    if case.alteration is not Alteration.NONE:
        return HostVerdict.REJECTED
    lowest, highest = age_interval(case.offset, drift)
    if highest <= float(MAX_AGE_SECONDS):
        return HostVerdict.ACCEPTED
    if lowest > float(MAX_AGE_SECONDS):
        return HostVerdict.REJECTED
    return HostVerdict.UNDECIDED


def refused(answer: Served) -> bool:
    """Whether one answer is a refusal that persisted nothing."""
    return (
        answer.status == UNAUTHORISED
        and answer.document == dict(REFUSAL_DOCUMENT)
        and answer.attempts == 0
    )


def reached_the_transaction(case: SignedRequest, answer: Served) -> bool:
    """Whether one answer is an acceptance, in the shape the batch size implies.

    A batch carrying records reaches the transaction, and the store refuses its
    connection, so acceptance is reported as unreachability. A batch carrying no
    records opens no transaction at all, so acceptance is reported as a 200 whose
    counts are both zero, and *nothing persisted* holds there because there was no
    record to persist rather than because nothing was asked for.
    """
    if not case.sent:
        return (
            answer.status == OK
            and answer.attempts == 0
            and answer.document["accepted"] == 0
            and answer.document["rejected"] == 0
        )
    return answer.status == UNAVAILABLE and answer.attempts > 0


def size_band(body: bytes) -> str:
    """Which part of the size range a body really landed in, for the record."""
    if not body:
        return "empty"
    if len(body) < PADDED_MIN:
        return "under two kibibytes"
    return "two kibibytes or more"


def record(case: SignedRequest, verdict: HostVerdict) -> None:
    """Report what one example covered, so every arm can be seen to be reached."""
    event(f"body band={case.band}")
    event(f"body size={size_band(case.sent)}")
    event(f"alteration={case.alteration}")
    event(f"timestamp offset={case.offset_class}")
    event(f"offset direction={'ahead of the reading' if case.ahead else 'behind the reading'}")
    event(f"expectation={'accepted' if case.acceptable else 'refused'}")
    event(f"host verdict={verdict}")


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 34: For any request body of 0 to 5 MiB paired with any
# request timestamp inside or outside the configured maximum request age, and any
# alteration drawn from body mutation, signature mutation, timestamp-header removal,
# and signature-header removal, a correctly signed request whose timestamp falls
# within the age bound is accepted, and every altered body, altered signature,
# absent header, and out-of-window timestamp is rejected with status code 401 while
# persisting no record from that request — including no record from the well-formed
# prefix of an otherwise valid batch.
@settings(max_examples=MAX_EXAMPLES)
@given(case=signed_requests())
# Eight pinned cases, because a hundred draws over a product this wide reach every
# arm on most seeds and not on all of them, and an arm reached in none of the
# examples asserts nothing. Between them they name every alteration arm, every
# offset class, both directions, and all three size bands: the empty body mutated,
# acceptance of the empty body, acceptance exactly at the bound ahead of the
# reading, and a correctly signed batch refused by the window alone a microsecond
# behind it.
@example(case=pinned(BodyBand.RECORDS, Alteration.NONE, OffsetClass.AT_BOUND, ahead=False))
@example(case=pinned(BodyBand.PADDED, Alteration.NONE, OffsetClass.AT_BOUND, ahead=True))
@example(case=pinned(BodyBand.EMPTY, Alteration.NONE, OffsetClass.INSIDE, ahead=True))
@example(case=pinned(BodyBand.PADDED, Alteration.NONE, OffsetClass.JUST_BEYOND, ahead=False))
@example(case=pinned(BodyBand.EMPTY, Alteration.BODY, OffsetClass.INSIDE, ahead=False))
@example(case=pinned(BodyBand.PADDED, Alteration.SIGNATURE, OffsetClass.JUST_BEYOND, ahead=True))
@example(
    case=pinned(BodyBand.RECORDS, Alteration.NO_TIMESTAMP, OffsetClass.FAR_BEYOND, ahead=False)
)
@example(case=pinned(BodyBand.RECORDS, Alteration.NO_SIGNATURE, OffsetClass.FAR_BEYOND, ahead=True))
def test_only_a_correctly_signed_in_window_request_is_accepted(case: SignedRequest) -> None:
    reading = datetime.now(UTC)
    headers = presented_headers(case, reading)
    sent_text = case.sent.decode("utf-8")

    # Every drawn body is inside the request bound, so every one of them reaches
    # the signature check rather than being answered by the bound ahead of it. The
    # 413 answer is asserted where it belongs, past the bound, at the foot of this
    # module.
    assert not exceeds_bound(
        headers, sent_text, base64_encoded=False, maximum=DEFAULT_MAX_BODY_BYTES
    )

    # A refused batch has a well-formed prefix of several records, so a write that
    # ran before verification would have left rows behind for the connection count
    # to find.
    if case.band is not BodyBand.EMPTY:
        batch = read_records(case.body)
        assert batch.rejections == ()
        assert len(batch.events) == case.records >= MIN_RECORDS

    # A mutation that left the bytes alone would be the no-alteration arm wearing
    # another name, which is the trap the empty body sets.
    if case.alteration is Alteration.BODY:
        assert case.sent != case.body

    # Requirements 47.2, 47.4, 47.5, 47.7 and 47.8 at the microsecond: with the
    # reading injected, the age is the drawn offset itself, so acceptance holds
    # exactly for a correctly signed request the bound covers and for nothing else.
    # The expectation is computed from the drawn dimensions rather than read off
    # the implementation.
    assert verified(headers, case.sent, reading) is case.acceptable, (
        f"a {case.band} body with a timestamp {case.offset_class} "
        f"{'ahead of' if case.ahead else 'behind'} the reading and {case.alteration} "
        f"was {'refused' if case.acceptable else 'accepted'}"
    )

    answer = served(case.sent, headers, reading)
    verdict = host_verdict(case, answer.drift)
    record(case, verdict)

    # Requirements 47.1 and 47.4: the same claim through the whole request path,
    # where the status the caller sees and the connection count are observable. A
    # refusal is 401 with that one body and no connection asked for; an acceptance
    # is the answer the batch size implies.
    if verdict is HostVerdict.REJECTED:
        assert refused(answer), (
            f"a request with {case.alteration} and a timestamp {case.offset_class} was "
            f"answered {answer.status} after {answer.attempts} connection attempt(s)"
        )
    elif verdict is HostVerdict.ACCEPTED:
        assert reached_the_transaction(case, answer), (
            f"a correctly signed {case.band} body with a timestamp {case.offset_class} was "
            f"answered {answer.status} after {answer.attempts} connection attempt(s)"
        )
    else:
        assert refused(answer) or reached_the_transaction(case, answer), (
            f"a request answered {answer.status} after {answer.attempts} connection "
            "attempt(s) is neither a refusal that persisted nothing nor an acceptance"
        )


# ---------------------------------------------------------------------------
# The request bound, crossed for real
# ---------------------------------------------------------------------------


def sized_batch(total: int) -> bytes:
    """A batch of several well-formed records measuring exactly that many bytes."""
    return padded(records_body(EXPLICIT_RECORDS), total)


@pytest.mark.parametrize(
    "total",
    [DEFAULT_MAX_BODY_BYTES - 1, DEFAULT_MAX_BODY_BYTES],
    ids=["one byte under the request bound", "on the request bound"],
)
def test_a_body_at_the_request_bound_still_reaches_the_signature_check(total: int) -> None:
    """The largest admissible body is verified rather than waved through.

    The property loop draws bodies in the kibibytes, because the claim does not
    vary with length and a hundred examples at 5 MiB apiece would buy padding
    rather than shapes. This is where the 5 MiB end is crossed: a body the bound
    admits is signed and accepted, and the same body with one hex character of its
    signature changed is refused with nothing persisted.
    """
    body = sized_batch(total)
    reading = datetime.now(UTC)
    headers = presented_headers(
        SignedRequest(
            band=BodyBand.PADDED,
            records=EXPLICIT_RECORDS,
            body=body,
            sent=body,
            offset=timedelta(),
            offset_class=OffsetClass.INSIDE,
            ahead=True,
            alteration=Alteration.NONE,
        ),
        reading,
    )

    accepted = served(body, headers, reading)
    forged = served(
        body,
        headers | {SIGNATURE_HEADER: mutated_signature(headers[SIGNATURE_HEADER])},
        reading,
    )

    assert accepted.status == UNAVAILABLE
    assert accepted.attempts > 0
    assert forged.status == UNAUTHORISED
    assert forged.document == dict(REFUSAL_DOCUMENT)
    assert forged.attempts == 0


def test_a_body_past_the_request_bound_is_answered_413_rather_than_401() -> None:
    """The bound is applied before the signature, so the two answers never merge.

    A body one byte past the bound is refused on its size whether it is correctly
    signed or not, which is what makes 413 a statement about the request and 401 a
    statement about the caller. Asserting that neither answer is 401 is the point:
    a property about the signature must not be readable as a claim about size.
    """
    body = sized_batch(DEFAULT_MAX_BODY_BYTES + 1)
    reading = datetime.now(UTC)
    headers = presented_headers(
        SignedRequest(
            band=BodyBand.PADDED,
            records=EXPLICIT_RECORDS,
            body=body,
            sent=body,
            offset=timedelta(),
            offset_class=OffsetClass.INSIDE,
            ahead=True,
            alteration=Alteration.NONE,
        ),
        reading,
    )

    signed = served(body, headers, reading)
    forged = served(
        body,
        headers | {SIGNATURE_HEADER: mutated_signature(headers[SIGNATURE_HEADER])},
        reading,
    )

    assert signed.status == TOO_LARGE
    assert signed.document == dict(OVERSIZED_DOCUMENT)
    assert signed.attempts == 0
    assert forged.status == TOO_LARGE
    assert forged.status != UNAUTHORISED
    assert forged.attempts == 0
