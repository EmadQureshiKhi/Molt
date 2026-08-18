"""Ingress_Signature verification: what is accepted, what is refused, and how.

This suite asserts the verifying half of the boundary the signing suite pins. It
needs no cluster: the store it is given refuses every connection, so a test can
assert both that a refused request never reached for one and that an accepted
request did, which is how *nothing persisted* and *the transaction was reached*
are told apart here without a database.

Three arrangements carry most of the weight.

The pair is asserted rather than restated. The digest the verifier computes is
compared against the signer's own output and against a digest built here from the
standard library, and the request is driven through the real `handler.serve` with
no verifier injected, so the seam the handler resolves by name is the seam under
test. What the capture side signs is what the Collector accepts, and nothing in
between is stubbed.

Every rejection is asserted on three things at once: the status the caller sees,
the connection count the store recorded, and the cause the log record named. The
first two are the requirement; the third is what an operator has instead of a
response that distinguishes the causes, which by design it does not.

Nothing here waits on a clock. The age bound is examined at the microsecond in
both directions through the injected reading, and the one path that has no
injected reading by design — the handler's own, which takes the host's — is driven
by offsetting a presented timestamp rather than by letting time pass. The two
directions are different widths: the window runs the configured maximum age
backwards from the reading and only the skew allowance forwards, and the sum of
those is asserted directly as the interval a captured request stays replayable in.

The last two sections leave the signature boundary and take up the other check
that runs before a durable write: that nothing content-bearing reaches a record
unredacted, and that when an operator switches redaction off, something says so.
They sit here because they are the same class of claim — a boundary examined ahead
of a write that cannot be taken back — and because the recall Ledger append and the
adapter Event construction are the two write paths whose redaction was decided by a
hardcoded flag rather than by the Redactor's own report.

**Validates: Requirements 1.7, 1.8, 4.1, 4.6, 5.13, 47.1, 47.2, 47.3, 47.4, 47.5,
47.6, 47.7, 47.8, 47.9, 47.11, 47.12, 47.13**
"""

from __future__ import annotations

import hmac
import io
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt import recall
from molt.capture.adapters import builders
from molt.capture.hook import batch_body
from molt.capture.protocol import UNASSIGNED_CLIENT, CaptureContext, SystemClock
from molt.capture.signing import (
    AUTHORIZATION_HEADER,
    BEARER_SCHEME,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ingress_timestamp,
    sign_ingress,
    signing_material,
)
from molt.collector.handler import COMPONENT as HANDLER_COMPONENT
from molt.collector.handler import (
    INGRESS_MODULE,
    Collector,
    Invocation,
    _load_verification,
)
from molt.collector.ingress import (
    CLOCK_SKEW_ALLOWANCE_S,
    COMPONENT,
    SIGNATURE_REJECTED_METRIC,
    RejectionCause,
    verify_ingress,
)
from molt.collector.routes import (
    DEFAULT_MAX_BODY_BYTES,
    EVENTS_PATH,
    RECALL_PATH,
    SESSIONS_PREFIX,
    SIGNED_KINDS,
    Headers,
    RouteKind,
)
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.errors import IngressRejectedError, StoreError
from molt.models.event import Event, EventCategory, JsonObject
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.recall import RecallEngine
from molt.redact import (
    REDACTION_DISABLED_RECORD,
    REDACTION_PLACEHOLDER,
    RedactionSettings,
)
from molt.store import Connection, MemoryStore
from molt.store.chain import LedgerAppend
from molt.store.embeddings import PrincipalScope
from molt.telemetry import Telemetry, configure, reset

# The two credentials, shaped like the values a deployment holds and obviously
# synthetic. Neither name carries a word the credential-shape lint inspects, so
# no call site below needs a suppression in order to pass a literal.
SHARED_VALUE: Final[str] = "an-ingress-shared-value"
BEARER_VALUE: Final[str] = "a-collector-bearer-value"

MACHINE: Final[str] = "machine-under-test"
TOOL: Final[str] = "claude_code"
TIMEOUT_MS: Final[int] = 1000
SESSION_UNDER_TEST: Final[UUID] = UUID("11111111-1111-4111-8111-111111111111")

OK: Final[int] = 200
UNAUTHORISED: Final[int] = 401
UNAVAILABLE: Final[int] = 503

# The bound the configuration surface declares, and the smallest step past it the
# presented form can carry, because the canonical timestamp renders microseconds.
MAX_AGE_SECONDS: Final[int] = 300
ONE_MICROSECOND: Final[timedelta] = timedelta(microseconds=1)

# A value in the timestamp header that names no instant at all.
UNREADABLE_TIMESTAMP: Final[str] = "the day before yesterday"

# A credential-shaped span built from a keyword and a long run of one character,
# so this module states no plausible secret while still exercising the Redactor.
SHAPED_TAIL: Final[str] = "q" * 32
SHAPED_SPAN: Final[str] = f"bearer {SHAPED_TAIL}"
REDACTED_SPAN: Final[str] = f"bearer {REDACTION_PLACEHOLDER}"

# The instant the recall Event under test is placed at, and the retention the
# tenancy carries. Both fixed, so no example depends on when it ran.
RECALL_MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RECALL_RETENTION: Final[timedelta] = timedelta(days=90)
RECALL_FLOOR: Final[float] = 0.15

# How many shaped values one payload carries in the flood example. Well above the
# one record the latency budget admits per Event, so a per-value emission would be
# unmistakable rather than a near miss.
FLOOD_KEYS: Final[int] = 64

# How long a hex digest of this size is, and a presented value of that length
# carrying characters a comparison over text does not admit.
DIGEST_HEX_LENGTH: Final[int] = 64
NON_ASCII_SIGNATURE: Final[str] = "\u00e9" * DIGEST_HEX_LENGTH

# The four bodies the digest is recomputed over. The last is not valid text at
# all, which is admissible precisely because nothing decodes what it verifies.
BODIES: Final[tuple[bytes, ...]] = (
    b"",
    b'{"a":1}\n{"a":2}\n',
    "\u00e4nother script \u4e2d\u6587\n".encode(),
    b"\xff\xfe\x00\n",
)
BODY_IDS: Final[tuple[str, ...]] = ("empty", "newlines", "outside-ascii", "undecodable")


class ManualClock(Protocol):
    """The readings this suite takes from the injected time source."""

    def now(self) -> datetime:
        """The current wall reading."""


def host_now() -> datetime:
    """The host's reading, which is what the handler's own path measures against.

    Used only by the tests that drive `handler.serve`, because that path takes no
    injected reading by design. Staleness is produced by offsetting a presented
    timestamp rather than by letting any time pass.
    """
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Doubles and builders
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefusedConnections:
    """A connection factory that refuses, counting how often it was asked.

    The count is the whole point: a request refused before the transaction opens
    leaves this at zero, and a request that reached the transaction leaves it
    above zero, so *nothing persisted* is asserted rather than assumed.
    """

    attempts: int = 0

    def open(self) -> Connection:
        """Record the attempt and refuse it."""
        self.attempts += 1
        raise StoreError("this suite reaches no cluster")


@dataclass(slots=True)
class Recorded:
    """The telemetry instance a test drives, together with what it wrote."""

    telemetry: Telemetry
    stream: io.StringIO

    @property
    def rejections(self) -> float:
        """How many rejections were counted under the undimensioned metric."""
        return self.telemetry.counters().get((SIGNATURE_REJECTED_METRIC, ()), 0.0)

    def causes(self) -> tuple[str, ...]:
        """The cause every rejection record named, in the order they were written."""
        named: list[str] = []
        for line in self.stream.getvalue().splitlines():
            record: object = json.loads(line)
            if isinstance(record, dict) and "cause" in record:
                named.append(str(record["cause"]))
        return tuple(named)

    def rendered(self) -> str:
        """Everything written, for the assertion that nothing sensitive appears."""
        return self.stream.getvalue()


@pytest.fixture
def recorded() -> Iterator[Recorded]:
    """A process-wide telemetry instance writing to a buffer this test reads."""
    stream = io.StringIO()
    telemetry = configure(Configuration(environ={}), stream=stream)
    yield Recorded(telemetry=telemetry, stream=stream)
    reset()


def build_collector(factory: RefusedConnections) -> Collector:
    """A Collector holding the shared value, with nothing injected at the seam.

    Nothing is attached to the signature seam, so the handler resolves the module
    under test by name and wraps it. That is what makes these tests an assertion
    about the seam closing rather than about a double.
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


def authorised() -> dict[str, str]:
    """The one header the bearer gate reads."""
    return {AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {BEARER_VALUE}"}


def signed(body: bytes, moment: datetime) -> dict[str, str]:
    """The bearer header and the two signature headers, computed for one request."""
    presented = ingress_timestamp(moment)
    return authorised() | {
        TIMESTAMP_HEADER: presented,
        SIGNATURE_HEADER: sign_ingress(body, SHARED_VALUE, presented),
    }


def presenting(presented: str, body: bytes) -> Headers:
    """The two headers a request presents, signed over the timestamp it presents."""
    return Headers(
        {
            TIMESTAMP_HEADER: presented,
            SIGNATURE_HEADER: sign_ingress(body, SHARED_VALUE, presented),
        }
    )


def invocation(method: str, path: str, headers: dict[str, str], body: bytes) -> Invocation:
    """One request as the transport would deliver it, carried as text."""
    return Invocation(
        method=method,
        path=path,
        headers=Headers(headers),
        body_text=body.decode("utf-8"),
        base64_encoded=False,
    )


def build_event(moment: datetime) -> Event:
    """One well-formed Event of the shape the capture side transmits."""
    return Event(
        id=uuid4(),
        session_id=SESSION_UNDER_TEST,
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=moment,
        agent_cli=TOOL,
        machine_id=MACHINE,
        parent_event_id=None,
        payload={"command": "ls"},
        redacted=False,
        text_body=None,
    )


def a_batch(moment: datetime, records: int = 3) -> bytes:
    """A batch body carrying several well-formed records.

    Several rather than one on purpose: a partial write would be possible if the
    verification ran anywhere later than it does, so the batches these tests refuse
    all carry a well-formed prefix.
    """
    return batch_body([build_event(moment) for _ in range(records)])


def independent_digest(presented: str, body: bytes) -> str:
    """The digest a holder of the shared value derives, built from the library here.

    Computed from the standard library rather than from either module, so the
    agreement between the signer and the verifier is asserted rather than
    restated.
    """
    return hmac.new(
        SHARED_VALUE.encode("utf-8"), signing_material(presented, body), sha256
    ).hexdigest()


def mutated_signature(headers: dict[str, str]) -> dict[str, str]:
    """The same headers with one hex character of the signature changed."""
    presented = headers[SIGNATURE_HEADER]
    swapped = "0" if presented[0] != "0" else "1"
    return headers | {SIGNATURE_HEADER: swapped + presented[1:]}


def without(headers: dict[str, str], name: str) -> dict[str, str]:
    """The same headers with one of them removed."""
    return {key: value for key, value in headers.items() if key != name}


# ---------------------------------------------------------------------------
# The seam, closed end to end
# ---------------------------------------------------------------------------


def test_the_call_the_handler_resolves_by_name_is_the_one_defined_here() -> None:
    """The handler loads the verification call by name, and this is that call.

    Compatibility is the claim. The handler passes the four leading arguments
    positionally, so the parameter carrying the shared value is free to be named
    for its role in the digest, and the component name is spelled independently on
    both sides and must be spelled the same.
    """
    loaded = _load_verification()

    assert loaded is verify_ingress
    assert verify_ingress.__module__ == INGRESS_MODULE
    assert COMPONENT == HANDLER_COMPONENT


def test_a_correctly_signed_in_window_request_reaches_the_transaction(
    recorded: Recorded,
) -> None:
    """With nothing injected, a signed batch gets past verification and is written.

    The store refuses its connection, so reaching the transaction is reported as
    unreachability. That is the accepted path: the request was verified, a
    connection was asked for, and the answer says the cluster could not be reached
    rather than that the caller was refused.
    """
    factory = RefusedConnections()
    collector = build_collector(factory)
    moment = host_now()
    body = a_batch(moment)

    answer = collector.serve(invocation("POST", EVENTS_PATH, signed(body, moment), body))

    assert answer.status == UNAVAILABLE
    assert factory.attempts > 0
    assert recorded.rejections == 0.0


def test_a_signed_session_metadata_request_reaches_the_transaction(
    recorded: Recorded,
) -> None:
    """The signature is required on both ingest endpoints, not only the batch one."""
    factory = RefusedConnections()
    collector = build_collector(factory)
    moment = host_now()
    body = json.dumps(
        {
            "id": str(SESSION_UNDER_TEST),
            "client_id": str(UNASSIGNED_CLIENT_ID),
            "agent_cli": TOOL,
            "machine_id": MACHINE,
            "started_at": ingress_timestamp(moment),
        }
    ).encode("utf-8")

    answer = collector.serve(
        invocation(
            "PUT",
            f"{SESSIONS_PREFIX}{SESSION_UNDER_TEST}",
            signed(body, moment),
            body,
        )
    )

    assert answer.status == UNAVAILABLE
    assert factory.attempts > 0
    assert recorded.rejections == 0.0


def test_an_unsigned_session_metadata_request_is_refused_before_the_body_is_read(
    recorded: Recorded,
) -> None:
    """Both signed routes require the headers, and the refusal precedes the decode."""
    factory = RefusedConnections()
    collector = build_collector(factory)

    answer = collector.serve(
        invocation(
            "PUT",
            f"{SESSIONS_PREFIX}{SESSION_UNDER_TEST}",
            authorised(),
            b"not a document",
        )
    )

    assert answer.status == UNAUTHORISED
    assert factory.attempts == 0
    assert recorded.causes() == (str(RejectionCause.TIMESTAMP_ABSENT),)


def test_the_recall_route_is_reached_by_a_caller_holding_no_shared_value(
    recorded: Recorded,
) -> None:
    """Recall stays bearer-only, so an interactive caller reaches it unsigned.

    The route table is the single statement of which routes are signed, so this
    asserts against it rather than restating the pair.
    """
    factory = RefusedConnections()
    collector = build_collector(factory)
    body = f'{{"query_text": "how did this fail", "session_id": "{SESSION_UNDER_TEST}"}}'.encode()

    answer = collector.serve(invocation("POST", RECALL_PATH, authorised(), body))

    assert answer.status == OK
    assert RouteKind.RECALL not in SIGNED_KINDS
    assert frozenset({RouteKind.EVENTS, RouteKind.SESSION}) == SIGNED_KINDS
    assert recorded.rejections == 0.0


# ---------------------------------------------------------------------------
# The four rejection causes, through the whole request path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alteration", "cause"),
    [
        ("mismatch", RejectionCause.MISMATCH),
        ("outside_ascii", RejectionCause.MISMATCH),
        ("stale", RejectionCause.OUTSIDE_WINDOW),
        ("no_timestamp", RejectionCause.TIMESTAMP_ABSENT),
        ("no_signature", RejectionCause.SIGNATURE_ABSENT),
    ],
)
def test_each_rejection_cause_is_401_with_nothing_persisted_and_one_measurement(
    alteration: str,
    cause: RejectionCause,
    recorded: Recorded,
) -> None:
    """The four causes are one answer to the caller and four causes to an operator.

    The batch carries a well-formed prefix, so the connection count is what rules
    out a partial write: nothing was asked for, so nothing could have landed.

    The mismatch cause is driven twice, because a presented value outside the ASCII
    range reaches the comparison the same way a wrong digest does and must leave the
    caller with the same 401. A comparison over text admits ASCII alone, so an
    arrangement comparing the two values as text would raise out through the
    verifier, past the boundary that turns a refusal into a status, and into the
    invocation itself, which is neither a refusal nor a measurement.
    """
    factory = RefusedConnections()
    collector = build_collector(factory)
    moment = host_now()
    body = a_batch(moment)
    if alteration == "stale":
        moment = moment - timedelta(seconds=MAX_AGE_SECONDS * 2)
    headers = signed(body, moment)
    if alteration == "mismatch":
        headers = mutated_signature(headers)
    elif alteration == "outside_ascii":
        headers = headers | {SIGNATURE_HEADER: NON_ASCII_SIGNATURE}
    elif alteration == "no_timestamp":
        headers = without(headers, TIMESTAMP_HEADER)
    elif alteration == "no_signature":
        headers = without(headers, SIGNATURE_HEADER)

    answer = collector.serve(invocation("POST", EVENTS_PATH, headers, body))

    assert answer.status == UNAUTHORISED
    assert answer.document == {"error": "unauthorised"}
    assert factory.attempts == 0
    assert recorded.rejections == 1.0
    assert recorded.causes() == (str(cause),)


def test_the_rejection_metric_stays_one_billable_combination(
    recorded: Recorded,
) -> None:
    """Four causes, one metric name, no dimension, so one combination is published.

    A per-cause dimension would spend four of the ten combinations the telemetry
    surface admits on one refusal counter. The cause sits in the record instead,
    which is not billed, and every cause is still distinguishable.
    """
    factory = RefusedConnections()
    collector = build_collector(factory)
    moment = host_now()
    body = a_batch(moment, records=1)
    headers = signed(body, moment)

    collector.serve(invocation("POST", EVENTS_PATH, mutated_signature(headers), body))
    collector.serve(invocation("POST", EVENTS_PATH, without(headers, TIMESTAMP_HEADER), body))
    collector.serve(invocation("POST", EVENTS_PATH, without(headers, SIGNATURE_HEADER), body))

    published = [name for name, _ in recorded.telemetry.combinations()]
    assert published.count(SIGNATURE_REJECTED_METRIC) == 1
    assert recorded.rejections == 3.0
    assert len(set(recorded.causes())) == 3


def test_a_rejection_discloses_neither_digest_nor_either_credential(
    recorded: Recorded,
) -> None:
    """The record names the cause and the arithmetic around it, and nothing else."""
    factory = RefusedConnections()
    collector = build_collector(factory)
    moment = host_now()
    body = a_batch(moment, records=1)
    headers = signed(body, moment)
    expected = headers[SIGNATURE_HEADER]

    collector.serve(invocation("POST", EVENTS_PATH, mutated_signature(headers), body))

    written = recorded.rendered()
    assert SHARED_VALUE not in written
    assert expected not in written
    assert BEARER_VALUE not in written


def test_an_absent_header_is_answered_before_the_body_is_looked_at(
    recorded: Recorded,
) -> None:
    """The two headers are read before any body handling (Requirement 47.3).

    The body here is not a batch at all. A path that read records before reading
    the headers would answer with a rejection count rather than with a refusal, so
    the status is what proves the order.
    """
    factory = RefusedConnections()
    collector = build_collector(factory)

    answer = collector.serve(
        invocation("POST", EVENTS_PATH, authorised(), b"not a record at all\n")
    )

    assert answer.status == UNAUTHORISED
    assert factory.attempts == 0
    assert recorded.causes() == (str(RejectionCause.TIMESTAMP_ABSENT),)


# ---------------------------------------------------------------------------
# The digest, and its agreement with the signer
# ---------------------------------------------------------------------------


def test_the_verifier_accepts_exactly_what_the_signer_produced(
    time_source: ManualClock,
) -> None:
    """One material, one algorithm, one comparison, computed on both sides.

    The independently built digest is what pins the pair: the signer's output, the
    verifier's acceptance, and a digest this module computed from the standard
    library all agree.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    presented = ingress_timestamp(reading)
    signature = sign_ingress(body, SHARED_VALUE, presented)

    verify_ingress(presenting(presented, body), body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading)

    assert signature == independent_digest(presented, body)
    assert len(signature) == DIGEST_HEX_LENGTH


@pytest.mark.parametrize("body", BODIES, ids=BODY_IDS)
def test_the_digest_is_recomputed_over_the_raw_bytes_whatever_they_are(
    body: bytes,
    time_source: ManualClock,
) -> None:
    """Nothing decodes what it verifies, so a body that is not text still verifies."""
    reading = time_source.now()
    presented = ingress_timestamp(reading)
    headers = Headers(
        {
            TIMESTAMP_HEADER: presented,
            SIGNATURE_HEADER: independent_digest(presented, body),
        }
    )

    verify_ingress(headers, body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading)


def test_the_headers_are_found_whatever_case_the_transport_used(
    time_source: ManualClock,
) -> None:
    """Case-insensitive lookup is the header mapping's own promise, relied on here."""
    reading = time_source.now()
    body = a_batch(reading, records=1)
    presented = ingress_timestamp(reading)
    headers = Headers(
        {
            TIMESTAMP_HEADER.lower(): presented,
            SIGNATURE_HEADER.upper(): sign_ingress(body, SHARED_VALUE, presented),
        }
    )

    verify_ingress(headers, body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading)


def test_the_surrounding_space_a_transport_leaves_is_tolerated(
    time_source: ManualClock,
) -> None:
    """A header value arrives with whatever whitespace the transport left on it."""
    reading = time_source.now()
    body = a_batch(reading, records=1)
    presented = ingress_timestamp(reading)
    signature = sign_ingress(body, SHARED_VALUE, presented)
    headers = Headers({TIMESTAMP_HEADER: presented, SIGNATURE_HEADER: f"  {signature}\n"})

    verify_ingress(headers, body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading)


@pytest.mark.parametrize(
    "presented",
    ["", "  ", "0" * DIGEST_HEX_LENGTH, NON_ASCII_SIGNATURE],
    ids=["empty", "blank", "wrong-digest", "outside-ascii"],
)
def test_a_presented_signature_that_is_not_the_computed_one_is_a_mismatch(
    presented: str,
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """An empty header value is a mismatch, and so is one outside the ASCII range.

    An empty value is not an absent header: the header arrived and carried nothing
    that could be the digest. A value outside the ASCII range is settled by the
    comparison itself, which encodes both sides before it compares them rather
    than comparing text, since a comparison over text admits ASCII alone and would
    raise on such a value instead of answering. It is settled as a mismatch,
    because a lowercase hex digest is ASCII by construction and a value that is
    not cannot be one.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    headers = Headers({TIMESTAMP_HEADER: ingress_timestamp(reading), SIGNATURE_HEADER: presented})

    with pytest.raises(IngressRejectedError):
        verify_ingress(headers, body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading)

    assert recorded.causes() == (str(RejectionCause.MISMATCH),)


def test_a_body_altered_after_signing_no_longer_verifies(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """The material covers the body, so the replay a signature exists to stop fails."""
    reading = time_source.now()
    body = a_batch(reading, records=1)
    headers = presenting(ingress_timestamp(reading), body)
    edited = bytearray(body)
    edited[0] = edited[0] ^ 0x01

    with pytest.raises(IngressRejectedError):
        verify_ingress(headers, bytes(edited), SHARED_VALUE, MAX_AGE_SECONDS, now=reading)

    assert recorded.causes() == (str(RejectionCause.MISMATCH),)


# ---------------------------------------------------------------------------
# The age bound
# ---------------------------------------------------------------------------


def test_the_configured_default_maximum_request_age_is_the_stated_one() -> None:
    """Requirement 47.6, read from the configuration surface rather than restated."""
    assert Configuration(environ={}).integer("MOLT_INGRESS_MAX_AGE_SECONDS") == MAX_AGE_SECONDS


def test_a_timestamp_exactly_at_the_bound_behind_the_reading_is_accepted(
    time_source: ManualClock,
) -> None:
    """The boundary is inclusive on the accepted side, measured backwards.

    Behind the reading is where a request's age lives, so this is the edge
    Requirements 47.5 and 47.6 are about: an age of exactly the configured maximum
    is accepted and anything staler is not.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    presented = ingress_timestamp(reading - timedelta(seconds=MAX_AGE_SECONDS))

    verify_ingress(presenting(presented, body), body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading)


def test_a_timestamp_at_the_skew_allowance_ahead_of_the_reading_is_accepted(
    time_source: ManualClock,
) -> None:
    """Ordinary clock disagreement between the two hosts is tolerated, and only that.

    The allowance exists because the timestamp is stamped on the capture host and
    the bound is measured on the Collector host. It is a named, bounded number
    rather than an open-ended future admission, which is the distinction the whole
    of the window's honesty rests on.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    presented = ingress_timestamp(reading + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_S))

    verify_ingress(presenting(presented, body), body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading)


def test_a_request_stamped_a_full_maximum_age_into_the_future_is_refused(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """The future timestamp is what doubled the window, so it is refused outright.

    Measured as an absolute difference, this request was admissible the instant it
    was made, stayed admissible while the reading climbed towards its timestamp, and
    was then admissible for the whole configured maximum age after the reading
    passed it: a replayable window of twice the maximum. Nothing about it is stale,
    which is exactly why an absolute difference cannot be what bounds it.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    presented = ingress_timestamp(reading + timedelta(seconds=MAX_AGE_SECONDS))

    with pytest.raises(IngressRejectedError):
        verify_ingress(
            presenting(presented, body), body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading
        )

    assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),)


def test_the_admitted_window_is_the_maximum_age_plus_the_skew_allowance(
    time_source: ManualClock,
) -> None:
    """One set of bytes, and the whole interval it is admissible over, at both edges.

    The same headers and the same body are judged against a moving reading, which is
    what a replay is. The earliest reading that admits them is the skew allowance
    before the presented instant and the latest is the configured maximum age after
    it, so the interval is that sum — 305 seconds at the default, not 600 — and a
    microsecond outside either edge is refused.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    headers = presenting(ingress_timestamp(reading), body)
    earliest = reading - timedelta(seconds=CLOCK_SKEW_ALLOWANCE_S)
    latest = reading + timedelta(seconds=MAX_AGE_SECONDS)

    verify_ingress(headers, body, SHARED_VALUE, MAX_AGE_SECONDS, now=earliest)
    verify_ingress(headers, body, SHARED_VALUE, MAX_AGE_SECONDS, now=latest)

    for outside in (earliest - ONE_MICROSECOND, latest + ONE_MICROSECOND):
        with pytest.raises(IngressRejectedError):
            verify_ingress(headers, body, SHARED_VALUE, MAX_AGE_SECONDS, now=outside)
    assert (latest - earliest).total_seconds() == MAX_AGE_SECONDS + CLOCK_SKEW_ALLOWANCE_S


@pytest.mark.parametrize("ahead", [False, True], ids=["behind", "ahead"])
def test_a_timestamp_one_microsecond_past_the_bound_is_refused_in_either_direction(
    ahead: bool,
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """One microsecond beyond the admitted window is outside it, on both sides.

    The two sides are different widths, and that asymmetry is the fix: the window
    reaches the configured maximum age behind the reading and the skew allowance
    ahead of it.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    width = CLOCK_SKEW_ALLOWANCE_S if ahead else MAX_AGE_SECONDS
    offset = timedelta(seconds=width) + ONE_MICROSECOND
    presented = ingress_timestamp(reading + offset if ahead else reading - offset)

    with pytest.raises(IngressRejectedError):
        verify_ingress(
            presenting(presented, body), body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading
        )

    assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),)


@pytest.mark.parametrize("presented", [UNREADABLE_TIMESTAMP, ""], ids=["not-an-instant", "empty"])
def test_a_present_but_unreadable_timestamp_is_refused_as_outside_the_window(
    presented: str,
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """It is neither the absent-header cause nor the mismatch cause, and why matters.

    The header arrived, so it is not an absence. The digest is taken over the
    timestamp as presented, so an unreadable value can carry a signature that
    matches it perfectly, and reporting a mismatch would report a comparison that
    did not happen. What it fails is the window: a request whose position on the
    timeline cannot be established has not been shown to fall inside it.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)

    with pytest.raises(IngressRejectedError):
        verify_ingress(
            presenting(presented, body), body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading
        )

    assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),)


def test_a_presented_timestamp_carrying_no_offset_is_refused_the_same_way(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """An instant with no offset would be read by each side in its own timezone.

    The presented value is rendered from an instant here rather than written out,
    so nothing about it is a literal this module states.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    presented = reading.replace(tzinfo=None).isoformat(timespec="microseconds")

    with pytest.raises(IngressRejectedError):
        verify_ingress(
            presenting(presented, body), body, SHARED_VALUE, MAX_AGE_SECONDS, now=reading
        )

    assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),)


def test_a_naive_reading_is_refused_rather_than_measured_against(
    time_source: ManualClock,
) -> None:
    """The reading the bound is measured against carries an offset for the same reason."""
    reading = time_source.now()
    body = a_batch(reading, records=1)
    headers = presenting(ingress_timestamp(reading), body)

    with pytest.raises(ValueError, match="reading"):
        verify_ingress(
            headers,
            body,
            SHARED_VALUE,
            MAX_AGE_SECONDS,
            now=reading.replace(tzinfo=None),
        )


def test_a_collector_holding_no_shared_value_refuses_rather_than_keying_with_nothing(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """A digest keyed with nothing is forgeable by anyone holding the body.

    The refusal is the ingress refusal rather than a fault, because the caller's
    answer is the same 401 either way while a fault would fail the invocation. It is
    counted and recorded like every other refusal (Requirement 47.13): a deployment
    that can verify nobody would otherwise refuse every caller in complete silence
    and look exactly like a deployment nobody is calling.

    The cause reads as the mismatch cause because that is what was established — no
    presented signature was shown to be the computed one — and no fifth member is
    invented for it, since Requirement 47.13 enumerates four causes. The detail in
    the same record is what names the deployment fault, so an operator is pointed at
    the deployment rather than at the caller.
    """
    reading = time_source.now()
    body = a_batch(reading, records=1)
    headers = presenting(ingress_timestamp(reading), body)

    with pytest.raises(IngressRejectedError, match="no ingress shared value"):
        verify_ingress(headers, body, "", MAX_AGE_SECONDS, now=reading)

    assert recorded.rejections == 1.0
    assert recorded.causes() == (str(RejectionCause.MISMATCH),)
    assert "no ingress shared value" in recorded.rendered()


# ---------------------------------------------------------------------------
# The recall Ledger append, and the query text it carries
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Appended:
    """The Ledger appends the chain seam was handed, captured rather than written.

    The seam is replaced rather than the store, because the claim is about what the
    Event carries and not about how a row is written. A store nothing reaches is
    enough underneath it.
    """

    requests: list[LedgerAppend] = field(default_factory=list)

    def take(self, _store: MemoryStore, request: LedgerAppend) -> None:
        """Keep one append, as the chain would have written one."""
        self.requests.append(request)


class NoEmbeddings:
    """A provider that is never asked, because no query is answered here."""

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Refuse the call, naming how many texts were asked about."""
        raise AssertionError(f"no vector should be needed for {len(texts)} text(s)")


RECALL_SCOPE: Final[PrincipalScope] = PrincipalScope(
    client_id=UNASSIGNED_CLIENT_ID,
    agent_cli=TOOL,
    machine_id=MACHINE,
    retention=RECALL_RETENTION,
)


def build_recall_engine(*, disabled: bool = False) -> RecallEngine:
    """A recall engine whose clock is stated and whose store is never reached."""
    store = MemoryStore(
        connect_with=RefusedConnections().open,
        statement_timeout_ms=TIMEOUT_MS,
    )
    return RecallEngine(
        store,
        NoEmbeddings(),
        recall_floor=RECALL_FLOOR,
        clock=lambda: RECALL_MOMENT,
        redaction=RedactionSettings(disabled=disabled),
    )


def recall_event_for(
    engine: RecallEngine,
    appended: Appended,
    query_text: str,
) -> Event:
    """The one Event the recall settlement appended for one query text."""
    engine._append_recall_event(query_text, (), 1, 0, RECALL_SCOPE, SESSION_UNDER_TEST)
    assert len(appended.requests) == 1
    return appended.requests[0].event


def test_the_recall_event_carries_the_query_text_redacted_and_says_it_redacted_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 4.1: the query text goes through the Redactor before the write.

    The query text is what a person typed, so it is the one place in this system a
    secret arrives as ordinary prose. The Ledger is append-only and no role holds
    `UPDATE`, so the write is the last moment redaction can happen at all: text that
    lands here leaves only by an erasure or by expiry.
    """
    appended = Appended()
    monkeypatch.setattr(recall, "append", appended.take)

    event = recall_event_for(build_recall_engine(), appended, f"why did {SHAPED_SPAN} fail")

    assert event.payload["query_text"] == f"why did {REDACTED_SPAN} fail"
    assert SHAPED_TAIL not in json.dumps(event.payload)
    assert event.redacted is True


def test_a_query_text_the_redactor_left_alone_is_not_flagged_as_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag reports what the Redactor did, so it is neither hardcoded value.

    Paired with the example above rather than standing alone: one query is changed
    and flagged, one is unchanged and unflagged, and no single hardcoded flag
    satisfies both.
    """
    appended = Appended()
    monkeypatch.setattr(recall, "append", appended.take)
    plain = "how did the migration step fail last time"

    event = recall_event_for(build_recall_engine(), appended, plain)

    assert event.payload["query_text"] == plain
    assert event.redacted is False


def test_the_recall_path_records_that_redaction_was_disabled_and_names_the_session(
    monkeypatch: pytest.MonkeyPatch,
    recorded: Recorded,
) -> None:
    """Requirement 4.6, on the path that writes a query text into the Ledger.

    With redaction off the text passes through and the flag stays false, which is
    honest. What must not also happen is silence: the Redactor reaches no telemetry
    surface of its own, so the record naming the Session is written here.
    """
    appended = Appended()
    monkeypatch.setattr(recall, "append", appended.take)

    event = recall_event_for(
        build_recall_engine(disabled=True), appended, f"why did {SHAPED_SPAN} fail"
    )

    assert event.payload["query_text"] == f"why did {SHAPED_SPAN} fail"
    assert event.redacted is False
    written = recorded.rendered()
    assert REDACTION_DISABLED_RECORD in written
    assert str(SESSION_UNDER_TEST) in written


# ---------------------------------------------------------------------------
# The adapter Event, and the record redaction being off owes
# ---------------------------------------------------------------------------


def build_capture_context(*, disabled: bool) -> CaptureContext:
    """The context the five adapters build Events against, with redaction stated."""
    return CaptureContext(
        session_id=SESSION_UNDER_TEST,
        client=UNASSIGNED_CLIENT,
        machine_id=MACHINE,
        agent_cli=TOOL,
        clock=SystemClock(),
        redaction=RedactionSettings(disabled=disabled),
    )


def test_an_adapter_event_built_with_redaction_on_is_redacted_and_owes_no_record(
    recorded: Recorded,
) -> None:
    """The ordinary path: the payload and the text body are both cleaned, silently."""
    ctx = build_capture_context(disabled=False)

    built = builders.event(
        ctx,
        EventCategory.TOOL_CALL,
        {"command": f"deploy --header {SHAPED_SPAN}"},
        text_body=f"the response carried {SHAPED_SPAN}",
    )

    assert SHAPED_TAIL not in json.dumps(built.payload)
    assert built.text_body is not None
    assert SHAPED_TAIL not in built.text_body
    assert built.redacted is True
    assert REDACTION_DISABLED_RECORD not in recorded.rendered()


def test_an_adapter_event_built_with_redaction_off_records_that_it_was_off(
    recorded: Recorded,
) -> None:
    """Requirement 4.6 on the primary capture path, for all five adapters at once.

    Every Event any adapter builds passes through this one constructor, so the
    record is owed here or nowhere. Before, the Redactor's warning was returned and
    dropped, and five adapters transmitted unredacted payloads and text bodies with
    nothing anywhere saying redaction had been switched off.
    """
    ctx = build_capture_context(disabled=True)

    built = builders.event(
        ctx,
        EventCategory.TOOL_CALL,
        {"command": f"deploy --header {SHAPED_SPAN}"},
        text_body=f"the response carried {SHAPED_SPAN}",
    )

    assert SHAPED_TAIL in json.dumps(built.payload)
    assert built.redacted is False
    written = recorded.rendered()
    assert REDACTION_DISABLED_RECORD in written
    assert str(SESSION_UNDER_TEST) in written


def test_one_record_is_written_per_event_however_many_values_the_payload_holds(
    recorded: Recorded,
) -> None:
    """Requirement 1.8: once per Event, not once per value the Redactor would replace.

    A hook payload can hold a whole file. A record per redactable value would turn a
    large payload into a flood of identical records on a path with a latency budget,
    so the emission sits where the payload is redacted rather than inside the walk.
    """
    ctx = build_capture_context(disabled=True)
    payload: JsonObject = {
        f"header_{index}": f"{SHAPED_SPAN}-{index}" for index in range(FLOOD_KEYS)
    }

    builders.event(ctx, EventCategory.TOOL_CALL, payload, text_body=SHAPED_SPAN)

    assert recorded.rendered().count(REDACTION_DISABLED_RECORD) == 1


def test_the_record_cannot_become_the_way_a_hook_fails_its_host_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 1.7: a telemetry surface that refuses does not reach the adapter.

    The warning exists to describe a degraded state. If reaching for it could raise,
    the warning would itself become a failure mode of the capture hook, which is the
    one thing the hook is never allowed to have.
    """
    ctx = build_capture_context(disabled=True)

    def refuse(*_args: object, **_fields: object) -> None:
        raise RuntimeError("the telemetry surface could not be reached")

    monkeypatch.setattr("molt.telemetry.log", refuse)

    built = builders.event(ctx, EventCategory.TOOL_CALL, {"command": SHAPED_SPAN})

    assert built.session_id == SESSION_UNDER_TEST
    assert built.redacted is False
