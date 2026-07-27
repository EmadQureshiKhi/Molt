"""The five per-tool hook adapters: mapping, linkage, capabilities, and envelopes.

Each assertion here is about a promise one vendor's own specification makes and the
adapter keeps. The suite drives the delivered adapters against payloads shaped as
each vendor documents them, with an invocation index in a directory of the test's
own and a manual clock, so no configuration is read, no socket is opened, and no
wall clock is consulted.

Two things are asserted for every one of the five, because they are obligations of
the protocol rather than of any vendor: every payload produces at least one Event
(Requirement 1.2), and an empty recall result set produces the vendor's own no-op
rather than a block of prose about having found nothing.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol
from uuid import uuid4

import pytest

from molt.capture.adapters import claude_code, codex, copilot, cursor, gemini_cli
from molt.capture.adapters.builders import RECALL_HEADING
from molt.capture.adapters.invocation_index import InvocationIndex
from molt.capture.hook import SUPPORTED_TOOLS, load_adapter
from molt.capture.protocol import (
    AdapterCapabilities,
    CaptureContext,
    ClientRef,
    HookAdapter,
    RecallResult,
    derive_session_id,
)
from molt.models.event import Event, EventCategory, JsonObject

MACHINE: Final[str] = "machine-under-test"
WORKSPACE: Final[str] = "/work/acme"
SESSION_KEY: Final[str] = "conversation-1"
AGENT_KEY: Final[str] = "agent-9"
SHAPED_VALUE: Final[str] = "AKIAIOSFODNN7EXAMPLE"
SOURCE_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "src"
DRIVER_PREFIXES: Final[tuple[str, ...]] = ("psycopg", "boto3", "botocore")
REDACT_PACKAGE: Final[str] = "molt.redact"
ADAPTER_MODULES: Final[tuple[str, ...]] = (
    "molt.capture.adapters.claude_code",
    "molt.capture.adapters.cursor",
    "molt.capture.adapters.codex",
    "molt.capture.adapters.gemini_cli",
    "molt.capture.adapters.copilot",
    "molt.capture.adapters.builders",
    "molt.capture.adapters.invocation_index",
)


class ManualClock(Protocol):
    """The manual time source, with the readings this suite takes from it."""

    def now(self) -> datetime:
        """The current wall reading."""

    def monotonic(self) -> float:
        """The current monotonic reading."""

    def advance(self, seconds: float) -> None:
        """Move both readings forward."""


@dataclass(slots=True)
class FailingClock:
    """A clock that fails once, standing in for any fault raised while mapping.

    Injecting the fault at the clock rather than at a payload field is deliberate:
    every field reader in every adapter is defensive, so the reachable failure mode is
    a dependency raising, and this is the dependency each mapper touches first. After
    the one failure the reading succeeds, because the error Event that records the
    failure needs an instant of its own.
    """

    inner: ManualClock
    failures: int = 1

    def now(self) -> datetime:
        """The wall reading, once the injected fault has been spent."""
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("a fault raised while mapping a payload")
        return self.inner.now()

    def monotonic(self) -> float:
        """The monotonic reading, which the fault does not touch."""
        return self.inner.monotonic()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_index(tmp_path: Path) -> InvocationIndex:
    """An invocation index inside the temporary directory."""
    return InvocationIndex(tmp_path / "spool", MACHINE)


def build_context(
    clock: ManualClock,
    tool: str,
    *,
    session_key: str = SESSION_KEY,
) -> CaptureContext:
    """The identity one invocation's Events are built against."""
    return CaptureContext(
        session_id=derive_session_id(tool, session_key),
        client=ClientRef(id=uuid4(), slug="acme", assigned=True),
        machine_id=MACHINE,
        agent_cli=tool,
        clock=clock,
        workspace_path=WORKSPACE,
    )


def payload_bytes(document: JsonObject) -> bytes:
    """One payload as the vendor delivers it: JSON bytes on standard input."""
    return json.dumps(document).encode("utf-8")


def recall_results(count: int) -> list[RecallResult]:
    """A ranked result set, deliberately out of order so ranking is observable."""
    return [
        RecallResult(
            artifact_id=uuid4(),
            distance=0.5 - (0.1 * position),
            outcome="failed" if position % 2 else "succeeded",
            session_id=uuid4(),
            machine_id=MACHINE,
            occurred_at=datetime.fromtimestamp(0.0, tz=UTC),
            excerpt=f"a prior attempt {position}",
        )
        for position in range(count)
    ]


# ---------------------------------------------------------------------------
# The per-tool cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One vendor's payloads, as that vendor's own specification shapes them."""

    tool: str
    module_tool: str
    session_start: JsonObject
    prompt: JsonObject
    tool_call: JsonObject
    tool_result: JsonObject
    subagent: JsonObject | None
    capability: AdapterCapabilities
    injection_marker: str
    blocking_marker: str
    correlated: bool
    recall_on_tool: bool


CLAUDE_CODE: Final[Case] = Case(
    tool="claude_code",
    module_tool=claude_code.TOOL,
    session_start={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "a-model",
    },
    prompt={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "add a retry to the writer",
    },
    tool_call={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "tool_use_id": "call-1",
    },
    tool_result={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "tool_response": {"stdout": "ok"},
        "tool_use_id": "call-1",
        "duration_ms": 12,
    },
    subagent={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "SubagentStart",
        "agent_id": AGENT_KEY,
        "agent_type": "Explore",
    },
    capability=AdapterCapabilities(
        structured_stdout=True, context_injection=True, blocking_decision=True
    ),
    injection_marker="additionalContext",
    blocking_marker="permissionDecision",
    correlated=True,
    recall_on_tool=True,
)

CURSOR: Final[Case] = Case(
    tool="cursor",
    module_tool=cursor.TOOL,
    session_start={
        "conversation_id": SESSION_KEY,
        "session_id": SESSION_KEY,
        "hook_event_name": "sessionStart",
        "workspace_roots": [WORKSPACE],
        "composer_mode": "agent",
        "is_background_agent": False,
    },
    prompt={
        "conversation_id": SESSION_KEY,
        "hook_event_name": "beforeSubmitPrompt",
        "workspace_roots": [WORKSPACE],
        "prompt": "add a retry to the writer",
    },
    tool_call={
        "conversation_id": SESSION_KEY,
        "hook_event_name": "preToolUse",
        "workspace_roots": [WORKSPACE],
        "tool_name": "Shell",
        "tool_input": {"command": "npm install"},
        "tool_use_id": "call-1",
        "cwd": WORKSPACE,
    },
    tool_result={
        "conversation_id": SESSION_KEY,
        "hook_event_name": "postToolUse",
        "workspace_roots": [WORKSPACE],
        "tool_name": "Shell",
        "tool_input": {"command": "npm install"},
        "tool_output": '{"exitCode":0}',
        "tool_use_id": "call-1",
        "duration": 5432,
    },
    subagent={
        "conversation_id": AGENT_KEY,
        "hook_event_name": "subagentStart",
        "workspace_roots": [WORKSPACE],
        "subagent_id": AGENT_KEY,
        "subagent_type": "explore",
        "task": "explore the writer",
        "parent_conversation_id": SESSION_KEY,
        "tool_call_id": "tc-789",
    },
    capability=AdapterCapabilities(
        structured_stdout=True, context_injection=False, blocking_decision=True
    ),
    injection_marker="user_message",
    blocking_marker="permission",
    correlated=True,
    recall_on_tool=True,
)

CODEX: Final[Case] = Case(
    tool="codex",
    module_tool=codex.TOOL,
    session_start={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "a-model",
    },
    prompt={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "add a retry to the writer",
        "turn_id": "turn-1",
    },
    tool_call={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "tool_use_id": "call-1",
        "turn_id": "turn-1",
    },
    tool_result={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "tool_response": {"output": "ok"},
        "tool_use_id": "call-1",
        "turn_id": "turn-1",
    },
    subagent={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "SubagentStart",
        "agent_id": AGENT_KEY,
        "agent_type": "reviewer",
        "turn_id": "turn-1",
    },
    capability=AdapterCapabilities(
        structured_stdout=True, context_injection=True, blocking_decision=True
    ),
    injection_marker="additionalContext",
    blocking_marker="permissionDecision",
    correlated=True,
    recall_on_tool=True,
)

GEMINI_CLI: Final[Case] = Case(
    tool="gemini_cli",
    module_tool=gemini_cli.TOOL,
    session_start={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "SessionStart",
        "source": "startup",
    },
    prompt={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "BeforeAgent",
        "prompt": "add a retry to the writer",
    },
    tool_call={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "BeforeTool",
        "tool_name": "run_shell_command",
        "tool_input": {"command": "npm test"},
    },
    tool_result={
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "AfterTool",
        "tool_name": "run_shell_command",
        "tool_input": {"command": "npm test"},
        "tool_response": {"llmContent": "ok", "returnDisplay": "ok"},
    },
    subagent=None,
    capability=AdapterCapabilities(
        structured_stdout=True, context_injection=True, blocking_decision=True
    ),
    injection_marker="additionalContext",
    blocking_marker="decision",
    correlated=False,
    recall_on_tool=False,
)

COPILOT: Final[Case] = Case(
    tool="copilot",
    module_tool=copilot.TOOL,
    session_start={
        "sessionId": SESSION_KEY,
        "cwd": WORKSPACE,
        "source": "startup",
    },
    prompt={
        "sessionId": SESSION_KEY,
        "cwd": WORKSPACE,
        "prompt": "add a retry to the writer",
    },
    tool_call={
        "sessionId": SESSION_KEY,
        "cwd": WORKSPACE,
        "toolName": "bash",
        "toolArgs": {"command": "npm test"},
    },
    tool_result={
        "sessionId": SESSION_KEY,
        "cwd": WORKSPACE,
        "toolName": "bash",
        "toolArgs": {"command": "npm test"},
        "toolResult": {"resultType": "success", "textResultForLlm": "ok"},
    },
    subagent={
        "sessionId": SESSION_KEY,
        "cwd": WORKSPACE,
        "hook_event_name": "SubagentStop",
        "agentId": AGENT_KEY,
        "agentType": "explore",
        "agentName": "explore",
        "last_assistant_message": "done",
    },
    capability=AdapterCapabilities(
        structured_stdout=True, context_injection=False, blocking_decision=True
    ),
    injection_marker="progress",
    blocking_marker="permissionDecision",
    correlated=False,
    recall_on_tool=True,
)

CASES: Final[tuple[Case, ...]] = (CLAUDE_CODE, CURSOR, CODEX, GEMINI_CLI, COPILOT)
CASE_IDS: Final[tuple[str, ...]] = tuple(case.tool for case in CASES)


def build_adapter(case: Case, index: InvocationIndex) -> HookAdapter:
    """The delivered adapter for one case, with the test's own index injected."""
    if case is CLAUDE_CODE:
        return claude_code.ClaudeCodeAdapter(index=index)
    if case is CURSOR:
        return cursor.CursorAdapter(index=index)
    if case is CODEX:
        return codex.CodexAdapter(index=index)
    if case is GEMINI_CLI:
        return gemini_cli.GeminiCliAdapter(index=index)
    return copilot.CopilotAdapter(index=index)


def events_for(
    case: Case,
    document: JsonObject,
    index: InvocationIndex,
    clock: ManualClock,
    *,
    adapter: HookAdapter | None = None,
) -> tuple[HookAdapter, list[Event]]:
    """Parse one payload and build its Events, returning both for assertions."""
    fired = build_adapter(case, index) if adapter is None else adapter
    invocation = fired.parse(payload_bytes(document))
    context = build_context(clock, case.tool)
    return fired, fired.to_events(invocation, context)


# ---------------------------------------------------------------------------
# Registration: the shim finds all five
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", sorted(SUPPORTED_TOOLS))
def test_every_supported_token_resolves_to_a_delivered_adapter(token: str) -> None:
    """The entry point loads each of the five by name with no change to its registry."""
    adapter = load_adapter(token)

    assert adapter.tool == token
    assert isinstance(adapter.capabilities(), AdapterCapabilities)


def test_the_five_tokens_are_exactly_the_delivered_adapters() -> None:
    """The supported set and the delivered modules name the same five tools."""
    assert {case.module_tool for case in CASES} == set(SUPPORTED_TOOLS)


# ---------------------------------------------------------------------------
# The mapping table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_session_start_carries_the_identity_fields(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The first Event of a Session names the tool, the machine, and the Client."""
    index = build_index(tmp_path)
    _, events = events_for(case, case.session_start, index, time_source)

    assert [event.category for event in events] == [EventCategory.SESSION_START]
    payload = events[0].payload
    assert payload["agent_cli"] == case.tool
    assert payload["machine_id"] == MACHINE
    assert payload["workspace_path"] == WORKSPACE
    assert payload["client_assigned"] is True


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_submitted_prompt_maps_to_a_user_prompt_event(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The prompt travels in the Event's text body, redacted."""
    index = build_index(tmp_path)
    _, events = events_for(case, case.prompt, index, time_source)

    assert [event.category for event in events] == [EventCategory.USER_PROMPT]
    assert events[0].text_body == "add a retry to the writer"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_pre_action_tool_event_maps_to_a_tool_call(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """A pending tool invocation is one tool call Event carrying the tool's name."""
    index = build_index(tmp_path)
    _, events = events_for(case, case.tool_call, index, time_source)

    assert [event.category for event in events] == [EventCategory.TOOL_CALL]
    assert events[0].payload["tool_name"]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_the_prompt_event_asks_memory_about_the_intended_work(
    case: Case,
    tmp_path: Path,
) -> None:
    """Every one of the five admits context on its prompt event, so all five query."""
    fired = build_adapter(case, build_index(tmp_path))

    invocation = fired.parse(payload_bytes(case.prompt))

    assert invocation.recall_query == "add a retry to the writer"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_memory_is_queried_before_a_tool_only_where_the_event_admits_context(
    case: Case,
    tmp_path: Path,
) -> None:
    """Requirement 13.6 is about a hook's documented format, so the query follows it.

    Four of the five document an added-context field on the event that fires before a
    tool runs, and those four query memory there, describing the action from the
    vendor's own input field names. The fifth documents a decision and a rewritten
    input on that event and puts added context on the event that fires before the
    agent plans, so it queries there instead rather than rendering a block into a
    channel that would not reach the model.
    """
    fired = build_adapter(case, build_index(tmp_path))

    query = fired.parse(payload_bytes(case.tool_call)).recall_query

    if case.recall_on_tool:
        assert query is not None
        assert "npm" in query
    else:
        assert query is None


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_tool_result_links_to_its_tool_call_through_the_parent_identifier(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Requirement 7.8: the linkage is a parent Event identifier, not nesting."""
    index = build_index(tmp_path)
    _, call_events = events_for(case, case.tool_call, index, time_source)
    _, result_events = events_for(case, case.tool_result, index, time_source)

    assert [event.category for event in result_events] == [EventCategory.TOOL_RESULT]
    assert result_events[0].parent_event_id == call_events[0].id


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_tool_result_with_no_recorded_call_leaves_the_parent_unset(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """A result whose call was never observed is recorded rather than dropped."""
    index = build_index(tmp_path)
    _, events = events_for(case, case.tool_result, index, time_source)

    assert events[0].parent_event_id is None


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_every_payload_produces_at_least_one_event(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Requirement 1.2, including for an event name this adapter has no category for."""
    index = build_index(tmp_path)
    document = dict(case.session_start)
    document["hook_event_name"] = "AnEventNobodyHasWrittenYet"
    _, events = events_for(case, document, index, time_source)

    assert len(events) >= 1
    assert events[0].category is EventCategory.DECISION


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_failure_while_mapping_becomes_an_error_event(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The mapping table's last row: an adapter-level failure is captured, not lost."""
    fired = build_adapter(case, InvocationIndex(tmp_path / "spool", MACHINE))
    invocation = fired.parse(payload_bytes(case.tool_call))
    context = build_context(time_source, case.tool)
    faulty = CaptureContext(
        session_id=context.session_id,
        client=context.client,
        machine_id=context.machine_id,
        agent_cli=context.agent_cli,
        clock=FailingClock(time_source),
        workspace_path=context.workspace_path,
    )

    events = fired.to_events(invocation, faulty)

    assert [event.category for event in events] == [EventCategory.ERROR]
    assert events[0].payload["exception_type"] == "RuntimeError"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_payload_that_is_not_one_object_is_refused(case: Case, tmp_path: Path) -> None:
    """A payload the specification does not admit raises rather than being guessed at."""
    fired = build_adapter(case, build_index(tmp_path))

    with pytest.raises(ValueError, match="JSON object"):
        fired.parse(b"[]")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_shaped_value_in_a_tool_input_is_redacted(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """A captured value of a recognised shape never reaches an Event payload."""
    document = dict(case.tool_call)
    for key in ("tool_input", "toolArgs"):
        if key in document:
            document[key] = {"command": f"deploy --key {SHAPED_VALUE}"}
    index = build_index(tmp_path)
    _, events = events_for(case, document, index, time_source)

    rendered = json.dumps(events[0].payload)
    assert SHAPED_VALUE not in rendered
    assert events[0].redacted is True


# ---------------------------------------------------------------------------
# Subagent parentage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [case for case in CASES if case.subagent is not None],
    ids=[case.tool for case in CASES if case.subagent is not None],
)
def test_a_spawned_subagent_names_its_parent_session(case: Case, tmp_path: Path) -> None:
    """Requirement 1.4: the child Session is distinct and names the parent's key."""
    assert case.subagent is not None
    fired = build_adapter(case, build_index(tmp_path))

    invocation = fired.parse(payload_bytes(case.subagent))

    assert invocation.subagent is not None
    assert invocation.subagent.parent_session_key == SESSION_KEY
    assert invocation.session_key != SESSION_KEY


def test_a_tool_with_no_subagent_hook_event_names_no_parent(tmp_path: Path) -> None:
    """Where no payload identifies a spawned subagent, no parentage is invented."""
    fired = gemini_cli.GeminiCliAdapter(index=build_index(tmp_path))

    invocation = fired.parse(payload_bytes(GEMINI_CLI.tool_call))

    assert invocation.subagent is None


# ---------------------------------------------------------------------------
# Correlation: the identifier where there is one, recency where there is not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [case for case in CASES if case.correlated],
    ids=[case.tool for case in CASES if case.correlated],
)
def test_the_vendor_correlation_identifier_decides_over_recency(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Two calls in flight: the result links to the one its identifier names."""
    index = build_index(tmp_path)
    _, first = events_for(case, case.tool_call, index, time_source)
    second_call = dict(case.tool_call)
    second_call["tool_use_id"] = "call-2"
    _, second = events_for(case, second_call, index, time_source)

    _, result = events_for(case, case.tool_result, index, time_source)

    assert first[0].id != second[0].id
    assert result[0].parent_event_id == first[0].id


@pytest.mark.parametrize(
    "case",
    [case for case in CASES if not case.correlated],
    ids=[case.tool for case in CASES if not case.correlated],
)
def test_a_specification_with_no_correlation_identifier_falls_back_to_recency(
    case: Case,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The most recent unlinked call of the same Session adopts the result."""
    index = build_index(tmp_path)
    _, first = events_for(case, case.tool_call, index, time_source)
    _, second = events_for(case, case.tool_call, index, time_source)

    _, result = events_for(case, case.tool_result, index, time_source)

    assert result[0].parent_event_id == second[0].id
    assert result[0].parent_event_id != first[0].id


def test_a_shell_result_links_to_the_shell_command_it_followed(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The dedicated shell events carry no identifier, so the index supplies the link."""
    index = build_index(tmp_path)
    fired = cursor.CursorAdapter(index=index)
    context = build_context(time_source, CURSOR.tool)
    command = fired.to_events(
        fired.parse(
            payload_bytes(
                {
                    "conversation_id": SESSION_KEY,
                    "hook_event_name": "beforeShellExecution",
                    "workspace_roots": [WORKSPACE],
                    "command": "rm -rf build",
                    "cwd": WORKSPACE,
                    "sandbox": False,
                }
            )
        ),
        context,
    )
    finished = fired.to_events(
        fired.parse(
            payload_bytes(
                {
                    "conversation_id": SESSION_KEY,
                    "hook_event_name": "afterShellExecution",
                    "workspace_roots": [WORKSPACE],
                    "command": "rm -rf build",
                    "output": "removed",
                    "duration": 12,
                }
            )
        ),
        context,
    )

    assert command[0].category is EventCategory.SHELL_COMMAND
    assert finished[0].parent_event_id == command[0].id


def test_a_file_read_and_a_file_edit_map_to_their_own_categories(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Where a tool exposes file access as its own hook event, the category follows."""
    fired = cursor.CursorAdapter(index=build_index(tmp_path))
    context = build_context(time_source, CURSOR.tool)
    read = fired.to_events(
        fired.parse(
            payload_bytes(
                {
                    "conversation_id": SESSION_KEY,
                    "hook_event_name": "beforeReadFile",
                    "workspace_roots": [WORKSPACE],
                    "file_path": f"{WORKSPACE}/writer.py",
                    "content": "a body of content",
                }
            )
        ),
        context,
    )
    written = fired.to_events(
        fired.parse(
            payload_bytes(
                {
                    "conversation_id": SESSION_KEY,
                    "hook_event_name": "afterFileEdit",
                    "workspace_roots": [WORKSPACE],
                    "file_path": f"{WORKSPACE}/writer.py",
                    "edits": [{"old_string": "a", "new_string": "b"}],
                }
            )
        ),
        context,
    )

    assert read[0].category is EventCategory.FILE_READ
    assert read[0].payload["path"] == f"{WORKSPACE}/writer.py"
    assert read[0].payload["byte_length"] == len(b"a body of content")
    assert written[0].category is EventCategory.FILE_WRITE
    assert written[0].payload["edit_count"] == 1


def test_model_traffic_maps_where_a_tool_exposes_it(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """One of the five documents model events, and that adapter emits both categories."""
    fired = gemini_cli.GeminiCliAdapter(index=build_index(tmp_path))
    context = build_context(time_source, GEMINI_CLI.tool)
    request = fired.to_events(
        fired.parse(
            payload_bytes(
                {
                    "session_id": SESSION_KEY,
                    "cwd": WORKSPACE,
                    "hook_event_name": "BeforeModel",
                    "llm_request": {
                        "model": "a-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "config": {"temperature": 0.2},
                    },
                }
            )
        ),
        context,
    )
    response = fired.to_events(
        fired.parse(
            payload_bytes(
                {
                    "session_id": SESSION_KEY,
                    "cwd": WORKSPACE,
                    "hook_event_name": "AfterModel",
                    "llm_request": {"model": "a-model"},
                    "llm_response": {
                        "candidates": [{"finishReason": "STOP"}],
                        "usageMetadata": {"totalTokenCount": 1234},
                    },
                }
            )
        ),
        context,
    )

    assert request[0].category is EventCategory.MODEL_REQUEST
    assert request[0].payload["model"] == "a-model"
    assert request[0].payload["message_count"] == 1
    assert response[0].category is EventCategory.MODEL_RESPONSE
    assert response[0].payload["total_tokens"] == 1234
    assert response[0].payload["finish_reason"] == "STOP"


def test_the_compatible_payload_spelling_is_read_as_the_same_event(
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """One tool documents two spellings of one payload, and both must map alike."""
    fired = copilot.CopilotAdapter(index=build_index(tmp_path))
    context = build_context(time_source, COPILOT.tool)
    events = fired.to_events(
        fired.parse(
            payload_bytes(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": SESSION_KEY,
                    "cwd": WORKSPACE,
                    "tool_name": "bash",
                    "tool_input": {"command": "npm test"},
                }
            )
        ),
        context,
    )

    assert [event.category for event in events] == [EventCategory.TOOL_CALL]
    assert events[0].payload["tool_name"] == "bash"


# ---------------------------------------------------------------------------
# Capabilities and the two response channels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_the_capability_flags_are_the_ones_the_specification_supports(
    case: Case,
    tmp_path: Path,
) -> None:
    """Each flag is what that vendor's own specification defines a channel for."""
    fired = build_adapter(case, build_index(tmp_path))

    assert fired.capabilities() == case.capability


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_an_empty_result_set_renders_the_vendor_no_op(case: Case, tmp_path: Path) -> None:
    """Nothing recalled means nothing written, not a block of prose saying so."""
    fired = build_adapter(case, build_index(tmp_path))

    assert fired.context_injection([]) == b""


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_recall_results_are_rendered_in_the_vendor_envelope(case: Case, tmp_path: Path) -> None:
    """The wording is shared; the envelope is the tool's own."""
    fired = build_adapter(case, build_index(tmp_path))

    rendered = fired.context_injection(recall_results(3))

    assert case.injection_marker.encode() in rendered
    assert RECALL_HEADING.encode() in rendered
    assert json.loads(rendered.decode("utf-8"))


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_refusal_carries_the_reason_in_the_vendor_channel(case: Case, tmp_path: Path) -> None:
    """The halt path writes the tool's own refusal, naming why."""
    fired = build_adapter(case, build_index(tmp_path))

    rendered = fired.blocking_response("the Session is halted")

    assert case.blocking_marker.encode() in rendered
    assert b"the Session is halted" in rendered
    assert b"deny" in rendered


def test_the_shared_block_ranks_the_nearest_prior_attempt_first(tmp_path: Path) -> None:
    """Requirement 13.6: the ranking is identical across tools, so it is done once."""
    fired = gemini_cli.GeminiCliAdapter(index=build_index(tmp_path))

    rendered = fired.context_injection(recall_results(3)).decode("utf-8")
    block = json.loads(rendered)["hookSpecificOutput"]["additionalContext"]
    distances = [
        float(line.split("distance ")[1].split(",")[0])
        for line in block.splitlines()
        if "distance " in line
    ]

    assert distances == sorted(distances)


# ---------------------------------------------------------------------------
# The invocation index itself
# ---------------------------------------------------------------------------


def test_the_index_file_is_owner_only_and_disappears_once_nothing_is_pending(
    tmp_path: Path,
) -> None:
    """The index follows the spool's discipline: owner-only, bounded, self-clearing."""
    index = build_index(tmp_path)
    session = derive_session_id("claude_code", SESSION_KEY)
    call = uuid4()

    index.record_call(session, call, at=1.0, correlation_id="call-1", tool_name="Bash")
    path = index.path_for(session)
    mode = path.stat().st_mode & 0o777
    taken = index.take_call(session, at=2.0, correlation_id="call-1")

    assert mode == 0o600
    assert taken == call
    assert not path.exists()


def test_an_entry_older_than_the_window_adopts_no_result(tmp_path: Path) -> None:
    """A call whose result never arrived must not link a later, unrelated result."""
    index = InvocationIndex(tmp_path / "spool", MACHINE, ttl_seconds=10.0)
    session = derive_session_id("copilot", SESSION_KEY)
    index.record_call(session, uuid4(), at=1.0, tool_name="bash")

    assert index.take_call(session, at=100.0, tool_name="bash") is None


def test_the_index_holds_no_more_than_its_bound(tmp_path: Path) -> None:
    """A run that never produces results cannot grow the file without limit."""
    index = InvocationIndex(tmp_path / "spool", MACHINE, max_entries=4)
    session = derive_session_id("copilot", SESSION_KEY)
    for _ in range(10):
        index.record_call(session, uuid4(), at=1.0, tool_name="bash")

    assert len(index.pending(session)) == 4


def test_an_unwritable_index_directory_costs_a_link_rather_than_the_hook(
    tmp_path: Path,
) -> None:
    """A filesystem fault leaves the parent unset; nothing raises out of the index."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    index = InvocationIndex(blocked / "spool", MACHINE)
    session = derive_session_id("codex", SESSION_KEY)

    index.record_call(session, uuid4(), at=1.0, tool_name="Bash")

    assert index.take_call(session, at=2.0, tool_name="Bash") is None


def test_two_sessions_do_not_share_pending_calls(tmp_path: Path) -> None:
    """One file per Session is what keeps concurrent runs out of each other's way."""
    index = build_index(tmp_path)
    first = derive_session_id("cursor", "conversation-1")
    second = derive_session_id("cursor", "conversation-2")
    call = uuid4()
    index.record_call(first, call, at=1.0, tool_name="Shell")
    index.record_call(second, uuid4(), at=1.0, tool_name="Shell")

    assert index.take_call(first, at=2.0, tool_name="Shell") == call
    assert index.path_for(second).exists()


# ---------------------------------------------------------------------------
# What an adapter module does not load
# ---------------------------------------------------------------------------


def module_path(name: str) -> Path | None:
    """The source file one dotted name inside the source package resolves to."""
    relative = Path(*name.split("."))
    for candidate in (SOURCE_ROOT / f"{relative}.py", SOURCE_ROOT / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def module_level_imports(path: Path) -> set[str]:
    """The dotted names a module imports at module level, and nothing else."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
    return names


def import_closure(entry: str) -> tuple[frozenset[str], frozenset[str]]:
    """Every module importing `entry` loads, split into own modules and outside ones."""
    own: set[str] = set()
    outside: set[str] = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in own:
            continue
        path = module_path(name)
        if path is None:
            outside.add(name)
            continue
        own.add(name)
        pending.extend(module_level_imports(path))
    return frozenset(own), frozenset(outside)


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_no_adapter_module_imports_a_driver_or_a_cloud_client(module: str) -> None:
    """The latency budget is met by what importing an adapter does not pull in."""
    _, outside = import_closure(module)

    assert sorted(name for name in outside if name.startswith(DRIVER_PREFIXES)) == []


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_no_adapter_module_imports_the_redaction_pattern_table(module: str) -> None:
    """Redaction is imported where it is used, so a module import does not pay for it."""
    own, _ = import_closure(module)

    assert [name for name in sorted(own) if name.startswith(REDACT_PACKAGE)] == []
