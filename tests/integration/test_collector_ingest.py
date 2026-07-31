"""The Collector against a live cluster: what a transaction did, and what it did not.

The unit module asserts every decision the request path makes before a row could
be written, against a store that refuses every connection. The property modules
assert the batch reading, the request bound, and the signature over generated
input. None of the three can answer the question this module exists for: after the
request, what does the database hold. So every assertion here is a row count or a
stored column, read on this module's own connection with a parameterised
statement rather than through the surface the Collector writes with. Reading the
rows back through the same functions that wrote them would let one shared mistake
satisfy both sides; a direct count cannot.

Five claims arrange the module.

**An absent Session is created inside the transaction that writes its Events, and
the proof is the failing half.** A batch naming a Session no row holds is posted,
and the Session row and its Ledger rows are counted afterwards. That half alone
would pass just as well against two separate transactions, so it is only the
setup. The claim is made by the second half: a batch whose final record names a
parent Event no Ledger row holds. The Session insert is the transaction's first
statement and succeeds, the earlier appends succeed after it, and then the last
append is refused on the self-reference the Ledger carries over its parent column.
If the Session were written in a transaction of its own it would survive that
refusal, and it does not survive it, which is the only observation that
distinguishes one transaction from two (Requirement 5.7).

**Nothing persisted is measured rather than inferred.** Every refusal case counts
the Session rows and the Ledger rows of the whole schema before the request and
again after it, and counts the rows naming the batch's own Session as well. A
partial write of a batch carrying several well-formed records would move the first
pair; a write of the Session without its Events would move it too. Both are
therefore excluded by observation rather than by reading the request path and
reasoning about where its checks sit (Requirements 5.4, 5.11, 47.4, 47.7, 47.8).

**All four counted signature rejection causes are driven, and the set is asserted
to be all of them.** The verifier is the real one, loaded by the handler through
its own module lookup rather than injected, so what is exercised is the deployed
arrangement. The parameterisation is checked against the cause enumeration itself,
so a fifth cause added later fails this module rather than passing unnoticed.

**Unreachability is produced without touching the shared instance.** The 503 case
is a second Collector over a second store whose connection factory addresses a
port nothing listens on. No process is stopped and no database is altered, which
matters because the instance is shared. The live schema is counted afterwards as
well, so a request answered as unreachable is shown to have reached nothing
(Requirement 5.9).

**The health route is asserted in the branch only a cluster can produce.** The
unit module can reach the unreachable branch alone; here the cluster answers, so
the reachable reading is asserted, and the document is searched for
content-bearing field names at every depth of its nesting rather than at the top
level (Requirement 5.3).

The recall route closes the module: the same Collector that refuses an unsigned
batch answers an unsigned recall query, because recall is authenticated by the
bearer token alone (Requirement 47.12).

**Validates: Requirements 5.3, 5.4, 5.7, 5.9, 5.11, 47.4, 47.7, 47.8, 47.12, 36.2**
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest

from molt.capture.hook import batch_body
from molt.capture.signing import (
    AUTHORIZATION_HEADER,
    BEARER_SCHEME,
    COLLECTOR_BEARER_ENV,
    INGRESS_KEY_ENV,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ingress_timestamp,
    sign_ingress,
)
from molt.collector.handler import (
    LIVE_STATUS,
    REACHABLE,
    WRITE_FAILURE_METRIC,
    Collector,
    Invocation,
)
from molt.collector.ingress import SIGNATURE_REJECTED_METRIC, RejectionCause
from molt.collector.routes import (
    DEFAULT_MAX_BODY_BYTES,
    EVENTS_PATH,
    HEALTH_PATH,
    RECALL_PATH,
    Headers,
    RecallAnswer,
    RecallQuery,
    RejectionReason,
)
from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.models.event import Event, EventCategory, JsonValue
from molt.models.session import UNASSIGNED_CLIENT_ID, SessionOutcome
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations
from molt.telemetry import CONTENT_KEYS, current, reset

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The two credential values this module presents, and the two it presents in their
# place to be refused. Both are obviously synthetic: they name what they are for
# rather than carrying anything shaped like a deployed value.
BEARER_VALUE: Final[str] = "a-bearer-value-the-ingest-suite-expects"
OTHER_BEARER_VALUE: Final[str] = "a-bearer-value-that-is-not-the-expected-one"
SHARED_VALUE: Final[str] = "a-shared-ingress-value-the-ingest-suite-signs-with"
OTHER_SHARED_VALUE: Final[str] = "a-shared-ingress-value-nothing-here-is-keyed-with"

# The age bound this module configures, and how far outside it the stale case
# places its timestamp. Both are values rather than waits: the offset is applied to
# the reading taken when the request is built, so nothing sleeps.
MAX_AGE_SECONDS: Final[int] = 60
STALE_OFFSET_SECONDS: Final[int] = 600

# The request bound the oversized case configures. Deliberately small: a body of
# the deployed default would be five mebibytes of allocation for an assertion about
# a comparison, and the comparison does not care which side of it the number is on.
SMALL_BOUND: Final[int] = 512

# The statement bound every connection this module opens carries.
TIMEOUT_MS: Final[int] = 5000

# How many well-formed records a batch carries. More than one, so a partial write
# would move a row count rather than leaving it where a single record would.
BATCH_RECORDS: Final[int] = 4

# How far apart the records of one batch are placed on the timeline, so the earliest
# is identifiable and the Session start instant can be asserted against it.
RECORD_GAP_SECONDS: Final[int] = 1

# Statuses this module reads.
OK: Final[int] = 200
UNAUTHORISED: Final[int] = 401
TOO_LARGE: Final[int] = 413
UNAVAILABLE: Final[int] = 503

# What the placed records say about themselves. None is what an assertion turns on
# beyond being read back unchanged, so each is fixed here rather than varied.
AGENT_CLI: Final[str] = "claude_code"
MACHINE_ID: Final[str] = "machine-under-test"
COMMAND: Final[str] = "ls"

# The port the unreachable store addresses. The lowest port is used because nothing
# on this platform listens there and a connection to it is refused rather than left
# to time out, so the 503 case costs one refused socket instead of a wait.
UNREACHABLE_PORT: Final[int] = 1

# How long a connection attempt to that port is allowed to take before it is given
# up on, so a platform that drops rather than refuses still bounds the case.
CONNECT_TIMEOUT_SECONDS: Final[int] = 2

# The counts every claim about what landed and what did not is read from. The
# schema-wide pair is what makes *nothing persisted* an observation: a partial write
# anywhere in the request moves it.
COUNT_SESSION: Final[str] = "SELECT count(*) FROM session WHERE id = %s"
COUNT_LEDGER: Final[str] = "SELECT count(*) FROM ledger WHERE session_id = %s"
COUNT_ALL_SESSIONS: Final[str] = "SELECT count(*) FROM session"
COUNT_ALL_LEDGER: Final[str] = "SELECT count(*) FROM ledger"

# The stored Session row, read for the columns the ingest path derived from the
# batch rather than merely counted.
SELECT_SESSION: Final[str] = (
    "SELECT client_id, agent_cli, machine_id, started_at, ended_at, outcome, depth, "
    "parent_session_id, spawning_event_id, halted FROM session WHERE id = %s"
)

# The sequence numbers of one Session's Ledger rows, ascending, so the batch is
# asserted to have landed contiguously rather than merely in the right number.
SELECT_SEQUENCES: Final[str] = "SELECT seq FROM ledger WHERE session_id = %s ORDER BY seq ASC"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# The schema, the store, and the direct reads every claim is made from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stored:
    """How many rows name one Session, and how many the whole schema holds.

    The pair is read together rather than as two numbers taken apart, because a
    claim that a request persisted nothing is a claim about both at once: the
    Session's own rows, and every row of the two tables the ingest path writes.
    """

    session_rows: int
    ledger_rows: int
    all_session_rows: int
    all_ledger_rows: int

    @property
    def own(self) -> tuple[int, int]:
        """The Session's own row counts, as the pair an assertion states."""
        return self.session_rows, self.ledger_rows

    @property
    def schema(self) -> tuple[int, int]:
        """The schema-wide row counts, as the pair a before-and-after compares."""
        return self.all_session_rows, self.all_ledger_rows


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the driver behind both."""

    store: MemoryStore
    connection: DriverConnection
    driver: ModuleType
    schema: str
    dsn: str

    def one(self, statement: str, params: tuple[object, ...] | None = None) -> tuple[Any, ...]:
        """The single row a statement is expected to produce, on this module's connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            produced = cursor.fetchall()
        assert len(produced) == 1, f"the statement produced {len(produced)} rows where one was read"
        row: tuple[Any, ...] = tuple(produced[0])
        return row

    def column(self, statement: str, params: tuple[object, ...] | None = None) -> tuple[Any, ...]:
        """The first column of every row a statement produced, in the order it produced them."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            produced = cursor.fetchall()
        return tuple(row[0] for row in produced)

    def count(self, statement: str, params: tuple[object, ...] | None = None) -> int:
        """The number one counting statement reports."""
        return int(self.one(statement, params)[0])

    def stored(self, session_id: UUID) -> Stored:
        """Every count a claim about persistence is made from, read in one reading."""
        return Stored(
            session_rows=self.count(COUNT_SESSION, (session_id,)),
            ledger_rows=self.count(COUNT_LEDGER, (session_id,)),
            all_session_rows=self.count(COUNT_ALL_SESSIONS),
            all_ledger_rows=self.count(COUNT_ALL_LEDGER),
        )

    def unreachable_store(self) -> MemoryStore:
        """A store addressing a port nothing listens on, over the same driver.

        Unreachability is produced by addressing nothing rather than by stopping
        anything: the instance is shared, so a case that needed it down would be a
        case that could not be run. The connection factory is otherwise the one the
        reachable store uses, so what differs between the two is the endpoint alone.
        """
        target = unreachable_target(self.dsn)

        def connect_with() -> Connection:
            opened: Connection = self.driver.connect(
                target,
                autocommit=True,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
            )
            return opened

        return MemoryStore(connect_with=connect_with, statement_timeout_ms=TIMEOUT_MS)


def unreachable_target(dsn: str) -> str:
    """The configured connection string with its port replaced by an unused one.

    Derived from the configured string rather than written out, so the unreachable
    case addresses the same host and the same database as the reachable one and
    differs from it in the port alone.
    """
    parts = urlsplit(dsn)
    assert parts.scheme.startswith("postgres"), (
        "the configured connection string should be a URI, so a port can be replaced in it"
    )
    host = parts.hostname or "localhost"
    authority = host if parts.username is None else f"{parts.username}@{host}"
    return urlunsplit(
        (parts.scheme, f"{authority}:{UNREACHABLE_PORT}", parts.path, parts.query, parts.fragment)
    )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store whose connections see this schema.

    Every migration is applied because the ingest path writes a Session and a Ledger
    row and reads the capability record, and the last of those arrives well after the
    first two.
    """
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    with MemoryStore(connect_with=connect_with, statement_timeout_ms=TIMEOUT_MS) as store:
        yield Cluster(
            store=store,
            connection=fresh_schema,
            driver=database_driver,
            schema=schema,
            dsn=local_instance_dsn,
        )


# ---------------------------------------------------------------------------
# The Collector under test, and the requests presented to it
# ---------------------------------------------------------------------------


def build_configuration(*, maximum: int = DEFAULT_MAX_BODY_BYTES) -> Configuration:
    """A configuration view over an explicit environment and no file."""
    return Configuration(
        environ={
            "MOLT_COLLECTOR_MAX_BODY_BYTES": str(maximum),
            "MOLT_DB_STATEMENT_TIMEOUT_MS": str(TIMEOUT_MS),
            "MOLT_INGRESS_MAX_AGE_SECONDS": str(MAX_AGE_SECONDS),
        }
    )


def wrapped(value: str, *, source_name: str) -> Credential:
    """One credential value, wrapped as the accessors would hand it over."""
    return Credential(value, source_name=source_name, source=CredentialSource.ENVIRONMENT)


def build_collector(
    store: MemoryStore,
    *,
    maximum: int = DEFAULT_MAX_BODY_BYTES,
    recall: Callable[[RecallQuery], RecallAnswer] | None = None,
) -> Collector:
    """A Collector over a live store, with the real signature verifier in place.

    No verifier is injected, so the handler resolves the verification call through
    its own module lookup and reveals the shared value inside it. That is the
    deployed arrangement, and it is the one worth exercising here: the unit module
    already asserts what the seam does when it is driven directly, and what this
    module needs is the four causes reaching a request path with a cluster behind it.
    """
    return Collector(
        configuration=build_configuration(maximum=maximum),
        store=store,
        bearer=wrapped(BEARER_VALUE, source_name=COLLECTOR_BEARER_ENV),
        ingress_key=wrapped(SHARED_VALUE, source_name=INGRESS_KEY_ENV),
        recall=recall,
    )


def invocation(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: bytes = b"",
) -> Invocation:
    """One request as the transport would deliver it, carried as text."""
    return Invocation(
        method=method,
        path=path,
        headers=Headers(headers),
        body_text=body.decode("utf-8"),
        base64_encoded=False,
    )


def authorisation(value: str = BEARER_VALUE) -> dict[str, str]:
    """The one header an authenticated request presents."""
    return {AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {value}"}


def signed(
    body: bytes,
    *,
    key: str = SHARED_VALUE,
    bearer: str = BEARER_VALUE,
    offset: timedelta = timedelta(),
) -> dict[str, str]:
    """The three headers a well-formed ingest request presents.

    The instant is read here rather than written down, because the age bound is
    measured against the host's reading and a module carrying a fixed instant would
    be outside the window the moment it was written. The offset is what the stale
    case moves that reading by, so no example waits for a clock.
    """
    presented = ingress_timestamp(datetime.now(UTC) + offset)
    return authorisation(bearer) | {
        TIMESTAMP_HEADER: presented,
        SIGNATURE_HEADER: sign_ingress(body, key, presented),
    }


def build_event(
    session_id: UUID,
    *,
    occurred_at: datetime,
    parent_event_id: UUID | None = None,
) -> Event:
    """One well-formed Event of the shape the capture side transmits.

    The parent reference is the lever the atomicity claim uses: the Ledger carries a
    reference from that column to its own primary key, so an identifier no row holds
    is refused by the append and by nothing before it.
    """
    return Event(
        id=uuid4(),
        session_id=session_id,
        client_id=UNASSIGNED_CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=occurred_at,
        agent_cli=AGENT_CLI,
        machine_id=MACHINE_ID,
        parent_event_id=parent_event_id,
        payload={"command": COMMAND},
        redacted=False,
        text_body=None,
    )


def build_batch(
    session_id: UUID,
    *,
    records: int = BATCH_RECORDS,
    absent_parent_on_last: bool = False,
) -> tuple[Event, ...]:
    """A batch of well-formed records for one Session, ordered on the timeline.

    The records are spaced so the earliest is identifiable, which is what lets the
    stored Session start instant be asserted against the batch rather than against
    whatever the cluster happened to default to. When the last record is given an
    absent parent, every record before it is still well-formed and still lands inside
    the transaction before the refusal arrives.
    """
    base = datetime.now(UTC)
    built: list[Event] = []
    for index in range(records):
        last = index == records - 1
        built.append(
            build_event(
                session_id,
                occurred_at=base + timedelta(seconds=index * RECORD_GAP_SECONDS),
                parent_event_id=uuid4() if last and absent_parent_on_last else None,
            )
        )
    return tuple(built)


def content_keys_in(value: JsonValue) -> set[str]:
    """Every content-bearing field name a document carries, at any depth.

    The recursion is the point: the health document nests a record per probed
    capability, so a top-level reading would not see a content-bearing name added
    inside one of them.
    """
    found: set[str] = set()
    if isinstance(value, dict):
        for name, nested in value.items():
            if name.lower() in CONTENT_KEYS:
                found.add(name)
            found |= content_keys_in(nested)
    elif isinstance(value, list):
        for item in value:
            found |= content_keys_in(item)
    return found


def counted(name: str) -> float:
    """How many times an undimensioned metric has been counted in this process."""
    return current().counters().get((name, ()), 0.0)


# ---------------------------------------------------------------------------
# The Session is created inside the transaction that writes its Events
# ---------------------------------------------------------------------------


def test_a_batch_naming_an_absent_session_creates_it_with_its_events(cluster: Cluster) -> None:
    """Both the Session row and the batch's Ledger rows are there afterwards.

    This half is the setup rather than the claim: two separate transactions would
    satisfy every assertion below. What it does establish is that the Session the
    ingest path derives from the batch is derived from the records themselves, so the
    columns are read back rather than counted, and the sequence numbers are read back
    ascending so the batch is contiguous rather than merely the right size.
    """
    session_id = uuid4()
    events = build_batch(session_id)
    body = batch_body(events)
    assert cluster.stored(session_id).own == (0, 0), "the Session names no row before the request"

    answer = build_collector(cluster.store).serve(
        invocation("POST", EVENTS_PATH, headers=signed(body), body=body)
    )

    assert answer.status == OK
    assert answer.document["accepted"] == BATCH_RECORDS
    assert answer.document["rejected"] == 0
    assert cluster.stored(session_id).own == (1, BATCH_RECORDS)

    row = cluster.one(SELECT_SESSION, (session_id,))
    assert row[0] == UNASSIGNED_CLIENT_ID
    assert row[1] == AGENT_CLI
    assert row[2] == MACHINE_ID
    assert row[3] == events[0].occurred_at, "the start instant is the earliest the batch observed"
    assert row[4] is None
    assert row[5] == SessionOutcome.IN_PROGRESS.value
    assert row[6] == 0
    assert row[7] is None
    assert row[8] is None
    assert row[9] is False
    assert cluster.column(SELECT_SEQUENCES, (session_id,)) == tuple(range(1, BATCH_RECORDS + 1)), (
        "the batch is one contiguous run of the Session's chain"
    )


def test_an_event_the_cluster_refuses_leaves_no_session_behind(cluster: Cluster) -> None:
    """The claim: the Session and its Events share one transaction, not two.

    The batch's final record names a parent Event no Ledger row holds, which the
    reference the Ledger carries over that column refuses. By the time the refusal
    arrives the Session insert has committed nothing but has succeeded, and so have
    the appends of every earlier record, so what the cluster discards is a partial
    write spanning both tables. A Session written in a transaction of its own would
    survive that discard and be counted here.

    The response is a 200 whose rejection breakdown says the records were refused,
    because a batch naming rows the cluster does not hold is a fault in the request
    rather than in the cluster: retrying it would fail the same way, so the caller is
    told to stop rather than to spool.
    """
    session_id = uuid4()
    events = build_batch(session_id, absent_parent_on_last=True)
    body = batch_body(events)
    before = cluster.stored(session_id)

    answer = build_collector(cluster.store).serve(
        invocation("POST", EVENTS_PATH, headers=signed(body), body=body)
    )

    assert answer.status == OK
    assert answer.document["accepted"] == 0
    assert answer.document["rejected"] == BATCH_RECORDS
    assert answer.document["rejections"] == {str(RejectionReason.REFUSED): BATCH_RECORDS}

    after = cluster.stored(session_id)
    assert after.own == (0, 0), (
        "the Session insert succeeded inside the transaction the refused append aborted, "
        "so a Session row here would mean the two were written separately"
    )
    assert after.schema == before.schema


# ---------------------------------------------------------------------------
# A refused bearer value persists nothing
# ---------------------------------------------------------------------------


def test_a_mismatched_bearer_value_is_refused_and_persists_nothing(cluster: Cluster) -> None:
    """The 401 is asserted alongside the row counts, not instead of them.

    The batch is well formed and correctly signed, so the only thing wrong with the
    request is the bearer value. Counting the schema before and after is what makes
    *nothing persisted* an observation rather than a reading of the order the checks
    sit in (Requirement 5.4).
    """
    session_id = uuid4()
    body = batch_body(build_batch(session_id))
    before = cluster.stored(session_id)

    answer = build_collector(cluster.store).serve(
        invocation("POST", EVENTS_PATH, headers=signed(body, bearer=OTHER_BEARER_VALUE), body=body)
    )

    after = cluster.stored(session_id)
    assert answer.status == UNAUTHORISED
    assert after.own == (0, 0)
    assert after.schema == before.schema


# ---------------------------------------------------------------------------
# Each of the four signature rejection causes persists nothing
# ---------------------------------------------------------------------------


def without_timestamp(body: bytes) -> dict[str, str]:
    """Present the signature and no timestamp header (Requirement 47.7)."""
    headers = signed(body)
    del headers[TIMESTAMP_HEADER]
    return headers


def without_signature(body: bytes) -> dict[str, str]:
    """Present the timestamp and no signature header (Requirement 47.8)."""
    headers = signed(body)
    del headers[SIGNATURE_HEADER]
    return headers


def outside_window(body: bytes) -> dict[str, str]:
    """Present a timestamp beyond the configured age bound, correctly signed.

    The signature is computed over the presented timestamp, so it matches. The only
    thing wrong with the request is where it sits on the timeline, which is what makes
    this the window cause rather than the mismatch one (Requirement 47.5).
    """
    return signed(body, offset=timedelta(seconds=-STALE_OFFSET_SECONDS))


def mismatched_signature(body: bytes) -> dict[str, str]:
    """Present a fresh timestamp and a signature keyed with another value.

    Keying with a different shared value rather than corrupting the digest is
    deliberate: the presented value is a well-formed digest of the right length over
    the right material, so what fails is the comparison rather than the shape
    (Requirement 47.4).
    """
    return signed(body, key=OTHER_SHARED_VALUE)


# Each cause with the way this module produces it. The mapping is asserted below to
# cover the enumeration, so a fifth cause added to the verifier fails this module
# rather than passing unexercised.
CAUSES: Final[tuple[tuple[RejectionCause, Callable[[bytes], dict[str, str]]], ...]] = (
    (RejectionCause.TIMESTAMP_ABSENT, without_timestamp),
    (RejectionCause.SIGNATURE_ABSENT, without_signature),
    (RejectionCause.OUTSIDE_WINDOW, outside_window),
    (RejectionCause.MISMATCH, mismatched_signature),
)


def test_every_counted_rejection_cause_is_one_this_suite_drives() -> None:
    """The parameterisation below covers the causes the verifier counts, all of them."""
    assert {cause for cause, _ in CAUSES} == set(RejectionCause)


@pytest.mark.parametrize(
    ("cause", "present"),
    CAUSES,
    ids=[str(cause) for cause, _ in CAUSES],
)
def test_each_signature_rejection_persists_nothing(
    cluster: Cluster,
    cause: RejectionCause,
    present: Callable[[bytes], dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One status, one measurement, and no row, for each of the four causes.

    The batch carries several well-formed records, so a request whose signature was
    checked after the records were read would leave a detectable partial write. The
    schema-wide counts are read before and after and compared, and the Session's own
    counts are read as zero, so neither a partial write nor a Session written without
    its Events could pass (Requirements 47.4, 47.7, 47.8).

    The cause is read off the written record rather than off the response, which is
    both how the design says a cause is disclosed and what keeps this parameterisation
    honest: all four causes answer with one status, so a case that failed under some
    other cause than the one it is named for would otherwise pass unnoticed. The
    response is checked for the cause too, and must not name it.
    """
    reset()
    session_id = uuid4()
    body = batch_body(build_batch(session_id))
    before = cluster.stored(session_id)

    answer = build_collector(cluster.store).serve(
        invocation("POST", EVENTS_PATH, headers=present(body), body=body)
    )

    after = cluster.stored(session_id)
    assert answer.status == UNAUTHORISED, f"the {cause} cause is one status like the others"
    assert after.own == (0, 0)
    assert after.schema == before.schema
    assert counted(SIGNATURE_REJECTED_METRIC) == 1.0
    assert f'"cause":"{cause}"' in capsys.readouterr().err, (
        "the record should name the cause this case was written to produce"
    )
    assert str(cause) not in str(answer.document), "the response discloses no cause"


# ---------------------------------------------------------------------------
# An oversized body persists nothing
# ---------------------------------------------------------------------------


def test_an_oversized_body_is_refused_and_persists_nothing(cluster: Cluster) -> None:
    """The bound is configured small rather than the body being built large.

    What the bound does is compare two numbers, and the comparison is indifferent to
    how large they are. Building a body of the deployed default to exercise it would
    cost megabytes of allocation for no additional assertion, so the configured
    maximum is moved instead and the batch is asserted to exceed it, which makes the
    case self-checking rather than dependent on how long a record happens to be
    (Requirement 5.11).
    """
    session_id = uuid4()
    body = batch_body(build_batch(session_id))
    assert len(body) > SMALL_BOUND, "the batch should exceed the configured maximum"
    before = cluster.stored(session_id)

    answer = build_collector(cluster.store, maximum=SMALL_BOUND).serve(
        invocation("POST", EVENTS_PATH, headers=signed(body), body=body)
    )

    after = cluster.stored(session_id)
    assert answer.status == TOO_LARGE
    assert after.own == (0, 0)
    assert after.schema == before.schema


# ---------------------------------------------------------------------------
# An unreachable cluster is a 503 and a counted write failure
# ---------------------------------------------------------------------------


def test_an_unreachable_cluster_answers_503_and_counts_the_write_failure(
    cluster: Cluster,
) -> None:
    """The caller is told to try again, and the failure is measured under its own name.

    Unreachability is produced by addressing a port nothing listens on rather than by
    stopping the instance, which is what lets this run against a shared one. The live
    schema is counted afterwards as well: a request answered as unreachable should
    have reached nothing, and the only way to say so is to look at the cluster that is
    up (Requirement 5.9).
    """
    reset()
    session_id = uuid4()
    body = batch_body(build_batch(session_id))
    before = cluster.stored(session_id)

    with cluster.unreachable_store() as store:
        answer = build_collector(store).serve(
            invocation("POST", EVENTS_PATH, headers=signed(body), body=body)
        )

    after = cluster.stored(session_id)
    assert answer.status == UNAVAILABLE
    assert counted(WRITE_FAILURE_METRIC) == 1.0
    assert after.own == (0, 0)
    assert after.schema == before.schema


# ---------------------------------------------------------------------------
# The health route, in the branch only a reachable cluster produces
# ---------------------------------------------------------------------------


def test_the_health_body_reports_a_reachable_cluster_and_no_memory_content(
    cluster: Cluster,
) -> None:
    """The cluster answers, so the reachable reading is asserted rather than inferred.

    This is the half the unit module cannot reach: with a store that refuses every
    connection, only the degraded reading is observable. The document is then searched
    for content-bearing field names at every depth of its nesting, because the
    capability summary nests a record per probed fact and a top-level reading would not
    see one added inside it (Requirement 5.3).

    No bearer value is presented, which is the other half of the criterion: the health
    route is the one route reachable without it.
    """
    answer = build_collector(cluster.store).serve(invocation("GET", HEALTH_PATH, headers={}))

    assert answer.status == OK
    assert answer.document["status"] == LIVE_STATUS
    assert answer.document["database"] == REACHABLE
    assert content_keys_in(answer.document) == set()


# ---------------------------------------------------------------------------
# Recall is answered by the bearer token alone
# ---------------------------------------------------------------------------


def test_the_recall_route_answers_with_the_bearer_token_alone(cluster: Cluster) -> None:
    """One Collector, two routes, one credential presented, two different answers.

    The contrast is the assertion. The same instance, with the same real verifier
    attached and the same shared value held, refuses an unsigned batch and answers an
    unsigned recall query, which is what Requirement 47.12 asks for: an interactive
    caller holding no shared secret still reaches the recall path. The refused batch is
    counted afterwards, so the refusal is shown to have persisted nothing here too.
    """
    asked: list[RecallQuery] = []

    def search(query: RecallQuery) -> RecallAnswer:
        asked.append(query)
        return RecallAnswer(results=({"artifact_id": str(uuid4()), "distance": 0.25},))

    collector = build_collector(cluster.store, recall=search)
    session_id = uuid4()
    batch = batch_body(build_batch(session_id))
    query = f'{{"query_text": "how did this fail", "session_id": "{session_id}"}}'.encode()
    before = cluster.stored(session_id)

    refused = collector.serve(invocation("POST", EVENTS_PATH, headers=authorisation(), body=batch))
    answered = collector.serve(invocation("POST", RECALL_PATH, headers=authorisation(), body=query))

    results = answered.document["results"]
    assert refused.status == UNAUTHORISED, "the batch route presents no signature and is refused"
    assert answered.status == OK
    assert isinstance(results, list)
    assert len(results) == 1
    assert len(asked) == 1
    assert asked[0].session_id == session_id

    after = cluster.stored(session_id)
    assert after.own == (0, 0)
    assert after.schema == before.schema
