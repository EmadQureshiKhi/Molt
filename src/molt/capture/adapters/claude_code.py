"""The hook adapter for Claude Code, written from that tool's own hooks reference.

Claude Code delivers one JSON object on standard input per hook event, names the
event in the payload itself, and reads a structured decision back from standard
output. Four facts of that specification decide this module.

**The event name is in the payload as well as in the shim arguments.** The payload
carries `hook_event_name`, so the adapter reads the event from the payload it was
given and the shim's second argument is only a fallback for a payload that omits
it. Nothing here trusts the shim over the vendor.

**A subagent is identified on every event fired inside it.** The reference states
that tool events fire inside a subagent with `agent_id` and `agent_type` added to
the input, and that the payload's `session_id` remains the parent conversation's.
That is exactly the parentage Requirement 1.4 asks for: the child Session key is
the parent key and the agent identifier together, and the parent Session key is the
payload's own `session_id`.

**Tool calls carry their own correlation identifier.** `tool_use_id` appears on the
pre-tool and post-tool events, so a tool result Event links to its tool call Event
by that identifier through the local invocation index, without needing the
recency fallback.

**All three capability flags are true, and each is set from a documented channel.**
Standard output is parsed as JSON when it begins with a brace; `hookSpecificOutput`
carries `additionalContext`, which the reference describes as text placed into the
model's context at the point the hook fired; and a pre-tool decision of `deny` with
a reason blocks the tool call. Where the specification says a hook with no decision
to report exits 0 with no output, that silence is what an empty recall result set
renders as, because emitting an explicit allow would change the tool call's
permission outcome rather than saying nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from molt.capture.adapters import builders
from molt.capture.adapters.invocation_index import InvocationIndex
from molt.capture.protocol import AdapterCapabilities, HookInvocation, SubagentSpawn
from molt.models.event import Event, EventCategory, JsonObject, JsonValue

if TYPE_CHECKING:  # pragma: no cover - imported for the annotations alone
    from collections.abc import Mapping

    from molt.capture.protocol import CaptureContext, RecallResult

__all__ = [
    "ADAPTER",
    "CONSUMED_FIELDS",
    "EMITTED_CATEGORIES",
    "SPECIFICATION",
    "TOOL",
    "ClaudeCodeAdapter",
]

# The token the shim is installed with, which is what identifies the Agent_CLI
# (Requirement 1.3).
TOOL: Final[str] = "claude_code"

# Where this adapter's field names come from. Recorded as a module attribute so the
# generated per-tool notes name the source rather than restating it by hand.
SPECIFICATION: Final[str] = "the published hooks reference for Claude Code"

# The vendor's own event names.
SESSION_START: Final[str] = "SessionStart"
SESSION_END: Final[str] = "SessionEnd"
USER_PROMPT_SUBMIT: Final[str] = "UserPromptSubmit"
PRE_TOOL_USE: Final[str] = "PreToolUse"
POST_TOOL_USE: Final[str] = "PostToolUse"
POST_TOOL_USE_FAILURE: Final[str] = "PostToolUseFailure"
PERMISSION_REQUEST: Final[str] = "PermissionRequest"
STOP: Final[str] = "Stop"
SUBAGENT_START: Final[str] = "SubagentStart"
SUBAGENT_STOP: Final[str] = "SubagentStop"
PRE_COMPACT: Final[str] = "PreCompact"
NOTIFICATION: Final[str] = "Notification"

# The payload field names this adapter reads, per vendor hook event. Machine
# readable on purpose: the per-tool notes are generated from this rather than
# maintained alongside it, so the notes cannot drift from the code.
CONSUMED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: ("session_id", "cwd", "hook_event_name", "source", "model"),
    SESSION_END: ("session_id", "cwd", "hook_event_name", "reason"),
    USER_PROMPT_SUBMIT: ("session_id", "cwd", "hook_event_name", "prompt", "permission_mode"),
    PRE_TOOL_USE: (
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "tool_use_id",
        "permission_mode",
        "agent_id",
        "agent_type",
    ),
    POST_TOOL_USE: (
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "tool_response",
        "tool_use_id",
        "duration_ms",
        "agent_id",
        "agent_type",
    ),
    POST_TOOL_USE_FAILURE: (
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_use_id",
        "error",
        "is_interrupt",
        "duration_ms",
    ),
    PERMISSION_REQUEST: ("session_id", "cwd", "hook_event_name", "tool_name", "tool_input"),
    STOP: ("session_id", "cwd", "hook_event_name", "last_assistant_message", "stop_hook_active"),
    SUBAGENT_START: ("session_id", "cwd", "hook_event_name", "agent_id", "agent_type"),
    SUBAGENT_STOP: (
        "session_id",
        "cwd",
        "hook_event_name",
        "agent_id",
        "agent_type",
        "last_assistant_message",
        "stop_hook_active",
    ),
    PRE_COMPACT: ("session_id", "cwd", "hook_event_name", "trigger", "custom_instructions"),
    NOTIFICATION: ("session_id", "cwd", "hook_event_name", "notification_type", "message", "title"),
}

# What each vendor hook event maps to, as Event category values. This is the
# per-adapter half of the design's mapping table, in the form the notes read.
EMITTED_CATEGORIES: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: (EventCategory.SESSION_START.value,),
    SESSION_END: (EventCategory.SESSION_END.value,),
    USER_PROMPT_SUBMIT: (EventCategory.USER_PROMPT.value,),
    PRE_TOOL_USE: (EventCategory.TOOL_CALL.value,),
    POST_TOOL_USE: (EventCategory.TOOL_RESULT.value,),
    POST_TOOL_USE_FAILURE: (EventCategory.ERROR.value,),
    PERMISSION_REQUEST: (EventCategory.DECISION.value,),
    STOP: (EventCategory.ASSISTANT_RESPONSE.value,),
    SUBAGENT_START: (EventCategory.SESSION_START.value,),
    SUBAGENT_STOP: (EventCategory.SESSION_END.value,),
    PRE_COMPACT: (EventCategory.DECISION.value,),
    NOTIFICATION: (EventCategory.DECISION.value,),
}

# The response document's fields, as the reference names them.
_HOOK_SPECIFIC: Final[str] = "hookSpecificOutput"
_HOOK_EVENT_NAME: Final[str] = "hookEventName"
_ADDITIONAL_CONTEXT: Final[str] = "additionalContext"
_PERMISSION_DECISION: Final[str] = "permissionDecision"
_PERMISSION_REASON: Final[str] = "permissionDecisionReason"
_DENY: Final[str] = "deny"

# The tool input fields a recall query is described from, in the order the
# description prefers them. These are the vendor's own built-in tool input names.
_DESCRIBED_INPUTS: Final[tuple[str, ...]] = (
    "command",
    "file_path",
    "url",
    "pattern",
    "query",
    "prompt",
)

# What separates the parent Session key from the agent identifier in a child
# Session key. A character no vendor identifier carries, so the two parts of the
# key cannot be confused with one another.
_CHILD_SEPARATOR: Final[str] = "\x1f"


@dataclass(slots=True)
class ClaudeCodeAdapter:
    """The five calls, answered from this tool's own specification.

    The index is a constructor argument so a test drives one in a directory of its
    own; left unset, it is resolved from the configured capture directory on the
    first tool event that needs it.

    `injection_event` is the event name a rendered context block declares itself
    for. It is updated by `parse`, because the reference requires the envelope to
    name the event that fired and one hook invocation is one process, so the event
    the process parsed is the event the process is answering.
    """

    tool: str = TOOL
    index: InvocationIndex | None = None
    injection_event: str = PRE_TOOL_USE

    # -- reading the payload ---------------------------------------------

    def parse(self, raw: bytes) -> HookInvocation:
        """Read one payload of this tool's own shape."""
        document = _document(raw)
        event_name = _text(document.get("hook_event_name"))
        session_key = _text(document.get("session_id"))
        agent_id = _text(document.get("agent_id"))
        spawn = SubagentSpawn(parent_session_key=session_key) if session_key and agent_id else None
        if event_name:
            self.injection_event = event_name
        return HookInvocation(
            tool=TOOL,
            event_name=event_name,
            payload=document,
            session_key=_child_key(session_key, agent_id) or None,
            workspace_path=_text(document.get("cwd")) or None,
            correlation_id=_text(document.get("tool_use_id")) or None,
            subagent=spawn,
            recall_query=_recall_query(event_name, document),
        )

    # -- building the Events ---------------------------------------------

    def to_events(self, inv: HookInvocation, ctx: CaptureContext) -> list[Event]:
        """Map one hook event to Events, or record the failure to do so as one.

        The mapping table's last row makes an adapter-level failure an error Event, so
        a payload this adapter could not read still reaches the Ledger carrying the
        exception type and a redacted message, rather than being lost to a diagnostic
        line alone.
        """
        try:
            return self._mapped(inv, ctx)
        except Exception as error:
            return [
                builders.error_event(
                    ctx,
                    type(error).__name__,
                    str(error),
                    extra={"hook_event": inv.event_name or "unnamed"},
                )
            ]

    def _mapped(self, inv: HookInvocation, ctx: CaptureContext) -> list[Event]:
        """Map one hook event to the Events its class carries."""
        payload = inv.payload
        name = inv.event_name
        if name == SESSION_START:
            return [self._session_start(ctx, payload)]
        if name == SUBAGENT_START:
            return [self._subagent_start(ctx, payload)]
        if name == SESSION_END:
            return [self._session_end(ctx, payload)]
        if name == SUBAGENT_STOP:
            return [self._subagent_stop(ctx, payload)]
        if name == USER_PROMPT_SUBMIT:
            return [self._user_prompt(ctx, payload)]
        if name == PRE_TOOL_USE:
            return [self._tool_call(ctx, inv, payload)]
        if name == POST_TOOL_USE:
            return [self._tool_result(ctx, inv, payload)]
        if name == POST_TOOL_USE_FAILURE:
            return [self._tool_failure(ctx, inv, payload)]
        if name == STOP:
            return [self._assistant_response(ctx, payload)]
        return [self._observation(ctx, name, payload)]

    def _session_start(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A session start, carrying how the session began and the active model."""
        extra: JsonObject = {
            "hook_event": SESSION_START,
            "source": _text(payload.get("source")),
        }
        model = _text(payload.get("model"))
        if model:
            extra["model"] = model
        return builders.event(
            ctx, EventCategory.SESSION_START, builders.session_payload(ctx, extra)
        )

    def _subagent_start(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A spawned subagent's own session start, with its type recorded."""
        extra: JsonObject = {
            "hook_event": SUBAGENT_START,
            "agent_id": _text(payload.get("agent_id")),
            "agent_type": _text(payload.get("agent_type")),
        }
        return builders.event(
            ctx, EventCategory.SESSION_START, builders.session_payload(ctx, extra)
        )

    def _session_end(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A session end, carrying the terminal classification the payload named."""
        fields: JsonObject = {
            "hook_event": SESSION_END,
            "reason": _text(payload.get("reason")),
        }
        return builders.event(ctx, EventCategory.SESSION_END, fields)

    def _subagent_stop(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A subagent's session end, with its final message kept as the text body."""
        fields: JsonObject = {
            "hook_event": SUBAGENT_STOP,
            "agent_id": _text(payload.get("agent_id")),
            "agent_type": _text(payload.get("agent_type")),
            "stop_hook_active": payload.get("stop_hook_active") is True,
        }
        message = _text(payload.get("last_assistant_message"))
        return builders.event(
            ctx,
            EventCategory.SESSION_END,
            fields,
            text_body=builders.clip(message, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _user_prompt(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The submitted prompt, redacted, as payload text and as the text body."""
        prompt = _text(payload.get("prompt"))
        fields: JsonObject = {"permission_mode": _text(payload.get("permission_mode"))}
        fields.update(builders.body_fields(prompt))
        return builders.event(
            ctx,
            EventCategory.USER_PROMPT,
            fields,
            text_body=builders.clip(prompt, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _tool_call(self, ctx: CaptureContext, inv: HookInvocation, payload: JsonObject) -> Event:
        """A pending tool call, recorded in the index so its result can link to it."""
        tool_name = _text(payload.get("tool_name"))
        fields: JsonObject = {
            "tool_name": tool_name,
            "tool_input": builders.bounded_value(payload.get("tool_input")),
            "permission_mode": _text(payload.get("permission_mode")),
        }
        if inv.correlation_id is not None:
            fields["tool_use_id"] = inv.correlation_id
        built = builders.event(ctx, EventCategory.TOOL_CALL, fields)
        self._index(ctx).record_call(
            ctx.session_id,
            built.id,
            at=ctx.clock.now().timestamp(),
            correlation_id=inv.correlation_id,
            tool_name=tool_name or None,
        )
        return built

    def _tool_result(self, ctx: CaptureContext, inv: HookInvocation, payload: JsonObject) -> Event:
        """A completed tool call's result, linked to the call it belongs to."""
        tool_name = _text(payload.get("tool_name"))
        fields: JsonObject = {
            "tool_name": tool_name,
            "tool_response": builders.bounded_value(payload.get("tool_response")),
        }
        duration = _number(payload.get("duration_ms"))
        if duration is not None:
            fields["duration_ms"] = duration
        if inv.correlation_id is not None:
            fields["tool_use_id"] = inv.correlation_id
        return builders.event(
            ctx,
            EventCategory.TOOL_RESULT,
            fields,
            parent_event_id=self._parent(ctx, inv, tool_name),
        )

    def _tool_failure(self, ctx: CaptureContext, inv: HookInvocation, payload: JsonObject) -> Event:
        """A failed tool call, recorded as an error linked to the call."""
        tool_name = _text(payload.get("tool_name"))
        extra: JsonObject = {
            "tool_name": tool_name,
            "hook_event": POST_TOOL_USE_FAILURE,
            "is_interrupt": payload.get("is_interrupt") is True,
        }
        duration = _number(payload.get("duration_ms"))
        if duration is not None:
            extra["duration_ms"] = duration
        return builders.error_event(
            ctx,
            "ToolFailure",
            _text(payload.get("error")),
            parent_event_id=self._parent(ctx, inv, tool_name),
            extra=extra,
        )

    def _assistant_response(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The turn's final assistant message, which this event carries directly."""
        message = _text(payload.get("last_assistant_message"))
        fields: JsonObject = {"stop_hook_active": payload.get("stop_hook_active") is True}
        fields.update(builders.body_fields(message))
        return builders.event(
            ctx,
            EventCategory.ASSISTANT_RESPONSE,
            fields,
            text_body=builders.clip(message, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _observation(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """One Event for a hook event with no more specific category.

        Requirement 1.2 obliges every payload to produce at least one Event, so an
        event this adapter recognises but has no category for, and an event the
        specification gained after this adapter was written, both land here rather
        than being dropped.

        The compaction instruction is read here alongside the trigger, because a
        manual compaction carries the operator's own text for what to preserve and
        that text is why the remainder of the session looks as it does. It is bounded
        like any other free-form field, and redacted with the rest of the payload.
        """
        fields: JsonObject = {"hook_event": name or "unnamed"}
        for key in (
            "notification_type",
            "message",
            "title",
            "trigger",
            "custom_instructions",
            "tool_name",
        ):
            value = _text(payload.get(key))
            if value:
                fields[key] = builders.clip(value, builders.PAYLOAD_TEXT_CAP)
        return builders.event(ctx, EventCategory.DECISION, fields)

    # -- the vendor's two response channels ------------------------------

    def context_injection(self, results: list[RecallResult]) -> bytes:
        """Render recall results in this tool's documented injection envelope.

        An empty result set renders as no output at all, which is the reference's own
        form for a hook with no decision to report. Writing an envelope carrying a
        line of prose about having found nothing would spend the model's context to
        say nothing.
        """
        if not results:
            return b""
        return builders.json_bytes(
            {
                _HOOK_SPECIFIC: {
                    _HOOK_EVENT_NAME: self.injection_event,
                    _ADDITIONAL_CONTEXT: builders.recall_block(results),
                }
            }
        )

    def blocking_response(self, reason: str) -> bytes:
        """Refuse the pending tool call through the documented pre-tool decision."""
        return builders.json_bytes(
            {
                _HOOK_SPECIFIC: {
                    _HOOK_EVENT_NAME: PRE_TOOL_USE,
                    _PERMISSION_DECISION: _DENY,
                    _PERMISSION_REASON: reason,
                }
            }
        )

    def capabilities(self) -> AdapterCapabilities:
        """All three, each from a channel this tool's specification defines."""
        return AdapterCapabilities(
            structured_stdout=True,
            context_injection=True,
            blocking_decision=True,
        )

    # -- linkage ---------------------------------------------------------

    def _index(self, ctx: CaptureContext) -> InvocationIndex:
        """The invocation index, resolved once and only where a tool event needs it."""
        if self.index is None:
            self.index = InvocationIndex.from_environment(ctx.machine_id)
        return self.index

    def _parent(self, ctx: CaptureContext, inv: HookInvocation, tool_name: str) -> UUID | None:
        """The tool call Event a result links to (Requirement 7.8)."""
        return self._index(ctx).take_call(
            ctx.session_id,
            at=ctx.clock.now().timestamp(),
            correlation_id=inv.correlation_id,
            tool_name=tool_name or None,
        )


# ---------------------------------------------------------------------------
# Reading this tool's payload
# ---------------------------------------------------------------------------


def _document(raw: bytes) -> JsonObject:
    """Decode one payload, refusing anything that is not one JSON object.

    Deliberately not shared with another adapter: every one of the five reads its
    own vendor's transport, and a helper the five reached into would be the shared
    parsing Requirement 1.9 forbids.
    """
    decoded: object = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("a hook payload for this tool must be one JSON object")
    return {str(key): _value(item) for key, item in decoded.items()}


def _value(item: object) -> JsonValue:
    """One decoded JSON value, narrowed to what an Event payload may hold."""
    if item is None or isinstance(item, (bool, int, float, str)):
        return item
    if isinstance(item, list):
        return [_value(element) for element in item]
    if isinstance(item, dict):
        return {str(key): _value(element) for key, element in item.items()}
    raise ValueError("a hook payload holds a value of no JSON type")


def _text(value: JsonValue) -> str:
    """A payload field as text, or empty when the field is absent or not text."""
    return value if isinstance(value, str) else ""


def _number(value: JsonValue) -> float | None:
    """A payload field as a number, or None when it is absent or not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _child_key(session_key: str, agent_id: str) -> str:
    """The Session key an invocation belongs to, which a subagent extends."""
    if session_key and agent_id:
        return f"{session_key}{_CHILD_SEPARATOR}{agent_id}"
    return session_key


def _recall_query(event_name: str, payload: JsonObject) -> str | None:
    """The intended action memory is queried with, on the events that admit context.

    Both are pre-action events whose documented output carries an additional-context
    field, so a block rendered for either lands in the model's context.
    """
    if event_name == USER_PROMPT_SUBMIT:
        return builders.clip(_text(payload.get("prompt")), builders.PAYLOAD_TEXT_CAP) or None
    if event_name != PRE_TOOL_USE:
        return None
    tool_name = _text(payload.get("tool_name"))
    described = _describe(payload.get("tool_input"))
    if not tool_name and not described:
        return None
    return f"{tool_name} {described}".strip()


def _describe(tool_input: JsonValue) -> str:
    """Describe a tool input using this tool's own built-in input field names."""
    if not isinstance(tool_input, dict):
        return builders.text_of(tool_input, limit=builders.EXCERPT_LIMIT)
    for key in _DESCRIBED_INPUTS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return builders.clip(value, builders.EXCERPT_LIMIT)
    return ""


# The adapter the entry point loads for this tool.
ADAPTER: Final[ClaudeCodeAdapter] = ClaudeCodeAdapter()
