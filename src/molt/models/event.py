"""The Event model, the Ledger category set, and the canonical Event wire form.

An Event is one immutable observation inside a Session. The dataclass is frozen
because an observation is never edited once captured, and slotted because Events
are allocated in volume on the capture path.

`EventCategory` is the single enumeration of Ledger categories in this codebase.
The schema check constraint holds the same values in the same order, and a
migration test compares the two, so the Python set and the SQL set cannot drift.

The wire form is the canonical JSON shape restricted to the Event field set:
UTF-8 without a byte order mark, keys sorted at every level, no insignificant
whitespace, identifiers as lowercase hyphenated UUID strings, timestamps as
RFC 3339 with a numeric offset at microsecond precision, and absent optional
fields omitted rather than emitted. Deserialising a serialisation yields an
equal Event, which is what the round-trip property asserts.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Final, TypeAlias
from uuid import UUID

# A JSON document as it is held in memory. The alias is recursive, which is what
# lets a payload be annotated without widening any parameter to a dynamic type.
JsonValue: TypeAlias = "str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None"
JsonObject: TypeAlias = "dict[str, JsonValue]"


class EventCategory(StrEnum):
    """Every category a Ledger row may carry, in schema-constraint order.

    The first fourteen are the base observation categories. `recall` records a
    pre-action memory query, `policy_halt` records a Kill_Switch decision, and
    `attribution_superseded` records the replacement of an Attribution_Version;
    each of the three exists because a governance surface obliges an Event that
    the base list does not name.
    """

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_PROMPT = "user_prompt"
    ASSISTANT_RESPONSE = "assistant_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    SHELL_COMMAND = "shell_command"
    DECISION = "decision"
    ERROR = "error"
    COST_RECORD = "cost_record"
    RECALL = "recall"
    POLICY_HALT = "policy_halt"
    ATTRIBUTION_SUPERSEDED = "attribution_superseded"


class EmbeddingState(StrEnum):
    """Whether an Artifact's vector is absent, owed, present, or unobtainable."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    EMBEDDED = "embedded"
    FAILED = "failed"


# The category values in the order the schema check constraint lists them. A
# migration test reads this tuple rather than restating the values.
EVENT_CATEGORY_VALUES: Final[tuple[str, ...]] = tuple(member.value for member in EventCategory)

# The states the embedding-state check constraints admit, in constraint order.
EMBEDDING_STATE_VALUES: Final[tuple[str, ...]] = tuple(member.value for member in EmbeddingState)

# The replacement character stands in for every byte sequence that is not valid
# UTF-8, so no undecodable content ever reaches an Event.
REPLACEMENT_DECODE_ERRORS: Final[str] = "replace"


def format_timestamp(value: datetime) -> str:
    """Render a timezone-aware instant in the canonical timestamp form."""
    require_aware(value, "a timestamp")
    return value.isoformat(timespec="microseconds")


def parse_timestamp(text: str) -> datetime:
    """Parse the canonical timestamp form back into a timezone-aware instant."""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        message = f"a timestamp must carry a numeric offset and microsecond precision: {text!r}"
        raise ValueError(message) from exc
    return require_aware(parsed, "a timestamp")


def require_aware(value: datetime, subject: str) -> datetime:
    """Return the instant unchanged, refusing a naive one.

    A naive instant is a correctness error rather than a stylistic one: the
    stored column carries an offset, so an instant without one has no defined
    position on the timeline.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{subject} must be timezone aware")
    return value


def decode_capture_text(raw: bytes) -> str:
    """Decode captured bytes to text, substituting for invalid sequences.

    This is the capture boundary for text: a hook payload, a tool result, or a
    subprocess stream may hold arbitrary bytes, and replacement here is what
    keeps an Event free of undecodable content.
    """
    return raw.decode("utf-8", errors=REPLACEMENT_DECODE_ERRORS)


def decode_capture_payload(payload: Mapping[str, object]) -> JsonObject:
    """Convert a captured mapping into a payload an Event may hold.

    Byte values are decoded with replacement, nested mappings and sequences are
    converted in place, and a value of no JSON type is refused rather than
    coerced, so a payload that reaches an Event is serialisable by construction.
    """
    return _decode_object(payload)


def _decode_object(value: Mapping[str, object]) -> JsonObject:
    decoded: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("a payload key must be text")
        decoded[key] = _decode_value(item)
    return decoded


def _decode_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("a payload number must be finite")
        return value
    if isinstance(value, bytes):
        return decode_capture_text(value)
    if isinstance(value, Mapping):
        return _decode_object({str(key): item for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return [_decode_value(item) for item in value]
    raise ValueError(f"a payload holds no value of type {type(value).__name__}")


def _reject_non_finite(payload: JsonObject) -> None:
    pending: list[JsonValue] = [payload]
    while pending:
        current = pending.pop()
        if isinstance(current, float) and not isfinite(current):
            raise ValueError("an Event payload must hold finite numbers only")
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


@dataclass(frozen=True, slots=True)
class Event:
    """One immutable observation within a Session.

    The digest columns of the Ledger row are absent on purpose: the content
    digest and the chain digests are computed by the statement that appends the
    row, so a captured Event never carries a value it did not observe.
    """

    id: UUID
    session_id: UUID
    client_id: UUID
    category: EventCategory
    occurred_at: datetime
    agent_cli: str
    machine_id: str
    parent_event_id: UUID | None
    payload: JsonObject
    redacted: bool
    text_body: str | None

    def __post_init__(self) -> None:
        EventCategory(self.category)
        require_aware(self.occurred_at, "an Event timestamp")
        _reject_non_finite(self.payload)


# The field names the wire form admits. Optional fields are absent from a
# serialisation rather than null, so membership rather than presence is checked.
EVENT_WIRE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "agent_cli",
        "category",
        "client_id",
        "id",
        "machine_id",
        "occurred_at",
        "parent_event_id",
        "payload",
        "redacted",
        "session_id",
        "text_body",
    }
)


def serialise_event(event: Event) -> str:
    """Render an Event in the canonical wire form."""
    fields: JsonObject = {
        "agent_cli": event.agent_cli,
        "category": str(event.category),
        "client_id": str(event.client_id),
        "id": str(event.id),
        "machine_id": event.machine_id,
        "occurred_at": format_timestamp(event.occurred_at),
        "payload": event.payload,
        "redacted": event.redacted,
        "session_id": str(event.session_id),
    }
    if event.parent_event_id is not None:
        fields["parent_event_id"] = str(event.parent_event_id)
    if event.text_body is not None:
        fields["text_body"] = event.text_body
    return json.dumps(
        fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def deserialise_event(text: str) -> Event:
    """Reconstruct an Event from the canonical wire form."""
    fields = _wire_fields(text)
    unknown = sorted(set(fields) - EVENT_WIRE_FIELDS)
    if unknown:
        raise ValueError(f"an Event serialisation carries no field named {unknown[0]!r}")
    return Event(
        id=_required_uuid(fields, "id"),
        session_id=_required_uuid(fields, "session_id"),
        client_id=_required_uuid(fields, "client_id"),
        category=EventCategory(_required_str(fields, "category")),
        occurred_at=parse_timestamp(_required_str(fields, "occurred_at")),
        agent_cli=_required_str(fields, "agent_cli"),
        machine_id=_required_str(fields, "machine_id"),
        parent_event_id=_optional_uuid(fields, "parent_event_id"),
        payload=_required_object(fields, "payload"),
        redacted=_required_bool(fields, "redacted"),
        text_body=_optional_str(fields, "text_body"),
    )


def _wire_fields(text: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("an Event serialisation must be one JSON object") from exc
    if not isinstance(decoded, dict):
        raise ValueError("an Event serialisation must be one JSON object")
    fields: dict[str, object] = {}
    for key, value in decoded.items():
        if not isinstance(key, str):
            raise ValueError("an Event serialisation carries text keys only")
        fields[key] = value
    return fields


def _required(fields: dict[str, object], key: str) -> object:
    if key not in fields:
        raise ValueError(f"an Event serialisation omits the required field {key!r}")
    return fields[key]


def _required_str(fields: dict[str, object], key: str) -> str:
    value = _required(fields, key)
    if not isinstance(value, str):
        raise ValueError(f"the Event field {key!r} must be text")
    return value


def _required_bool(fields: dict[str, object], key: str) -> bool:
    value = _required(fields, key)
    if not isinstance(value, bool):
        raise ValueError(f"the Event field {key!r} must be a boolean")
    return value


def _required_uuid(fields: dict[str, object], key: str) -> UUID:
    return _parse_uuid(_required_str(fields, key), key)


def _required_object(fields: dict[str, object], key: str) -> JsonObject:
    value = _required(fields, key)
    if not isinstance(value, Mapping):
        raise ValueError(f"the Event field {key!r} must be a JSON object")
    return _decode_object({str(name): item for name, item in value.items()})


def _optional_str(fields: dict[str, object], key: str) -> str | None:
    if key not in fields:
        return None
    return _required_str(fields, key)


def _optional_uuid(fields: dict[str, object], key: str) -> UUID | None:
    if key not in fields:
        return None
    return _required_uuid(fields, key)


def _parse_uuid(value: str, key: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"the Event field {key!r} must be a hyphenated UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"the Event field {key!r} must be a lowercase hyphenated UUID")
    return parsed
