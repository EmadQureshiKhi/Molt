"""Gate assertions for the linter check and the formatter check.

Both checks are run the way the workflow runs them: the same interpreter, the
same module invocation, the four checked paths passed on the command line, and
the repository root as the working directory. The paths are passed explicitly
rather than left to discovery because that is how the workflow states them, and
stating them at the call site is what keeps the scope visible.

The suites are inside the checked scope, so both checks read this file. That is
the intent rather than a hazard: a style violation written here fails the test
that guards style violations, and the run terminates because the process launched
is the linter and not the test runner.

The formatter is run in check mode, and one assertion below establishes that
check mode rewrote nothing, by comparing a size and modification snapshot of
every checked source file taken either side of the run. A gate that silently
reformatted the tree would otherwise pass while destroying the property it is
supposed to report on.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final

import pytest

# Static gates over tracked files: no reachable instance and no credential.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.quality

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MANIFEST: Final[Path] = REPOSITORY_ROOT / "pyproject.toml"

#: The four paths the linter and the formatter cover, in the order the workflow
#: passes them.
CHECKED_PATHS: Final[tuple[str, ...]] = ("src/molt", "tests", "scripts", "infra")

#: The rule families the configuration selects and no change may quietly drop.
#: Each one is load-bearing for an obligation stated elsewhere: annotations,
#: naming, security-sensitive constructs, path handling, and import order.
REQUIRED_RULE_FAMILIES: Final[tuple[str, ...]] = (
    "ANN",
    "B",
    "E",
    "F",
    "I",
    "N",
    "PTH",
    "RET",
    "S",
    "UP",
    "W",
)

GATE_TIMEOUT_SECONDS: Final[float] = 900.0

CREDENTIAL_PREFIXES: Final[tuple[str, ...]] = (
    "AWS_",
    "AMAZON_",
    "MOLT_",
    "PG",
    "COCKROACH_",
    "DATABASE_",
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """What one gate run reported: its arguments, its status, and both streams."""

    arguments: tuple[str, ...]
    status: int
    out: str
    err: str

    @property
    def report(self) -> str:
        """A failure message carrying the invocation and everything it printed."""
        return "\n".join(
            (
                f"exit status {self.status} from: {' '.join(self.arguments)}",
                self.out.rstrip(),
                self.err.rstrip(),
            )
        ).rstrip()


def credential_free_environment() -> dict[str, str]:
    """Copy the environment with every credential-bearing name removed."""
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(CREDENTIAL_PREFIXES)
    }


def run_linter() -> GateResult:
    """Run the linter over the four checked paths and capture both streams.

    The invocation is written out in full at the call site rather than assembled
    from parts, so what runs here is textually the workflow step. The path list
    below and the checked-path constant above are held in agreement by the
    invocation test in this module, which reads both, so the two cannot drift.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "src/molt", "tests", "scripts", "infra"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=credential_free_environment(),
        timeout=GATE_TIMEOUT_SECONDS,
    )
    return GateResult(
        ("-m", "ruff", "check", *CHECKED_PATHS),
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def run_formatter_check() -> GateResult:
    """Run the formatter in check mode over the same four paths.

    Check mode is what makes this a gate rather than an edit: a file that would
    be reformatted is reported and no file is rewritten.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            "src/molt",
            "tests",
            "scripts",
            "infra",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=credential_free_environment(),
        timeout=GATE_TIMEOUT_SECONDS,
    )
    return GateResult(
        ("-m", "ruff", "format", "--check", *CHECKED_PATHS),
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def lint_settings() -> Mapping[str, Any]:
    """The linter table of the dependency manifest."""
    with MANIFEST.open("rb") as handle:
        manifest = tomllib.load(handle)
    tools = manifest["tool"]
    assert isinstance(tools, dict)
    settings = tools["ruff"]
    assert isinstance(settings, dict)
    return settings


def invocation_tails() -> tuple[tuple[str, ...], ...]:
    """The trailing path words of every gate invocation written in this module.

    Reading this file's own syntax tree is what keeps the spelled-out invocations
    and the checked-path constant in agreement. The invocations have to be spelled
    out at their call sites to stay recognisable as the workflow steps, and this
    is what stops that spelling from quietly diverging from the declared scope.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    collected: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if not (isinstance(called, ast.Attribute) and called.attr == "run"):
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            continue
        words = [
            element.value
            for element in node.args[0].elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        collected.append(tuple(words[-len(CHECKED_PATHS) :]))
    return tuple(collected)


def source_snapshot() -> dict[str, tuple[int, int]]:
    """Record the size and modification reading of every checked source file."""
    recorded: dict[str, tuple[int, int]] = {}
    for path in CHECKED_PATHS:
        for source in sorted((REPOSITORY_ROOT / path).rglob("*.py")):
            status = source.stat()
            recorded[str(source)] = (status.st_size, status.st_mtime_ns)
    return recorded


# ---------------------------------------------------------------------------
# The declared scope and the declared rule set
# ---------------------------------------------------------------------------


def test_every_checked_path_exists() -> None:
    """A path in the invocation that named nothing would check nothing."""
    for path in CHECKED_PATHS:
        assert (REPOSITORY_ROOT / path).is_dir(), f"{path} is not a directory"


def test_each_invocation_names_the_four_checked_paths() -> None:
    """Both gates are invoked over the four paths, in the declared order."""
    tails = invocation_tails()
    assert len(tails) == 2, "the module runs other than the two gates it documents"
    for tail in tails:
        assert tail == CHECKED_PATHS


def test_the_tool_is_pinned_to_one_exact_version() -> None:
    """One exact pin is what makes a developer machine and the workflow agree."""
    required = lint_settings()["required-version"]
    assert isinstance(required, str)
    assert required.startswith("==")
    assert version("ruff") == required.removeprefix("==")


def test_the_selected_rule_families_are_all_present() -> None:
    """The rule set covers every family the surrounding obligations lean on."""
    lint = lint_settings()["lint"]
    assert isinstance(lint, dict)
    selected = {str(code) for code in lint["select"]}
    missing = [family for family in REQUIRED_RULE_FAMILIES if family not in selected]
    assert missing == [], f"rule families no longer selected: {missing}"


def test_no_rule_is_disabled_repository_wide() -> None:
    """Nothing is silenced globally, so a violation is fixed rather than muted."""
    lint = lint_settings()["lint"]
    assert isinstance(lint, dict)
    for key in ("ignore", "extend-ignore"):
        assert not lint.get(key), f"{key} disables rules across the whole tree"


def test_the_only_narrowing_is_scoped_to_the_suites() -> None:
    """The single per-file relaxation covers the suites and nothing else.

    Assertions are the substance of a test module and a conjoined property
    assertion often reads worse split apart, so those two checks are relaxed
    there. Any other narrowing would be a quiet weakening of the gate.
    """
    lint = lint_settings()["lint"]
    assert isinstance(lint, dict)
    per_file = lint["per-file-ignores"]
    assert isinstance(per_file, dict)
    assert set(per_file) == {"tests/**/*.py"}


# ---------------------------------------------------------------------------
# The linter
# ---------------------------------------------------------------------------


def test_the_linter_reports_no_violation() -> None:
    """The four checked paths carry no lint violation."""
    result = run_linter()

    assert result.status == 0, result.report
    assert "All checks passed" in result.out, result.report


# ---------------------------------------------------------------------------
# The formatter
# ---------------------------------------------------------------------------


def test_the_formatter_reports_no_violation_and_rewrites_nothing() -> None:
    """No file would be reformatted, and check mode leaves the tree untouched."""
    before = source_snapshot()
    assert before

    result = run_formatter_check()

    assert result.status == 0, result.report
    assert "Would reformat" not in result.out, result.report
    assert "already formatted" in result.out, result.report
    assert source_snapshot() == before, "the formatter check rewrote a tracked file"


def test_the_formatter_covers_more_than_the_source_package() -> None:
    """The reported file count exceeds what the source package alone holds.

    An invocation that lost three of its four paths would still exit 0, so the
    count is compared against the tracked source under the package.
    """
    package_sources = sum(1 for _ in (REPOSITORY_ROOT / "src" / "molt").rglob("*.py"))
    assert package_sources > 0

    result = run_formatter_check()

    assert result.status == 0, result.report
    counted = [word for word in result.out.split() if word.isdigit()]
    assert counted, result.report
    assert int(counted[0]) > package_sources, result.report


# ---------------------------------------------------------------------------
# Credential independence
# ---------------------------------------------------------------------------


def test_both_checks_run_with_no_credential_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both gates pass while every cloud and instance variable is withheld."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "placeholder")
    monkeypatch.setenv("MOLT_DSN", "placeholder")
    monkeypatch.setenv("PGPASSWORD", "placeholder")

    scrubbed = credential_free_environment()
    assert [name for name in scrubbed if name.startswith(CREDENTIAL_PREFIXES)] == []

    linted = run_linter()
    formatted = run_formatter_check()

    assert linted.status == 0, linted.report
    assert formatted.status == 0, formatted.report
