"""The hook entry point: one process per vendor hook event, exiting 0 whatever happens.

An agent command-line tool fires a hook, this process runs, and the agent waits
for it. That single fact decides everything below. Six claims arrange the module.

**Exit status 0 is unconditional, and the handler that guarantees it is the
outermost frame.** A malformed payload, a payload that is not valid UTF-8, an
unset configuration surface, an unreachable Collector, an unwritable spool
directory, and a programming mistake all reach the same place: one line on
standard error and status 0 (Requirements 1.7, 6.6). The handler catches
`Exception` rather than a list of expected failures, because the obligation is
about the exit status rather than about a catalogue of causes, and a cause nobody
anticipated is exactly the one that would otherwise fail an engineer's session.

**Standard output is the vendor's decision channel and nothing else writes to
it.** Only a path that reached a structured hook response writes bytes there: a
refusal of the pending action, or an injected block of recall results. Every
diagnostic goes to standard error, and standard error receives at most one line
per invocation, because the notes accumulated along the way are joined into one
line at the end rather than written as they arise.

**The tool token comes from the invoking shim, so the Agent_CLI identity is not an
operator setting.** The console script is installed per tool as `molt-hook <tool>
<event-name>` (Requirement 1.3). A token outside the supported set is a
configuration mistake worth reporting and still exits 0, and it is reported
without importing anything, because an unsupported token names no adapter to load.

**The latency budget is met by what this module does not do.** It imports no
database driver, no cloud client, and no redaction pattern table; the adapter for
the one tool that fired is imported by name and the other four are never touched;
one transport connection is reused across the spool flush and the new batch. The
soft deadline is what turns the 5 second network cap into a bound the agent can
afford: the cap is the ceiling on any single operation (Requirement 6.4), while
the soft deadline bounds the transmission phase as a whole, and a batch that
cannot be placed inside it is spooled rather than waited on.

**A signature is computed immediately before transmission.** The spool holds Event
records, so a batch flushed after an outage is signed with a fresh timestamp and
lands inside the Collector's age bound however long the outage lasted (Requirement
47.10). With the shared secret unset, the batch is spooled rather than sent
unsigned, because an unsigned ingest request would be refused and the Events lost;
the recall path is untouched by that, because recall is bearer-only (Requirement
47.12).

**Halt state is learned from the response envelope, and an unread envelope blocks
nothing.** The hook holds no database credential, so `halted`, `halt_reason`, and
`pending_approvals` arrive on the ingest and recall responses (Requirements 23.7,
23.9). When no envelope was read — unreachable, refused, or never asked — the
halt state of the Session is unknown rather than clear, so the hook spools, does
not block, and says so in its diagnostic line, because keeping the agent working
is the higher obligation (Requirement 6.1).
"""

from __future__ import annotations

import http.client
import importlib
import json
import random
import sys
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from molt.capture.protocol import (
    UNASSIGNED_CLIENT,
    CaptureContext,
    ClientRef,
    Clock,
    HookAdapter,
    HookInvocation,
    HookOutcome,
    PendingApproval,
    RecallResult,
    SystemClock,
    TransmitResult,
    derive_session_id,
)
from molt.capture.signing import (
    authorization,
    bearer_token,
    ingress_headers,
    shared_secret,
)
from molt.capture.spool import RECORD_SEPARATOR, Spool, SpooledBatch, resolve_machine_id
from molt.config.resolve import Configuration, load_configuration
from molt.config.secrets import Credential
from molt.models.event import (
    Event,
    EventCategory,
    JsonObject,
    JsonValue,
    format_timestamp,
    parse_timestamp,
    serialise_event,
)

if TYPE_CHECKING:  # pragma: no cover - imported for the annotation alone
    from molt.redact import RedactionSettings

__all__ = [
    "ADAPTER_ATTRIBUTE",
    "ADAPTER_PACKAGE",
    "BACKOFF_SECONDS",
    "COMPONENT",
    "DEADLINE_NOTE",
    "EVENTS_PATH",
    "EXIT_OK",
    "JITTER_FRACTION",
    "MIN_OPERATION_SECONDS",
    "NO_BEARER_NOTE",
    "RECALL_PATH",
    "SESSION_PATH_TEMPLATE",
    "SUPPORTED_TOOLS",
    "TERMINAL_STATUSES",
    "UNASSIGNED_NOTE",
    "UNOBSERVED_HALT_NOTE",
    "UNSIGNED_NOTE",
    "ClientMapEntry",
    "Envelope",
    "HttpTransport",
    "Jitter",
    "RecallOutcome",
    "Reply",
    "Sleeper",
    "Transmitter",
    "Transport",
    "batch_body",
    "capture_context",
    "dispatch",
    "emit",
    "failure_note",
    "load_adapter",
    "load_client_map",
    "main",
    "map_payload",
    "policy_halt_event",
    "read_envelope",
    "read_payload",
    "resolve_client",
    "session_metadata",
    "write_decision",
    "write_diagnostic",
]

# The component name this module reports itself as.
COMPONENT: Final[str] = "capture"

# The one status this process ever exits with (Requirements 1.7, 6.6).
EXIT_OK: Final[int] = 0

# The agent tools an adapter is written for (Requirement 1.1). A token outside
# this set is reported and exits 0 rather than raising.
SUPPORTED_TOOLS: Final[frozenset[str]] = frozenset(
    {"claude_code", "cursor", "codex", "gemini_cli", "copilot"}
)

# Where an adapter lives and what it is bound to there. The entry point imports
# the one module the fired token names and never the other four.
ADAPTER_PACKAGE: Final[str] = "molt.capture.adapters"
ADAPTER_ATTRIBUTE: Final[str] = "ADAPTER"

# The Collector routes the capture side calls. The two ingest routes carry the
# signature headers; the recall route is bearer-only.
EVENTS_PATH: Final[str] = "/events"
SESSION_PATH_TEMPLATE: Final[str] = "/sessions/{session_id}"
RECALL_PATH: Final[str] = "/recall"

CONTENT_TYPE_HEADER: Final[str] = "Content-Type"
NDJSON_CONTENT_TYPE: Final[str] = "application/x-ndjson"
JSON_CONTENT_TYPE: Final[str] = "application/json"

# The backoff schedule of Requirement 6.3, in seconds, one entry per retry.
BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.2, 0.4, 0.8)

# How much jitter is added to a delay, as a fraction of that delay. Additive
# rather than multiplicative, so the schedule above is the floor each delay is
# drawn upwards from and two hook processes that failed together do not retry in
# step.
JITTER_FRACTION: Final[float] = 0.25

# The least time any single network operation is given. A soft budget that has
# nearly run out would otherwise produce a timeout so short that the attempt is
# certain to fail, which spends a round trip to learn nothing.
MIN_OPERATION_SECONDS: Final[float] = 0.05

# Statuses that will not improve on a retry: the request itself was refused
# rather than the service being unavailable.
TERMINAL_STATUSES: Final[frozenset[int]] = frozenset({400, 401, 403, 413, 422})

# How many results a pre-action recall query asks for, and how long a diagnostic
# line's rendering of a failure may run before it is cut.
RECALL_RESULT_LIMIT: Final[int] = 5
NOTE_LIMIT: Final[int] = 200

# The phrases the diagnostic line is composed from. Each is a fixed string so a
# reader of standard error sees one vocabulary rather than one phrasing per call
# site, and so a test asserts against a name rather than a sentence.
UNSIGNED_NOTE: Final[str] = (
    "the ingress shared secret is unset, so the batch was spooled rather than sent unsigned"
)
DEADLINE_NOTE: Final[str] = "the transmission soft deadline elapsed, so the batch was spooled"
UNOBSERVED_HALT_NOTE: Final[str] = "halt state unobserved, so no action was blocked"
NO_BEARER_NOTE: Final[str] = (
    "the Collector bearer token is unset, so the batch was spooled rather than sent unauthenticated"
)
UNASSIGNED_NOTE: Final[str] = (
    "the workspace matches no mapping entry, so the reserved unassigned Client was used"
)

# How a delay is drawn and how waiting is done. Both are injected so a test drives
# the schedule rather than waiting it out.
Sleeper = Callable[[float], None]
Jitter = Callable[[float, float], float]

# A system-seeded source, so a caller that seeded the shared module generator for
# its own reasons does not put every hook process back in step.
_jitter_source: Final[random.SystemRandom] = random.SystemRandom()

DEFAULT_SLEEP: Final[Sleeper] = time.sleep
DEFAULT_JITTER: Final[Jitter] = _jitter_source.uniform


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    """One response, reduced to what the capture side reads from it."""

    status: int
    body: bytes


class Transport(Protocol):
    """The one call the capture side makes on a connection, plus closing it.

    Declared structurally so a test drives a recorded transport and the delivered
    one is the only place a socket appears. `send` raises `OSError` for every
    transport fault, which is the one failure family the caller retries on.
    """

    def send(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        timeout: float,
    ) -> Reply:
        """Place one request and read its response."""

    def close(self) -> None:
        """Release the underlying connection, if one is open."""


class HttpTransport:
    """One connection to the Collector, reused across every request of a process.

    Reuse is what the latency budget spends its saving on: the spool flush and the
    new batch travel over one already-established connection rather than paying a
    handshake each (Requirement 1.8). A transport fault closes the connection
    rather than leaving a half-consumed one behind, because a reused connection
    whose previous response was never fully read is worse than a new one.
    """

    __slots__ = ("_connection", "_host", "_port", "_prefix", "_secure")

    def __init__(self, url: str) -> None:
        parts = urlsplit(url if "//" in url else f"//{url}", scheme="https")
        if not parts.hostname:
            raise ValueError("the Collector address names no host")
        self._secure = parts.scheme != "http"
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self._connection: http.client.HTTPConnection | None = None

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> HttpTransport:
        """Build a transport for the configured Collector address."""
        return cls(configuration.text("MOLT_COLLECTOR_URL"))

    @property
    def secure(self) -> bool:
        """Whether the connection is made over TLS."""
        return self._secure

    def send(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        timeout: float,
    ) -> Reply:
        """Place one request over the reused connection, bounded by the timeout."""
        connection = self._open(timeout)
        try:
            connection.request(method, f"{self._prefix}{path}", body=body, headers=dict(headers))
            response = connection.getresponse()
            return Reply(status=response.status, body=response.read())
        except (OSError, http.client.HTTPException) as error:
            self.close()
            raise OSError(type(error).__name__) from error

    def close(self) -> None:
        """Close the connection, so the next request establishes a fresh one."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _open(self, timeout: float) -> http.client.HTTPConnection:
        """The open connection, created on first use and re-bounded per request.

        The bound is re-applied to the live socket as well as to the connection,
        because a connection established for an earlier request carries that
        request's timeout on its socket and the soft deadline shrinks between one
        request and the next.
        """
        if self._connection is None:
            self._connection = (
                http.client.HTTPSConnection(self._host, self._port, timeout=timeout)
                if self._secure
                else http.client.HTTPConnection(self._host, self._port, timeout=timeout)
            )
        self._connection.timeout = timeout
        socket = self._connection.sock
        if socket is not None:
            socket.settimeout(timeout)
        return self._connection


# ---------------------------------------------------------------------------
# The response envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Envelope:
    """What the Collector said back, beyond the status line.

    Every ingest and recall response carries the halt fields, which is the only
    channel the capture side has for them (Requirement 23.7). The counts are
    carried because a partial batch reports both (Requirement 5.6).
    """

    accepted: int = 0
    rejected: int = 0
    halted: bool = False
    halt_reason: str | None = None
    pending_approvals: tuple[PendingApproval, ...] = ()


def read_envelope(body: bytes) -> Envelope | None:
    """Read a response envelope, or report that the body carried none.

    None rather than a default envelope: a body that is not a JSON object is a
    body the halt fields were not read from, and treating that as *not halted*
    would turn an unread envelope into a clear one.
    """
    try:
        decoded: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    return Envelope(
        accepted=_as_count(decoded.get("accepted")),
        rejected=_as_count(decoded.get("rejected")),
        halted=decoded.get("halted") is True,
        halt_reason=_as_text(decoded.get("halt_reason")),
        pending_approvals=_as_approvals(decoded.get("pending_approvals")),
    )


def _as_count(value: object) -> int:
    """A non-negative count from an envelope field, or zero when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _as_text(value: object) -> str | None:
    """Non-empty text from an envelope field, or None."""
    return value if isinstance(value, str) and value else None


def _as_uuid(value: object) -> UUID | None:
    """An identifier from an envelope field, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _as_fragments(value: object) -> tuple[str, ...]:
    """The non-empty text entries of an envelope list field."""
    if not isinstance(value, list):
        return ()
    return tuple(entry for entry in value if isinstance(entry, str) and entry)


def _as_approvals(value: object) -> tuple[PendingApproval, ...]:
    """The queued approvals an envelope reported, skipping any entry of no shape."""
    if not isinstance(value, list):
        return ()
    approvals: list[PendingApproval] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        approvals.append(
            PendingApproval(
                rule_id=_as_uuid(entry.get("rule_id")),
                rule_name=_as_text(entry.get("rule_name")) or "an unnamed rule",
                categories=_as_fragments(entry.get("categories")),
                patterns=_as_fragments(entry.get("patterns")),
            )
        )
    return tuple(approvals)


@dataclass(frozen=True, slots=True)
class RecallOutcome:
    """What a recall query returned, and the envelope it arrived with.

    An unreachable Recall_Engine yields no results and no envelope, which is the
    empty result set the adapter renders and the unobserved halt state the
    diagnostic line reports (Requirement 13.8).
    """

    results: tuple[RecallResult, ...] = ()
    envelope: Envelope | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# Workspace to Client resolution
# ---------------------------------------------------------------------------

# The table name and the field names of the mapping document. It is read with the
# standard library's own reader rather than through the configuration surface,
# because the surface refuses a key it does not declare and every key of this
# document is an operator's own workspace path.
CLIENT_TABLE: Final[str] = "client"
CLIENT_WORKSPACE_FIELD: Final[str] = "workspace"
CLIENT_ID_FIELD: Final[str] = "id"
CLIENT_SLUG_FIELD: Final[str] = "slug"


@dataclass(frozen=True, slots=True)
class ClientMapEntry:
    """One workspace-to-Client mapping entry, with the workspace already expanded."""

    workspace: Path
    client: ClientRef


def load_client_map(path: Path) -> tuple[ClientMapEntry, ...]:
    """Read the workspace-to-Client mapping document.

    The document is an array of tables, each naming a workspace root, a Client
    identifier, and a Client slug. An array of tables rather than a table keyed by
    path, because a workspace path holds separators and quoting a path into a key
    is a mistake waiting to be made. An entry missing a field or carrying an
    identifier that is not a UUID is skipped rather than failing the read, so one
    bad line costs one mapping rather than every mapping.
    """
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    declared = document.get(CLIENT_TABLE)
    if not isinstance(declared, list):
        return ()
    entries: list[ClientMapEntry] = []
    for item in declared:
        if not isinstance(item, Mapping):
            continue
        workspace = _as_text(item.get(CLIENT_WORKSPACE_FIELD))
        identifier = _as_uuid(item.get(CLIENT_ID_FIELD))
        slug = _as_text(item.get(CLIENT_SLUG_FIELD))
        if workspace is None or identifier is None or slug is None:
            continue
        entries.append(
            ClientMapEntry(
                workspace=Path(workspace).expanduser(),
                client=ClientRef(id=identifier, slug=slug, assigned=True),
            )
        )
    return tuple(entries)


def resolve_client(
    workspace_path: str,
    *,
    configuration: Configuration | None = None,
) -> ClientRef:
    """Resolve the Client a workspace's memory belongs to (Requirements 1.5, 1.6).

    The longest matching workspace root wins, so a mapping may name a repository
    and a sub-project inside it and the sub-project's entry is the one that
    applies. A workspace matching nothing, an unconfigured mapping, and an
    unreadable mapping all resolve to the reserved unassigned Client, and the
    caller reports that through the flag the reference carries rather than by
    comparing identifiers.
    """
    if not workspace_path:
        return UNASSIGNED_CLIENT
    resolved = configuration if configuration is not None else load_configuration()
    mapping_path = resolved.optional_path("MOLT_CLIENT_MAP")
    if mapping_path is None:
        return UNASSIGNED_CLIENT
    try:
        entries = load_client_map(mapping_path)
    except (OSError, tomllib.TOMLDecodeError):
        return UNASSIGNED_CLIENT

    workspace = Path(workspace_path).expanduser()
    best: ClientMapEntry | None = None
    for entry in entries:
        if workspace != entry.workspace and entry.workspace not in workspace.parents:
            continue
        if best is None or len(entry.workspace.parts) > len(best.workspace.parts):
            best = entry
    return UNASSIGNED_CLIENT if best is None else best.client


# ---------------------------------------------------------------------------
# The adapter registry
# ---------------------------------------------------------------------------


def load_adapter(tool: str) -> HookAdapter:
    """Import the one adapter the fired token names.

    Imported by name so the four adapters that did not fire are never read from
    disk, which is part of what keeps the invocation inside its budget. An
    unsupported token raises before any import is attempted, because a token that
    names no tool names no module either.
    """
    if tool not in SUPPORTED_TOOLS:
        raise ValueError(f"the hook token {tool!r} names no supported agent tool")
    module = importlib.import_module(f"{ADAPTER_PACKAGE}.{tool}")
    adapter = getattr(module, ADAPTER_ATTRIBUTE, None)
    if adapter is None:
        raise ValueError(f"the adapter module for {tool!r} exposes no adapter")
    return _as_adapter(adapter)


def _as_adapter(candidate: object) -> HookAdapter:
    """Confirm a loaded object answers the five calls, naming the first it does not."""
    for name in ("parse", "to_events", "context_injection", "blocking_response", "capabilities"):
        if not callable(getattr(candidate, name, None)):
            raise ValueError(f"the loaded adapter answers no {name!r} call")
    if not isinstance(getattr(candidate, "tool", None), str):
        raise ValueError("the loaded adapter names no tool")
    return cast(HookAdapter, candidate)


# ---------------------------------------------------------------------------
# Transmission
# ---------------------------------------------------------------------------


def batch_body(events: Sequence[Event]) -> bytes:
    """The exact bytes an Event batch request carries.

    Newline-delimited Event wire records (Requirement 5.1). These are the bytes the
    signature is taken over, so they are produced once and both signed and sent,
    with nothing between the two that could re-render them differently.
    """
    return b"".join(serialise_event(event).encode("utf-8") + RECORD_SEPARATOR for event in events)


def _json_body(document: JsonObject) -> bytes:
    """A request body for a route that carries one JSON object."""
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class Transmitter:
    """One process's conversation with the Collector, spool-first and time-bounded.

    The order of operations is the whole of Requirement 6.2: the spool is claimed
    before anything new is sent, the claimed records and the new ones travel as one
    batch, and the claim is confirmed only once the Collector has accepted it. A
    failure releases the claim back into the spool, so records are never both in
    flight and lost.

    Two bounds act together. Every network operation is given at most the
    configured cap, which is Requirement 6.4 and is a ceiling rather than a target.
    Within that ceiling the operation is given no more than what remains of the
    soft deadline, and no retry and no backoff wait is started once the soft
    deadline has passed. The cap therefore bounds a single operation and the soft
    deadline bounds the phase, which is what keeps a slow Collector from spending
    the agent's wall-clock time (Requirement 1.8).
    """

    __slots__ = (
        "_bearer",
        "_cap_seconds",
        "_clock",
        "_jitter",
        "_retries",
        "_secret",
        "_sleep",
        "_soft_deadline",
        "_spool",
        "_started",
        "_transport",
    )

    def __init__(
        self,
        *,
        spool: Spool,
        transport: Transport,
        bearer: Credential | None,
        secret: Credential | None,
        cap_seconds: float,
        soft_deadline_seconds: float,
        retries: int,
        clock: Clock | None = None,
        sleep: Sleeper = DEFAULT_SLEEP,
        jitter: Jitter = DEFAULT_JITTER,
        started: float | None = None,
    ) -> None:
        if cap_seconds <= 0.0 or soft_deadline_seconds <= 0.0:
            raise ValueError("the network cap and the soft deadline must both be positive")
        if retries < 0:
            raise ValueError("the retry count cannot be negative")
        self._spool = spool
        self._transport = transport
        self._bearer = bearer
        self._secret = secret
        self._cap_seconds = cap_seconds
        self._soft_deadline = soft_deadline_seconds
        self._retries = retries
        self._clock: Clock = SystemClock() if clock is None else clock
        self._sleep = sleep
        self._jitter = jitter
        self._started = self._clock.monotonic() if started is None else started

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration,
        *,
        spool: Spool | None = None,
        transport: Transport | None = None,
        clock: Clock | None = None,
        sleep: Sleeper = DEFAULT_SLEEP,
        jitter: Jitter = DEFAULT_JITTER,
        started: float | None = None,
    ) -> Transmitter:
        """Build a transmitter from the resolved configuration surface."""
        machine_id = resolve_machine_id(configuration)
        return cls(
            spool=(
                Spool.from_configuration(configuration, machine_id=machine_id)
                if spool is None
                else spool
            ),
            transport=(
                HttpTransport.from_configuration(configuration) if transport is None else transport
            ),
            bearer=bearer_token(configuration),
            secret=shared_secret(configuration),
            cap_seconds=float(configuration.integer("MOLT_HTTP_TIMEOUT_SECONDS")),
            soft_deadline_seconds=configuration.integer("MOLT_HOOK_SOFT_DEADLINE_MS") / 1000.0,
            retries=configuration.integer("MOLT_HTTP_RETRIES"),
            clock=clock,
            sleep=sleep,
            jitter=jitter,
            started=started,
        )

    # -- the two bounds --------------------------------------------------

    @property
    def spool(self) -> Spool:
        """The spool this transmitter claims from and releases back into."""
        return self._spool

    def remaining(self) -> float:
        """What is left of the soft deadline, which may be zero or negative."""
        return self._soft_deadline - (self._clock.monotonic() - self._started)

    def expired(self) -> bool:
        """Whether the soft deadline has passed, so nothing further is started."""
        return self.remaining() <= 0.0

    def operation_timeout(self) -> float:
        """The bound one network operation is given.

        The configured cap is the ceiling, the remaining soft budget is the
        practical bound, and the floor keeps a nearly-exhausted budget from
        producing an attempt that cannot possibly complete.
        """
        return max(MIN_OPERATION_SECONDS, min(self._cap_seconds, self.remaining()))

    def backoff(self, retry: int) -> float:
        """The wait before one retry: the scheduled delay, drawn upwards by jitter."""
        base = BACKOFF_SECONDS[min(retry, len(BACKOFF_SECONDS) - 1)]
        return base + self._jitter(0.0, base * JITTER_FRACTION)

    # -- the ingest path -------------------------------------------------

    def emit(self, events: Sequence[Event]) -> TransmitResult:
        """Place the spooled records and the new ones, or spool them all.

        Returns what happened, including whether an envelope was read, which is
        what the caller turns the halt decision on.
        """
        new = tuple(events)
        batch = self._spool.claim()
        outgoing = batch.events + new
        if not outgoing:
            self._spool.confirm(batch)
            return TransmitResult(observed=False)
        if self._secret is None:
            return self._spool_all(batch, new, note=UNSIGNED_NOTE, attempts=0)
        if self._bearer is None:
            return self._spool_all(batch, new, note=NO_BEARER_NOTE, attempts=0)

        body = batch_body(outgoing)
        note: str | None = None
        attempts = 0
        for retry in range(self._retries + 1):
            if self.expired():
                note = DEADLINE_NOTE
                break
            attempts += 1
            reply, failure = self._attempt(EVENTS_PATH, body, NDJSON_CONTENT_TYPE, signed=True)
            if reply is not None and 200 <= reply.status < 300:
                self._spool.confirm(batch)
                return self._accepted(reply, len(outgoing), attempts)
            note = failure if reply is None else _status_note(reply.status)
            if reply is not None and reply.status in TERMINAL_STATUSES:
                break
            if retry >= self._retries:
                break
            waiting = self.backoff(retry)
            if self.remaining() <= waiting:
                note = DEADLINE_NOTE
                break
            self._sleep(waiting)
        return self._spool_all(batch, new, note=note, attempts=attempts)

    def put_session(self, session_id: UUID, metadata: JsonObject) -> Envelope | None:
        """Place Session metadata on the signed Session endpoint (Requirement 5.2).

        One attempt rather than a retried one, and nothing is spooled on failure:
        the Event batch carries the Session identity and the Collector creates the
        Session in the same transaction as the Event when it does not yet exist
        (Requirement 5.7), so the metadata call adds detail rather than carrying
        the record. It is made on a Session-opening invocation alone, so a run pays
        for it once.
        """
        if self._secret is None or self._bearer is None or self.expired():
            return None
        path = SESSION_PATH_TEMPLATE.format(session_id=session_id)
        reply, _ = self._attempt(
            path,
            _json_body(metadata),
            JSON_CONTENT_TYPE,
            signed=True,
            method="PUT",
        )
        if reply is None or not 200 <= reply.status < 300:
            return None
        return read_envelope(reply.body)

    # -- the recall path -------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        session_id: UUID,
        limit: int = RECALL_RESULT_LIMIT,
    ) -> RecallOutcome:
        """Ask memory about an intended action, bearer-only and best-effort.

        No signature is presented and none is required: recall is the interactive
        path, and an absent shared secret must not close it (Requirement 47.12).
        Any failure yields no results, which the adapter renders as the vendor's
        no-op response, and exit status 0 is unaffected (Requirement 13.8).
        """
        if self._bearer is None or self.expired():
            return RecallOutcome()
        body = _json_body({"query_text": query, "k": limit, "session_id": str(session_id)})
        reply, failure = self._attempt(RECALL_PATH, body, JSON_CONTENT_TYPE, signed=False)
        if reply is None:
            return RecallOutcome(note=failure)
        if not 200 <= reply.status < 300:
            return RecallOutcome(note=_status_note(reply.status))
        return RecallOutcome(results=_read_results(reply.body), envelope=read_envelope(reply.body))

    def close(self) -> None:
        """Release the transport connection."""
        self._transport.close()

    # -- one attempt -----------------------------------------------------

    def _attempt(
        self,
        path: str,
        body: bytes,
        content_type: str,
        *,
        signed: bool,
        method: str = "POST",
    ) -> tuple[Reply | None, str | None]:
        """Make one bounded request, reporting a transport fault as a note.

        The signature is computed here rather than by the caller, which is what
        makes *immediately before transmission* structural: the timestamp is read,
        the digest is taken over that timestamp and these exact body bytes, and the
        request is sent in the same statement sequence, so a retry after a wait
        presents a fresh timestamp rather than the one that went stale.
        """
        headers = {CONTENT_TYPE_HEADER: content_type}
        if self._bearer is not None:
            headers.update(authorization(self._bearer))
        if signed:
            if self._secret is None:
                return None, UNSIGNED_NOTE
            headers.update(ingress_headers(body, self._secret, self._clock.now()))
        try:
            return self._transport.send(
                method, path, body, headers, timeout=self.operation_timeout()
            ), None
        except OSError as error:
            return None, f"the Collector could not be reached ({type(error).__name__})"

    def _accepted(self, reply: Reply, sent: int, attempts: int) -> TransmitResult:
        """Turn an accepted reply into a result, reading the halt fields from it."""
        envelope = read_envelope(reply.body)
        if envelope is None:
            return TransmitResult(transmitted=sent, attempts=attempts, observed=False)
        return TransmitResult(
            transmitted=sent,
            attempts=attempts,
            observed=True,
            halted=envelope.halted,
            halt_reason=envelope.halt_reason,
            pending_approvals=envelope.pending_approvals,
        )

    def _spool_all(
        self,
        batch: SpooledBatch,
        new: tuple[Event, ...],
        *,
        note: str | None,
        attempts: int,
    ) -> TransmitResult:
        """Put the claim back and buffer the new records (Requirement 6.1).

        The claim is released rather than discarded, and the new records are
        appended after it, so nothing is dropped and the bound is enforced once
        over the whole file rather than twice over halves of it.
        """
        released = self._spool.release(batch)
        appended = self._spool.append(new).written
        return TransmitResult(
            spooled=released + appended,
            attempts=attempts,
            observed=False,
            note=note,
        )


def _status_note(status: int) -> str:
    """The note a refused or failed response contributes to the diagnostic line."""
    return f"the Collector answered with status {status}"


def _read_results(body: bytes) -> tuple[RecallResult, ...]:
    """Read the recall results from a response, skipping any entry of no shape.

    A result that cannot be read is dropped rather than failing the response,
    because a partially readable answer is still an answer and the alternative is
    an empty injection where a usable one was available.
    """
    try:
        decoded: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return ()
    if not isinstance(decoded, Mapping):
        return ()
    entries = decoded.get("results")
    if not isinstance(entries, list):
        return ()
    results: list[RecallResult] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        parsed = _read_result(entry)
        if parsed is not None:
            results.append(parsed)
    return tuple(results)


def _read_result(entry: Mapping[str, object]) -> RecallResult | None:
    """One recall result, or None when a required field is absent or of no shape."""
    artifact_id = _as_uuid(entry.get("artifact_id"))
    session_id = _as_uuid(entry.get("session_id"))
    distance = entry.get("distance")
    occurred_at = _as_text(entry.get("occurred_at"))
    if artifact_id is None or session_id is None or occurred_at is None:
        return None
    if isinstance(distance, bool) or not isinstance(distance, (int, float)):
        return None
    try:
        moment = parse_timestamp(occurred_at)
    except ValueError:
        return None
    confidence = entry.get("confidence")
    return RecallResult(
        artifact_id=artifact_id,
        distance=float(distance),
        outcome=_as_text(entry.get("outcome")) or "",
        session_id=session_id,
        machine_id=_as_text(entry.get("machine_id")) or "",
        occurred_at=moment,
        excerpt=_as_text(entry.get("excerpt")) or "",
        kind=_as_text(entry.get("kind")) or "",
        confidence=(
            float(confidence)
            if not isinstance(confidence, bool) and isinstance(confidence, (int, float))
            else None
        ),
    )


# ---------------------------------------------------------------------------
# One invocation
# ---------------------------------------------------------------------------


def capture_context(
    invocation: HookInvocation,
    *,
    configuration: Configuration,
    clock: Clock,
    machine_id: str | None = None,
) -> tuple[CaptureContext, tuple[str, ...]]:
    """Resolve the identity one invocation's Events are built against.

    Returns the context and the notes resolution owed: the unassigned-Client
    warning of Requirement 1.6, and a note when the payload named no session key
    and the run is therefore attributed to a Session scoped to this machine and
    tool rather than to the vendor's own run.
    """
    notes: list[str] = []
    resolved_machine = resolve_machine_id(configuration) if machine_id is None else machine_id
    client = resolve_client(invocation.workspace_path or "", configuration=configuration)
    if not client.assigned:
        notes.append(UNASSIGNED_NOTE)

    session_key = invocation.session_key
    if not session_key:
        session_key = f"{resolved_machine}\x1f{invocation.tool}"
        notes.append("the payload named no session key, so a machine-scoped Session was used")

    spawn = invocation.subagent
    parent = (
        derive_session_id(invocation.tool, spawn.parent_session_key)
        if spawn is not None and spawn.parent_session_key
        else None
    )
    settings = _redaction_settings(configuration)
    context = CaptureContext(
        session_id=derive_session_id(invocation.tool, session_key),
        client=client,
        machine_id=resolved_machine,
        agent_cli=invocation.tool,
        clock=clock,
        team_id=configuration.optional_text("MOLT_TEAM_ID"),
        workspace_path=invocation.workspace_path,
        parent_session_id=parent,
        spawning_event_id=None if spawn is None else spawn.spawning_event_id,
        depth=0 if parent is None else 1,
        redaction=settings,
    )
    return context, tuple(notes)


def _redaction_settings(configuration: Configuration) -> RedactionSettings:
    """The operator's redaction settings, with the pattern table imported lazily.

    The import happens here rather than at module scope because the pattern table
    is the largest thing the capture path would otherwise load, and a hook process
    that spools without mapping a payload never needs it (Requirement 1.8).
    """
    from molt.redact import RedactionSettings

    return RedactionSettings(
        disabled=configuration.flag("MOLT_REDACTION_DISABLED"),
        max_depth=configuration.integer("MOLT_REDACTION_MAX_DEPTH"),
        sensitive_names=frozenset(configuration.text_list("MOLT_REDACTION_SENSITIVE_NAMES")),
    )


def session_metadata(context: CaptureContext, started_at: datetime) -> JsonObject:
    """The Session record the Session metadata endpoint carries."""
    document: JsonObject = {
        "id": str(context.session_id),
        "client_id": str(context.client.id),
        "agent_cli": context.agent_cli,
        "machine_id": context.machine_id,
        "started_at": format_timestamp(started_at),
        "depth": context.depth,
    }
    if context.team_id is not None:
        document["team_id"] = context.team_id
    if context.workspace_path is not None:
        document["workspace_path"] = context.workspace_path
    if context.parent_session_id is not None:
        document["parent_session_id"] = str(context.parent_session_id)
    if context.spawning_event_id is not None:
        document["spawning_event_id"] = str(context.spawning_event_id)
    return document


def policy_halt_event(context: CaptureContext, reason: str) -> Event:
    """The Event a refusal records, queued rather than sent (Requirement 23.7).

    Queued because the refusal is already on standard output and the agent is
    waiting: writing it to the spool costs a file append, whereas transmitting it
    would cost a second round trip on the path that has the least time to spend.
    """
    return Event(
        id=uuid4(),
        session_id=context.session_id,
        client_id=context.client.id,
        category=EventCategory.POLICY_HALT,
        occurred_at=context.clock.now(),
        agent_cli=context.agent_cli,
        machine_id=context.machine_id,
        parent_event_id=None,
        payload={"reason": reason},
        redacted=False,
        text_body=None,
    )


def dispatch(
    tool: str,
    raw: bytes,
    *,
    event_name: str | None = None,
    adapter: HookAdapter | None = None,
    configuration: Configuration | None = None,
    transmitter: Transmitter | None = None,
    clock: Clock | None = None,
    sleep: Sleeper = DEFAULT_SLEEP,
    jitter: Jitter = DEFAULT_JITTER,
) -> HookOutcome:
    """Handle one hook payload and decide what the agent is told.

    Every failure inside is turned into a note rather than propagated, so the
    entry point's own handler is a second net rather than the only one. The
    ordering is deliberate: the payload is mapped first, then memory is asked, then
    the batch is placed, so the one round trip that carries the Events is also the
    one that carries the halt state the decision is made from.
    """
    if tool not in SUPPORTED_TOOLS:
        return HookOutcome(notes=(f"the hook token {tool!r} names no supported agent tool",))

    reading: Clock = SystemClock() if clock is None else clock
    try:
        resolved = configuration if configuration is not None else load_configuration()
        fired = load_adapter(tool) if adapter is None else adapter
        invocation = fired.parse(raw)
    except Exception as error:
        return HookOutcome(notes=(failure_note(error),))

    if event_name and not invocation.event_name:
        invocation = replace(invocation, event_name=event_name)

    notes: list[str] = []
    sender = transmitter
    try:
        context, resolution_notes = capture_context(
            invocation, configuration=resolved, clock=reading
        )
        notes.extend(resolution_notes)
        events = tuple(fired.to_events(invocation, context))
        if sender is None:
            sender = Transmitter.from_configuration(
                resolved,
                clock=reading,
                sleep=sleep,
                jitter=jitter,
                started=reading.monotonic(),
            )
        return _decide(fired, invocation, context, events, sender, notes)
    except Exception as error:
        notes.append(failure_note(error))
        return HookOutcome(notes=tuple(notes))
    finally:
        if transmitter is None and sender is not None:
            sender.close()


def _decide(
    adapter: HookAdapter,
    invocation: HookInvocation,
    context: CaptureContext,
    events: tuple[Event, ...],
    sender: Transmitter,
    notes: list[str],
) -> HookOutcome:
    """Ask memory, place the batch, and turn what came back into a decision."""
    capabilities = adapter.capabilities()
    recalled = RecallOutcome()
    if invocation.recall_query:
        recalled = sender.recall(invocation.recall_query, session_id=context.session_id)

    if any(event.category is EventCategory.SESSION_START for event in events):
        sender.put_session(context.session_id, session_metadata(context, context.clock.now()))

    result = sender.emit(events)
    if result.note is not None:
        notes.append(result.note)

    reason = result.blocking_reason(events)
    if reason is None and recalled.envelope is not None:
        reason = _envelope_reason(recalled.envelope, events)
    observed = result.observed or recalled.envelope is not None

    if reason is not None:
        sender.spool.append((policy_halt_event(context, reason),))
        notes.append(f"the action was refused because {reason}")
        if not capabilities.blocking_decision:
            notes.append("the tool documents no blocking channel, so the refusal is advisory")
        return HookOutcome(
            stdout=adapter.blocking_response(reason),
            notes=tuple(notes),
            events=events,
            transmitted=result.transmitted,
            spooled=result.spooled,
            blocked=True,
        )

    if not observed:
        notes.append(UNOBSERVED_HALT_NOTE)

    stdout = b""
    if invocation.recall_query:
        # Called whatever the capability flag says: where a tool documents only an
        # advisory text channel the adapter writes the block there and reports the
        # flag as false, so the flag records which channel was used rather than
        # whether results are surfaced at all.
        stdout = adapter.context_injection(list(recalled.results))
        if not capabilities.context_injection:
            notes.append("the tool documents no injection envelope, so results are advisory")
    if recalled.note is not None:
        notes.append(recalled.note)
    return HookOutcome(
        stdout=stdout,
        notes=tuple(notes),
        events=events,
        transmitted=result.transmitted,
        spooled=result.spooled,
    )


def _envelope_reason(envelope: Envelope, events: tuple[Event, ...]) -> str | None:
    """The refusal a recall envelope carries, when it carries one."""
    return TransmitResult(
        observed=True,
        halted=envelope.halted,
        halt_reason=envelope.halt_reason,
        pending_approvals=envelope.pending_approvals,
    ).blocking_reason(events)


def map_payload(tool: str, payload: Mapping[str, JsonValue]) -> list[Event]:
    """Map one already-decoded hook payload to Events, for a caller holding one.

    The payload is handed to the tool's own parser rather than read here, because
    the reading of a vendor payload belongs to that vendor's adapter and nowhere
    else (Requirement 1.9).
    """
    adapter = load_adapter(tool)
    invocation = adapter.parse(_json_body(dict(payload)))
    configuration = load_configuration()
    context, _ = capture_context(invocation, configuration=configuration, clock=SystemClock())
    return adapter.to_events(invocation, context)


def emit(events: list[Event]) -> TransmitResult:
    """Place a list of Events with the Collector, spooling whatever does not land."""
    configuration = load_configuration()
    sender = Transmitter.from_configuration(configuration)
    try:
        return sender.emit(events)
    finally:
        sender.close()


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def failure_note(error: BaseException) -> str:
    """One line describing a failure, with the description redacted and cut.

    The message is redacted because a failure message is the likeliest place for a
    captured value to reach standard error: an exception raised while reading a
    payload often quotes the payload. Redaction imports the pattern table, which
    is why it happens here, on a path that has already lost its budget, rather
    than at module scope.
    """
    try:
        from molt.redact import redact_text

        described, _ = redact_text(str(error))
    except Exception:
        described = ""
    cut = described[:NOTE_LIMIT].replace("\n", " ").replace("\r", " ").strip()
    named = type(error).__name__
    return f"{named}: {cut}" if cut else named


def main(argv: Sequence[str] | None = None) -> int:
    """Run one hook invocation and exit 0, whatever happened (Requirements 1.7, 6.6).

    The arguments are the shim's own, after the program name: the agent tool token
    and the vendor's event name. Standard input carries the payload as bytes and is
    never decoded here, so a payload that is not valid UTF-8 reaches the adapter as
    the bytes it was and fails there, inside the handler, rather than at the
    boundary where a decode would raise before any handler existed.
    """
    notes: list[str] = []
    body = b""
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if not arguments:
            notes.append("the hook shim was invoked with no agent tool token")
        else:
            outcome = dispatch(
                arguments[0],
                read_payload(),
                event_name=arguments[1] if len(arguments) > 1 else None,
            )
            body = outcome.stdout
            notes.extend(outcome.notes)
    except Exception as error:
        notes.append(failure_note(error))
    write_decision(body)
    write_diagnostic(notes)
    return EXIT_OK


def read_payload() -> bytes:
    """Read the hook payload from standard input as bytes, never as text."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return b""
    read = stream.read()
    return read if isinstance(read, bytes) else b""


def write_decision(body: bytes) -> None:
    """Write the vendor decision to standard output, and nothing else ever does.

    An empty body writes nothing at all, rather than writing an empty line, because
    a tool reading its hook's output should see silence where no decision was made.
    """
    if not body:
        return
    stream = getattr(sys.stdout, "buffer", None)
    try:
        if stream is None:
            sys.stdout.write(body.decode("utf-8", errors="replace"))
        else:
            stream.write(body)
        sys.stdout.flush()
    except (OSError, ValueError):
        return


def write_diagnostic(notes: Sequence[str]) -> None:
    """Write the accumulated notes to standard error as exactly one line.

    Joining rather than writing each note as it arose is what makes *at most one
    diagnostic line* hold by construction: an invocation that resolved an
    unassigned Client, failed to transmit, and could not observe halt state has
    three things to say and one line to say them on.
    """
    written = [note.replace("\n", " ").replace("\r", " ").strip() for note in notes]
    line = "; ".join(note for note in written if note)
    if not line:
        return
    try:
        sys.stderr.write(f"{COMPONENT}: {line}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        return


if __name__ == "__main__":  # pragma: no cover - the console script calls main directly
    raise SystemExit(main())
