"""Property 22: the hook never fails the agent that is waiting on it.

An agent command-line tool fires a hook and waits for it, so a hook process that
exits non-zero, prints where a decision was expected, or floods standard error is
a fault in the agent's session rather than in memory. This property drives the
delivered entry point over payloads that are valid, truncated, wrongly typed,
missing fields, a mebibyte long, not decodable as text, and empty, in a configured
environment and in one holding no setting at all, and asserts the three things the
agent depends on: status 0, no unredacted credential on either stream, and at most
one diagnostic line.

Four decisions shape what is asserted.

**The real entry point is driven, not the injectable seam.** `dispatch` takes an
adapter, a configuration, and a transmitter, and a property that supplied all
three would assert that a stub cannot fail rather than that the shim cannot. So
`main` is called with the argument vector a shim passes, the payload is placed on
standard input as bytes, and the adapter, the configuration, and the transport are
all resolved the way the installed console script resolves them.

**No credential is configured, and that is what keeps the property offline.** With
the bearer absent the recall route is not asked and the Session route is not
called, and with the shared secret absent the Event batch is spooled rather than
sent, so the transport is constructed and never connected. The property therefore
reaches no network and needs no cluster, while still exercising the whole of the
mapping, redaction, spooling, and diagnostic path. It is also why standard output
is asserted empty: a structured decision needs a response envelope, an envelope
needs a reply, and no reply is ever read.

**Every example runs against a home directory and a working directory of its
own.** The capture path writes an invocation index and a spool beside it, and the
unconfigured example resolves both from their built-in defaults, which are stated
relative to the home directory. Pointing the home directory at a temporary tree is
what lets the truly-unconfigured path be exercised without writing anywhere real,
and the working directory is pointed at a tree holding no configuration file so
that file resolution finds nothing.

**Both streams are judged as bytes.** A diagnostic is counted by newline bytes
rather than by decoded lines, because a payload that is not valid text is one of
the inputs and a diagnostic that could not be decoded must not slip past a
text-shaped assertion.

The example budget is 100 with no per-example deadline. One example builds a
temporary tree, runs the entry point, and removes the tree again, and one arm in
seven carries a mebibyte-long field that redaction scans and the spool writes, so
per-example cost varies by more than an order of magnitude. A deadline would fail
the oversized arm for being large rather than for being wrong.

**Validates: Requirements 1.7, 6.6, 4.1**
"""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from shutil import rmtree
from typing import Final, TextIO, cast

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.capture.hook import COMPONENT, EXIT_OK, SUPPORTED_TOOLS, main
from molt.models.event import JsonObject, JsonValue

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

# The one status the entry point ever exits with, and the prefix every diagnostic
# line carries. Both are read from the module under test so neither is restated.
DIAGNOSTIC_PREFIX: Final[bytes] = f"{COMPONENT}: ".encode()
NEWLINE: Final[bytes] = b"\n"

# How long an oversized field is. One mebibyte is the size the plan names, and it
# is reached by repeating one drawn character rather than by drawing a million of
# them, so the generator's own buffer stays small.
MEBIBYTE: Final[int] = 1024 * 1024
FILLER_CHARACTERS: Final[tuple[str, ...]] = ("a", "-", " ")

# The identity fields the documented payload shapes carry.
SESSION_KEY: Final[str] = "conversation-1"
AGENT_KEY: Final[str] = "agent-9"
WORKSPACE: Final[str] = "/work/acme"
MACHINE: Final[str] = "machine-under-test"

# The Collector address the configured examples name. Nothing connects to it,
# because no credential is configured, and the address resolves to nothing in any
# case.
COLLECTOR_URL: Final[str] = "https://collector.invalid/api"

# The credential-shaped value planted in every generated payload. It is assembled
# from two fragments that recognise nothing on their own, which is the device the
# shared strategies use, so this file holds no span of the shape it is about. The
# prefix is one of the resource-type prefixes the access-key-identifier class
# admits and the body is sixteen uppercase alphanumeric characters, which is what
# makes the whole span recognisable; the body itself says plainly that it is not
# a real value.
_SHAPED_PREFIX: Final[str] = "AKIA"
_SHAPED_BODY: Final[str] = "SYNTHETICVALUE00"
SHAPED_VALUE: Final[str] = _SHAPED_PREFIX + _SHAPED_BODY
SHAPED_BYTES: Final[bytes] = SHAPED_VALUE.encode("utf-8")

# The field names the planting walks into, and the sensitive name it adds. The
# first two are the fields the five specifications carry free text and commands
# in, so a planted value travels the same route captured content travels; the
# third is a name the Redactor treats as sensitive whatever its value looks like,
# so both recognition routes are exercised.
PLANTED_TEXT_FIELDS: Final[frozenset[str]] = frozenset({"prompt", "command", "output"})
PLANTED_SENSITIVE_FIELD: Final[str] = "authorization"

# The tokens that name no supported agent tool, including the empty one.
UNSUPPORTED_TOKENS: Final[tuple[str, ...]] = ("not_a_tool", "claude-code", "CLAUDE_CODE", "")

# The vendor event names a shim passes as its second argument, including one no
# adapter has a category for and the empty one.
EVENT_NAMES: Final[tuple[str, ...]] = (
    "PreToolUse",
    "SessionStart",
    "beforeSubmitPrompt",
    "AnEventNobodyHasWrittenYet",
    "",
)

# The byte sequences that are not valid text: a lone continuation byte, two bytes
# that begin a sequence and do not finish it, a surrogate encoding, and a byte the
# encoding never uses at all.
INVALID_SEQUENCES: Final[tuple[bytes, ...]] = (
    b"\x80",
    b"\xc3\x28",
    b"\xed\xa0\x80",
    b"\xff\xfe",
)

# The wrongly typed values a text field is replaced with: a number, a real, a
# list, an object, and the absent value.
WRONG_TYPES: Final[tuple[JsonValue, ...]] = (0, 4.5, ["one", "two"], {"nested": "value"}, None)

# The arms of the payload generator. Each names one of the seven payload shapes
# the plan lists; the eighth arm is the configuration dimension, drawn separately
# because it is a property of the environment rather than of the payload.
ARM_VALID: Final[str] = "valid"
ARM_TRUNCATED: Final[str] = "truncated"
ARM_WRONG_TYPE: Final[str] = "wrong type"
ARM_ABSENT_FIELDS: Final[str] = "absent fields"
ARM_OVERSIZED: Final[str] = "oversized field"
ARM_NOT_TEXT: Final[str] = "not valid text"
ARM_EMPTY: Final[str] = "empty input"

ARMS: Final[tuple[str, ...]] = (
    ARM_VALID,
    ARM_TRUNCATED,
    ARM_WRONG_TYPE,
    ARM_ABSENT_FIELDS,
    ARM_OVERSIZED,
    ARM_NOT_TEXT,
    ARM_EMPTY,
)

# How often an example names a token outside the supported set, and how many
# arguments a shim passes. Both are drawn from a weighted pool rather than from a
# uniform choice: the unsupported token and the argument-free invocation are
# single stated paths that need reaching rather than half the budget, while the
# seven payload arms are all worth the same share.
UNSUPPORTED_SHARE: Final[tuple[bool, ...]] = (True, False, False, False, False)
ARGUMENT_COUNTS: Final[tuple[int, ...]] = (2, 2, 2, 2, 1, 0)


# ---------------------------------------------------------------------------
# The documented payload shapes, one set per delivered adapter
#
# These are restated here rather than imported, because a test module is not an
# importable surface. Each set names the fields that tool's own specification
# names, spelled as that specification spells them, so the valid arm really is
# valid for the adapter the token loads.
# ---------------------------------------------------------------------------

CLAUDE_CODE_DOCUMENTS: Final[tuple[tuple[str, JsonObject], ...]] = (
    (
        "session start",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "model": "a-model",
        },
    ),
    (
        "prompt",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "add a retry to the writer",
        },
    ),
    (
        "tool call",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "tool_use_id": "call-1",
        },
    ),
    (
        "tool result",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "tool_response": {"stdout": "ok"},
            "tool_use_id": "call-1",
            "duration_ms": 12,
        },
    ),
    (
        "subagent",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "SubagentStart",
            "agent_id": AGENT_KEY,
            "agent_type": "Explore",
        },
    ),
)

CURSOR_DOCUMENTS: Final[tuple[tuple[str, JsonObject], ...]] = (
    (
        "session start",
        {
            "conversation_id": SESSION_KEY,
            "session_id": SESSION_KEY,
            "hook_event_name": "sessionStart",
            "workspace_roots": [WORKSPACE],
            "composer_mode": "agent",
            "is_background_agent": False,
        },
    ),
    (
        "prompt",
        {
            "conversation_id": SESSION_KEY,
            "hook_event_name": "beforeSubmitPrompt",
            "workspace_roots": [WORKSPACE],
            "prompt": "add a retry to the writer",
        },
    ),
    (
        "tool call",
        {
            "conversation_id": SESSION_KEY,
            "hook_event_name": "preToolUse",
            "workspace_roots": [WORKSPACE],
            "tool_name": "Shell",
            "tool_input": {"command": "npm install"},
            "tool_use_id": "call-1",
            "cwd": WORKSPACE,
        },
    ),
    (
        "shell command",
        {
            "conversation_id": SESSION_KEY,
            "hook_event_name": "beforeShellExecution",
            "workspace_roots": [WORKSPACE],
            "command": "rm -rf build",
            "cwd": WORKSPACE,
            "sandbox": False,
        },
    ),
    (
        "subagent",
        {
            "conversation_id": AGENT_KEY,
            "hook_event_name": "subagentStart",
            "workspace_roots": [WORKSPACE],
            "subagent_id": AGENT_KEY,
            "subagent_type": "explore",
            "task": "explore the writer",
            "parent_conversation_id": SESSION_KEY,
            "tool_call_id": "tc-789",
        },
    ),
)

CODEX_DOCUMENTS: Final[tuple[tuple[str, JsonObject], ...]] = (
    (
        "session start",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "model": "a-model",
        },
    ),
    (
        "prompt",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "add a retry to the writer",
            "turn_id": "turn-1",
        },
    ),
    (
        "tool call",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "tool_use_id": "call-1",
            "turn_id": "turn-1",
        },
    ),
    (
        "tool result",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "tool_response": {"output": "ok"},
            "tool_use_id": "call-1",
            "turn_id": "turn-1",
        },
    ),
)

GEMINI_CLI_DOCUMENTS: Final[tuple[tuple[str, JsonObject], ...]] = (
    (
        "session start",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "SessionStart",
            "source": "startup",
        },
    ),
    (
        "prompt",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "BeforeAgent",
            "prompt": "add a retry to the writer",
        },
    ),
    (
        "tool call",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "npm test"},
        },
    ),
    (
        "model request",
        {
            "session_id": SESSION_KEY,
            "cwd": WORKSPACE,
            "hook_event_name": "BeforeModel",
            "llm_request": {
                "model": "a-model",
                "messages": [{"role": "user", "content": "hello"}],
                "config": {"temperature": 0.2},
            },
        },
    ),
)

COPILOT_DOCUMENTS: Final[tuple[tuple[str, JsonObject], ...]] = (
    (
        "session start",
        {
            "sessionId": SESSION_KEY,
            "cwd": WORKSPACE,
            "source": "startup",
        },
    ),
    (
        "prompt",
        {
            "sessionId": SESSION_KEY,
            "cwd": WORKSPACE,
            "prompt": "add a retry to the writer",
        },
    ),
    (
        "tool call",
        {
            "sessionId": SESSION_KEY,
            "cwd": WORKSPACE,
            "toolName": "bash",
            "toolArgs": {"command": "npm test"},
        },
    ),
    (
        "tool result",
        {
            "sessionId": SESSION_KEY,
            "cwd": WORKSPACE,
            "toolName": "bash",
            "toolArgs": {"command": "npm test"},
            "toolResult": {"resultType": "success", "textResultForLlm": "ok"},
        },
    ),
)

VENDOR_DOCUMENTS: Final[dict[str, tuple[tuple[str, JsonObject], ...]]] = {
    "claude_code": CLAUDE_CODE_DOCUMENTS,
    "cursor": CURSOR_DOCUMENTS,
    "codex": CODEX_DOCUMENTS,
    "gemini_cli": GEMINI_CLI_DOCUMENTS,
    "copilot": COPILOT_DOCUMENTS,
}


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookInput:
    """One whole invocation: the shim's arguments, the payload, and the environment.

    Attributes:
        arguments: What the shim passes after the program name, which is a token
            and a vendor event name, a token alone, or nothing at all.
        payload: The bytes standard input carries, which may be no valid document
            and no valid text.
        vendor: Which adapter's documented shapes the payload was built from.
        arm: Which of the seven payload shapes was drawn, for the coverage record.
        unconfigured: Whether the environment holds no setting of the surface, so
            that the entry point resolves everything from its defaults and fails
            on the one required key that has none.
    """

    arguments: tuple[str, ...]
    payload: bytes
    vendor: str
    arm: str
    unconfigured: bool


# ---------------------------------------------------------------------------
# Planting the credential-shaped value
# ---------------------------------------------------------------------------


def _plant(value: JsonValue) -> JsonValue:
    """Return a copy of a payload carrying the shaped value where content travels.

    The free-text and command fields are extended rather than replaced, so the
    document remains the shape its specification describes and the planted value
    arrives the way a captured credential really arrives: inside a longer string.
    """
    if isinstance(value, dict):
        planted: JsonObject = {name: _plant(item) for name, item in value.items()}
        planted[PLANTED_SENSITIVE_FIELD] = SHAPED_VALUE
        return planted
    if isinstance(value, list):
        return [_plant(item) for item in value]
    return value


def _planted_document(document: JsonObject) -> JsonObject:
    """One documented payload with the shaped value planted throughout."""
    seeded: JsonObject = {}
    for name, item in document.items():
        if name in PLANTED_TEXT_FIELDS and isinstance(item, str):
            seeded[name] = f"{item} --key {SHAPED_VALUE}"
        else:
            seeded[name] = _plant(item)
    seeded[PLANTED_SENSITIVE_FIELD] = SHAPED_VALUE
    return seeded


def _document_bytes(document: JsonObject) -> bytes:
    """One payload as a vendor delivers it: JSON bytes on standard input."""
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _text_fields(document: JsonObject) -> tuple[str, ...]:
    """The field names of a document whose values are text."""
    return tuple(name for name, item in document.items() if isinstance(item, str))


def _filler(character: str) -> str:
    """A field one mebibyte long, built by repetition rather than by drawing."""
    return character * MEBIBYTE


# ---------------------------------------------------------------------------
# The seven payload arms
# ---------------------------------------------------------------------------


def _valid_payloads(document: JsonObject) -> st.SearchStrategy[bytes]:
    """Arm one: the document the vendor's own specification describes."""
    return st.just(_document_bytes(_planted_document(document)))


def _truncated_payloads(document: JsonObject) -> st.SearchStrategy[bytes]:
    """Arm two: a valid document cut short at a drawn offset.

    The offset stops one byte before the end so every drawn payload really is
    truncated, and it reaches zero so the cut-to-nothing case is drawn as well.
    """
    blob = _document_bytes(_planted_document(document))
    return st.integers(min_value=0, max_value=len(blob) - 1).map(lambda cut: blob[:cut])


def _wrongly_typed_payloads(document: JsonObject) -> st.SearchStrategy[bytes]:
    """Arm three: a field the specification declares as text arriving as something else.

    Both levels are drawn: one text field replaced by a number, a real, a list, an
    object, or the absent value, and the whole payload replaced by a value that is
    no object at all, which is the shape every adapter refuses by name.
    """
    seeded = _planted_document(document)

    def replaced(name: str, value: JsonValue) -> bytes:
        mutated = dict(seeded)
        mutated[name] = value
        return _document_bytes(mutated)

    per_field = st.builds(
        replaced,
        st.sampled_from(_text_fields(seeded)),
        st.sampled_from(WRONG_TYPES),
    )
    whole = st.sampled_from(
        (
            b"[]",
            b"null",
            b"3",
            b'"a payload that is one string"',
            f'["{SHAPED_VALUE}"]'.encode(),
        )
    )
    return st.one_of(per_field, whole)


def _payloads_missing_fields(document: JsonObject) -> st.SearchStrategy[bytes]:
    """Arm four: a valid document with a drawn set of its fields absent.

    The drawn set may be every field, so the empty object is reached, and it may
    be none, so a document that is complete is drawn on this arm too.
    """
    seeded = _planted_document(document)
    names = tuple(seeded)

    def without(dropped: frozenset[str]) -> bytes:
        return _document_bytes({name: item for name, item in seeded.items() if name not in dropped})

    return st.frozensets(st.sampled_from(names), max_size=len(names)).map(without)


def _oversized_payloads(document: JsonObject) -> st.SearchStrategy[bytes]:
    """Arm five: a valid document one of whose text fields is a mebibyte long.

    The shaped value is appended to the filler rather than dropped, so the arm
    asserts about a recognised span inside an oversized field rather than about
    length alone.
    """
    seeded = _planted_document(document)

    def enlarged(name: str, character: str) -> bytes:
        mutated = dict(seeded)
        mutated[name] = f"{_filler(character)} {SHAPED_VALUE}"
        return _document_bytes(mutated)

    return st.builds(
        enlarged,
        st.sampled_from(_text_fields(seeded)),
        st.sampled_from(FILLER_CHARACTERS),
    )


def _undecodable_payloads(document: JsonObject) -> st.SearchStrategy[bytes]:
    """Arm six: bytes that are not valid text, inside a document and standing alone."""
    blob = _document_bytes(_planted_document(document))

    def spliced(sequence: bytes, offset: int) -> bytes:
        return blob[:offset] + sequence + blob[offset:]

    inside = st.builds(
        spliced,
        st.sampled_from(INVALID_SEQUENCES),
        st.integers(min_value=0, max_value=len(blob)),
    )
    alone = st.builds(
        lambda sequence, tail: sequence + SHAPED_BYTES + tail,
        st.sampled_from(INVALID_SEQUENCES),
        st.sampled_from(INVALID_SEQUENCES),
    )
    return st.one_of(inside, alone)


def _payloads_of(arm: str, document: JsonObject) -> st.SearchStrategy[bytes]:
    """The payload strategy for one arm of the generator."""
    if arm == ARM_VALID:
        return _valid_payloads(document)
    if arm == ARM_TRUNCATED:
        return _truncated_payloads(document)
    if arm == ARM_WRONG_TYPE:
        return _wrongly_typed_payloads(document)
    if arm == ARM_ABSENT_FIELDS:
        return _payloads_missing_fields(document)
    if arm == ARM_OVERSIZED:
        return _oversized_payloads(document)
    if arm == ARM_NOT_TEXT:
        return _undecodable_payloads(document)
    if arm == ARM_EMPTY:
        return st.just(b"")
    raise AssertionError(f"no payload builder covers the arm {arm}")


@st.composite
def hook_inputs(draw: st.DrawFn) -> HookInput:
    """Draw one whole invocation of the hook shim.

    Every dimension the property is quantified over is drawn here: which adapter's
    payload shapes are used, whether the token names a supported tool at all, how
    many arguments the shim passed, which of the seven payload arms produced the
    bytes, and whether the environment holds any setting.
    """
    vendor = draw(st.sampled_from(sorted(VENDOR_DOCUMENTS)))
    _, document = draw(st.sampled_from(VENDOR_DOCUMENTS[vendor]))
    token = (
        draw(st.sampled_from(UNSUPPORTED_TOKENS))
        if draw(st.sampled_from(UNSUPPORTED_SHARE))
        else vendor
    )
    count = draw(st.sampled_from(ARGUMENT_COUNTS))
    arguments: tuple[str, ...] = ()
    if count == 1:
        arguments = (token,)
    elif count == 2:
        arguments = (token, draw(st.sampled_from(EVENT_NAMES)))
    arm = draw(st.sampled_from(ARMS))
    return HookInput(
        arguments=arguments,
        payload=draw(_payloads_of(arm, document)),
        vendor=vendor,
        arm=arm,
        unconfigured=draw(st.booleans()),
    )


# ---------------------------------------------------------------------------
# The harness: one invocation, judged as bytes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ByteStream:
    """A stream double collecting bytes, whichever channel a caller writes through.

    The entry point writes a vendor decision to the byte buffer of standard output
    and writes its diagnostic as text to standard error, so both calls are answered
    here and both land in one buffer. Text is encoded rather than kept as text
    because the assertions count newline bytes.
    """

    buffer: io.BytesIO = field(default_factory=io.BytesIO)

    def write(self, text: str) -> int:
        """Encode and collect a text write, reporting the character count."""
        self.buffer.write(text.encode("utf-8", errors="surrogateescape"))
        return len(text)

    def flush(self) -> None:
        """Answer a flush, which a buffer in memory needs nothing for."""
        return

    @property
    def written(self) -> bytes:
        """Everything written to this stream so far."""
        return self.buffer.getvalue()


@dataclass(slots=True)
class ByteStandardInput:
    """A standard input carrying bytes, so a payload is never decoded on the way in."""

    buffer: io.BytesIO


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one invocation exited with and wrote."""

    status: int
    out: bytes
    err: bytes

    @property
    def lines(self) -> int:
        """How many diagnostic lines standard error received."""
        return self.err.count(NEWLINE)


def _configured_environment(root: Path) -> dict[str, str]:
    """The settings a configured example runs with.

    Neither credential is set, and that is the whole reason this property reaches
    no network: with no shared secret the batch is spooled instead of sent, and
    with no bearer the recall and Session routes are not called, so the transport
    is built and never connected.
    """
    return {
        "MOLT_COLLECTOR_URL": COLLECTOR_URL,
        "MOLT_SPOOL_DIR": str(root / "spool"),
        "MOLT_MACHINE_ID": MACHINE,
        "MOLT_HTTP_TIMEOUT_SECONDS": "5",
        "MOLT_HTTP_RETRIES": "3",
        "MOLT_HOOK_SOFT_DEADLINE_MS": "1200",
    }


@contextmanager
def _environment(case: HookInput, root: Path) -> Iterator[None]:
    """Run one example against an environment and a working directory of its own.

    Every key of the surface is removed first, so the unconfigured arm really does
    resolve from defaults alone. The home directory is pointed inside the example's
    tree because the spool and the invocation index default to a location stated
    relative to it, and the working directory is pointed there too because a
    configuration file is looked for in the working directory and this tree holds
    none.
    """
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    previous_environment = dict(os.environ)
    previous_directory = Path.cwd()
    for name in [name for name in os.environ if name.startswith("MOLT_")]:
        del os.environ[name]
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    if not case.unconfigured:
        os.environ.update(_configured_environment(root))
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous_directory)
        os.environ.clear()
        os.environ.update(previous_environment)


@contextmanager
def _standard_input(payload: bytes) -> Iterator[None]:
    """Place a byte payload on standard input for the duration of one invocation."""
    previous = sys.stdin
    sys.stdin = cast("TextIO", ByteStandardInput(io.BytesIO(payload)))
    try:
        yield
    finally:
        sys.stdin = previous


def _invoke(case: HookInput, root: Path) -> Outcome:
    """Run the entry point once and collect its status and both streams.

    The streams are collected by redirection rather than through the capture
    fixture, because that fixture is function scoped and a property drives many
    examples inside one function call.
    """
    out, err = ByteStream(), ByteStream()
    with (
        _environment(case, root),
        _standard_input(case.payload),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        status = main(list(case.arguments))
    return Outcome(status=status, out=out.written, err=err.written)


def _size_band(payload: bytes) -> str:
    """Which part of the size range a payload sits in, for the coverage record."""
    if not payload:
        return "empty"
    if len(payload) < 1024:
        return "under a kibibyte"
    if len(payload) < MEBIBYTE:
        return "under a mebibyte"
    return "a mebibyte or more"


@pytest.fixture(scope="module")
def hook_area(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A directory every example builds its own tree beneath.

    Module scope is deliberate: a function-scoped temporary directory would be
    shared across the examples of one property rather than created per example,
    and building a fresh subdirectory here is both correct and cheaper.

    The area is removed as a whole afterwards as well as per example, because a
    delivered adapter binds its invocation index on first use and keeps it for the
    life of the process: one example's index therefore recreates a directory a
    later example already removed. That costs nothing here, since a hook runs as a
    fresh process in the field, but it does mean the per-example removal is not the
    last word on what the tree holds.
    """
    area = tmp_path_factory.mktemp("molt_p22_capture")
    yield area
    rmtree(area, ignore_errors=True)


# Feature: molt, Property 22: For any hook payload, including malformed JSON,
# absent fields, oversized fields, non-UTF-8 bytes, and an absent configuration,
# the Capture_Hook exits with status code 0, emits no unredacted secret, and
# writes at most one diagnostic line per failure.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(case=hook_inputs())
def test_the_hook_exits_zero_leaks_nothing_and_writes_one_line(
    hook_area: Path, case: HookInput
) -> None:
    # The valid arm is only valid if it covers every delivered adapter, so the
    # generator's own coverage is stated rather than assumed.
    assert frozenset(VENDOR_DOCUMENTS) == SUPPORTED_TOOLS, (
        "the generator names "
        f"{sorted(frozenset(VENDOR_DOCUMENTS) ^ SUPPORTED_TOOLS)} differently from the "
        "supported set, so an adapter is drawn against no payload of its own"
    )

    event(f"arm={case.arm}")
    event(f"vendor={case.vendor}")
    event(f"payload={_size_band(case.payload)}")
    event(f"arguments={len(case.arguments)}")
    event(f"token={'supported' if case.arguments[:1] == (case.vendor,) else 'unsupported'}")
    event(f"configuration={'absent' if case.unconfigured else 'present'}")

    root = hook_area / token_hex(8)
    root.mkdir(parents=True)
    try:
        outcome = _invoke(case, root)
    finally:
        # The example's tree is removed as soon as the invocation has been judged,
        # so a hundred examples leave one empty directory rather than a hundred
        # spools, one of which held a mebibyte.
        rmtree(root, ignore_errors=True)

    # Requirements 1.7 and 6.6: the status is 0 whatever the payload was and
    # whatever the environment held.
    assert outcome.status == EXIT_OK, (
        f"the {case.arm} arm exited {outcome.status} rather than {EXIT_OK} with "
        f"{'no' if case.unconfigured else 'a'} configuration: {outcome.err!r}"
    )

    # Requirement 1.7: one line per invocation, however many things went wrong.
    assert outcome.lines <= 1, (
        f"standard error received {outcome.lines} lines rather than at most one: {outcome.err!r}"
    )
    if outcome.err:
        assert outcome.err.startswith(DIAGNOSTIC_PREFIX), (
            f"a diagnostic was written without the component prefix: {outcome.err!r}"
        )
        assert outcome.err.endswith(NEWLINE), (
            f"a diagnostic was written without terminating its line: {outcome.err!r}"
        )

    # Requirement 4.1: the planted value is of a shape the Redactor recognises, and
    # it reaches neither stream. Both are judged as bytes, so a diagnostic that is
    # not decodable cannot carry it past this.
    assert SHAPED_BYTES not in outcome.err, (
        f"the planted credential-shaped value reached standard error: {outcome.err!r}"
    )
    assert SHAPED_BYTES not in outcome.out, (
        f"the planted credential-shaped value reached standard output: {outcome.out!r}"
    )

    # Standard output is the vendor's decision channel alone. No credential is
    # configured here, so no response envelope is ever read and no recall result is
    # ever returned, which means no path reaches a structured decision and the
    # channel stays silent.
    assert outcome.out == b"", (
        f"standard output carried {outcome.out!r} where no structured decision was produced"
    )
