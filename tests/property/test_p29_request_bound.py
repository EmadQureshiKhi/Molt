"""Property 29: the request bound is read from a length, and refusing costs nothing.

**Validates: Requirements 5.10, 5.11**

A size bound that is enforced after the body has been decoded, or after the
records have been read, or after a connection has been leased, is not a bound on
what a request costs. This property drives bodies whose length sits well below,
one byte below, exactly on, one byte above, and far above the configured maximum,
carried as text, as text that is not ASCII, and transport-encoded, on all three
bounded routes, declaring their length honestly, dishonestly, unreadably, and not
at all, and asserts the two halves of the promise together: a body over the
maximum is a 413 that reached for no connection, and a body on or under it is not
refused on its length at all.

Seven decisions shape what is generated and what is asserted.

**The band is applied to the bytes the sender wrote.** The maximum is a bound on
the request body, and the request body is the payload: a transport encoding is how
the platform hands those bytes over rather than part of what the caller sent, so
the one number a band can be stated against is the payload length, and it is
stated against the same number whatever the carriage. That is what makes the three
carriages say different things about which reading settles a request. An ASCII body
is refused on the first and cheapest reading, because its character count is its
byte count. A body carrying characters outside ASCII has a character count *below*
its byte count, so the cheap reading cannot settle it and the exact measurement is
what refuses it. A transport-encoded body's character count is four thirds of its
payload, so the cheap reading cannot refuse it at all — a count over the maximum
proves nothing about a quantity it is an upper bound on — and the exact measurement
is what settles it in both directions.

**An understated length is generated, because it must buy nothing.** One arm
declares a single byte for a body of any size, and one declares a length past the
maximum for a body of any size. The first must not admit an oversized body and the
second must refuse an admissible one, which is what makes the header a reading
alongside the measurement rather than a substitute for it.

**The injected bound is small and the delivered bound is crossed separately.** The
property loop runs against a maximum of four kibibytes injected through the
configuration surface, and the far-above band is eight times that, so the largest
payload one example builds is 32768 bytes and the characters carrying it never pass
four thirds of that — held as text, as bytes, and once as a transport encoding,
which is well under 256 KiB of live allocation per example. The delivered default
of five mebibytes is crossed in the explicit cases at the foot of the module, as
text and transport-encoded, where one body of that size at a time is built and the
peak is a small multiple of it. Generating five mebibytes a hundred times over would
buy padding rather than shapes, and would buy it on a disk that has other uses.

**Nothing persisted is witnessed by a connection that was never asked for.** The
store's connection factory refuses every connection and counts how often it was
asked, so a count of zero says no statement could have been sent and no row could
have landed. That is what the oversized-body-with-a-well-formed-prefix arm is for:
the leading records of those bodies parse and validate perfectly, so a bound
applied any later than it is would leave a partial batch behind.

**The order of the bound and the signature is asserted from both sides.** The
signature seam is an injected verifier that counts its calls and either accepts or
refuses. An oversized body must be a 413 with that verifier never called, whether
it would have accepted or refused — so an oversized body with a bad signature is
413 rather than 401, and one with a good signature is 413 rather than accepted. A
body the bound admits must reach the verifier exactly once on the two signed
routes. One explicit case at the foot drives the same ordering with nothing
injected at the seam and a genuinely computed signature.

**Two things are deliberately not asserted here.** The bearer gate runs before the
bound, so every generated request presents the bearer value; an unauthenticated
oversized request is a 401 by that ordering and is the bearer gate's subject rather
than this one's. And the health route is not generated, because it answers before
the bound is consulted and carries no body.

**One reading is settled here rather than bracketed.** The maximum belongs to the
payload, so a caller may carry a payload of exactly the configured maximum however
the transport encodes it, and the property asserts that in both directions: an
encoded body whose payload is on the maximum is admitted although the characters
that carried it are four thirds of the maximum, and an encoded body whose payload
is one byte past it is refused although no cheap reading could have said so. The
delivered five mebibytes is crossed base64-carried as well as as text, at the foot
of the module, because that is the size at which the two readings differ by two
mebibytes of a caller's payload.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from hypothesis import event, given, settings
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
from molt.collector.routes import (
    CONTENT_LENGTH_HEADER,
    DEFAULT_MAX_BODY_BYTES,
    EVENTS_PATH,
    RECALL_PATH,
    SESSIONS_PREFIX,
    SIGNED_KINDS,
    Headers,
    RouteKind,
    exceeds_bound,
    method_of,
    read_body,
    transport_length,
)
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.errors import IngressRejectedError, StoreError
from molt.models.event import Event, EventCategory, JsonObject
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.store import Connection, MemoryStore

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

# The maximum the property loop injects through the configuration surface. Four
# kibibytes, a multiple of four so a transport-encoded body can land on it exactly,
# and comfortably above the well-formed prefix below so no band has to truncate a
# record to fit.
SMALL_BOUND: Final[int] = 4 * 1024

# The two credentials, shaped like the values a deployment holds and obviously
# synthetic. Neither name carries a word the credential-shape lint inspects.
BEARER_VALUE: Final[str] = "a-collector-bearer-value"
SHARED_VALUE: Final[str] = "an-ingress-shared-value"

# The statement timeout the store is built with, which nothing here waits on.
TIMEOUT_MS: Final[int] = 1000

# The statuses this module distinguishes. A body the bound admits may be answered
# with any of the others, which is why the admitted side asserts *not* 413 rather
# than a status it cannot reach without a cluster.
TOO_LARGE: Final[int] = 413
UNAVAILABLE: Final[int] = 503

# The body a refused request carries, read from the handler rather than guessed.
TOO_LARGE_DOCUMENT: Final[JsonObject] = {"error": "request body too large"}

# The Session the metadata route addresses and the Events name.
SESSION_UNDER_TEST: Final[UUID] = UUID("11111111-1111-4111-8111-111111111111")
MACHINE: Final[str] = "machine-under-test"
TOOL: Final[str] = "claude_code"

# The instant every generated record is placed at, read from a fixed offset rather
# than from the host so no run embeds a reading of the machine it ran on.
FIXED_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# The filler a body is grown with. One ASCII character per byte, and one character
# outside ASCII that costs two bytes, which is what lets a body's byte count exceed
# its character count by construction.
FILLER_CHARACTER: Final[str] = "x"
TWO_BYTE_CHARACTER: Final[str] = "\u00e9"
TWO_BYTE_WIDTH: Final[int] = len(TWO_BYTE_CHARACTER.encode("utf-8"))

# What a transport wrapping an encoded body inserts between its lines. Discarded
# by the decode and counted by the character reading, which is exactly the
# difference the length arithmetic has to account for.
LINE_BREAK: Final[str] = "\n"
LINE_BREAK_COUNTS: Final[tuple[int, ...]] = (0, 1, 5, 9)

# The three routes the bound is applied on. The health route is absent because it
# is answered before the bound is consulted.
BOUNDED_KINDS: Final[tuple[RouteKind, ...]] = (
    RouteKind.EVENTS,
    RouteKind.SESSION,
    RouteKind.RECALL,
)

# Spellings of the length header a transport may deliver, since the lookup is
# case-insensitive and the bound relies on that.
HEADER_SPELLINGS: Final[tuple[str, ...]] = (
    CONTENT_LENGTH_HEADER,
    CONTENT_LENGTH_HEADER.lower(),
    CONTENT_LENGTH_HEADER.upper(),
)

# Values in the length header that name no count of bytes, including the empty one
# and a negative one. All of them read as no declaration rather than as a fault.
UNREADABLE_LENGTHS: Final[tuple[str, ...]] = ("", "   ", "not a count of bytes", "-1", "12 bytes")


# ---------------------------------------------------------------------------
# The well-formed prefix
# ---------------------------------------------------------------------------


def a_record() -> Event:
    """One well-formed Event of the shape the capture side transmits."""
    return Event(
        id=uuid4(),
        session_id=SESSION_UNDER_TEST,
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=FIXED_INSTANT,
        agent_cli=TOOL,
        machine_id=MACHINE,
        parent_event_id=None,
        payload={"command": "ls"},
        redacted=False,
        text_body=None,
    )


# Several well-formed records, built once rather than per example. Several rather
# than one on purpose: these are the records a bound applied any later than it is
# would have written before noticing the size of the request they arrived in.
PREFIX_RECORD_COUNT: Final[int] = 4
PREFIX_RECORDS: Final[bytes] = batch_body([a_record() for _ in range(PREFIX_RECORD_COUNT)])
PREFIX_BYTES: Final[int] = len(PREFIX_RECORDS)
PREFIX_TEXT: Final[str] = PREFIX_RECORDS.decode("utf-8")


# ---------------------------------------------------------------------------
# What a generated request is made of
# ---------------------------------------------------------------------------


class Band(StrEnum):
    """Where the bytes a request's sender wrote sit relative to the maximum."""

    WELL_BELOW = "well below the maximum"
    ONE_BELOW = "one byte below the maximum"
    AT = "exactly on the maximum"
    ONE_ABOVE = "one byte above the maximum"
    FAR_ABOVE = "far above the maximum"


# How far each band lands from the maximum, as a signed offset on the payload. The
# well-below band is half the maximum and the far-above band is seven times past
# it, which is the same spread the request-path unit suite parametrises by hand.
# The four consecutive bands leave payload lengths of every residue modulo three
# among them, so an encoded body carries no padding, one padding character, and two
# across the bands without padding having to be drawn as a dimension of its own.
BAND_OFFSETS: Final[Mapping[Band, int]] = {
    Band.WELL_BELOW: -(SMALL_BOUND // 2),
    Band.ONE_BELOW: -1,
    Band.AT: 0,
    Band.ONE_ABOVE: 1,
    Band.FAR_ABOVE: SMALL_BOUND * 7,
}


class Carriage(StrEnum):
    """How the transport carried the body, which decides what the readings say."""

    TEXT_ASCII = "text, one byte per character"
    TEXT_MULTIBYTE = "text, more bytes than characters"
    BASE64 = "transport encoded"


class Shape(StrEnum):
    """What the body holds, which decides what a partial write would have written."""

    FILLER = "filler alone"
    WELL_FORMED_PREFIX = "well-formed records, then filler"


class Declared(StrEnum):
    """What the request said about its own length."""

    ABSENT = "no declared length"
    HONEST = "declared as carried"
    UNDERSTATED = "declared as one byte"
    UNREADABLE = "declared unreadably"
    OVERSTATED = "declared past the maximum"


@dataclass(frozen=True, slots=True)
class RequestCase:
    """One whole request, and the two facts the property is quantified over.

    Attributes:
        band: Which side of the maximum the carried bytes land on.
        carriage: How the transport carried the body.
        shape: Whether the body opens with records that would have persisted.
        declared: What the request said its own length was.
        kind: Which bounded route the request addressed.
        body_text: The body exactly as the transport delivered it.
        payload: The bytes the sender wrote, which the transport decode produces.
        payload_bytes: How many bytes the sender wrote, which the band names and
            which the maximum is taken against.
        signature_accepts: Whether the injected verifier would accept this request.
        declared_value: The length header's value, or None when it carried none.
        header_name: The spelling the transport used for the length header.
    """

    band: Band
    carriage: Carriage
    shape: Shape
    declared: Declared
    kind: RouteKind
    body_text: str
    payload: bytes
    payload_bytes: int
    signature_accepts: bool
    declared_value: str | None
    header_name: str

    @property
    def base64_encoded(self) -> bool:
        """Whether the body arrived transport-encoded."""
        return self.carriage is Carriage.BASE64

    @property
    def refused(self) -> bool:
        """Whether the maximum refuses this request, from the drawn lengths alone.

        Two readings can refuse it and they are independent: the sender wrote more
        bytes than the maximum admits, or the request declared that it did. Neither
        is a statement about the characters that carried them, and nothing here
        consults the implementation.
        """
        return self.payload_bytes > SMALL_BOUND or self.declared is Declared.OVERSTATED

    @property
    def signed_route(self) -> bool:
        """Whether this route requires the Ingress_Signature."""
        return self.kind in SIGNED_KINDS

    def headers(self) -> Headers:
        """The headers the request presents, always including the bearer value."""
        values = {AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {BEARER_VALUE}"}
        if self.declared_value is not None:
            values[self.header_name] = self.declared_value
        return Headers(values)

    def invocation(self) -> Invocation:
        """The request as the transport would deliver it, body still encoded."""
        return Invocation(
            method=method_of(self.kind),
            path=path_of(self.kind),
            headers=self.headers(),
            body_text=self.body_text,
            base64_encoded=self.base64_encoded,
        )


def path_of(kind: RouteKind) -> str:
    """The path one bounded route is addressed at."""
    if kind is RouteKind.EVENTS:
        return EVENTS_PATH
    if kind is RouteKind.RECALL:
        return RECALL_PATH
    return f"{SESSIONS_PREFIX}{SESSION_UNDER_TEST}"


# ---------------------------------------------------------------------------
# Building a body of an exact length
# ---------------------------------------------------------------------------


def filler(byte_count: int, *, multibyte: bool) -> str:
    """Filler occupying exactly that many bytes, and no well-formed record.

    The multibyte form spends two bytes per character, so a body built from it
    carries fewer characters than bytes and the cheap character reading cannot
    settle whether it is within the maximum.
    """
    if not multibyte:
        return FILLER_CHARACTER * byte_count
    whole, remainder = divmod(byte_count, TWO_BYTE_WIDTH)
    return TWO_BYTE_CHARACTER * whole + FILLER_CHARACTER * remainder


def payload_of(byte_count: int, shape: Shape) -> bytes:
    """The bytes a sender wrote, occupying exactly that many of them.

    The prefixed shape opens with whole well-formed records and grows with filler
    after them, so every record before the filler parses and validates and the
    filler itself is one trailing record that does not.
    """
    if shape is Shape.FILLER:
        return FILLER_CHARACTER.encode("ascii") * byte_count
    return PREFIX_RECORDS + FILLER_CHARACTER.encode("ascii") * (byte_count - PREFIX_BYTES)


def carried_as_text(byte_count: int, shape: Shape, *, multibyte: bool) -> tuple[str, bytes]:
    """A text body occupying exactly that many bytes, and the bytes themselves."""
    if shape is Shape.FILLER:
        body_text = filler(byte_count, multibyte=multibyte)
    else:
        body_text = PREFIX_TEXT + filler(byte_count - PREFIX_BYTES, multibyte=multibyte)
    payload = body_text.encode("utf-8")
    assert len(payload) == byte_count
    return body_text, payload


def with_line_breaks(encoded: str, count: int) -> str:
    """Insert exactly that many line breaks into an encoded body.

    A transport that wraps an encoded body leaves separators the decode discards
    and the character reading counts, so a body carrying them measures more
    characters than its encoding does while decoding to the same payload. That is
    what puts the character count further still from the quantity the bound is taken
    against, and it is why the exact measurement discards whitespace of its own.
    """
    if count <= 0:
        return encoded
    step = max(len(encoded) // (count + 1), 1)
    pieces: list[str] = []
    cursor = 0
    for _ in range(count):
        pieces.append(encoded[cursor : cursor + step])
        pieces.append(LINE_BREAK)
        cursor += step
    pieces.append(encoded[cursor:])
    return "".join(pieces)


def transport_encoded(byte_count: int, shape: Shape, *, breaks: int) -> tuple[str, bytes]:
    """An encoded body whose payload occupies exactly that many bytes.

    The payload is sized to the band and then encoded, which is the order the
    reading being asserted takes: the band belongs to the bytes the sender wrote,
    and how many characters carrying them costs is the encoding's business. The
    line breaks a wrapping transport would leave are inserted afterwards, so the
    characters exceed even the four-thirds expansion and no cheap reading of them
    could stand in for the measurement.
    """
    payload = payload_of(byte_count, shape)
    encoded = base64.b64encode(payload).decode("ascii")
    body_text = with_line_breaks(encoded, breaks)
    assert len(base64.b64decode(body_text.encode("ascii"), validate=False)) == byte_count
    return body_text, payload


def declared_value_of(declared: Declared, payload_bytes: int, spelling: str) -> str | None:
    """What the length header carried, or None when the request presented none.

    The honest declaration names the payload, because that is the quantity the
    header declares and the quantity the maximum is taken against; declaring the
    characters an encoding happened to cost would be a third reading of a different
    number rather than an honest statement of this one.
    """
    if declared is Declared.ABSENT:
        return None
    if declared is Declared.HONEST:
        return str(payload_bytes)
    if declared is Declared.UNDERSTATED:
        return "1"
    if declared is Declared.UNREADABLE:
        return spelling
    return str(SMALL_BOUND * 4)


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


@st.composite
def request_bodies(draw: st.DrawFn) -> RequestCase:
    """Draw one request whose carried length is exactly one band from the maximum.

    Every dimension the property is quantified over is drawn here: which band the
    carried bytes land in, how the transport carried them, whether the body opens
    with records that would have persisted, what the request declared about its own
    length and under which spelling of the header, which bounded route it
    addressed, and whether the signature seam would accept it.
    """
    band = draw(st.sampled_from(Band))
    carriage = draw(st.sampled_from(Carriage))
    shape = draw(st.sampled_from(Shape))
    declared = draw(st.sampled_from(Declared))
    kind = draw(st.sampled_from(BOUNDED_KINDS))
    payload_bytes = SMALL_BOUND + BAND_OFFSETS[band]

    if carriage is Carriage.BASE64:
        body_text, payload = transport_encoded(
            payload_bytes,
            shape,
            breaks=draw(st.sampled_from(LINE_BREAK_COUNTS)),
        )
    else:
        body_text, payload = carried_as_text(
            payload_bytes,
            shape,
            multibyte=carriage is Carriage.TEXT_MULTIBYTE,
        )

    spelling = draw(st.sampled_from(UNREADABLE_LENGTHS))
    return RequestCase(
        band=band,
        carriage=carriage,
        shape=shape,
        declared=declared,
        kind=kind,
        body_text=body_text,
        payload=payload,
        payload_bytes=payload_bytes,
        signature_accepts=draw(st.booleans()),
        declared_value=declared_value_of(declared, payload_bytes, spelling),
        header_name=draw(st.sampled_from(HEADER_SPELLINGS)),
    )


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefusedConnections:
    """A connection factory that refuses, counting how often it was asked.

    The count is the witness for *nothing persisted*: a request the bound refused
    leaves it at zero, so no statement could have been sent and no row could have
    landed, whatever the body held.
    """

    attempts: int = 0

    def open(self) -> Connection:
        """Record the attempt and refuse it."""
        self.attempts += 1
        raise StoreError("this property reaches no cluster")


@dataclass(slots=True)
class CountingVerifier:
    """The signature seam, counting its calls and answering as it was built to.

    Counting is what makes the ordering assertable in both directions: a request
    the bound refused must not have reached this at all, and a request the bound
    admitted on a signed route must have reached it exactly once.
    """

    accepts: bool
    calls: int = 0

    def __call__(self, headers: Mapping[str, str], body: bytes) -> None:
        """Answer one verification, refusing when this verifier was built to."""
        self.calls += 1
        if not self.accepts:
            raise IngressRejectedError(
                f"the injected verifier refused {len(body)} byte(s) under {len(headers)} header(s)"
            )


def build_collector(
    *,
    factory: RefusedConnections,
    verifier: CountingVerifier | None = None,
    ingress_key: Credential | None = None,
    maximum: int = SMALL_BOUND,
) -> Collector:
    """A Collector over a refusing store, at the maximum a caller states.

    With a verifier passed the signature seam is driven explicitly; with a shared
    value passed instead, nothing is attached and the handler resolves the real
    verification call by name, which is what the ordering case at the foot needs.
    """
    store = MemoryStore(connect_with=factory.open, statement_timeout_ms=TIMEOUT_MS)
    return Collector(
        configuration=Configuration(
            environ={
                "MOLT_COLLECTOR_MAX_BODY_BYTES": str(maximum),
                "MOLT_DB_STATEMENT_TIMEOUT_MS": str(TIMEOUT_MS),
            }
        ),
        store=store,
        bearer=Credential(
            BEARER_VALUE,
            source_name="MOLT_COLLECTOR_TOKEN",
            source=CredentialSource.ENVIRONMENT,
        ),
        ingress_key=ingress_key,
        ingress=verifier,
    )


def bearer_header() -> dict[str, str]:
    """The one header the bearer gate reads, which every request here presents."""
    return {AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {BEARER_VALUE}"}


# ---------------------------------------------------------------------------
# The coverage record
# ---------------------------------------------------------------------------


def refusing_reading(case: RequestCase) -> str:
    """Which of the three readings refuses this request, cheapest first.

    Derived from the drawn lengths rather than observed, and reported so that the
    dearest reading can be seen to be reached. A body carrying characters outside
    ASCII is over the maximum in bytes while under it in characters, and an encoded
    body's characters say nothing either way, so for both of those nothing but the
    exact measurement can refuse it.
    """
    if not case.base64_encoded and len(case.body_text) > SMALL_BOUND:
        return "the character count"
    if case.declared is Declared.OVERSTATED:
        return "the declared length"
    if case.payload_bytes > SMALL_BOUND:
        return "the exact byte length"
    return "nothing"


def record(case: RequestCase) -> None:
    """Report what one example covered, so every band can be seen to be reached."""
    event(f"band={case.band}")
    event(f"carriage={case.carriage}")
    event(f"body={case.shape}")
    event(f"length header={case.declared}")
    event(f"route={case.kind}")
    event(f"verifier={'accepts' if case.signature_accepts else 'refuses'}")
    event(f"refused={case.refused}")
    event(f"refused by={refusing_reading(case)}")
    if case.base64_encoded:
        event(f"characters over the maximum={len(case.body_text) > SMALL_BOUND}")
        event(f"padding characters={case.body_text.count('=')}")
    if case.carriage is Carriage.TEXT_MULTIBYTE:
        event(f"characters under the maximum={len(case.body_text) <= SMALL_BOUND}")


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 29: For any request body spanning sizes below, at, and
# above the configured maximum request body size, a body within the maximum is
# processed normally and a body exceeding the maximum is rejected with status code
# 413 while persisting no record from that request, including no record from the
# well-formed prefix of an oversized batch.
@settings(max_examples=MAX_EXAMPLES)
@given(case=request_bodies())
def test_a_body_over_the_maximum_is_413_and_reaches_for_no_connection(
    case: RequestCase,
) -> None:
    record(case)

    # The bound as a pure predicate, against the drawn lengths rather than against
    # anything the implementation reports: the sender wrote more bytes than the
    # maximum admits, or declared that it did, or the request is within the bound.
    # An encoded body is judged on its payload, so one carried by more characters
    # than the maximum is admitted when the bytes it decodes to are inside it.
    assert (
        exceeds_bound(
            case.headers(),
            case.body_text,
            base64_encoded=case.base64_encoded,
            maximum=SMALL_BOUND,
        )
        is case.refused
    ), (
        f"a payload of {case.payload_bytes} byte(s) carried as {case.carriage} in "
        f"{len(case.body_text)} character(s) declaring {case.declared_value!r} was judged "
        f"the other way against {SMALL_BOUND}"
    )

    # The exact length is arrived at by arithmetic over the encoded form, and the
    # transport decode produces the bytes the sender wrote. Both are checked against
    # the payload this example built, which is the only measurement taken by
    # decoding anything.
    assert transport_length(case.body_text, base64_encoded=case.base64_encoded) == len(case.payload)
    assert read_body(case.body_text, base64_encoded=case.base64_encoded) == case.payload

    factory = RefusedConnections()
    verifier = CountingVerifier(accepts=case.signature_accepts)
    collector = build_collector(factory=factory, verifier=verifier)

    answer = collector.serve(case.invocation())

    if case.refused:
        # Requirement 5.11, and the ordering that makes it structural: the status is
        # 413 whatever else was wrong with the request, the connection was never
        # asked for, so nothing from the well-formed prefix could have landed, and
        # the signature was never consulted, so an oversized body is refused on its
        # length rather than on its signature.
        assert answer.status == TOO_LARGE, (
            f"a payload of {case.payload_bytes} byte(s) declaring "
            f"{case.declared_value!r} was answered {answer.status}"
        )
        assert answer.document == TOO_LARGE_DOCUMENT
        assert factory.attempts == 0, (
            f"{factory.attempts} connection(s) were asked for by a request the bound refused"
        )
        assert verifier.calls == 0, "the bound must be applied before the signature is verified"
        return

    # Requirement 5.10 from the other side: a body on or under the maximum is not
    # refused on its length. What it is answered with depends on the route and on
    # the signature, and a 200 needs a cluster, so the assertion is that the length
    # did not refuse it and that it reached the check that comes next.
    assert answer.status != TOO_LARGE, (
        f"a payload of {case.payload_bytes} byte(s) carried in {len(case.body_text)} "
        f"character(s) was refused against {SMALL_BOUND}"
    )
    if case.signed_route:
        assert verifier.calls == 1, (
            "a body the bound admits reaches the signature check exactly once"
        )


# ---------------------------------------------------------------------------
# The arrangement the generator relies on
# ---------------------------------------------------------------------------


def test_every_band_leaves_room_for_the_whole_well_formed_prefix() -> None:
    """No band truncates a record, so the prefix really is well-formed everywhere.

    Every band names a payload length directly now, whatever the carriage, so the
    smallest payload any of them builds is the smallest band. If that holds the
    prefix, all of them do.
    """
    smallest = min(SMALL_BOUND + offset for offset in BAND_OFFSETS.values())
    assert smallest >= PREFIX_BYTES
    assert PREFIX_BYTES > 0


def test_the_bands_cover_every_padding_residue_an_encoding_can_carry() -> None:
    """Padding is covered by the bands rather than drawn, so it has to be checked.

    An encoded body carries no padding, one character of it, or two, according to
    the payload length modulo three, and the exact measurement subtracts exactly
    that many. Dropping padding as a drawn dimension is only sound if the bands
    between them reach all three residues.
    """
    residues = {(SMALL_BOUND + offset) % 3 for offset in BAND_OFFSETS.values()}
    assert residues == {0, 1, 2}


# ---------------------------------------------------------------------------
# The ordering, driven through the real verification call
# ---------------------------------------------------------------------------


def signed_headers(body: bytes) -> dict[str, str]:
    """The bearer header and a genuinely computed signature over these bytes.

    The instant is taken at runtime rather than written out, and the signature is
    the capture side's own, so the request really would be accepted if it were ever
    verified.
    """
    presented = ingress_timestamp(datetime.now(UTC))
    return bearer_header() | {
        TIMESTAMP_HEADER: presented,
        SIGNATURE_HEADER: sign_ingress(body, SHARED_VALUE, presented),
    }


def signing_collector(factory: RefusedConnections, maximum: int = SMALL_BOUND) -> Collector:
    """A Collector holding the shared value, with nothing injected at the seam."""
    return build_collector(
        factory=factory,
        ingress_key=Credential(
            SHARED_VALUE,
            source_name="MOLT_INGRESS_SECRET",
            source=CredentialSource.ENVIRONMENT,
        ),
        maximum=maximum,
    )


def test_the_bound_is_taken_before_the_signature_that_would_have_passed() -> None:
    """A correctly signed oversized batch is 413, and the same batch on the bound is not.

    The pair is the whole ordering claim at the byte: one byte past the maximum is
    refused before the signature is looked at, and one byte fewer gets past both the
    bound and the signature and reaches the transaction, which the refusing store
    reports as unreachability. The property loop drives the same ordering against an
    injected verifier; this drives it against the verification call the handler
    resolves by name.
    """
    over = payload_of(SMALL_BOUND + 1, Shape.WELL_FORMED_PREFIX)
    refused_factory = RefusedConnections()
    refused = signing_collector(refused_factory).serve(
        Invocation(
            method="POST",
            path=EVENTS_PATH,
            headers=Headers(signed_headers(over)),
            body_text=over.decode("utf-8"),
            base64_encoded=False,
        )
    )

    assert refused.status == TOO_LARGE
    assert refused.document == TOO_LARGE_DOCUMENT
    assert refused_factory.attempts == 0

    admitted_body = payload_of(SMALL_BOUND, Shape.WELL_FORMED_PREFIX)
    admitted_factory = RefusedConnections()
    admitted = signing_collector(admitted_factory).serve(
        Invocation(
            method="POST",
            path=EVENTS_PATH,
            headers=Headers(signed_headers(admitted_body)),
            body_text=admitted_body.decode("utf-8"),
            base64_encoded=False,
        )
    )

    assert admitted.status == UNAVAILABLE
    assert admitted_factory.attempts > 0


# ---------------------------------------------------------------------------
# The delivered maximum, crossed for real
# ---------------------------------------------------------------------------


def test_the_delivered_maximum_admits_a_body_on_it_and_refuses_the_next_byte() -> None:
    """Five mebibytes is within the bound and one byte more is not.

    The property loop straddles an injected maximum of a few kibibytes, because a
    hundred examples at this size would buy padding rather than shapes. This is
    where the delivered default is crossed, and it is crossed as the predicate alone
    so that only one body of this size exists at a time.
    """
    on_it = FILLER_CHARACTER * DEFAULT_MAX_BODY_BYTES

    assert (
        exceeds_bound(Headers({}), on_it, base64_encoded=False, maximum=DEFAULT_MAX_BODY_BYTES)
        is False
    )
    assert (
        exceeds_bound(
            Headers({}),
            on_it + FILLER_CHARACTER,
            base64_encoded=False,
            maximum=DEFAULT_MAX_BODY_BYTES,
        )
        is True
    )


def encoded_verdict(payload_bytes: int) -> tuple[bool, int]:
    """Whether the delivered maximum refuses that payload base64-carried, and its size.

    One body of this size exists at a time: the payload is encoded, judged, and
    dropped before the caller asks about another length.
    """
    body_text = base64.b64encode(FILLER_CHARACTER.encode("ascii") * payload_bytes).decode("ascii")
    verdict = exceeds_bound(
        Headers({}), body_text, base64_encoded=True, maximum=DEFAULT_MAX_BODY_BYTES
    )
    return verdict, len(body_text)


def test_the_delivered_maximum_admits_a_full_payload_the_transport_encoded() -> None:
    """Five mebibytes of payload is within the bound however the transport carried it.

    This is the size at which the two readings of the bound differ by more than a
    mebibyte of a caller's payload, and it is the case a caller notices: the
    characters carrying a payload of exactly the maximum are four thirds of the
    maximum, so a bound taken against the characters would refuse a request that
    does not exceed the configured maximum request body size at all. One byte more
    of payload is refused, so the reading gives nothing away in the direction the
    requirement cares about.
    """
    admitted, carried = encoded_verdict(DEFAULT_MAX_BODY_BYTES)
    refused, _ = encoded_verdict(DEFAULT_MAX_BODY_BYTES + 1)

    assert carried > DEFAULT_MAX_BODY_BYTES
    assert admitted is False
    assert refused is True


def test_a_body_one_byte_past_the_delivered_maximum_persists_nothing() -> None:
    """The delivered bound refuses a batch of well-formed records and reaches nothing.

    The same shape the property loop generates, at the size a deployment really
    runs with: the records at the head of this body would all have persisted if the
    bound were applied after they were read.
    """
    factory = RefusedConnections()
    verifier = CountingVerifier(accepts=True)
    collector = build_collector(
        factory=factory,
        verifier=verifier,
        maximum=DEFAULT_MAX_BODY_BYTES,
    )
    body = payload_of(DEFAULT_MAX_BODY_BYTES + 1, Shape.WELL_FORMED_PREFIX)

    answer = collector.serve(
        Invocation(
            method="POST",
            path=EVENTS_PATH,
            headers=Headers(bearer_header()),
            body_text=body.decode("utf-8"),
            base64_encoded=False,
        )
    )

    assert answer.status == TOO_LARGE
    assert answer.document == TOO_LARGE_DOCUMENT
    assert factory.attempts == 0
    assert verifier.calls == 0
    assert collector.max_body_bytes == DEFAULT_MAX_BODY_BYTES
