"""Signed ingress: how long a captured request stays replayable, and no longer.

A bearer token authenticates a caller and resists no replay. A holder of a
captured request body carrying a valid bearer value can re-send those bytes
indefinitely, and every replay writes Ledger rows indistinguishable from the
originals. The signature together with the age bound is what closes that window
to the configured maximum request age (Requirement 47.14). The bound runs
backwards from the presented instant rather than around it: a stamp behind the
reading is admitted out to the configured maximum age, and a stamp ahead of it
only as far as `CLOCK_SKEW_ALLOWANCE_S`, so stamping a capture into the future
buys no second window. This module is the demonstration that the window is
closed. That is a different claim from the one the unit suite makes: there, the
bound is shown to be computed correctly at the microsecond in both directions;
here, one request is captured as it went over the wire, presented once and
accepted, then presented again byte for byte after the window has passed and
refused.

Four arrangements carry the claims.

**The whole request path is driven, and only the reading is injected.** Every
narrative below goes through `Collector.serve`, so the route match, the bearer
gate, the request bound, the transport decode, the verification, and the
transaction all run in their deployed order; the final measurement of the
window's width calls the verification alone, because what it measures is the
window and a leased connection would tell it nothing further. The
verification attached at the handler's seam is the ingress module's own call,
wrapped so the age bound is measured against the manual clock instead of the
host's. That wrapper is the only substitution: the material, the digest, the
bound, the cause, and the status are all the real ones. It is needed because the
handler's own path takes the host's reading by design, and the only other way to
make a captured request stale there is to wait out the configured maximum age in
real seconds.

**The replay is the same bytes.** The captured request is held once and
re-presented from that holding, and each presentation is checked against the
previous one for the same body and the same header mapping, with the digest
asserted to be the very object computed at capture. A replay that had to be
re-signed is not a replay, so nothing below re-signs anything.

**What refuses the replay is its age and not its digest.** At the point of
refusal the presented signature is still the correct one over the presented
timestamp and the presented body, which is shown by recomputing it here from the
standard library, and the same bytes at the same reading are accepted once the
bound alone is widened. That is the distinction the requirement rests on: a
bearer-only design has nothing to refuse this request with, and the bearer value
the replay carries is demonstrated below to be still good.

**Nothing persisted is witnessed rather than assumed.** The store is given a
connection factory that refuses and counts, so an accepted request leaves the
count above zero and a refused one leaves it untouched. Nothing here reaches a
cluster and nothing here waits on a clock.

**Validates: Requirements 47.5, 47.6, 47.14**
"""

from __future__ import annotations

import hmac
import io
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt.capture.hook import batch_body
from molt.capture.signing import (
    AUTHORIZATION_HEADER,
    BEARER_SCHEME,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ingress_timestamp,
    sign_ingress,
    signing_material,
)
from molt.collector.handler import Collector, Invocation
from molt.collector.ingress import (
    CLOCK_SKEW_ALLOWANCE_S,
    SIGNATURE_REJECTED_METRIC,
    RejectionCause,
    verify_ingress,
)
from molt.collector.routes import (
    DEFAULT_MAX_BODY_BYTES,
    EVENTS_PATH,
    RECALL_PATH,
    Headers,
)
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.errors import IngressRejectedError, StoreError
from molt.models.event import Event, EventCategory
from molt.models.session import UNASSIGNED_CLIENT_ID
from molt.store import Connection, MemoryStore
from molt.telemetry import Telemetry, configure, reset

# The two credentials, shaped like the values a deployment holds and obviously
# synthetic. Neither name carries a word the credential-shape lint inspects.
SHARED_VALUE: Final[str] = "an-ingress-shared-value"
BEARER_VALUE: Final[str] = "a-collector-bearer-value"

MACHINE: Final[str] = "machine-under-test"
TOOL: Final[str] = "claude_code"
TIMEOUT_MS: Final[int] = 1000
SESSION_UNDER_TEST: Final[UUID] = UUID("11111111-1111-4111-8111-111111111111")

OK: Final[int] = 200
UNAUTHORISED: Final[int] = 401
UNAVAILABLE: Final[int] = 503

# The deployment this suite serves as, and the bound it declares. The bound is
# read off the configuration surface rather than restated, so the window measured
# below is the configured one and this module states no number of its own.
DEPLOYED: Final[Configuration] = Configuration(
    environ={
        "MOLT_COLLECTOR_MAX_BODY_BYTES": str(DEFAULT_MAX_BODY_BYTES),
        "MOLT_DB_STATEMENT_TIMEOUT_MS": str(TIMEOUT_MS),
    }
)
MAX_AGE_SECONDS: Final[int] = DEPLOYED.integer("MOLT_INGRESS_MAX_AGE_SECONDS")

# The smallest step past the bound the presented form can carry, because the
# canonical timestamp renders microseconds. Held as a count of seconds because
# that is what the clock is advanced by.
ONE_MICROSECOND: Final[float] = 1e-6

# The step the window's width is measured at, and the span it is measured over.
# The span reaches twice the bound so a request that stayed replayable for longer
# than the bound would be caught by the scan rather than assumed away.
SCAN_STEP_SECONDS: Final[int] = 1
SCAN_SPAN_SECONDS: Final[int] = MAX_AGE_SECONDS * 2

# One whole second, the coarse step the future-stamped sequence stands outside an
# edge by before closing on it at the microsecond.
ONE_SECOND: Final[int] = 1

# How far the reading is walked to bring a capture stamped twice the bound ahead
# to one whole second outside the skew allowance ahead of it, having already been
# walked one configured maximum age. Derived from the bound and the allowance, so
# this module still states no width of its own.
APPROACH_SECONDS: Final[int] = (
    SCAN_SPAN_SECONDS - MAX_AGE_SECONDS - CLOCK_SKEW_ALLOWANCE_S - ONE_SECOND
)

# How many refusals the future-stamped sequence collects: the presentation made
# while the stamp is twice the bound ahead, the one made a whole configured
# maximum age ahead, the one a whole second outside the allowance, the one a
# microsecond outside it, and the one a microsecond past the far edge behind.
REFUSALS_STAMPED_AHEAD: Final[int] = 5


class ManualClock(Protocol):
    """The two operations this suite performs on the injected time source."""

    def now(self) -> datetime:
        """The current wall reading, carrying an offset."""

    def advance(self, seconds: float) -> None:
        """Move the reading forward, standing in for that much waiting."""


# ---------------------------------------------------------------------------
# Doubles and builders
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefusedConnections:
    """A connection factory that refuses, counting how often it was asked.

    The count is how *nothing persisted* is witnessed. A request refused before
    the transaction opens leaves it untouched; a request that got as far as the
    transaction raises it, so the two are told apart without a cluster.
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


@pytest.fixture
def recorded() -> Iterator[Recorded]:
    """A process-wide telemetry instance writing to a buffer this test reads."""
    stream = io.StringIO()
    telemetry = configure(Configuration(environ={}), stream=stream)
    yield Recorded(telemetry=telemetry, stream=stream)
    reset()


def build_collector(factory: RefusedConnections, clock: ManualClock) -> Collector:
    """A Collector whose verification is the real one, measured against the clock.

    The wrapper attached at the seam calls the ingress module's own verification
    with the reading the module provides that parameter for. Nothing else about
    the path is substituted, so what accepts and refuses below is the deployed
    arrangement and the only thing a test controls is what time it is.
    """
    store = MemoryStore(connect_with=factory.open, statement_timeout_ms=TIMEOUT_MS)

    def verify(headers: Mapping[str, str], body: bytes) -> None:
        verify_ingress(headers, body, SHARED_VALUE, MAX_AGE_SECONDS, now=clock.now())

    return Collector(
        configuration=DEPLOYED,
        store=store,
        bearer=Credential(
            BEARER_VALUE,
            source_name="MOLT_COLLECTOR_TOKEN",
            source=CredentialSource.ENVIRONMENT,
        ),
        ingress=verify,
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


@dataclass(frozen=True, slots=True)
class CapturedRequest:
    """One ingest request exactly as it went over the wire, held for replaying.

    Both fields are read-only and nothing below rebuilds either of them: every
    presentation is made from this one holding, which is what makes a replay a
    replay rather than a fresh request that happens to look like one.
    """

    body: bytes
    headers: dict[str, str]

    @property
    def signature(self) -> str:
        """The digest computed at capture, which no replay recomputes."""
        return self.headers[SIGNATURE_HEADER]

    @property
    def timestamp(self) -> str:
        """The timestamp presented at capture, which the digest covers."""
        return self.headers[TIMESTAMP_HEADER]

    def presented(self, path: str = EVENTS_PATH, method: str = "POST") -> Invocation:
        """The request as the transport would deliver it, from the held bytes."""
        return Invocation(
            method=method,
            path=path,
            headers=Headers(self.headers),
            body_text=self.body.decode("utf-8"),
            base64_encoded=False,
        )


def capture(
    clock: ManualClock,
    *,
    stamped_ahead_by: timedelta | None = None,
    records: int = 3,
) -> CapturedRequest:
    """Capture a signed batch request the way the capture side produces one.

    The body is real Event wire records, the timestamp is the canonical rendering
    of an instant, and the digest is the signer's own over both. Several records
    rather than one on purpose: a batch carrying a well-formed prefix is what
    makes the connection count meaningful, since a partial write would be
    possible if verification ran any later than it does.

    A caller may stamp the request ahead of the reading, which is what a caller
    whose clock runs fast presents and what an attacker stamping into the future
    presents. Only `CLOCK_SKEW_ALLOWANCE_S` of that is admitted, so a stamp
    further ahead than the allowance is refused when it is made and stays refused
    until the reading has climbed to within the allowance of it.
    """
    moment = clock.now()
    stamped = moment if stamped_ahead_by is None else moment + stamped_ahead_by
    body = batch_body([build_event(moment) for _ in range(records)])
    presented = ingress_timestamp(stamped)
    return CapturedRequest(
        body=body,
        headers={
            AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {BEARER_VALUE}",
            TIMESTAMP_HEADER: presented,
            SIGNATURE_HEADER: sign_ingress(body, SHARED_VALUE, presented),
        },
    )


def same_request(first: Invocation, second: Invocation) -> bool:
    """Whether two presentations carry the same body and the same headers.

    The header mapping is compared as a plain mapping because the request's own
    header type looks names up without regard to case and defines no equality of
    its own.
    """
    return first.body_text == second.body_text and dict(first.headers) == dict(second.headers)


def independent_digest(presented: str, body: bytes) -> str:
    """The digest a holder of the shared value derives, built from the library here.

    Computed from the standard library rather than from either side of the
    boundary, so *the signature is still valid* is established rather than
    restated.
    """
    return hmac.new(
        SHARED_VALUE.encode("utf-8"), signing_material(presented, body), sha256
    ).hexdigest()


def accepted_by(
    collector: Collector,
    captured: CapturedRequest,
    factory: RefusedConnections,
) -> bool:
    """Whether one presentation got past verification and reached the transaction.

    Acceptance is reported as unreachability, because the store refuses its
    connection: the request was verified, a connection was asked for, and the
    answer says the cluster could not be reached rather than that the caller was
    refused. A refusal is the 401 with the constant body and no connection asked
    for at all.
    """
    before = factory.attempts
    answer = collector.serve(captured.presented())
    if answer.status == UNAVAILABLE:
        assert factory.attempts > before
        return True
    assert answer.status == UNAUTHORISED
    assert answer.document == {"error": "unauthorised"}
    assert factory.attempts == before
    return False


# ---------------------------------------------------------------------------
# The narrative: one capture, one acceptance, one refused replay
# ---------------------------------------------------------------------------


def test_a_captured_request_is_accepted_once_and_refused_once_the_window_has_passed(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """The same bytes, accepted inside the window and refused outside it.

    This is the whole of Requirement 47.14 in one sequence. The second
    presentation is the first one's bytes and headers, unaltered and unre-signed,
    and it is refused under the cause that owns the window while the store is
    never asked for a connection.
    """
    factory = RefusedConnections()
    collector = build_collector(factory, time_source)
    captured = capture(time_source)
    digest_at_capture = captured.signature

    first = captured.presented()
    inside = collector.serve(first)

    assert inside.status == UNAVAILABLE
    reached = factory.attempts
    assert reached > 0
    assert recorded.rejections == 0.0

    # Long after the window closed, with nothing waited for.
    time_source.advance(SCAN_SPAN_SECONDS)
    second = captured.presented()
    outside = collector.serve(second)

    assert outside.status == UNAUTHORISED
    assert outside.document == {"error": "unauthorised"}
    assert factory.attempts == reached
    assert recorded.rejections == 1.0
    assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),)
    # The replay is the capture. Same body, same headers, and the digest is the
    # object computed at capture rather than one recomputed for the replay.
    assert same_request(first, second)
    assert second.headers[SIGNATURE_HEADER] is digest_at_capture


def test_the_replayed_request_is_refused_by_its_age_and_not_by_its_digest(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """At the point of refusal the signature is still the correct one.

    Two things establish it. The digest is recomputed here from the standard
    library over the presented timestamp and the presented body and is the
    presented value. And the identical bytes at the identical reading are
    accepted once the bound alone is widened to cover the elapsed span, so the
    only thing that refused the replay was its age. A design authenticating by
    bearer token alone has neither of those to refuse it with.
    """
    factory = RefusedConnections()
    collector = build_collector(factory, time_source)
    captured = capture(time_source)
    elapsed = SCAN_SPAN_SECONDS

    time_source.advance(elapsed)
    refused = collector.serve(captured.presented())

    assert refused.status == UNAUTHORISED
    assert factory.attempts == 0
    assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),)
    assert captured.signature == independent_digest(captured.timestamp, captured.body)
    # Same headers, same body, same reading; only the bound is widened.
    verify_ingress(
        Headers(captured.headers),
        captured.body,
        SHARED_VALUE,
        elapsed,
        now=time_source.now(),
    )
    assert recorded.rejections == 1.0


def test_the_bearer_value_the_refused_replay_carries_is_still_good(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """The credential still authenticates; it is the age that refuses the request.

    The replay's own authorisation header is presented, unchanged, to the route
    the deployment authenticates by bearer token alone, and that route answers.
    So the caller is who it says it is and the bytes are refused anyway, which is
    exactly the window a bearer-only design leaves open for as long as the
    capture is kept.
    """
    factory = RefusedConnections()
    collector = build_collector(factory, time_source)
    captured = capture(time_source)

    time_source.advance(SCAN_SPAN_SECONDS)
    replay = collector.serve(captured.presented())

    assert replay.status == UNAUTHORISED
    assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),)

    query = json.dumps(
        {"query_text": "how did this fail", "session_id": str(SESSION_UNDER_TEST)}
    ).encode("utf-8")
    bearer_only = collector.serve(
        Invocation(
            method="POST",
            path=RECALL_PATH,
            headers=Headers({AUTHORIZATION_HEADER: captured.headers[AUTHORIZATION_HEADER]}),
            body_text=query.decode("utf-8"),
            base64_encoded=False,
        )
    )

    assert bearer_only.status == OK
    assert factory.attempts == 0
    assert recorded.rejections == 1.0


# ---------------------------------------------------------------------------
# The edge of the window, and its other side
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("beyond", "still_replayable"),
    [(0.0, True), (ONE_MICROSECOND, False)],
    ids=["at-the-bound", "one-step-beyond"],
)
def test_a_replay_is_accepted_at_the_bound_and_refused_one_step_beyond_it(
    beyond: float,
    still_replayable: bool,
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """The window is closed at the smallest step the presented form can carry.

    The clock is advanced to exactly the bound and to one microsecond past it,
    and the same capture is replayed at each. The bound being inclusive is what
    lets a request in flight for the whole permitted age still land.
    """
    factory = RefusedConnections()
    collector = build_collector(factory, time_source)
    captured = capture(time_source)

    time_source.advance(MAX_AGE_SECONDS)
    time_source.advance(beyond)

    assert accepted_by(collector, captured, factory) is still_replayable
    if still_replayable:
        assert recorded.rejections == 0.0
    else:
        assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),)


def test_a_request_stamped_ahead_of_the_reading_is_refused_until_the_reading_reaches_it(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """A capture stamped into the future is refused, and stays refused.

    The request is stamped twice the bound ahead of the reading and the identical
    bytes are re-presented as the reading climbs towards the stamp. While the
    stamp is further ahead than the skew allowance nothing has placed the request
    inside the window, so every presentation is refused: the one made when the
    stamp is twice the bound ahead, the one made when the difference has fallen to
    exactly the configured maximum age, the one a whole second outside the
    allowance, and the one a microsecond outside it. The first acceptance comes
    when the reading is within the allowance of the stamp, and from there the
    ordinary backwards window runs, so the bytes stay usable out to the maximum
    age past the stamp and are refused a microsecond later.

    The second presentation is the one carrying the claim. A bound taken as an
    absolute difference admits a stamp exactly the configured maximum age ahead,
    and would go on admitting these bytes for the whole maximum age after the
    reading passed the stamp, making the span a capture is replayable in twice the
    configured maximum instead of the maximum plus the allowance. This case fails
    if that difference ever comes back.
    """
    factory = RefusedConnections()
    collector = build_collector(factory, time_source)
    captured = capture(time_source, stamped_ahead_by=timedelta(seconds=SCAN_SPAN_SECONDS))

    # Stamped twice the bound ahead: nothing places it inside the window.
    assert accepted_by(collector, captured, factory) is False

    # A whole configured maximum age of climbing later the stamp is still that
    # much ahead, which is the presentation the absolute difference admitted.
    time_source.advance(MAX_AGE_SECONDS)
    assert accepted_by(collector, captured, factory) is False

    # A whole second outside the allowance, then a microsecond outside it.
    time_source.advance(APPROACH_SECONDS)
    assert accepted_by(collector, captured, factory) is False
    time_source.advance(ONE_SECOND - ONE_MICROSECOND)
    assert accepted_by(collector, captured, factory) is False
    assert factory.attempts == 0

    # The reading is within the allowance of the stamp: the held bytes are
    # admitted for the first time, unaltered and unre-signed.
    time_source.advance(ONE_MICROSECOND)
    assert accepted_by(collector, captured, factory) is True
    reached = factory.attempts

    # From the stamp the window runs backwards, so the maximum age past it is the
    # last reading these bytes are admitted at.
    time_source.advance(CLOCK_SKEW_ALLOWANCE_S + MAX_AGE_SECONDS)
    assert accepted_by(collector, captured, factory) is True
    assert factory.attempts > reached
    reached = factory.attempts

    time_source.advance(ONE_MICROSECOND)
    assert accepted_by(collector, captured, factory) is False
    assert factory.attempts == reached
    assert recorded.causes() == (str(RejectionCause.OUTSIDE_WINDOW),) * REFUSALS_STAMPED_AHEAD


# ---------------------------------------------------------------------------
# The width of the window
# ---------------------------------------------------------------------------


def test_the_window_a_capture_stays_replayable_in_is_the_configured_width(
    time_source: ManualClock,
    recorded: Recorded,
) -> None:
    """The seconds a captured request stays replayable are the configured maximum.

    This is the security property rather than the arithmetic one: not merely that
    a stale request is refused, but that the span over which a capture remains
    usable is bounded by the configured age and nothing more. The reading is
    walked forward a second at a time over twice the bound and every offset the
    same capture is still accepted at is recorded, so a window wider than the
    configured age would show up as an offset in the record rather than have to be
    anticipated. Ahead of the stamp the interval reaches no further than the named
    skew allowance, which the future-stamped sequence above exhibits directly, so
    the whole span measured from the stamp is the configured age plus that
    allowance rather than twice the configured age.

    Verification is called here rather than the whole request path, because the
    measurement is of the window and the sequences above already establish what
    an acceptance and a refusal amount to at the boundary the caller sees. It
    leases no connection and opens no transaction, so no presentation in the scan
    could persist anything whatever its answer.
    """
    captured = capture(time_source, records=1)
    headers = Headers(captured.headers)

    replayable: list[int] = []
    refused: list[int] = []
    for offset in range(0, SCAN_SPAN_SECONDS + SCAN_STEP_SECONDS, SCAN_STEP_SECONDS):
        try:
            verify_ingress(
                headers,
                captured.body,
                SHARED_VALUE,
                MAX_AGE_SECONDS,
                now=time_source.now(),
            )
        except IngressRejectedError:
            refused.append(offset)
        else:
            replayable.append(offset)
        time_source.advance(SCAN_STEP_SECONDS)

    assert replayable == list(range(0, MAX_AGE_SECONDS + SCAN_STEP_SECONDS, SCAN_STEP_SECONDS))
    assert max(replayable) - min(replayable) == MAX_AGE_SECONDS
    assert min(refused) == MAX_AGE_SECONDS + SCAN_STEP_SECONDS
    assert recorded.rejections == float(len(refused))
