"""Pre-action memory queries that return prior attempts and their outcomes.

This is the read an agent performs before it acts, and it is the one read in this
system that sits on a person's critical path. Everything about the shape below
follows from that.

**One embedding call, one query, and the recording afterwards.** The query text is
embedded once through the configured provider, one statement answers the page, and
the two things the page obliges — the recall Event and one retrieval record per
returned Learned_Procedure — happen after the answer has been composed. A caller
waits for the provider and the cluster once each and for nothing else
(Requirements 13.1, 13.7, 49.3).

**Tenancy is resolved here rather than presented.** The recall request body names a
Session and carries no Client, which is deliberate: a request that could name its
own permitted Clients would be a request that could widen its own reach. The
Clients a caller may see are read from the stored Session row, and the filter over
them is applied inside SQL over unsuperseded Attribution_Versions, so a Client
whose claim on an Artifact was closed no longer reaches that Artifact's vector
(Requirements 13.2, 13.4, 43.6). A caller with its own configured permitted set,
which is what the tool server and the command line have, presents it and it is
used alongside the Session's own Client rather than instead of it.

**Confidence filters and orders, and the two are different claims.** A
Learned_Procedure below the configured recall floor is excluded, and a procedure
above it is ranked. The floor itself is the Confidence_Tracker's: this module
imports the tracker's policy rather than naming the configuration key or restating
the comparison, because a floor stated in two places is a floor the recall
predicate and the tracker can disagree about while both believe they agree. The
exclusion happens inside the statement's predicate rather
than after truncation, so a low-standing procedure does not silently shorten a
k-result page — it gives up its position to the next admissible result instead.
The ranking is the tie-break: at equal cosine distance the more-trusted procedure
comes first, and the Artifact identifier is the final key, so the ordering is
total and two runs over one corpus agree row for row. Nothing is deleted by
either: an excluded procedure stays stored and stays inside erasure's reach
(Requirements 49.8, 49.9, 49.10).

**An unreachable cluster costs the agent a result set, not a step.** Recall is
advisory. A failure to reach the cluster, a failure to reach the embedding
provider, and a Session identifier no Session holds all produce an empty answer,
a measurement, and a log record, and the caller proceeds. That is Requirement
13.8 read as what it is: the hook returns an empty injection and exits zero, which
it can only do if this module declines to raise.

**What the page trades for being index-served, stated rather than hidden.** On
this cluster a predicate on any column other than the vector takes the plan off
the distributed vector index — the index is created over the vector alone, with no
prefix columns, and it serves the ordering expression and nothing else — and the
tenancy admission is exactly such a predicate. The statement therefore ranks a
candidate pool by the ordering expression alone and admits from that pool, so the
page is the nearest k the caller may see *within the pool* rather than within the
corpus. The cap is sixteen candidates per result asked for, never fewer than two
hundred and fifty-six and never more than twenty thousand: sixteen answers a
caller entitled to a sixteenth of the corpus fully in the ordinary case, the floor
keeps a small `k` from producing a pool too narrow to survive any filtering, and
the ceiling keeps the pool from growing into the scan the staging exists to avoid.
A page that fills the pool and still comes back short is measured, so under-recall
from the pool's horizon is visible rather than silent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Final, Protocol
from uuid import UUID, uuid4

from molt.collector.routes import (
    MAX_RECALL_RESULTS,
    HaltReport,
    RecallAnswer,
    RecallQuery,
)
from molt.confidence import ConfidencePolicy, record_retrieval
from molt.config.resolve import Configuration
from molt.models.artifact import ArtifactKind, DerivedArtifactKind
from molt.models.event import (
    Event,
    EventCategory,
    JsonObject,
    JsonValue,
    format_timestamp,
)
from molt.store import MemoryStore
from molt.store.chain import LedgerAppend, append
from molt.store.embeddings import (
    DEFAULT_EXCERPT_CHARACTERS,
    PrincipalScope,
    RecallRow,
    principal_scope,
    recall_page,
)
from molt.telemetry import Severity, log, metric
from molt.telemetry.inventory import UNIT_MILLISECONDS

__all__ = [
    "CANDIDATE_POOL_CEILING",
    "CANDIDATE_POOL_FLOOR",
    "COMPONENT",
    "DEFAULT_RESULT_LIMIT",
    "MAX_RESULT_LIMIT",
    "OVER_FETCH_FACTOR",
    "POOL_SATURATED_METRIC",
    "PROCEDURE_KIND",
    "RECALL_EVENT_FAILURE_METRIC",
    "RECALL_FLOOR_EXCLUSIONS_METRIC",
    "RECALL_LATENCY_METRIC",
    "RECALL_QUERIES_METRIC",
    "RECALL_UNAVAILABLE_METRIC",
    "QueryEmbedder",
    "RecallEngine",
    "Recalled",
    "RetrievalRecorder",
    "candidate_pool_for",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "recall"

# How many results a caller gets when it names no bound, and the ceiling it may
# not ask past. The ceiling is the route's own, so a query that arrived over the
# Collector and one raised in this process are bounded by one number.
DEFAULT_RESULT_LIMIT: Final[int] = 10
MAX_RESULT_LIMIT: Final[int] = MAX_RECALL_RESULTS

# How many Embeddings the ranking stage considers per result asked for, and the
# bounds that multiple is held between.
#
# This is the whole of the trade the staged statement makes. The ordering is
# served by the index only while it is the query's sole restriction, so the
# tenancy admission has to be applied to a pool the ordering already produced.
# Over-fetching sixteen candidates per result means a caller permitted a
# sixteenth of the corpus is answered fully in the ordinary case, and the floor of
# two hundred and fifty-six keeps a small `k` from producing a pool too narrow to
# survive any filtering at all. The ceiling is what keeps the promise the pool
# exists to make: a pool wide enough to be a scan would have given back the
# latency the staging bought.
OVER_FETCH_FACTOR: Final[int] = 16
CANDIDATE_POOL_FLOOR: Final[int] = 256
CANDIDATE_POOL_CEILING: Final[int] = 20000

# The kind whose results carry a standing, and so the only kind the floor and the
# tie-break apply to.
PROCEDURE_KIND: Final[str] = DerivedArtifactKind.LEARNED_PROCEDURE.value

# The measurements this path emits.
#
# The exclusion count is the one Requirement 49.9 is observable through: a
# procedure excluded by the floor is still stored, so nothing else in the system
# would show that recall stopped returning it.
#
# The saturation count is what makes the candidate pool's trade auditable. It is
# emitted only when the pool filled and the page still came back short, which is
# the one condition under which a result the caller was entitled to may have been
# below the pool's horizon.
#
# Unreachability is measured because recall answers empty rather than failing, so
# without a measurement a cluster outage would look like a corpus with no similar
# content in it.
RECALL_QUERIES_METRIC: Final[str] = "recall.queries"
RECALL_LATENCY_METRIC: Final[str] = "recall.latency_ms"
RECALL_FLOOR_EXCLUSIONS_METRIC: Final[str] = "procedure.recall_floor_exclusions"
POOL_SATURATED_METRIC: Final[str] = "recall.candidate_pool_saturated"
RECALL_UNAVAILABLE_METRIC: Final[str] = "recall.unavailable"
RECALL_EVENT_FAILURE_METRIC: Final[str] = "recall.event_write_failures"

# The latency is declared in milliseconds, and the monotonic reading is seconds.
_MILLISECONDS_PER_SECOND: Final[float] = 1000.0

# The keys the recall Event's payload records, and the keys one result carries in
# the response envelope. The response keys are the ones the Capture_Hook reads a
# result back from, so they are named here rather than spelled inline.
_PAYLOAD_QUERY: Final[str] = "query_text"
_PAYLOAD_IDENTIFIERS: Final[str] = "artifact_ids"
_PAYLOAD_DISTANCES: Final[str] = "distances"
_PAYLOAD_LIMIT: Final[str] = "limit"
_PAYLOAD_EXCLUSIONS: Final[str] = "floor_exclusions"


class QueryEmbedder(Protocol):
    """The Embedder surface this path uses, which is one call and no more.

    Narrow on purpose. Recall needs one vector for one text, and stating that as a
    protocol rather than importing the Embedder keeps the engine drivable by a
    provider stub and keeps the dependency pointing one way.
    """

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one unit vector per text, in the input order."""
        ...


# One retrieval of one Learned_Procedure by one consuming Session, as the
# Confidence_Tracker records it. It is a seam rather than a direct call because
# the tracker is a separate component with its own transaction discipline, and
# because recall must keep answering when recording a retrieval fails. A caller
# naming none gets the tracker's own recorder, so the default is the real write
# rather than silence.
RetrievalRecorder = Callable[[UUID, UUID], None]

# The halt state one Session is in, as the Collector's response envelope carries
# it on the recall path as well as on the ingest path. It is a seam because the
# reader belongs to the Collector, which holds the policy state, and because a
# recall answer must not depend on that read succeeding.
HaltObserver = Callable[[UUID, UUID], HaltReport]


def candidate_pool_for(limit: int) -> int:
    """How many Embeddings the ranking stage considers for a page of one size.

    A multiple of the caller's bound, never below the floor and never above the
    ceiling, so the pool scales with what was asked for while staying inside the
    range the latency obligation was measured over.
    """
    scaled = max(1, limit) * OVER_FETCH_FACTOR
    return min(CANDIDATE_POOL_CEILING, max(CANDIDATE_POOL_FLOOR, scaled))


@dataclass(frozen=True, slots=True)
class Recalled:
    """One prior Artifact recall returned, with the provenance it is judged by.

    The fields are the ones every hook adapter renders from, so the wording and the
    ranking of an injected block are the same across tools and only the envelope
    differs.
    """

    artifact_id: UUID
    artifact_kind: ArtifactKind
    client_id: UUID
    distance: float
    session_id: UUID
    machine_id: str
    occurred_at: datetime
    outcome: str
    kind: str
    excerpt: str
    confidence: float | None = None

    @property
    def is_procedure(self) -> bool:
        """Whether this result is a Learned_Procedure, and so carries a standing."""
        return self.kind == PROCEDURE_KIND

    def as_document(self) -> JsonObject:
        """The result as the response envelope carries it.

        Every value is a JSON scalar, and the instant is the canonical timestamp
        form, because the hook parses it back with the parser that form is defined
        by.
        """
        document: dict[str, JsonValue] = {
            "artifact_id": str(self.artifact_id),
            "artifact_kind": str(self.artifact_kind),
            "distance": self.distance,
            "session_id": str(self.session_id),
            "machine_id": self.machine_id,
            "occurred_at": format_timestamp(self.occurred_at),
            "outcome": self.outcome,
            "kind": self.kind,
            "excerpt": self.excerpt,
        }
        if self.confidence is not None:
            document["confidence"] = self.confidence
        return document


def _recalled_of(row: RecallRow) -> Recalled:
    """Build one answer from one row of the page."""
    return Recalled(
        artifact_id=row.artifact_id,
        artifact_kind=row.artifact_kind,
        client_id=row.client_id,
        distance=row.cosine_distance,
        session_id=row.session_id,
        machine_id=row.machine_id,
        occurred_at=row.occurred_at,
        outcome=row.outcome,
        kind=row.content_kind,
        excerpt=row.excerpt,
        confidence=row.procedure_confidence,
    )


class RecallEngine:
    """The pre-action query: one embedding, one page, and the records it owes.

    Held as an object rather than offered as a function because everything it
    needs resolves once per process: the store, the provider, the floor, and the
    two optional seams. A caller then asks a question and supplies nothing but the
    question.
    """

    __slots__ = (
        "_clock",
        "_embedder",
        "_excerpt_characters",
        "_floor",
        "_halt",
        "_limit",
        "_recorder",
        "_store",
    )

    def __init__(
        self,
        store: MemoryStore,
        embedder: QueryEmbedder,
        *,
        recall_floor: float,
        limit: int = DEFAULT_RESULT_LIMIT,
        excerpt_characters: int = DEFAULT_EXCERPT_CHARACTERS,
        retrievals: RetrievalRecorder | None = None,
        halt: HaltObserver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._floor = recall_floor
        self._limit = _bounded_limit(limit)
        self._excerpt_characters = excerpt_characters
        self._recorder = self._track if retrievals is None else retrievals
        self._halt = halt
        self._clock = clock if clock is not None else _now

    @classmethod
    def from_configuration(
        cls,
        store: MemoryStore,
        embedder: QueryEmbedder,
        configuration: Configuration,
        *,
        retrievals: RetrievalRecorder | None = None,
        halt: HaltObserver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> RecallEngine:
        """Build the engine, taking the recall floor from the Confidence_Tracker's policy.

        The floor arrives as a field of that policy rather than as a key this module
        reads for itself. The tracker owns the number because two components consult
        it — this predicate excludes below it and the erasure sweep includes below it
        — and a second reader naming the key would be a second place an operator's
        change could fail to reach.
        """
        return cls(
            store,
            embedder,
            recall_floor=ConfidencePolicy.from_configuration(configuration).recall_floor,
            retrievals=retrievals,
            halt=halt,
            clock=clock,
        )

    @property
    def recall_floor(self) -> float:
        """The standing a Learned_Procedure must reach to be recalled."""
        return self._floor

    # -- the query -------------------------------------------------------

    def recall(
        self,
        query_text: str,
        k: int | None = None,
        *,
        permitted: Iterable[UUID] = (),
        session_id: UUID | None = None,
    ) -> tuple[Recalled, ...]:
        """The k most similar prior Artifacts the caller may see, closest first.

        Args:
            query_text: A natural-language description of the intended action.
            k: How many results to return, bounded by the route's own ceiling.
            permitted: Clients the caller is separately entitled to, which is what
                the tool server and the command line supply from their own
                configuration. The Session's own Client is added to these rather
                than replaced by them.
            session_id: The Session asking, whose stored row supplies the tenancy
                a request body cannot and which the recall Event is recorded
                within.

        Returns:
            The page, ordered by ascending cosine distance with the standing as
            the tie-break and the Artifact identifier as the final key. Empty when
            the cluster or the provider could not be reached, when the Session
            names no stored row and no Client was presented, or when nothing the
            caller may see is similar.
        """
        started = perf_counter()
        metric(RECALL_QUERIES_METRIC)
        try:
            return self._answer(query_text, k, permitted, session_id)
        finally:
            # Timed in a finally rather than beside the successful return: recall
            # answers empty on an unreachable cluster and on a provider failure,
            # and those are exactly the queries whose latency an operator wants,
            # so a measurement taken only on the happy path would report the
            # healthy population only.
            elapsed = max(perf_counter() - started, 0.0) * _MILLISECONDS_PER_SECOND
            metric(RECALL_LATENCY_METRIC, elapsed, unit=UNIT_MILLISECONDS)

    def _answer(
        self,
        query_text: str,
        k: int | None,
        permitted: Iterable[UUID],
        session_id: UUID | None,
    ) -> tuple[Recalled, ...]:
        """The three reads and the settlement, which is what the timing wraps."""
        bound = self._limit if k is None else _bounded_limit(k)
        scope = self._scope_of(session_id)
        clients = _permitted_set(permitted, scope)
        if not clients:
            log(
                Severity.WARNING,
                COMPONENT,
                "the query resolved to no permitted Client, so it returned no result",
                session_named=session_id is not None,
            )
            return ()
        vector = self._vector_of(query_text)
        if vector is None:
            return ()
        page = self._page_of(vector, clients, bound)
        if page is None:
            return ()
        rows, excluded = page
        results = tuple(_recalled_of(row) for row in rows)
        self._settle(query_text, results, bound, excluded, scope, session_id)
        return results

    def search(self, query: RecallQuery) -> RecallAnswer:
        """Answer one recall request, in the shape the Collector's seam returns.

        The request names a Session and no Client, so the tenancy comes from that
        Session's stored row. Nothing here raises: an unreachable cluster answers
        an empty result set, which is what lets the hook inject nothing and exit
        zero.
        """
        results = self.recall(query.query_text, query.limit, session_id=query.session_id)
        return RecallAnswer(
            results=tuple(result.as_document() for result in results),
            halt=self._halt_of(query.session_id),
        )

    # -- the three reads, each of which may fail without failing recall --

    def _scope_of(self, session_id: UUID | None) -> PrincipalScope | None:
        """The Client the asking Session belongs to, or None when it resolves to none."""
        if session_id is None:
            return None
        try:
            return principal_scope(self._store, session_id)
        except Exception as error:
            self._degrade("the asking Session's tenancy could not be resolved", error)
            return None

    def _vector_of(self, query_text: str) -> tuple[float, ...] | None:
        """The query's vector, or None when the provider would not produce one."""
        try:
            produced = self._embedder.embed_texts([query_text])
        except Exception as error:
            self._degrade("the query text could not be embedded", error)
            return None
        if not produced:
            self._degrade("the provider returned no vector for the query text", None)
            return None
        return tuple(float(component) for component in produced[0])

    def _page_of(
        self,
        vector: tuple[float, ...],
        clients: tuple[UUID, ...],
        bound: int,
    ) -> tuple[tuple[RecallRow, ...], int] | None:
        """The page and the exclusion tally, or None when the cluster did not answer."""
        pool = candidate_pool_for(bound)
        try:
            rows, excluded = recall_page(
                self._store,
                vector,
                permitted_clients=clients,
                limit=bound,
                recall_floor=self._floor,
                candidate_pool=pool,
                excerpt_characters=self._excerpt_characters,
            )
        except Exception as error:
            self._degrade("the memory store did not answer the recall query", error)
            return None
        if len(rows) + excluded >= pool and len(rows) < bound:
            metric(POOL_SATURATED_METRIC)
            log(
                Severity.WARNING,
                COMPONENT,
                "the candidate pool filled and the page came back short, so a "
                "further admissible result may lie below the pool's horizon",
                pool=pool,
                admitted=len(rows),
                asked=bound,
            )
        return rows, excluded

    def _halt_of(self, session_id: UUID) -> HaltReport:
        """The halt state the response envelope reports, defaulting to none observed."""
        if self._halt is None:
            return HaltReport()
        scope = self._scope_of(session_id)
        if scope is None:
            return HaltReport()
        try:
            return self._halt(session_id, scope.client_id)
        except Exception as error:
            self._degrade("the Session's halt state could not be read", error)
            return HaltReport()

    # -- what the answer owes, after the answer is composed --------------

    def _settle(
        self,
        query_text: str,
        results: tuple[Recalled, ...],
        bound: int,
        excluded: int,
        scope: PrincipalScope | None,
        session_id: UUID | None,
    ) -> None:
        """Record what the query excluded, what it returned, and what it retrieved.

        Everything here happens after the page is composed and none of it can fail
        the query, because a memory read that failed for want of its own bookkeeping
        would be worse than one whose bookkeeping is short.
        """
        if excluded:
            metric(RECALL_FLOOR_EXCLUSIONS_METRIC, float(excluded))
        self._append_recall_event(query_text, results, bound, excluded, scope, session_id)
        self._record_retrievals(results, session_id)

    def _append_recall_event(
        self,
        query_text: str,
        results: tuple[Recalled, ...],
        bound: int,
        excluded: int,
        scope: PrincipalScope | None,
        session_id: UUID | None,
    ) -> None:
        """Append the one Event that records the query, its results, and its distances.

        The Event belongs to the asking Session, so a query raised with no Session
        records none: an Event needs a Session and a Client, and inventing either
        would put a claim in the Ledger that the hash chain then attests to.
        """
        if scope is None or session_id is None:
            return
        moment = self._clock()
        payload: dict[str, JsonValue] = {
            _PAYLOAD_QUERY: query_text,
            _PAYLOAD_IDENTIFIERS: [str(result.artifact_id) for result in results],
            _PAYLOAD_DISTANCES: [result.distance for result in results],
            _PAYLOAD_LIMIT: bound,
            _PAYLOAD_EXCLUSIONS: excluded,
        }
        request = LedgerAppend(
            event=Event(
                id=uuid4(),
                session_id=session_id,
                client_id=scope.client_id,
                category=EventCategory.RECALL,
                occurred_at=moment,
                agent_cli=scope.agent_cli,
                machine_id=scope.machine_id,
                parent_event_id=None,
                payload=payload,
                redacted=False,
                text_body=None,
            ),
            expires_at=moment + scope.retention,
        )
        try:
            append(self._store, request)
        except Exception as error:
            metric(RECALL_EVENT_FAILURE_METRIC)
            log(
                Severity.WARNING,
                COMPONENT,
                "the recall Event could not be appended, so the query is unrecorded",
                session_id=str(session_id),
                error_type=type(error).__name__,
            )

    def _record_retrievals(
        self,
        results: tuple[Recalled, ...],
        session_id: UUID | None,
    ) -> None:
        """Record one retrieval per returned Learned_Procedure, off the latency path.

        Being retrieved is not evidence of being right, so this moves no standing;
        it is the record the outcome path later joins a Session's ending to.
        """
        if session_id is None:
            return
        for result in results:
            if not result.is_procedure:
                continue
            try:
                self._recorder(result.artifact_id, session_id)
            except Exception as error:
                log(
                    Severity.WARNING,
                    COMPONENT,
                    "a retrieval of a learned procedure went unrecorded",
                    procedure_id=str(result.artifact_id),
                    session_id=str(session_id),
                    error_type=type(error).__name__,
                )

    def _track(self, procedure_id: UUID, session_id: UUID) -> None:
        """Record one retrieval through the Confidence_Tracker, which owns the write.

        This is the default the seam is filled with when a caller supplies none, so
        an engine built plainly still discharges Requirement 49.3 rather than
        quietly recording nothing. The write itself belongs to the tracker: it holds
        the equivalence between the retrieval table and the procedure kind, and a
        second implementation here would be a second thing to keep true.
        """
        record_retrieval(self._store, procedure_id, session_id)

    def _degrade(self, message: str, error: BaseException | None) -> None:
        """Record one degradation, which is what recall does instead of raising."""
        metric(RECALL_UNAVAILABLE_METRIC)
        log(
            Severity.WARNING,
            COMPONENT,
            message,
            error_type="none" if error is None else type(error).__name__,
        )


def _permitted_set(
    presented: Iterable[UUID],
    scope: PrincipalScope | None,
) -> tuple[UUID, ...]:
    """The Clients one query may see: the caller's own, plus the Session's.

    The union rather than either alone. A caller with a configured entitlement is
    not narrowed to the Session it happens to be asking within, and a caller with
    no configured entitlement is not left with nothing when the Session's own row
    says whose data it produces. Duplicates are removed and the order is kept, so
    the bound array is the same array for the same inputs.
    """
    ordered = list(presented)
    if scope is not None:
        ordered.append(scope.client_id)
    return tuple(dict.fromkeys(ordered))


def _bounded_limit(limit: int) -> int:
    """The result bound to ask for, held inside the range a page may have.

    A bound below one is raised to the default and a bound above the ceiling is
    lowered to it, rather than either being refused: a caller asking for a strange
    number of results still wants an answer, and this read is on a path where a
    refusal costs a person a step.
    """
    if limit < 1:
        return DEFAULT_RESULT_LIMIT
    return min(limit, MAX_RESULT_LIMIT)


def _now() -> datetime:
    """The instant a recall Event records, with an offset so it has one reading."""
    return datetime.now(UTC)
