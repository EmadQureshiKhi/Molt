"""Gate assertion for the strict static type check.

The check is run here exactly as the workflow runs it: the same interpreter, the
same module invocation, no path argument, and the repository root as the working
directory. Passing no path is deliberate rather than lazy. The checked path set
belongs to the dependency manifest, so a run driven from this file cannot check a
narrower set than the workflow checks, and the assertions below read that set out
of the manifest instead of restating it.

Two properties of this module are worth stating because they look like problems
and are not.

First, this file is inside the checked set, so the check reads the file that
asserts it. That is the intent: an annotation gap written here fails the very
test that guards annotation gaps. The recursion terminates because the process
launched here is the type checker and not the test runner; nothing in this suite
re-enters pytest, so the nesting is one level deep by construction.

Second, every gate here is launched with a credential-free environment. The
child process is handed no cloud variable and no database variable at all, which
makes the credential independence observable rather than merely claimed, and one
test below checks that the scrubbing is real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

# The suite runs static gates over tracked files and needs neither a reachable
# instance nor any credential, so it carries the marker that says so.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.quality

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MANIFEST: Final[Path] = REPOSITORY_ROOT / "pyproject.toml"

#: The paths the strict check covers, in the order the manifest declares them.
CHECKED_PATHS: Final[tuple[str, ...]] = ("src/molt", "tests", "scripts")

#: The gate is bounded, so a wedged child fails the test rather than holding the
#: session open.
GATE_TIMEOUT_SECONDS: Final[float] = 900.0

#: Environment names removed before a gate is launched. Prefix matching is used
#: rather than an exhaustive name list because the point is to hand the child no
#: cloud, provider, or database variable whatsoever, including ones this file
#: does not know about.
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


def run_type_check() -> GateResult:
    """Run the strict type check from the repository root and capture both streams.

    The invocation is written out in full at the call site rather than assembled
    from parts, so what runs here is textually the workflow step: the interpreter
    running this suite, addressed through its own executable path, the checker as
    a module, and no path argument, which leaves the checked set to the manifest.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "mypy"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=credential_free_environment(),
        timeout=GATE_TIMEOUT_SECONDS,
    )
    return GateResult(("-m", "mypy"), completed.returncode, completed.stdout, completed.stderr)


def type_check_settings() -> Mapping[str, Any]:
    """The type-check table of the dependency manifest."""
    with MANIFEST.open("rb") as handle:
        manifest = tomllib.load(handle)
    tools = manifest["tool"]
    assert isinstance(tools, dict)
    settings = tools["mypy"]
    assert isinstance(settings, dict)
    return settings


# ---------------------------------------------------------------------------
# The declared scope and strictness of the check
# ---------------------------------------------------------------------------


def test_the_manifest_declares_the_three_checked_paths() -> None:
    """The checked set is the source package, the suites, and the scripts."""
    declared = type_check_settings()["files"]
    assert isinstance(declared, list)
    assert tuple(str(item) for item in declared) == CHECKED_PATHS


def test_every_checked_path_exists() -> None:
    """A declared path naming nothing would make the check hollow."""
    for path in CHECKED_PATHS:
        assert (REPOSITORY_ROOT / path).is_dir(), f"{path} is not a directory"


def test_the_check_is_declared_strict() -> None:
    """Strictness is what turns the annotation obligation into a checked one."""
    settings = type_check_settings()
    assert settings["strict"] is True
    for name in (
        "disallow_untyped_defs",
        "disallow_incomplete_defs",
        "disallow_untyped_calls",
        "disallow_any_generics",
        "warn_return_any",
        "warn_unused_ignores",
        "strict_equality",
    ):
        assert settings[name] is True, f"{name} is not enabled"
    # An unfollowed import is the quietest way to lose coverage, so the two
    # settings that would permit one are checked to be off.
    assert settings["ignore_missing_imports"] is False
    assert settings["follow_untyped_imports"] is False


def test_the_manifest_carries_no_per_module_relaxation() -> None:
    """No module is exempted from strictness; a directive is the only escape.

    That escape is itself gated by the ignore allowlist, so an exemption is a
    documented line in a tracked file rather than a quiet table entry here.
    """
    assert "overrides" not in type_check_settings()


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------


def test_the_strict_type_check_reports_no_error() -> None:
    """The declared paths type-check clean under the strict configuration."""
    result = run_type_check()

    assert result.status == 0, result.report
    assert "Success" in result.out, result.report
    assert " error:" not in result.out, result.report


def test_the_check_covers_more_than_the_source_package() -> None:
    """The reported file count exceeds what the source package alone holds.

    A configuration that quietly narrowed to one path would still exit 0, so the
    count the run reports is compared against the tracked source under the
    package. A count at or below that means the suites and the scripts went
    unchecked and the clean exit says less than it appears to.
    """
    package_sources = sum(1 for _ in (REPOSITORY_ROOT / "src" / "molt").rglob("*.py"))
    assert package_sources > 0

    result = run_type_check()

    assert result.status == 0, result.report
    counted = [word for word in result.out.split() if word.isdigit()]
    assert counted, result.report
    assert int(counted[0]) > package_sources, result.report


# ---------------------------------------------------------------------------
# Credential independence
# ---------------------------------------------------------------------------


def test_the_gate_environment_carries_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrubbing removes every cloud and instance variable and keeps the rest.

    The variables are set here rather than assumed absent, so the assertion
    proves removal instead of passing by accident on a bare machine.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "placeholder")
    monkeypatch.setenv("AWS_PROFILE", "placeholder")
    monkeypatch.setenv("MOLT_DSN", "placeholder")
    monkeypatch.setenv("MOLT_TEST_DSN", "placeholder")
    monkeypatch.setenv("PGPASSWORD", "placeholder")
    monkeypatch.setenv("DATABASE_URL", "placeholder")
    monkeypatch.setenv("QUALITY_GATE_PROBE", "kept")

    scrubbed = credential_free_environment()

    assert [name for name in scrubbed if name.startswith(CREDENTIAL_PREFIXES)] == []
    # A name outside the credential prefixes survives, so the scrub is targeted
    # rather than an empty environment that would break every child process.
    assert scrubbed["QUALITY_GATE_PROBE"] == "kept"
