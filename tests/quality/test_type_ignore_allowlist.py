"""Gate assertions for the type-ignore allowlist check.

The check is bidirectional, and both directions are what make it worth having: a
directive the allowlist names no entry for is a finding, and an allowlist entry
naming a directive the source no longer carries is equally a finding. The first
keeps a silenced type error from passing unnoticed, the second keeps the
allowlist from rotting into a list of exemptions that protect nothing.

Every case below is driven against a tree the test builds under its own
temporary directory, with a temporary allowlist beside it, so no assertion here
mutates tracked source and none of them writes to the shipped allowlist. That
matters more than usual: the shipped allowlist is deliberately empty, and a test
that added an entry to make itself pass would disarm the gate for the whole
repository. Two assertions near the end read the shipped state and the whole
tree instead of a fixture, which is what keeps that emptiness honest.

The gate is loaded from its script path and driven through its entry point,
which returns the status the process exits with, so a status assertion here is a
statement about the exit code the workflow step observes. Loading rather than
launching also keeps the run inside one process, so the environment a case sees
is the environment a case set.

One discipline shapes this module. The directives the cases need are exactly the
directives the gate forbids wherever the allowlist names none, and this file is
tracked source the gate scans. So no directive appears here as a literal: each
one is fused at run time from fragments that match nothing on their own.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

# Static gates over tracked files: no reachable instance and no credential.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.quality

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
GATE_SOURCE: Final[Path] = REPOSITORY_ROOT / "scripts" / "check_type_ignores.py"
SHIPPED_ALLOWLIST: Final[Path] = REPOSITORY_ROOT / "scripts" / "type_ignore_allowlist.txt"


def load_gate() -> ModuleType:
    """Load the gate from its script path, since scripts form no import package."""
    specification = importlib.util.spec_from_file_location(
        "molt_type_ignore_gate_under_test", GATE_SOURCE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    # Registration precedes execution because the gate defines slotted
    # dataclasses, and that decorator resolves its own module by name.
    sys.modules[specification.name] = module
    # Bytecode writing is suppressed for the load, so running this suite leaves
    # no cache directory beside the script it exercises.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        specification.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


GATE: Final[ModuleType] = load_gate()

EXIT_OK: Final[int] = int(GATE.EXIT_OK)
EXIT_FINDINGS: Final[int] = int(GATE.EXIT_FINDINGS)

#: What the gate prints in place of a line number for an allowlist entry naming
#: no live directive, since a stale entry names no line by definition.
STALE_LINE: Final[str] = str(GATE._STALE_LINE)

#: The separator and the column count the allowlist format fixes.
COLUMN_SEPARATOR: Final[str] = str(GATE._COLUMN_SEPARATOR)

#: Environment names withheld from every case. Prefix matching is used rather
#: than an exhaustive list because the point is that the gate sees no cloud,
#: provider, or database variable whatsoever, including ones this file does not
#: know about.
CREDENTIAL_PREFIXES: Final[tuple[str, ...]] = (
    "AWS_",
    "AMAZON_",
    "MOLT_",
    "PG",
    "COCKROACH_",
    "DATABASE_",
)

MODULE_NAME: Final[str] = "carrier.py"
OTHER_NAME: Final[str] = "neighbour.py"
REASON: Final[str] = "a stand-in reason for one temporary case"


def fuse(*parts: str) -> str:
    """Join fragments into one span, so no forbidden literal appears in source."""
    return "".join(parts)


#: The plain spelling and the spelling carrying a bracketed error-code list,
#: both assembled from fragments that carry no match apart.
PLAIN_DIRECTIVE: Final[str] = fuse("# ", "type", ": ", "ignore")
CODED_DIRECTIVE: Final[str] = fuse(PLAIN_DIRECTIVE, "[", "attr-defined", "]")

DIRECTIVE_SPELLINGS: Final[tuple[str, ...]] = (PLAIN_DIRECTIVE, CODED_DIRECTIVE)
DIRECTIVE_IDS: Final[tuple[str, ...]] = ("plain", "with_error_code")


@dataclass(frozen=True, slots=True)
class GateRun:
    """What one run of the gate reported: its status and both streams."""

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

    @property
    def lines(self) -> tuple[str, ...]:
        """The non-empty lines the run printed on the reporting stream."""
        return tuple(line for line in self.out.splitlines() if line.strip())


@pytest.fixture(autouse=True)
def _no_credential_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Withhold every cloud and instance variable from every case in this module.

    Applying this to the whole module rather than to one case makes the
    credential independence structural: no assertion here can pass because a
    variable happened to be set. Restoration is the monkeypatch fixture's, so
    the session outside this module sees its environment unchanged.
    """
    for name in list(os.environ):
        if name.startswith(CREDENTIAL_PREFIXES):
            monkeypatch.delenv(name, raising=False)


def run_gate(capsys: pytest.CaptureFixture[str], *arguments: str) -> GateRun:
    """Drive the gate through its entry point and collect what it reported."""
    status = int(GATE.main(list(arguments)))
    captured = capsys.readouterr()
    return GateRun(arguments, status, captured.out, captured.err)


# ---------------------------------------------------------------------------
# Temporary trees and temporary allowlists
# ---------------------------------------------------------------------------


def clean_module() -> str:
    """Source carrying no directive at all."""
    return '"""A module standing in for tracked source."""\n\nvalue: int = 0\n'


def module_carrying(directive: str) -> tuple[str, int]:
    """Source carrying one directive, with the line number it lands on."""
    lines = (
        '"""A module standing in for tracked source."""',
        "",
        f"value: int = 0  {directive}",
    )
    return "\n".join(lines) + "\n", len(lines)


def make_tree(root: Path, files: Mapping[str, str]) -> Path:
    """Materialise a scan target under the test's own directory."""
    tree = root / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tree


def make_allowlist(root: Path, entries: Sequence[tuple[str, str, str]]) -> Path:
    """Write a well-formed allowlist outside the tree and return its path."""
    holder = root / "lists"
    holder.mkdir(parents=True, exist_ok=True)
    allowlist = holder / "allow.txt"
    separator = f" {COLUMN_SEPARATOR} "
    lines = ["# a temporary allowlist for one case", ""]
    lines.extend(separator.join(entry) for entry in entries)
    lines.append("")
    allowlist.write_text("\n".join(lines), encoding="utf-8")
    return allowlist


def shipped_entries() -> tuple[str, ...]:
    """The entry lines of the shipped allowlist, comments and blanks excluded."""
    text = SHIPPED_ALLOWLIST.read_text(encoding="utf-8")
    return tuple(
        stripped
        for stripped in (line.strip() for line in text.splitlines())
        if stripped and not stripped.startswith("#")
    )


# ---------------------------------------------------------------------------
# An unlisted directive fails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("directive", list(DIRECTIVE_SPELLINGS), ids=list(DIRECTIVE_IDS))
def test_an_unlisted_directive_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], directive: str
) -> None:
    """A directive in a file the allowlist names no entry for is a finding."""
    content, line = module_carrying(directive)
    tree = make_tree(tmp_path, {MODULE_NAME: content})
    allowlist = make_allowlist(tmp_path, ())

    run = run_gate(capsys, "--root", str(tree), "--allowlist", str(allowlist))

    assert run.status == EXIT_FINDINGS, run.report
    assert f"{MODULE_NAME}:{line}:{directive}" in run.lines, run.report


def test_an_entry_for_another_file_does_not_cover_this_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The allowlist match is bound to a path, not to a directive spelling."""
    content, line = module_carrying(PLAIN_DIRECTIVE)
    tree = make_tree(tmp_path, {MODULE_NAME: content, OTHER_NAME: clean_module()})
    allowlist = make_allowlist(tmp_path, ((OTHER_NAME, PLAIN_DIRECTIVE, REASON),))

    run = run_gate(capsys, "--root", str(tree), "--allowlist", str(allowlist))

    # Both directions report in one run: the directive is unlisted for the file
    # it sits in, and the entry naming the other file justifies nothing.
    assert run.status == EXIT_FINDINGS, run.report
    assert f"{MODULE_NAME}:{line}:{PLAIN_DIRECTIVE}" in run.lines, run.report
    assert f"{OTHER_NAME}:{STALE_LINE}:{PLAIN_DIRECTIVE}" in run.lines, run.report


# ---------------------------------------------------------------------------
# A stale allowlist entry fails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("directive", list(DIRECTIVE_SPELLINGS), ids=list(DIRECTIVE_IDS))
def test_a_stale_allowlist_entry_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], directive: str
) -> None:
    """An entry naming a directive the source no longer carries is a finding."""
    tree = make_tree(tmp_path, {MODULE_NAME: clean_module()})
    allowlist = make_allowlist(tmp_path, ((MODULE_NAME, directive, REASON),))

    run = run_gate(capsys, "--root", str(tree), "--allowlist", str(allowlist))

    assert run.status == EXIT_FINDINGS, run.report
    assert f"{MODULE_NAME}:{STALE_LINE}:{directive}" in run.lines, run.report


def test_an_entry_naming_a_removed_file_is_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Removing the file an entry justifies leaves that entry stale as well."""
    tree = make_tree(tmp_path, {OTHER_NAME: clean_module()})
    allowlist = make_allowlist(tmp_path, ((MODULE_NAME, PLAIN_DIRECTIVE, REASON),))

    run = run_gate(capsys, "--root", str(tree), "--allowlist", str(allowlist))

    assert run.status == EXIT_FINDINGS, run.report
    assert f"{MODULE_NAME}:{STALE_LINE}:{PLAIN_DIRECTIVE}" in run.lines, run.report


# ---------------------------------------------------------------------------
# A listed directive passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("directive", list(DIRECTIVE_SPELLINGS), ids=list(DIRECTIVE_IDS))
def test_a_listed_directive_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], directive: str
) -> None:
    """A directive an entry names for its own file is no finding."""
    content, _ = module_carrying(directive)
    tree = make_tree(tmp_path, {MODULE_NAME: content})
    allowlist = make_allowlist(tmp_path, ((MODULE_NAME, directive, REASON),))

    run = run_gate(capsys, "--root", str(tree), "--allowlist", str(allowlist))

    assert run.status == EXIT_OK, run.report
    assert "1 directive(s) found" in run.out, run.report
    assert "named by the allowlist" in run.out, run.report


def test_a_tree_with_no_directive_and_no_entry_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The empty case passes, which is the state the repository is held in."""
    tree = make_tree(tmp_path, {MODULE_NAME: clean_module()})
    allowlist = make_allowlist(tmp_path, ())

    run = run_gate(capsys, "--root", str(tree), "--allowlist", str(allowlist))

    assert run.status == EXIT_OK, run.report
    assert "0 directive(s) found" in run.out, run.report


# ---------------------------------------------------------------------------
# The shipped state
# ---------------------------------------------------------------------------


def test_the_shipped_allowlist_names_no_entry() -> None:
    """The allowlist is empty, so no directive anywhere is currently permitted.

    Keeping it empty is what makes the gate an absolute rule rather than a
    negotiable one: the fix for a reported type error is the code, and adding an
    entry is a visible change to a tracked file that a reviewer reads.
    """
    assert shipped_entries() == ()


def test_the_shipped_allowlist_documents_its_format() -> None:
    """The file explains its own columns, so an entry can be written correctly."""
    text = SHIPPED_ALLOWLIST.read_text(encoding="utf-8").lower()
    assert "format" in text
    assert "reason" in text
    assert COLUMN_SEPARATOR in text


def test_the_repository_carries_no_unlisted_directive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The gate passes over the whole tree, driven as the workflow drives it.

    No argument is passed, so the root and the allowlist are the ones the gate
    resolves for itself, which is the invocation the workflow step performs.
    """
    run = run_gate(capsys)

    assert run.status == EXIT_OK, run.report
    assert "0 directive(s) found" in run.out, run.report


def test_the_whole_tree_run_scanned_more_than_one_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The clean run above covered the tracked source rather than nothing at all."""
    package_sources = sum(1 for _ in (REPOSITORY_ROOT / "src" / "molt").rglob("*.py"))
    assert package_sources > 0

    run = run_gate(capsys)

    assert run.status == EXIT_OK, run.report
    counted = [word for word in run.out.split() if word.isdigit()]
    assert counted, run.report
    assert int(counted[0]) > package_sources, run.report


# ---------------------------------------------------------------------------
# Credential independence
# ---------------------------------------------------------------------------


def test_the_gate_runs_with_no_credential_in_the_environment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing cloud-shaped or instance-shaped is set while the gate runs.

    The withholding is applied to every case in this module by the fixture
    above; this case states the consequence and checks the environment it ran
    under, so the claim is observed rather than assumed.
    """
    assert [name for name in os.environ if name.startswith(CREDENTIAL_PREFIXES)] == []

    run = run_gate(capsys)

    assert run.status == EXIT_OK, run.report
