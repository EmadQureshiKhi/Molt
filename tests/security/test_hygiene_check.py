"""Self-test for the metadata-hygiene gate.

The gate is what makes the originality claim checkable, so the gate itself has
to be checked. Every assertion below drives the gate against a tree built under
the test's own temporary directory, with temporary list files, so the scan under
test never depends on the state of the repository and never mutates it.

One tension shapes this module. The fixtures must contain the very patterns the
gate forbids, yet this module is itself a tracked file the gate scans. So no
prohibited span is written as a literal here: each one is fused at run time from
fragments that carry no match on their own, the same device the gate's own
pattern module uses on its keywords. The final test in the file scans a copy of
this module and asserts the gate passes it, which is what keeps that discipline
honest rather than assumed.

The claim that no path under the reading-material directory is ever opened is
established by instrumentation rather than by assertion: an audit hook over the
filesystem access events records every path the scan touches, and the recording
is then checked for anything naming that directory while the directory is
confirmed present in the tree.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
GATE_SOURCE: Final[Path] = REPOSITORY_ROOT / "scripts" / "hygiene.py"
SHIPPED_DENYLIST: Final[Path] = REPOSITORY_ROOT / "scripts" / "hygiene_denylist.txt"
SHIPPED_ALLOWLIST: Final[Path] = REPOSITORY_ROOT / "scripts" / "hygiene_allowlist.txt"


def _load_gate() -> ModuleType:
    """Load the gate from its script path, since scripts form no import package."""
    specification = importlib.util.spec_from_file_location("molt_hygiene_under_test", GATE_SOURCE)
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


GATE: Final[ModuleType] = _load_gate()

SPAN_LIMIT: Final[int] = int(GATE.SPAN_LIMIT)
CLASS_ORDER: Final[tuple[str, ...]] = tuple(str(name) for name in GATE.CLASS_ORDER)
LICENCE_NAME: Final[str] = str(GATE.LICENCE_FILE_NAME)
ALLOWLIST_SECTIONS: Final[tuple[str, ...]] = tuple(sorted(str(s) for s in GATE.ALLOWLIST_SECTIONS))

# Synthetic tokens for the two token-driven classes. They name nobody and
# nothing, they appear in no shipped list file, and they are matched only by the
# temporary denylist a test writes, so this module stays clean under the gate.
DENIED_PERSON: Final[str] = "qqzpersontoken"
DENIED_PROJECT: Final[str] = "qqzprojecttoken"

CLEAN_LINE: Final[str] = "this file states behaviour and intent only"


# ---------------------------------------------------------------------------
# Prohibited spans, fused at run time from fragments that match nothing alone
# ---------------------------------------------------------------------------


def _fuse(*parts: str) -> str:
    """Join fragments into one span, so no forbidden literal appears in source."""
    return "".join(parts)


_MAIL_DOMAIN: Final[tuple[str, ...]] = ("example", ".", "invalid")
_DATE_PARTS: Final[tuple[str, str, str]] = ("1999", "07", "04")
_CLOCK_PARTS: Final[tuple[str, str, str]] = ("23", "07", "09")


def _address(local: str = "route") -> str:
    """A span of the electronic-address class."""
    return _fuse(local, "@", *_MAIL_DOMAIN)


def _calendar() -> str:
    """A span of the calendar class."""
    return "-".join(_DATE_PARTS)


def _clock() -> str:
    """A span of the wall-reading class."""
    return ":".join(_CLOCK_PARTS)


def _stamp() -> str:
    """A span of the combined instant class."""
    return _fuse(_calendar(), "T", _clock(), "Z")


def _release_heading(gap: str = " ") -> str:
    """A heading of the release-history class, with a caller-chosen gap."""
    return _fuse("## ", "version", gap, _fuse("histor", "y"))


def _attribution() -> str:
    """A line of the ownership-attribution class, as a licence text carries it."""
    marker = _fuse("(", "c", ")")
    return " ".join((_fuse("copyrigh", "t"), marker, _DATE_PARTS[0], "the holder"))


CLASS_CASES: Final[tuple[tuple[str, str, str], ...]] = (
    (str(GATE.CLASS_EMAIL), _fuse("reach the operator at ", _address()), _address()),
    (str(GATE.CLASS_DATE), _fuse("the recorded value is ", _calendar()), _calendar()),
    (str(GATE.CLASS_TIME), _fuse("the recorded value is ", _clock()), _clock()),
    (str(GATE.CLASS_TIMESTAMP), _fuse("the recorded value is ", _stamp()), _stamp()),
    (str(GATE.CLASS_VERSION), _release_heading(), _release_heading()),
    (str(GATE.CLASS_ATTRIBUTION), _attribution(), _fuse("copyrigh", "t")),
    (str(GATE.CLASS_PERSONAL), _fuse("a note about ", DENIED_PERSON), DENIED_PERSON),
    (str(GATE.CLASS_PROJECT), _fuse("a note about ", DENIED_PROJECT), DENIED_PROJECT),
)


# ---------------------------------------------------------------------------
# Temporary trees, temporary list files, and one call into the gate
# ---------------------------------------------------------------------------


def _make_tree(root: Path, files: Mapping[str, str]) -> Path:
    """Materialise a scan target under the test's own directory."""
    tree = root / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tree


def _denylist_text(persons: Sequence[str], projects: Sequence[str]) -> str:
    """Render a well-formed denylist over the two token-driven sections."""
    lines = ["# temporary denylist", "", "[personal-name]", *persons]
    lines.extend(("", "[reference-project]", *projects, ""))
    return "\n".join(lines)


def _allowlist_text(terms: Mapping[str, Sequence[str]]) -> str:
    """Render a well-formed allowlist over every permitted section."""
    lines = ["# temporary allowlist"]
    for section in ALLOWLIST_SECTIONS:
        lines.extend(("", _fuse("[", section, "]"), *terms.get(section, ())))
    lines.append("")
    return "\n".join(lines)


def _write_lists(
    root: Path,
    *,
    persons: Sequence[str] = (DENIED_PERSON,),
    projects: Sequence[str] = (DENIED_PROJECT,),
    allowed: Mapping[str, Sequence[str]] | None = None,
) -> tuple[Path, Path]:
    """Write a denylist and an allowlist outside the tree, returning both paths."""
    holder = root / "lists"
    holder.mkdir(parents=True, exist_ok=True)
    denylist = holder / "deny.txt"
    allowlist = holder / "allow.txt"
    denylist.write_text(_denylist_text(persons, projects), encoding="utf-8")
    allowlist.write_text(_allowlist_text(allowed or {}), encoding="utf-8")
    return denylist, allowlist


def _run(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    denylist: Path,
    allowlist: Path,
    as_json: bool = False,
) -> tuple[int, str, str]:
    """Run the gate over one tree and return its status and both streams."""
    argv = [str(root), "--denylist", str(denylist), "--allowlist", str(allowlist)]
    if as_json:
        argv.append("--json")
    status = int(GATE.main(argv))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _findings(output: str) -> tuple[tuple[str, int, str, str], ...]:
    """Parse the finding report into path, line, class, and span tuples."""
    parsed: list[tuple[str, int, str, str]] = []
    for line in output.splitlines():
        if line.startswith("total:"):
            continue
        path, number, class_name, span = line.split(":", 3)
        parsed.append((path, int(number), class_name, span))
    return tuple(parsed)


def _spans_for(output: str, class_name: str) -> tuple[str, ...]:
    """The spans reported for one pattern class."""
    return tuple(finding[3] for finding in _findings(output) if finding[2] == class_name)


def _counts(output: str) -> dict[str, int]:
    """Parse the per-class scanned-file counts of a clean report."""
    counts: dict[str, int] = {}
    for line in output.splitlines():
        name, _, tail = line.partition(":")
        if name in CLASS_ORDER:
            counts[name] = int(tail.split()[0])
    return counts


# ---------------------------------------------------------------------------
# Filesystem access instrumentation
#
# The reading-material claim is a claim about what the scanner does, not about
# what it reports, so it is established by observing the interpreter's own
# filesystem access events rather than by reading the scan output. The hook is
# installed once, on first use, and records nothing unless a recording is
# active, so its presence costs the rest of the session a single comparison per
# event.
# ---------------------------------------------------------------------------

WATCHED_EVENTS: Final[frozenset[str]] = frozenset(
    {"open", "os.listdir", "os.scandir", "os.stat", "os.lstat"}
)

_RECORDS: Final[list[tuple[str, str]]] = []
_ACTIVE: Final[list[str]] = []
_INSTALLED: Final[list[bool]] = []


def _as_text(item: object) -> str | None:
    """Render a filesystem argument as a path string, or report it is not one."""
    if isinstance(item, str):
        return item
    if isinstance(item, bytes):
        return item.decode("utf-8", "replace")
    if isinstance(item, os.PathLike):
        return str(os.fspath(item))
    return None


def _audit(event: str, arguments: tuple[object, ...]) -> None:
    """Record every watched filesystem access whose path lies inside the tree."""
    if not _ACTIVE or event not in WATCHED_EVENTS:
        return
    prefix = _ACTIVE[0]
    for item in arguments:
        text = _as_text(item)
        if text is not None and text.startswith(prefix):
            _RECORDS.append((event, text))


@contextlib.contextmanager
def _recording(prefix: str) -> Iterator[list[tuple[str, str]]]:
    """Record filesystem access under one prefix for the duration of the block."""
    if not _INSTALLED:
        sys.addaudithook(_audit)
        _INSTALLED.append(True)
    _RECORDS.clear()
    _ACTIVE.append(prefix)
    try:
        yield _RECORDS
    finally:
        _ACTIVE.clear()


def _under(text: str, directory: Path) -> bool:
    """Whether a recorded path names a directory or anything beneath it."""
    prefix = str(directory)
    return text == prefix or text.startswith(_fuse(prefix, os.sep))


def _opened(records: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """The paths a recording shows were opened for reading."""
    return tuple(path for event, path in records if event == "open")


# ---------------------------------------------------------------------------
# A clean scan
# ---------------------------------------------------------------------------


def test_clean_tree_exits_zero_with_per_class_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tree holding nothing prohibited passes and reports coverage per class."""
    tree = _make_tree(tmp_path, {"notes.md": CLEAN_LINE, LICENCE_NAME: CLEAN_LINE})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 0
    counts = _counts(out)
    assert set(counts) == set(CLASS_ORDER)
    # The licence file is scanned for the token-driven classes only, so it
    # raises those two counts and not the six shape-recognised ones.
    assert counts[str(GATE.CLASS_EMAIL)] == 1
    assert counts[str(GATE.CLASS_PERSONAL)] == 2
    assert counts[str(GATE.CLASS_PROJECT)] == 2
    assert "total: 2 files scanned" in out


# ---------------------------------------------------------------------------
# One test per prohibited pattern class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("class_name", "content", "span"),
    CLASS_CASES,
    ids=[case[0] for case in CLASS_CASES],
)
def test_each_prohibited_class_is_named_with_its_span(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    class_name: str,
    content: str,
    span: str,
) -> None:
    """Each class exits 1 and names itself, with the matched span reported."""
    tree = _make_tree(tmp_path, {"notes.md": content})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    assert span in _spans_for(out, class_name)
    assert _fuse("notes.md:1:", class_name, ":", span) in out.splitlines()


def test_the_cases_cover_every_pattern_class() -> None:
    """The parametrisation names all eight classes, so none is left unchecked."""
    assert {case[0] for case in CLASS_CASES} == set(CLASS_ORDER)


def test_finding_report_carries_a_total(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The report ends with the finding total over the files scanned."""
    tree = _make_tree(tmp_path, {"one.md": _address(), "two.md": _calendar()})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    assert out.splitlines()[-1] == "total: 2 findings in 2 files of 2 scanned"


# ---------------------------------------------------------------------------
# Span rendering
# ---------------------------------------------------------------------------


def test_span_at_the_limit_is_kept_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A span of exactly the limit is reported unabbreviated."""
    suffix_length = len(_address(""))
    address = _address("a" * (SPAN_LIMIT - suffix_length))
    assert len(address) == SPAN_LIMIT
    tree = _make_tree(tmp_path, {"notes.md": address})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    assert _spans_for(out, str(GATE.CLASS_EMAIL)) == (address,)


def test_span_beyond_the_limit_is_cut_at_the_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A longer span is cut at the limit exactly, keeping its leading characters."""
    suffix_length = len(_address(""))
    address = _address("a" * (SPAN_LIMIT - suffix_length + 1))
    assert len(address) == SPAN_LIMIT + 1
    tree = _make_tree(tmp_path, {"notes.md": address})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    reported = _spans_for(out, str(GATE.CLASS_EMAIL))
    assert reported == (address[:SPAN_LIMIT],)
    assert len(reported[0]) == SPAN_LIMIT


def test_span_whitespace_is_collapsed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Interior whitespace inside a span is collapsed to single spaces."""
    tree = _make_tree(tmp_path, {"notes.md": _release_heading("\t")})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    assert _spans_for(out, str(GATE.CLASS_VERSION)) == (_release_heading(),)


# ---------------------------------------------------------------------------
# Permitted terms
# ---------------------------------------------------------------------------


def test_allowlisted_span_is_not_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A denied token falling wholly inside a permitted term is no finding."""
    term = " ".join((DENIED_PERSON, "platform"))
    tree = _make_tree(tmp_path, {"notes.md": _fuse("built on the ", term)})
    denylist, allowlist = _write_lists(tmp_path, allowed={ALLOWLIST_SECTIONS[0]: (term,)})

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 0
    assert "no findings" in out


def test_the_same_token_outside_a_permitted_term_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The permission is span-bounded rather than token-wide."""
    term = " ".join((DENIED_PERSON, "platform"))
    tree = _make_tree(tmp_path, {"notes.md": _fuse("built on ", DENIED_PERSON)})
    denylist, allowlist = _write_lists(tmp_path, allowed={ALLOWLIST_SECTIONS[0]: (term,)})

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    assert _spans_for(out, str(GATE.CLASS_PERSONAL)) == (DENIED_PERSON,)


def test_the_obliged_platform_names_pass_the_shipped_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file holding only the names the documentation must state passes.

    The terms are read from the shipped allowlist rather than restated here, so
    the assertion covers the names the gate actually permits and this module
    names no vendor of its own.
    """
    grouped: dict[str, list[str]] = GATE._read_list_file(SHIPPED_ALLOWLIST, GATE.ALLOWLIST_SECTIONS)
    assert set(grouped) == set(ALLOWLIST_SECTIONS)
    for section in ALLOWLIST_SECTIONS:
        assert grouped[section], _fuse("the allowlist section ", section, " names nothing")

    lines = [CLEAN_LINE]
    for section in ALLOWLIST_SECTIONS:
        lines.extend(grouped[section])
    tree = _make_tree(tmp_path, {"platforms.md": "\n".join(lines)})

    status, out, _ = _run(tree, capsys, denylist=SHIPPED_DENYLIST, allowlist=SHIPPED_ALLOWLIST)

    assert status == 0
    assert "no findings" in out


# ---------------------------------------------------------------------------
# A malformed list file is never reported as a clean scan
# ---------------------------------------------------------------------------

MALFORMED_LISTS: Final[tuple[tuple[str, str | None, str], ...]] = (
    ("unknown_section", "\n".join(("[not-a-section]", DENIED_PERSON)), "unknown section"),
    ("entry_before_section", "\n".join((DENIED_PERSON, "[personal-name]")), "precedes any section"),
    (
        "duplicate_entry",
        "\n".join(("[personal-name]", DENIED_PERSON, DENIED_PERSON)),
        "duplicate entry",
    ),
    (
        "control_character",
        "\n".join(("[personal-name]", _fuse(DENIED_PERSON, "\x01"))),
        "control character",
    ),
    ("absent_file", None, "absent"),
)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(
    ("content", "reason"),
    [(case[1], case[2]) for case in MALFORMED_LISTS],
    ids=[case[0] for case in MALFORMED_LISTS],
)
def test_malformed_denylist_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
    reason: str,
    as_json: bool,
) -> None:
    """Every malformed condition exits 2, in both the text and the object form."""
    tree = _make_tree(tmp_path, {"notes.md": CLEAN_LINE})
    _, allowlist = _write_lists(tmp_path)
    denylist = tmp_path / "lists" / "broken.txt"
    if content is not None:
        denylist.write_text(content, encoding="utf-8")

    status, out, err = _run(tree, capsys, denylist=denylist, allowlist=allowlist, as_json=as_json)

    assert status == 2
    if as_json:
        payload = json.loads(out)
        assert payload["status"] == "malformed_list_file"
        assert payload["exit_code"] == 2
        assert payload["list_file"] == str(denylist)
        assert reason in payload["reason"]
    else:
        assert "malformed list file" in err
        assert reason in err
        assert out == ""


def test_malformed_allowlist_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The permitted-term file is held to the same format as the denied-token file."""
    tree = _make_tree(tmp_path, {"notes.md": CLEAN_LINE})
    denylist, _ = _write_lists(tmp_path)
    allowlist = tmp_path / "lists" / "broken_allow.txt"
    allowlist.write_text("\n".join(("[not-a-section]", "term")), encoding="utf-8")

    status, _, err = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 2
    assert str(allowlist) in err


# ---------------------------------------------------------------------------
# The machine-readable form
# ---------------------------------------------------------------------------


def test_json_form_on_a_clean_scan_is_one_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean scan emits a single object carrying the per-class counts."""
    tree = _make_tree(tmp_path, {"notes.md": CLEAN_LINE})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist, as_json=True)

    assert status == 0
    assert len(out.strip().splitlines()) == 1
    payload = json.loads(out)
    assert payload["status"] == "clean"
    assert payload["exit_code"] == 0
    assert payload["total_findings"] == 0
    assert payload["findings"] == []
    assert payload["files_scanned"] == 1
    assert set(payload["scanned_file_counts"]) == set(CLASS_ORDER)


def test_json_form_on_findings_is_one_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scan with findings emits a single object naming each class and span."""
    tree = _make_tree(tmp_path, {"notes.md": _address()})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist, as_json=True)

    assert status == 1
    assert len(out.strip().splitlines()) == 1
    payload = json.loads(out)
    assert payload["status"] == "findings"
    assert payload["exit_code"] == 1
    assert payload["total_findings"] == 1
    assert payload["findings"] == [
        {"class": str(GATE.CLASS_EMAIL), "line": 1, "path": "notes.md", "span": _address()}
    ]


# ---------------------------------------------------------------------------
# The licence file
# ---------------------------------------------------------------------------


def test_licence_file_passes_with_its_attribution_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The licence text may carry the ownership line its terms require."""
    tree = _make_tree(tmp_path, {LICENCE_NAME: "\n".join((_attribution(), CLEAN_LINE))})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 0
    assert "no findings" in out


def test_the_same_line_in_another_file_is_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The permission is the licence file's alone, not the line's."""
    tree = _make_tree(tmp_path, {"notes.md": _attribution()})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    assert _fuse("copyrigh", "t") in _spans_for(out, str(GATE.CLASS_ATTRIBUTION))


def test_licence_file_is_still_scanned_for_the_denied_tokens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The licence exemption covers the shape classes and not the token classes."""
    tree = _make_tree(tmp_path, {LICENCE_NAME: _fuse("granted to ", DENIED_PERSON)})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    assert _spans_for(out, str(GATE.CLASS_PERSONAL)) == (DENIED_PERSON,)


# ---------------------------------------------------------------------------
# The reading-material directory, established by instrumentation
# ---------------------------------------------------------------------------


def test_no_path_under_the_reference_directory_is_ever_touched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scan opens nothing beneath either spelling of the ignored directory.

    Both the plain and the hidden spelling hold a file carrying a prohibited
    pattern, so a scan that descended into either would exit 1. The recording is
    the stronger evidence: it shows the scanner never reached them at all.
    """
    plain = _fuse("refer", "ence")
    hidden = _fuse(".", plain)
    tree = _make_tree(
        tmp_path,
        {
            "notes.md": CLEAN_LINE,
            _fuse(plain, "/material.md"): _address(),
            _fuse(hidden, "/material.md"): _attribution(),
            ".gitignore": "\n".join((_fuse(plain, "/"), "")),
        },
    )
    denylist, allowlist = _write_lists(tmp_path)

    with _recording(str(tree)) as records:
        status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 0
    assert "no findings" in out
    # The directories were present while the scan ran, so the absence of any
    # access to them is a property of the scanner rather than of the fixture.
    assert (tree / plain / "material.md").is_file()
    assert (tree / hidden / "material.md").is_file()
    assert records
    assert str(tree / "notes.md") in _opened(records)
    for event, path in records:
        assert not _under(path, tree / plain), _fuse(event, " touched ", path)
        assert not _under(path, tree / hidden), _fuse(event, " touched ", path)


def test_an_ignored_file_is_never_opened(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ignore rules are applied before any file is opened."""
    tree = _make_tree(
        tmp_path,
        {
            "notes.md": CLEAN_LINE,
            "excluded.md": _stamp(),
            ".gitignore": "\n".join(("excluded.md", "")),
        },
    )
    denylist, allowlist = _write_lists(tmp_path)

    with _recording(str(tree)) as records:
        status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 0
    assert "total: 1 files scanned" in out
    assert str(tree / "excluded.md") not in _opened(records)
    assert str(tree / "notes.md") in _opened(records)


# ---------------------------------------------------------------------------
# Scan membership
# ---------------------------------------------------------------------------


def test_the_tracked_definition_directory_is_scanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow directory is hidden yet tracked, so the gate covers it."""
    tree = _make_tree(tmp_path, {".github/workflows/checks.yml": _address()})
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 1
    assert _findings(out)[0][0] == ".github/workflows/checks.yml"


def test_other_hidden_paths_and_unscanned_suffixes_are_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hidden material and unrecognised extensions are outside the scan."""
    tree = _make_tree(
        tmp_path,
        {
            ".local/material.md": _address(),
            ".hidden.md": _calendar(),
            "notes.txt": _stamp(),
            "notes.md": CLEAN_LINE,
        },
    )
    denylist, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=denylist, allowlist=allowlist)

    assert status == 0
    assert "total: 1 files scanned" in out


def test_the_denylist_is_excluded_from_the_scan_it_drives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A denylist inside the tree is not reported for holding its own tokens."""
    listing = _denylist_text((DENIED_PERSON,), (DENIED_PROJECT,))
    tree = _make_tree(tmp_path, {"deny.md": listing, "notes.md": CLEAN_LINE})
    _, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=tree / "deny.md", allowlist=allowlist)

    assert status == 0
    assert "total: 1 files scanned" in out


def test_the_same_listing_in_another_file_is_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exclusion is that one path's alone, not the content's."""
    listing = _denylist_text((DENIED_PERSON,), (DENIED_PROJECT,))
    tree = _make_tree(tmp_path, {"deny.md": listing, "copy.md": listing})
    _, allowlist = _write_lists(tmp_path)

    status, out, _ = _run(tree, capsys, denylist=tree / "deny.md", allowlist=allowlist)

    assert status == 1
    assert {finding[0] for finding in _findings(out)} == {"copy.md"}
    assert _spans_for(out, str(GATE.CLASS_PERSONAL)) == (DENIED_PERSON,)
    assert _spans_for(out, str(GATE.CLASS_PROJECT)) == (DENIED_PROJECT,)


# ---------------------------------------------------------------------------
# This module, and the repository, under the shipped lists
# ---------------------------------------------------------------------------


def test_this_module_passes_the_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The fixtures above are fused at run time, so this file carries no span."""
    tree = _make_tree(tmp_path, {Path(__file__).name: Path(__file__).read_text(encoding="utf-8")})

    status, out, _ = _run(
        tree, capsys, denylist=SHIPPED_DENYLIST, allowlist=SHIPPED_ALLOWLIST, as_json=True
    )

    assert status == 0
    assert json.loads(out)["files_scanned"] == 1


def test_the_repository_scan_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """The gate passes over the whole tree with this module present."""
    status = int(GATE.main([str(REPOSITORY_ROOT)]))
    out = capsys.readouterr().out

    assert status == 0, out
    assert "no findings" in out


# ---------------------------------------------------------------------------
# The whole repository, and what the whole-repository scan covered
# ---------------------------------------------------------------------------

# The tracked definitions the completed tree holds, by the prefix each lives under.
# A scan that exits zero over a tree it never descended into would be a clean scan of
# nothing, so the coverage is asserted beside the status rather than inferred from it.
TRACKED_DEFINITION_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("documentation", "docs/"),
    ("agent skill definition", "skills/"),
    ("schema migration", "src/molt/store/migrations/"),
    ("workflow definition", ".github/workflows/"),
    ("infrastructure template", "infra/"),
    ("repository script", "scripts/"),
    ("source package", "src/molt/"),
    ("test suite", "tests/"),
)

# One file per category whose presence in the scan list is checked by name, so a
# category cannot be satisfied by an unrelated file that happens to share a prefix.
NAMED_DEFINITIONS: Final[tuple[str, ...]] = (
    "docs/glossary.md",
    "docs/threat-model.md",
    ".github/workflows/ci.yml",
    "README.md",
)

# The suffixes those categories are written in, each of which the gate must scan.
DEFINITION_SUFFIXES: Final[tuple[str, ...]] = (".md", ".py", ".sql", ".yml", ".yaml", ".sh")


def _repository_scan_list() -> frozenset[str]:
    """The paths the gate's own walk retains over the whole tree, relative to it.

    Built by calling the gate rather than by walking again, so what is asserted below
    is the membership of the scan that actually ran, with the denylist excluded from
    the scan it drives exactly as the gate excludes it.
    """
    excluded = frozenset({SHIPPED_DENYLIST.resolve(), SHIPPED_DENYLIST})
    return frozenset(
        Path(path).relative_to(REPOSITORY_ROOT).as_posix()
        for path in GATE.collect_paths(REPOSITORY_ROOT, excluded)
    )


def test_the_repository_scan_reaches_every_tracked_definition() -> None:
    """Every kind of tracked definition the completed tree holds is in the scan list."""
    scanned = _repository_scan_list()

    for description, prefix in TRACKED_DEFINITION_PREFIXES:
        assert any(name.startswith(prefix) for name in scanned), _fuse(
            "the scan list holds no ", description, " under ", prefix
        )
    for name in NAMED_DEFINITIONS:
        assert name in scanned, _fuse("the scan list omits ", name)
    for suffix in DEFINITION_SUFFIXES:
        assert any(name.endswith(suffix) for name in scanned), _fuse(
            "the scan list holds no file written in ", suffix
        )
    assert SHIPPED_DENYLIST.relative_to(REPOSITORY_ROOT).as_posix() not in scanned


def test_the_whole_repository_scan_exits_zero_over_everything_it_covered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The gate passes over the whole tree, having read every file of the scan list.

    The object form is used so the file count is a number rather than a sentence, and
    the count is compared against the scan list, which is what makes *exits zero* a
    statement about the whole tree and not about whatever subset was walked.
    """
    scanned = _repository_scan_list()

    status = int(GATE.main([str(REPOSITORY_ROOT), "--json"]))
    out = capsys.readouterr().out

    assert status == 0, out
    payload = json.loads(out)
    assert payload["status"] == "clean"
    assert payload["exit_code"] == 0
    assert payload["total_findings"] == 0
    assert payload["findings"] == []
    assert payload["files_scanned"] == len(scanned)
    # The licence file is scanned for the two token-driven classes alone, so the six
    # shape-recognised counts may sit one below the total; every class must have been
    # applied to something.
    counts = payload["scanned_file_counts"]
    assert set(counts) == set(CLASS_ORDER)
    for class_name in CLASS_ORDER:
        assert counts[class_name] > 0
        assert counts[class_name] <= len(scanned)
