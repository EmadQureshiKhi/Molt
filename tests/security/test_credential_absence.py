"""No tracked file carries a credential value, a connection string, or key material.

Requirement 30.1 obliges the Repository to hold no credential, connection string,
bearer token, or private key value, and Requirement 30.12 extends that to every
model provider credential. Both are claims about the whole tree rather than about
any one file, so this module scans the whole tree.

**The path set is built the way the metadata-hygiene gate builds it.** The gate's
own ignore-rule parser, its ignore predicate, and its directory pruning are called
here, so a path this scan reaches is a path that gate would reach and no
version-control command is invoked. What is widened is the file filter: the gate
scans the suffixes it recognises, while a credential can sit in any text file at
all, so every file that decodes as text is read here, including dotfiles the gate
skips. Binary content is skipped by looking for a null byte in its leading bytes,
because a credential is a string and no useful pattern applies to a compiled
artifact.

**Seven shapes are recognised.** A private key block, a connection string carrying
an inline credential, a bearer value on an authorisation header, a cloud access key
identifier, a model provider credential prefix, a quoted assignment to a
secret-named field, and an environment assignment to a secret-named variable. Each
class is one pattern, and each pattern is checked against a synthetic specimen
below, so a pattern that had stopped recognising anything could not pass this scan
by recognising nothing.

**A match is set aside for one of four stated reasons, and only these.** The matched
value holds an interpolation or a call rather than a literal; the value names itself
as an example or a placeholder; the value is a filesystem or parameter path rather
than a secret; or the value is a lowercase hyphen-separated phrase, which is the
form this repository's own synthetic fixtures take and which no issued credential
takes. Each is a statement about the matched value and not about the line it sits
on, so a genuine value on a line that also holds a call is still a finding.

**How this module avoids matching itself.** The denylist of the hygiene gate solves
the analogous problem by excluding one path from the scan it drives. Excluding a
path is the weaker answer, because the excluded file is then the one file nobody
checks. So nothing is excluded here. Instead every pattern and every specimen is
fused at run time from fragments that match nothing on their own, and no complete
pattern or specimen appears as a literal in this source. Two assertions keep that
honest rather than assumed: one scans the pattern sources themselves and requires no
finding, and one asserts this very file is inside the path set the scan covers, so
the scan that must come back empty is a scan that read this module too.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
GATE_SOURCE: Final[Path] = REPOSITORY_ROOT / "scripts" / "hygiene.py"

# How many leading bytes are inspected for a null byte before a file is called
# binary, and the encoding every text file is read as.
BINARY_PROBE_BYTES: Final[int] = 8192
TEXT_ENCODING: Final[str] = "utf-8"


def _load_gate() -> ModuleType:
    """Load the hygiene gate from its script path, since scripts form no package."""
    specification = importlib.util.spec_from_file_location("molt_hygiene_for_paths", GATE_SOURCE)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        specification.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


GATE: Final[ModuleType] = _load_gate()


# ---------------------------------------------------------------------------
# Fragments, fused at run time so no complete pattern appears in this source
# ---------------------------------------------------------------------------


def _fuse(*parts: str) -> str:
    """Join fragments into one pattern or specimen, none of which matches alone."""
    return "".join(parts)


# The secret-named fields a quoted assignment is recognised under, and the
# secret-named environment variables a bare assignment is recognised under.
_FIELD_NAMES: Final[str] = _fuse(
    r"(?i:pass",
    r"word|sec",
    r"ret|to",
    r"ken|api[_-]?key|cred",
    r"ential|priv",
    r"ate[_-]?key|access[_-]?key)",
)
_VARIABLE_NAMES: Final[str] = _fuse(
    r"\b[A-Z][A-Z0-9_]*(?:PASS",
    r"WORD|SEC",
    r"RET|TO",
    r"KEN|API_?KEY|CRED",
    r"ENTIAL|KEY|DSN)[A-Z0-9_]*",
)

CLASS_PRIVATE_KEY: Final[str] = "private_key"
CLASS_CONNECTION_STRING: Final[str] = "connection_string"
CLASS_PRESENTED_BEARER: Final[str] = "presented_bearer"
CLASS_CLOUD_ACCESS_KEY: Final[str] = "cloud_access_key"
CLASS_PROVIDER_CREDENTIAL: Final[str] = "provider_credential"
CLASS_QUOTED_ASSIGNMENT: Final[str] = "quoted_assignment"
CLASS_ENVIRONMENT_ASSIGNMENT: Final[str] = "environment_assignment"

CLASS_ORDER: Final[tuple[str, ...]] = (
    CLASS_PRIVATE_KEY,
    CLASS_CONNECTION_STRING,
    CLASS_PRESENTED_BEARER,
    CLASS_CLOUD_ACCESS_KEY,
    CLASS_PROVIDER_CREDENTIAL,
    CLASS_QUOTED_ASSIGNMENT,
    CLASS_ENVIRONMENT_ASSIGNMENT,
)

# One pattern per class. The value group, where a class has one, is what the four
# set-aside rules are applied to; a class with no value group is judged on its
# whole match, because the whole match is the value.
PATTERN_SOURCES: Final[dict[str, str]] = {
    CLASS_PRIVATE_KEY: _fuse(
        "-",
        "----",
        "BEGIN ",
        "[A-Z ]{0,24}",
        "PRIV",
        "ATE KEY",
        "-",
        "----",
    ),
    CLASS_CONNECTION_STRING: _fuse(
        r"\b",
        "postg",
        r"res(?:ql)?://",
        r"[^\s:/@\"']+",
        ":",
        r"[^\s:/@\"']+",
        "@",
    ),
    CLASS_PRESENTED_BEARER: _fuse(
        r"(?i:bear",
        r"er)\s+",
        r"(?P<value>[A-Za-z0-9_\-\.=]{20,})",
    ),
    CLASS_CLOUD_ACCESS_KEY: _fuse(r"\b", "AKI", r"A[0-9A-Z]{16}\b"),
    CLASS_PROVIDER_CREDENTIAL: _fuse(
        r"\bs",
        r"k-[A-Za-z0-9_\-]{16,}\b",
        "|",
        r"\bp",
        r"a-[A-Za-z0-9_\-]{16,}\b",
    ),
    CLASS_QUOTED_ASSIGNMENT: _fuse(
        _FIELD_NAMES,
        r"\s*[:=]\s*[\"']",
        r"(?P<value>[^\"'\s]{12,})",
        r"[\"']",
    ),
    CLASS_ENVIRONMENT_ASSIGNMENT: _fuse(
        _VARIABLE_NAMES,
        r"\s*=\s*",
        r"(?P<value>[^\s\"'#]{12,})",
    ),
}

PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    name: re.compile(source) for name, source in PATTERN_SOURCES.items()
}

# The four reasons a match is set aside, each a statement about the matched value.
INTERPOLATION_MARKERS: Final[tuple[str, ...]] = (
    "{",
    "}",
    "$(",
    "${",
    "%s",
    "%(",
    "<",
    ">",
    "(",
    ")",
)
SELF_DESCRIBING: Final[re.Pattern[str]] = re.compile(
    _fuse(
        "(?i:exam",
        "ple|place",
        "holder|synth",
        "etic|redac",
        "ted|sam",
        "ple|molt_cred",
        "ential)",
    )
)
PATH_SHAPED: Final[re.Pattern[str]] = re.compile(r"^[/.~]")
SEPARATED_WORDS: Final[re.Pattern[str]] = re.compile(r"[a-z]+(?:[-_][a-z0-9]+)+")


# ---------------------------------------------------------------------------
# Synthetic specimens, fused at run time for the same reason
# ---------------------------------------------------------------------------

# Character runs the specimens are built from. Each is short enough and plain
# enough to match no pattern on its own, and the runs carry no word a reader could
# mistake for a real value.
_RUN_UPPER: Final[str] = _fuse("QZWRTPLKJ", "NHBGVFC")
_RUN_MIXED: Final[str] = _fuse("Qz9", "Wr7", "Pl4", "Kj2", "Nh6")
_RUN_LONG: Final[str] = _fuse(_RUN_MIXED, _RUN_MIXED)


def _specimen_private_key() -> str:
    """A specimen of the private key class."""
    return _fuse(
        "-----",
        "BEGIN ",
        "RSA ",
        "PRIV",
        "ATE KEY",
        "-----",
    )


def _specimen_connection_string() -> str:
    """A specimen of the connection string class, carrying an inline credential."""
    return _fuse(
        "postg",
        "resql://",
        _RUN_MIXED,
        ":",
        _RUN_LONG,
        "@",
        "host.invalid/molt",
    )


def _specimen_presented_bearer() -> str:
    """A specimen of the bearer value class."""
    return _fuse("Bear", "er ", _RUN_LONG)


def _specimen_cloud_access_key() -> str:
    """A specimen of the cloud access key identifier class."""
    return _fuse("AKI", "A", _RUN_UPPER)


def _specimen_provider_credential() -> str:
    """A specimen of the model provider credential class."""
    return _fuse("s", "k-", _RUN_LONG)


def _specimen_quoted_assignment() -> str:
    """A specimen of the quoted assignment class."""
    return _fuse("api", "_key", '="', _RUN_LONG, '"')


def _specimen_environment_assignment() -> str:
    """A specimen of the environment assignment class."""
    return _fuse("MOLT", "_INGRESS_", "SEC", "RET", "=", _RUN_LONG)


SPECIMENS: Final[tuple[tuple[str, str], ...]] = (
    (CLASS_PRIVATE_KEY, _specimen_private_key()),
    (CLASS_CONNECTION_STRING, _specimen_connection_string()),
    (CLASS_PRESENTED_BEARER, _specimen_presented_bearer()),
    (CLASS_CLOUD_ACCESS_KEY, _specimen_cloud_access_key()),
    (CLASS_PROVIDER_CREDENTIAL, _specimen_provider_credential()),
    (CLASS_QUOTED_ASSIGNMENT, _specimen_quoted_assignment()),
    (CLASS_ENVIRONMENT_ASSIGNMENT, _specimen_environment_assignment()),
)


# ---------------------------------------------------------------------------
# The tracked path set, built the way the gate builds its own
# ---------------------------------------------------------------------------


def ignore_rules(root: Path) -> Sequence[object]:
    """The tree's parsed ignore rules, from the gate's own parser."""
    rules: Sequence[object] = GATE.load_ignore_rules(root)
    return rules


def prunes_directory(rules: Sequence[object], root: Path, directory: Path) -> bool:
    """Whether the gate would descend into a directory, by the gate's own rule."""
    return bool(GATE._prunes_directory(rules, root, directory))


def is_ignored(rules: Sequence[object], relative: str, name: str) -> bool:
    """Whether the gate's ignore rules exclude one file."""
    return bool(GATE.is_ignored(rules, relative, name, False))


def tracked_paths(root: Path = REPOSITORY_ROOT) -> tuple[Path, ...]:
    """Every tracked file of the tree, by walk plus the gate's own ignore rules.

    Wider than the gate's own list in exactly one respect: every file is kept
    whatever its suffix and whatever its name, because a credential is not confined
    to the suffixes documentation and source use.
    """
    rules = ignore_rules(root)
    kept: list[Path] = []
    for directory, subdirectories, file_names in os.walk(root):
        current = Path(directory)
        subdirectories[:] = sorted(
            name for name in subdirectories if not prunes_directory(rules, root, current / name)
        )
        for name in sorted(file_names):
            candidate = current / name
            if candidate.is_symlink():
                continue
            relative = candidate.relative_to(root).as_posix()
            if is_ignored(rules, relative, name):
                continue
            kept.append(candidate)
    return tuple(kept)


def readable_text(path: Path) -> str | None:
    """One file's text, or None when it is binary or could not be read."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:BINARY_PROBE_BYTES]:
        return None
    return raw.decode(TEXT_ENCODING, errors="replace")


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """One occurrence of a credential shape in a tracked file."""

    path: str
    line: int
    class_name: str
    span: str

    def render(self) -> str:
        """The finding as one report line."""
        return f"{self.path}:{self.line}:{self.class_name}:{self.span}"


def set_aside(value: str) -> str | None:
    """Why a matched value is not a credential, or None when it is a finding."""
    if any(marker in value for marker in INTERPOLATION_MARKERS):
        return "the value is an interpolation or a call rather than a literal"
    if SELF_DESCRIBING.search(value):
        return "the value names itself as an example or a placeholder"
    if PATH_SHAPED.match(value):
        return "the value is a filesystem or parameter path"
    if SEPARATED_WORDS.fullmatch(value):
        return "the value is a lowercase separated phrase, which no credential is"
    return None


def matched_value(match: re.Match[str]) -> str:
    """The value a match is judged on: its value group, or the whole match."""
    captured = match.groupdict().get("value")
    return captured if captured else match.group(0)


def findings_in(relative: str, text: str) -> Iterator[Finding]:
    """Every credential shape one file's text carries."""
    for number, line in enumerate(text.splitlines(), start=1):
        for class_name in CLASS_ORDER:
            for match in PATTERNS[class_name].finditer(line):
                if set_aside(matched_value(match)) is None:
                    yield Finding(relative, number, class_name, match.group(0))


def scan(paths: Sequence[Path], root: Path = REPOSITORY_ROOT) -> tuple[Finding, ...]:
    """Scan every readable path and return the findings in path order."""
    found: list[Finding] = []
    for path in paths:
        text = readable_text(path)
        if text is None:
            continue
        found.extend(findings_in(path.relative_to(root).as_posix(), text))
    return tuple(found)


def report(found: Sequence[Finding]) -> str:
    """One line per finding, followed by the total."""
    lines = [item.render() for item in found]
    lines.append(f"total: {len(found)} finding(s)")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def paths() -> tuple[Path, ...]:
    """The tracked path set, walked once for the whole module."""
    return tracked_paths()


# ---------------------------------------------------------------------------
# The path set
# ---------------------------------------------------------------------------


def test_the_path_set_covers_everything_the_hygiene_gate_covers(
    paths: tuple[Path, ...],
) -> None:
    """The gate's own list is a subset of this one, so nothing it reads is missed."""
    gated = {Path(path) for path in GATE.collect_paths(REPOSITORY_ROOT, frozenset())}
    assert gated
    assert gated <= set(paths)


def test_the_path_set_reaches_every_kind_of_tracked_file(paths: tuple[Path, ...]) -> None:
    """Source, documentation, migrations, skills, workflow, and infrastructure."""
    relative = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths}
    for expected in (
        "scripts/hygiene.py",
        "docs/glossary.md",
        ".github/workflows/ci.yml",
        "pyproject.toml",
    ):
        assert expected in relative
    for prefix in ("src/molt/store/migrations/", "skills/", "infra/", "web/"):
        assert any(name.startswith(prefix) for name in relative), prefix


def test_the_ignored_and_pruned_paths_are_outside_the_set(paths: tuple[Path, ...]) -> None:
    """Reading material and ignored trees are excluded before a file is opened."""
    relative = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths}
    pruned = tuple(str(name) for name in GATE.PRUNED_DIRECTORY_NAMES)
    for name in relative:
        head = name.split("/", 1)[0]
        assert head not in pruned
        assert head != ".hypothesis"


def test_this_module_is_inside_the_scan_it_drives(paths: tuple[Path, ...]) -> None:
    """Nothing is excluded from this scan, including the file that defines it.

    This is what makes the empty result below a claim about this module too, rather
    than a claim that skipped the one file holding every pattern.
    """
    assert Path(__file__).resolve() in {path.resolve() for path in paths}


# ---------------------------------------------------------------------------
# The patterns recognise something, and do not recognise themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("class_name", "specimen"),
    SPECIMENS,
    ids=[case[0] for case in SPECIMENS],
)
def test_each_pattern_recognises_its_specimen(class_name: str, specimen: str) -> None:
    """A pattern that recognised nothing would pass the scan by finding nothing."""
    found = tuple(findings_in("specimen", specimen))
    assert found, f"the {class_name} pattern recognised no specimen of its own class"
    assert class_name in {item.class_name for item in found}


def test_the_specimens_cover_every_class() -> None:
    """Every class carries a specimen, so no class is left unexercised."""
    assert {case[0] for case in SPECIMENS} == set(CLASS_ORDER)


def test_no_pattern_matches_the_pattern_sources() -> None:
    """The fusion discipline holds: no pattern recognises the patterns themselves."""
    for class_name, source in PATTERN_SOURCES.items():
        found = tuple(findings_in(f"pattern:{class_name}", source))
        assert not found, report(found)


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


def test_no_tracked_file_carries_a_credential_shape(paths: tuple[Path, ...]) -> None:
    """The whole obligation of Requirements 30.1 and 30.12, over the whole tree."""
    found = scan(paths)
    assert not found, report(found)
