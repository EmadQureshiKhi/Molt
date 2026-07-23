"""Property 14: one current claim per pair, holding the strongest confidence submitted.

**Validates: Requirements 12.5, 12.7, 43.5**

This property needs the cluster, and every clause of it says why. The single
current claim is a partial unique index over the rows carrying no superseding
reference; the retained maximum is `greatest` evaluated by the cluster against the
confidence the closing statement returned; the closed unit interval is a check
constraint on the column. None of the three exists in Python, so a sequence of
writes replayed against a dictionary would be evidence about a reimplementation
rather than about the stored history a governance claim is read from. The module is
therefore marked so it gates on a reachable instance and is deselected from the
credential-free workflow, like every other suite that needs one.

Four decisions shape what is asserted.

**The confidences of one example are drawn from a small pool rather than freely.**
A sequence of twenty independently drawn reals almost never repeats a value, and
the two paths this property is most at risk from are the repeated write and the
equal-confidence write: a submission carrying the same method and no greater
confidence supersedes nothing at all, and a submission carrying a different method
at exactly the same confidence supersedes. Both of those need a value the pair
already holds. Drawing a pool of up to four confidences and sampling each write
from it makes both reachable in most examples, and both ends of the closed unit
interval are offered as pool members in their own right so the boundary the check
constraint admits is actually submitted.

**The expected claim is walked in Python from the drawn sequence, not read back
from a second query.** Asserting that the store agrees with itself would assert
nothing, so the outcome of every write and the confidence the pair should hold
after it are computed from the drawn submissions alone, and the stored state is
compared against that. The maximum is then asserted a second way, directly against
the maximum of every confidence submitted so far, which is the clause the property
states.

**The pair is read back column by column on the fixture's own connection.** The
claim *exactly one unsuperseded version exists* must not rest on the same
statement text the module reads bindings through, so the current-version count and
every stored confidence are read by a statement belonging to this module.

**Each example owns a tenant and a Session of its own.** A tenant of its own is
what makes the pair queries total: they return that example's whole history and
nothing another example wrote. The Session is there because a supersession appends
one Ledger Event, which names the Session it was observed in.

The example budget is 100 with no per-example deadline, matching the other
database-backed properties. Per-example cost is one tenant insert, one Session
insert, up to twenty real attribution writes each in a transaction of its own, one
current-version read after each of them, and one history read at the end. Measured
against a local instance that runs from about 4 milliseconds for a single-write
example to about 670 for a twenty-write one, so a hundred examples spend under 30
seconds and the file finishes under a minute including the one-time migration of
the schema, which is about 20 seconds of it. A deadline would fail a twenty-write
example for being large rather than for being wrong, which is why there is none.
Where a budget had to give, it was the budget; no assertion was.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.artifact import ArtifactKind
from molt.models.binding import BindingMethod
from molt.store import Connection, MemoryStore
from molt.store.attribution import (
    AttributionOutcome,
    AttributionSubmission,
    SupersessionContext,
    write_attribution,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# How many examples the property runs and how long a drawn sequence is. The
# reasoning behind the budget is in the module docstring.
MAX_EXAMPLES: Final[int] = 100
MIN_WRITES: Final[int] = 1
MAX_WRITES: Final[int] = 20

# How many distinct confidences one example draws from. A small pool is what makes
# a repeated submission and an equal-confidence submission ordinary rather than
# vanishingly rare across twenty writes.
MAX_POOL_SIZE: Final[int] = 4

# The two ends of the interval the schema admits, offered as pool members so a
# boundary value is really submitted rather than only approached.
INTERVAL_ENDS: Final[tuple[float, float]] = (0.0, 1.0)

# The bounds every stored confidence is asserted to lie within, which is the
# closed interval the requirement states and the check constraint enforces.
CONFIDENCE_FLOOR: Final[float] = 0.0
CONFIDENCE_CEILING: Final[float] = 1.0

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The writes and reads this module makes for itself. The module under test owns no
# tenant insert and no Session insert, and the history is read back by a statement
# of this module's own so that no claim about stored state rests on the statement
# text the module under test reads bindings through.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
SELECT_CURRENT_OF_PAIR: Final[str] = (
    "SELECT id, method, confidence FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NULL"
)
SELECT_PAIR_HISTORY: Final[str] = (
    "SELECT id, method, confidence, superseded_by FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s ORDER BY valid_from ASC, id ASC"
)

# The command and the machine every write of this module records.
AGENT_CLI: Final[str] = "molt"
MACHINE_ID: Final[str] = "property-machine"

# The detection instant every submission carries and how long an appended row is
# retained for. The reading is derived from the epoch rather than written out, so
# no example embeds a calendar value, and it is a detection reading rather than a
# validity reading: every validity instant is the cluster's own.
DETECTED_AT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmittedWrite:
    """One detection result of a drawn sequence, as the detector would submit it."""

    method: BindingMethod
    confidence: float


@dataclass(frozen=True, slots=True)
class WriteSequence:
    """One to twenty repeated writes for a single Artifact and Client pair."""

    writes: tuple[SubmittedWrite, ...]

    @property
    def size(self) -> int:
        """How many submissions this sequence holds."""
        return len(self.writes)


@dataclass(frozen=True, slots=True)
class ExpectedStep:
    """What one submission ought to do, and what the pair ought to hold after it.

    Attributes:
        outcome: Whether the submission is the first version of the pair, a
            supersession of the current one, or a repeated claim that says nothing
            the current version does not already say.
        confidence: The confidence the current version carries afterwards, which
            is the greater of the submitted value and the value that was closed.
        method: The method the current version carries afterwards, which is the
            submitted one on every path except the repeated claim.
    """

    outcome: AttributionOutcome
    confidence: float
    method: BindingMethod


def reference_steps(writes: tuple[SubmittedWrite, ...]) -> tuple[ExpectedStep, ...]:
    """Walk a drawn sequence and return what the pair ought to hold after each write.

    This is the independent model the stored state is compared against. Three
    branches, matching the three the write path takes: a pair holding no current
    version takes the first-write path; a submission carrying the same method and
    no greater confidence is a repeated claim and changes nothing; anything else
    supersedes, and the successor carries the greater of the two confidences.
    """
    steps: list[ExpectedStep] = []
    current: ExpectedStep | None = None
    for write in writes:
        if current is None:
            current = ExpectedStep(
                outcome=AttributionOutcome.INSERTED,
                confidence=write.confidence,
                method=write.method,
            )
        elif write.method is current.method and write.confidence <= current.confidence:
            current = ExpectedStep(
                outcome=AttributionOutcome.UNCHANGED,
                confidence=current.confidence,
                method=current.method,
            )
        else:
            current = ExpectedStep(
                outcome=AttributionOutcome.SUPERSEDED,
                confidence=max(write.confidence, current.confidence),
                method=write.method,
            )
        steps.append(current)
    return tuple(steps)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def confidences() -> st.SearchStrategy[float]:
    """Draw one confidence from the closed unit interval, ends included.

    The two ends are a branch of their own rather than left to the real generator
    to reach, because the requirement states a closed interval and a check
    constraint admits both ends: a property that never submitted either would say
    nothing about the boundary it is partly about.
    """
    return st.one_of(
        st.sampled_from(INTERVAL_ENDS),
        st.floats(
            min_value=CONFIDENCE_FLOOR,
            max_value=CONFIDENCE_CEILING,
            allow_nan=False,
            allow_infinity=False,
        ),
    )


def confidence_pools() -> st.SearchStrategy[tuple[float, ...]]:
    """Draw the small set of confidences one example's writes are sampled from.

    Sampling from a pool is what makes the two paths this property is most at risk
    from ordinary: the repeated claim needs a value the pair already holds, and the
    equal-confidence method change needs the same. A pool of one makes every write
    after the first either a repeated claim or a bare method change, which is the
    most concentrated form of both.

    Distinctness is not asked for. A pool holding one value twice is a pool of one
    with a weight, which is a fine thing to draw, and asking for distinctness makes
    the two interval ends collide with each other and discards whole examples for
    it rather than drawing something usable.
    """
    return st.lists(confidences(), min_size=1, max_size=MAX_POOL_SIZE).map(tuple)


def submissions(pool: tuple[float, ...]) -> st.SearchStrategy[SubmittedWrite]:
    """Draw one submission: any detection method, and a confidence from the pool."""
    return st.builds(SubmittedWrite, st.sampled_from(BindingMethod), st.sampled_from(pool))


@st.composite
def binding_write_sequences(draw: st.DrawFn) -> WriteSequence:
    """Draw 1 to 20 repeated writes for one Artifact and Client pair.

    The pool is drawn first and every write samples its confidence from it, so a
    sequence is by construction full of values the pair has already seen rather
    than twenty unrelated reals.

    The length is drawn as a number before the submissions are drawn, rather than
    left to a list's own sizing, because a list generator concentrates near its
    lower bound and the long histories are the interesting end: a twenty-write
    example is where a repeated claim, an equal-confidence method change, and a
    weaker submission all appear in one history.
    """
    pool = draw(confidence_pools())
    size = draw(st.integers(min_value=MIN_WRITES, max_value=MAX_WRITES))
    writes = [draw(submissions(pool)) for _ in range(size)]
    return WriteSequence(writes=tuple(writes))


# ---------------------------------------------------------------------------
# The cluster the history is stored on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredVersion:
    """One stored version of a pair, read back column by column."""

    id: UUID
    method: str
    confidence: float
    superseded_by: UUID | None

    @property
    def current(self) -> bool:
        """Whether this row is the live claim for its pair."""
        return self.superseded_by is None


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

    def current_of(self, artifact_id: UUID, client_id: UUID) -> tuple[StoredVersion, ...]:
        """Every unsuperseded version of one pair, which ought to be exactly one."""
        return tuple(
            StoredVersion(
                id=_as_uuid(row[0]),
                method=str(row[1]),
                confidence=float(str(row[2])),
                superseded_by=None,
            )
            for row in self.rows_of(SELECT_CURRENT_OF_PAIR, (artifact_id, client_id))
        )

    def history_of(self, artifact_id: UUID, client_id: UUID) -> tuple[StoredVersion, ...]:
        """Every stored version of one pair, oldest first."""
        return tuple(
            StoredVersion(
                id=_as_uuid(row[0]),
                method=str(row[1]),
                confidence=float(str(row[2])),
                superseded_by=None if row[3] is None else _as_uuid(row[3]),
            )
            for row in self.rows_of(SELECT_PAIR_HISTORY, (artifact_id, client_id))
        )


def _as_uuid(value: object) -> UUID:
    """Narrow a stored identifier, refusing anything else."""
    return value if isinstance(value, UUID) else UUID(str(value))


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store whose connections see that schema.

    Every migration is applied because the partial unique index, the total closure
    check, and the supersession Event category all arrive with the attribution
    migration, and the self-referencing constraint that would refuse the ordered
    pair of statements is dropped by the protection migration.

    Module scope is what keeps the schema cost paid once: examples are isolated
    from each other by a tenant of their own rather than by a schema of their own.
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


def submission_of(
    write: SubmittedWrite,
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


# ---------------------------------------------------------------------------
# The coverage record
# ---------------------------------------------------------------------------


def size_band(size: int) -> str:
    """Which part of the length range an example drew, for the coverage record."""
    if size == MIN_WRITES:
        return "1"
    if size <= 5:
        return "2-5"
    if size <= 12:
        return "6-12"
    return f"13-{MAX_WRITES}"


def count_band(count: int) -> str:
    """How often an outcome or a shape occurred, for the coverage record."""
    if count == 0:
        return "none"
    if count == 1:
        return "1"
    if count <= 4:
        return "2-4"
    return "5+"


def submitted_ends(writes: tuple[SubmittedWrite, ...]) -> str:
    """Which ends of the closed interval an example submitted, for the coverage record."""
    floor, ceiling = INTERVAL_ENDS
    submitted = {write.confidence for write in writes}
    reached = [
        name for name, value in (("floor", floor), ("ceiling", ceiling)) if value in submitted
    ]
    return "+".join(reached) if reached else "neither"


def repeated_claims(writes: tuple[SubmittedWrite, ...], steps: tuple[ExpectedStep, ...]) -> int:
    """How many submissions restated a claim the pair already held exactly."""
    return sum(
        1
        for index, write in enumerate(writes)
        if index > 0
        and steps[index].outcome is AttributionOutcome.UNCHANGED
        and write.confidence == steps[index - 1].confidence
        and write.method is steps[index - 1].method
    )


def equal_confidence_method_changes(
    writes: tuple[SubmittedWrite, ...],
    steps: tuple[ExpectedStep, ...],
) -> int:
    """How many submissions changed only the method, at the confidence already held."""
    return sum(
        1
        for index, write in enumerate(writes)
        if index > 0
        and write.method is not steps[index - 1].method
        and write.confidence == steps[index - 1].confidence
    )


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 14: For any sequence of repeated Client_Binding writes
# for the same Artifact and Client with varying confidence values, exactly one
# unsuperseded Attribution_Version exists per Artifact and Client pair, it holds
# the maximum submitted confidence value, and every stored confidence lies in the
# closed interval from 0.0 to 1.0.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(sequence=binding_write_sequences())
def test_one_current_version_holds_the_strongest_confidence_ever_submitted(
    cluster: Cluster, sequence: WriteSequence
) -> None:
    steps = reference_steps(sequence.writes)
    supersessions = sum(1 for step in steps if step.outcome is AttributionOutcome.SUPERSEDED)
    unchanged = sum(1 for step in steps if step.outcome is AttributionOutcome.UNCHANGED)
    event(f"writes={size_band(sequence.size)}")
    event(f"supersessions={count_band(supersessions)}")
    event(f"repeated claims={count_band(unchanged)}")
    event(f"identical resubmissions={count_band(repeated_claims(sequence.writes, steps))}")
    event(
        "equal-confidence method changes="
        f"{count_band(equal_confidence_method_changes(sequence.writes, steps))}"
    )
    event(f"interval ends submitted={submitted_ends(sequence.writes)}")
    event(f"distinct methods={len({write.method for write in sequence.writes})}")

    owner = cluster.tenant()
    context = cluster.context(cluster.session(owner))
    artifact_id = uuid4()

    strongest = CONFIDENCE_FLOOR
    for index, write in enumerate(sequence.writes):
        strongest = max(strongest, write.confidence)
        written = write_attribution(
            cluster.store,
            submission_of(write, artifact_id, owner),
            context=context,
        )

        # The write path took the branch the model says it should have, so the
        # state asserted below was reached the way the property describes.
        assert written.outcome is steps[index].outcome, (
            f"write {index} was reported as {written.outcome} where the drawn sequence "
            f"makes it {steps[index].outcome}"
        )

        # Requirements 12.7 and 43.5, after every write rather than only at the
        # end: the partial uniqueness admits exactly one live claim for the pair.
        live = cluster.current_of(artifact_id, owner)
        assert len(live) == 1, (
            f"the pair holds {len(live)} unsuperseded version(s) after write {index}"
        )
        assert live[0].id == written.version_id
        assert live[0].method == steps[index].method.value

        # The retained confidence, asserted both against the walked model and
        # against the maximum of everything submitted so far.
        assert live[0].confidence == steps[index].confidence, (
            f"the pair holds {live[0].confidence} after write {index} where the model "
            f"makes it {steps[index].confidence}"
        )
        assert live[0].confidence == strongest, (
            f"the pair holds {live[0].confidence} after write {index} where the strongest "
            f"confidence submitted is {strongest}"
        )
        assert written.confidence == strongest

    # The history the pair accumulated: one row per write that changed something,
    # exactly one of them current, and every stored confidence inside the closed
    # interval of Requirement 12.5.
    stored = cluster.history_of(artifact_id, owner)
    assert len(stored) == supersessions + 1, (
        f"the pair holds {len(stored)} version(s) where {supersessions} supersession(s) "
        "and one first write were expected"
    )
    assert sum(1 for version in stored if version.current) == 1
    for version in stored:
        assert CONFIDENCE_FLOOR <= version.confidence <= CONFIDENCE_CEILING, (
            f"a stored version carries confidence {version.confidence}, outside the "
            "closed unit interval"
        )
    assert max(version.confidence for version in stored) == strongest
