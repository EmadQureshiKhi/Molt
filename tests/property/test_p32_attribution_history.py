"""Property 32: the attribution history answers every instant, and stores what it said.

**Validates: Requirements 43.1, 43.2, 43.3, 43.4, 43.5, 43.8, 12.7**

This property needs the cluster for every clause it makes. The validity interval
comes from the cluster's own transaction timestamp, the half-open containment is a
predicate the cluster evaluates, the single current claim per pair is a
partial unique index, the retained maximum confidence is `greatest` evaluated
against the value the closing statement returned, and the supersession Event is
appended on the same cursor inside the same transaction as the two attribution
statements. A history assembled in memory would be evidence about a
reimplementation rather than about the stored record an auditor reads, so the
module is marked to gate on a reachable instance and is deselected from the
credential-free workflow.

Six decisions shape what is asserted.

**Each write is drawn as a shape against what the pair already holds, not as an
independent triple.** The interesting histories are the ones where the write path
takes each of its branches: a submission repeating the current claim exactly, which
supersedes nothing at all; a run of strictly increasing confidences under one
method; a method change at exactly the confidence already held, which does
supersede because the method is part of the claim; and a weaker submission under a
different method, which supersedes and keeps the confidence it closed. Fifty
independently drawn triples would reach the first and third almost never, so the
generator carries the claim each Client currently holds while it draws and resolves
each shape against it.

**The expected history is walked in Python from the drawn writes.** The outcome of
every write, the claim each pair holds after it, and the list of supersessions are
all computed from the drawn sequence alone, so nothing below compares the store
against itself.

**The stored history is read back column by column on the fixture's own
connection, and every as-of answer is checked against a containment computed from
those rows.** The half-open rule is restated here as *the validity start is at or
before the instant and the validity end is absent or strictly after it*, and the
query's answer is required to be exactly that set. Restating the rule is the point:
if the query were closed at its end, an instant lying on a supersession would come
back carrying two versions for one Client and the comparison would show it.

**The instants are read off the stored rows rather than chosen.** Four classes are
covered: before the first write, at a supersession instant, between two adjacent
instants, and after the last write. Only the two ends of that list are computable
without reading, so the generator draws positions and the positions select which
supersession instant and which gap an example asks about, once the history exists.
Every class an example can reach is asked at least once, and the number asked is
bounded so that a fifty-write history does not cost fifty as-of queries.

**Immutability is asserted against what each write returned, not against a second
read of the same row.** A version's stored method, confidence, Artifact, and Client
are compared with the method submitted and the confidence the write reported at the
time it committed, after every later write of the example has landed, so a
supersession that quietly restated an earlier version would be caught. The
database-side guard that refuses such a change is not what is exercised here: this
suite connects as an administrative role, which that guard exempts by design, so
what is asserted is that the write path holds no statement which changes those
columns. The guard's own refusal is asserted in the unit module of the same
concern.

**Withdrawals are out of scope for this module.** The property is stated over
attribution writes and the instants they produce, and a withdrawal's terminal
marker carries an empty validity interval that no instant contains, so it would
contribute a row to the history that neither read form returns. The withdrawal path
is asserted in the instance-backed module of the same concern.

The example budget is the hundred the plan states, with no per-example deadline. What
one example costs is bounded by the drawn write count and by nothing else: up to fifty
real attribution writes, each a transaction of its own carrying a decision read, a
closing statement, an insert, and a Ledger append, plus at most eight as-of queries and
three readbacks. Nothing in that grows with the example's position, because every read
is keyed by the Artifact the example drew and no other example shares it. Two costs are
paid once for the whole module rather than per example: the schema and its migrations,
and the Clients the writes are spread over, which carry nothing an example varies and
are therefore placed once by a fixture of their own. What is left per example is the
Session a supersession Event needs and the writes themselves, and the writes are the
coverage — the range they are drawn from is the design's own 1 to 50 and it has not
moved.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.artifact import ArtifactKind
from molt.models.binding import BindingMethod
from molt.models.event import EventCategory
from molt.store import Connection, MemoryStore
from molt.store.attribution import (
    AttributionOutcome,
    AttributionSubmission,
    SupersessionContext,
    attribution_as_of,
    current_attribution,
    write_attribution,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# How many examples the property runs, how long a drawn sequence is, and how many
# Clients one Artifact is written for. The reasoning behind the budget is in the
# module docstring.
MAX_EXAMPLES: Final[int] = 100
MIN_WRITES: Final[int] = 1
MAX_WRITES: Final[int] = 50
MIN_CLIENTS: Final[int] = 2
MAX_CLIENTS: Final[int] = 5

# How many instants of the two interior classes an example asks about. The two
# exterior classes are asked once each and cost nothing to locate.
MAX_SAMPLED_INSTANTS: Final[int] = 3

# The ends of the interval a confidence is drawn from.
CONFIDENCE_FLOOR: Final[float] = 0.0
CONFIDENCE_CEILING: Final[float] = 1.0

# How far outside the stored history the two exterior instants sit. Far enough that
# neither is a boundary case of the containment predicate, since the boundary is
# what the supersession-instant class is for.
OUTSIDE_MARGIN: Final[timedelta] = timedelta(seconds=60)

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The writes and reads this module makes for itself. The module under test owns no
# tenant insert and no Session insert, and the history and the Ledger are read back
# by statements of this module's own, so no claim about stored state rests on the
# statement text the module under test reads through.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
SELECT_ARTIFACT_HISTORY: Final[str] = (
    "SELECT id, artifact_id, client_id, method, confidence, valid_from, valid_to, superseded_by "
    "FROM client_binding WHERE artifact_id = %s "
    "ORDER BY valid_from ASC, valid_to ASC NULLS LAST, id ASC"
)
SELECT_EVENTS: Final[str] = (
    "SELECT id, category, payload FROM ledger WHERE session_id = %s ORDER BY seq ASC"
)

# The command and the machine every write of this module records.
AGENT_CLI: Final[str] = "molt"
MACHINE_ID: Final[str] = "property-machine"

# The detection instant every submission carries and how long an appended row is
# retained for. The reading is derived from the epoch rather than written out, so
# no example embeds a calendar value, and it is a detection reading rather than a
# validity reading: every validity instant compared below is read off a stored row.
DETECTED_AT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# What the payload of a supersession Event names, and the reason a detection change
# records. Read from the module under test would make the assertion circular, so
# the field names are stated here.
PAYLOAD_ARTIFACT: Final[str] = "artifact_id"
PAYLOAD_CLIENT: Final[str] = "client_id"
PAYLOAD_SUPERSEDED: Final[str] = "superseded_version_id"
PAYLOAD_SUPERSEDING: Final[str] = "superseding_version_id"

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


class WriteShape(StrEnum):
    """How one drawn write relates to the claim its pair already holds.

    The names say what the submission does rather than which outcome it produces,
    because two of them produce different outcomes depending on what is held: a
    repeat of nothing is a first write, and a stronger submission for a pair at the
    ceiling of the interval is a repeat.
    """

    REPEAT = "repeat"
    STRONGER = "stronger"
    METHOD_AT_EQUAL = "method_at_equal"
    WEAKER = "weaker"
    FRESH = "fresh"


@dataclass(frozen=True, slots=True)
class Claim:
    """What one Artifact and Client pair currently asserts."""

    method: BindingMethod
    confidence: float


@dataclass(frozen=True, slots=True)
class PlannedWrite:
    """One drawn submission: which Client it is for, what it says, and why."""

    client_index: int
    method: BindingMethod
    confidence: float
    shape: WriteShape


@dataclass(frozen=True, slots=True)
class WritePlan:
    """A drawn history for one Artifact, and which instants to ask about.

    Attributes:
        client_count: How many Clients the Artifact's writes are spread over.
        writes: The submissions in the order they are sent.
        positions: Fractions selecting which supersession instant and which gap
            between adjacent instants an example asks about. They are fractions
            rather than indices because the instants do not exist until the writes
            have been sent and the history read back.
    """

    client_count: int
    writes: tuple[PlannedWrite, ...]
    positions: tuple[float, ...]

    @property
    def size(self) -> int:
        """How many submissions this plan holds."""
        return len(self.writes)


def resulting_claim(held: Claim | None, method: BindingMethod, confidence: float) -> Claim:
    """What a pair holds after one submission, by the rule the write path applies.

    Three branches. A pair holding nothing takes the submission as it stands. A
    submission carrying the same method and no greater confidence says nothing new
    and leaves the claim alone. Anything else supersedes, and the successor carries
    the submitted method with the greater of the two confidences, because the
    comparison the cluster evaluates is against the confidence it just closed.
    """
    if held is None:
        return Claim(method=method, confidence=confidence)
    if method is held.method and confidence <= held.confidence:
        return held
    return Claim(method=method, confidence=max(confidence, held.confidence))


def expected_outcome(
    held: Claim | None,
    method: BindingMethod,
    confidence: float,
) -> AttributionOutcome:
    """Which of the three things one submission does to the pair it names."""
    if held is None:
        return AttributionOutcome.INSERTED
    if method is held.method and confidence <= held.confidence:
        return AttributionOutcome.UNCHANGED
    return AttributionOutcome.SUPERSEDED


@dataclass(frozen=True, slots=True)
class ExpectedWrite:
    """What one submission ought to do, and what its pair ought to hold after it."""

    outcome: AttributionOutcome
    claim: Claim


def reference_writes(plan: WritePlan) -> tuple[ExpectedWrite, ...]:
    """Walk a drawn plan and return the outcome and resulting claim of every write.

    This is the independent model every assertion about the write path is compared
    against, computed from the drawn submissions and nothing that was read back.
    """
    held: dict[int, Claim] = {}
    expected: list[ExpectedWrite] = []
    for write in plan.writes:
        prior = held.get(write.client_index)
        outcome = expected_outcome(prior, write.method, write.confidence)
        claim = resulting_claim(prior, write.method, write.confidence)
        held[write.client_index] = claim
        expected.append(ExpectedWrite(outcome=outcome, claim=claim))
    return tuple(expected)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def confidences() -> st.SearchStrategy[float]:
    """Draw one confidence from the closed unit interval, both ends included."""
    return st.one_of(
        st.sampled_from((CONFIDENCE_FLOOR, CONFIDENCE_CEILING)),
        st.floats(
            min_value=CONFIDENCE_FLOOR,
            max_value=CONFIDENCE_CEILING,
            allow_nan=False,
            allow_infinity=False,
        ),
    )


def other_methods(method: BindingMethod) -> st.SearchStrategy[BindingMethod]:
    """Draw any detection method other than the one a pair already carries."""
    return st.sampled_from([member for member in BindingMethod if member is not method])


def fresh_claims() -> st.SearchStrategy[tuple[BindingMethod, float]]:
    """Draw an unconstrained submission: any method, any admissible confidence."""
    return st.tuples(st.sampled_from(BindingMethod), confidences())


def shaped_claims(
    shape: WriteShape,
    held: Claim | None,
) -> st.SearchStrategy[tuple[BindingMethod, float]]:
    """Resolve one drawn shape against the claim its pair holds.

    A pair holding nothing has no shape to be relative to, so every shape resolves
    to a fresh submission there. The two comparative shapes are clamped at the ends
    of the interval rather than discarded: a pair already at the ceiling admits no
    stronger submission, and one at the floor admits no weaker, and in both cases
    the equal value is the honest resolution and produces the repeated-claim path.
    """
    if held is None or shape is WriteShape.FRESH:
        return fresh_claims()
    if shape is WriteShape.REPEAT:
        return st.just((held.method, held.confidence))
    if shape is WriteShape.METHOD_AT_EQUAL:
        return st.tuples(other_methods(held.method), st.just(held.confidence))
    if shape is WriteShape.STRONGER:
        if held.confidence >= CONFIDENCE_CEILING:
            return st.just((held.method, held.confidence))
        return st.tuples(
            st.just(held.method),
            st.floats(
                min_value=held.confidence,
                max_value=CONFIDENCE_CEILING,
                exclude_min=True,
                allow_nan=False,
                allow_infinity=False,
            ),
        )
    if held.confidence <= CONFIDENCE_FLOOR:
        return st.tuples(other_methods(held.method), st.just(held.confidence))
    return st.tuples(
        other_methods(held.method),
        st.floats(
            min_value=CONFIDENCE_FLOOR,
            max_value=held.confidence,
            exclude_max=True,
            allow_nan=False,
            allow_infinity=False,
        ),
    )


@st.composite
def attribution_write_sequences(draw: st.DrawFn) -> WritePlan:
    """Draw 1 to 50 writes for one Artifact over 2 to 5 Clients, and the instants to ask.

    The claim every Client holds is carried along while the sequence is drawn, so a
    shape is resolved against the state the write it describes will actually meet.
    That is what makes a repeated identical write, a monotone confidence run, and a
    method change at equal confidence ordinary members of a drawn history rather
    than coincidences.

    The length is drawn as a number before the writes are drawn, rather than left
    to a list's own sizing, because a list generator concentrates near its lower
    bound and the long histories are the interesting end: a fifty-write history over
    a handful of Clients is where the interleaving, the supersession instants, and
    the gaps between them all appear at once.
    """
    client_count = draw(st.integers(min_value=MIN_CLIENTS, max_value=MAX_CLIENTS))
    size = draw(st.integers(min_value=MIN_WRITES, max_value=MAX_WRITES))
    held: dict[int, Claim] = {}
    writes: list[PlannedWrite] = []
    for _ in range(size):
        client_index = draw(st.integers(min_value=0, max_value=client_count - 1))
        shape = draw(st.sampled_from(WriteShape))
        method, confidence = draw(shaped_claims(shape, held.get(client_index)))
        writes.append(
            PlannedWrite(
                client_index=client_index,
                method=method,
                confidence=confidence,
                shape=shape,
            )
        )
        held[client_index] = resulting_claim(held.get(client_index), method, confidence)
    positions = draw(
        st.lists(
            st.floats(min_value=0.0, max_value=1.0),
            min_size=1,
            max_size=MAX_SAMPLED_INSTANTS,
        )
    )
    return WritePlan(client_count=client_count, writes=tuple(writes), positions=tuple(positions))


# ---------------------------------------------------------------------------
# The cluster the history is stored on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredVersion:
    """One stored version of an Artifact, read back column by column."""

    id: UUID
    artifact_id: UUID
    client_id: UUID
    method: str
    confidence: float
    valid_from: datetime
    valid_to: datetime | None
    superseded_by: UUID | None

    @property
    def current(self) -> bool:
        """Whether this row is the live claim for its pair."""
        return self.superseded_by is None

    def contains(self, at: datetime) -> bool:
        """Whether this version's half-open validity interval holds an instant.

        Inclusive at the start and exclusive at the end, restated here rather than
        read from the module under test, so the query's answer is compared against
        the rule the requirement states.
        """
        return self.valid_from <= at and (self.valid_to is None or self.valid_to > at)


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One appended Ledger row, read back column by column."""

    id: UUID
    category: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the reads of this module."""

    store: MemoryStore
    connection: DriverConnection

    def tenant(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        self.send(
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:10]}", "Tenant", "eu"),
        )
        return identifier

    def session(self, client_id: UUID) -> UUID:
        """Place one Session for a tenant, which a supersession Event belongs to."""
        identifier = uuid4()
        self.send(INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
        return identifier

    def context(self, session_id: UUID) -> SupersessionContext:
        """The Session context a supersession Event is recorded within."""
        return SupersessionContext(
            session_id=session_id,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=DETECTED_AT + RETENTION,
        )

    def send(self, statement: str, params: tuple[object, ...]) -> None:
        """Send one parameterised statement on the fixture's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)

    def rows_of(self, statement: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        """Read every row of one parameterised statement on the fixture's connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            return list(cursor.fetchall())

    def history_of(self, artifact_id: UUID) -> tuple[StoredVersion, ...]:
        """Every stored version of one Artifact, oldest first."""
        return tuple(
            StoredVersion(
                id=_as_uuid(row[0]),
                artifact_id=_as_uuid(row[1]),
                client_id=_as_uuid(row[2]),
                method=str(row[3]),
                confidence=float(str(row[4])),
                valid_from=_as_moment(row[5]),
                valid_to=None if row[6] is None else _as_moment(row[6]),
                superseded_by=None if row[7] is None else _as_uuid(row[7]),
            )
            for row in self.rows_of(SELECT_ARTIFACT_HISTORY, (artifact_id,))
        )

    def events_of(self, session_id: UUID) -> tuple[LedgerRow, ...]:
        """Every Ledger row of one Session, in sequence order."""
        return tuple(
            LedgerRow(id=_as_uuid(row[0]), category=str(row[1]), payload=_as_payload(row[2]))
            for row in self.rows_of(SELECT_EVENTS, (session_id,))
        )


def client_key(version: StoredVersion) -> str:
    """The Client identifier of a version as text, which is the order both reads use.

    Both read forms order by the Client identifier, and the cluster orders that
    column by its bytes, which is the order the hexadecimal rendering sorts in.
    """
    return str(version.client_id)


def _as_uuid(value: object) -> UUID:
    """Narrow a stored identifier, refusing anything else."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _as_moment(value: object) -> datetime:
    """Narrow a stored validity instant, refusing anything else."""
    assert isinstance(value, datetime), "a validity column holds an instant"
    return value


def _as_payload(value: object) -> dict[str, object]:
    """Narrow a stored Event payload, refusing anything else."""
    assert isinstance(value, dict), "an Event payload is a mapping"
    return {str(key): item for key, item in value.items()}


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store whose connections see that schema.

    Every migration is applied because the partial unique index, the total closure
    check, the ordered-interval check, and the supersession Event category all
    arrive with the attribution migration, and the self-referencing constraint that
    would refuse the ordered pair of statements is dropped by the protection
    migration.

    Module scope is what keeps the schema cost paid once: examples are isolated from
    each other by an Artifact and a set of tenants of their own rather than by a
    schema of their own.
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

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


@pytest.fixture(scope="module")
def tenants(cluster: Cluster) -> tuple[UUID, ...]:
    """The Clients every example's writes are spread over, placed once for the module.

    A tenant row carries nothing an example varies, and every claim this property makes
    is keyed by the pair of an Artifact and a Client while the Artifact is drawn fresh
    in each example, so one set of Clients can hold the histories of a hundred Artifacts
    without any two examples meeting on a pair. Placing them once rather than per
    example is what keeps the rows an example writes for itself down to the Session a
    supersession Event is appended within, which does have to be its own.
    """
    return tuple(cluster.tenant() for _ in range(MAX_CLIENTS))


# ---------------------------------------------------------------------------
# Sending a drawn plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SentWrite:
    """What one write committed, held beside what it was asked to record.

    The submitted method and the reported confidence together are *the values
    written*, which is what the immutability clause compares the stored row
    against after every later write of the example has landed.
    """

    version_id: UUID
    client_id: UUID
    method: BindingMethod
    confidence: float
    outcome: AttributionOutcome
    superseded_id: UUID | None
    event_id: UUID | None


def submission_of(
    write: PlannedWrite,
    artifact_id: UUID,
    client_id: UUID,
) -> AttributionSubmission:
    """One drawn submission, in the shape the Binding_Detector submits."""
    return AttributionSubmission(
        artifact_id=artifact_id,
        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
        client_id=client_id,
        method=write.method,
        confidence=write.confidence,
        detected_at=DETECTED_AT,
    )


def send_plan(
    cluster: Cluster,
    plan: WritePlan,
    *,
    artifact_id: UUID,
    clients: Sequence[UUID],
    context: SupersessionContext,
) -> tuple[SentWrite, ...]:
    """Send every drawn write in order and report what each one committed."""
    sent: list[SentWrite] = []
    for write in plan.writes:
        client_id = clients[write.client_index]
        written = write_attribution(
            cluster.store,
            submission_of(write, artifact_id, client_id),
            context=context,
        )
        sent.append(
            SentWrite(
                version_id=written.version_id,
                client_id=client_id,
                method=write.method,
                confidence=written.confidence,
                outcome=written.outcome,
                superseded_id=written.superseded_id,
                event_id=written.event_id,
            )
        )
    return tuple(sent)


# ---------------------------------------------------------------------------
# The instants an example asks about
# ---------------------------------------------------------------------------


class AsOfClass(StrEnum):
    """Which class of instant an as-of read is taken at."""

    BEFORE_FIRST = "before the first write"
    AT_SUPERSESSION = "at a supersession"
    BETWEEN_SUPERSESSIONS = "between supersessions"
    AFTER_LAST = "after the last write"


@dataclass(frozen=True, slots=True)
class AsOfRead:
    """One instant to ask about, and which class it belongs to."""

    at: datetime
    kind: AsOfClass


def _selected(instants: Sequence[datetime], position: float) -> datetime:
    """The instant a drawn fraction selects from a non-empty ordered list."""
    index = round(position * (len(instants) - 1))
    return instants[index]


def as_of_reads(
    stored: tuple[StoredVersion, ...],
    positions: tuple[float, ...],
) -> tuple[AsOfRead, ...]:
    """The instants to ask about, one class at a time, read off the stored history.

    Four classes. The two exterior ones sit a margin outside the history and are
    always asked. A supersession instant is a validity end a closed version carries,
    which is also its successor's validity start, and is the boundary the half-open
    rule is stated at; those exist only when something was superseded. A gap instant
    is the midpoint of two adjacent instants of the history, which exists only when
    the history holds two distinct instants. The drawn positions select which of
    each to ask about, so the number of reads is bounded however long the history is.
    """
    starts = sorted({version.valid_from for version in stored})
    closures = sorted({version.valid_to for version in stored if version.valid_to is not None})
    boundaries = sorted(set(starts) | set(closures))

    reads: list[AsOfRead] = [
        AsOfRead(at=boundaries[0] - OUTSIDE_MARGIN, kind=AsOfClass.BEFORE_FIRST),
        AsOfRead(at=boundaries[-1] + OUTSIDE_MARGIN, kind=AsOfClass.AFTER_LAST),
    ]
    if closures:
        reads.extend(
            AsOfRead(at=_selected(closures, position), kind=AsOfClass.AT_SUPERSESSION)
            for position in positions
        )
    gaps = [earlier + (later - earlier) / 2 for earlier, later in pairwise(boundaries)]
    if gaps:
        reads.extend(
            AsOfRead(at=_selected(gaps, position), kind=AsOfClass.BETWEEN_SUPERSESSIONS)
            for position in positions
        )
    return tuple(reads)


# ---------------------------------------------------------------------------
# The coverage record
# ---------------------------------------------------------------------------


def size_band(size: int) -> str:
    """Which part of the length range an example drew, for the coverage record."""
    if size == MIN_WRITES:
        return "1"
    if size <= 10:
        return "2-10"
    if size <= 25:
        return "11-25"
    return f"26-{MAX_WRITES}"


def count_band(count: int) -> str:
    """How often a shape or an outcome occurred, for the coverage record."""
    if count == 0:
        return "none"
    if count == 1:
        return "1"
    if count <= 5:
        return "2-5"
    return "6+"


def realised_shape(write: PlannedWrite, held: Claim | None) -> str:
    """What a drawn shape actually amounted to against the claim it met.

    A shape drawn for a pair holding nothing is a first write whatever it was drawn
    as, and one drawn at an end of the interval may resolve to the value already
    held, so the coverage record names what happened rather than what was asked for.
    """
    if held is None:
        return "first write"
    if write.method is held.method and write.confidence == held.confidence:
        return "identical resubmission"
    if write.method is held.method and write.confidence > held.confidence:
        return "monotone run step"
    if write.method is not held.method and write.confidence == held.confidence:
        return "method change at equal confidence"
    if write.confidence < held.confidence:
        return "weaker submission"
    return "stronger submission under a new method"


def shape_tally(plan: WritePlan) -> dict[str, int]:
    """How many writes of a plan amounted to each realised shape."""
    held: dict[int, Claim] = {}
    tally: dict[str, int] = {}
    for write in plan.writes:
        prior = held.get(write.client_index)
        name = realised_shape(write, prior)
        tally[name] = tally.get(name, 0) + 1
        held[write.client_index] = resulting_claim(prior, write.method, write.confidence)
    return tally


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 32: For any sequence of 1 to 50 attribution writes per
# Artifact over 2 to 5 Clients with arbitrary detection methods and confidence
# values, paired with as-of timestamps spanning before, within, and after the write
# sequence, the as-of-attribution query returns exactly the Attribution_Versions
# whose half-open validity interval contains the supplied timestamp, the
# current-attribution query returns exactly the versions carrying no superseding
# reference, exactly one unsuperseded version exists per Artifact and Client pair
# holding the maximum confidence submitted, every stored version's detection
# method, confidence value, Artifact identifier, and Client identifier are
# unchanged from the values written, and every supersession produced exactly one
# Ledger Event naming both version identifiers.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(plan=attribution_write_sequences())
def test_the_history_answers_every_instant_and_stores_what_each_write_said(
    cluster: Cluster, tenants: tuple[UUID, ...], plan: WritePlan
) -> None:
    expected = reference_writes(plan)
    supersessions = [
        index
        for index, entry in enumerate(expected)
        if entry.outcome is AttributionOutcome.SUPERSEDED
    ]
    event(f"writes={size_band(plan.size)}")
    event(f"clients={plan.client_count}")
    event(f"supersessions={count_band(len(supersessions))}")
    for name, count in sorted(shape_tally(plan).items()):
        event(f"{name}={count_band(count)}")

    clients = list(tenants[: plan.client_count])
    context = cluster.context(cluster.session(clients[0]))
    artifact_id = uuid4()

    sent = send_plan(cluster, plan, artifact_id=artifact_id, clients=clients, context=context)

    # Every write took the branch the drawn plan says it should have, so the state
    # asserted below was reached the way the property describes.
    for index, entry in enumerate(expected):
        assert sent[index].outcome is entry.outcome, (
            f"write {index} was reported as {sent[index].outcome} where the drawn plan "
            f"makes it {entry.outcome}"
        )

    stored = cluster.history_of(artifact_id)
    by_id = {version.id: version for version in stored}

    # Requirement 43.2: every version the example wrote holds, after every later
    # write has landed, the method it was submitted with and the confidence its own
    # write reported, for the Artifact and the Client it was written for.
    written_versions = {
        write.version_id: write
        for write in sent
        if write.outcome is not AttributionOutcome.UNCHANGED
    }
    assert set(by_id) == set(written_versions), (
        "the stored versions of the Artifact are exactly the versions the writes produced"
    )
    for version_id, write in written_versions.items():
        version = by_id[version_id]
        assert version.artifact_id == artifact_id
        assert version.client_id == write.client_id
        assert version.method == write.method.value, (
            f"a stored version carries method {version.method} where it was written "
            f"with {write.method.value}"
        )
        assert version.confidence == write.confidence, (
            f"a stored version carries confidence {version.confidence} where its write "
            f"reported {write.confidence}"
        )

    # Requirement 43.1: closure is total, and a closed version names the successor
    # whose validity interval begins where its own ends.
    for version in stored:
        assert (version.valid_to is None) == (version.superseded_by is None)
        if version.superseded_by is not None:
            successor = by_id[version.superseded_by]
            assert successor.valid_from == version.valid_to, (
                "a closed version's validity end is its successor's validity start"
            )
            assert successor.client_id == version.client_id
            assert successor.confidence >= version.confidence, (
                "a supersession never lowers the confidence it closed"
            )

    # Requirements 12.7 and 43.5: exactly one unsuperseded version per pair, each
    # holding the maximum confidence submitted for that pair.
    strongest: dict[UUID, float] = {}
    for planned in plan.writes:
        owner = clients[planned.client_index]
        strongest[owner] = max(strongest.get(owner, CONFIDENCE_FLOOR), planned.confidence)
    live = sorted((version for version in stored if version.current), key=client_key)
    assert sorted(version.client_id for version in live) == sorted(strongest), (
        "every Client written for holds exactly one unsuperseded version"
    )
    for version in live:
        assert version.confidence == strongest[version.client_id], (
            f"the live claim holds {version.confidence} where the strongest confidence "
            f"submitted for its Client is {strongest[version.client_id]}"
        )

    # Requirement 43.5: the current-attribution query returns exactly those rows,
    # ordered by Client, and nothing that carries a superseding reference.
    current = current_attribution(cluster.store, artifact_id)
    assert {answer.id for answer in current} == {version.id for version in live}, (
        "the current query returns exactly the versions carrying no superseding reference"
    )
    assert [str(answer.client_id) for answer in current] == sorted(
        str(answer.client_id) for answer in current
    ), "the current query orders its answer by Client"
    for answer in current:
        found = by_id[answer.id]
        assert answer.client_id == found.client_id
        assert answer.method.value == found.method
        assert answer.confidence == found.confidence

    # Requirement 43.4: the as-of query returns exactly the versions whose half-open
    # validity interval contains the instant, at every class of instant.
    for read in as_of_reads(stored, plan.positions):
        event(f"asked {read.kind}")
        contained = {version.id for version in stored if version.contains(read.at)}
        answered = attribution_as_of(cluster.store, artifact_id, read.at)
        assert {answer.id for answer in answered} == contained, (
            f"the as-of answer {read.kind} is not the set of versions whose half-open "
            "interval contains the instant"
        )
        assert len({answer.client_id for answer in answered}) == len(answered), (
            f"one Client contributed two versions to the answer {read.kind}, so an "
            "instant lies inside two intervals of one pair"
        )
        assert [str(answer.client_id) for answer in answered] == sorted(
            str(answer.client_id) for answer in answered
        ), f"the as-of answer {read.kind} is not ordered by Client"
        for version_at in answered:
            found = by_id[version_at.id]
            assert version_at.valid_from == found.valid_from
            assert version_at.valid_to == found.valid_to
            assert version_at.method.value == found.method
            assert version_at.confidence == found.confidence
        if read.kind is AsOfClass.BEFORE_FIRST:
            assert answered == (), "nothing was attributed before the first write"
        if read.kind is AsOfClass.AFTER_LAST:
            assert {answer.id for answer in answered} == {version.id for version in live}, (
                "after the last write the answer is the set of live claims"
            )

    # Requirement 43.8: one Ledger Event per supersession, in the order the
    # supersessions happened, each naming the pair and both version identifiers.
    appended = cluster.events_of(context.session_id)
    assert len(appended) == len(supersessions), (
        f"{len(appended)} Event(s) were appended where {len(supersessions)} supersession(s) "
        "happened"
    )
    for row, index in zip(appended, supersessions, strict=True):
        write = sent[index]
        assert row.category == EventCategory.ATTRIBUTION_SUPERSEDED.value
        assert row.id == write.event_id
        assert write.superseded_id is not None
        assert row.payload[PAYLOAD_ARTIFACT] == str(artifact_id)
        assert row.payload[PAYLOAD_CLIENT] == str(write.client_id)
        assert row.payload[PAYLOAD_SUPERSEDED] == str(write.superseded_id)
        assert row.payload[PAYLOAD_SUPERSEDING] == str(write.version_id)
