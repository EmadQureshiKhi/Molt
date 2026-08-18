"""The Collector handler: one plain function behind a function endpoint.

No web framework sits in the ingest path. A framework would add import time to
every cold start and the capture side is waiting on this request inside an
agent's own latency budget, so the routing, the authentication, and the request
bound are all plain functions over one request shape (Requirement 1.8).

Six claims arrange the module, and each is arranged so a caller cannot get it
wrong by forgetting something.

**The order of the checks is the security posture.** A request is matched, then
authenticated, then bounded, then transport-decoded, then signature-verified, and
only then does a connection get leased. Refusing an unauthenticated caller before
the route table is consulted means an unauthenticated caller learns nothing about
which paths exist; refusing an oversized body before the transport decode means no
oversized body is ever held in a decoded form; and verifying the signature before
the transaction opens means no batch with a well-formed prefix can leave a partial
write behind (Requirements 5.4, 5.10, 5.11, 47.4).

**The health route is the only route reachable without the bearer token, and it
discloses no memory content.** It reports that the function is live, whether the
cluster answered a read that can match no row, and which platform facts the
capability record holds. No row value, no error message from the cluster, and no
count of anything stored appears in it (Requirements 5.3, 30.5, 31.5).

**The bearer comparison is constant time and both failure causes are one
answer.** An absent header and a mismatched value produce the same status and the
same body, so the response distinguishes them no more than the comparison does
(Requirements 5.4, 5.5).

**An absent Session is created in the transaction that writes its Events.** The
Session upsert and the chain appends for one Session run inside one SERIALIZABLE
transaction, taken through the store's own retry wrapper, so either the Session
and its Events are both there or neither is (Requirements 5.7, 15.1).

**Credentials are read once and held for the container lifetime.** The connection
string, the expected bearer value, and the ingress shared secret are resolved
through the secret accessors, whose per-process cache is what *cached for the
container lifetime* means; nothing here adds a second cache and nothing here
holds a revealed value (Requirements 5.8, 30.2, 30.3).

**Unreachability and refusal are different answers.** A cluster that cannot be
reached is a 503 with `collector.write_failure`, because the caller should spool
and try again. A batch naming rows the cluster does not hold is a 200 whose
rejection count says so, because trying again would fail the same way
(Requirements 5.9, 32.6).
"""

from __future__ import annotations

import hmac
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from typing import Final, cast
from uuid import UUID

from molt.capture.signing import AUTHORIZATION_HEADER, BEARER_SCHEME
from molt.collector.routes import (
    SIGNED_KINDS,
    HaltReport,
    Headers,
    ReadBatch,
    RecallAnswer,
    RecallQuery,
    RejectionReason,
    Response,
    Route,
    RouteKind,
    envelope,
    exceeds_bound,
    match_path,
    max_body_bytes,
    method_of,
    read_body,
    read_document,
    read_recall_query,
    read_records,
    read_session_metadata,
    reserved_concurrency,
    response_headers,
    session_for,
)
from molt.config.resolve import (
    Configuration,
    InvalidConfigValueError,
    Kind,
    load_configuration,
)
from molt.config.secrets import (
    Credential,
    ParameterReader,
    resolve_collector_bearer,
    resolve_ingress_signing_key,
)
from molt.errors import IngressRejectedError, MissingParentError
from molt.lifecycle import current_termination
from molt.models.event import EmbeddingState, Event, JsonObject
from molt.models.session import Session
from molt.store import Cursor, MemoryStore
from molt.store.capability import PROBED_CAPABILITIES, CapabilityRecord
from molt.store.chain import LedgerAppend, append_in_transaction
from molt.store.sessions import (
    insert_session_in_transaction,
    session_of_client,
    upsert_session,
)
from molt.telemetry import Severity, correlation, log, metric
from molt.telemetry.inventory import UNIT_MILLISECONDS

__all__ = [
    "BATCH_LATENCY_METRIC",
    "COMPONENT",
    "DEGRADED_STATUS",
    "EVENTS_ACCEPTED_METRIC",
    "EVENTS_REJECTED_METRIC",
    "INGRESS_MODULE",
    "LIVE_STATUS",
    "PROBE_IDENTIFIER",
    "REACHABLE",
    "RETENTION_INTERVAL_KEY",
    "TRANSACTION_LABEL",
    "UNREACHABLE",
    "WRITE_FAILURE_METRIC",
    "ApprovalReader",
    "Collector",
    "IngressVerifier",
    "Invocation",
    "PersistOutcome",
    "RecallSearch",
    "handler",
    "invocation_of",
    "rendered",
    "reserved_concurrency",
    "reset_collector",
    "retention_interval",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "collector"

# The metric an unreachable cluster emits (Requirement 5.9). Undimensioned, so it
# adds one fixed billable combination rather than one per Session or per machine.
WRITE_FAILURE_METRIC: Final[str] = "collector.write_failure"

# What one batch is measured by: the two record counts and how long the batch took
# end to end. All three are undimensioned for the same reason the write failure is:
# the tenant and the machine are unbounded, and they belong in the log record.
EVENTS_ACCEPTED_METRIC: Final[str] = "collector.events_accepted"
EVENTS_REJECTED_METRIC: Final[str] = "collector.events_rejected"
BATCH_LATENCY_METRIC: Final[str] = "collector.batch_latency_ms"

# The batch latency is declared in milliseconds, and the monotonic reading is in
# seconds.
_MILLISECONDS_PER_SECOND: Final[float] = 1000.0

# What the ingest transaction is called in a log record and in the note an
# exhausted retry attaches.
TRANSACTION_LABEL: Final[str] = "collector_ingest"

# The module the Ingress_Signature verifier is loaded from. It is resolved by
# name at first use rather than imported at module scope, so the handler and the
# verifier stay separately replaceable and a caller may inject its own.
INGRESS_MODULE: Final[str] = "molt.collector.ingress"

# The vocabulary of the health body. Fixed strings so an operator reads one
# vocabulary and a test asserts against a name rather than a sentence.
LIVE_STATUS: Final[str] = "ok"
DEGRADED_STATUS: Final[str] = "degraded"
REACHABLE: Final[str] = "reachable"
UNREACHABLE: Final[str] = "unreachable"

# The identifier the reachability probe reads with. The nil identifier names no
# Session and no Client, so the probe proves the cluster answered and returns no
# row whatever the cluster holds.
PROBE_IDENTIFIER: Final[UUID] = UUID(int=0)

# Where the row expiry comes from. A Ledger row carries a non-null expiry and the
# capture side presents none, so the ingest path derives one from the configured
# default retention interval. A per-Client interval is the Retention component's
# own concern and supersedes this default where one is recorded.
RETENTION_INTERVAL_KEY: Final[str] = "MOLT_RETENTION_DEFAULT_INTERVAL"

_INTERVAL_SECONDS: Final[Mapping[str, int]] = {
    "second": 1,
    "seconds": 1,
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
    "day": 86400,
    "days": 86400,
    "week": 604800,
    "weeks": 604800,
}

# The statement state family the cluster reports for a write naming a row that
# does not exist, or one violating a uniqueness or check constraint. A refusal
# under this family is a fault in the request rather than in the cluster, so the
# records of that write are rejected and the response is not a 503.
_INTEGRITY_STATE_PREFIX: Final[str] = "23"
_STATE_ATTRIBUTES: Final[tuple[str, ...]] = ("sqlstate", "pgcode")

# The one body a refused request carries. One body for an absent bearer value and
# for a mismatched one, so the response tells the two apart no more than the
# comparison does.
_UNAUTHORISED_DOCUMENT: Final[JsonObject] = {"error": "unauthorised"}

# Statuses this module answers with.
_OK: Final[int] = 200
_BAD_REQUEST: Final[int] = 400
_UNAUTHORISED: Final[int] = 401
_NOT_FOUND: Final[int] = 404
_METHOD_NOT_ALLOWED: Final[int] = 405
_CONFLICT: Final[int] = 409
_TOO_LARGE: Final[int] = 413
_UNAVAILABLE: Final[int] = 503


# The three seams the components that own them attach to. Each is a callable so
# that the Collector depends on a shape rather than on a module that may not have
# been written yet, and each has a stated behaviour when nothing is attached.
IngressVerifier = Callable[[Mapping[str, str], bytes], None]
ApprovalReader = Callable[[UUID, UUID], tuple[JsonObject, ...]]
RecallSearch = Callable[[RecallQuery], RecallAnswer]

# The two modules the recall seam is built from, resolved by name at the point of use
# rather than imported here. The engine imports the store and the provider registry
# imports the configuration surface, and neither belongs in this module's import graph:
# the seam exists so the Collector depends on a shape, and building the default
# implementation must not turn that shape back into a hard dependency. Resolving by name
# also keeps a container that never serves a recall from paying either import.
_RECALL_MODULE: Final[str] = "molt.recall"
_PROVIDER_REGISTRY_MODULE: Final[str] = "molt.providers.registry"
_PROVIDER_MODULE: Final[str] = "molt.providers"

# The setting naming which embedding implementation a recall query is vectorised by. The
# same one the ingest path embeds with, so a query and the corpus it searches are
# measured by the same model.
_EMBEDDING_PROVIDER_ENV: Final[str] = "MOLT_EMBEDDING_PROVIDER"


def _recall_of(configuration: Configuration, store: MemoryStore) -> RecallSearch | None:
    """The recall implementation this container serves, or None when it cannot build one.

    The seam had no default, and nothing supplied one. A deployed Collector therefore
    answered every recall with an empty result set and a warning saying an engine was
    not attached — on the agent's critical path, for the feature the rest of the system
    exists to serve, while the engine itself was built, tested, and reachable from every
    other surface. The route worked, the corpus was searchable, and the two were never
    joined.

    None is still returned rather than raised when the engine cannot be built, because
    that is what the route already handles and what it should: a container whose provider
    credential is unreadable must still accept ingest, and a recall that answers nothing
    costs the agent one request where a failed cold start would cost it every request.
    The reason is logged once, at build time, so an empty answer is explained somewhere
    other than at the caller.

    The tenancy filter is not applied here and is not this function's to apply. The
    engine resolves the asking Session's Client from the stored row and permits that
    Client, so a query cannot widen its own reach by naming a Session it does not own.
    """
    try:
        recall_module = importlib.import_module(_RECALL_MODULE)
        registry = importlib.import_module(_PROVIDER_REGISTRY_MODULE)
        providers = importlib.import_module(_PROVIDER_MODULE)
        provider = registry.load_embedding_builder(configuration.text(_EMBEDDING_PROVIDER_ENV))(
            configuration
        )
        # The engine's query surface is one call and every provider names another, so the
        # provider is adapted rather than passed. Without the adapter the engine attaches
        # and the call fails on a missing attribute, which is what a deployed recall did.
        embedder = providers.ProviderEmbedder(provider)
        engine = recall_module.RecallEngine.from_configuration(store, embedder, configuration)
    # A container that cannot build the engine still serves ingest, so every cause of a
    # failed build is one outcome here: the route's documented empty answer.
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "no recall engine could be built, so recall answers an empty result set",
            cause=type(error).__name__,
        )
        return None

    def search(query: RecallQuery) -> RecallAnswer:
        found = engine.recall(
            query.query_text,
            query.limit,
            session_id=query.session_id,
        )
        return RecallAnswer(results=tuple(item.as_document() for item in found))

    log(
        Severity.INFO,
        COMPONENT,
        "attached the recall engine",
        embedding_provider=provider.name,
        model_id=provider.model_id,
        recall_floor=engine.recall_floor,
    )
    return search


@dataclass(frozen=True, slots=True)
class Invocation:
    """One request as the transport delivered it, before the body is decoded.

    The body is still transport-encoded text here on purpose: the request bound is
    taken against a length rather than against a decoded body, so the decode
    happens after the bound has been applied (Requirement 5.11).
    """

    method: str
    path: str
    headers: Headers
    body_text: str
    base64_encoded: bool


@dataclass(frozen=True, slots=True)
class PersistOutcome:
    """What one batch write produced: what landed, what was refused, and how.

    `unreachable` is separate from the rejection count because the two lead to
    different responses: a refused record is reported inside a 200 and an
    unreachable cluster is a 503 (Requirements 5.6, 5.9).
    """

    accepted: int = 0
    refused: int = 0
    unreachable: bool = False
    halt: HaltReport | None = None


def retention_interval(configuration: Configuration) -> timedelta:
    """The interval a Ledger row's expiry is set at from its observation instant.

    The configured value is a count and a unit, which is the form the schema's own
    interval columns are written in. An unreadable value is refused at cold start
    rather than per request, because a deployment that cannot derive a row expiry
    can write no row at all and should say so before it accepts traffic.
    """
    text = configuration.text(RETENTION_INTERVAL_KEY).strip().lower()
    parts = text.split()
    detail = "the value reads as neither a count of seconds nor a count and a unit"
    if len(parts) == 1:
        try:
            return timedelta(seconds=int(parts[0], 10))
        except ValueError as exc:
            raise InvalidConfigValueError(RETENTION_INTERVAL_KEY, Kind.TEXT, detail) from exc
    if len(parts) != 2 or parts[1] not in _INTERVAL_SECONDS:
        raise InvalidConfigValueError(RETENTION_INTERVAL_KEY, Kind.TEXT, detail)
    try:
        count = int(parts[0], 10)
    except ValueError as exc:
        raise InvalidConfigValueError(RETENTION_INTERVAL_KEY, Kind.TEXT, detail) from exc
    if count <= 0:
        raise InvalidConfigValueError(
            RETENTION_INTERVAL_KEY, Kind.TEXT, "the count must be positive"
        )
    return timedelta(seconds=count * _INTERVAL_SECONDS[parts[1]])


class Collector:
    """One container's Collector: the cold-start state plus the request path.

    An instance holds the configuration, the store, the two credentials, and the
    three seams. Constructing one costs no round trip, so a test builds one
    against its own store and its own credentials while the deployed path builds
    one through `from_configuration` on the first request the container serves.
    """

    __slots__ = (
        "_approvals",
        "_bearer",
        "_capabilities_primed",
        "_configuration",
        "_expiry_interval",
        "_ingress",
        "_ingress_key",
        "_max_age_seconds",
        "_max_body_bytes",
        "_recall",
        "_store",
    )

    def __init__(
        self,
        *,
        configuration: Configuration,
        store: MemoryStore,
        bearer: Credential,
        ingress_key: Credential | None = None,
        ingress: IngressVerifier | None = None,
        approvals: ApprovalReader | None = None,
        recall: RecallSearch | None = None,
    ) -> None:
        self._configuration = configuration
        self._store = store
        self._bearer = bearer
        self._ingress_key = ingress_key
        self._ingress = ingress
        self._approvals = approvals
        self._recall = recall
        self._max_body_bytes = max_body_bytes(configuration)
        self._max_age_seconds = configuration.integer("MOLT_INGRESS_MAX_AGE_SECONDS")
        self._expiry_interval = retention_interval(configuration)
        self._capabilities_primed = False

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration | None = None,
        *,
        reader: ParameterReader | None = None,
        store: MemoryStore | None = None,
        ingress: IngressVerifier | None = None,
        approvals: ApprovalReader | None = None,
        recall: RecallSearch | None = None,
    ) -> Collector:
        """Build the container's Collector, resolving every secret once.

        The three secrets come from the accessors that cache per process, so the
        parameter store is read on the first request a container serves and not
        again (Requirements 5.8, 30.2, 30.3). Nothing revealed is held: the
        wrappers are, and each is revealed inside the one call that needs it.
        """
        resolved = load_configuration() if configuration is None else configuration
        built = MemoryStore.from_configuration(resolved, reader=reader) if store is None else store
        bearer = resolve_collector_bearer(resolved, reader=reader)
        ingress_key = resolve_ingress_signing_key(resolved, reader=reader)
        search = _recall_of(resolved, built) if recall is None else recall
        log(
            Severity.INFO,
            COMPONENT,
            "resolved the Collector cold-start configuration",
            bearer_source=bearer.source_name,
            ingress_source=ingress_key.source_name,
            max_body_bytes=max_body_bytes(resolved),
            reserved_concurrency=reserved_concurrency(resolved),
            statement_timeout_ms=built.statement_timeout_ms,
        )
        return cls(
            configuration=resolved,
            store=built,
            bearer=bearer,
            ingress_key=ingress_key,
            ingress=ingress,
            approvals=approvals,
            recall=search,
        )

    # -- properties ------------------------------------------------------

    @property
    def store(self) -> MemoryStore:
        """The connection surface every write and every read goes through."""
        return self._store

    @property
    def max_body_bytes(self) -> int:
        """The configured maximum request body size, in bytes."""
        return self._max_body_bytes

    @property
    def reserved_concurrency(self) -> int:
        """The configured reserved concurrency ceiling the deployment declares."""
        return reserved_concurrency(self._configuration)

    # -- the request path ------------------------------------------------

    def serve(self, invocation: Invocation) -> Response:
        """Answer one request, in the order the module docstring states.

        Every step that can refuse returns before a connection is leased, so a
        refused request costs no cluster work and leaves nothing behind.
        """
        route = match_path(invocation.path)
        if route is not None and route.kind is RouteKind.HEALTH:
            return self._health_or_method(invocation)

        if not self._authorised(invocation.headers):
            return Response(_UNAUTHORISED, dict(_UNAUTHORISED_DOCUMENT))
        if route is None:
            return Response(_NOT_FOUND, {"error": "no such route"})
        if invocation.method.upper() != method_of(route.kind):
            return Response(_METHOD_NOT_ALLOWED, {"error": "method not allowed"})

        if exceeds_bound(
            invocation.headers,
            invocation.body_text,
            base64_encoded=invocation.base64_encoded,
            maximum=self._max_body_bytes,
        ):
            log(
                Severity.WARNING,
                COMPONENT,
                "a request body exceeded the configured maximum and nothing was persisted",
                route=str(route.kind),
                maximum=self._max_body_bytes,
            )
            return Response(_TOO_LARGE, {"error": "request body too large"})

        try:
            body = read_body(invocation.body_text, base64_encoded=invocation.base64_encoded)
        except ValueError:
            return Response(_BAD_REQUEST, {"error": "unreadable request body"})

        if route.kind in SIGNED_KINDS and not self._signature_accepted(invocation.headers, body):
            return Response(_UNAUTHORISED, dict(_UNAUTHORISED_DOCUMENT))

        # The termination check sits here, after every refusal that costs no
        # cluster work and immediately before the one step that writes. A draining
        # instance declines the request so the caller retries it elsewhere, and the
        # request already inside its block below is always allowed to finish.
        termination = current_termination()
        if termination.stopping:
            log(
                Severity.WARNING,
                COMPONENT,
                "the Collector is terminating, so the request was declined unwritten",
                route=str(route.kind),
            )
            return Response(_UNAVAILABLE, {"error": "the Collector is shutting down"})

        with termination.in_flight_work():
            return self._dispatch(route, body)

    def _dispatch(self, route: Route, body: bytes) -> Response:
        """Hand a matched, authenticated, bounded, verified request to its route."""
        if route.kind is RouteKind.EVENTS:
            return self.ingest(body)
        if route.kind is RouteKind.RECALL:
            return self.recall(body)
        if route.session_id is None:
            return Response(_NOT_FOUND, {"error": "no such route"})
        return self.put_session(route.session_id, body)

    # -- authentication --------------------------------------------------

    def _authorised(self, headers: Mapping[str, str]) -> bool:
        """Whether the request presents the expected bearer value.

        The comparison is `hmac.compare_digest` over the encoded values, so it is
        constant time in the length of the shorter one and leaks no information
        about the correct prefix (Requirement 5.5). Both values are encoded first
        rather than compared as text, because the comparison over text admits
        ASCII alone and a caller may present anything at all.
        """
        presented = headers.get(AUTHORIZATION_HEADER)
        if presented is None:
            return False
        scheme, _, value = presented.partition(" ")
        if scheme.strip().lower() != BEARER_SCHEME.lower():
            return False
        return hmac.compare_digest(
            value.strip().encode("utf-8"), self._bearer.reveal().encode("utf-8")
        )

    def _signature_accepted(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Whether the Ingress_Signature verifier accepted this request.

        This is the seam the signature verification attaches to. It runs after the
        request bound and after the transport decode, and before any record is
        read and before any transaction opens, which is what makes *nothing
        persisted* structural for every rejection cause (Requirement 47.4).

        With no verifier attached and none loadable, the request is refused. The
        signature is required on the two ingest routes, so a Collector that cannot
        verify one must not accept it.
        """
        verifier = self._verifier()
        if verifier is None:
            log(
                Severity.ERROR,
                COMPONENT,
                "no ingress signature verifier is available, so the request was refused",
                module=INGRESS_MODULE,
            )
            return False
        try:
            verifier(headers, body)
        except IngressRejectedError:
            return False
        return True

    def _verifier(self) -> IngressVerifier | None:
        """The attached verifier, or one built over the loadable verification call.

        The verification call is looked up by name so this module carries no
        import-time dependency on it, and the shared secret is revealed inside the
        one call that needs it rather than held revealed for the container's life.
        """
        if self._ingress is not None:
            return self._ingress
        if self._ingress_key is None:
            return None
        loaded = _load_verification()
        if loaded is None:
            return None
        key = self._ingress_key
        max_age = self._max_age_seconds

        def verify(headers: Mapping[str, str], body: bytes) -> None:
            loaded(headers, body, key.reveal(), max_age)

        self._ingress = verify
        return verify

    # -- the Event batch route -------------------------------------------

    def ingest(self, body: bytes) -> Response:
        """Persist every well-formed record of a batch and report both counts.

        A malformed record is a rejection rather than a failure of the request it
        arrived in, so a partly malformed batch is a 200 whose counts sum to the
        batch size (Requirement 5.6).
        """
        started = perf_counter()
        batch = read_records(body)
        outcome = self._persist(batch.events)
        rejected = len(batch.rejections) + outcome.refused
        _count_batch(outcome.accepted, rejected, started)
        if outcome.unreachable:
            return Response(_UNAVAILABLE, {"error": "the memory store is unreachable"})
        return Response(
            _OK,
            envelope(
                accepted=outcome.accepted,
                rejected=rejected,
                halt=outcome.halt,
                rejections=_rejection_counts(batch, outcome.refused),
            ),
        )

    def _persist(self, events: Sequence[Event]) -> PersistOutcome:
        """Write the well-formed records, one transaction per Session.

        Appends to distinct Sessions share no conflict window, so grouping by
        Session is what lets many machines write at once without one Session's
        conflict aborting another's batch. Each group's transaction writes the
        Session first and its Events after, so an absent Session is created inside
        the transaction that writes the Events that named it (Requirement 5.7).
        """
        if not events:
            return PersistOutcome()
        accepted = 0
        refused = 0
        unreachable = False
        for group in _grouped_by_session(events).values():
            landed = self._write_group(group)
            if landed is None:
                unreachable = True
                break
            if landed:
                accepted += len(group)
            else:
                refused += len(group)
        halt = None if unreachable else self._halt_for(events)
        return PersistOutcome(
            accepted=accepted,
            refused=refused,
            unreachable=unreachable,
            halt=halt,
        )

    def _write_group(self, group: Sequence[Event]) -> bool | None:
        """Write one Session's records, or report how the write failed.

        Returns True when the group landed, False when the cluster refused it
        because the request named rows it does not hold, and None when the cluster
        could not be reached at all. The three are distinguished because they lead
        to three different answers.
        """
        record = session_for(group)
        requests = [self._append_request(event) for event in group]

        def body(cursor: Cursor) -> None:
            _insert_session_in_transaction(cursor, record)
            for request in requests:
                append_in_transaction(cursor, request)

        try:
            self._store.in_serializable(body, label=TRANSACTION_LABEL)
        except Exception as error:
            if _is_request_fault(error):
                log(
                    Severity.WARNING,
                    COMPONENT,
                    "a batch group named a row the cluster does not hold and was refused",
                    session_id=str(record.id),
                    records=len(group),
                    error_type=type(error).__name__,
                )
                return False
            metric(WRITE_FAILURE_METRIC)
            log(
                Severity.ERROR,
                COMPONENT,
                "the memory store could not be written to",
                session_id=str(record.id),
                records=len(group),
                error_type=type(error).__name__,
            )
            return None
        return True

    def _append_request(self, event: Event) -> LedgerAppend:
        """The append request one Event becomes, with the two row fields added.

        A record carrying text owes a vector, so it lands in the pending state and
        the Embedder drains it later; a record carrying none owes nothing. Either
        way the Event is accepted, which is what keeps an unavailable
        Embedding_Provider from stopping capture (Requirement 32.1).
        """
        owed = EmbeddingState.PENDING if event.text_body else EmbeddingState.NOT_REQUIRED
        return LedgerAppend(
            event=event,
            expires_at=event.occurred_at + self._expiry_interval,
            embedding_state=owed,
        )

    # -- the Session metadata route --------------------------------------

    def put_session(self, session_id: UUID, body: bytes) -> Response:
        """Create or update one Session's metadata (Requirement 5.2).

        The write is idempotent: the conflict path of the Session insert restates
        neither tenancy nor lineage nor counters, so a repeated metadata write
        leaves an existing Session exactly as it was.
        """
        try:
            record = read_session_metadata(read_document(body), session_id=session_id)
        except ValueError as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "the Session metadata was refused",
                session_id=str(session_id),
                detail=str(error),
            )
            return Response(_BAD_REQUEST, {"error": "unreadable Session metadata"})

        try:
            upsert_session(self._store, record)
        except MissingParentError:
            return Response(_CONFLICT, {"error": "the Session names a row that does not exist"})
        except Exception as error:
            if _is_request_fault(error):
                return Response(_CONFLICT, {"error": "the Session names a row that does not exist"})
            metric(WRITE_FAILURE_METRIC)
            log(
                Severity.ERROR,
                COMPONENT,
                "the Session metadata could not be written",
                session_id=str(session_id),
                error_type=type(error).__name__,
            )
            return Response(_UNAVAILABLE, {"error": "the memory store is unreachable"})
        return Response(
            _OK,
            envelope(accepted=1, halt=self._halt_of(record.id, record.client_id)),
        )

    # -- the recall route ------------------------------------------------

    def recall(self, body: bytes) -> Response:
        """Answer a recall query on the agent's critical path (Requirement 13).

        Bearer-only by design: recall is the interactive path and a caller holding
        the bearer value but not the shared secret must still be able to ask
        memory a question (Requirement 47.12).

        With no Recall_Engine attached the answer is an empty result set rather
        than a failure, because a recall that returns nothing costs the agent one
        request while a failure would cost it the retry schedule as well.
        """
        try:
            query = read_recall_query(read_document(body))
        except ValueError:
            return Response(_BAD_REQUEST, {"error": "unreadable recall query"})
        if self._recall is None:
            log(
                Severity.WARNING,
                COMPONENT,
                "no recall engine is attached, so the query returned no result",
            )
            return Response(_OK, envelope(results=()))
        answer = self._recall(query)
        return Response(_OK, envelope(halt=answer.halt, results=answer.results))

    # -- the health route ------------------------------------------------

    def _health_or_method(self, invocation: Invocation) -> Response:
        """The health answer, or a method refusal, both without the bearer value."""
        if invocation.method.upper() != method_of(RouteKind.HEALTH):
            return Response(_METHOD_NOT_ALLOWED, {"error": "method not allowed"})
        return self.health()

    def health(self) -> Response:
        """Report liveness, cluster reachability, and the capability summary.

        Answering at all is the liveness report. Reachability is a read that can
        match no row, so the cluster's answer is proof it answered and carries no
        stored value. The capability summary is read from what the store already
        holds, which sends no statement and needs no privilege on the capability
        table, after one best-effort attempt to read it per container.

        No memory content appears here, and no message the cluster composed
        appears either: a failed probe is reported as unreachability and the
        failure is written to the log instead (Requirements 5.3, 31.4, 31.5).
        """
        reachable = self._reachable()
        record = self._known_capabilities()
        document: JsonObject = {
            "status": LIVE_STATUS if reachable else DEGRADED_STATUS,
            "component": COMPONENT,
            "database": REACHABLE if reachable else UNREACHABLE,
            "capabilities": {
                name: {"probed": record.probed(name), "available": record.available(name)}
                for name in PROBED_CAPABILITIES
            },
            "unprobed": list(record.unprobed),
        }
        return Response(_OK, document)

    def _reachable(self) -> bool:
        """Whether the cluster answered a read that can match no row."""
        try:
            session_of_client(self._store, PROBE_IDENTIFIER, PROBE_IDENTIFIER)
        except Exception as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "the cluster did not answer the reachability probe",
                error_type=type(error).__name__,
            )
            return False
        return True

    def _known_capabilities(self) -> CapabilityRecord:
        """The capability record the store holds, attempting one read per container.

        The attempt is best-effort because the least-privileged role this component
        connects with is not granted a read of the capability table. An empty
        record is the honest reading when nobody could look, and every accessor
        reports each fact as unprobed rather than as absent.
        """
        if not self._capabilities_primed:
            self._capabilities_primed = True
            try:
                self._store.capabilities()
            except Exception as error:
                log(
                    Severity.DEBUG,
                    COMPONENT,
                    "the capability record could not be read, so none is held",
                    error_type=type(error).__name__,
                )
        return self._store.known_capabilities()

    # -- the halt fields -------------------------------------------------

    def _halt_for(self, events: Sequence[Event]) -> HaltReport:
        """The halt state the batch's own Session is in.

        The Session of the last record is the one the current action belongs to:
        a batch carries the spooled records first and the new ones after, so the
        final record is the newest observation and its Session is the one the
        capture side is deciding about (Requirement 23.7).
        """
        if not events:
            return HaltReport()
        last = events[-1]
        return self._halt_of(last.session_id, last.client_id)

    def _halt_of(self, session_id: UUID, client_id: UUID) -> HaltReport:
        """Read one Session's halt state, reporting the default when it cannot be read.

        The read happens after the transaction has committed, so a cluster that
        stops answering between the write and this read costs the response its
        halt fields rather than costing the write its records.
        """
        try:
            stored = session_of_client(self._store, session_id, client_id)
        except Exception as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "the Session halt state could not be read",
                session_id=str(session_id),
                error_type=type(error).__name__,
            )
            return HaltReport()
        if stored is None:
            return HaltReport()
        return HaltReport(
            halted=stored.halted,
            halt_reason=stored.halt_reason,
            pending_approvals=self._queued_approvals(session_id, client_id),
        )

    def _queued_approvals(self, session_id: UUID, client_id: UUID) -> tuple[JsonObject, ...]:
        """The approvals queued for one Session, or none when no reader is attached.

        This is the seam the approval queue attaches to. The envelope carries the
        field either way, so the capture side reads one shape whether the queue is
        readable or not.
        """
        if self._approvals is None:
            return ()
        try:
            return self._approvals(session_id, client_id)
        except Exception as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "the approval queue could not be read",
                session_id=str(session_id),
                error_type=type(error).__name__,
            )
            return ()


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def _is_request_fault(error: BaseException) -> bool:
    """Whether a write failed because the request named rows the cluster lacks.

    The state is read off the failure rather than inferred from a type, because
    the driver is imported lazily by the store and its exception classes are
    therefore not nameable here.
    """
    if isinstance(error, MissingParentError):
        return True
    for attribute in _STATE_ATTRIBUTES:
        state = getattr(error, attribute, None)
        if isinstance(state, str) and state.startswith(_INTEGRITY_STATE_PREFIX):
            return True
    return False


def _count_batch(accepted: int, rejected: int, started: float) -> None:
    """Measure one batch: the two record counts and the end-to-end latency.

    A count of zero is not emitted, so an empty batch spends no billable metric on
    saying nothing happened, while the latency is always emitted because the time a
    batch took is the measurement a saturated Collector shows up in first.
    """
    if accepted:
        metric(EVENTS_ACCEPTED_METRIC, float(accepted))
    if rejected:
        metric(EVENTS_REJECTED_METRIC, float(rejected))
    elapsed = max(perf_counter() - started, 0.0) * _MILLISECONDS_PER_SECOND
    metric(BATCH_LATENCY_METRIC, elapsed, unit=UNIT_MILLISECONDS)


def _rejection_counts(batch: ReadBatch, refused: int) -> dict[str, int]:
    """How many records were rejected under each stated reason."""
    counts: dict[str, int] = {}
    for reason in batch.rejections:
        counts[str(reason)] = counts.get(str(reason), 0) + 1
    if refused:
        counts[str(RejectionReason.REFUSED)] = refused
    return counts


def _grouped_by_session(events: Sequence[Event]) -> dict[UUID, list[Event]]:
    """The batch's records grouped by Session, in the order the Sessions appeared."""
    grouped: dict[UUID, list[Event]] = {}
    for event in events:
        grouped.setdefault(event.session_id, []).append(event)
    return grouped


def _insert_session_in_transaction(cursor: Cursor, record: Session) -> int:
    """Write the Session of a batch group on the caller's cursor.

    The store's Session insert resolves a conflict by closing a Session and
    nothing else, so restating an open Session leaves tenancy, lineage, and every
    counter exactly as they were. That is what lets the ingest path write the
    Session unconditionally rather than reading first: an absent Session is
    created and a present one is untouched, both inside the transaction that
    writes the Events (Requirement 5.7).

    A batch carries Events and no spawning Event, so the spawning reference is
    stated as absent here rather than defaulted to absent by the store.
    """
    return insert_session_in_transaction(cursor, record, spawning_event_id=None)


def _load_verification() -> Callable[[Mapping[str, str], bytes, str, int], None] | None:
    """The Ingress_Signature verification call, or None when it is not available."""
    try:
        module = importlib.import_module(INGRESS_MODULE)
    except ModuleNotFoundError:
        return None
    call: object = getattr(module, "verify_ingress", None)
    if not callable(call):
        return None
    return cast(Callable[[Mapping[str, str], bytes, str, int], None], call)


# ---------------------------------------------------------------------------
# The transport adapter
# ---------------------------------------------------------------------------


def invocation_of(event: Mapping[str, object]) -> Invocation:
    """Read one function-endpoint event into the request shape the Collector serves.

    The body is left transport-encoded, because the request bound is applied to
    its length before anything is decoded.
    """
    context = event.get("requestContext")
    http: Mapping[str, object] = {}
    if isinstance(context, Mapping):
        nested = context.get("http")
        if isinstance(nested, Mapping):
            http = nested
    headers: dict[str, str] = {}
    raw_headers = event.get("headers")
    if isinstance(raw_headers, Mapping):
        for name, value in raw_headers.items():
            if isinstance(name, str) and isinstance(value, str):
                headers[name] = value
    body = event.get("body")
    return Invocation(
        method=_text(http.get("method")) or _text(event.get("httpMethod")) or "",
        path=_text(event.get("rawPath")) or _text(http.get("path")) or _text(event.get("path")),
        headers=Headers(headers),
        body_text=body if isinstance(body, str) else "",
        base64_encoded=event.get("isBase64Encoded") is True,
    )


def _text(value: object) -> str:
    """The text a transport field carries, or empty text when it carries none."""
    return value if isinstance(value, str) else ""


def rendered(response: Response) -> dict[str, object]:
    """Render one response as the function endpoint's own reply shape."""
    return {
        "statusCode": response.status,
        "headers": response_headers(),
        "body": json.dumps(
            response.document,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
        "isBase64Encoded": False,
    }


_collector: Collector | None = None


def reset_collector() -> None:
    """Discard the container's Collector, so the next request builds a fresh one.

    The holding is a container-lifetime cache by design, so this exists for a test
    that needs a clean container rather than for any runtime path.
    """
    global _collector
    _collector = None


def _current() -> Collector:
    """The container's Collector, built on the first request it serves."""
    global _collector
    if _collector is None:
        _collector = Collector.from_configuration()
    return _collector


def handler(event: Mapping[str, object], context: object = None) -> dict[str, object]:
    """The function entry point: one request in, one reply out.

    The invocation identifier the runtime supplies becomes the correlation
    identifier every record written while serving the request carries
    (Requirement 31.2).

    A cold start that cannot resolve its configuration or its credentials answers
    the health route with a degraded body and every other route with a 503, rather
    than failing the invocation: an operator reading the health route is exactly
    who needs to know that the container came up unable to work.
    """
    invocation = invocation_of(event)
    identifier = getattr(context, "aws_request_id", None)
    with correlation(identifier if isinstance(identifier, str) else ""):
        try:
            collector = _current()
        except Exception as error:
            log(
                Severity.ERROR,
                COMPONENT,
                "the Collector could not complete its cold start",
                error_type=type(error).__name__,
            )
            return rendered(_cold_start_failure(invocation))
        return rendered(collector.serve(invocation))


def _cold_start_failure(invocation: Invocation) -> Response:
    """The answer a container that could not come up gives."""
    route = match_path(invocation.path)
    if route is not None and route.kind is RouteKind.HEALTH:
        return Response(
            _OK,
            {
                "status": DEGRADED_STATUS,
                "component": COMPONENT,
                "database": UNREACHABLE,
                "capabilities": {
                    name: {"probed": False, "available": False} for name in PROBED_CAPABILITIES
                },
                "unprobed": list(PROBED_CAPABILITIES),
            },
        )
    return Response(_UNAVAILABLE, {"error": "the Collector is unavailable"})
