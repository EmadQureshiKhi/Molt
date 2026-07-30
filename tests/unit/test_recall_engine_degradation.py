"""The recall path against a scripted cursor: what it answers, and what it owes.

Four claims, and none of them needs a socket.

**A page is composed from the statement's own ordering, minus the row that only
carries a measurement.** The statement returns the admitted results and one
flagged row whose presence is how the floor's exclusion count stays observable on
a page that admitted nothing. That row is not a result and never reaches a caller,
so it is asserted absent from the answer and its tally is asserted present in the
measurement.

**The floor excludes and the standing orders, and they are separate claims.** A
Learned_Procedure below the configured floor is not in the answer, is still in the
returned rows, and is counted. A Learned_Procedure above it is in the answer and
carries its standing outward, because the confidence is what an agent weighs a
procedure by after distance has already ranked it.

**What the query owes happens after the answer exists.** One recall Event naming
the query text, the returned identifiers, and the distances, and one retrieval
record per returned procedure and per no other kind. Both are asserted from what
the scripted cursor was sent rather than from a claim about intent.

**An unreachable cluster costs a result set and nothing else.** Recall runs on the
agent's critical path, so a refusing connection, a refusing provider, and a
Session identifier no row holds each produce an empty answer, a measurement, and
no exception. That is Requirement 13.8 asserted where it is decided: the hook can
only inject nothing and exit zero if this module declines to raise.

**Validates: Requirements 13.1, 13.2, 13.3, 13.7, 13.8, 49.3, 49.8, 49.9, 49.10**
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt import telemetry
from molt.collector.routes import RecallQuery
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind
from molt.models.event import parse_timestamp
from molt.recall import (
    CANDIDATE_POOL_CEILING,
    CANDIDATE_POOL_FLOOR,
    MAX_RESULT_LIMIT,
    OVER_FETCH_FACTOR,
    POOL_SATURATED_METRIC,
    RECALL_FLOOR_EXCLUSIONS_METRIC,
    RECALL_UNAVAILABLE_METRIC,
    QueryEmbedder,
    RecallEngine,
    candidate_pool_for,
)
from molt.store import Connection, MemoryStore
from molt.store.embeddings import RECALL_STATEMENT

# The fragments the script matches statements on, each naming one read.
PRINCIPAL_FRAGMENT: Final[str] = "FROM session AS s JOIN client AS c"
PAGE_FRAGMENT: Final[str] = "WITH candidates AS"
APPEND_FRAGMENT: Final[str] = "INSERT INTO ledger"

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)

# The identities every example names. Fixed at module scope so an expectation and
# the value it is about are the same value.
SESSION_ID: Final[UUID] = uuid4()
CLIENT_ID: Final[UUID] = uuid4()
OTHER_CLIENT_ID: Final[UUID] = uuid4()
EVENT_ARTIFACT: Final[UUID] = uuid4()
PROCEDURE_ARTIFACT: Final[UUID] = uuid4()
EXCLUDED_ARTIFACT: Final[UUID] = uuid4()

AGENT_CLI: Final[str] = "a-coding-agent"
MACHINE_ID: Final[str] = "a-machine"

# The standing the admitted procedure carries and the standing the excluded one
# carries, either side of the floor under test.
RECALL_FLOOR: Final[float] = 0.15
ADMITTED_CONFIDENCE: Final[float] = 0.80
EXCLUDED_CONFIDENCE: Final[float] = 0.05

# How many procedures the statement reports the floor excluded. Larger than the
# one flagged row the page carries, which is the whole reason the tally is a
# column rather than a count of returned rows.
FLOOR_EXCLUSIONS: Final[int] = 3

# The distances the two admitted rows carry, ascending, so the answer's order is
# checkable against the order the statement produced.
NEAR_DISTANCE: Final[float] = 0.10
FAR_DISTANCE: Final[float] = 0.32
EXCLUDED_DISTANCE: Final[float] = 0.90

# The row the append statement answers with, of the width that statement selects.
APPENDED_ROW: Final[tuple[object, ...]] = (1, "b" * 64, "0" * 64, "c" * 64)

# The bound the examples ask for, distinct from the module's own default so an
# assertion about it is not satisfied by a coincidence.
PAGE: Final[int] = 4


def unit_query_vector() -> tuple[float, ...]:
    """A unit vector of the fixed width, which is what the read is held to.

    One component carries the whole length. That is enough: this module asserts
    what the engine does with a page rather than which page a corpus produces, and
    a vector the width check refuses would never reach the statement at all.
    """
    return (1.0,) + (0.0,) * (EMBEDDING_DIMENSION - 1)


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()
    error: Exception | None = None


@dataclass(slots=True)
class Script:
    """The answers a connection hands out, consumed in the order they match."""

    answers: list[Answer] = field(default_factory=list)
    sent: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    armed: tuple[tuple[object, ...], ...] = ()

    @property
    def statements(self) -> list[str]:
        """Every statement the script was sent, in order."""
        return [query for query, _ in self.sent]

    def holding(self, fragment: str) -> list[tuple[str, tuple[object, ...] | None]]:
        """Every statement sent that holds a fragment, with its bound values."""
        return [entry for entry in self.sent if fragment in entry[0]]

    def take(self, query: str) -> Answer | None:
        """The next answer matching a statement, removed from the script."""
        for index, answer in enumerate(self.answers):
            if answer.fragment in query:
                return self.answers.pop(index)
        return None


class ScriptedCursor:
    """A cursor answering from a script and recording what it was sent."""

    def __init__(self, script: Script) -> None:
        self._script = script

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then raise or arm rows as the script says."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        if answer is None:
            self._script.armed = ()
            return None
        if answer.error is not None:
            raise answer.error
        self._script.armed = answer.rows
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or None when the statement armed none."""
        rows = self._script.armed
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._script.armed)

    def close(self) -> None:
        """Release this cursor, which the script needs no record of."""


class ScriptedConnection:
    """A connection handing out scripted cursors over one shared script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Open a recording cursor over this connection's script."""
        return ScriptedCursor(self.script)

    def close(self) -> None:
        """Mark this connection closed, so the pool may discard it."""
        self.closed = True


class RefusingConnections:
    """A connection factory that refuses, as an unreachable cluster does."""

    def __init__(self) -> None:
        self.attempts = 0

    def open(self) -> Connection:
        """Refuse to connect, counting the attempt."""
        self.attempts += 1
        raise OSError("the cluster did not accept a connection")


class RefusingProvider:
    """An embedding surface that will not answer, as an unavailable provider does."""

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Refuse the call, naming how many texts were asked about."""
        raise RuntimeError(f"the provider refused {len(texts)} text(s)")


@dataclass(slots=True)
class StubProvider:
    """An embedding surface answering one fixed unit vector, recording its calls."""

    asked: list[tuple[str, ...]] = field(default_factory=list)

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Answer one unit vector per text, in the input order."""
        self.asked.append(tuple(texts))
        return [unit_query_vector() for _ in texts]


@dataclass(slots=True)
class RecordedRetrievals:
    """The retrieval records the Confidence_Tracker seam was handed."""

    seen: list[tuple[UUID, UUID]] = field(default_factory=list)

    def record(self, procedure_id: UUID, session_id: UUID) -> None:
        """Keep one retrieval, as the tracker's own recorder would write one."""
        self.seen.append((procedure_id, session_id))


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def principal_row() -> tuple[object, ...]:
    """The row the tenancy read answers with, of the width that statement selects."""
    return (CLIENT_ID, AGENT_CLI, MACHINE_ID, RETENTION)


def page_rows(*, exclusions: int = FLOOR_EXCLUSIONS) -> tuple[tuple[object, ...], ...]:
    """One page: two admitted results, then the one flagged row carrying the tally.

    The flagged row is a Learned_Procedure below the floor. It is last, as the
    statement's own ordering puts it, and it is a row rather than a result.
    """
    return (
        (
            EVENT_ARTIFACT,
            ArtifactKind.EVENT.value,
            CLIENT_ID,
            NEAR_DISTANCE,
            SESSION_ID,
            MACHINE_ID,
            MOMENT,
            "succeeded",
            "tool_call",
            "the earlier attempt",
            None,
            False,
            exclusions,
        ),
        (
            PROCEDURE_ARTIFACT,
            ArtifactKind.DERIVED_ARTIFACT.value,
            CLIENT_ID,
            FAR_DISTANCE,
            SESSION_ID,
            MACHINE_ID,
            MOMENT,
            "failed",
            "learned_procedure",
            "the distilled procedure",
            ADMITTED_CONFIDENCE,
            False,
            exclusions,
        ),
        (
            EXCLUDED_ARTIFACT,
            ArtifactKind.DERIVED_ARTIFACT.value,
            CLIENT_ID,
            EXCLUDED_DISTANCE,
            SESSION_ID,
            MACHINE_ID,
            MOMENT,
            "abandoned",
            "learned_procedure",
            "the discredited procedure",
            EXCLUDED_CONFIDENCE,
            True,
            exclusions,
        ),
    )


def answered_script(*, page: tuple[tuple[object, ...], ...] | None = None) -> Script:
    """A script answering the tenancy read, the page, and the recall Event."""
    return Script(
        answers=[
            Answer(PRINCIPAL_FRAGMENT, (principal_row(),)),
            Answer(PAGE_FRAGMENT, page_rows() if page is None else page),
            Answer(APPEND_FRAGMENT, (APPENDED_ROW,)),
        ]
    )


def build_engine(
    store: MemoryStore,
    *,
    provider: QueryEmbedder | None = None,
    retrievals: RecordedRetrievals | None = None,
) -> RecallEngine:
    """The engine under test, with the floor stated rather than resolved."""
    return RecallEngine(
        store,
        StubProvider() if provider is None else provider,
        recall_floor=RECALL_FLOOR,
        retrievals=None if retrievals is None else retrievals.record,
        clock=lambda: MOMENT,
    )


@pytest.fixture(autouse=True)
def fresh_telemetry() -> None:
    """Discard the process-wide counters, so a measurement read here is this test's."""
    telemetry.reset()


def counter_for(name: str) -> float:
    """The value the process-wide instance holds for one undimensioned metric."""
    return telemetry.current().counters().get((name, ()), 0.0)


# ---------------------------------------------------------------------------
# The page, and the row that is not a result
# ---------------------------------------------------------------------------


def test_the_page_is_the_admitted_rows_in_the_order_the_statement_produced() -> None:
    """Two results, ascending by distance, and the flagged row is not among them."""
    script = answered_script()
    engine = build_engine(build_store(script))

    results = engine.recall("a description of the intended action", PAGE, session_id=SESSION_ID)

    assert [result.artifact_id for result in results] == [EVENT_ARTIFACT, PROCEDURE_ARTIFACT]
    assert [result.distance for result in results] == [NEAR_DISTANCE, FAR_DISTANCE]
    assert EXCLUDED_ARTIFACT not in {result.artifact_id for result in results}


def test_each_result_carries_the_originating_session_machine_instant_and_outcome() -> None:
    """Requirement 13.2 and 13.3 are fields on the answer rather than a later lookup."""
    engine = build_engine(build_store(answered_script()))

    results = engine.recall("a description", PAGE, session_id=SESSION_ID)

    first = results[0]
    assert first.session_id == SESSION_ID
    assert first.machine_id == MACHINE_ID
    assert first.occurred_at == MOMENT
    assert first.outcome == "succeeded"
    assert first.excerpt == "the earlier attempt"


def test_the_admitted_procedure_carries_its_standing_and_the_event_carries_none() -> None:
    """A standing is a procedure's property, and the schema holds that as an equivalence."""
    engine = build_engine(build_store(answered_script()))

    results = engine.recall("a description", PAGE, session_id=SESSION_ID)

    event_result, procedure_result = results
    assert event_result.confidence is None
    assert not event_result.is_procedure
    assert procedure_result.confidence == ADMITTED_CONFIDENCE
    assert procedure_result.is_procedure


def test_the_floor_exclusions_are_measured_from_the_tally_rather_than_the_page() -> None:
    """The tally counts every excluded procedure, not the ones that would have fitted."""
    engine = build_engine(build_store(answered_script()))

    engine.recall("a description", PAGE, session_id=SESSION_ID)

    assert counter_for(RECALL_FLOOR_EXCLUSIONS_METRIC) == float(FLOOR_EXCLUSIONS)


def test_a_page_admitting_nothing_still_reports_what_the_floor_excluded() -> None:
    """The one flagged row exists for this case, which is where the floor matters most."""
    flagged_only = (page_rows()[2],)
    engine = build_engine(build_store(answered_script(page=flagged_only)))

    results = engine.recall("a description", PAGE, session_id=SESSION_ID)

    assert results == ()
    assert counter_for(RECALL_FLOOR_EXCLUSIONS_METRIC) == float(FLOOR_EXCLUSIONS)


def test_the_floor_and_the_bound_reach_the_statement_as_bound_values() -> None:
    """The floor is applied by the cluster inside the predicate, not here after the fact."""
    script = answered_script()
    engine = build_engine(build_store(script))

    engine.recall("a description", PAGE, session_id=SESSION_ID)

    sent = script.holding(PAGE_FRAGMENT)
    assert len(sent) == 1
    statement, bound = sent[0]
    assert statement == RECALL_STATEMENT
    assert bound is not None
    assert bound[3] == [CLIENT_ID]
    assert bound[-2] == RECALL_FLOOR
    assert bound[-1] == PAGE
    assert bound[2] == candidate_pool_for(PAGE)


def test_a_presented_client_set_is_widened_by_the_asking_sessions_own_client() -> None:
    """A configured entitlement is not narrowed by the Session it is exercised in."""
    script = answered_script()
    engine = build_engine(build_store(script))

    engine.recall(
        "a description",
        PAGE,
        permitted=[OTHER_CLIENT_ID],
        session_id=SESSION_ID,
    )

    bound = script.holding(PAGE_FRAGMENT)[0][1]
    assert bound is not None
    assert bound[3] == [OTHER_CLIENT_ID, CLIENT_ID]


# ---------------------------------------------------------------------------
# What the answer owes, after the answer exists
# ---------------------------------------------------------------------------


def test_one_recall_event_records_the_query_text_the_identifiers_and_the_distances() -> None:
    """Requirement 13.7, asserted from what the cursor was sent."""
    script = answered_script()
    engine = build_engine(build_store(script))

    engine.recall("a description of the intended action", PAGE, session_id=SESSION_ID)

    appended = script.holding(APPEND_FRAGMENT)
    assert len(appended) == 1
    bound = appended[0][1]
    assert bound is not None
    payload = [value for value in bound if isinstance(value, str) and "artifact_ids" in value]
    assert payload, "the appended Event should carry the recall payload"
    recorded = payload[0]
    assert "a description of the intended action" in recorded
    assert str(EVENT_ARTIFACT) in recorded
    assert str(PROCEDURE_ARTIFACT) in recorded
    assert str(EXCLUDED_ARTIFACT) not in recorded
    assert str(NEAR_DISTANCE) in recorded


def test_the_recall_event_is_appended_after_the_page_has_been_read() -> None:
    """The Event is off the latency path, which is an order rather than an intention."""
    script = answered_script()
    engine = build_engine(build_store(script))

    engine.recall("a description", PAGE, session_id=SESSION_ID)

    statements = script.statements
    page_at = next(index for index, sent in enumerate(statements) if PAGE_FRAGMENT in sent)
    append_at = next(index for index, sent in enumerate(statements) if APPEND_FRAGMENT in sent)
    assert page_at < append_at


def test_one_retrieval_is_recorded_per_returned_procedure_and_for_no_other_kind() -> None:
    """Requirement 49.3: retrieval is recorded, and being retrieved moves nothing."""
    retrievals = RecordedRetrievals()
    engine = build_engine(build_store(answered_script()), retrievals=retrievals)

    engine.recall("a description", PAGE, session_id=SESSION_ID)

    assert retrievals.seen == [(PROCEDURE_ARTIFACT, SESSION_ID)]


def test_a_failing_retrieval_record_does_not_fail_the_answer() -> None:
    """The bookkeeping is owed by the answer rather than a condition of it."""

    def refuse(procedure_id: UUID, session_id: UUID) -> None:
        raise RuntimeError(f"the tracker refused {procedure_id} for {session_id}")

    engine = RecallEngine(
        build_store(answered_script()),
        StubProvider(),
        recall_floor=RECALL_FLOOR,
        retrievals=refuse,
        clock=lambda: MOMENT,
    )

    results = engine.recall("a description", PAGE, session_id=SESSION_ID)

    assert len(results) == 2


# ---------------------------------------------------------------------------
# Degradation, which is what recall does instead of failing
# ---------------------------------------------------------------------------


def test_an_unreachable_cluster_answers_an_empty_page_and_raises_nothing() -> None:
    """Requirement 13.8, asserted where it is decided rather than at the hook."""
    connections = RefusingConnections()
    store = MemoryStore(
        connect_with=connections.open,
        sleep=lambda _: None,
        jitter=lambda low, _: low,
    )
    engine = build_engine(store)

    results = engine.recall("a description", PAGE, session_id=SESSION_ID)

    assert results == ()
    assert connections.attempts > 0
    assert counter_for(RECALL_UNAVAILABLE_METRIC) > 0.0


def test_an_unavailable_embedding_provider_answers_an_empty_page() -> None:
    """A provider outage costs the query its answer and the agent no step."""
    engine = build_engine(build_store(answered_script()), provider=RefusingProvider())

    results = engine.recall("a description", PAGE, session_id=SESSION_ID)

    assert results == ()
    assert counter_for(RECALL_UNAVAILABLE_METRIC) > 0.0


def test_a_session_no_row_holds_and_no_presented_client_answers_nothing() -> None:
    """With no tenancy resolved there is nothing the caller is entitled to see."""
    script = Script(answers=[Answer(PRINCIPAL_FRAGMENT, ())])
    engine = build_engine(build_store(script))

    results = engine.recall("a description", PAGE, session_id=SESSION_ID)

    assert results == ()
    assert not script.holding(PAGE_FRAGMENT)


def test_the_collector_seam_answers_documents_the_hook_can_read_back() -> None:
    """The seam's shape is the response envelope's, and the instant round-trips."""
    engine = build_engine(build_store(answered_script()))
    query = RecallQuery(query_text="a description", limit=PAGE, session_id=SESSION_ID)

    answer = engine.search(query)

    assert len(answer.results) == 2
    first = answer.results[0]
    assert first["artifact_id"] == str(EVENT_ARTIFACT)
    assert first["session_id"] == str(SESSION_ID)
    assert first["distance"] == NEAR_DISTANCE
    assert first["outcome"] == "succeeded"
    rendered_instant = first["occurred_at"]
    assert isinstance(rendered_instant, str)
    assert parse_timestamp(rendered_instant) == MOMENT
    assert "confidence" not in first
    assert answer.results[1]["confidence"] == ADMITTED_CONFIDENCE
    assert not answer.halt.halted


# ---------------------------------------------------------------------------
# The candidate pool, which is the trade the staged statement makes
# ---------------------------------------------------------------------------


def test_the_candidate_pool_scales_with_the_bound_between_a_floor_and_a_ceiling() -> None:
    """A pool sized from the bound, never so narrow as to be filtered away."""
    assert candidate_pool_for(1) == CANDIDATE_POOL_FLOOR
    assert candidate_pool_for(MAX_RESULT_LIMIT) == min(
        CANDIDATE_POOL_CEILING, MAX_RESULT_LIMIT * OVER_FETCH_FACTOR
    )
    assert candidate_pool_for(CANDIDATE_POOL_CEILING) == CANDIDATE_POOL_CEILING
    assert candidate_pool_for(0) == CANDIDATE_POOL_FLOOR


def test_a_bound_outside_the_admitted_range_is_moved_into_it_rather_than_refused() -> None:
    """A strange bound still gets an answer, because this read is on a person's path."""
    script = answered_script()
    engine = build_engine(build_store(script))

    engine.recall("a description", MAX_RESULT_LIMIT * 10, session_id=SESSION_ID)

    bound = script.holding(PAGE_FRAGMENT)[0][1]
    assert bound is not None
    assert bound[-1] == MAX_RESULT_LIMIT


def test_a_saturated_pool_that_came_back_short_is_measured() -> None:
    """Under-recall from the pool's horizon is visible rather than silent."""
    pool = candidate_pool_for(PAGE)
    crowded = (
        (
            EXCLUDED_ARTIFACT,
            ArtifactKind.DERIVED_ARTIFACT.value,
            CLIENT_ID,
            EXCLUDED_DISTANCE,
            SESSION_ID,
            MACHINE_ID,
            MOMENT,
            "abandoned",
            "learned_procedure",
            "a discredited procedure",
            EXCLUDED_CONFIDENCE,
            True,
            pool,
        ),
    )
    engine = build_engine(build_store(answered_script(page=crowded)))

    results = engine.recall("a description", PAGE, session_id=SESSION_ID)

    assert results == ()
    assert counter_for(POOL_SATURATED_METRIC) > 0.0
