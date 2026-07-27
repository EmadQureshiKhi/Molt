"""The five adapters' mapping tables, driven as tables rather than case by case.

Each adapter publishes two module attributes that together are its half of the
design's mapping table: `CONSUMED_FIELDS`, the payload field names it reads per
vendor hook event, and `EMITTED_CATEGORIES`, the Event categories that event maps
to. The generated per-tool notes are produced from those tables rather than
maintained beside them, so a table that disagrees with the code it describes is a
false note about what Molt records.

This suite is the anti-drift gate over both tables. The per-vendor behavioural
cases live elsewhere and assert one payload shape at a time; what is asserted here
is every entry of both tables, for all five adapters, as a parametrised sweep:
that the categories an entry declares are exactly the categories `to_events`
produces for that event name (Requirement 1.2), that the fields an entry declares
do reach the Event it produces, that the two tables cannot name different sets of
hook events, and that wherever an entry yields subagent parentage the child
Session key extends the parent's (Requirement 1.4).

Nothing here reads configuration, opens a socket, or consults a wall clock: the
invocation index lives in a directory of the test's own and the instants come from
the injected manual time source.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol
from uuid import uuid4

import pytest

from molt.capture.adapters import claude_code, codex, copilot, cursor, gemini_cli
from molt.capture.adapters.invocation_index import InvocationIndex
from molt.capture.protocol import (
    AdapterCapabilities,
    CaptureContext,
    ClientRef,
    HookAdapter,
    HookInvocation,
    RecallResult,
    derive_session_id,
)
from molt.models.event import Event, EventCategory, JsonObject, JsonValue

MACHINE: Final[str] = "machine-under-test"
WORKSPACE: Final[str] = "/work/acme"
SESSION_KEY: Final[str] = "conversation-1"

# What every declared field is filled with, so a field that reaches the Event is
# visible in the rendered Event and a field that does not is visible by absence.
# Deliberately unlike any value the Redactor acts on, because a replaced value
# would be indistinguishable from a field the adapter never read.
MARKER: Final[str] = "declared-field-value"

# The reason a refusal carries, which a blocking envelope must report back.
REASON: Final[str] = "the Session is halted"


class ManualClock(Protocol):
    """The two readings an adapter takes from the injected time source."""

    def now(self) -> datetime:
        """The current wall reading."""

    def monotonic(self) -> float:
        """The current monotonic reading."""


# ---------------------------------------------------------------------------
# The five tables under test
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """One adapter's tables, plus the two things a payload must carry to be read.

    Attributes:
        tool: The token the shim is installed with.
        module: The adapter module itself, which the per-field probe reads the
            source of. Held here rather than looked up by name so the probe cannot
            drift onto a module the rest of the suite is not exercising.
        consumed: The adapter's declared field names, per vendor hook event.
        emitted: The adapter's declared Event categories, per vendor hook event.
        session_field: The payload field this vendor names its run in, which is
            what a parentage assertion compares against.
        parentage: Whether any entry of this table yields subagent parentage,
            which is a fact about the vendor's specification rather than a choice.
    """

    tool: str
    module: ModuleType
    consumed: Mapping[str, tuple[str, ...]]
    emitted: Mapping[str, tuple[str, ...]]
    session_field: str
    parentage: bool


SPECS: Final[tuple[AdapterSpec, ...]] = (
    AdapterSpec(
        tool=claude_code.TOOL,
        module=claude_code,
        consumed=claude_code.CONSUMED_FIELDS,
        emitted=claude_code.EMITTED_CATEGORIES,
        session_field="session_id",
        parentage=True,
    ),
    AdapterSpec(
        tool=cursor.TOOL,
        module=cursor,
        consumed=cursor.CONSUMED_FIELDS,
        emitted=cursor.EMITTED_CATEGORIES,
        session_field="conversation_id",
        parentage=True,
    ),
    AdapterSpec(
        tool=codex.TOOL,
        module=codex,
        consumed=codex.CONSUMED_FIELDS,
        emitted=codex.EMITTED_CATEGORIES,
        session_field="session_id",
        parentage=True,
    ),
    AdapterSpec(
        tool=gemini_cli.TOOL,
        module=gemini_cli,
        consumed=gemini_cli.CONSUMED_FIELDS,
        emitted=gemini_cli.EMITTED_CATEGORIES,
        session_field="session_id",
        parentage=False,
    ),
    AdapterSpec(
        tool=copilot.TOOL,
        module=copilot,
        consumed=copilot.CONSUMED_FIELDS,
        emitted=copilot.EMITTED_CATEGORIES,
        session_field="sessionId",
        parentage=True,
    ),
)

SPEC_IDS: Final[tuple[str, ...]] = tuple(spec.tool for spec in SPECS)

# One case per entry of one adapter's table. This is what makes the sweep a sweep:
# an entry added to a table without the mapping to go with it fails on its own
# case rather than hiding inside a passing suite.
ENTRIES: Final[tuple[tuple[AdapterSpec, str], ...]] = tuple(
    (spec, event_name) for spec in SPECS for event_name in sorted(spec.emitted)
)
ENTRY_IDS: Final[tuple[str, ...]] = tuple(
    f"{spec.tool}-{event_name}" for spec, event_name in ENTRIES
)


def build_adapter(spec: AdapterSpec, index: InvocationIndex) -> HookAdapter:
    """The delivered adapter for one table, with the test's own index injected."""
    if spec.tool == claude_code.TOOL:
        return claude_code.ClaudeCodeAdapter(index=index)
    if spec.tool == cursor.TOOL:
        return cursor.CursorAdapter(index=index)
    if spec.tool == codex.TOOL:
        return codex.CodexAdapter(index=index)
    if spec.tool == gemini_cli.TOOL:
        return gemini_cli.GeminiCliAdapter(index=index)
    return copilot.CopilotAdapter(index=index)


def build_context(clock: ManualClock, tool: str) -> CaptureContext:
    """The identity one invocation's Events are built against."""
    return CaptureContext(
        session_id=derive_session_id(tool, SESSION_KEY),
        client=ClientRef(id=uuid4(), slug="acme", assigned=True),
        machine_id=MACHINE,
        agent_cli=tool,
        clock=clock,
        workspace_path=WORKSPACE,
    )


def place(document: JsonObject, path: tuple[str, ...], value: str) -> None:
    """Set one declared field, which may be a dotted name of a nested field.

    A table may declare both a container and a field inside it, so a name already
    holding an object is left as the object and a name holding text is replaced by
    one when a nested field needs it.
    """
    branch = document
    for step in path[:-1]:
        existing = branch.get(step)
        if not isinstance(existing, dict):
            existing = {}
            branch[step] = existing
        branch = existing
    if not isinstance(branch.get(path[-1]), dict):
        branch[path[-1]] = value


def declared_payload(spec: AdapterSpec, event_name: str) -> JsonObject:
    """A payload carrying exactly the fields one table entry declares.

    The nested names are placed first so a declared container is built as an
    object before a sibling entry could fill it with text, and the event name and
    the run identifier are written last because those two decide what is mapped
    rather than what is recorded.
    """
    document: JsonObject = {}
    for name in sorted(spec.consumed[event_name], key=lambda field: -field.count(".")):
        place(document, tuple(name.split(".")), MARKER)
    document["hook_event_name"] = event_name
    document[spec.session_field] = SESSION_KEY
    return document


def mapped(
    spec: AdapterSpec,
    event_name: str,
    directory: Path,
    clock: ManualClock,
) -> tuple[HookInvocation, list[Event]]:
    """Parse one entry's payload and map it, returning both for assertions."""
    adapter = build_adapter(spec, InvocationIndex(directory / "index", MACHINE))
    invocation = adapter.parse(json.dumps(declared_payload(spec, event_name)).encode("utf-8"))
    return invocation, adapter.to_events(invocation, build_context(clock, spec.tool))


def rendered(events: list[Event]) -> str:
    """Every Event of one mapping, rendered as one body of text to search."""
    payloads: list[JsonValue] = [event.payload for event in events]
    bodies = [event.text_body or "" for event in events]
    return json.dumps(payloads) + "".join(bodies)


# ---------------------------------------------------------------------------
# The per-field probe
# ---------------------------------------------------------------------------

# The table the probe reads, named once so the probe and the adapters cannot
# disagree about which attribute is under examination.
CONSUMED_TABLE: Final[str] = "CONSUMED_FIELDS"

# A field name no vendor specification defines, used to show the probe can fail.
UNDOCUMENTED_NAME: Final[str] = "a_field_no_vendor_documents"


def table_statement(tree: ast.Module) -> ast.stmt:
    """The statement that assigns the declared-field table."""
    for statement in tree.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == CONSUMED_TABLE
        ):
            return statement
    raise AssertionError(CONSUMED_TABLE)


def prose_statements(tree: ast.Module) -> Iterator[ast.stmt]:
    """Every docstring the module holds, at module, class, and function level.

    Excluded from the probe because prose that mentions a field name is a claim
    about the vendor rather than a read of the payload, and a field named only in a
    docstring is exactly the drift being looked for.
    """
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, scopes):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            yield first


def text_literals(node: ast.AST, excluded: frozenset[int]) -> Iterator[str]:
    """Every text literal under a node, pruning the subtrees that are excluded."""
    if id(node) in excluded:
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    for child in ast.iter_child_nodes(node):
        yield from text_literals(child, excluded)


def names_the_adapter_reads(module: ModuleType) -> frozenset[str]:
    """The text literals an adapter's code holds outside its table and its prose.

    A payload field is read by naming it, so a declared name that appears nowhere in
    the module other than in the table it is declared in is a name the adapter cannot
    be reading. The probe works on the parsed module rather than on the text, so a
    name inside a comment is not mistaken for a read, and it names no field itself,
    so it keeps catching drift without being edited when a table gains an entry.

    What it does not do is attribute a read to one hook event: the dispatch that
    decides which function serves an event would have to be modelled, and the
    identity fields every event declares are read in `parse` rather than in any
    per-event function. Module scope is therefore the granularity, and it is the
    granularity that caught four declared-but-unread names.
    """
    source = module.__file__
    assert source is not None, module.__name__
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    excluded = frozenset(id(node) for node in (table_statement(tree), *prose_statements(tree)))
    return frozenset(text_literals(tree, excluded))


READS: Final[Mapping[str, frozenset[str]]] = {
    spec.tool: names_the_adapter_reads(spec.module) for spec in SPECS
}


def recall_results(count: int) -> list[RecallResult]:
    """A ranked result set, for the two response envelopes."""
    return [
        RecallResult(
            artifact_id=uuid4(),
            distance=0.1 * (position + 1),
            outcome="succeeded",
            session_id=uuid4(),
            machine_id=MACHINE,
            occurred_at=datetime.fromtimestamp(0.0, tz=UTC),
            excerpt=f"a prior attempt {position}",
        )
        for position in range(count)
    ]


# ---------------------------------------------------------------------------
# The two tables describe the same hook events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_the_two_tables_name_exactly_the_same_hook_events(spec: AdapterSpec) -> None:
    """Neither table may name an event the other omits, in either direction.

    The notes are generated from both, so an event with fields and no categories
    would document an input that records nothing, and an event with categories and
    no fields would document an Event built from nothing.
    """
    consumed = set(spec.consumed)
    emitted = set(spec.emitted)

    assert consumed - emitted == set()
    assert emitted - consumed == set()
    assert consumed


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_every_declared_category_is_one_the_event_model_defines(spec: AdapterSpec) -> None:
    """A table entry naming a category that does not exist could never be built."""
    declared = {value for values in spec.emitted.values() for value in values}

    assert declared
    assert {EventCategory(value).value for value in declared} == declared


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_every_declared_field_entry_is_a_non_empty_set_of_field_names(spec: AdapterSpec) -> None:
    """A duplicated or empty field name in a table is a note that misreports itself."""
    for event_name, fields in spec.consumed.items():
        assert fields, event_name
        assert len(set(fields)) == len(fields), event_name
        assert all(name and name == name.strip() for name in fields), event_name
        assert spec.emitted[event_name], event_name


# ---------------------------------------------------------------------------
# The sweep: every entry of every table, mapped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("spec", "event_name"), ENTRIES, ids=ENTRY_IDS)
def test_each_table_entry_emits_exactly_the_categories_it_declares(
    spec: AdapterSpec,
    event_name: str,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The declared categories are what the adapter produces, entry by entry.

    The payload is built from the entry's own declared fields, so the case states
    nothing about the vendor beyond what the table already claims. A category that
    drifted from the mapping, and an event whose mapping fell through to the
    catch-all, both surface here as one failing case naming the entry.
    """
    _, events = mapped(spec, event_name, tmp_path, time_source)

    assert tuple(event.category.value for event in events) == spec.emitted[event_name]
    assert events


@pytest.mark.parametrize(("spec", "event_name"), ENTRIES, ids=ENTRY_IDS)
def test_each_table_entry_reaches_its_event_from_the_fields_it_declares(
    spec: AdapterSpec,
    event_name: str,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The declared fields are an input surface rather than a hand-kept list.

    Every declared field is filled with one recognisable value, so an entry whose
    fields no longer reach the Event it describes produces an Event carrying none
    of them. This is the weaker half of the claim on purpose: it holds the entry to
    the fields it declares without asserting which one of them a given category
    carries, which is the per-vendor business of the behavioural cases.
    """
    _, events = mapped(spec, event_name, tmp_path, time_source)

    assert MARKER in rendered(events)


@pytest.mark.parametrize(("spec", "event_name"), ENTRIES, ids=ENTRY_IDS)
def test_every_individual_declared_field_is_named_by_the_adapter_that_declares_it(
    spec: AdapterSpec,
    event_name: str,
) -> None:
    """The stronger half of the claim: each declared name, not the declared set.

    The case above is satisfied by an entry where one field reaches the Event and the
    rest are decorative, which is how a table comes to promise inputs no code reads:
    the generated per-tool notes would then describe an input surface wider than the
    one Molt has. Here every name of the entry is required to appear in the adapter's
    own code, and a dotted name is required segment by segment, because a nested field
    is only reachable through the container that holds it.
    """
    read = READS[spec.tool]

    for name in spec.consumed[event_name]:
        for segment in name.split("."):
            assert segment in read, f"{spec.tool}:{event_name}:{name}"


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_the_per_field_probe_reports_a_name_the_adapter_does_not_read(
    spec: AdapterSpec,
) -> None:
    """The probe is not vacuous, which a probe over a whole module has to show.

    A literal set gathered from a large module could hold most short identifiers by
    accident, and a check that nothing can fail is a check that proves nothing. A name
    no vendor documents is absent from all five, and the set is non-empty, so the
    sweep above is answering from the code rather than from an empty comparison.
    """
    read = READS[spec.tool]

    assert read
    assert UNDOCUMENTED_NAME not in read


@pytest.mark.parametrize(("spec", "event_name"), ENTRIES, ids=ENTRY_IDS)
def test_where_an_entry_names_a_subagent_the_child_key_extends_the_parent(
    spec: AdapterSpec,
    event_name: str,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """Requirement 1.4, asserted over every entry in both of its directions.

    Where an entry produces parentage, the child Session key is the parent key with
    the agent identifier appended, so a spawned Session names the Session that
    spawned it and the two are distinct. Where an entry produces none, the Session
    key is the vendor's own run key unchanged, so no parentage is invented for an
    invocation that observed no spawn. Asserted as a sweep rather than over the
    events one vendor happens to call subagent events, because which events carry
    an agent identifier is the vendor's decision and changes with its reference.
    """
    invocation, _ = mapped(spec, event_name, tmp_path, time_source)

    if invocation.subagent is None:
        assert invocation.session_key == SESSION_KEY
        return
    parent = invocation.subagent.parent_session_key
    assert parent
    assert invocation.session_key is not None
    assert invocation.session_key != parent
    assert invocation.session_key.startswith(parent)


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_parentage_is_produced_by_the_tables_whose_vendor_documents_it(
    spec: AdapterSpec,
    tmp_path: Path,
    time_source: ManualClock,
) -> None:
    """The conditional sweep above is not vacuous, and is vacuous where it must be.

    Four of the five vendors document a payload identifying a spawned subagent and
    those four produce parentage from at least one table entry. The fifth documents
    none, and inventing parentage for it would be a claim about a run that nothing
    observed.
    """
    observed = any(
        mapped(spec, event_name, tmp_path / event_name, time_source)[0].subagent is not None
        for event_name in sorted(spec.emitted)
    )

    assert observed is spec.parentage


# ---------------------------------------------------------------------------
# Capability flags against the envelopes they describe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=SPEC_IDS)
def test_the_capability_flags_agree_with_the_envelopes_the_adapter_writes(
    spec: AdapterSpec,
    tmp_path: Path,
) -> None:
    """A flag is a claim about a channel, so the channel must behave as claimed.

    Structured output means both envelopes decode as one JSON object, and a
    blocking decision means the refusal carries the reason it was given. The
    context-injection flag is asserted elsewhere to be advisory rather than absent;
    what matters here is that no flag claims a channel the adapter cannot write.
    """
    adapter = build_adapter(spec, InvocationIndex(tmp_path / "index", MACHINE))
    capability = adapter.capabilities()
    injection = adapter.context_injection(recall_results(2))
    refusal = adapter.blocking_response(REASON)

    assert isinstance(capability, AdapterCapabilities)
    if capability.structured_stdout:
        assert isinstance(json.loads(injection.decode("utf-8")), dict)
        assert isinstance(json.loads(refusal.decode("utf-8")), dict)
    if capability.blocking_decision:
        assert REASON.encode("utf-8") in refusal
    # Memory is surfaced whichever channel the vendor documents, so an adapter
    # declaring no injection envelope still renders the results somewhere.
    assert injection
