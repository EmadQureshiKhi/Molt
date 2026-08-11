"""Loading and running the residue-sweep skill.

Four obligations, in the order a client meets them. The definition parses in the
open format it declares itself in. Its inputs, its outputs, and its behavior are
declared in the format's own metadata fields rather than left to the body's prose.
Every operation it declares falls inside the read-only set. And the entry point it
declares actually runs, completing a transport session and reporting the candidates
the seeded corpus holds with their distances and their decisions.

The instance the entry point runs against is stood up locally: a seeded stand-in for
the interface is placed on PATH, speaking the transport framing and answering the
residue candidate tool from a small corpus, and it records every frame it was sent.
That keeps this suite credential-free and cluster-free while still executing the
declared entry point rather than reading it, and it makes the read-only claim
checkable from the other direction: the recorded frames are the tools the skill
actually called, so a sweep that had reached for a tool outside its declaration
would be visible in them.

**Validates: Requirements 36.17, 39.5, 39.6**
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SKILL_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "skills" / "residue-sweep"
DEFINITION_PATH: Final[Path] = SKILL_DIRECTORY / "SKILL.md"

# The declaration keys, carried in the format's metadata map rather than in a
# manifest of this project's own invention.
INPUTS_KEY: Final[str] = "molt-inputs"
OUTPUTS_KEY: Final[str] = "molt-outputs"
BEHAVIOR_KEY: Final[str] = "molt-behavior"
OPERATIONS_KEY: Final[str] = "molt-operations"
ENTRY_POINT_KEY: Final[str] = "molt-entry-point"
EFFECT_KEY: Final[str] = "molt-effect"
ROLE_KEY: Final[str] = "molt-database-role"

READ_ONLY_EFFECT: Final[str] = "read_only"
READ_ONLY_ROLE: Final[str] = "reader"

# The read-only operation set. The sweep is a vector search and a threshold
# comparison: it records no candidate row and starts no erasure run.
READ_ONLY_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "cli:attest verify",
        "cli:retention",
        "cli:recall",
        "cli:verify-chain",
        "cli:sensitivity",
        "cli:mcp",
        "mcp:recall_memory",
        "mcp:lineage_ancestors",
        "mcp:lineage_descendants",
        "mcp:residue_candidates",
    }
)

# The one tool this skill declares, and the fields its answer carries.
RESIDUE_TOOL: Final[str] = "residue_candidates"
EXPECTED_OUTPUTS: Final[tuple[str, ...]] = (
    "artifact_id",
    "artifact_kind",
    "cosine_distance",
    "band",
    "decision",
    "candidate_count",
)

# The seeded corpus the stand-in answers from: one candidate inside the auto-include
# band and one inside the review band, so both decisions are exercised.
SEEDED_CLIENT: Final[str] = "tenant-north"
SEEDED_CANDIDATES: Final[tuple[Mapping[str, object], ...]] = (
    {
        "artifact_id": "8b2f6c1e-0a4d-4c7b-9f31-5d6e7a8b9c01",
        "artifact_kind": "summary",
        "cosine_distance": 0.11,
        "band": "auto_include",
        "decision": "include",
    },
    {
        "artifact_id": "8b2f6c1e-0a4d-4c7b-9f31-5d6e7a8b9c02",
        "artifact_kind": "learned_procedure",
        "cosine_distance": 0.38,
        "band": "review",
        "decision": "review",
    },
)

# The names the run is driven and observed through. The transport revision is the
# caller's to name, which is why the entry point refuses without it.
LOG_VARIABLE: Final[str] = "SKILL_STUB_LOG"
SEED_VARIABLE: Final[str] = "SKILL_STUB_SEED"
ROLE_VARIABLE: Final[str] = "MOLT_DB_ROLE"
REVISION_VARIABLE: Final[str] = "MOLT_MCP_PROTOCOL_VERSION"
TRANSPORT_REVISION: Final[str] = "test-revision"
STUB_NAME: Final[str] = "molt"
RUN_TIMEOUT_SECONDS: Final[float] = 30.0
USAGE_STATUS: Final[int] = 2

# The seeded stand-in for the interface. It speaks the transport framing, answers the
# one tool this skill declares from the seeded corpus, and refuses any other tool or
# verb, so a skill that reached beyond its declaration fails here rather than
# passing quietly.
# The stand-in is written as a shell-and-Python polyglot rather than with a plain
# shebang. A shebang carries one unquoted interpreter path, so a checkout whose own
# path contains a space -- or an interpreter installed under one -- produces a script
# the kernel refuses with a bad-interpreter error. The two lines below are read by the
# shell as an exec of the quoted interpreter, and by Python as a comment followed by a
# string expression, so the same file is valid to both and the path may contain
# anything.
STUB_PROGRAM: Final[str] = '''#!/bin/sh
"exec" "{interpreter}" "$0" "$@"
"""A seeded stand-in for the molt interface, answering one stdio transport session."""

import json
import os
import sys


def record(entry: dict[str, object]) -> None:
    """Append one recorded invocation or frame to the log."""
    with open(os.environ["{log_variable}"], "a", encoding="utf-8") as log:
        log.write(json.dumps(entry))
        log.write("\\n")


def main() -> int:
    """Answer one transport session, recording every frame it was sent."""
    argv = sys.argv[1:]
    record({{"argv": argv, "role": os.environ.get("{role_variable}", "")}})
    if argv[:1] != ["mcp"]:
        print("the stand-in answers the tool server alone", file=sys.stderr)
        return 2
    with open(os.environ["{seed_variable}"], encoding="utf-8") as handle:
        seeded = json.load(handle)
    for line in sys.stdin:
        if not line.strip():
            continue
        frame = json.loads(line)
        method = str(frame.get("method", ""))
        record({{"method": method, "params": frame.get("params", {{}})}})
        if method == "initialize":
            print(json.dumps({{"jsonrpc": "2.0", "id": frame["id"], "result": {{"tools": []}}}}))
            continue
        if method != "tools/call":
            continue
        params = frame.get("params", {{}})
        if params.get("name") != "{residue_tool}":
            print("the stand-in exposes one tool", file=sys.stderr)
            return 1
        arguments = params.get("arguments", {{}})
        rows = [row for row in seeded if row["cosine_distance"] <= arguments.get(
            "review_threshold", 1.0)]
        if "limit" in arguments:
            rows = rows[: int(arguments["limit"])]
        print(json.dumps({{
            "jsonrpc": "2.0",
            "id": frame["id"],
            "result": {{"candidate_count": len(rows), "candidates": rows}},
        }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# ---------------------------------------------------------------------------
# The definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Definition:
    """One parsed skill definition, reduced to what the assertions walk."""

    frontmatter: Mapping[str, object]
    body: str

    @property
    def metadata(self) -> Mapping[str, object]:
        """The format's metadata map, narrowed to string keys."""
        value = self.frontmatter.get("metadata")
        assert isinstance(value, Mapping), "the definition carries no metadata map"
        narrowed: dict[str, object] = {}
        for key, item in value.items():
            assert isinstance(key, str), "the metadata carries a non-string key"
            narrowed[key] = item
        return narrowed

    def declaration(self, key: str) -> str:
        """One metadata declaration, asserted to be a non-empty string."""
        value = self.metadata.get(key)
        assert isinstance(value, str), f"the definition declares no string {key}"
        assert value.strip(), f"the definition declares an empty {key}"
        return " ".join(value.split())

    def declared_list(self, key: str) -> tuple[str, ...]:
        """One comma-separated metadata declaration, split and trimmed."""
        return tuple(item.strip() for item in self.declaration(key).split(",") if item.strip())

    @property
    def entry_point(self) -> Path:
        """The declared entry point, resolved inside the skill directory."""
        return SKILL_DIRECTORY / self.declaration(ENTRY_POINT_KEY)


def _load() -> Definition:
    """Parse the definition into its frontmatter and its body."""
    assert DEFINITION_PATH.is_file(), "the residue-sweep definition is absent"
    lines = DEFINITION_PATH.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].strip() == "---", "the definition opens with no frontmatter fence"
    closing = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
        None,
    )
    assert closing is not None, "the definition carries no closing frontmatter fence"
    parsed: object = YAML(typ="safe").load("\n".join(lines[1:closing]))
    assert isinstance(parsed, Mapping), "the frontmatter parses to no mapping"
    frontmatter = {str(key): value for key, value in parsed.items()}
    return Definition(frontmatter=frontmatter, body="\n".join(lines[closing + 1 :]))


DEFINITION: Final[Definition] = _load()


def test_the_definition_parses_into_frontmatter_and_body() -> None:
    """The format's two halves are both present and both carry content."""
    assert DEFINITION.frontmatter.get("name") == "residue-sweep"
    assert isinstance(DEFINITION.frontmatter.get("description"), str)
    assert DEFINITION.body.strip(), "the definition carries an empty body"


def test_the_declared_inputs_outputs_and_behavior_are_present() -> None:
    """Each of the three is declared in the format's own field rather than left to prose."""
    inputs = DEFINITION.declared_list(INPUTS_KEY)
    outputs = DEFINITION.declared_list(OUTPUTS_KEY)
    assert "client-slug" in inputs
    assert set(outputs) == set(EXPECTED_OUTPUTS)
    assert DEFINITION.declaration(BEHAVIOR_KEY), "the definition declares no behavior"
    for name in (*inputs, *outputs):
        assert f"`{name}`" in DEFINITION.body, f"the body details no {name}"


def test_every_declared_operation_falls_inside_the_read_only_set() -> None:
    """The sweep is a search and a comparison, and its declaration says so."""
    operations = DEFINITION.declared_list(OPERATIONS_KEY)
    assert operations, "the definition names no operation"
    for operation in operations:
        assert operation in READ_ONLY_OPERATIONS, f"{operation} is no read-only operation"
    assert f"mcp:{RESIDUE_TOOL}" in operations
    assert DEFINITION.declaration(EFFECT_KEY) == READ_ONLY_EFFECT
    assert DEFINITION.declaration(ROLE_KEY) == READ_ONLY_ROLE


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    """What one execution of the entry point produced."""

    status: int
    answers: tuple[Mapping[str, object], ...]
    vectors: tuple[tuple[str, ...], ...]
    roles: tuple[str, ...]
    frames: tuple[Mapping[str, object], ...]
    stderr: str

    @property
    def called_tools(self) -> tuple[str, ...]:
        """Every tool the recorded frames named, in call order."""
        called: list[str] = []
        for frame in self.frames:
            params = frame.get("params")
            if frame.get("method") == "tools/call" and isinstance(params, Mapping):
                called.append(str(params.get("name")))
        return tuple(called)


def _execute(root: Path, arguments: tuple[str, ...], *, revision: str = TRANSPORT_REVISION) -> Run:
    """Run the declared entry point against the seeded stand-in on PATH."""
    binaries = root / "bin"
    binaries.mkdir()
    seed = root / "seed.json"
    seed.write_text(json.dumps(list(SEEDED_CANDIDATES)), encoding="utf-8")
    log = root / "invocations.jsonl"
    log.touch()
    stub = binaries / STUB_NAME
    stub.write_text(
        STUB_PROGRAM.format(
            interpreter=sys.executable,
            log_variable=LOG_VARIABLE,
            seed_variable=SEED_VARIABLE,
            role_variable=ROLE_VARIABLE,
            residue_tool=RESIDUE_TOOL,
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)

    environment = dict(os.environ)
    environment["PATH"] = f"{binaries}{os.pathsep}{environment.get('PATH', '')}"
    environment[LOG_VARIABLE] = str(log)
    environment[SEED_VARIABLE] = str(seed)
    environment.pop(ROLE_VARIABLE, None)
    if revision:
        environment[REVISION_VARIABLE] = revision
    else:
        environment.pop(REVISION_VARIABLE, None)

    entry_point = DEFINITION.entry_point
    assert entry_point.is_file(), "the declared entry point is absent"
    completed = subprocess.run(  # noqa: S603 - a fixed vector of this module's own values
        [str(entry_point), *arguments],
        cwd=SKILL_DIRECTORY,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=RUN_TIMEOUT_SECONDS,
    )
    recorded = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return Run(
        status=completed.returncode,
        answers=tuple(json.loads(line) for line in completed.stdout.splitlines() if line.strip()),
        vectors=tuple(
            tuple(str(item) for item in entry["argv"]) for entry in recorded if "argv" in entry
        ),
        roles=tuple(str(entry["role"]) for entry in recorded if "argv" in entry),
        frames=tuple(entry for entry in recorded if "method" in entry),
        stderr=completed.stderr,
    )


def test_the_declared_entry_point_executes_against_the_seeded_instance(tmp_path: Path) -> None:
    """The sweep completes a session and reports the seeded candidates with their decisions."""
    run = _execute(tmp_path, ("--client", SEEDED_CLIENT))

    assert run.status == 0, run.stderr
    assert run.vectors == (("mcp", "--transport", "stdio"),)
    assert run.roles == (READ_ONLY_ROLE,), "the entry point sets the role rather than inheriting it"
    assert run.called_tools == (RESIDUE_TOOL,), "the sweep calls the one tool it declares"

    handshake = run.frames[0]
    parameters = handshake.get("params")
    assert handshake.get("method") == "initialize"
    assert isinstance(parameters, Mapping)
    assert parameters.get("protocolVersion") == TRANSPORT_REVISION

    result = run.answers[-1].get("result")
    assert isinstance(result, Mapping)
    candidates = result.get("candidates")
    assert isinstance(candidates, list)
    assert result.get("candidate_count") == len(SEEDED_CANDIDATES)
    for candidate in candidates:
        assert isinstance(candidate, Mapping)
        for field_name in EXPECTED_OUTPUTS:
            if field_name == "candidate_count":
                continue
            assert field_name in candidate, f"the candidate carries no {field_name}"
    assert {str(candidate["decision"]) for candidate in candidates} == {"include", "review"}


def test_the_thresholds_and_the_bound_reach_the_tool_the_caller_named(tmp_path: Path) -> None:
    """Every declared input the caller names arrives inside the tool arguments."""
    run = _execute(
        tmp_path,
        (
            "--client",
            SEEDED_CLIENT,
            "--auto-include-threshold",
            "0.2",
            "--review-threshold",
            "0.2",
            "--limit",
            "1",
        ),
    )

    assert run.status == 0, run.stderr
    call = next(frame for frame in run.frames if frame.get("method") == "tools/call")
    params = call.get("params")
    assert isinstance(params, Mapping)
    arguments = params.get("arguments")
    assert isinstance(arguments, Mapping)
    assert arguments.get("client_slug") == SEEDED_CLIENT
    assert arguments.get("auto_include_threshold") == 0.2
    assert arguments.get("review_threshold") == 0.2
    assert arguments.get("limit") == 1

    result = run.answers[-1].get("result")
    assert isinstance(result, Mapping)
    # The narrowed review threshold excludes the candidate beyond it, so the bound the
    # caller named is the bound the answer respects rather than a post-filter.
    assert result.get("candidate_count") == 1


def test_the_entry_point_refuses_a_slug_outside_the_accepted_shape(tmp_path: Path) -> None:
    """A refused slug never reaches the interface, so nothing is recorded."""
    run = _execute(tmp_path, ("--client", "Not A Slug"))

    assert run.status == USAGE_STATUS, run.stderr
    assert run.vectors == ()
    assert run.frames == ()


def test_the_entry_point_refuses_when_the_caller_names_no_transport_revision(
    tmp_path: Path,
) -> None:
    """The revision is the caller's to name, so its absence is a usage refusal."""
    run = _execute(tmp_path, ("--client", SEEDED_CLIENT), revision="")

    assert run.status == USAGE_STATUS, run.stderr
    assert REVISION_VARIABLE in run.stderr
    assert run.frames == ()
