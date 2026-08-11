"""Structural assertions over the three shipped agent skill definitions.

The skills are the executable half of a claim the documentation makes in prose:
that verification, residue detection, and retention auditing are procedures a
client agent can run rather than paragraphs an operator can read. Two properties
carry that claim, and neither is safe to leave to convention.

The first is that each definition is well-formed in the open format it declares
itself in, so a client that never saw this repository loads it without a local
patch: the frontmatter parses, the name obeys the format's naming rule and its
directory, the bounded fields stay inside their bounds, and the metadata map
holds string values as the format requires.

The second is that every operation a definition declares is a read. That is
asserted twice over, from both directions: every operation named in a definition
must fall inside the read-only set, and every interface path an entry point
actually invokes must be one the same definition declares. A definition that
promises a read while its entry point calls a mutating verb therefore fails
here, and so does an entry point that quietly gains a call its definition never
mentioned.

The suite reads tracked files and runs no process, so it needs no cloud provider
credential and no cluster credential. Executing an entry point against a seeded
instance is the separate, instance-marked obligation of the per-skill loading
tests; this module deliberately stops at the declaration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SKILLS_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "skills"
INDEX_PATH: Final[Path] = SKILLS_DIRECTORY / "README.md"
DEFINITION_FILE_NAME: Final[str] = "SKILL.md"

# The three procedures the requirements oblige, one directory each: a
# certificate verified against a live cluster, a residue sweep for a named
# client, and a retention audit per client.
EXPECTED_SKILLS: Final[tuple[str, ...]] = (
    "verify-certificate",
    "residue-sweep",
    "retention-audit",
)

# The format's own frontmatter fields. The first two are required by the format;
# the rest are the optional fields these definitions choose to carry.
REQUIRED_FRONTMATTER_KEYS: Final[tuple[str, ...]] = ("name", "description")
OPTIONAL_FRONTMATTER_KEYS: Final[tuple[str, ...]] = (
    "license",
    "compatibility",
    "allowed-tools",
    "metadata",
)
NAME_LIMIT: Final[int] = 64
DESCRIPTION_LIMIT: Final[int] = 1024
COMPATIBILITY_LIMIT: Final[int] = 500
NAME_SHAPE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# The declaration keys, carried in the format's metadata map rather than in a
# manifest of this project's own invention.
INPUTS_KEY: Final[str] = "molt-inputs"
OUTPUTS_KEY: Final[str] = "molt-outputs"
BEHAVIOR_KEY: Final[str] = "molt-behavior"
OPERATIONS_KEY: Final[str] = "molt-operations"
ENTRY_POINT_KEY: Final[str] = "molt-entry-point"
EFFECT_KEY: Final[str] = "molt-effect"
ROLE_KEY: Final[str] = "molt-database-role"

REQUIRED_METADATA_KEYS: Final[tuple[str, ...]] = (
    INPUTS_KEY,
    OUTPUTS_KEY,
    BEHAVIOR_KEY,
    OPERATIONS_KEY,
    ENTRY_POINT_KEY,
    EFFECT_KEY,
    ROLE_KEY,
)

READ_ONLY_EFFECT: Final[str] = "read_only"
READ_ONLY_ROLE: Final[str] = "reader"

# The read-only interface paths. Each reads and none writes: the verification
# path and the sensitivity grid run under the reader role by design, the
# retention report and the chain verification read configuration and counts, the
# recall path reads ranked artifacts, and the server verb spawns a server whose
# role holds SELECT alone and whose registry carries no mutation tool.
READ_ONLY_CLI_PATHS: Final[frozenset[str]] = frozenset(
    {
        "attest verify",
        "retention",
        "recall",
        "verify-chain",
        "sensitivity",
        "mcp",
    }
)

# The server's four tools, every one of which declares a read-only effect.
READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "recall_memory",
        "lineage_ancestors",
        "lineage_descendants",
        "residue_candidates",
    }
)

CLI_PREFIX: Final[str] = "cli:"
TOOL_PREFIX: Final[str] = "mcp:"
READ_ONLY_OPERATIONS: Final[frozenset[str]] = frozenset(
    [f"{CLI_PREFIX}{path}" for path in READ_ONLY_CLI_PATHS]
    + [f"{TOOL_PREFIX}{tool}" for tool in READ_ONLY_TOOLS]
)

# An invocation of the interface, recognised only where a verb path is followed
# by a flag, a pipe, a redirect, or the end of the line, so that a mention of the
# interface inside a message is not mistaken for a call of it.
INVOCATION: Final[re.Pattern[str]] = re.compile(
    r"\bmolt[ \t]+(?P<path>[a-z][a-z-]*(?:[ \t]+[a-z][a-z-]*)?)(?=[ \t]+-|[ \t]*[|>]|[ \t]*$)"
)

# Statement keywords that would make an entry point a writer. The read-only
# posture rests on the role grant, but an entry point naming one of these would
# mean the definition and the script disagree, which is the finding.
MUTATING_KEYWORDS: Final[tuple[str, ...]] = (
    "insert",
    "update",
    "delete",
    "upsert",
    "truncate",
    "drop",
    "alter",
    "create",
    "grant",
    "revoke",
    "backup",
    "restore",
)

# The prefix a client gives a server tool in its pre-approved tool list, and the
# file-reading tool the bodies rely on.
TOOL_TOOL_PREFIX: Final[str] = "mcp__molt__"
FILE_READ_TOOL: Final[str] = "Read"
SHELL_TOOL: Final[re.Pattern[str]] = re.compile(r"^Bash\((?P<target>[^:)]+)(?::\*)?\)$")


@dataclass(frozen=True, slots=True)
class Definition:
    """One parsed skill definition, reduced to what the assertions walk."""

    directory: str
    path: Path
    frontmatter: Mapping[str, object]
    body: str

    @property
    def metadata(self) -> Mapping[str, object]:
        """The format's metadata map, narrowed to string keys."""
        return _as_mapping(self.frontmatter.get("metadata"), f"the metadata of {self.directory}")

    def field(self, key: str) -> str:
        """One frontmatter field, asserted to be a string."""
        value = self.frontmatter.get(key)
        assert isinstance(value, str), f"{self.directory} declares no string {key}"
        return value

    def declaration(self, key: str) -> str:
        """One metadata declaration, asserted to be a non-empty string."""
        value = self.metadata.get(key)
        assert isinstance(value, str), f"{self.directory} declares no string {key}"
        assert value.strip(), f"{self.directory} declares an empty {key}"
        return value

    def declared_list(self, key: str) -> tuple[str, ...]:
        """One comma-separated metadata declaration, split and trimmed."""
        raw = self.declaration(key)
        return tuple(item.strip() for item in raw.split(",") if item.strip())

    @property
    def entry_point(self) -> Path:
        """The declared entry point, resolved inside the skill directory."""
        return self.path.parent / self.declaration(ENTRY_POINT_KEY)


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    """Narrow a parsed node to a mapping with string keys."""
    assert isinstance(value, Mapping), f"{label} is no mapping"
    narrowed: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str), f"{label} carries a non-string key"
        narrowed[key] = item
    return narrowed


def _split_definition(text: str, label: str) -> tuple[str, str]:
    """Split a definition into its frontmatter block and its body."""
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", f"{label} opens with no frontmatter fence"
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise AssertionError(f"{label} carries no closing frontmatter fence")


def _load(directory: str) -> Definition:
    """Parse one skill definition into plain data."""
    path = SKILLS_DIRECTORY / directory / DEFINITION_FILE_NAME
    label = f"the definition of {directory}"
    assert path.is_file(), f"{label} is absent"
    block, body = _split_definition(path.read_text(encoding="utf-8"), label)
    reader = YAML(typ="safe")
    parsed: object = reader.load(block)
    return Definition(
        directory=directory,
        path=path,
        frontmatter=_as_mapping(parsed, f"the frontmatter of {directory}"),
        body=body,
    )


def _code_lines(script: Path) -> tuple[str, ...]:
    """The lines of a script that are neither blank nor whole-line comments."""
    kept: list[str] = []
    for line in script.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kept.append(line)
    return tuple(kept)


def _invoked_paths(script: Path) -> frozenset[str]:
    """Every interface verb path the script's code lines invoke."""
    found: set[str] = set()
    for line in _code_lines(script):
        for match in INVOCATION.finditer(line):
            found.add(" ".join(match.group("path").split()))
    return frozenset(found)


DEFINITIONS: Final[tuple[Definition, ...]] = tuple(_load(name) for name in EXPECTED_SKILLS)
INDEX: Final[str] = INDEX_PATH.read_text(encoding="utf-8")


def test_every_obliged_procedure_ships_as_its_own_skill_directory() -> None:
    directories = {
        path.name for path in SKILLS_DIRECTORY.iterdir() if path.is_dir() and path.name[0] != "."
    }
    assert set(EXPECTED_SKILLS) <= directories
    assert len(EXPECTED_SKILLS) >= 3


def test_every_definition_parses_into_frontmatter_and_body() -> None:
    for definition in DEFINITIONS:
        assert definition.frontmatter, f"{definition.directory} declares an empty frontmatter"
        assert definition.body.strip(), f"{definition.directory} carries an empty body"


def test_every_frontmatter_field_belongs_to_the_format() -> None:
    permitted = set(REQUIRED_FRONTMATTER_KEYS) | set(OPTIONAL_FRONTMATTER_KEYS)
    for definition in DEFINITIONS:
        for key in REQUIRED_FRONTMATTER_KEYS:
            assert key in definition.frontmatter, f"{definition.directory} declares no {key}"
        unknown = set(definition.frontmatter) - permitted
        assert not unknown, f"{definition.directory} declares fields outside the format: {unknown}"


def test_every_name_obeys_the_naming_rule_and_matches_its_directory() -> None:
    for definition in DEFINITIONS:
        name = definition.field("name")
        assert name == definition.directory, f"{name} does not match its directory"
        assert 1 <= len(name) <= NAME_LIMIT
        assert NAME_SHAPE.fullmatch(name), f"{name} is no valid skill name"


def test_every_bounded_field_stays_inside_the_format_bound() -> None:
    for definition in DEFINITIONS:
        description = definition.field("description")
        assert 1 <= len(description) <= DESCRIPTION_LIMIT
        compatibility = definition.field("compatibility")
        assert 1 <= len(compatibility) <= COMPATIBILITY_LIMIT


def test_every_metadata_value_is_a_string_as_the_format_requires() -> None:
    for definition in DEFINITIONS:
        for key, value in definition.metadata.items():
            assert isinstance(value, str), f"{definition.directory} maps {key} to a non-string"


def test_every_definition_declares_its_inputs_outputs_and_behavior() -> None:
    for definition in DEFINITIONS:
        for key in REQUIRED_METADATA_KEYS:
            definition.declaration(key)
        assert definition.declared_list(INPUTS_KEY), f"{definition.directory} names no input"
        assert definition.declared_list(OUTPUTS_KEY), f"{definition.directory} names no output"


def test_every_declared_input_and_output_is_detailed_in_the_body() -> None:
    for definition in DEFINITIONS:
        for key in (INPUTS_KEY, OUTPUTS_KEY):
            for name in definition.declared_list(key):
                assert f"`{name}`" in definition.body, (
                    f"{definition.directory} declares {name} without detailing it in the body"
                )


def test_every_declared_operation_falls_inside_the_read_only_set() -> None:
    for definition in DEFINITIONS:
        operations = definition.declared_list(OPERATIONS_KEY)
        assert operations, f"{definition.directory} names no operation"
        for operation in operations:
            normalised = " ".join(operation.split())
            assert normalised in READ_ONLY_OPERATIONS, (
                f"{definition.directory} declares {normalised}, which is no read-only operation"
            )


def test_every_definition_declares_the_read_only_effect_and_the_reader_role() -> None:
    for definition in DEFINITIONS:
        assert definition.declaration(EFFECT_KEY) == READ_ONLY_EFFECT
        assert definition.declaration(ROLE_KEY) == READ_ONLY_ROLE


def test_every_declared_entry_point_exists_inside_its_skill_and_is_executable() -> None:
    for definition in DEFINITIONS:
        entry_point = definition.entry_point
        assert entry_point.is_file(), f"{definition.directory} declares an absent entry point"
        assert entry_point.resolve().is_relative_to(definition.path.parent.resolve()), (
            f"{definition.directory} declares an entry point outside its own directory"
        )
        assert entry_point.stat().st_mode & 0o111, (
            f"the entry point of {definition.directory} is not executable"
        )


def test_every_path_an_entry_point_invokes_is_declared_and_read_only() -> None:
    for definition in DEFINITIONS:
        declared = {
            " ".join(operation.split())
            for operation in definition.declared_list(OPERATIONS_KEY)
            if operation.startswith(CLI_PREFIX)
        }
        invoked = _invoked_paths(definition.entry_point)
        assert invoked, f"the entry point of {definition.directory} invokes nothing"
        for path in invoked:
            assert path in READ_ONLY_CLI_PATHS, (
                f"the entry point of {definition.directory} invokes {path}, which is no read"
            )
            assert f"{CLI_PREFIX}{path}" in declared, (
                f"the entry point of {definition.directory} invokes an undeclared {path}"
            )


def test_no_entry_point_names_a_mutating_statement_keyword() -> None:
    for definition in DEFINITIONS:
        for line in _code_lines(definition.entry_point):
            folded = line.lower()
            for keyword in MUTATING_KEYWORDS:
                assert not re.search(rf"\b{keyword}\b", folded), (
                    f"the entry point of {definition.directory} names {keyword}"
                )


def test_every_pre_approved_tool_is_read_only_and_reaches_no_further_than_the_skill() -> None:
    for definition in DEFINITIONS:
        entry_point = definition.declaration(ENTRY_POINT_KEY)
        for tool in definition.field("allowed-tools").split():
            if tool == FILE_READ_TOOL:
                continue
            if tool.startswith(TOOL_TOOL_PREFIX):
                assert tool.removeprefix(TOOL_TOOL_PREFIX) in READ_ONLY_TOOLS, (
                    f"{definition.directory} pre-approves {tool}, which is no read-only tool"
                )
                continue
            shell = SHELL_TOOL.fullmatch(tool)
            assert shell is not None, f"{definition.directory} pre-approves {tool} in no known form"
            target = shell.group("target").removeprefix("./")
            assert target == entry_point, (
                f"{definition.directory} pre-approves a shell target beyond its entry point"
            )


def test_every_definition_declares_only_tools_its_operations_name() -> None:
    for definition in DEFINITIONS:
        declared_tools = {
            operation.removeprefix(TOOL_PREFIX).strip()
            for operation in definition.declared_list(OPERATIONS_KEY)
            if operation.startswith(TOOL_PREFIX)
        }
        approved_tools = {
            tool.removeprefix(TOOL_TOOL_PREFIX)
            for tool in definition.field("allowed-tools").split()
            if tool.startswith(TOOL_TOOL_PREFIX)
        }
        assert approved_tools == declared_tools, (
            f"{definition.directory} pre-approves a tool set its operations do not name"
        )


def test_the_index_names_the_format_the_definitions_use_and_the_loading_path() -> None:
    assert "Agent Skills" in INDEX
    assert DEFINITION_FILE_NAME in INDEX
    assert "frontmatter" in INDEX
    assert "without modification" in INDEX
    for directory in EXPECTED_SKILLS:
        assert directory in INDEX, f"the index names no {directory}"


def test_the_index_names_every_read_only_operation() -> None:
    for operation in sorted(READ_ONLY_OPERATIONS):
        assert f"`{operation}`" in INDEX, f"the index names no {operation}"
