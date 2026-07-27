"""The hook adapter for Cursor, written from that tool's own hooks documentation.

Cursor spawns a hook process per configured event, hands it one JSON object on
standard input, and reads one JSON object back on standard output. Five facts of
that specification decide this module.

**The run is identified by a conversation rather than a session.** Every payload
carries `conversation_id`, stable across the turns of one conversation, and the two
session lifecycle events carry `session_id`, which the documentation states is the
same value. The Session key is therefore the conversation identifier, read from
whichever of the two field names the event carries.

**The workspace arrives as a list of roots.** `workspace_roots` holds the folders
of the workspace, normally one, so the first entry is the workspace the Client is
resolved from and the whole list is kept in the payload of a Session-scoped Event.

**Shell, file, and MCP actions have hook events of their own.** Beyond the generic
tool events, this tool exposes dedicated events for a shell command, a file read,
a file edit, and an MCP call, so those map to the shell command, file read, and
file write categories rather than being flattened into tool calls. None of those
dedicated events carries a call identifier, which is precisely the case the local
invocation index exists for: the result of a shell command links to the shell
command Event by the most recent unlinked call of the same Session.

**A refusal has a documented channel; injected context does not.** A permission
decision of `deny` with a message for the user and a message for the agent is the
documented way to stop a pending action, so the blocking flag is true. The
documented additional-context field belongs to the post-tool and session-start
events, and no pre-action event of this tool defines one, so recall results travel
in the pre-action response's own user-facing message field and the
context-injection flag is false: the block reaches the engineer rather than the
model's context.

**An empty result set writes nothing.** The response fields of a pre-action event
are permission fields, and emitting one to carry an empty block would turn a report
about memory into a permission decision this hook was not asked to make.
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
    "CursorAdapter",
]

# The token the shim is installed with, which is what identifies the Agent_CLI.
TOOL: Final[str] = "cursor"

# Where this adapter's field names come from.
SPECIFICATION: Final[str] = "the published agent hooks documentation for Cursor"

# The vendor's own event names, which this tool spells in lower camel case.
SESSION_START: Final[str] = "sessionStart"
SESSION_END: Final[str] = "sessionEnd"
BEFORE_SUBMIT_PROMPT: Final[str] = "beforeSubmitPrompt"
PRE_TOOL_USE: Final[str] = "preToolUse"
POST_TOOL_USE: Final[str] = "postToolUse"
POST_TOOL_USE_FAILURE: Final[str] = "postToolUseFailure"
BEFORE_SHELL: Final[str] = "beforeShellExecution"
AFTER_SHELL: Final[str] = "afterShellExecution"
BEFORE_MCP: Final[str] = "beforeMCPExecution"
AFTER_MCP: Final[str] = "afterMCPExecution"
BEFORE_READ_FILE: Final[str] = "beforeReadFile"
AFTER_FILE_EDIT: Final[str] = "afterFileEdit"
BEFORE_TAB_FILE_READ: Final[str] = "beforeTabFileRead"
AFTER_TAB_FILE_EDIT: Final[str] = "afterTabFileEdit"
SUBAGENT_START: Final[str] = "subagentStart"
SUBAGENT_STOP: Final[str] = "subagentStop"
AFTER_AGENT_RESPONSE: Final[str] = "afterAgentResponse"
AFTER_AGENT_THOUGHT: Final[str] = "afterAgentThought"
STOP: Final[str] = "stop"
PRE_COMPACT: Final[str] = "preCompact"

# The payload field names this adapter reads, per vendor hook event, for the
# generated per-tool notes. The authenticated user's electronic address is a base
# field of every payload and is deliberately absent: it is personal data that no
# Event needs.
CONSUMED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: (
        "conversation_id",
        "session_id",
        "hook_event_name",
        "workspace_roots",
        "model",
        "cursor_version",
        "is_background_agent",
        "composer_mode",
    ),
    SESSION_END: (
        "conversation_id",
        "session_id",
        "hook_event_name",
        "workspace_roots",
        "reason",
        "duration_ms",
        "final_status",
        "error_message",
        "is_background_agent",
    ),
    BEFORE_SUBMIT_PROMPT: ("conversation_id", "hook_event_name", "workspace_roots", "prompt"),
    PRE_TOOL_USE: (
        "conversation_id",
        "generation_id",
        "hook_event_name",
        "workspace_roots",
        "tool_name",
        "tool_input",
        "tool_use_id",
        "cwd",
        "model",
    ),
    POST_TOOL_USE: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "tool_name",
        "tool_input",
        "tool_output",
        "tool_use_id",
        "duration",
        "cwd",
    ),
    POST_TOOL_USE_FAILURE: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "tool_name",
        "tool_input",
        "tool_use_id",
        "error_message",
        "failure_type",
        "duration",
        "is_interrupt",
    ),
    BEFORE_SHELL: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "command",
        "cwd",
        "sandbox",
    ),
    AFTER_SHELL: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "command",
        "output",
        "duration",
        "sandbox",
    ),
    BEFORE_MCP: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "tool_name",
        "tool_input",
        "url",
        "command",
    ),
    AFTER_MCP: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "tool_name",
        "tool_input",
        "result_json",
        "duration",
    ),
    BEFORE_READ_FILE: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "file_path",
        "content",
    ),
    AFTER_FILE_EDIT: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "file_path",
        "edits",
    ),
    BEFORE_TAB_FILE_READ: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "file_path",
        "content",
    ),
    AFTER_TAB_FILE_EDIT: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "file_path",
        "edits",
    ),
    SUBAGENT_START: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "subagent_id",
        "subagent_type",
        "task",
        "parent_conversation_id",
        "tool_call_id",
        "subagent_model",
        "is_parallel_worker",
    ),
    SUBAGENT_STOP: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "subagent_type",
        "status",
        "task",
        "summary",
        "duration_ms",
        "message_count",
        "tool_call_count",
        "modified_files",
    ),
    AFTER_AGENT_RESPONSE: ("conversation_id", "hook_event_name", "workspace_roots", "text"),
    AFTER_AGENT_THOUGHT: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "text",
        "duration_ms",
    ),
    STOP: ("conversation_id", "hook_event_name", "workspace_roots", "status", "loop_count"),
    PRE_COMPACT: (
        "conversation_id",
        "hook_event_name",
        "workspace_roots",
        "trigger",
        "context_usage_percent",
        "context_tokens",
        "message_count",
    ),
}

# What each vendor hook event maps to, as Event category values.
EMITTED_CATEGORIES: Final[Mapping[str, tuple[str, ...]]] = {
    SESSION_START: (EventCategory.SESSION_START.value,),
    SESSION_END: (EventCategory.SESSION_END.value,),
    BEFORE_SUBMIT_PROMPT: (EventCategory.USER_PROMPT.value,),
    PRE_TOOL_USE: (EventCategory.TOOL_CALL.value,),
    POST_TOOL_USE: (EventCategory.TOOL_RESULT.value,),
    POST_TOOL_USE_FAILURE: (EventCategory.ERROR.value,),
    BEFORE_SHELL: (EventCategory.SHELL_COMMAND.value,),
    AFTER_SHELL: (EventCategory.TOOL_RESULT.value,),
    BEFORE_MCP: (EventCategory.TOOL_CALL.value,),
    AFTER_MCP: (EventCategory.TOOL_RESULT.value,),
    BEFORE_READ_FILE: (EventCategory.FILE_READ.value,),
    AFTER_FILE_EDIT: (EventCategory.FILE_WRITE.value,),
    BEFORE_TAB_FILE_READ: (EventCategory.FILE_READ.value,),
    AFTER_TAB_FILE_EDIT: (EventCategory.FILE_WRITE.value,),
    SUBAGENT_START: (EventCategory.SESSION_START.value,),
    SUBAGENT_STOP: (EventCategory.SESSION_END.value,),
    AFTER_AGENT_RESPONSE: (EventCategory.ASSISTANT_RESPONSE.value,),
    AFTER_AGENT_THOUGHT: (EventCategory.DECISION.value,),
    STOP: (EventCategory.DECISION.value,),
    PRE_COMPACT: (EventCategory.DECISION.value,),
}

# The response document's fields, as the documentation names them.
_PERMISSION: Final[str] = "permission"
_DENY: Final[str] = "deny"
_USER_MESSAGE: Final[str] = "user_message"
_AGENT_MESSAGE: Final[str] = "agent_message"

# The index key the shell events are recorded under. The shell pair carries no
# call identifier of its own, so a name of this adapter's choosing keeps a shell
# result from adopting an unrelated MCP call.
_SHELL_KEY: Final[str] = "shell"

# The tool input fields a recall query is described from.
_DESCRIBED_INPUTS: Final[tuple[str, ...]] = ("command", "file_path", "url", "query", "pattern")

# What separates a parent Session key from a subagent identifier.
_CHILD_SEPARATOR: Final[str] = "\x1f"


@dataclass(slots=True)
class CursorAdapter:
    """The five calls, answered from this tool's own documentation."""

    tool: str = TOOL
    index: InvocationIndex | None = None

    # -- reading the payload ---------------------------------------------

    def parse(self, raw: bytes) -> HookInvocation:
        """Read one payload of this tool's own shape."""
        document = _document(raw)
        event_name = _text(document.get("hook_event_name"))
        conversation = _text(document.get("conversation_id")) or _text(document.get("session_id"))
        subagent_id = _text(document.get("subagent_id"))
        parent = _text(document.get("parent_conversation_id"))
        spawn = (
            SubagentSpawn(
                parent_session_key=parent,
                spawning_event_id=_identifier(document.get("tool_call_id")),
            )
            if parent and subagent_id
            else None
        )
        session_key = conversation
        if spawn is not None:
            session_key = f"{parent}{_CHILD_SEPARATOR}{subagent_id}"
        return HookInvocation(
            tool=TOOL,
            event_name=event_name,
            payload=document,
            session_key=session_key or None,
            workspace_path=_first_root(document.get("workspace_roots"))
            or _text(document.get("cwd"))
            or None,
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
        if name in (SESSION_END, SUBAGENT_STOP):
            return [self._session_end(ctx, name, payload)]
        if name == BEFORE_SUBMIT_PROMPT:
            return [self._user_prompt(ctx, payload)]
        if name in (PRE_TOOL_USE, BEFORE_MCP):
            return [self._tool_call(ctx, inv, payload)]
        if name in (POST_TOOL_USE, AFTER_MCP):
            return [self._tool_result(ctx, inv, name, payload)]
        if name == POST_TOOL_USE_FAILURE:
            return [self._tool_failure(ctx, inv, payload)]
        if name == BEFORE_SHELL:
            return [self._shell_command(ctx, payload)]
        if name == AFTER_SHELL:
            return [self._shell_result(ctx, payload)]
        if name in (BEFORE_READ_FILE, BEFORE_TAB_FILE_READ):
            return [self._file_read(ctx, name, payload)]
        if name in (AFTER_FILE_EDIT, AFTER_TAB_FILE_EDIT):
            return [self._file_write(ctx, name, payload)]
        if name == AFTER_AGENT_RESPONSE:
            return [self._assistant_response(ctx, payload)]
        return [self._observation(ctx, name, payload)]

    def _session_start(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A conversation start, with how the composer opened it.

        The client version is kept because a run's behaviour belongs to the build that
        produced it: a mapping that changed with a release is only explicable from the
        Ledger if the Ledger says which release was running.
        """
        extra: JsonObject = {
            "hook_event": SESSION_START,
            "composer_mode": _text(payload.get("composer_mode")),
            "background_agent": payload.get("is_background_agent") is True,
            "workspace_roots": _roots(payload.get("workspace_roots")),
        }
        model = _text(payload.get("model"))
        if model:
            extra["model"] = model
        client_version = _text(payload.get("cursor_version"))
        if client_version:
            extra["cursor_version"] = client_version
        return builders.event(
            ctx, EventCategory.SESSION_START, builders.session_payload(ctx, extra)
        )

    def _subagent_start(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A spawned subagent's own session start, with the task it was given."""
        extra: JsonObject = {
            "hook_event": SUBAGENT_START,
            "subagent_id": _text(payload.get("subagent_id")),
            "subagent_type": _text(payload.get("subagent_type")),
            "task": builders.clip(_text(payload.get("task")), builders.PAYLOAD_TEXT_CAP),
            "parallel_worker": payload.get("is_parallel_worker") is True,
        }
        model = _text(payload.get("subagent_model"))
        if model:
            extra["subagent_model"] = model
        return builders.event(
            ctx, EventCategory.SESSION_START, builders.session_payload(ctx, extra)
        )

    def _session_end(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """A conversation or subagent end, with its outcome and terminal counters."""
        fields: JsonObject = {
            "hook_event": name,
            "outcome": _text(payload.get("status")) or _text(payload.get("reason")),
        }
        for key in ("final_status", "error_message", "subagent_type"):
            value = _text(payload.get(key))
            if value:
                fields[key] = value
        for key in ("duration_ms", "message_count", "tool_call_count"):
            number = _number(payload.get(key))
            if number is not None:
                fields[key] = number
        modified = _roots(payload.get("modified_files"))
        if modified:
            fields["modified_files"] = modified
        summary = _text(payload.get("summary"))
        return builders.event(
            ctx,
            EventCategory.SESSION_END,
            fields,
            text_body=builders.clip(summary, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _user_prompt(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The submitted prompt, before the request that carries it is made."""
        prompt = _text(payload.get("prompt"))
        fields: JsonObject = builders.body_fields(prompt)
        return builders.event(
            ctx,
            EventCategory.USER_PROMPT,
            fields,
            text_body=builders.clip(prompt, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _tool_call(self, ctx: CaptureContext, inv: HookInvocation, payload: JsonObject) -> Event:
        """A pending tool or MCP call, recorded so its result can link to it.

        The generation identifier is kept where the payload carries one: it names the
        model turn the call was produced by, so the several calls of one generation are
        recognisable as one act of the agent rather than as unrelated neighbours.
        """
        tool_name = _text(payload.get("tool_name"))
        fields: JsonObject = {
            "tool_name": tool_name,
            "tool_input": builders.bounded_value(payload.get("tool_input")),
        }
        for key in ("url", "command"):
            value = _text(payload.get(key))
            if value:
                fields[key] = builders.clip(value, builders.PAYLOAD_TEXT_CAP)
        generation = _text(payload.get("generation_id"))
        if generation:
            fields["generation_id"] = generation
        if inv.correlation_id is not None:
            fields["tool_use_id"] = inv.correlation_id
        built = builders.event(ctx, EventCategory.TOOL_CALL, fields)
        self._record(ctx, built.id, inv.correlation_id, tool_name or None)
        return built

    def _tool_result(
        self,
        ctx: CaptureContext,
        inv: HookInvocation,
        name: str,
        payload: JsonObject,
    ) -> Event:
        """A completed tool or MCP call's result, linked to the call."""
        tool_name = _text(payload.get("tool_name"))
        fields: JsonObject = {"tool_name": tool_name, "hook_event": name}
        result = payload.get("tool_output")
        if result is None:
            result = payload.get("result_json")
        fields["result"] = builders.bounded_value(result)
        duration = _number(payload.get("duration"))
        if duration is not None:
            fields["duration_ms"] = duration
        return builders.event(
            ctx,
            EventCategory.TOOL_RESULT,
            fields,
            parent_event_id=self._take(ctx, inv.correlation_id, tool_name or None),
        )

    def _tool_failure(self, ctx: CaptureContext, inv: HookInvocation, payload: JsonObject) -> Event:
        """A tool call that failed, timed out, or was denied."""
        tool_name = _text(payload.get("tool_name"))
        extra: JsonObject = {
            "tool_name": tool_name,
            "hook_event": POST_TOOL_USE_FAILURE,
            "failure_type": _text(payload.get("failure_type")),
            "is_interrupt": payload.get("is_interrupt") is True,
        }
        duration = _number(payload.get("duration"))
        if duration is not None:
            extra["duration_ms"] = duration
        return builders.error_event(
            ctx,
            "ToolFailure",
            _text(payload.get("error_message")),
            parent_event_id=self._take(ctx, inv.correlation_id, tool_name or None),
            extra=extra,
        )

    def _shell_command(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A pending shell command, which this tool exposes as its own hook event."""
        command = _text(payload.get("command"))
        fields: JsonObject = {
            "command": builders.clip(command, builders.PAYLOAD_TEXT_CAP),
            "sandbox": payload.get("sandbox") is True,
        }
        directory = _text(payload.get("cwd"))
        if directory:
            fields["cwd"] = directory
        built = builders.event(ctx, EventCategory.SHELL_COMMAND, fields)
        self._record(ctx, built.id, None, _SHELL_KEY)
        return built

    def _shell_result(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """A finished shell command's output, linked through the invocation index.

        This event carries no call identifier, so the linkage is the most recent
        unlinked shell command of the same Session, which is the documented gap the
        index fills.
        """
        fields: JsonObject = {"hook_event": AFTER_SHELL}
        fields.update(builders.content_fields(_text(payload.get("output"))))
        command = _text(payload.get("command"))
        if command:
            fields["command"] = builders.clip(command, builders.PAYLOAD_TEXT_CAP)
        duration = _number(payload.get("duration"))
        if duration is not None:
            fields["duration_ms"] = duration
        return builders.event(
            ctx,
            EventCategory.TOOL_RESULT,
            fields,
            parent_event_id=self._take(ctx, None, _SHELL_KEY),
        )

    def _file_read(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """A file the agent is about to read, identified rather than copied whole."""
        fields: JsonObject = {
            "hook_event": name,
            "path": _text(payload.get("file_path")),
        }
        fields.update(builders.content_fields(_text(payload.get("content"))))
        return builders.event(ctx, EventCategory.FILE_READ, fields)

    def _file_write(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """A file the agent has edited, with the edits it applied."""
        edits = payload.get("edits")
        fields: JsonObject = {
            "hook_event": name,
            "path": _text(payload.get("file_path")),
            "edit_count": len(edits) if isinstance(edits, list) else 0,
            "edits": builders.bounded_value(edits),
        }
        return builders.event(ctx, EventCategory.FILE_WRITE, fields)

    def _assistant_response(self, ctx: CaptureContext, payload: JsonObject) -> Event:
        """The assistant message this event delivers once it is complete."""
        text = _text(payload.get("text"))
        return builders.event(
            ctx,
            EventCategory.ASSISTANT_RESPONSE,
            builders.body_fields(text),
            text_body=builders.clip(text, builders.PAYLOAD_TEXT_CAP) or None,
        )

    def _observation(self, ctx: CaptureContext, name: str, payload: JsonObject) -> Event:
        """One Event for a hook event with no more specific category (Requirement 1.2)."""
        fields: JsonObject = {"hook_event": name or "unnamed"}
        for key in ("status", "trigger"):
            value = _text(payload.get(key))
            if value:
                fields[key] = value
        for key in ("loop_count", "duration_ms", "context_usage_percent", "context_tokens"):
            number = _number(payload.get(key))
            if number is not None:
                fields[key] = number
        text = _text(payload.get("text"))
        return builders.event(
            ctx,
            EventCategory.DECISION,
            fields,
            text_body=builders.clip(text, builders.PAYLOAD_TEXT_CAP) or None,
        )

    # -- the vendor's two response channels ------------------------------

    def context_injection(self, results: list[RecallResult]) -> bytes:
        """Render recall results in the only text channel a pre-action event defines.

        The documented additional-context field belongs to events that fire after an
        action, so a block rendered here reaches the engineer through the pre-action
        response's user-facing message rather than the model's context, and the
        capability flag says so. No permission field is written, because reporting
        what memory holds is not a decision about the pending action.
        """
        if not results:
            return b""
        return builders.json_bytes({_USER_MESSAGE: builders.recall_block(results)})

    def blocking_response(self, reason: str) -> bytes:
        """Refuse the pending action through the documented permission decision."""
        return builders.json_bytes(
            {_PERMISSION: _DENY, _USER_MESSAGE: reason, _AGENT_MESSAGE: reason}
        )

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

    def _record(
        self,
        ctx: CaptureContext,
        event_id: UUID,
        correlation_id: str | None,
        tool_name: str | None,
    ) -> None:
        """Record a pending call so the result Event can name it as its parent."""
        self._index(ctx).record_call(
            ctx.session_id,
            event_id,
            at=ctx.clock.now().timestamp(),
            correlation_id=correlation_id,
            tool_name=tool_name,
        )

    def _take(
        self,
        ctx: CaptureContext,
        correlation_id: str | None,
        tool_name: str | None,
    ) -> UUID | None:
        """The call Event a result links to (Requirement 7.8)."""
        return self._index(ctx).take_call(
            ctx.session_id,
            at=ctx.clock.now().timestamp(),
            correlation_id=correlation_id,
            tool_name=tool_name,
        )


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


def _number(value: JsonValue) -> float | None:
    """A payload field as a number, or None when it is absent or not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _identifier(value: JsonValue) -> UUID | None:
    """A payload field as an Event identifier, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _roots(value: JsonValue) -> list[JsonValue]:
    """The text entries of a payload list field."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry]


def _first_root(value: JsonValue) -> str:
    """The workspace the Client is resolved from, which is the first root."""
    roots = _roots(value)
    first = roots[0] if roots else ""
    return first if isinstance(first, str) else ""


def _recall_query(event_name: str, payload: JsonObject) -> str | None:
    """The intended action memory is queried with, on this tool's pre-action events."""
    if event_name == BEFORE_SUBMIT_PROMPT:
        return builders.clip(_text(payload.get("prompt")), builders.PAYLOAD_TEXT_CAP) or None
    if event_name == BEFORE_SHELL:
        return builders.clip(_text(payload.get("command")), builders.EXCERPT_LIMIT) or None
    if event_name not in (PRE_TOOL_USE, BEFORE_MCP):
        return None
    tool_name = _text(payload.get("tool_name"))
    described = _describe(payload.get("tool_input"))
    if not tool_name and not described:
        return None
    return f"{tool_name} {described}".strip()


def _describe(tool_input: JsonValue) -> str:
    """Describe a tool input using this tool's own input field names."""
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
ADAPTER: Final[CursorAdapter] = CursorAdapter()
