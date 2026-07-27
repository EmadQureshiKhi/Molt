"""The hook adapter for Copilot, written from that tool's own hooks reference.

This tool runs a command hook per lifecycle point, hands it one JSON payload, and
reads one JSON decision object back on standard output. Five facts of that
specification decide this module.

**One payload has two documented spellings.** The reference defines a lower camel
case payload for a lower camel case event name, and a compatible payload whose
fields are lower snake case for the same event registered under its upper camel
case name. Which one arrives depends on how the operator registered the hook, so
every field is read under both spellings and the event name is reduced to one form
before it is mapped. That dual reading is documented behaviour rather than a guess.

**Nothing identifies an individual tool call.** The documented tool inputs are the
tool name and its arguments, and the documented tool result is the text the model
will see; no call identifier appears in either. A tool result Event therefore links
to its tool call Event through the local invocation index, taking the most recent
unlinked call of the same Session and preferring one of the same tool name.

**A refusal has a documented channel.** A pre-tool decision of `deny` with a reason
prevents the tool from executing, and the reference states the reason is shown to
the agent, so the blocking flag is true.

**Injected context has no pre-action channel, but a display channel exists.** The
documented output of the pre-tool event is a permission decision, a reason, and
substituted arguments; the additional-context field belongs to the post-tool and
notification events. What the reference does define for a command hook on any event
is a progress line: a single-line object with a message, stripped from the decision
stream and shown on the timeline. Recall results are written there, which reaches
the engineer and decides nothing, and the context-injection flag is false because
the block does not reach the model's context.

**An empty result set writes nothing.** The reference states that empty output falls
through to default behaviour, which is exactly the no-op an absent recall result set
calls for.
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
    "EVENT_ALIASES",
    "SPECIFICATION",
    "TOOL",
    "CopilotAdapter",
]

# The token the shim is installed with, which is what identifies the Agent_CLI.
TOOL: Final[str] = "copilot"

# Where this adapter's field names come from.
SPECIFICATION: Final[str] = "the published hooks reference for GitHub Copilot"

# The vendor's own event names, in the lower camel case spelling this adapter maps
# on.
SESSION_START: Final[str] = "sessionStart"
SESSION_END: Final[str] = "sessionEnd"
USER_PROMPT_SUBMITTED: Final[str] = "userPromptSubmitted"
USER_PROMPT_TRANSFORMED: Final[str] = "userPromptTransformed"
PRE_TOOL_USE: Final[str] = "preToolUse"
POST_TOOL_USE: Final[str] = "postToolUse"
POST_TOOL_USE_FAILURE: Final[str] = "postToolUseFailure"
PERMISSION_REQUEST: Final[str] = "permissionRequest"
AGENT_STOP: Final[str] = "agentStop"
SUBAGENT_START: Final[str] = "subagentStart"
SUBAGENT_STOP: Final[str] = "subagentStop"
ERROR_OCCURRED: Final[str] = "errorOccurred"
PRE_COMPACT: Final[str] = "preCompact"
NOTIFICATION: Final[str] = "notification"

# The upper camel case event names of the compatible format, and the lower camel
# case name each one is the same event as. Registration decides which arrives, so
# both are recognised and only one is mapped on.
EVENT_ALIASES: Final[Mapping[str, str]] = {
    "SessionStart": SESSION_START,
    "SessionEnd": SESSION_END,
    "UserPromptSubmit": USER_PROMPT_SUBMITTED,
    "PreToolUse": PRE_TOOL_USE,
    "PostToolUse": POST_TOOL_USE,
    "PostToolUseFailure": POST_TOOL_USE_FAILURE,
    "PermissionRequest": PERMISSION_REQUEST,
    "Stop": AGENT_STOP,
    "SubagentStart": SUBAGENT_START,
    "SubagentStop": SUBAGENT_STOP,
    "ErrorOccurred": ERROR_OCCURRED,
    "PreCompact": PRE_COMPACT,
    "Notification": NOTIFICATION,
}

# The payload field names this adapter reads, per vendor hook event. Each entry
# names both documented spellings, because either may arrive.
CONSUMED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: (
        "sessionId",
        "session_id",
        "cwd",
        "hook_event_name",
        "source",
        "initialPrompt",
        "initial_prompt",
    ),
    SESSION_END: ("sessionId", "session_id", "cwd", "hook_event_name", "reason"),
    USER_PROMPT_SUBMITTED: ("sessionId", "session_id", "cwd", "hook_event_name", "prompt"),
    USER_PROMPT_TRANSFORMED: (
        "sessionId",
        "cwd",
        "hook_event_name",
        "prompt",
        "transformedPrompt",
    ),
    PRE_TOOL_USE: (
        "sessionId",
        "session_id",
        "cwd",
        "hook_event_name",
        "toolName",
        "tool_name",
        "toolArgs",
        "tool_input",
    ),
    POST_TOOL_USE: (
        "sessionId",
        "session_id",
        "cwd",
        "hook_event_name",
        "toolName",
        "tool_name",
        "toolArgs",
        "tool_input",
        "toolResult.resultType",
        "toolResult.textResultForLlm",
        "tool_result.result_type",
        "tool_result.text_result_for_llm",
    ),
    POST_TOOL_USE_FAILURE: (
        "sessionId",
        "session_id",
        "cwd",
        "hook_event_name",
        "toolName",
        "tool_name",
        "toolArgs",
        "tool_input",
        "error",
    ),
    PERMISSION_REQUEST: ("sessionId", "cwd", "hook_event_name", "toolName", "tool_name"),
    AGENT_STOP: (
        "sessionId",
        "session_id",
        "cwd",
        "hook_event_name",
        "stopReason",
        "stop_reason",
        "stop_hook_active",
    ),
    SUBAGENT_START: (
        "sessionId",
        "cwd",
        "hook_event_name",
        "agentName",
        "agentDisplayName",
        "agentDescription",
    ),
    SUBAGENT_STOP: (
        "sessionId",
        "session_id",
        "cwd",
        "hook_event_name",
        "agentId",
        "agent_id",
        "agentType",
        "agent_type",
        "agentName",
        "agent_name",
        "response",
        "last_assistant_message",
        "stopReason",
        "stop_reason",
    ),
    ERROR_OCCURRED: (
        "sessionId",
        "session_id",
        "cwd",
        "hook_event_name",
        "error.name",
        "error.message",
        "errorContext",
        "error_context",
        "recoverable",
    ),
    PRE_COMPACT: (
        "sessionId",
        "session_id",
        "cwd",
        "hook_event_name",
        "trigger",
        "customInstructions",
        "custom_instructions",
    ),
    NOTIFICATION: (
        "sessionId",
        "cwd",
        "hook_event_name",
        "notification_type",
        "message",
        "title",
    ),
}

# What each vendor hook event maps to, as Event category values.
EMITTED_CATEGORIES: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: (EventCategory.SESSION_START.value,),
    SESSION_END: (EventCategory.SESSION_END.value,),
    USER_PROMPT_SUBMITTED: (EventCategory.USER_PROMPT.value,),
    USER_PROMPT_TRANSFORMED: (EventCategory.DECISION.value,),
    PRE_TOOL_USE: (EventCategory.TOOL_CALL.value,),
    POST_TOOL_USE: (EventCategory.TOOL_RESULT.value,),
    POST_TOOL_USE_FAILURE: (EventCategory.ERROR.value,),
    PERMISSION_REQUEST: (EventCategory.DECISION.value,),
    AGENT_STOP: (EventCategory.DECISION.value,),
    SUBAGENT_START: (EventCategory.SESSION_START.value,),
    SUBAGENT_STOP: (EventCategory.SESSION_END.value,),
    ERROR_OCCURRED: (EventCategory.ERROR.value,),
    PRE_COMPACT: (EventCategory.DECISION.value,),
    NOTIFICATION: (EventCategory.DECISION.value,),
}

# The response document's fields, as the reference names them.
_PERMISSION_DECISION: Final[str] = "permissionDecision"
_PERMISSION_REASON: Final[str] = "permissionDecisionReason"
_DENY: Final[str] = "deny"
_TYPE: Final[str] = "type"
_PROGRESS: Final[str] = "progress"
_MESSAGE: Final[str] = "message"

# The tool argument names a recall query is described from. These are the argument
# names of this tool's own documented tool set.
_DESCRIBED_INPUTS: Final[tuple[str, ...]] = ("command", "path", "filePath", "file_path", "pattern")

# What separates a parent Session key from a subagent identifier.
_CHILD_SEPARATOR: Final[str] = "\x1f"

# The fields that distinguish one documented payload from another, tried in order,
# for the spelling that carries no event name. The order is what makes the
# recognition unambiguous: a subagent payload is examined before a turn payload
# because both name a stop reason, and a tool result before a tool call because both
# name a tool.
_SHAPE_MARKERS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("toolResult", "tool_result"), POST_TOOL_USE),
    (("errorContext", "error_context"), ERROR_OCCURRED),
    (("transformedPrompt", "transformed_prompt"), USER_PROMPT_TRANSFORMED),
    (("notification_type",), NOTIFICATION),
    (("trigger",), PRE_COMPACT),
    (("agentId", "agent_id", "response"), SUBAGENT_STOP),
    (("agentName", "agentDisplayName", "agentDescription"), SUBAGENT_START),
    (("error",), POST_TOOL_USE_FAILURE),
    (("toolName", "tool_name", "toolArgs", "tool_input"), PRE_TOOL_USE),
    (("source",), SESSION_START),
    (("reason",), SESSION_END),
    (("stopReason", "stop_reason"), AGENT_STOP),
    (("prompt",), USER_PROMPT_SUBMITTED),
)


@dataclass(slots=True)
class CopilotAdapter:
    """The five calls, answered from this tool's own reference."""

    tool: str = TOOL
    index: InvocationIndex | None = None

    # -- reading the payload ---------------------------------------------

    def parse(self, raw: bytes) -> HookInvocation:
        """Read one payload of this tool's own shape, in either documented spelling."""
        document = _document(raw)
        event_name = _event_name(document)
        session_key = _either(document, "sessionId", "session_id")
        agent = ""
        if event_name in (SUBAGENT_START, SUBAGENT_STOP):
            agent = _either(document, "agentId", "agent_id") or _either(
                document, "agentName", "agent_name"
            )
        spawn = SubagentSpawn(parent_session_key=session_key) if session_key and agent else None
        return HookInvocation(
            tool=TOOL,
            event_name=event_name,
            payload=document,
            session_key=(
                f"{session_key}{_CHILD_SEPARATOR}{agent}" if spawn is not None else session_key
            )
            or None,
            workspace_path=_text(document.get("cwd")) or None,
            correlation_id=None,
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
        if name == USER_PROMPT_SUBMITTED:
            return [self._user_prompt(ctx, payload)]
        if name == PRE_TOOL_USE:
            return [self._tool_call(ctx, payload)]
        if name == POST_TOOL_USE:
            return [self._tool_result(ctx, payload)]
        if name == POST_TOOL_USE_FAILURE:
            return [self._tool_failure(ctx, payload)]
        if name == ERROR_OCCURRED:
            return [self._error(ctx, payload)]
        return [self._observation(ctx, name, payload)]

    def _session_start(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """A session start, or a spawned subagent's own start."""
        extra: JsonObject = {"hook_event": name, "source": _text(payload.get("source"))}
        for camel, snake in (
            ("agentName", "agent_name"),
            ("agentDisplayName", "agent_display_name"),
            ("agentDescription", "agent_description"),
        ):
            value = _either(payload, camel, snake)
            if value:
                extra[snake] = builders.clip(value, builders.PAYLOAD_TEXT_CAP)
        initial = _either(payload, "initialPrompt", "initial_prompt")
        if initial:
            extra["initial_prompt_digest"] = builders.digest_hex(initial)
        return builders.event(
            ctx, EventCategory.SESSION_START, builders.session_payload(ctx, extra)
        )

    def _session_end(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """A session end, or a subagent's completion with its final response."""
        fields: JsonObject = {
            "hook_event": name,
            "outcome": _text(payload.get("reason"))
            or _either(payload, "stopReason", "stop_reason"),
        }
        for camel, snake in (
            ("agentId", "agent_id"),
            ("agentType", "agent_type"),
            ("agentName", "agent_name"),
        ):
            value = _either(payload, camel, snake)
            if value:
                fields[snake] = value
        response = _either(payload, "response", "last_assistant_message")
        fields.update(builders.body_fields(response))
        return builders.event(
            ctx,
            EventCategory.SESSION_END,
            fields,
            text_body=builders.clip(response, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _user_prompt(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The submitted prompt, redacted."""
        prompt = _text(payload.get("prompt"))
        return builders.event(
            ctx,
            EventCategory.USER_PROMPT,
            builders.body_fields(prompt),
            text_body=builders.clip(prompt, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _tool_call(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A pending tool call, recorded so its result can link to it.

        No call identifier is documented for this event, so the index entry is keyed
        by the tool name alone and the result takes the most recent unlinked call.
        """
        tool_name = _either(payload, "toolName", "tool_name")
        fields: JsonObject = {
            "tool_name": tool_name,
            "tool_input": builders.bounded_value(_arguments(payload)),
        }
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
        """A completed tool call's result, linked through the invocation index."""
        tool_name = _either(payload, "toolName", "tool_name")
        result = _result(payload)
        fields: JsonObject = {
            "tool_name": tool_name,
            "result_type": _either(result, "resultType", "result_type"),
        }
        fields.update(
            builders.content_fields(_either(result, "textResultForLlm", "text_result_for_llm"))
        )
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

    def _tool_failure(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A tool call that failed, recorded as an error linked to the call."""
        tool_name = _either(payload, "toolName", "tool_name")
        return builders.error_event(
            ctx,
            "ToolFailure",
            _text(payload.get("error")),
            parent_event_id=self._index(ctx).take_call(
                ctx.session_id,
                at=ctx.clock.now().timestamp(),
                correlation_id=None,
                tool_name=tool_name or None,
            ),
            extra={"tool_name": tool_name, "hook_event": POST_TOOL_USE_FAILURE},
        )

    def _error(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """An error the session reported, with its type and its redacted message."""
        error = payload.get("error")
        described = error if isinstance(error, dict) else {}
        return builders.error_event(
            ctx,
            _text(described.get("name")) or "Error",
            _text(described.get("message")) or _text(error),
            extra={
                "hook_event": ERROR_OCCURRED,
                "error_context": _either(payload, "errorContext", "error_context"),
                "recoverable": payload.get("recoverable") is True,
            },
        )

    def _observation(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """One Event for a hook event with no more specific category (Requirement 1.2)."""
        fields: JsonObject = {"hook_event": name or "unnamed"}
        for key in ("notification_type", "message", "title", "trigger"):
            value = _text(payload.get(key))
            if value:
                fields[key] = builders.clip(value, builders.PAYLOAD_TEXT_CAP)
        for camel, snake in (
            ("stopReason", "stop_reason"),
            ("toolName", "tool_name"),
            ("customInstructions", "custom_instructions"),
        ):
            value = _either(payload, camel, snake)
            if value:
                fields[snake] = builders.clip(value, builders.PAYLOAD_TEXT_CAP)
        if "stop_hook_active" in payload:
            fields["stop_hook_active"] = payload.get("stop_hook_active") is True
        transformed = _either(payload, "transformedPrompt", "transformed_prompt")
        text = transformed or _text(payload.get("prompt"))
        if transformed:
            fields.update(builders.body_fields(transformed))
        return builders.event(
            ctx,
            EventCategory.DECISION,
            fields,
            text_body=builders.clip(text, builders.PAYLOAD_TEXT_CAP) or None,
        )

    # -- the vendor's two response channels ------------------------------

    def context_injection(self, results: list[RecallResult]) -> bytes:
        """Render recall results in the documented progress line.

        The pre-action event of this tool documents no additional-context field, so
        the block travels on the one channel the reference does define for a command
        hook on any event: a single-line progress object, stripped from the decision
        stream and shown on the timeline. It is advisory by construction, which is
        what the capability flag reports, and the line carries no newline of its own
        because progress recognition is line-oriented.
        """
        if not results:
            return b""
        line = builders.recall_block(results).replace("\n", " | ")
        return builders.json_bytes({_TYPE: _PROGRESS, _MESSAGE: line})

    def blocking_response(self, reason: str) -> bytes:
        """Refuse the pending tool call through the documented permission decision."""
        return builders.json_bytes({_PERMISSION_DECISION: _DENY, _PERMISSION_REASON: reason})

    def capabilities(self) -> AdapterCapabilities:
        """Structured output and a refusal, but no pre-action injection envelope."""
        return AdapterCapabilities(
            structured_stdout=True,
            context_injection=False,
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


def _either(payload: JsonObject, camel: str, snake: str) -> str:
    """One field under either documented spelling, the camel case one first."""
    return _text(payload.get(camel)) or _text(payload.get(snake))


def _event_name(payload: JsonObject) -> str:
    """The event that fired, reduced to the lower camel case spelling.

    The compatible payload names the event in a field of its own. The lower camel
    case payload does not, and the shim's own event argument is applied by the entry
    point only after this call, so the event is recognised here from the fields the
    reference documents for each one. That recognition is an inference from the
    documented payload shapes rather than something the specification states, which
    is why it is the last resort and why an unrecognised shape yields no name at all
    rather than a guessed one.
    """
    named = _text(payload.get("hook_event_name"))
    if named:
        return EVENT_ALIASES.get(named, named)
    for markers, event_name in _SHAPE_MARKERS:
        if any(marker in payload for marker in markers):
            return event_name
    return ""


def _arguments(payload: JsonObject) -> JsonValue:
    """The tool arguments under either documented spelling."""
    arguments = payload.get("toolArgs")
    return payload.get("tool_input") if arguments is None else arguments


def _result(payload: JsonObject) -> JsonObject:
    """The tool result object under either documented spelling."""
    for key in ("toolResult", "tool_result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _recall_query(event_name: str, payload: JsonObject) -> str | None:
    """The intended action memory is queried with, on this tool's pre-action events."""
    if event_name == USER_PROMPT_SUBMITTED:
        return builders.clip(_text(payload.get("prompt")), builders.PAYLOAD_TEXT_CAP) or None
    if event_name != PRE_TOOL_USE:
        return None
    tool_name = _either(payload, "toolName", "tool_name")
    described = _describe(_arguments(payload))
    if not tool_name and not described:
        return None
    return f"{tool_name} {described}".strip()


def _describe(arguments: JsonValue) -> str:
    """Describe tool arguments using this tool's own documented argument names."""
    if isinstance(arguments, str):
        return builders.clip(arguments, builders.EXCERPT_LIMIT)
    if not isinstance(arguments, dict):
        return ""
    for key in _DESCRIBED_INPUTS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return builders.clip(value, builders.EXCERPT_LIMIT)
    return ""


# The adapter the entry point loads for this tool.
ADAPTER: Final[CopilotAdapter] = CopilotAdapter()
