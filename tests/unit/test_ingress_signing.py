"""What an ingest request is signed over, and when the timestamp on it is read.

Two requirements meet in this module. Requirement 47.2 says the signature is a
keyed digest over the presented timestamp followed by the exact body bytes, and the
verifier recomputes over the raw bytes it received before any decode or parse, so
the two sides agree only if neither inserts anything the other does not. Requirement
47.10 says the signature is computed at transmission and never held, so a batch
buffered through an outage is signed with a fresh timestamp and lands inside the
configured age bound however long the outage ran.

Both are asserted here as claims about bytes. The material is compared against the
concatenation itself, for an empty body, a body carrying newlines, a body carrying
characters outside the ASCII range, and a body that is not valid text at all, so
nothing along the path may decode, re-encode, normalise, or terminate what it signs.
The verifier's side is computed independently in this module, from the header
timestamp and the transmitted bytes, with the keyed digest built from the standard
library rather than from the function under test, which is what makes the agreement
an assertion rather than a restatement.

The end-to-end agreement between this signer and the Collector's verifier belongs
to the Collector's own ingest suite, which drives the verifier over the whole
request path. What makes that agreement structural rather than a convention two
modules must remember is that both sides build the material by calling one
function, and that function is what is pinned here.

Nothing here opens a socket or consults a wall clock: the transport is a recorded
double and every instant comes from the injected manual time source.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt.capture.hook import EVENTS_PATH, Reply, Transmitter, batch_body
from molt.capture.signing import (
    AUTHORIZATION_HEADER,
    BEARER_SCHEME,
    COLLECTOR_BEARER_ENV,
    DIGEST_NAME,
    INGRESS_KEY_ENV,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    authorization,
    bearer_token,
    ingress_headers,
    ingress_timestamp,
    shared_secret,
    sign_ingress,
    signatures_match,
    signing_material,
)
from molt.capture.spool import Spool
from molt.config.resolve import Configuration
from molt.config.secrets import CREDENTIAL_PLACEHOLDER, Credential, CredentialSource
from molt.models.event import Event, EventCategory, deserialise_event, parse_timestamp

MACHINE: Final[str] = "machine-under-test"
TOOL: Final[str] = "claude_code"
COLLECTOR_URL: Final[str] = "https://collector.invalid/api"

# The two credentials, shaped like the values an operator's shell profile injects
# and obviously synthetic, so this module states nothing that could be one.
SHARED_VALUE: Final[str] = "an-ingress-shared-value"
BEARER_VALUE: Final[str] = "a-collector-bearer-value"

# The four bodies the material is asserted over. The empty one is what a request
# carrying no records would present, the newline-bearing one is the shape every
# Event batch has, the one outside the ASCII range is what a prompt in another
# script produces, and the last is not valid text at all, which is admissible
# because the verifier recomputes before any decode.
EMPTY_BODY: Final[bytes] = b""
LINES_BODY: Final[bytes] = b'{"a":1}\n{"a":2}\r\n{"a":3}\n'
WIDE_BODY: Final[bytes] = "a prompt in \u00e4nother script \u4e2d\u6587\n".encode()
UNDECODABLE_BODY: Final[bytes] = b"\xff\xfe\x00not text at all\n"
BODIES: Final[tuple[bytes, ...]] = (EMPTY_BODY, LINES_BODY, WIDE_BODY, UNDECODABLE_BODY)
BODY_IDS: Final[tuple[str, ...]] = ("empty", "newlines", "outside-ascii", "undecodable")

# How long a hex digest of this size is, which is what a presented signature must
# measure for the comparison to be against a whole digest.
DIGEST_HEX_LENGTH: Final[int] = 64

# Presented values a comparison over text cannot take: one as long as a digest and
# one longer, both carrying characters outside the ASCII range. A caller sends
# whatever it likes, so these arrive at the comparison and must be answered by it.
NON_ASCII_OF_DIGEST_LENGTH: Final[str] = "\u00e9" * DIGEST_HEX_LENGTH
NON_ASCII_PRESENTED: Final[tuple[str, ...]] = (
    NON_ASCII_OF_DIGEST_LENGTH,
    "\u4e2d\u6587",
    f"  {NON_ASCII_OF_DIGEST_LENGTH}\n",
)

# The transmitter's two bounds, and the reply a Collector that accepted the batch
# sends. Neither bound is under test here; both are stated so the transmission
# completes on its first attempt.
CAP_SECONDS: Final[float] = 5.0
DEADLINE_SECONDS: Final[float] = 10.0
RETRIES: Final[int] = 0
ACCEPTED: Final[Reply] = Reply(status=200, body=b'{"accepted": 1, "rejected": 0, "halted": false}')

# How long a batch sits in the spool before it is transmitted, as a multiple of the
# configured maximum request age. Chosen so the capture instant is far outside the
# bound the presented timestamp must fall inside.
OUTAGE_MULTIPLE: Final[float] = 4.0


class ManualClock(Protocol):
    """The manual time source, with the calls this suite makes on it."""

    def now(self) -> datetime:
        """The current wall reading."""

    def monotonic(self) -> float:
        """The current monotonic reading."""

    def advance(self, seconds: float) -> None:
        """Move both readings forward."""

    def sleep(self, seconds: float) -> None:
        """Stand in for waiting by advancing."""


# ---------------------------------------------------------------------------
# Doubles and builders
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecordedTransport:
    """A transport that answers with one scripted reply and records what it was asked."""

    reply: Reply = ACCEPTED
    sent: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)

    def send(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: object,
        *,
        timeout: float,
    ) -> Reply:
        """Record the request, ignoring the bound it was given."""
        assert timeout > 0.0
        fields = dict(headers) if isinstance(headers, dict) else {}
        self.sent.append((f"{method} {path}", body, fields))
        return self.reply

    def close(self) -> None:
        """Release nothing, because nothing was opened."""


def no_jitter(low: float, high: float) -> float:
    """Return zero, so a delay is its scheduled value."""
    assert low <= high
    return 0.0


def wrapped(value: str, env: str) -> Credential:
    """One environment-injected credential, wrapped as the reader would wrap it."""
    return Credential(value, source_name=env, source=CredentialSource.ENVIRONMENT)


def keyed_digest(material: bytes) -> str:
    """The digest a verifier holding the shared value would compute over material.

    Built from the standard library here rather than from the module under test, so
    the agreement between signer and verifier is asserted rather than restated.
    """
    return hmac.new(SHARED_VALUE.encode("utf-8"), material, sha256).hexdigest()


def verifier_signature(presented_timestamp: str, received: bytes) -> str:
    """What a verifier computes from the header it read and the bytes it received."""
    return keyed_digest(signing_material(presented_timestamp, received))


def build_event(clock: ManualClock) -> Event:
    """One Event of the shape a hook invocation spools."""
    return Event(
        id=uuid4(),
        session_id=UUID(int=1),
        client_id=UUID(int=2),
        category=EventCategory.TOOL_CALL,
        occurred_at=clock.now(),
        agent_cli=TOOL,
        machine_id=MACHINE,
        parent_event_id=None,
        payload={"tool": "read", "path": "/work/acme/writer.py"},
        redacted=False,
        text_body=None,
    )


def build_transmitter(
    spool: Spool,
    transport: RecordedTransport,
    clock: ManualClock,
) -> Transmitter:
    """A transmitter wired to the doubles, with both bounds stated explicitly."""
    return Transmitter(
        spool=spool,
        transport=transport,
        bearer=wrapped(BEARER_VALUE, COLLECTOR_BEARER_ENV),
        secret=wrapped(SHARED_VALUE, INGRESS_KEY_ENV),
        cap_seconds=CAP_SECONDS,
        soft_deadline_seconds=DEADLINE_SECONDS,
        retries=RETRIES,
        clock=clock,
        sleep=clock.sleep,
        jitter=no_jitter,
    )


def max_request_age_seconds() -> float:
    """The configured bound a presented timestamp must fall inside (Requirement 47.5)."""
    return float(Configuration(environ={}).integer("MOLT_INGRESS_MAX_AGE_SECONDS"))


def presented_age_seconds(presented_timestamp: str, at: datetime) -> float:
    """How old a presented timestamp is at a given instant, in seconds."""
    return (at - parse_timestamp(presented_timestamp)).total_seconds()


# ---------------------------------------------------------------------------
# Requirement 47.2: what the material is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", BODIES, ids=BODY_IDS)
def test_the_signed_material_is_the_timestamp_then_the_body_with_no_separator(
    body: bytes,
    time_source: ManualClock,
) -> None:
    """The material is the concatenation, and its length proves nothing was inserted.

    A separator would be safe only if both sides inserted the same one, so neither
    inserts anything. Asserting the length as well as the value is what rules out a
    separator that happens to be empty in one direction and not the other, and what
    rules out a terminator appended after the body.
    """
    presented = ingress_timestamp(time_source.now())

    material = signing_material(presented, body)

    assert material == presented.encode("utf-8") + body
    assert len(material) == len(presented.encode("utf-8")) + len(body)
    assert material[: len(presented.encode("utf-8"))] == presented.encode("utf-8")
    assert material[len(presented.encode("utf-8")) :] == body


@pytest.mark.parametrize("body", BODIES, ids=BODY_IDS)
def test_the_body_bytes_survive_the_material_unchanged(
    body: bytes,
    time_source: ManualClock,
) -> None:
    """Nothing decodes, re-encodes, or normalises what it signs.

    The verifier recomputes over the raw bytes it received, before any decode or
    parse, so a signer that decoded its body to text and encoded it back would agree
    only for the bodies where that round trip happens to be the identity. A body that
    is not valid text at all is included precisely because it must still sign.
    """
    presented = ingress_timestamp(time_source.now())

    material = signing_material(presented, body)

    assert material.endswith(body)
    assert material.count(b"\n") == presented.count("\n") + body.count(b"\n")
    assert bytes(material[len(presented.encode("utf-8")) :]) == bytes(body)


@pytest.mark.parametrize("body", BODIES, ids=BODY_IDS)
def test_a_verifier_recomputing_from_the_presented_header_derives_the_same_signature(
    body: bytes,
    time_source: ManualClock,
) -> None:
    """Both sides build the material by calling one function, so both sides agree.

    The signature is produced by the module under test and recomputed here the way a
    verifier does: the header timestamp it read, the bytes it received, and a keyed
    digest of its own. The constant-time comparison the boundary uses is the one that
    accepts it.
    """
    presented = ingress_timestamp(time_source.now())

    signature = sign_ingress(body, SHARED_VALUE, presented)

    assert signature == verifier_signature(presented, body)
    assert signatures_match(signature, verifier_signature(presented, body))
    assert len(signature) == DIGEST_HEX_LENGTH
    assert signature == signature.lower()
    assert sha256().name == DIGEST_NAME


def test_a_body_or_a_timestamp_altered_by_one_byte_no_longer_matches(
    time_source: ManualClock,
) -> None:
    """The material covers both halves, so tampering with either is detected.

    The body case is the replay a signature exists to stop: a captured request whose
    payload was edited. The timestamp case is what closes the replay window, because
    a holder of a valid signature cannot present it under a fresher timestamp.
    """
    presented = ingress_timestamp(time_source.now())
    body = LINES_BODY
    signature = sign_ingress(body, SHARED_VALUE, presented)
    edited = bytearray(body)
    edited[0] = edited[0] ^ 0x01

    assert not signatures_match(signature, verifier_signature(presented, bytes(edited)))
    assert not signatures_match(signature, verifier_signature(presented, body + b"\n"))
    time_source.advance(1.0)
    fresher = ingress_timestamp(time_source.now())
    assert fresher != presented
    assert not signatures_match(signature, verifier_signature(fresher, body))


def test_the_comparison_tolerates_the_transport_trimming_and_nothing_else(
    time_source: ManualClock,
) -> None:
    """A header arrives with whatever whitespace the transport left around it.

    Trimming is the one liberty taken, because a header value is transported with
    optional surrounding space. A digest in the wrong case, a truncated digest, and a
    prefix of the correct one are all refused, which is what keeps the comparison a
    comparison of whole digests.
    """
    presented = ingress_timestamp(time_source.now())
    signature = sign_ingress(LINES_BODY, SHARED_VALUE, presented)

    assert signatures_match(f"  {signature}\n", signature)
    assert not signatures_match(signature.upper(), signature)
    assert not signatures_match(signature[:-1], signature)
    assert not signatures_match("", signature)


@pytest.mark.parametrize("presented", NON_ASCII_PRESENTED, ids=["digest-length", "short", "spaced"])
def test_a_presented_value_outside_the_ascii_range_is_answered_rather_than_raised_on(
    presented: str,
    time_source: ManualClock,
) -> None:
    """The comparison answers a caller's value, whatever the caller sent.

    A comparison over text admits ASCII alone and refuses anything else by raising,
    so both sides are encoded before they are compared. What arrives here is
    untrusted: a value outside that range must be *not the computed one* rather
    than an exception travelling out through the verifier, past the boundary that
    turns a refusal into a 401, and into the invocation itself. The same holds for
    a presented value that has been trimmed first, because trimming a value outside
    the range leaves it outside the range.
    """
    signature = sign_ingress(LINES_BODY, SHARED_VALUE, ingress_timestamp(time_source.now()))

    matched = signatures_match(presented, signature)

    assert matched is False


def test_an_empty_shared_value_is_refused_rather_than_keying_the_digest_with_nothing(
    time_source: ManualClock,
) -> None:
    """A digest keyed with nothing is forgeable by anyone holding the body."""
    presented = ingress_timestamp(time_source.now())

    with pytest.raises(ValueError, match="empty"):
        sign_ingress(LINES_BODY, "", presented)


# ---------------------------------------------------------------------------
# The two headers one request presents
# ---------------------------------------------------------------------------


def test_the_two_headers_are_the_ones_the_verifier_reads_and_they_agree_with_each_other(
    time_source: ManualClock,
) -> None:
    """One call produces the timestamp and the signature over that same timestamp.

    The two are built together on purpose: a signature computed over a timestamp
    other than the one presented would be unverifiable, and a caller assembling the
    pair itself is a caller that can get that wrong.
    """
    body = batch_body((build_event(time_source),))

    headers = ingress_headers(body, wrapped(SHARED_VALUE, INGRESS_KEY_ENV), time_source.now())

    assert set(headers) == {TIMESTAMP_HEADER, SIGNATURE_HEADER}
    presented = headers[TIMESTAMP_HEADER]
    assert presented == ingress_timestamp(time_source.now())
    assert headers[SIGNATURE_HEADER] == verifier_signature(presented, body)
    assert presented_age_seconds(presented, time_source.now()) == 0.0


def test_a_naive_instant_is_refused_rather_than_signed_under_an_ambiguous_timestamp(
    time_source: ManualClock,
) -> None:
    """The age bound is compared against a value carrying a numeric offset.

    An instant with no offset would be read by the verifier in its own timezone, so
    a request could be refused as stale or accepted as fresh depending on where the
    two sides run.
    """
    naive = time_source.now().replace(tzinfo=None)

    with pytest.raises(ValueError, match="timestamp"):
        ingress_timestamp(naive)


def test_both_credentials_are_read_from_the_environment_alone_and_returned_wrapped() -> None:
    """A hook process holds no parameter-store access, and absence is a return value.

    The capture side spools rather than transmitting when either credential is unset,
    so the readers report absence instead of raising, and every value they do return
    is wrapped: text, representation, and format all yield the placeholder, so an
    accidental interpolation into a diagnostic line discloses the placeholder.
    """
    injected = Configuration(
        environ={
            INGRESS_KEY_ENV: SHARED_VALUE,
            COLLECTOR_BEARER_ENV: BEARER_VALUE,
            "MOLT_COLLECTOR_URL": COLLECTOR_URL,
        },
        file_values={},
    )
    absent = Configuration(environ={}, file_values={})

    held = shared_secret(injected)
    presented = bearer_token(injected)

    assert held is not None
    assert presented is not None
    assert held.reveal() == SHARED_VALUE
    assert presented.reveal() == BEARER_VALUE
    assert f"{held}" == CREDENTIAL_PLACEHOLDER
    assert repr(presented) == CREDENTIAL_PLACEHOLDER
    assert held.source is CredentialSource.ENVIRONMENT
    assert shared_secret(absent) is None
    assert bearer_token(absent) is None
    assert authorization(presented) == {AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {BEARER_VALUE}"}


# ---------------------------------------------------------------------------
# Requirement 47.10: the timestamp is read at transmission
# ---------------------------------------------------------------------------


def test_the_spool_holds_records_rather_than_a_prepared_request(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Nothing signed is buffered, which is why a fresh timestamp is available later.

    A spooled request would carry the timestamp it was prepared with, so an outage
    longer than the age bound would fill the file with batches that are refused the
    moment they become sendable. The file therefore holds Event records and neither
    header, and every line reads back as an Event.
    """
    spool = Spool(tmp_path / "spool", MACHINE)
    spool.append((build_event(time_source),))

    raw = spool.path.read_bytes()

    assert TIMESTAMP_HEADER.encode("utf-8") not in raw
    assert SIGNATURE_HEADER.encode("utf-8") not in raw
    assert SHARED_VALUE.encode("utf-8") not in raw
    assert [deserialise_event(line.decode("utf-8")).machine_id for line in raw.splitlines()] == [
        MACHINE
    ]


def test_a_batch_held_through_an_outage_is_signed_at_transmission_and_lands_in_the_bound(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Requirement 47.10, asserted as the verifier would decide it.

    The records are spooled, the clock is moved well past the configured maximum
    request age, and the batch is then transmitted. The presented timestamp is the
    one read at transmission, the presented signature verifies against the material
    built from that timestamp and the bytes actually sent, and the same signature
    does not verify against the material a capture-time timestamp would have given.
    The age of what is presented is inside the bound; the age of the capture instant
    is well outside it, which is the loss this arrangement avoids.
    """
    spool = Spool(tmp_path / "spool", MACHINE)
    spool.append((build_event(time_source),))
    captured_at = ingress_timestamp(time_source.now())
    bound = max_request_age_seconds()
    time_source.advance(bound * OUTAGE_MULTIPLE)
    transport = RecordedTransport()

    result = build_transmitter(spool, transport, time_source).emit(())

    assert result.transmitted == 1
    route, body, headers = transport.sent[0]
    assert route == f"POST {EVENTS_PATH}"
    presented = headers[TIMESTAMP_HEADER]
    assert presented == ingress_timestamp(time_source.now())
    assert presented != captured_at
    assert signatures_match(headers[SIGNATURE_HEADER], verifier_signature(presented, body))
    assert not signatures_match(headers[SIGNATURE_HEADER], verifier_signature(captured_at, body))
    assert presented_age_seconds(presented, time_source.now()) < bound
    assert presented_age_seconds(captured_at, time_source.now()) > bound
    assert spool.is_empty()
