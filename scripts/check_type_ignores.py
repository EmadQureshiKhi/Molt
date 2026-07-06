#!/usr/bin/env python3.12
"""Bidirectional gate over type-check ignore directives.

The scan walks the tracked tree, applying the repository ignore rules, and
collects every type-check ignore directive it finds in Python source. That
found set is then compared against the allowlist in both directions:

* a directive the allowlist names no entry for is a finding, so silencing the
  type checker is a documented decision rather than a quiet one;
* an allowlist entry naming a directive the source no longer carries is also a
  finding, so the allowlist cannot rot into a list of stale exemptions.

Findings are printed one per line as ``path:line:directive``, with a line
number of ``-`` for a stale entry, which names no live line by definition.

Exit status is 0 with no finding, 1 with at least one finding in either
direction, and 2 when the allowlist file is absent or malformed.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Final

EXIT_OK: Final[int] = 0
EXIT_FINDINGS: Final[int] = 1
EXIT_MALFORMED: Final[int] = 2

#: Matches a type-check ignore directive in a trailing or standalone comment,
#: with or without a bracketed error-code list. The pattern is written so that
#: this module's own source carries no literal directive to match.
_DIRECTIVE: Final[re.Pattern[str]] = re.compile(r"#\s*type:\s*ignore(?:\[[^\]]*\])?")

_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")

_SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset({".py", ".pyi"})
_COLUMN_SEPARATOR: Final[str] = "|"
_ENTRY_COLUMNS: Final[int] = 3
_STALE_LINE: Final[str] = "-"

#: Directories that carry no tracked source regardless of the ignore rules.
_SKIPPED_NAMES: Final[frozenset[str]] = frozenset({"__pycache__", "node_modules"})

_ALLOWLIST_NAME: Final[str] = "type_ignore_allowlist.txt"


@dataclass(frozen=True, slots=True)
class Directive:
    """One type-check ignore directive located in tracked source."""

    path: str
    line: int
    text: str

    @property
    def key(self) -> tuple[str, str]:
        """The pair the allowlist matches on: the file path and the directive."""
        return (self.path, self.text)


@dataclass(frozen=True, slots=True)
class Entry:
    """One allowlist entry: a file path, an exact directive, and a reason."""

    path: str
    directive: str
    reason: str

    @property
    def key(self) -> tuple[str, str]:
        """The pair the source is matched against."""
        return (self.path, self.directive)


@dataclass(frozen=True, slots=True)
class IgnoreRules:
    """The subset of the repository ignore rules the walk needs."""

    directory_patterns: tuple[str, ...]
    path_patterns: tuple[str, ...]

    def skips_directory(self, name: str, relative: str) -> bool:
        """Report whether a directory is outside the tracked tree."""
        if name.startswith(".") or name in _SKIPPED_NAMES:
            return True
        if any(fnmatch(name, pattern) for pattern in self.directory_patterns):
            return True
        return self._matches_path_pattern(name, relative)

    def skips_file(self, name: str, relative: str) -> bool:
        """Report whether a file is outside the tracked tree."""
        if name.startswith("."):
            return True
        return self._matches_path_pattern(name, relative)

    def _matches_path_pattern(self, name: str, relative: str) -> bool:
        return any(
            fnmatch(name, pattern) or fnmatch(relative, pattern) for pattern in self.path_patterns
        )


class MalformedAllowlistError(Exception):
    """Raised when the allowlist file is absent or an entry is unreadable."""


def load_ignore_rules(root: Path) -> IgnoreRules:
    """Read the repository ignore file and reduce it to matchable patterns."""
    ignore_file = root / ".gitignore"
    directory_patterns: list[str] = []
    path_patterns: list[str] = []
    if ignore_file.is_file():
        for raw in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "!")):
                continue
            pattern = line.lstrip("/")
            if pattern.endswith("/"):
                directory_patterns.append(pattern.rstrip("/"))
            else:
                path_patterns.append(pattern)
    return IgnoreRules(tuple(directory_patterns), tuple(path_patterns))


def tracked_sources(root: Path, rules: IgnoreRules) -> list[Path]:
    """Collect tracked Python source files under a root, ignore rules applied."""
    collected: list[Path] = []
    pending: list[Path] = [root]
    while pending:
        directory = pending.pop()
        for child in sorted(directory.iterdir()):
            relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                continue
            if child.is_dir():
                if not rules.skips_directory(child.name, relative):
                    pending.append(child)
            elif child.suffix in _SOURCE_SUFFIXES and not rules.skips_file(child.name, relative):
                collected.append(child)
    return sorted(collected)


def normalise(text: str) -> str:
    """Collapse whitespace runs so a directive has one comparable spelling."""
    return _WHITESPACE_RUN.sub(" ", text.strip())


def scan_file(path: Path, root: Path) -> list[Directive]:
    """Collect every type-check ignore directive in one source file."""
    content = path.read_text(encoding="utf-8", errors="replace")
    relative = path.relative_to(root).as_posix()
    found: list[Directive] = []
    for number, line in enumerate(content.splitlines(), start=1):
        found.extend(
            Directive(relative, number, normalise(match.group(0)))
            for match in _DIRECTIVE.finditer(line)
        )
    return found


def load_allowlist(path: Path) -> list[Entry]:
    """Parse the allowlist, raising when the file is absent or an entry is bad."""
    if not path.is_file():
        message = f"allowlist file not found: {path}"
        raise MalformedAllowlistError(message)
    entries: list[Entry] = []
    for number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        columns = [column.strip() for column in line.split(_COLUMN_SEPARATOR)]
        if len(columns) != _ENTRY_COLUMNS or not all(columns):
            message = (
                f"{path}:{number}: an entry needs exactly {_ENTRY_COLUMNS} "
                f"non-empty columns separated by {_COLUMN_SEPARATOR!r}"
            )
            raise MalformedAllowlistError(message)
        entries.append(Entry(columns[0], normalise(columns[1]), columns[2]))
    return entries


def report(directives: list[Directive], entries: list[Entry], scanned: int) -> int:
    """Print both failure directions and return the exit status."""
    allowed = {entry.key for entry in entries}
    present = {directive.key for directive in directives}

    unlisted = [directive for directive in directives if directive.key not in allowed]
    stale = [entry for entry in entries if entry.key not in present]

    for directive in unlisted:
        print(f"{directive.path}:{directive.line}:{directive.text}")
    for entry in stale:
        print(f"{entry.path}:{_STALE_LINE}:{entry.directive}")

    if unlisted or stale:
        print(
            f"{len(unlisted)} directive(s) with no allowlist entry, "
            f"{len(stale)} allowlist entry(ies) naming no existing directive",
            file=sys.stderr,
        )
        return EXIT_FINDINGS

    print(
        f"{scanned} source file(s) scanned, "
        f"{len(directives)} directive(s) found, all named by the allowlist"
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the check."""
    parser = argparse.ArgumentParser(
        description=(
            "Compare the type-check ignore directives in tracked source against "
            "the allowlist in both directions."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root to scan; defaults to the parent of the script directory",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="allowlist file to compare against; defaults to the file beside the script",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the check and return the process exit status."""
    arguments = build_parser().parse_args(argv)
    script_directory = Path(__file__).resolve().parent
    root: Path = (arguments.root or script_directory.parent).resolve()
    allowlist: Path = arguments.allowlist or (root / "scripts" / _ALLOWLIST_NAME)
    if arguments.allowlist is None and not allowlist.is_file():
        allowlist = script_directory / _ALLOWLIST_NAME

    try:
        entries = load_allowlist(allowlist)
    except MalformedAllowlistError as error:
        print(str(error), file=sys.stderr)
        return EXIT_MALFORMED

    sources = tracked_sources(root, load_ignore_rules(root))
    directives: list[Directive] = []
    for source in sources:
        directives.extend(scan_file(source, root))
    return report(directives, entries, len(sources))


if __name__ == "__main__":
    sys.exit(main())
