"""The data-access layer: the only module family that issues SQL.

This module is the store's connection surface. It owns how a connection is
obtained, what settings every connection carries, and how a write transaction is
framed; the query surface is built on top of it, one module per concern.

Five rules shape it, and each is enforced by construction rather than by asking
callers to remember.

**Every caller-supplied value is a bound parameter, and no identifier is ever
interpolated.** Every statement this module sends is a whole module-level literal.
The one statement carrying a value at all, the session statement timeout, binds
that value rather than formatting it into the statement text. There is no place
here where a name reaches statement text, so a Client slug or a scratch key
holding a quote character or a statement terminator is data and can be nothing
else.

**Transport security is required on the deployed path, and the local bypass is
the only exception.** A connection string read from the parameter store must
carry full certificate verification: absent, the requirement is added; set to
anything weaker, the connection string is refused by name. The one exception is
the local development bypass, a connection string the operator injects into the
environment for a loopback test instance, which the secret accessors already
refuse outright once the deployment marks itself as production. So the exception
is unreachable exactly where it would matter, and taking it is recorded as a
warning naming the source rather than passing silently.

**Every write runs in an explicit transaction whose isolation level is stated.**
The retry wrapper sends `BEGIN` and sets SERIALIZABLE on every attempt, so no
write depends on a connection default and no connection reused for a read can
leave a write at a weaker level. The wrapper also owns the bounded, jittered
retry of a serialization conflict.

**Every operation is bounded by a timeout the cluster enforces.** Each connection
sets the session statement timeout when it is created, so a statement that would
hang is ended by the cluster rather than held by the caller. The same bound
governs how long a caller waits for a connection from the pool, so a saturated
pool fails with a message rather than blocking without end.

**Connections are pooled, bounded, and closed together.** A pool holds idle
connections up to a maximum, hands one out under a lease, resets it on return,
and discards it instead of reusing it when it comes back unusable. Closing the
store closes every connection it holds, which is what a termination signal needs.

**The method surface the design names is implemented by the later store modules,
not here.** This module carries the connection handling, the transaction framing,
and the retry entry point. The chain append and its verifier, the Session upsert
and counter statements, the lineage insert and traversals, the embedding write and
the nearest-neighbour query, the historical read, the capability probes, the
attribution history queries, the fenced erasure writes, and the working-tier
accessors each arrive with the module that owns them and reach the cluster through
`in_serializable`, `read`, and `lease` alone. Where the design names one of those
as a method on the store, the method here is a delegation and nothing else: the
owning module is imported inside the method body, so the import direction stays
submodule to store and no cycle is closed at import time.

The driver is reached through two narrow structural protocols declared here rather
than by importing it, so this module imports and type-checks with no driver
installed and every credential-free suite collects on a bare checkout.
"""

from __future__ import annotations

import importlib
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING, Final, Protocol, TypeVar, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from molt.config.resolve import Configuration, InvalidConfigValueError, Kind, load_configuration
from molt.config.secrets import Credential, CredentialSource, ParameterReader, resolve_dsn
from molt.errors import StoreError
from molt.store.retry import (
    DEFAULT_JITTER,
    DEFAULT_SLEEP,
    DEFAULT_TRANSACTION_LABEL,
    Jitter,
    RetryPolicy,
    Sleeper,
    in_serializable,
)
from molt.store.retry import Cursor as SendingCursor
from molt.telemetry import Severity, log

if TYPE_CHECKING:  # The delegating methods name these shapes and import them lazily.
    from datetime import datetime
    from uuid import UUID

    from molt.models.artifact import DerivedArtifact
    from molt.models.event import EmbeddingState
    from molt.store.attribution import (
        AttributionSubmission,
        AttributionWrite,
        CurrentVersion,
        FirstAttribution,
        SupersessionContext,
        VersionAsOf,
    )
    from molt.store.capability import CapabilityRecord
    from molt.store.embeddings import (
        ArtifactWrite,
        EmbeddingWrite,
        Neighbour,
        PendingArtifact,
    )
    from molt.store.historical import GcHorizon
    from molt.store.working import ScratchRow, ScratchWrite, WorkingInterval

__all__ = [
    "COMPONENT",
    "DEFAULT_MAX_CONNECTIONS",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
    "READER_DSN_PARAM_KEY",
    "READER_ROLE_NAMES",
    "REQUIRED_SSL_MODE",
    "ROLE_KEY",
    "SSL_MODE_PARAMETER",
    "STATEMENT_TIMEOUT_KEY",
    "STATEMENT_TIMEOUT_STATEMENT",
    "Connection",
    "Cursor",
    "MemoryStore",
    "connect",
    "require_verified_tls",
    "ssl_mode_of",
]

# The component name every record from the store carries.
COMPONENT: Final[str] = "store"

# The transport security every deployed connection is required to carry, and the
# connection-string parameter it is named by. Full verification means the server
# certificate and the server name are both checked, so a connection cannot be
# intercepted by a host merely holding some valid certificate.
REQUIRED_SSL_MODE: Final[str] = "verify-full"
SSL_MODE_PARAMETER: Final[str] = "sslmode"

# The configuration surface keys this module reads.
STATEMENT_TIMEOUT_KEY: Final[str] = "MOLT_DB_STATEMENT_TIMEOUT_MS"
ROLE_KEY: Final[str] = "MOLT_DB_ROLE"
READER_DSN_PARAM_KEY: Final[str] = "MOLT_READER_DSN_PARAM"

# The names that name the read-only database role, stated once for every component
# that has to insist on it. The migrations create the role as `molt_reader`, while a
# configuration surface and a test both commonly name it `reader`, so a component
# checking one spelling alone refuses the deployment the grants were written for.
# Keeping the pair here rather than in each consumer is what stops the two spellings
# drifting into two different answers about the same role.
READER_ROLE_NAMES: Final[frozenset[str]] = frozenset({"reader", "molt_reader"})

# The bound every statement runs under unless the configuration says otherwise,
# matching the default of the configuration surface key.
DEFAULT_STATEMENT_TIMEOUT_MS: Final[int] = 10000

# How many connections the pool holds at most. The scale obligation is at least
# twenty concurrent writers, so the default admits that many without queueing.
DEFAULT_MAX_CONNECTIONS: Final[int] = 20

# The one statement here that carries a value, with the value bound rather than
# formatted in. The setting name is a literal and the scope is session-wide.
STATEMENT_TIMEOUT_STATEMENT: Final[str] = "SELECT set_config('statement_timeout', %s, false)"

# The statement a returned connection is reset with, so a lease that ended with a
# transaction still open cannot hand that transaction to the next caller.
RESET_STATEMENT: Final[str] = "ROLLBACK"

# The driver package, imported lazily by name for the reason the module docstring
# gives.
_DRIVER_PACKAGE: Final[str] = "psycopg"

# A connection string may arrive as a URI or as space-separated keywords. The
# keyword form is read with this pattern, which matches the security parameter
# and nothing else, so no other part of the value is ever parsed or rendered.
_KEYWORD_SSL_MODE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)" + SSL_MODE_PARAMETER + r"\s*=\s*'?(?P<mode>[A-Za-z-]+)'?"
)

# The URI schemes the connection string may use. Anything else is read as the
# keyword form.
_URI_SCHEMES: Final[frozenset[str]] = frozenset({"postgresql", "postgres"})

T = TypeVar("T")


class Cursor(SendingCursor, Protocol):
    """The four calls the store and every query module make on a cursor.

    The sending call is inherited from the shape the transaction wrapper needs, so
    a cursor handed to a write body satisfies both without either module
    restating the other. The reading calls and the release are added here because
    a query module has to read rows and a lease has to end.
    """

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the next row the last statement produced, or None when there is none."""

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every remaining row the last statement produced."""

    def close(self) -> None:
        """Release this cursor."""


class Connection(Protocol):
    """The three things the pool needs from a connection.

    `closed` is part of the shape because a pooled connection that has died is
    discarded rather than handed to the next caller, and asking is cheaper than
    finding out with a statement.
    """

    @property
    def closed(self) -> bool:
        """Whether this connection can no longer be used."""

    def cursor(self) -> Cursor:
        """Open a cursor on this connection."""

    def close(self) -> None:
        """Close this connection."""


# ---------------------------------------------------------------------------
# Transport security
# ---------------------------------------------------------------------------


def _is_uri(dsn: str) -> bool:
    """Whether a connection string is in the URI form rather than the keyword form."""
    scheme, separator, _ = dsn.partition("://")
    return bool(separator) and scheme.lower() in _URI_SCHEMES


def ssl_mode_of(dsn: str) -> str | None:
    """The transport security mode a connection string asks for, or None when it names none.

    Both connection-string forms are read, and nothing but the security parameter
    is looked at, so no other part of the value is parsed and none of it is
    returned.
    """
    if _is_uri(dsn):
        query = urlsplit(dsn).query
        for name, value in parse_qsl(query, keep_blank_values=True):
            if name.lower() == SSL_MODE_PARAMETER:
                return value.strip().lower() or None
        return None
    match = _KEYWORD_SSL_MODE.search(dsn)
    return match.group("mode").strip().lower() if match else None


def require_verified_tls(dsn: str, *, source_name: str) -> str:
    """Return the connection string with full certificate verification required.

    A string naming no mode gains the requirement. A string naming the required
    mode is returned unchanged. A string naming a weaker mode is refused, and the
    refusal names the configured source and the mode that was found, never the
    connection string itself.
    """
    present = ssl_mode_of(dsn)
    if present == REQUIRED_SSL_MODE:
        return dsn
    if present is not None:
        raise InvalidConfigValueError(
            source_name,
            Kind.TEXT,
            f"the connection string asks for {SSL_MODE_PARAMETER} {present} where "
            f"{REQUIRED_SSL_MODE} is required",
        )
    if _is_uri(dsn):
        parts = urlsplit(dsn)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        pairs.append((SSL_MODE_PARAMETER, REQUIRED_SSL_MODE))
        return urlunsplit(parts._replace(query=urlencode(pairs)))
    separator = " " if dsn.strip() else ""
    return f"{dsn.strip()}{separator}{SSL_MODE_PARAMETER}={REQUIRED_SSL_MODE}"


def _target_for(credential: Credential) -> str:
    """The connection string to dial, with the security requirement applied.

    The requirement is applied to the deployed path and deliberately not to the
    local development bypass. The bypass exists so a contributor can run the
    suites against a loopback test instance, and the secret accessors refuse it
    outright once the deployment marks itself as production, so declining to
    rewrite it here weakens nothing that is reachable in a deployment. Taking it
    is recorded, by source name, so a run against a plaintext local instance is
    visible in the log rather than merely assumed.
    """
    if credential.source is CredentialSource.ENVIRONMENT:
        log(
            Severity.WARNING,
            COMPONENT,
            "connecting through the local development connection string bypass",
            source=credential.source_name,
        )
        return credential.reveal()
    return require_verified_tls(credential.reveal(), source_name=credential.source_name)


# ---------------------------------------------------------------------------
# The driver, reached lazily
# ---------------------------------------------------------------------------


def connect(dsn: str) -> Connection:
    """Open one connection, importing the driver on first use.

    The connection is opened in statement-level autocommit mode on purpose: the
    transaction framing is explicit, so the driver must not open a transaction of
    its own around statements the wrapper has already framed.
    """
    try:
        package = importlib.import_module(_DRIVER_PACKAGE)
    except ModuleNotFoundError as exc:
        raise StoreError(
            "the database driver is not installed, so no connection can be opened; "
            "install the project dependencies"
        ) from exc
    created: object = package.connect(dsn, autocommit=True)
    return cast(Connection, created)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class MemoryStore:
    """The connection surface every query module reaches the cluster through.

    An instance owns a bounded pool, the retry policy, and the statement timeout.
    Nothing about the cluster is discovered at construction: a store builds
    connections when a caller first asks for one, so constructing one costs no
    round trip and a test can construct one against a connection factory of its
    own.
    """

    __slots__ = (
        "_capabilities",
        "_closed",
        "_condition",
        "_connect",
        "_idle",
        "_jitter",
        "_leased",
        "_max_connections",
        "_policy",
        "_role",
        "_sleep",
        "_statement_timeout_ms",
    )

    def __init__(
        self,
        *,
        connect_with: Callable[[], Connection],
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        policy: RetryPolicy | None = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        role: str = "",
        sleep: Sleeper = DEFAULT_SLEEP,
        jitter: Jitter = DEFAULT_JITTER,
    ) -> None:
        if statement_timeout_ms <= 0:
            raise ValueError("the statement timeout must be a positive number of milliseconds")
        if max_connections < 1:
            raise ValueError("the pool must admit at least one connection")
        self._connect = connect_with
        self._statement_timeout_ms = statement_timeout_ms
        self._policy = RetryPolicy() if policy is None else policy
        self._max_connections = max_connections
        self._role = role
        self._sleep = sleep
        self._jitter = jitter
        self._condition = threading.Condition()
        self._idle: deque[Connection] = deque()
        self._leased = 0
        self._closed = False
        self._capabilities: CapabilityRecord | None = None

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration | None = None,
        *,
        reader: ParameterReader | None = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        sleep: Sleeper = DEFAULT_SLEEP,
        jitter: Jitter = DEFAULT_JITTER,
    ) -> MemoryStore:
        """Build a store from the configuration surface and the secret accessors.

        The connection string is resolved once, through the accessors that refuse
        the local bypass in production, and is held inside the connection factory
        alone. It is never an attribute of the store and never reaches a log
        record, an error message, or any output stream.
        """
        resolved = load_configuration() if configuration is None else configuration
        credential = resolve_dsn(resolved, reader=reader)
        target = _target_for(credential)
        role = resolved.text(ROLE_KEY)
        log(
            Severity.INFO,
            COMPONENT,
            "resolved the cluster connection string",
            source=credential.source_name,
            role=role,
        )

        def open_connection() -> Connection:
            return connect(target)

        return cls(
            connect_with=open_connection,
            statement_timeout_ms=resolved.integer(STATEMENT_TIMEOUT_KEY),
            policy=RetryPolicy.from_configuration(resolved),
            max_connections=max_connections,
            role=role,
            sleep=sleep,
            jitter=jitter,
        )

    # -- properties ------------------------------------------------------

    @property
    def statement_timeout_ms(self) -> int:
        """The bound the cluster ends a statement at, in milliseconds."""
        return self._statement_timeout_ms

    @property
    def retry_policy(self) -> RetryPolicy:
        """How a conflicting write transaction is retried."""
        return self._policy

    @property
    def max_connections(self) -> int:
        """How many connections the pool holds at most."""
        return self._max_connections

    @property
    def role(self) -> str:
        """The least-privileged role this store's connection string authenticates as."""
        return self._role

    @property
    def closed(self) -> bool:
        """Whether this store has been closed and hands out no further connection."""
        return self._closed

    def pool_state(self) -> tuple[int, int]:
        """How many connections are idle and how many are leased out.

        Reported as a pair rather than two properties so a caller sees one
        consistent reading of both rather than two readings taken apart.
        """
        with self._condition:
            return len(self._idle), self._leased

    # -- leases ----------------------------------------------------------

    @contextmanager
    def lease(self) -> Iterator[Connection]:
        """Hold one connection for the duration of a block, then return it.

        A connection is returned in every case, failure included, so a raised
        error cannot leak a lease. A connection that comes back unusable is closed
        and discarded rather than handed on.
        """
        connection = self._checkout()
        try:
            yield connection
        finally:
            self._return(connection)

    @contextmanager
    def cursor(self) -> Iterator[Cursor]:
        """Hold one cursor on one leased connection for the duration of a block."""
        with self.lease() as connection:
            opened = connection.cursor()
            try:
                yield opened
            finally:
                opened.close()

    def in_serializable(
        self,
        body: Callable[[Cursor], T],
        *,
        label: str = DEFAULT_TRANSACTION_LABEL,
    ) -> T:
        """Run a write body in one explicit SERIALIZABLE transaction, retrying a conflict.

        This is the only path a write reaches the cluster by. The body must be
        free of effects outside the transaction it is handed, because a conflict
        runs it again from the beginning.
        """
        with self.cursor() as opened:
            return in_serializable(
                opened,
                body,
                policy=self._policy,
                label=label,
                sleep=self._sleep,
                jitter=self._jitter,
            )

    def read(self, body: Callable[[Cursor], T]) -> T:
        """Run a read body on one leased connection, framing no transaction.

        A read needs no explicit transaction: each statement is its own, bounded
        by the same statement timeout, and a read that must see one consistent
        instant says so with a historical timestamp rather than by holding a
        transaction open.
        """
        with self.cursor() as opened:
            return body(opened)

    # -- historical reads, delegated to the module that owns them ---------

    def gc_horizon(self) -> GcHorizon:
        """The measured garbage-collection horizon, read from the capability record.

        Nothing is assumed: a horizon that has not been probed is a refusal
        naming what was missing rather than a default.
        """
        from molt.store.historical import gc_horizon

        return gc_horizon(self)

    def within_gc_horizon(
        self,
        at: datetime,
        *,
        now: datetime | None = None,
        horizon: GcHorizon | None = None,
    ) -> bool:
        """Whether a historical read at an instant is still reachable on this cluster.

        Callers consult this before attempting a historical read, so an instant
        the cluster no longer holds versions for is a recorded decision not to
        read rather than a failed read.
        """
        from molt.store.historical import within_gc_horizon

        return within_gc_horizon(self, at, now=now, horizon=horizon)

    def historical(
        self,
        statement: str,
        parameters: Sequence[object] | None = None,
        *,
        at: datetime,
        now: datetime | None = None,
        horizon: GcHorizon | None = None,
    ) -> tuple[tuple[object, ...], ...]:
        """Run one statement against the state the cluster held at an instant.

        The instant is validated and the horizon consulted before anything is
        sent, and an instant outside the horizon raises the named horizon failure
        with no read attempted at any other instant.
        """
        from molt.store.historical import historical

        return historical(self, statement, parameters, at=at, now=now, horizon=horizon)

    # -- the capability record, read once and held -------------------------

    def capabilities(self, *, refresh: bool = False) -> CapabilityRecord:
        """Read the probed platform facts once and hold them for this store's life.

        The record is a handful of rows that change when an operator reprovisions
        rather than between two queries, so it is read at process start and
        consulted from what is held afterwards. Re-reading is asked for
        explicitly, which is what lets a probe that has just recorded a row take
        effect without the holding being invisible module state.
        """
        from molt.store.capability import capabilities

        if refresh or self._capabilities is None:
            self._capabilities = capabilities(self)
        return self._capabilities

    def prime_capabilities(self, record: CapabilityRecord) -> None:
        """Establish the held record without reading one.

        A startup sequence that has just probed primes what it produced, and a
        test drives a path choice by priming the record that choice turns on,
        rather than either of them depending on when a read happened.
        """
        self._capabilities = record

    def known_capabilities(self) -> CapabilityRecord:
        """The record already held, or the empty record when none has been read.

        This never sends a statement, which is what a query path needs: a
        nearest-neighbour query on the agent's critical path must not spend a read
        discovering which statement to send, and not every role that runs one is
        granted `SELECT` on the capability table. An empty record is the honest
        reading of a cluster nobody probed, and every fallback treats it as
        leaving the primary path in place rather than as an absent capability.
        """
        from molt.store.capability import CapabilityRecord

        return CapabilityRecord() if self._capabilities is None else self._capabilities

    # -- embeddings, delegated to the module that owns them ----------------

    def write_derived_artifact(
        self,
        artifact: DerivedArtifact,
        *,
        embedding: EmbeddingWrite | None = None,
    ) -> ArtifactWrite:
        """Write a Derived_Artifact and its Embedding in one transaction.

        Either both rows land or neither does, so a corpus never holds content
        whose vector never arrived alongside content whose vector did.
        """
        from molt.store.embeddings import write_derived_artifact

        return write_derived_artifact(self, artifact, embedding=embedding)

    def write_embedding(self, request: EmbeddingWrite) -> UUID:
        """Write one Embedding whose Artifact was stored earlier owing a vector."""
        from molt.store.embeddings import write_embedding

        return write_embedding(self, request)

    def mark_embedding_state(
        self,
        artifact_id: UUID,
        client_id: UUID,
        state: EmbeddingState,
    ) -> EmbeddingState | None:
        """Record a vector as owed, present, or unobtainable for one Artifact."""
        from molt.store.embeddings import mark_embedding_state

        return mark_embedding_state(self, artifact_id, client_id, state)

    def pending_artifacts(self, *, limit: int | None = None) -> tuple[PendingArtifact, ...]:
        """The Artifacts still owing a vector, oldest first and bounded by a limit.

        The bound is optional here and defaulted by the owning module, so the one
        default lives in one place rather than being restated by this delegation.
        """
        from molt.store.embeddings import DEFAULT_PENDING_LIMIT, pending_artifacts

        return pending_artifacts(self, limit=DEFAULT_PENDING_LIMIT if limit is None else limit)

    def nearest(
        self,
        query_vector: Sequence[float],
        *,
        permitted_clients: Iterable[UUID],
        limit: int | None = None,
        max_cosine: float | None = None,
    ) -> tuple[Neighbour, ...]:
        """The k closest Embeddings the presented Clients are permitted to see.

        The tenancy restriction is part of the statement's predicate rather than a
        filter applied to its result, so a page of k results is k results the
        caller may see. Which form of the query is sent follows the capability
        record this store holds, so a tier reporting no vector index is answered
        by the bounded exact scan without a read to discover that.
        """
        from molt.store.embeddings import DEFAULT_NEIGHBOUR_LIMIT, nearest

        return nearest(
            self,
            query_vector,
            permitted_clients=permitted_clients,
            limit=DEFAULT_NEIGHBOUR_LIMIT if limit is None else limit,
            max_cosine=max_cosine,
        )

    # -- fenced erasure writes, delegated to the module that owns them -----

    def fenced(
        self,
        client_id: UUID,
        generation: int,
        body: Callable[[Cursor], T],
    ) -> T:
        """Run an erasure write behind the guarded write predicate of the fence.

        The current fencing generation is read on the write's own cursor, inside
        the write's own transaction, so a superseded owner's write is refused
        rather than merely unlikely: a check taken in an earlier transaction would
        leave a window in which the lease is taken over before the write commits.
        """
        from molt.store.fencing import fenced

        return fenced(self, client_id, generation, body)

    # -- the working tier, delegated to the module that owns it -----------

    def write_scratch(
        self,
        entry: ScratchWrite,
        *,
        interval: WorkingInterval | None = None,
        now: datetime | None = None,
    ) -> ScratchRow:
        """Write one working row, overwriting whatever the same key held before.

        The expiry is set from the configured interval on every write, and the
        interval is read from the configuration surface when a caller passes
        none, so no expiry here is a constant of this codebase.
        """
        from molt.store.working import write_scratch

        return write_scratch(self, entry, interval=interval, now=now)

    def read_scratch(
        self,
        session_id: UUID,
        scratch_key: str,
        client_id: UUID,
    ) -> ScratchRow | None:
        """Read one working row by its whole key, scoped by tenant.

        A row the cluster has already removed on expiry is absent in exactly the
        way a row that never existed is, which is what the tier is for.
        """
        from molt.store.working import read_scratch

        return read_scratch(self, session_id, scratch_key, client_id)

    def session_scratch(
        self,
        session_id: UUID,
        client_id: UUID,
        *,
        limit: int | None = None,
    ) -> tuple[ScratchRow, ...]:
        """One Session's working rows, in scratch-key order and bounded by a limit.

        The bound is optional here and defaulted by the owning module, so the one
        default lives in one place rather than being restated by this delegation.
        """
        from molt.store.working import DEFAULT_SCRATCH_LIMIT, session_scratch

        return session_scratch(
            self,
            session_id,
            client_id,
            limit=DEFAULT_SCRATCH_LIMIT if limit is None else limit,
        )

    def purge_working_rows(self, client_id: UUID) -> int:
        """Delete every working row of one Client and report the aggregate count.

        One set-based statement and one number, which is what an Erasure_Run
        records on its run row instead of a Disposition per row.
        """
        from molt.store.working import purge_working_rows

        return purge_working_rows(self, client_id)

    # -- attribution history, delegated to the module that owns it ---------

    def record_attribution(
        self,
        submission: AttributionSubmission,
        *,
        context: SupersessionContext,
        version_id: UUID | None = None,
    ) -> AttributionWrite:
        """Record one detection result, superseding the current version when it differs.

        This is the design's supersession entry point widened to the two cases
        that are not supersessions: a pair holding no current version takes the
        first-write path, and a repeated detection saying nothing new leaves the
        current version alone. A caller writing the Artifact composes the module's
        cursor form into that Artifact's own transaction instead, because a
        binding belongs in the transaction that writes what it describes.
        """
        from molt.store.attribution import write_attribution

        return write_attribution(self, submission, context=context, version_id=version_id)

    def remove_attribution(
        self,
        artifact_id: UUID,
        client_id: UUID,
        *,
        context: SupersessionContext,
        marker_id: UUID | None = None,
    ) -> AttributionWrite | None:
        """Withdraw one Client's claim on one Artifact by closing it, never deleting it.

        The pair holds no current version afterwards, so every operational read
        stops returning the Client, while the history still records that the claim
        was withdrawn rather than that it never existed.
        """
        from molt.store.attribution import remove_attribution

        return remove_attribution(
            self,
            artifact_id,
            client_id,
            context=context,
            marker_id=marker_id,
        )

    def current_attribution(self, artifact_id: UUID) -> tuple[CurrentVersion, ...]:
        """The current attribution of one Artifact, which every operational read uses."""
        from molt.store.attribution import current_attribution

        return current_attribution(self, artifact_id)

    def attribution_as_of(self, artifact_id: UUID, at: datetime) -> tuple[VersionAsOf, ...]:
        """The attribution of one Artifact as it stood at an instant.

        The validity interval is half-open, so the instant a supersession happened
        belongs to the successor alone and one Client contributes at most one
        version to an answer.
        """
        from molt.store.attribution import attribution_as_of

        return attribution_as_of(self, artifact_id, at)

    def first_attributions(
        self,
        client_id: UUID,
        artifact_ids: Iterable[UUID],
    ) -> tuple[FirstAttribution, ...]:
        """When each Artifact was first attributed to one Client, and how it was concluded.

        Read before any disposition runs, because a hard delete removes the rows
        this reads from.
        """
        from molt.store.attribution import first_attributions

        return first_attributions(self, client_id, artifact_ids)

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close every connection the pool holds and refuse further leases.

        Leased connections are not closed from underneath their holders: they are
        closed as they come back, which is what lets a termination signal complete
        in-flight transactions before connections go away.
        """
        with self._condition:
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            self._condition.notify_all()
        for connection in idle:
            _close_quietly(connection)

    def __enter__(self) -> MemoryStore:
        """Return the store, so a caller may bound its lifetime with a block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the pool when the block ends, however it ends."""
        self.close()

    # -- the pool --------------------------------------------------------

    def _checkout(self) -> Connection:
        """Take an idle connection, or build one, or wait for one to come back.

        The wait is bounded by the same timeout every statement runs under, so a
        saturated pool reports a bound that was reached rather than blocking
        without end.
        """
        deadline = time.monotonic() + self._statement_timeout_ms / 1000.0
        while True:
            reuse: Connection | None = None
            with self._condition:
                if self._closed:
                    raise StoreError("the store is closed, so no connection is available")
                while self._idle:
                    candidate = self._idle.popleft()
                    if candidate.closed:
                        continue
                    reuse = candidate
                    break
                if reuse is None and self._leased >= self._max_connections:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        raise StoreError(
                            f"no connection became available within the "
                            f"{self._statement_timeout_ms} millisecond bound; all "
                            f"{self._max_connections} connection(s) are in use"
                        )
                    self._condition.wait(remaining)
                    continue
                self._leased += 1
            if reuse is not None:
                return reuse
            return self._build()

    def _build(self) -> Connection:
        """Open one connection and establish the settings every connection carries.

        The lease this connection was counted against is given back on a failure,
        so a cluster that refuses connections leaves no phantom lease behind and
        the next caller sees the same refusal rather than a saturated pool.
        """
        try:
            connection = self._connect()
        except Exception:
            self._give_back()
            raise
        try:
            self._prepare(connection)
        except Exception:
            _close_quietly(connection)
            self._give_back()
            raise
        return connection

    def _prepare(self, connection: Connection) -> None:
        """Set the statement timeout on a new connection, with the value bound."""
        opened = connection.cursor()
        try:
            opened.execute(STATEMENT_TIMEOUT_STATEMENT, (str(self._statement_timeout_ms),))
        finally:
            opened.close()

    def _give_back(self) -> None:
        """Release the lease counted for a connection that never came into being."""
        with self._condition:
            self._leased -= 1
            self._condition.notify()

    def _return(self, connection: Connection) -> None:
        """Reset a leased connection and make it idle, or discard it.

        A connection is discarded rather than reused when the store has been
        closed, when the connection reports itself closed, or when the reset
        statement fails, because a connection whose state cannot be established is
        worse than no connection at all.
        """
        usable = not self._closed and not connection.closed and _reset(connection)
        with self._condition:
            self._leased -= 1
            if usable and not self._closed:
                self._idle.append(connection)
            self._condition.notify()
        if not usable or self._closed:
            _close_quietly(connection)


def _reset(connection: Connection) -> bool:
    """Discard any transaction a returned connection still holds open.

    A returned connection ordinarily holds no transaction, because the wrapper
    commits or rolls back its own. This is what covers the case it cannot: a
    caller that leased a connection directly and left a transaction open.
    """
    try:
        opened = connection.cursor()
        try:
            opened.execute(RESET_STATEMENT)
        finally:
            opened.close()
    except Exception as error:
        log(
            Severity.DEBUG,
            COMPONENT,
            "a returned connection could not be reset and is being discarded",
            error_type=type(error).__name__,
        )
        return False
    return True


def _close_quietly(connection: Connection) -> None:
    """Close a connection, reporting rather than raising when it will not close."""
    try:
        connection.close()
    except Exception as error:
        log(
            Severity.DEBUG,
            COMPONENT,
            "a connection could not be closed",
            error_type=type(error).__name__,
        )
