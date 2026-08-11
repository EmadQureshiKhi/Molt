"""Loading and running the retention-audit skill.

Four obligations, in the order a client meets them. The definition parses in the
open format it declares itself in. Its inputs, its outputs, and its behavior are
declared in the format's own metadata fields rather than left to the body's prose.
Every operation it declares falls inside the read-only set. And the entry point it
declares actually runs, producing one report object per audited client.

The instance the entry point runs against is stood up locally: a seeded stand-in for
the interface is placed on PATH, holding a small corpus of clients with their
jurisdictions, intervals, and counts, and it records every argument vector it was
handed. That keeps this suite credential-free and cluster-free while still executing
the declared entry point rather than reading it, and it makes the read-only claim
checkable from the other direction: the recorded vectors are what the skill actually
asked the interface to do, and an audit that had reached for a mutating verb would
be visible in them.

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

import pytest
from ruamel.yaml import YAML

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SKILL_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "skills" / "retention-audit"
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

# The read-only operation set. The report path reads the configured jurisdiction, the
# configured interval, and the two counts, and changes none of them, because expiry
# is enforced by the cluster itself.
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

# The fields the definition declares as its outputs, which the run below produces.
EXPECTED_OUTPUTS: Final[tuple[str, ...]] = (
    "client_slug",
    "jurisdiction",
    "retention_interval",
    "expiring_within_seven_days",
    "already_expired",
)

# The seeded corpus the stand-in answers from: two clients under two jurisdictions,
# one with rows already past expiry and one with none.
SEEDED_CLIENTS: Final[tuple[Mapping[str, object], ...]] = (
    {
        "client_slug": "tenant-north",
        "jurisdiction": "eu",
        "retention_interval": "P30D",
        "expiring_within_seven_days": 4,
        "already_expired": 1,
    },
    {
        "client_slug": "tenant-south",
        "jurisdiction": "us",
        "retention_interval": "P365D",
        "expiring_within_seven_days": 0,
        "already_expired": 0,
    },
)

# The names the run is driven and observed through.
LOG_VARIABLE: Final[str] = "SKILL_STUB_LOG"
SEED_VARIABLE: Final[str] = "SKILL_STUB_SEED"
ROLE_VARIABLE: Final[str] = "MOLT_DB_ROLE"
STUB_NAME: Final[str] = "molt"
RUN_TIMEOUT_SECONDS: Final[float] = 30.0

# The seeded stand-in for the interface. It answers the one verb this skill declares,
# records the vector and the role selector it was handed, and refuses anything else,
# so a skill that reached beyond its declaration fails here rather than passing
# quietly.
# The stand-in is written as a shell-and-Python polyglot rather than with a plain
# shebang. A shebang carries one unquoted interpreter path, so a checkout whose own
# path contains a space -- or an interpreter installed under one -- produces a script
# the kernel refuses with a bad-interpreter error. The two lines below are read by the
# shell as an exec of the quoted interpreter, and by Python as a comment followed by a
# string expression, so the same file is valid to both and the path may contain
# anything.
STUB_PROGRAM: Final[str] = '''#!/bin/sh
"exec" "{interpreter}" "$0" "$@"
"""A seeded stand-in for the molt interface, answering the retention report."""

import json
import os
import sys


def main() -> int:
    """Answer one invocation, recording the vector it arrived as."""
    argv = sys.argv[1:]
    with open(os.environ["{log_variable}"], "a", encoding="utf-8") as log:
        log.write(json.dumps({{"argv": argv, "role": os.environ.get("{role_variable}", "")}}))
        log.write("\\n")
    if not argv or argv[0] != "retention":
        print("the stand-in answers the retention report alone", file=sys.stderr)
        return 2
    with open(os.environ["{seed_variable}"], encoding="utf-8") as handle:
        seeded = json.load(handle)
    wanted = argv[argv.index("--client") + 1] if "--client" in argv else None
    reported = [row for row in seeded if wanted is None or row["client_slug"] == wanted]
    if not reported:
        print("the seeded corpus holds no such client", file=sys.stderr)
        return 1
    for row in reported:
        print(json.dumps(row))
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
    assert DEFINITION_PATH.is_file(), "the retention-audit definition is absent"
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
    assert DEFINITION.frontmatter.get("name") == "retention-audit"
    assert isinstance(DEFINITION.frontmatter.get("description"), str)
    assert DEFINITION.body.strip(), "the definition carries an empty body"


def test_the_declared_inputs_outputs_and_behavior_are_present() -> None:
    """Each of the three is declared in the format's own field rather than left to prose."""
    inputs = DEFINITION.declared_list(INPUTS_KEY)
    outputs = DEFINITION.declared_list(OUTPUTS_KEY)
    assert inputs, "the definition names no input"
    assert set(outputs) == set(EXPECTED_OUTPUTS)
    behavior = DEFINITION.declaration(BEHAVIOR_KEY)
    assert behavior, "the definition declares no behavior"
    for name in (*inputs, *outputs):
        assert f"`{name}`" in DEFINITION.body, f"the body details no {name}"


def test_every_declared_operation_falls_inside_the_read_only_set() -> None:
    """The audit is a query, and its declaration says so operation by operation."""
    operations = DEFINITION.declared_list(OPERATIONS_KEY)
    assert operations, "the definition names no operation"
    for operation in operations:
        assert operation in READ_ONLY_OPERATIONS, f"{operation} is no read-only operation"
    assert DEFINITION.declaration(EFFECT_KEY) == READ_ONLY_EFFECT
    assert DEFINITION.declaration(ROLE_KEY) == READ_ONLY_ROLE


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    """What one execution of the entry point produced."""

    status: int
    reports: tuple[Mapping[str, object], ...]
    vectors: tuple[tuple[str, ...], ...]
    roles: tuple[str, ...]
    stderr: str


def _execute(root: Path, arguments: tuple[str, ...]) -> Run:
    """Run the declared entry point against the seeded stand-in on PATH."""
    binaries = root / "bin"
    binaries.mkdir()
    seed = root / "seed.json"
    seed.write_text(json.dumps(list(SEEDED_CLIENTS)), encoding="utf-8")
    log = root / "invocations.jsonl"
    log.touch()
    stub = binaries / STUB_NAME
    stub.write_text(
        STUB_PROGRAM.format(
            interpreter=sys.executable,
            log_variable=LOG_VARIABLE,
            seed_variable=SEED_VARIABLE,
            role_variable=ROLE_VARIABLE,
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)

    environment = dict(os.environ)
    environment["PATH"] = f"{binaries}{os.pathsep}{environment.get('PATH', '')}"
    environment[LOG_VARIABLE] = str(log)
    environment[SEED_VARIABLE] = str(seed)
    environment.pop(ROLE_VARIABLE, None)

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
        reports=tuple(json.loads(line) for line in completed.stdout.splitlines() if line.strip()),
        vectors=tuple(tuple(str(item) for item in entry["argv"]) for entry in recorded),
        roles=tuple(str(entry["role"]) for entry in recorded),
        stderr=completed.stderr,
    )


def test_the_declared_entry_point_executes_against_the_seeded_instance(tmp_path: Path) -> None:
    """One named client is audited, and the report carries every declared output."""
    slug = str(SEEDED_CLIENTS[0]["client_slug"])
    run = _execute(tmp_path, ("--client", slug))

    assert run.status == 0, run.stderr
    assert len(run.reports) == 1
    report = run.reports[0]
    for field_name in EXPECTED_OUTPUTS:
        assert field_name in report, f"the report carries no {field_name}"
    assert report["client_slug"] == slug
    assert run.vectors == (("retention", "--json", "--client", slug),)
    assert run.roles == (READ_ONLY_ROLE,), "the entry point sets the role rather than inheriting it"


def test_the_entry_point_audits_every_covered_client_when_none_is_named(tmp_path: Path) -> None:
    """Naming no client audits the whole corpus, which is the compliance reviewer's ask."""
    run = _execute(tmp_path, ())

    assert run.status == 0, run.stderr
    assert len(run.reports) == len(SEEDED_CLIENTS)
    assert {str(report["client_slug"]) for report in run.reports} == {
        str(row["client_slug"]) for row in SEEDED_CLIENTS
    }
    assert run.vectors == (("retention", "--json"),)


def test_the_entry_point_refuses_a_slug_outside_the_accepted_shape(tmp_path: Path) -> None:
    """A refused slug never reaches the interface, so nothing is recorded."""
    run = _execute(tmp_path, ("--client", "Not A Slug"))

    assert run.status == 2, run.stderr
    assert run.reports == ()
    assert run.vectors == ()


@pytest.mark.parametrize("argument", ["--truncate", "--erase"])
def test_the_entry_point_refuses_an_argument_its_declaration_does_not_name(
    tmp_path: Path,
    argument: str,
) -> None:
    """An unknown argument is a usage refusal rather than a vector handed onward."""
    run = _execute(tmp_path, (argument,))

    assert run.status == 2, run.stderr
    assert run.vectors == ()
