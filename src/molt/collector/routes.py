"""The Collector's route table, its request bounds, and the wire shapes it reads.

This module holds everything about a Collector request that is decided before a
connection is leased: which route was addressed, whether the body is within the
configured bound, which records a batch carries, and what a response envelope
looks like. It reaches no cluster and holds no credential, so the whole of it is
exercisable without a database and without a parameter store.

Five claims arrange it.

**The bound is on the body the sender wrote, and it is read from a length rather
than from a decoded body.** The subject of Requirements 5.10 and 5.11 is the
request body, which is the bytes the sender wrote: a transport encoding is the
platform's decision about how to hand those bytes over rather than part of what
the caller sent, and the configured maximum is one value rather than one value per
carriage. So a caller may carry a payload of exactly the maximum however the
transport encodes it. That costs nothing in what the bound protects, because an
encoded body's character count exceeds its payload by a fixed factor of four
thirds, so the work one request can demand stays bounded by the configured
maximum either way. The character count, the declared content length, and the
exact byte length the payload occupies are all obtainable by arithmetic, so an
oversized request is refused before anything is decoded and before any record is
looked at. The boundary is inclusive: a body whose length equals the configured
maximum is processed, and a body one byte longer is refused, which is what
`exceeds_bound` spells so that no caller has to infer it.

**A blank line is a separator rather than a record.** The batch body is
newline-delimited and the capture side terminates every record with the
separator, so the text after the final separator is empty by construction.
Counting that emptiness as a malformed record would report a rejection on every
well-formed batch. The batch size a partial-batch response reports is therefore
the number of non-blank lines, which `split_records` is the single definition of
(Requirement 5.6).

**A record is rejected for one of three stated reasons, and the reasons are
distinguishable.** A line that is not valid UTF-8 is unreadable, a line that is
not one JSON object is unparsed, and a line that is a JSON object the Event
shape refuses is invalid. The three are told apart rather than collapsed,
because an operator reading a rejection count needs to know whether a hook is
truncating its output or sending a field the model does not admit. A fourth
reason belongs to the write rather than to the read: a record the cluster
refuses because it names a row that does not exist.

**There is no per-record size bound.** The only size bound in this design is the
per-request one, and it is enforced before the body is split. A single line
longer than that bound therefore cannot appear inside a request that was
accepted, and a long line inside an accepted request is judged by whether it
parses and validates like any other.

**Every response carries the halt fields.** The counts, the halt state, the halt
reason, and the queued approvals are one envelope shape, produced here and read
by the capture side's own envelope reader, so the two sides cannot disagree about
the field names (Requirements 5.6, 23.7, 23.9).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from molt.config.resolve import Configuration
from molt.models.event import (
    Event,
    JsonObject,
    JsonValue,
    deserialise_event,
    parse_timestamp,
)
from molt.models.session import Session, SessionOutcome

__all__ = [
    "AUTHENTICATED_KINDS",
    "CONTENT_LENGTH_HEADER",
    "CONTENT_TYPE_HEADER",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_RESERVED_CONCURRENCY",
    "EVENTS_PATH",
    "HEALTH_PATH",
    "JSON_CONTENT_TYPE",
    "MAX_BODY_KEY",
    "MAX_RECALL_RESULTS",
    "RECALL_PATH",
    "RECORD_SEPARATOR",
    "RESERVED_CONCURRENCY_KEY",
    "SESSIONS_PREFIX",
    "SIGNED_KINDS",
    "HaltReport",
    "Headers",
    "ReadBatch",
    "RecallAnswer",
    "RecallQuery",
    "RejectionReason",
    "Request",
    "Response",
    "Route",
    "RouteKind",
    "declared_content_length",
    "envelope",
    "exceeds_bound",
    "match_path",
    "max_body_bytes",
    "method_of",
    "read_body",
    "read_document",
    "read_recall_query",
    "read_records",
    "read_session_metadata",
    "reserved_concurrency",
    "response_headers",
    "session_for",
    "split_records",
    "transport_length",
]

# The four routes the Collector serves. The paths are the ones the capture side
# addresses; a unit test compares them against the capture side's own constants
# so the two cannot drift while staying independent at import time.
EVENTS_PATH: Final[str] = "/events"
SESSIONS_PREFIX: Final[str] = "/sessions/"
RECALL_PATH: Final[str] = "/recall"
HEALTH_PATH: Final[str] = "/health"

# The delimiter between records of a batch body.
RECORD_SEPARATOR: Final[bytes] = b"\n"

# The request headers this module reads by name.
CONTENT_LENGTH_HEADER: Final[str] = "Content-Length"
CONTENT_TYPE_HEADER: Final[str] = "Content-Type"
JSON_CONTENT_TYPE: Final[str] = "application/json"

# The configuration keys the two deployment bounds are read from, and the
# defaults the surface declares for them. The concurrency ceiling is read here
# rather than in the deployment template so that the template and the running
# handler quote one value (Requirement 5.12).
MAX_BODY_KEY: Final[str] = "MOLT_COLLECTOR_MAX_BODY_BYTES"
RESERVED_CONCURRENCY_KEY: Final[str] = "MOLT_COLLECTOR_RESERVED_CONCURRENCY"
DEFAULT_MAX_BODY_BYTES: Final[int] = 5 * 1024 * 1024
DEFAULT_RESERVED_CONCURRENCY: Final[int] = 10

# The ceiling on how many neighbours one recall request may ask for. A request
# asking for more is served the ceiling rather than refused, because a caller
# asking for too much still wants an answer.
MAX_RECALL_RESULTS: Final[int] = 100


class RouteKind(StrEnum):
    """The four routes, named rather than described by their paths."""

    EVENTS = "events"
    SESSION = "session"
    RECALL = "recall"
    HEALTH = "health"


# Every route other than the health route requires the bearer token
# (Requirements 5.4, 30.5). The two ingest routes require the Ingress_Signature
# in addition, and the recall route deliberately does not (Requirement 47.12).
AUTHENTICATED_KINDS: Final[frozenset[RouteKind]] = frozenset(
    {RouteKind.EVENTS, RouteKind.SESSION, RouteKind.RECALL}
)
SIGNED_KINDS: Final[frozenset[RouteKind]] = frozenset({RouteKind.EVENTS, RouteKind.SESSION})

_METHODS: Final[Mapping[RouteKind, str]] = {
    RouteKind.EVENTS: "POST",
    RouteKind.SESSION: "PUT",
    RouteKind.RECALL: "POST",
    RouteKind.HEALTH: "GET",
}


class RejectionReason(StrEnum):
    """Why one record of a batch was not persisted.

    The three read-time reasons are told apart at the point the record is read.
    The write-time reason is applied to a record the cluster refused because it
    named a row no table holds, which is a fault in the request rather than in
    the cluster.
    """

    UNREADABLE = "unreadable"
    UNPARSED = "unparsed"
    INVALID = "invalid"
    REFUSED = "refused"


class Headers(Mapping[str, str]):
    """One request's headers, looked up without regard to case.

    A transport may deliver header names lowercased, capitalised, or as the
    sender spelled them. Case-insensitive lookup is provided here, once, so no
    caller downstream has to remember which of the three it is holding. The
    mapping is read-only and iterates the names as they arrived.
    """

    __slots__ = ("_by_lowercase", "_original")

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._original: dict[str, str] = {}
        self._by_lowercase: dict[str, str] = {}
        for name, value in (values or {}).items():
            self._original[name] = value
            self._by_lowercase[name.lower()] = value

    def __getitem__(self, name: str) -> str:
        """The value under a name, matched without regard to case."""
        return self._by_lowercase[name.lower()]

    def __iter__(self) -> Iterator[str]:
        """The header names, as they arrived."""
        return iter(self._original)

    def __len__(self) -> int:
        """How many headers arrived."""
        return len(self._original)


@dataclass(frozen=True, slots=True)
class Route:
    """A matched route and the identifier its path named, when it names one."""

    kind: RouteKind
    session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Request:
    """One request, reduced to what every route reads from it.

    The body is the exact bytes the transport carried, taken before any decode of
    the content, because that is what the Ingress_Signature covers
    (Requirement 47.2).
    """

    method: str
    path: str
    headers: Headers
    body: bytes


@dataclass(frozen=True, slots=True)
class Response:
    """One response, as a status and a document the handler renders."""

    status: int
    document: JsonObject


@dataclass(frozen=True, slots=True)
class HaltReport:
    """The halt state one Session is in, as every response envelope carries it.

    The default is the report for a Session nothing is known against: not
    halted, no reason, no queued approval. It is a default rather than an
    absence because the envelope always carries the three fields.
    """

    halted: bool = False
    halt_reason: str | None = None
    pending_approvals: tuple[JsonObject, ...] = ()


@dataclass(frozen=True, slots=True)
class RecallAnswer:
    """What a recall query produced, together with the halt state it observed."""

    results: tuple[JsonObject, ...] = ()
    halt: HaltReport = field(default_factory=HaltReport)


@dataclass(frozen=True, slots=True)
class RecallQuery:
    """One recall request, after the body has been read and bounded."""

    query_text: str
    limit: int
    session_id: UUID


@dataclass(frozen=True, slots=True)
class ReadBatch:
    """The outcome of reading a batch body: the records that parsed, and the rest.

    One rejection entry is held per rejected record rather than one count per
    reason, so the order the rejections arose in is preserved and the counts are
    derived from it. The sum of the well-formed records and the rejections is the
    batch size (Requirement 5.6).
    """

    events: tuple[Event, ...] = ()
    rejections: tuple[RejectionReason, ...] = ()

    @property
    def records(self) -> int:
        """How many records the batch carried."""
        return len(self.events) + len(self.rejections)


# ---------------------------------------------------------------------------
# The route table
# ---------------------------------------------------------------------------


def match_path(path: str) -> Route | None:
    """Match a request path to a route, or report that it addresses none.

    A trailing separator is ignored, so a caller that appended one addresses the
    same route. A Session path whose remainder is not an identifier matches
    nothing, because a path that names no Session addresses no resource.
    """
    normalised = path.rstrip("/") or "/"
    if normalised == HEALTH_PATH:
        return Route(RouteKind.HEALTH)
    if normalised == EVENTS_PATH:
        return Route(RouteKind.EVENTS)
    if normalised == RECALL_PATH:
        return Route(RouteKind.RECALL)
    if not normalised.startswith(SESSIONS_PREFIX):
        return None
    remainder = normalised[len(SESSIONS_PREFIX) :]
    try:
        session_id = UUID(remainder)
    except ValueError:
        return None
    return Route(RouteKind.SESSION, session_id=session_id)


def method_of(kind: RouteKind) -> str:
    """The one method a route answers."""
    return _METHODS[kind]


# ---------------------------------------------------------------------------
# The request bound
# ---------------------------------------------------------------------------


def max_body_bytes(configuration: Configuration) -> int:
    """The configured maximum request body size, in bytes (Requirement 5.10)."""
    return configuration.integer(MAX_BODY_KEY)


def reserved_concurrency(configuration: Configuration) -> int:
    """The configured reserved concurrency ceiling (Requirement 5.12).

    Read here so that the deployment template and the running handler quote one
    value rather than two that happen to agree.
    """
    return configuration.integer(RESERVED_CONCURRENCY_KEY)


def declared_content_length(headers: Mapping[str, str]) -> int | None:
    """The length the request declares for itself, or None when it declares none.

    A value that is not a non-negative whole number is treated as no declaration
    rather than as a fault, because the length the payload actually occupies is
    measured as well and is what the bound is ultimately taken against.
    """
    raw = headers.get(CONTENT_LENGTH_HEADER)
    if raw is None:
        return None
    try:
        value = int(raw.strip(), 10)
    except ValueError:
        return None
    return value if value >= 0 else None


def transport_length(body_text: str, *, base64_encoded: bool) -> int:
    """The exact byte length of the body, computed without decoding it.

    A transport-encoded body's decoded length follows from its character count
    and its padding, so the measurement is arithmetic. A body carried as text is
    measured by encoding it, which is the measurement rather than a decode of the
    content: no record is read and nothing is parsed.
    """
    if base64_encoded:
        encoded = "".join(body_text.split())
        padding = len(encoded) - len(encoded.rstrip("="))
        return max(len(encoded) * 3 // 4 - padding, 0)
    return len(body_text.encode("utf-8"))


def exceeds_bound(
    headers: Mapping[str, str],
    body_text: str,
    *,
    base64_encoded: bool,
    maximum: int,
) -> bool:
    """Whether a request body is larger than the configured maximum.

    The maximum is taken against the bytes the sender wrote, which for a
    transport-encoded request is the payload rather than the characters the
    transport carried. The boundary is inclusive on the accepted side: a body of
    exactly the maximum is within the bound and a body one byte longer is not.

    Three readings are taken and none of them decodes the body. The character count
    settles a request in one direction only, and which direction that is depends on
    the carriage. It is a lower bound on the byte length of a text body, so a text
    body counting more characters than the maximum is over it and is refused on the
    cheapest reading. It is an upper bound on the payload of an encoded one, so an
    encoded body counting no more characters than the maximum carries a payload
    inside the bound whatever it decodes to. Neither reading is turned around: an
    upper bound being exceeded settles nothing, so an encoded body counting more
    characters than the maximum is passed on to be measured rather than refused
    here. The declared length is consulted next, so a sender that announces an
    oversized body is refused on its own statement, and it is consulted for both
    carriages because it declares the same quantity the maximum is taken against.
    The exact length is measured last, by arithmetic over the encoded form rather
    than by decoding it, which is what settles an encoded body the character count
    could not and what makes a sender that understates its length in the header no
    more admissible than one that states it honestly.
    """
    if not base64_encoded and len(body_text) > maximum:
        return True
    declared = declared_content_length(headers)
    if declared is not None and declared > maximum:
        return True
    if base64_encoded and len(body_text) <= maximum:
        return False
    return transport_length(body_text, base64_encoded=base64_encoded) > maximum


def read_body(body_text: str, *, base64_encoded: bool) -> bytes:
    """The exact request body bytes, decoded from the transport encoding alone.

    This is a transport decode rather than a content decode: it produces the bytes
    the sender wrote, which are the bytes the signature covers and the bytes the
    records are then split from.
    """
    if not base64_encoded:
        return body_text.encode("utf-8")
    try:
        return base64.b64decode(body_text.encode("ascii"), validate=False)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("the request body is not valid transport encoding") from exc


# ---------------------------------------------------------------------------
# The batch body
# ---------------------------------------------------------------------------


def split_records(body: bytes) -> tuple[bytes, ...]:
    """The records a newline-delimited batch body carries.

    Blank lines are separators rather than records. The capture side terminates
    every record with the separator, so the final line of a well-formed batch is
    empty; counting it would report a rejection on every batch. This is the one
    definition of what a batch's size is.
    """
    return tuple(line for line in body.split(RECORD_SEPARATOR) if line.strip())


def read_records(body: bytes) -> ReadBatch:
    """Read a batch body, keeping every well-formed record and counting the rest.

    Nothing raised by reading one record reaches the caller: a malformed record is
    a rejection to report rather than a failure of the request it arrived in
    (Requirement 5.6). The reason is established by asking the cheaper question
    first and only re-reading a record that was already refused, so a batch of
    well-formed records is parsed once.
    """
    events: list[Event] = []
    rejections: list[RejectionReason] = []
    for line in split_records(body):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            rejections.append(RejectionReason.UNREADABLE)
            continue
        try:
            events.append(deserialise_event(text))
        except (ValueError, TypeError):
            rejections.append(_refused_reason(text))
    return ReadBatch(events=tuple(events), rejections=tuple(rejections))


def _refused_reason(text: str) -> RejectionReason:
    """Whether a refused record failed to parse at all or failed the Event shape."""
    try:
        decoded: object = json.loads(text)
    except ValueError:
        return RejectionReason.UNPARSED
    return RejectionReason.INVALID if isinstance(decoded, dict) else RejectionReason.UNPARSED


# ---------------------------------------------------------------------------
# The Session record
# ---------------------------------------------------------------------------


def session_for(events: Sequence[Event]) -> Session:
    """The Session record one batch group implies, for a Session that may be absent.

    Everything here comes from the Events themselves: the identity, the tenant,
    the tool, the machine, and the start instant, which is the earliest instant
    the group observed. Nothing is invented and no counter is presented, because
    the counters belong to the increment path and the depth is derived by the
    cluster from the parent row rather than trusted from here (Requirement 5.7).
    """
    if not events:
        raise ValueError("a Session record cannot be derived from no Event")
    first = events[0]
    started_at = min(event.occurred_at for event in events)
    return Session(
        id=first.session_id,
        client_id=first.client_id,
        agent_cli=first.agent_cli,
        machine_id=first.machine_id,
        team_id=None,
        attribution={},
        workspace_path=None,
        started_at=started_at,
        ended_at=None,
        outcome=SessionOutcome.IN_PROGRESS,
        parent_session_id=None,
        spawning_event_id=None,
        depth=0,
        tool_call_count=0,
        model_request_count=0,
        error_count=0,
        token_count=0,
        cost_usd=Decimal(0),
        halted=False,
        halted_at=None,
        halt_reason=None,
        halt_rule_id=None,
    )


def read_session_metadata(document: Mapping[str, JsonValue], *, session_id: UUID) -> Session:
    """Read the Session record the metadata route carries (Requirement 5.2).

    The identifier in the path is authoritative and the document must agree with
    it, so a request cannot address one Session and describe another. The nesting
    depth the document states is not sent to the cluster: the cluster derives it
    from the parent row, and what is passed here is only the half the model itself
    constrains, which is zero for a Session naming no parent.

    Raises:
        ValueError: A required field is absent, is of the wrong shape, or names a
            Session other than the one the path addressed.
    """
    declared = _required_uuid(document, "id")
    if declared != session_id:
        raise ValueError("the Session metadata describes a Session the path does not address")
    parent_session_id = _optional_uuid(document, "parent_session_id")
    depth = _optional_int(document, "depth") or 0
    return Session(
        id=declared,
        client_id=_required_uuid(document, "client_id"),
        agent_cli=_required_text(document, "agent_cli"),
        machine_id=_required_text(document, "machine_id"),
        team_id=_optional_text(document, "team_id"),
        attribution={},
        workspace_path=_optional_text(document, "workspace_path"),
        started_at=parse_timestamp(_required_text(document, "started_at")),
        ended_at=None,
        outcome=SessionOutcome.IN_PROGRESS,
        parent_session_id=parent_session_id,
        spawning_event_id=_optional_uuid(document, "spawning_event_id"),
        depth=0 if parent_session_id is None else max(depth, 1),
        tool_call_count=0,
        model_request_count=0,
        error_count=0,
        token_count=0,
        cost_usd=Decimal(0),
        halted=False,
        halted_at=None,
        halt_reason=None,
        halt_rule_id=None,
    )


def read_recall_query(document: Mapping[str, JsonValue]) -> RecallQuery:
    """Read the recall request body, bounding how many neighbours it may ask for.

    Raises:
        ValueError: The query text is absent or empty, the identifier is not one,
            or the requested count is not a positive whole number.
    """
    query_text = _required_text(document, "query_text")
    if not query_text.strip():
        raise ValueError("a recall query carries the text to search for")
    requested = _optional_int(document, "k")
    if requested is not None and requested < 1:
        raise ValueError("a recall query asks for at least one result")
    return RecallQuery(
        query_text=query_text,
        limit=min(MAX_RECALL_RESULTS, requested or MAX_RECALL_RESULTS),
        session_id=_required_uuid(document, "session_id"),
    )


# ---------------------------------------------------------------------------
# The response envelope
# ---------------------------------------------------------------------------


def envelope(
    *,
    accepted: int = 0,
    rejected: int = 0,
    halt: HaltReport | None = None,
    rejections: Mapping[str, int] | None = None,
    results: Sequence[JsonObject] | None = None,
) -> JsonObject:
    """Build the one response envelope every ingest and recall response carries.

    The five envelope fields are always present, so an unhalted Session and a
    Session nothing was read for are reported in the same shape the capture side
    reads either way. The rejection breakdown is a count per stated reason and
    carries no record content, so it is safe on a response and useful to an
    operator reading one.
    """
    report = HaltReport() if halt is None else halt
    document: JsonObject = {
        "accepted": accepted,
        "rejected": rejected,
        "halted": report.halted,
        "halt_reason": report.halt_reason,
        "pending_approvals": [dict(approval) for approval in report.pending_approvals],
    }
    if rejections:
        document["rejections"] = {name: count for name, count in rejections.items() if count}
    if results is not None:
        document["results"] = [dict(result) for result in results]
    return document


# ---------------------------------------------------------------------------
# Document field readers
# ---------------------------------------------------------------------------


def _required(document: Mapping[str, JsonValue], key: str) -> JsonValue:
    """One field a document must carry."""
    if key not in document:
        raise ValueError(f"the request document omits the required field {key!r}")
    return document[key]


def _required_text(document: Mapping[str, JsonValue], key: str) -> str:
    """One field a document must carry as text."""
    value = _required(document, key)
    if not isinstance(value, str):
        raise ValueError(f"the request field {key!r} must be text")
    return value


def _required_uuid(document: Mapping[str, JsonValue], key: str) -> UUID:
    """One field a document must carry as an identifier."""
    try:
        return UUID(_required_text(document, key))
    except ValueError as exc:
        raise ValueError(f"the request field {key!r} must be an identifier") from exc


def _optional_text(document: Mapping[str, JsonValue], key: str) -> str | None:
    """One optional text field, or None when it is absent or empty."""
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"the request field {key!r} must be text")
    return value or None


def _optional_uuid(document: Mapping[str, JsonValue], key: str) -> UUID | None:
    """One optional identifier field, or None when it is absent."""
    text = _optional_text(document, key)
    if text is None:
        return None
    try:
        return UUID(text)
    except ValueError as exc:
        raise ValueError(f"the request field {key!r} must be an identifier") from exc


def _optional_int(document: Mapping[str, JsonValue], key: str) -> int | None:
    """One optional whole-number field, or None when it is absent."""
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"the request field {key!r} must be a whole number")
    return value


def read_document(body: bytes) -> JsonObject:
    """Read a request body that carries one JSON object.

    Raises:
        ValueError: The body is not valid UTF-8, is not valid JSON, or is JSON
            that is not an object.
    """
    try:
        decoded: object = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("the request body is not one JSON object") from exc
    if not isinstance(decoded, dict):
        raise ValueError("the request body is not one JSON object")
    document: JsonObject = {}
    for key, value in decoded.items():
        if not isinstance(key, str):
            raise ValueError("the request body carries text keys only")
        document[key] = value
    return document


def response_headers() -> dict[str, str]:
    """The headers every response carries."""
    return {CONTENT_TYPE_HEADER: JSON_CONTENT_TYPE}
