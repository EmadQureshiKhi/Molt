"""The Event construction the five per-tool adapters share, and nothing else.

An adapter is written from one vendor's own published hook specification and no
adapter reads another vendor's payload shape (Requirements 1.9, 29.9). What the
five legitimately have in common is what happens *after* a payload has been read:
an Event carries the same field set whatever produced it, a Session-scoped payload
carries the same identity fields, a recall block is worded and ranked identically
so that only the vendor envelope differs (Requirement 13.6), and content too large
for a payload is reduced the same way. That is exactly what lives here.

Three claims arrange the module.

**Nothing here reads a vendor field name.** Every function takes values an adapter
has already lifted out of its own payload. A helper that took a payload and knew
where to look inside it would be the shared parsing this separation exists to
prevent, so no function here accepts a raw hook payload.

**Redaction happens at Event construction, once.** An adapter never redacts a
field itself; it hands the assembled payload to `event`, which redacts it, sets
the Event's redaction flag from whether anything changed, and returns the Event.
That makes it impossible for one adapter to build an Event whose payload skipped
the Redactor, and it puts the pattern-table import on one line of one module.

**The redaction import is deferred to the call.** The pattern table is the largest
thing the capture path could load and a hook process that maps no payload never
needs it, so it is imported inside the function that redacts rather than at module
scope (Requirement 1.8). The telemetry import is deferred for the same reason and
by the same means, so an adapter that builds no Event pays for neither.

**Redaction being switched off is recorded here, once per Event.** With redaction
disabled the Redactor returns the payload untouched and hands back the record the
operator is owed; discarding it would leave five adapters transmitting unredacted
payloads and text bodies with nothing anywhere saying so, which is the one outcome
Requirement 4.6 exists to prevent. It is emitted where the payload is redacted, so
one Event costs one record however many values the Redactor would have replaced
and however large the payload is (Requirement 1.8), and the emission is contained
so that reaching for the telemetry surface cannot become the way a capture hook
fails its host agent (Requirement 1.7).
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from molt.models.event import Event, EventCategory, JsonObject, JsonValue

if TYPE_CHECKING:  # pragma: no cover - imported for the annotations alone
    from collections.abc import Mapping, Sequence

    from molt.capture.protocol import CaptureContext, RecallResult

__all__ = [
    "COMPONENT",
    "DIGEST_NAME",
    "EXCERPT_LIMIT",
    "PAYLOAD_TEXT_CAP",
    "RECALL_EMPTY",
    "RECALL_HEADING",
    "REDACTION_DISABLED_MESSAGE",
    "body_fields",
    "bounded_value",
    "clip",
    "content_fields",
    "digest_hex",
    "error_event",
    "event",
    "json_bytes",
    "recall_block",
    "session_payload",
    "text_of",
]

# The component name a record from this module carries. The same name the
# decorator, the proxy, and the hook report, because Requirement 4.6 obliges one
# record wherever redaction was skipped and an operator searching for it should not
# have to know which of the capture paths produced the Event.
COMPONENT: Final[str] = "capture"

# The wording of that record, spelled once. It is the decorator's sentence with
# *and text bodies* added, because this path carries both.
REDACTION_DISABLED_MESSAGE: Final[str] = (
    "redaction is disabled, so this Session's payloads and text bodies are recorded unmodified"
)

# How much text a payload field may carry before it is replaced by its length and
# its digest. A hook payload can hold a whole file, and a Ledger row is not the
# place for one; the digest keeps the content identifiable without keeping it.
PAYLOAD_TEXT_CAP: Final[int] = 8192

# The digest a reduced field is identified by, named in the payload so a reader
# knows what to recompute.
DIGEST_NAME: Final[str] = "sha256"

# How much of one prior Artifact a recall block quotes. Long enough to recognise
# the prior attempt, short enough that five results stay inside a hook response.
EXCERPT_LIMIT: Final[int] = 320

# The wording of an injected recall block. Fixed text rather than per-adapter
# phrasing, because Requirement 13.6 differs across tools only in the envelope.
RECALL_HEADING: Final[str] = "Prior attempts at similar actions, nearest first."
RECALL_EMPTY: Final[str] = "No prior attempt resembles this action."


# ---------------------------------------------------------------------------
# Text reduction
# ---------------------------------------------------------------------------


def clip(text: str, limit: int) -> str:
    """Cut text to a bound, on a character boundary, adding nothing to it."""
    if limit < 0:
        raise ValueError("a text bound cannot be negative")
    return text if len(text) <= limit else text[:limit]


def digest_hex(text: str) -> str:
    """The hexadecimal digest of text, as a payload carries it in place of content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_of(value: JsonValue, *, limit: int = PAYLOAD_TEXT_CAP) -> str:
    """Render any lifted JSON value as bounded text.

    A vendor field that a specification types as an opaque JSON value may arrive
    as text, as a number, or as a structure, and an Event field that is text must
    hold text either way. Structures are rendered canonically so that the same
    structure always renders the same way and a digest over it is stable.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return clip(_canonical(value), limit)


def _canonical(value: JsonValue) -> str:
    """One structure's canonical rendering, so a digest over it is stable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bounded_value(value: JsonValue, *, cap: int = PAYLOAD_TEXT_CAP) -> JsonValue:
    """A lifted value as a payload may carry it: itself, or its digest when too large.

    A tool input or a tool result is whatever shape the vendor gave it, and one of
    them may hold a whole file. Keeping the shape where it fits preserves what a
    later query can read; replacing it with a length and a digest beyond the cap
    keeps the Ledger row bounded while leaving the content identifiable.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    rendered = value if isinstance(value, str) else _canonical(value)
    if len(rendered.encode("utf-8")) <= cap:
        return value
    return content_fields(rendered, cap=cap)


def body_fields(text: str) -> JsonObject:
    """The payload fields standing for text the Event carries in its text body.

    The length and the digest rather than the text itself, because the text body is
    where that text lives and one Event should not hold two copies of it.
    """
    encoded = text.encode("utf-8")
    return {
        "byte_length": len(encoded),
        "digest_algorithm": DIGEST_NAME,
        "digest": hashlib.sha256(encoded).hexdigest(),
    }


def content_fields(content: str, *, cap: int = PAYLOAD_TEXT_CAP) -> JsonObject:
    """The payload fields standing for a body of content.

    Content within the cap is carried as it is. Content beyond it is replaced by
    its byte length and its digest, which is what the mapping table asks for: a
    file write of a megabyte is identified rather than copied into the Ledger.
    """
    encoded = content.encode("utf-8")
    fields: JsonObject = {
        "byte_length": len(encoded),
        "digest_algorithm": DIGEST_NAME,
        "digest": hashlib.sha256(encoded).hexdigest(),
    }
    if len(encoded) <= cap:
        fields["content"] = content
    else:
        fields["content_omitted"] = True
    return fields


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def event(
    ctx: CaptureContext,
    category: EventCategory,
    payload: JsonObject,
    *,
    parent_event_id: UUID | None = None,
    text_body: str | None = None,
) -> Event:
    """Build one Event against a capture context, with its payload redacted.

    The identifier is fresh, the instant is read from the injected clock, and the
    identity fields come from the context, so an adapter decides only the category,
    the payload, and the parentage.
    """
    redacted_payload, payload_modified = _redact_payload(ctx, payload)
    redacted_text, text_modified = _redact_text(ctx, text_body)
    return Event(
        id=uuid4(),
        session_id=ctx.session_id,
        client_id=ctx.client.id,
        category=category,
        occurred_at=ctx.clock.now(),
        agent_cli=ctx.agent_cli,
        machine_id=ctx.machine_id,
        parent_event_id=parent_event_id,
        payload=redacted_payload,
        redacted=payload_modified or text_modified,
        text_body=redacted_text,
    )


def error_event(
    ctx: CaptureContext,
    exception_type: str,
    message: str,
    *,
    parent_event_id: UUID | None = None,
    extra: Mapping[str, JsonValue] | None = None,
) -> Event:
    """The Event an adapter-level failure records: the type and a redacted message."""
    payload: JsonObject = {
        "exception_type": exception_type,
        "message": clip(message, PAYLOAD_TEXT_CAP),
    }
    if extra is not None:
        payload.update(extra)
    return event(ctx, EventCategory.ERROR, payload, parent_event_id=parent_event_id)


def session_payload(
    ctx: CaptureContext,
    extra: Mapping[str, JsonValue] | None = None,
) -> JsonObject:
    """The identity fields a Session-scoped Event carries.

    The agent tool identity, the workspace, the machine, and the resolved Client
    are on every one of them, and the parentage fields are present only on a
    Session that a payload named a spawned subagent for (Requirement 1.4).
    """
    payload: JsonObject = {
        "agent_cli": ctx.agent_cli,
        "machine_id": ctx.machine_id,
        "client_id": str(ctx.client.id),
        "client_slug": ctx.client.slug,
        "client_assigned": ctx.client.assigned,
        "depth": ctx.depth,
    }
    if ctx.workspace_path is not None:
        payload["workspace_path"] = ctx.workspace_path
    if ctx.team_id is not None:
        payload["team_id"] = ctx.team_id
    if ctx.parent_session_id is not None:
        payload["parent_session_id"] = str(ctx.parent_session_id)
    if ctx.spawning_event_id is not None:
        payload["spawning_event_id"] = str(ctx.spawning_event_id)
    if extra is not None:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# What memory answers with, and how a decision is written
# ---------------------------------------------------------------------------


def recall_block(results: Sequence[RecallResult]) -> str:
    """Render recall results as the one block every adapter injects.

    Ordering is by ascending distance here rather than trusted from the caller, so
    the nearest prior attempt is first however the results arrived, and every tool
    presents the same ranking of the same text (Requirement 13.6).
    """
    if not results:
        return RECALL_EMPTY
    ranked = sorted(results, key=lambda item: (item.distance, str(item.artifact_id)))
    lines = [RECALL_HEADING]
    for position, item in enumerate(ranked, start=1):
        lines.append(
            f"{position}. distance {item.distance:.3f}"
            f", outcome {item.outcome or 'unknown'}"
            f", session {item.session_id}"
            f", machine {item.machine_id or 'unknown'}"
            f", observed {item.occurred_at.isoformat(timespec='seconds')}"
        )
        excerpt = clip(item.excerpt, EXCERPT_LIMIT).replace("\r", " ").strip()
        if excerpt:
            lines.append(f"   {excerpt}")
    return "\n".join(lines)


def json_bytes(document: JsonObject) -> bytes:
    """Render a hook response document as the bytes standard output receives."""
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Redaction, imported where it is used
# ---------------------------------------------------------------------------


def _redact_payload(ctx: CaptureContext, payload: JsonObject) -> tuple[JsonObject, bool]:
    """Redact an assembled payload, reporting whether anything changed.

    The Redactor's warning is emitted rather than dropped. Every Event this module
    builds passes through here, so the one record covers the text body as well and
    no Event is transmitted unredacted without a record naming its Session.
    """
    from molt.redact import RedactionSettings, redact_payload

    settings = ctx.redaction if ctx.redaction is not None else RedactionSettings()
    result = redact_payload(payload, session_id=ctx.session_id, settings=settings)
    if result.warning is not None:
        _note_redaction_disabled(ctx, result.warning.record)
    return result.payload, result.modified


def _redact_text(ctx: CaptureContext, text: str | None) -> tuple[str | None, bool]:
    """Redact a text body, reporting whether anything changed.

    No record is emitted here. The text-redaction call reports the modification
    and nothing else, and the payload call that precedes it on every Event has
    already named the Session once, so emitting again would be the second record
    for one Event that the latency budget does not want.
    """
    if text is None:
        return None, False
    from molt.redact import RedactionSettings, redact_text

    settings = ctx.redaction if ctx.redaction is not None else RedactionSettings()
    redacted, modified = redact_text(text, settings=settings)
    return redacted, modified


def _note_redaction_disabled(ctx: CaptureContext, record: str) -> None:
    """Record that this Session's content is being transmitted unmodified.

    The severity, the component, and the two fields are the decorator's and the
    proxy's, so the three capture paths write one record rather than three variants
    of one and an operator greps for a single name.

    Contained on purpose. A capture hook may never fail the host agent
    (Requirement 1.7), and a process-wide telemetry surface that could not be
    reached would otherwise make the warning about redaction being off the way an
    adapter raises into its caller. Suppressing here means the worst case is the
    record the Redactor already returned going unwritten, which is the state this
    function exists to improve on rather than a state it can make worse.
    """
    with suppress(Exception):
        from molt.telemetry import Severity, log

        log(
            Severity.WARNING,
            COMPONENT,
            REDACTION_DISABLED_MESSAGE,
            record=record,
            session_id=str(ctx.session_id),
        )
