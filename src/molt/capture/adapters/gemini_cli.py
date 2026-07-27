"""The hook adapter for Gemini CLI, written from that tool's own hooks reference.

This tool passes one JSON object on standard input, reads one JSON object back on
standard output, and reserves standard error for the hook's own logging. Five facts
of that specification decide this module.

**Model traffic has hook events of its own.** The reference documents events that
fire before a request to the model and after a response from it, carrying a stable
request and response shape with the model identifier and a total token count. This
is the one of the five tools that exposes model traffic to a hook, so it is the one
adapter that emits model request and model response Events.

**Tool events carry no call identifier.** The documented input of the two tool
events is the tool name, the tool arguments, the tool response, and optional
metadata for calls that came from a tool server. Nothing identifies the individual
call, so a tool result Event links to its tool call Event through the local
invocation index: the most recent unlinked call of the same Session, preferring one
of the same tool name.

**Only some events accept added context.** The reference gives the
additional-context field to the events that fire before the agent plans, after a
tool returns, and at session start. The event that fires before a tool runs
documents a decision and a rewritten input, not added context. Memory is therefore
queried on the pre-planning event, whose documented envelope is the one a rendered
block is written into, and the capability flag is true because that envelope exists.

**A refusal is a top-level decision.** A decision of `deny` with a reason is
documented for the tool event and for the pre-planning event alike, so one refusal
document serves both and the blocking flag is true.

**No hook event names a spawned subagent.** The reference's event list covers
sessions, prompts, tools, and model traffic, and names no subagent lifecycle event,
so this adapter populates no parent Session. Requirement 1.4 applies where the
payload identifies a spawned subagent, and here no payload does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from molt.capture.adapters import builders
from molt.capture.adapters.invocation_index import InvocationIndex
from molt.capture.protocol import AdapterCapabilities, HookInvocation
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
    "GeminiCliAdapter",
]

# The token the shim is installed with, which is what identifies the Agent_CLI.
TOOL: Final[str] = "gemini_cli"

# Where this adapter's field names come from.
SPECIFICATION: Final[str] = "the published hooks reference for Gemini CLI"

# The vendor's own event names.
SESSION_START: Final[str] = "SessionStart"
SESSION_END: Final[str] = "SessionEnd"
BEFORE_AGENT: Final[str] = "BeforeAgent"
AFTER_AGENT: Final[str] = "AfterAgent"
BEFORE_TOOL: Final[str] = "BeforeTool"
AFTER_TOOL: Final[str] = "AfterTool"
BEFORE_MODEL: Final[str] = "BeforeModel"
AFTER_MODEL: Final[str] = "AfterModel"
BEFORE_TOOL_SELECTION: Final[str] = "BeforeToolSelection"
NOTIFICATION: Final[str] = "Notification"
PRE_COMPRESS: Final[str] = "PreCompress"

# The payload field names this adapter reads, per vendor hook event, for the
# generated per-tool notes. Dotted names are nested fields of the documented
# request and response shapes.
CONSUMED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: ("session_id", "cwd", "hook_event_name", "source"),
    SESSION_END: ("session_id", "cwd", "hook_event_name", "reason"),
    BEFORE_AGENT: ("session_id", "cwd", "hook_event_name", "prompt"),
    AFTER_AGENT: (
        "session_id",
        "cwd",
        "hook_event_name",
        "prompt",
        "prompt_response",
        "stop_hook_active",
    ),
    BEFORE_TOOL: (
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "mcp_context",
        "original_request_name",
    ),
    AFTER_TOOL: (
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "tool_response",
        "tool_response.error",
        "mcp_context",
        "original_request_name",
    ),
    BEFORE_MODEL: (
        "session_id",
        "cwd",
        "hook_event_name",
        "llm_request.model",
        "llm_request.messages",
        "llm_request.config",
    ),
    AFTER_MODEL: (
        "session_id",
        "cwd",
        "hook_event_name",
        "llm_request.model",
        "llm_response.candidates",
        "llm_response.usageMetadata.totalTokenCount",
    ),
    BEFORE_TOOL_SELECTION: (
        "session_id",
        "cwd",
        "hook_event_name",
        "llm_request.model",
        "llm_request.toolConfig",
    ),
    NOTIFICATION: (
        "session_id",
        "cwd",
        "hook_event_name",
        "notification_type",
        "message",
    ),
    PRE_COMPRESS: ("session_id", "cwd", "hook_event_name", "trigger"),
}

# What each vendor hook event maps to, as Event category values.
EMITTED_CATEGORIES: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: (EventCategory.SESSION_START.value,),
    SESSION_END: (EventCategory.SESSION_END.value,),
    BEFORE_AGENT: (EventCategory.USER_PROMPT.value,),
    AFTER_AGENT: (EventCategory.ASSISTANT_RESPONSE.value,),
    BEFORE_TOOL: (EventCategory.TOOL_CALL.value,),
    AFTER_TOOL: (EventCategory.TOOL_RESULT.value,),
    BEFORE_MODEL: (EventCategory.MODEL_REQUEST.value,),
    AFTER_MODEL: (EventCategory.MODEL_RESPONSE.value,),
    BEFORE_TOOL_SELECTION: (EventCategory.DECISION.value,),
    NOTIFICATION: (EventCategory.DECISION.value,),
    PRE_COMPRESS: (EventCategory.DECISION.value,),
}

# The response document's fields, as the reference names them.
_HOOK_SPECIFIC: Final[str] = "hookSpecificOutput"
_HOOK_EVENT_NAME: Final[str] = "hookEventName"
_ADDITIONAL_CONTEXT: Final[str] = "additionalContext"
_DECISION: Final[str] = "decision"
_REASON: Final[str] = "reason"
_DENY: Final[str] = "deny"

# The tool input fields a recall query and a description are read from. These are
# the argument names of this tool's own built-in tools.
_DESCRIBED_INPUTS: Final[tuple[str, ...]] = (
    "command",
    "absolute_path",
    "file_path",
    "path",
    "pattern",
    "query",
    "url",
)

# The nested fields of the documented model request and response shapes.
_REQUEST: Final[str] = "llm_request"
_RESPONSE: Final[str] = "llm_response"
_MODEL: Final[str] = "model"
_MESSAGES: Final[str] = "messages"
_CONFIG: Final[str] = "config"
_TOOL_CONFIG: Final[str] = "toolConfig"
_CANDIDATES: Final[str] = "candidates"
_USAGE: Final[str] = "usageMetadata"
_TOTAL_TOKENS: Final[str] = "totalTokenCount"
_FINISH_REASON: Final[str] = "finishReason"


@dataclass(slots=True)
class GeminiCliAdapter:
    """The five calls, answered from this tool's own reference."""

    tool: str = TOOL
    index: InvocationIndex | None = None

    # -- reading the payload ---------------------------------------------

    def parse(self, raw: bytes) -> HookInvocation:
        """Read one payload of this tool's own shape."""
        document = _document(raw)
        event_name = _text(document.get("hook_event_name"))
        return HookInvocation(
            tool=TOOL,
            event_name=event_name,
            payload=document,
            session_key=_text(document.get("session_id")) or None,
            workspace_path=_text(document.get("cwd")) or None,
            correlation_id=None,
            subagent=None,
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
        if name == SESSION_END:
            return [self._session_end(ctx, payload)]
        if name == BEFORE_AGENT:
            return [self._user_prompt(ctx, payload)]
        if name == AFTER_AGENT:
            return [self._assistant_response(ctx, payload)]
        if name == BEFORE_TOOL:
            return [self._tool_call(ctx, payload)]
        if name == AFTER_TOOL:
            return [self._tool_result(ctx, payload)]
        if name == BEFORE_MODEL:
            return [self._model_request(ctx, payload)]
        if name == AFTER_MODEL:
            return [self._model_response(ctx, payload)]
        return [self._observation(ctx, name, payload)]

    def _session_start(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A session start, carrying how the session began."""
        extra: JsonObject = {
            "hook_event": SESSION_START,
            "source": _text(payload.get("source")),
        }
        return builders.event(
            ctx, EventCategory.SESSION_START, builders.session_payload(ctx, extra)
        )

    def _session_end(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A session end, carrying why the session ended."""
        fields: JsonObject = {"hook_event": SESSION_END, "reason": _text(payload.get("reason"))}
        return builders.event(ctx, EventCategory.SESSION_END, fields)

    def _user_prompt(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The submitted prompt, before the agent begins planning."""
        prompt = _text(payload.get("prompt"))
        return builders.event(
            ctx,
            EventCategory.USER_PROMPT,
            builders.body_fields(prompt),
            text_body=builders.clip(prompt, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _assistant_response(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The turn's generated response, which this event carries as text."""
        response = _text(payload.get("prompt_response"))
        fields: JsonObject = {"stop_hook_active": payload.get("stop_hook_active") is True}
        fields.update(builders.body_fields(response))
        return builders.event(
            ctx,
            EventCategory.ASSISTANT_RESPONSE,
            fields,
            text_body=builders.clip(response, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _tool_call(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A pending tool call, recorded so its result can link to it.

        The tool server metadata is kept where the payload carries it, because a call
        that arrived through a server is a call whose behaviour is that server's rather
        than this tool's, and the two are indistinguishable from the tool name alone.
        """
        tool_name = _text(payload.get("tool_name"))
        fields: JsonObject = {
            "tool_name": tool_name,
            "tool_input": builders.bounded_value(payload.get("tool_input")),
        }
        server = payload.get("mcp_context")
        if server is not None:
            fields["mcp_context"] = builders.bounded_value(server)
        original = _text(payload.get("original_request_name"))
        if original:
            fields["original_request_name"] = original
        described = _describe(payload.get("tool_input"))
        if described:
            fields["description"] = described
        built = builders.event(ctx, EventCategory.TOOL_CALL, fields)
        self._index(ctx).record_call(
            ctx.session_id,
            built.id,
            at=ctx.clock.now().timestamp(),
            correlation_id=None,
            tool_name=tool_name or None,
        )
        return built

    def _tool_result(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A completed tool call's result, linked through the invocation index.

        The tool server metadata and the server-side request name are kept here as well
        as on the call, because a result read on its own should say which server
        answered it without a reader having to walk back to the call Event.
        """
        tool_name = _text(payload.get("tool_name"))
        response = payload.get("tool_response")
        fields: JsonObject = {
            "tool_name": tool_name,
            "tool_response": builders.bounded_value(response),
            "failed": isinstance(response, dict) and response.get("error") is not None,
        }
        server = payload.get("mcp_context")
        if server is not None:
            fields["mcp_context"] = builders.bounded_value(server)
        original = _text(payload.get("original_request_name"))
        if original:
            fields["original_request_name"] = original
        return builders.event(
            ctx,
            EventCategory.TOOL_RESULT,
            fields,
            parent_event_id=self._index(ctx).take_call(
                ctx.session_id,
                at=ctx.clock.now().timestamp(),
                correlation_id=None,
                tool_name=tool_name or None,
            ),
        )

    def _model_request(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A request about to be sent to the model, in this tool's stable shape."""
        request = _mapping(payload.get(_REQUEST))
        messages = request.get(_MESSAGES)
        fields: JsonObject = {
            "model": _text(request.get(_MODEL)),
            "message_count": len(messages) if isinstance(messages, list) else 0,
            "config": builders.bounded_value(request.get(_CONFIG)),
        }
        return builders.event(ctx, EventCategory.MODEL_REQUEST, fields)

    def _model_response(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A response received from the model, with the token count it reported."""
        request = _mapping(payload.get(_REQUEST))
        response = _mapping(payload.get(_RESPONSE))
        candidates = response.get(_CANDIDATES)
        usage = _mapping(response.get(_USAGE))
        fields: JsonObject = {
            "model": _text(request.get(_MODEL)),
            "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
            "finish_reason": _finish_reason(candidates),
        }
        total = usage.get(_TOTAL_TOKENS)
        if isinstance(total, int) and not isinstance(total, bool):
            fields["total_tokens"] = total
        return builders.event(ctx, EventCategory.MODEL_RESPONSE, fields)

    def _observation(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """One Event for a hook event with no more specific category (Requirement 1.2)."""
        fields: JsonObject = {"hook_event": name or "unnamed"}
        for key in ("notification_type", "message", "trigger"):
            value = _text(payload.get(key))
            if value:
                fields[key] = builders.clip(value, builders.PAYLOAD_TEXT_CAP)
        request = _mapping(payload.get(_REQUEST))
        model = _text(request.get(_MODEL))
        if model:
            fields["model"] = model
        tool_config = request.get(_TOOL_CONFIG)
        if tool_config is not None:
            fields["tool_config"] = builders.bounded_value(tool_config)
        return builders.event(ctx, EventCategory.DECISION, fields)

    # -- the vendor's two response channels ------------------------------

    def context_injection(self, results: list[RecallResult]) -> bytes:
        """Render recall results in the documented pre-planning context envelope.

        Memory is queried on the event that fires before the agent plans, and that is
        the event whose documented output carries an additional-context field, so the
        envelope names it. An empty result set writes nothing, which is this tool's
        own no-op: the reference's own example of a hook with nothing to decide.
        """
        if not results:
            return b""
        return builders.json_bytes(
            {
                _HOOK_SPECIFIC: {
                    _HOOK_EVENT_NAME: BEFORE_AGENT,
                    _ADDITIONAL_CONTEXT: builders.recall_block(results),
                }
            }
        )

    def blocking_response(self, reason: str) -> bytes:
        """Refuse the pending action through the documented top-level decision."""
        return builders.json_bytes({_DECISION: _DENY, _REASON: reason})

    def capabilities(self) -> AdapterCapabilities:
        """All three, each from a channel this tool's reference defines."""
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


def _mapping(value: JsonValue) -> JsonObject:
    """A nested payload object, or an empty one when the field is absent."""
    return value if isinstance(value, dict) else {}


def _finish_reason(candidates: JsonValue) -> str:
    """The first candidate's finish reason, which is what a response is read for."""
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0]
    return _text(first.get(_FINISH_REASON)) if isinstance(first, dict) else ""


def _recall_query(event_name: str, payload: JsonObject) -> str | None:
    """The intended action memory is queried with.

    Only the pre-planning event, because it is the only pre-action event of this
    tool whose documented output carries a field that reaches the model's context.
    """
    if event_name != BEFORE_AGENT:
        return None
    return builders.clip(_text(payload.get("prompt")), builders.PAYLOAD_TEXT_CAP) or None


def _describe(tool_input: JsonValue) -> str:
    """Describe a tool input using this tool's own built-in argument names."""
    if isinstance(tool_input, str):
        return builders.clip(tool_input, builders.EXCERPT_LIMIT)
    if not isinstance(tool_input, dict):
        return ""
    for key in _DESCRIBED_INPUTS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return builders.clip(value, builders.EXCERPT_LIMIT)
    return ""


# The adapter the entry point loads for this tool.
ADAPTER: Final[GeminiCliAdapter] = GeminiCliAdapter()
