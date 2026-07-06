#!/usr/bin/env python3.12
"""Metadata-hygiene gate over the repository's own source and documentation.

The gate builds its file list by walking the tree and applying the ignore
rules, so ignored and untracked paths are excluded before any file is opened.
Each retained file is matched against eight pattern classes. Two of the eight
are driven by a data file of forbidden tokens, because personal names and the
identifiers of studied reading material cannot be recognised by shape. A match
that falls wholly inside an allowlisted platform or vendor term is not a
finding, which is what lets the documentation state the names it is obliged to
state.

Exit status is 0 with a per-class scanned-file count, 1 with one line per
finding and a total, and 2 when a list file is malformed, so a broken
configuration is never reported as a clean scan.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

CLASS_EMAIL: Final[str] = "email_address"
CLASS_DATE: Final[str] = "calendar_date"
CLASS_TIME: Final[str] = "clock_time"
CLASS_TIMESTAMP: Final[str] = "timestamp_literal"
CLASS_VERSION: Final[str] = "version_history"
CLASS_ATTRIBUTION: Final[str] = "attribution"
CLASS_PERSONAL: Final[str] = "personal_name"
CLASS_PROJECT: Final[str] = "reference_project"

CLASS_ORDER: Final[tuple[str, ...]] = (
    CLASS_EMAIL,
    CLASS_DATE,
    CLASS_TIME,
    CLASS_TIMESTAMP,
    CLASS_VERSION,
    CLASS_ATTRIBUTION,
    CLASS_PERSONAL,
    CLASS_PROJECT,
)

# The two token-driven classes. The licence text is scanned for these alone,
# because a licence legitimately carries the attribution line every other file
# is forbidden from carrying.
TOKEN_CLASSES: Final[tuple[str, ...]] = (CLASS_PERSONAL, CLASS_PROJECT)

SCANNED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".py",
        ".md",
        ".sql",
        ".yaml",
        ".yml",
        ".toml",
        ".html",
        ".js",
        ".css",
        ".sh",
    }
)
DOCUMENT_SUFFIXES: Final[frozenset[str]] = frozenset({".md"})
LICENCE_FILE_NAME: Final[str] = "LICENSE"

# Directory names never descended into, whatever the ignore rules say. Reading
# material is held under the reference names and is read by nothing here.
PRUNED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {".git", "reference", ".reference", ".secrets"}
)
# Hidden directories are local material by default; these carry tracked
# definitions the gate must cover.
HIDDEN_DIRECTORIES_SCANNED: Final[frozenset[str]] = frozenset({".github"})

SPAN_LIMIT: Final[int] = 40

DENYLIST_SECTIONS: Final[dict[str, str]] = {
    "personal-name": CLASS_PERSONAL,
    "reference-project": CLASS_PROJECT,
}
ALLOWLIST_SECTIONS: Final[frozenset[str]] = frozenset({"database", "cloud", "agent-cli", "tooling"})

_MONTH: Final[str] = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?"
    r"|Dec(?:ember)?)"
)


@dataclass(frozen=True, slots=True)
class ShapeRule:
    """One shape-recognised pattern class."""

    class_name: str
    pattern: re.Pattern[str]
    comment_context_only: bool


SHAPE_RULES: Final[tuple[ShapeRule, ...]] = (
    ShapeRule(
        CLASS_EMAIL,
        re.compile(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*"
            r"\.[A-Za-z]{2,}"
        ),
        False,
    ),
    ShapeRule(
        CLASS_DATE,
        re.compile(
            r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
            r"|\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b"
            r"|\b" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
            r"|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"\.?,?\s+\d{4}\b"
        ),
        False,
    ),
    ShapeRule(
        CLASS_TIME,
        re.compile(
            r"(?<![\d.:%$])\d{1,2}:[0-5]\d(?::[0-5]\d)?"
            r"(?:\s?[AaPp]\.?[Mm]\.?)?"
            r"(?:\s?(?:[Zz]|UTC|GMT)|\s?[+\-]\d{2}:?\d{2})?(?![\d.:])"
        ),
        False,
    ),
    ShapeRule(
        CLASS_TIMESTAMP,
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
            r"(?:[Zz]|[+\-]\d{2}:?\d{2})?"
        ),
        False,
    ),
    # Epoch-shaped integers are ambiguous in running code, so they are a
    # finding in comment and documentation context only.
    ShapeRule(CLASS_TIMESTAMP, re.compile(r"\b1[5-9]\d{8}\b"), True),
    ShapeRule(CLASS_TIMESTAMP, re.compile(r"\b1[5-9]\d{11}\b"), True),
    ShapeRule(
        CLASS_VERSION,
        re.compile(
            r"^[ \t]{0,3}(?:#{1,6}[ \t]*|[-*+][ \t]+|\d+\.[ \t]+)?"
            r"(?:chang[e][ \t]?log|releas[e][ \t]+notes|"
            r"revision[ \t]+histor[y]|version[ \t]+histor[y]|"
            r"what['\u2019]s[ \t]+new|unreleased)\b",
            re.IGNORECASE,
        ),
        False,
    ),
    ShapeRule(
        CLASS_VERSION,
        re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*\[?v?\d+\.\d+\.\d+"),
        False,
    ),
    ShapeRule(
        CLASS_ATTRIBUTION,
        re.compile(
            r"\bcopyrigh[t]\b|\u00a9"
            r"|\(c\)[ \t]*\d{4}|(?-i:\(c\)[ \t]+[A-Z])"
            r"|\bautho[r]s?\b[ \t]*[:=]|@autho[r]\b"
            r"|\bmaintaine[r]s?\b[ \t]*[:=]|@maintaine[r]\b"
            r"|\b(?:written|authored|created|contributed|maintained)"
            r"[ \t]+b[y]\b",
            re.IGNORECASE,
        ),
        False,
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One prohibited pattern occurrence."""

    path: str
    line: int
    class_name: str
    span: str

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.class_name}:{self.span}"

    def as_object(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "class": self.class_name,
            "span": self.span,
        }


@dataclass(frozen=True, slots=True)
class ScanResult:
    """The outcome of one scan over one tree."""

    findings: tuple[Finding, ...]
    scanned_counts: dict[str, int]
    files_scanned: int


class ListFileError(Exception):
    """A denylist or allowlist file could not be read as its format requires."""

    def __init__(self, path: Path, line: int, reason: str) -> None:
        self.path = path
        self.line = line
        self.reason = reason
        location = f"{path}:{line}" if line else str(path)
        super().__init__(f"{location}: {reason}")


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    """One parsed ignore-file rule."""

    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool


def load_ignore_rules(root: Path) -> tuple[IgnoreRule, ...]:
    """Parse the tree's ignore file into rules applied during the walk."""
    source = root / ".gitignore"
    if not source.is_file():
        return ()
    rules: list[IgnoreRule] = []
    text = source.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        directory_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = line.startswith("/")
        line = line.lstrip("/")
        if not line:
            continue
        rules.append(IgnoreRule(line, negated, directory_only, anchored))
    return tuple(rules)


def _rule_matches(rule: IgnoreRule, relative: str, name: str, is_directory: bool) -> bool:
    if rule.directory_only and not is_directory:
        return False
    if rule.anchored or "/" in rule.pattern:
        return fnmatch.fnmatch(relative, rule.pattern) or fnmatch.fnmatch(
            relative, f"{rule.pattern}/*"
        )
    return fnmatch.fnmatch(name, rule.pattern)


def is_ignored(rules: Iterable[IgnoreRule], relative: str, name: str, is_directory: bool) -> bool:
    """Report whether the ignore rules exclude a path, last rule winning."""
    ignored = False
    for rule in rules:
        if _rule_matches(rule, relative, name, is_directory):
            ignored = not rule.negated
    return ignored


def _prunes_directory(rules: Iterable[IgnoreRule], root: Path, directory: Path) -> bool:
    name = directory.name
    if name in PRUNED_DIRECTORY_NAMES:
        return True
    if name.startswith(".") and name not in HIDDEN_DIRECTORIES_SCANNED:
        return True
    if directory.is_symlink():
        return True
    relative = directory.relative_to(root).as_posix()
    return is_ignored(rules, relative, name, True)


def collect_paths(root: Path, excluded: frozenset[Path]) -> list[Path]:
    """Build the scan list by walking the tree and applying the ignore rules.

    Ignored directories, hidden directories other than the tracked-definition
    set, reading-material directories, and symbolic links are pruned before
    their contents are examined, so no file inside them is ever opened.
    """
    rules = load_ignore_rules(root)
    kept: list[Path] = []
    for directory, subdirectories, file_names in os.walk(root):
        current = Path(directory)
        subdirectories[:] = sorted(
            name for name in subdirectories if not _prunes_directory(rules, root, current / name)
        )
        for name in sorted(file_names):
            candidate = current / name
            if candidate in excluded or candidate.is_symlink():
                continue
            if name.startswith("."):
                continue
            if candidate.suffix not in SCANNED_SUFFIXES and name != LICENCE_FILE_NAME:
                continue
            relative = candidate.relative_to(root).as_posix()
            if is_ignored(rules, relative, name, False):
                continue
            kept.append(candidate)
    return kept


def _read_list_file(path: Path, sections: frozenset[str]) -> dict[str, list[str]]:
    """Read a sectioned token file, refusing anything the format disallows."""
    if not path.is_file():
        raise ListFileError(path, 0, "list file is absent")
    grouped: dict[str, list[str]] = {name: [] for name in sections}
    seen: set[str] = set()
    section: str | None = None
    text = path.read_text(encoding="utf-8", errors="replace")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in sections:
                raise ListFileError(path, number, f"unknown section {line}")
            continue
        if section is None:
            raise ListFileError(path, number, "entry precedes any section")
        if any(character.isspace() and character != " " for character in line):
            raise ListFileError(path, number, "entry holds a control character")
        if any(ord(character) < 32 for character in line):
            raise ListFileError(path, number, "entry holds a control character")
        folded = line.casefold()
        if folded in seen:
            raise ListFileError(path, number, f"duplicate entry {line}")
        seen.add(folded)
        grouped[section].append(line)
    return grouped


def _token_pattern(tokens: Sequence[str]) -> re.Pattern[str] | None:
    if not tokens:
        return None
    ordered = sorted(tokens, key=len, reverse=True)
    body = "|".join(re.escape(token) for token in ordered)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])", re.IGNORECASE)


def load_denylist(path: Path) -> dict[str, re.Pattern[str] | None]:
    """Compile one matcher per token-driven class from the denylist file."""
    grouped = _read_list_file(path, frozenset(DENYLIST_SECTIONS))
    return {
        class_name: _token_pattern(grouped[section])
        for section, class_name in DENYLIST_SECTIONS.items()
    }


def load_allowlist(path: Path) -> re.Pattern[str] | None:
    """Compile the matcher for terms whose occurrence is never a finding."""
    grouped = _read_list_file(path, ALLOWLIST_SECTIONS)
    terms: list[str] = []
    for section in sorted(ALLOWLIST_SECTIONS):
        terms.extend(grouped[section])
    return _token_pattern(terms)


def _allowed_spans(line: str, allowlist: re.Pattern[str] | None) -> tuple[tuple[int, int], ...]:
    if allowlist is None:
        return ()
    return tuple((match.start(), match.end()) for match in allowlist.finditer(line))


def _is_allowed(start: int, end: int, spans: tuple[tuple[int, int], ...]) -> bool:
    return any(begin <= start and end <= finish for begin, finish in spans)


def _in_comment_context(suffix: str, line: str, start: int) -> bool:
    if suffix in DOCUMENT_SUFFIXES:
        return True
    prefix = line[:start]
    if any(marker in prefix for marker in ("#", "--", "//", "/*")):
        return True
    return prefix.lstrip().startswith(('"', "'"))


def _in_format_context(line: str, start: int, end: int) -> bool:
    prefix = line[:start]
    if "%" in prefix[-3:]:
        return True
    opened = prefix.rfind("{")
    closed = prefix.rfind("}")
    return opened > closed and "}" in line[end:]


def _truncate(span: str) -> str:
    collapsed = " ".join(span.split())
    if len(collapsed) <= SPAN_LIMIT:
        return collapsed
    return collapsed[:SPAN_LIMIT]


def scan_text(
    relative: str,
    suffix: str,
    text: str,
    classes: Sequence[str],
    denylist: dict[str, re.Pattern[str] | None],
    allowlist: re.Pattern[str] | None,
) -> list[Finding]:
    """Match one file's content against the requested pattern classes."""
    active = frozenset(classes)
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        spans = _allowed_spans(line, allowlist)
        for rule in SHAPE_RULES:
            if rule.class_name not in active:
                continue
            for match in rule.pattern.finditer(line):
                start, end = match.start(), match.end()
                if _is_allowed(start, end, spans):
                    continue
                if rule.comment_context_only and not _in_comment_context(suffix, line, start):
                    continue
                if rule.class_name == CLASS_TIME and _in_format_context(line, start, end):
                    continue
                findings.append(
                    Finding(relative, number, rule.class_name, _truncate(match.group(0)))
                )
        for class_name in TOKEN_CLASSES:
            if class_name not in active:
                continue
            pattern = denylist.get(class_name)
            if pattern is None:
                continue
            for match in pattern.finditer(line):
                if _is_allowed(match.start(), match.end(), spans):
                    continue
                findings.append(Finding(relative, number, class_name, _truncate(match.group(0))))
    return findings


def scan_tree(
    root: Path,
    denylist_path: Path,
    allowlist_path: Path,
) -> ScanResult:
    """Scan every retained path under the root and tally per-class coverage."""
    denylist = load_denylist(denylist_path)
    allowlist = load_allowlist(allowlist_path)
    # The denylist necessarily holds the tokens it forbids, so it is the one
    # path excluded from the scan it drives.
    excluded = frozenset({denylist_path.resolve(), denylist_path})
    counts: dict[str, int] = dict.fromkeys(CLASS_ORDER, 0)
    findings: list[Finding] = []
    paths = collect_paths(root, excluded)
    for path in paths:
        relative = path.relative_to(root).as_posix()
        licence_only = path.name == LICENCE_FILE_NAME
        classes = TOKEN_CLASSES if licence_only else CLASS_ORDER
        for class_name in classes:
            counts[class_name] += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(scan_text(relative, path.suffix, text, classes, denylist, allowlist))
    order = {name: index for index, name in enumerate(CLASS_ORDER)}
    findings.sort(key=lambda item: (item.path, item.line, order[item.class_name]))
    return ScanResult(tuple(findings), counts, len(paths))


def render_findings(result: ScanResult) -> str:
    """Render one line per finding followed by the total."""
    lines = [finding.render() for finding in result.findings]
    affected = len({finding.path for finding in result.findings})
    lines.append(
        f"total: {len(result.findings)} findings in {affected} files"
        f" of {result.files_scanned} scanned"
    )
    return "\n".join(lines)


def render_counts(result: ScanResult) -> str:
    """Render the per-class scanned-file counts of a clean scan."""
    lines = ["hygiene: no findings"]
    for class_name in CLASS_ORDER:
        lines.append(f"{class_name}: {result.scanned_counts[class_name]} files")
    lines.append(f"total: {result.files_scanned} files scanned")
    return "\n".join(lines)


def render_json(result: ScanResult, exit_code: int) -> str:
    """Render the whole outcome as one machine-readable object."""
    payload: dict[str, Any] = {
        "status": "findings" if result.findings else "clean",
        "exit_code": exit_code,
        "files_scanned": result.files_scanned,
        "scanned_file_counts": {name: result.scanned_counts[name] for name in CLASS_ORDER},
        "total_findings": len(result.findings),
        "findings": [finding.as_object() for finding in result.findings],
    }
    return json.dumps(payload, sort_keys=True)


def render_list_error_json(error: ListFileError) -> str:
    """Render a malformed-list outcome as one machine-readable object."""
    payload: dict[str, Any] = {
        "status": "malformed_list_file",
        "exit_code": 2,
        "list_file": str(error.path),
        "line": error.line,
        "reason": error.reason,
    }
    return json.dumps(payload, sort_keys=True)


def _default_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _build_parser() -> argparse.ArgumentParser:
    scripts = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog="hygiene",
        description="Scan tracked source and documentation for prohibited metadata patterns.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help="tree to scan; defaults to the repository root",
    )
    parser.add_argument(
        "--denylist",
        default=str(scripts / "hygiene_denylist.txt"),
        help="path to the forbidden-token file",
    )
    parser.add_argument(
        "--allowlist",
        default=str(scripts / "hygiene_allowlist.txt"),
        help="path to the permitted-term file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit one machine-readable object instead of text",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the gate and return its exit status."""
    arguments = _build_parser().parse_args(argv)
    root = Path(arguments.root).resolve() if arguments.root else _default_root()
    denylist_path = Path(arguments.denylist)
    allowlist_path = Path(arguments.allowlist)
    try:
        result = scan_tree(root, denylist_path, allowlist_path)
    except ListFileError as error:
        if arguments.as_json:
            print(render_list_error_json(error))
        else:
            print(f"hygiene: malformed list file: {error}", file=sys.stderr)
        return 2
    exit_code = 1 if result.findings else 0
    if arguments.as_json:
        print(render_json(result, exit_code))
    elif result.findings:
        print(render_findings(result))
    else:
        print(render_counts(result))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
