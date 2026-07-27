"""The hook adapter for Codex, written from that tool's own hooks documentation.

This tool runs a command hook per lifecycle event, hands it one JSON object on
standard input, and reads a decision back on standard output. Four facts of that
specification decide this module.

**A subagent's hook carries the parent's session identifier.** The documentation
states that subagent hooks report the parent session identifier in `session_id` and
name the subagent in `agent_id`, so the child Session key is the two together and
the parent Session key is `session_id` on its own (Requirement 1.4).

**Tool events carry a call identifier and a turn identifier.** `tool_use_id` links a
result to its call, and `turn_id` is kept in the payload because it groups the
Events of one turn without being the linkage itself.

**Plain standard output is ignored on most events, so a decision must be JSON.**
The documentation states that plain text is added as developer context only on the
session-start, subagent-start, and prompt-submission events, and ignored elsewhere.
Recall results therefore travel in the documented additional-context object rather
than as text, and the envelope names the event that fired.

**All three capability flags are true.** Standard output is read as JSON; the
additional-context object is documented for the pre-tool and prompt events this
adapter queries memory on; and a pre-tool permission decision of `deny` with a
reason stops the call. A hook with nothing to say exits 0 with no output, which is
what an empty recall result set renders as.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

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
    "CodexAdapter",
]

# The token the shim is installed with, which is what identifies the Agent_CLI.
TOOL: Final[str] = "codex"

# Where this adapter's field names come from.
SPECIFICATION: Final[str] = "the published hooks documentation for Codex"

# The vendor's own event names.
SESSION_START: Final[str] = "SessionStart"
SESSION_END: Final[str] = "SessionEnd"
USER_PROMPT_SUBMIT: Final[str] = "UserPromptSubmit"
PRE_TOOL_USE: Final[str] = "PreToolUse"
POST_TOOL_USE: Final[str] = "PostToolUse"
PERMISSION_REQUEST: Final[str] = "PermissionRequest"
SUBAGENT_START: Final[str] = "SubagentStart"
SUBAGENT_STOP: Final[str] = "SubagentStop"
STOP: Final[str] = "Stop"
PRE_COMPACT: Final[str] = "PreCompact"
POST_COMPACT: Final[str] = "PostCompact"

# The payload field names this adapter reads, per vendor hook event, for the
# generated per-tool notes.
CONSUMED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: ("session_id", "cwd", "hook_event_name", "source", "model", "permission_mode"),
    SESSION_END: ("session_id", "cwd", "hook_event_name", "reason"),
    USER_PROMPT_SUBMIT: ("session_id", "cwd", "hook_event_name", "prompt", "turn_id"),
    PRE_TOOL_USE: (
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "tool_use_id",
        "turn_id",
        "permission_mode",
    ),
    POST_TOOL_USE: (
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "tool_response",
        "tool_use_id",
        "turn_id",
    ),
    PERMISSION_REQUEST: (
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "turn_id",
    ),
    SUBAGENT_START: (
        "session_id",
        "cwd",
        "hook_event_name",
        "agent_id",
        "agent_type",
        "turn_id",
        "permission_mode",
    ),
    SUBAGENT_STOP: (
        "session_id",
        "cwd",
        "hook_event_name",
        "agent_id",
        "agent_type",
        "last_assistant_message",
        "stop_hook_active",
        "turn_id",
    ),
    STOP: (
        "session_id",
        "cwd",
        "hook_event_name",
        "last_assistant_message",
        "stop_hook_active",
        "turn_id",
    ),
    PRE_COMPACT: ("session_id", "cwd", "hook_event_name", "trigger", "turn_id"),
    POST_COMPACT: ("session_id", "cwd", "hook_event_name", "trigger", "turn_id"),
}

# What each vendor hook event maps to, as Event category values.
EMITTED_CATEGORIES: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: (EventCategory.SESSION_START.value,),
    SESSION_END: (EventCategory.SESSION_END.value,),
    USER_PROMPT_SUBMIT: (EventCategory.USER_PROMPT.value,),
    PRE_TOOL_USE: (EventCategory.TOOL_CALL.value,),
    POST_TOOL_USE: (EventCategory.TOOL_RESULT.value,),
    PERMISSION_REQUEST: (EventCategory.DECISION.value,),
    SUBAGENT_START: (EventCategory.SESSION_START.value,),
    SUBAGENT_STOP: (EventCategory.SESSION_END.value,),
    STOP: (EventCategory.ASSISTANT_RESPONSE.value,),
    PRE_COMPACT: (EventCategory.DECISION.value,),
    POST_COMPACT: (EventCategory.DECISION.value,),
}

# The response document's fields, as the documentation names them.
_HOOK_SPECIFIC: Final[str] = "hookSpecificOutput"
_HOOK_EVENT_NAME: Final[str] = "hookEventName"
_ADDITIONAL_CONTEXT: Final[str] = "additionalContext"
_PERMISSION_DECISION: Final[str] = "permissionDecision"
_PERMISSION_REASON: Final[str] = "permissionDecisionReason"
_DENY: Final[str] = "deny"

# The tool input field the documentation names for a shell command and for a patch,
# followed by the description a permission request may carry.
_DESCRIBED_INPUTS: Final[tuple[str, ...]] = ("command", "description", "path", "query")

# What separates a parent Session key from a subagent identifier.
_CHILD_SEPARATOR: Final[str] = "\x1f"


@dataclass(slots=True)
class CodexAdapter:
    """The five calls, answered from this tool's own documentation.

    `injection_event` is the event name a rendered context block declares itself
    for; it is updated by `parse`, because a hook invocation is one process and the
    documented envelope names the event that fired.
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
            session_key=(
                f"{session_key}{_CHILD_SEPARATOR}{agent_id}" if spawn is not None else session_key
            )
            or None,
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
        if name in (SESSION_START, SUBAGENT_START):
            return [self._session_start(ctx, name, payload)]
        if name in (SESSION_END, SUBAGENT_STOP):
            return [self._session_end(ctx, name, payload)]
        if name == USER_PROMPT_SUBMIT:
            return [self._user_prompt(ctx, payload)]
        if name == PRE_TOOL_USE:
            return [self._tool_call(ctx, inv, payload)]
        if name == POST_TOOL_USE:
            return [self._tool_result(ctx, inv, payload)]
        if name == STOP:
            return [self._assistant_response(ctx, payload)]
        return [self._observation(ctx, name, payload)]

    def _session_start(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """A session start, or a spawned subagent's own start."""
        extra: JsonObject = {"hook_event": name}
        for key in ("source", "model", "permission_mode", "agent_id", "agent_type", "turn_id"):
            value = _text(payload.get(key))
            if value:
                extra[key] = value
        return builders.event(
            ctx, EventCategory.SESSION_START, builders.session_payload(ctx, extra)
        )

    def _session_end(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """A session end, or a subagent's completion, with its final message."""
        fields: JsonObject = {
            "hook_event": name,
            "outcome": _text(payload.get("reason")),
            "stop_hook_active": payload.get("stop_hook_active") is True,
        }
        for key in ("agent_id", "agent_type", "turn_id"):
            value = _text(payload.get(key))
            if value:
                fields[key] = value
        message = _text(payload.get("last_assistant_message"))
        return builders.event(
            ctx,
            EventCategory.SESSION_END,
            fields,
            text_body=builders.clip(message, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _user_prompt(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The prompt that is about to be sent, redacted."""
        prompt = _text(payload.get("prompt"))
        fields: JsonObject = {"turn_id": _text(payload.get("turn_id"))}
        fields.update(builders.body_fields(prompt))
        return builders.event(
            ctx,
            EventCategory.USER_PROMPT,
            fields,
            text_body=builders.clip(prompt, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _tool_call(self, ctx: CaptureContext, inv: HookInvocation, payload: JsonObject) -> Event:
        """A pending tool call, recorded so its result can link to it."""
        tool_name = _text(payload.get("tool_name"))
        fields: JsonObject = {
            "tool_name": tool_name,
            "tool_input": builders.bounded_value(payload.get("tool_input")),
            "turn_id": _text(payload.get("turn_id")),
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
            "turn_id": _text(payload.get("turn_id")),
        }
        if inv.correlation_id is not None:
            fields["tool_use_id"] = inv.correlation_id
        return builders.event(
            ctx,
            EventCategory.TOOL_RESULT,
            fields,
            parent_event_id=self._index(ctx).take_call(
                ctx.session_id,
                at=ctx.clock.now().timestamp(),
                correlation_id=inv.correlation_id,
                tool_name=tool_name or None,
            ),
        )

    def _assistant_response(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The turn's final assistant message, which this event carries directly."""
        message = _text(payload.get("last_assistant_message"))
        fields: JsonObject = {
            "stop_hook_active": payload.get("stop_hook_active") is True,
            "turn_id": _text(payload.get("turn_id")),
        }
        fields.update(builders.body_fields(message))
        return builders.event(
            ctx,
            EventCategory.ASSISTANT_RESPONSE,
            fields,
            text_body=builders.clip(message, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _observation(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """One Event for a hook event with no more specific category (Requirement 1.2)."""
        fields: JsonObject = {"hook_event": name or "unnamed"}
        for key in ("trigger", "tool_name", "turn_id"):
            value = _text(payload.get(key))
            if value:
                fields[key] = value
        described = _describe(payload.get("tool_input"))
        if described:
            fields["description"] = described
        return builders.event(ctx, EventCategory.DECISION, fields)

    # -- the vendor's two response channels ------------------------------

    def context_injection(self, results: list[RecallResult]) -> bytes:
        """Render recall results in this tool's documented additional-context object.

        Plain text is ignored on the pre-tool event, so the object is the only channel
        that reaches the model there; an empty result set writes nothing at all, which
        is this tool's own form for a hook with nothing to report.
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
        """Refuse the pending tool call through the documented permission decision."""
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
        """All three, each from a channel this tool's documentation defines."""
        return AdapterCapabilities(
            structured_stdout=True,
            context_injection=True,
            blocking_decision=True,
        )

    # -- linkage ---------------------------------------------------------

    def _index(self, ctx: CaptureContext) -> InvocationIndex:
        """The invocation index, resolved only where a tool event needs it."""
        if self.index is None:
            self.index = InvocationIndex.from_environment(ctx.machine_id)
        return self.index


# ---------------------------------------------------------------------------
# Reading this tool's payload
# ---------------------------------------------------------------------------


def _document(raw: bytes) -> JsonObject:
    """Decode one payload, refusing anything that is not one JSON object."""
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


def _recall_query(event_name: str, payload: JsonObject) -> str | None:
    """The intended action memory is queried with, on the events that admit context."""
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
    """Describe a tool input using this tool's own input field names.

    A shell call and a patch both carry the action in `command`, and an approval
    request may carry a human-readable `description`, so those are read first and
    anything else falls back to the canonical rendering of the arguments.
    """
    if isinstance(tool_input, str):
        return builders.clip(tool_input, builders.EXCERPT_LIMIT)
    if not isinstance(tool_input, dict):
        return ""
    for key in _DESCRIBED_INPUTS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return builders.clip(value, builders.EXCERPT_LIMIT)
    return builders.text_of(tool_input, limit=builders.EXCERPT_LIMIT)


# The adapter the entry point loads for this tool.
ADAPTER: Final[CodexAdapter] = CodexAdapter()
