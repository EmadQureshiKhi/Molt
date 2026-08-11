"""Loading and running the verify-certificate skill.

Four obligations, in the order a client meets them. The definition parses in the
open format it declares itself in. Its inputs, its outputs, and its behavior are
declared in the format's own metadata fields rather than left to the body's prose.
Every operation it declares falls inside the read-only set. And the entry point it
declares actually runs, verifying a certificate and then confirming through the two
lineage tools that no named artifact retains an edge.

The instance the entry point runs against is stood up locally: a seeded stand-in for
the interface is placed on PATH, answering the verification path and the transport
session from a small corpus, and it records every argument vector and every frame it
was sent. That keeps this suite credential-free and cluster-free while still
executing the declared entry point rather than reading it, and it makes the read-only
claim checkable from the other direction: the recorded vectors and frames are what
the skill actually asked the interface to do, so a stage that had reached for a
mutating verb or an undeclared tool would be visible in them.

The failed outcome is exercised beside the verified one, because the exit status is
the reviewer's automation surface: a certificate that does not verify must leave a
non-zero status rather than a line of prose, and the lineage stage must still run so
a surviving descendant is reported rather than skipped.

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
SKILL_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "skills" / "verify-certificate"
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

# The read-only operation set. The verification path holds SELECT alone and the tool
# server the entry point spawns exposes no mutation tool.
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

# The two tools this skill declares, and the fields the two stages produce.
LINEAGE_TOOLS: Final[tuple[str, ...]] = ("lineage_ancestors", "lineage_descendants")
VERIFICATION_OUTPUTS: Final[tuple[str, ...]] = (
    "outcome",
    "failed_checks",
    "verification_query_row_counts",
    "count_agreement",
    "chain_mismatch",
    "checkpoint_outcome",
)

# The seeded corpus the stand-in answers from. One artifact retains no edge, which is
# what a verified certificate claims, and the outcome is settable per run.
SEEDED_ARTIFACT: Final[str] = "8b2f6c1e-0a4d-4c7b-9f31-5d6e7a8b9c01"
VERIFIED_OUTCOME: Final[str] = "verified"
FAILED_OUTCOME: Final[str] = "failed"
FAILED_CHECK_NAME: Final[str] = "signature_invalid"
FAILED_STATUS: Final[int] = 3
USAGE_STATUS: Final[int] = 2

# The names the run is driven and observed through. The transport revision is the
# caller's to name, which is why the entry point refuses without it.
LOG_VARIABLE: Final[str] = "SKILL_STUB_LOG"
SEED_VARIABLE: Final[str] = "SKILL_STUB_SEED"
ROLE_VARIABLE: Final[str] = "MOLT_DB_ROLE"
REVISION_VARIABLE: Final[str] = "MOLT_MCP_PROTOCOL_VERSION"
TRANSPORT_REVISION: Final[str] = "test-revision"
STUB_NAME: Final[str] = "molt"
CERTIFICATE_NAME: Final[str] = "certificate.json"
RUN_TIMEOUT_SECONDS: Final[float] = 30.0

# The seeded stand-in for the interface. It answers the verification path and one
# transport session, refuses any other verb or tool, and exits with the verification
# path's own status, so the skill's exit status is the one a reviewer branches on.
# The stand-in is written as a shell-and-Python polyglot rather than with a plain
# shebang. A shebang carries one unquoted interpreter path, so a checkout whose own
# path contains a space -- or an interpreter installed under one -- produces a script
# the kernel refuses with a bad-interpreter error. The two lines below are read by the
# shell as an exec of the quoted interpreter, and by Python as a comment followed by a
# string expression, so the same file is valid to both and the path may contain
# anything.
STUB_PROGRAM: Final[str] = '''#!/bin/sh
"exec" "{interpreter}" "$0" "$@"
"""A seeded stand-in for the molt interface, answering verification and lineage."""

import json
import os
import sys


def record(entry: dict[str, object]) -> None:
    """Append one recorded invocation or frame to the log."""
    with open(os.environ["{log_variable}"], "a", encoding="utf-8") as log:
        log.write(json.dumps(entry))
        log.write("\\n")


def seeded() -> dict[str, object]:
    """The corpus this stand-in answers from."""
    with open(os.environ["{seed_variable}"], encoding="utf-8") as handle:
        loaded: dict[str, object] = json.load(handle)
    return loaded


def attest_verify(argv: list[str]) -> int:
    """Answer the verification path, returning its own status."""
    corpus = seeded()
    report = dict(corpus["report"])
    if "--certificate" in argv:
        path = argv[argv.index("--certificate") + 1]
        if not os.path.isfile(path):
            print("the named certificate is absent", file=sys.stderr)
            return 2
    print(json.dumps(report))
    return 0 if report["outcome"] == "{verified_outcome}" else {failed_status}


def serve(argv: list[str]) -> int:
    """Answer one transport session over the declared lineage tools."""
    del argv
    corpus = seeded()
    edges = dict(corpus["edges"])
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
        name = str(params.get("name"))
        if name not in {lineage_tools}:
            print("the stand-in exposes the two lineage tools alone", file=sys.stderr)
            return 1
        arguments = params.get("arguments", {{}})
        found = edges.get(name, {{}}).get(str(arguments.get("artifact_id")), [])
        print(json.dumps({{
            "jsonrpc": "2.0",
            "id": frame["id"],
            "result": {{name: found, "row_count": len(found)}},
        }}))
    return 0


def main() -> int:
    """Dispatch one invocation, recording the vector it arrived as."""
    argv = sys.argv[1:]
    record({{"argv": argv, "role": os.environ.get("{role_variable}", "")}})
    if argv[:2] == ["attest", "verify"]:
        return attest_verify(argv)
    if argv[:1] == ["mcp"]:
        return serve(argv)
    print("the stand-in answers verification and the tool server alone", file=sys.stderr)
    return 2


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
    assert DEFINITION_PATH.is_file(), "the verify-certificate definition is absent"
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
    assert DEFINITION.frontmatter.get("name") == "verify-certificate"
    assert isinstance(DEFINITION.frontmatter.get("description"), str)
    assert DEFINITION.body.strip(), "the definition carries an empty body"


def test_the_declared_inputs_outputs_and_behavior_are_present() -> None:
    """Each of the three is declared in the format's own field rather than left to prose."""
    inputs = DEFINITION.declared_list(INPUTS_KEY)
    outputs = DEFINITION.declared_list(OUTPUTS_KEY)
    assert {"certificate", "artifact-id"} <= set(inputs)
    assert set(VERIFICATION_OUTPUTS) <= set(outputs)
    assert set(LINEAGE_TOOLS) <= set(outputs)
    assert DEFINITION.declaration(BEHAVIOR_KEY), "the definition declares no behavior"
    for name in (*inputs, *outputs):
        assert f"`{name}`" in DEFINITION.body, f"the body details no {name}"


def test_every_declared_operation_falls_inside_the_read_only_set() -> None:
    """Both stages are reads, and the declaration says so operation by operation."""
    operations = DEFINITION.declared_list(OPERATIONS_KEY)
    assert operations, "the definition names no operation"
    for operation in operations:
        assert operation in READ_ONLY_OPERATIONS, f"{operation} is no read-only operation"
    assert "cli:attest verify" in operations
    for tool in LINEAGE_TOOLS:
        assert f"mcp:{tool}" in operations
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


def _lineage_results(run: Run) -> tuple[Mapping[str, object], ...]:
    """Every tool answer of a run, narrowed to the results that carry a row count.

    The handshake answer is on the same stream, so the two are told apart by what
    they carry rather than by their position, which keeps the assertions readable
    when the framing gains a frame.
    """
    found: list[Mapping[str, object]] = []
    for answer in run.answers:
        result = answer.get("result")
        if isinstance(result, Mapping) and "row_count" in result:
            found.append({str(key): value for key, value in result.items()})
    return tuple(found)


def _corpus(outcome: str) -> Mapping[str, object]:
    """The seeded corpus, with the verification outcome the example asks for."""
    return {
        "report": {
            "outcome": outcome,
            "failed_checks": [] if outcome == VERIFIED_OUTCOME else [FAILED_CHECK_NAME],
            "verification_query_row_counts": {"no_sessions_remain": 0},
            "count_agreement": outcome == VERIFIED_OUTCOME,
            "chain_mismatch": None,
            "checkpoint_outcome": outcome,
        },
        "edges": {tool: {SEEDED_ARTIFACT: []} for tool in LINEAGE_TOOLS},
    }


def _execute(
    root: Path,
    arguments: tuple[str, ...],
    *,
    outcome: str = VERIFIED_OUTCOME,
    revision: str = TRANSPORT_REVISION,
    place_certificate: bool = True,
) -> Run:
    """Run the declared entry point against the seeded stand-in on PATH."""
    binaries = root / "bin"
    binaries.mkdir()
    seed = root / "seed.json"
    seed.write_text(json.dumps(_corpus(outcome)), encoding="utf-8")
    log = root / "invocations.jsonl"
    log.touch()
    if place_certificate:
        (root / CERTIFICATE_NAME).write_text(json.dumps({"payload": {}}), encoding="utf-8")
    stub = binaries / STUB_NAME
    stub.write_text(
        STUB_PROGRAM.format(
            interpreter=sys.executable,
            log_variable=LOG_VARIABLE,
            seed_variable=SEED_VARIABLE,
            role_variable=ROLE_VARIABLE,
            verified_outcome=VERIFIED_OUTCOME,
            failed_status=FAILED_STATUS,
            lineage_tools=list(LINEAGE_TOOLS),
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
        cwd=root,
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
    """Both declared stages run in order, and the verified outcome exits zero."""
    run = _execute(tmp_path, ("--certificate", CERTIFICATE_NAME, "--artifact-id", SEEDED_ARTIFACT))

    assert run.status == 0, run.stderr
    assert run.vectors == (
        ("attest", "verify", "--json", "--certificate", CERTIFICATE_NAME),
        ("mcp", "--transport", "stdio"),
    )
    assert run.roles == (READ_ONLY_ROLE, READ_ONLY_ROLE), (
        "each stage sets the role rather than inheriting it"
    )
    assert run.called_tools == LINEAGE_TOOLS, "the lineage stage calls the two tools it declares"

    report = run.answers[0]
    assert report["outcome"] == VERIFIED_OUTCOME
    for field_name in VERIFICATION_OUTPUTS:
        assert field_name in report, f"the report carries no {field_name}"

    handshake = run.frames[0]
    parameters = handshake.get("params")
    assert handshake.get("method") == "initialize"
    assert isinstance(parameters, Mapping)
    assert parameters.get("protocolVersion") == TRANSPORT_REVISION

    lineage_results = _lineage_results(run)
    assert len(lineage_results) == len(LINEAGE_TOOLS)
    for result in lineage_results:
        assert result["row_count"] == 0, "a verified certificate leaves no surviving edge"


def test_a_failed_outcome_leaves_a_non_zero_status_and_still_checks_lineage(
    tmp_path: Path,
) -> None:
    """The status is the reviewer's automation surface, and the lineage stage still runs."""
    run = _execute(
        tmp_path,
        ("--certificate", CERTIFICATE_NAME, "--artifact-id", SEEDED_ARTIFACT),
        outcome=FAILED_OUTCOME,
    )

    assert run.status == FAILED_STATUS, run.stderr
    assert run.answers[0]["outcome"] == FAILED_OUTCOME
    assert run.answers[0]["failed_checks"] == [FAILED_CHECK_NAME]
    assert run.called_tools == LINEAGE_TOOLS


def test_the_entry_point_refuses_an_identifier_outside_the_accepted_shape(tmp_path: Path) -> None:
    """A refused identifier never reaches a request body, so nothing is recorded."""
    run = _execute(tmp_path, ("--certificate", CERTIFICATE_NAME, "--artifact-id", "not an id"))

    assert run.status == USAGE_STATUS, run.stderr
    assert run.vectors == ()
    assert run.frames == ()


def test_the_entry_point_refuses_naming_no_certificate_source(tmp_path: Path) -> None:
    """One source is named or none is, and naming none is a usage refusal."""
    run = _execute(
        tmp_path,
        ("--artifact-id", SEEDED_ARTIFACT),
        place_certificate=False,
    )

    assert run.status == USAGE_STATUS, run.stderr
    assert run.vectors == ()


def test_the_entry_point_refuses_when_the_caller_names_no_transport_revision(
    tmp_path: Path,
) -> None:
    """The revision is the caller's to name, so its absence is a usage refusal."""
    run = _execute(
        tmp_path,
        ("--certificate", CERTIFICATE_NAME, "--artifact-id", SEEDED_ARTIFACT),
        revision="",
    )

    assert run.status == USAGE_STATUS, run.stderr
    assert REVISION_VARIABLE in run.stderr
    assert run.frames == ()
