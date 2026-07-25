"""Property 39: the hygiene gate over generated trees, and bound values as data.

The property has two halves, and they are asserted through two different
harnesses because they are claims about two different things.

**The detection half is pure.** It drives the gate over trees this module builds
under a temporary directory, with list files it writes there too, so nothing it
asserts depends on the state of the repository and nothing it does mutates it.
That half needs no cluster and carries no instance marker, so it runs on the
credential-free path where the originality claim is actually checked.

**The binding half needs the cluster.** "Alters no schema or query semantics" is
a claim about what the cluster did with a value, not about what a statement
looked like, so it is asserted against a live instance and the test carrying it
is marked accordingly. The schema is compared by reading the catalog before the
write and again after it and requiring the two readings to be identical, rather
than by asserting that some particular object still exists: a reading taken both
sides of the write cannot miss a column, a constraint, an index, a check clause,
or a view that the write altered, and a hand-written list of objects can.

The two halves therefore sit in one module with the gate applied per test rather
than at module scope. A contributor holding no connection string still runs the
detection half in full; the binding half skips with a message naming what was
missing.

Two devices are worth stating.

**No prohibited span is written as a literal here.** This module is itself a
tracked file the gate scans, and its fixtures must contain the very patterns the
gate forbids. Every span is therefore composed at generation time from drawn
parts and from fragments that match nothing on their own, which is the same
device the gate's own pattern module uses on its keywords. The two token-driven
classes cannot be recognised by shape at all, so their tokens are generated and
installed into a temporary denylist rather than taken from the shipped one; a
generated token carries a prefix no shipped list holds, so an example's token
collides with nothing.

**The permitted-term half is asserted against the shipped lists.** The claim
worth checking is that the names the documentation is obliged to state pass, so
that content is composed of terms read from the shipped allowlist at run time
and is scanned with the shipped lists in place. Drawing the terms rather than
restating them means this module names no vendor of its own.

Every value the binding half sends reaches the cluster as a bound parameter of
the statements the store module owns; this module interpolates nothing into any
statement it sends either, including in its own fixtures.

**Validates: Requirements 29.4, 29.8, 30.6, 36.16**
"""

from __future__ import annotations

import importlib.util
import io
import json
import string
import sys
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from secrets import token_hex
from shutil import rmtree
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.event import JsonObject
from molt.models.session import Session, SessionOutcome
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations
from molt.store.sessions import session_of_client, upsert_session

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
GATE_SOURCE: Final[Path] = REPOSITORY_ROOT / "scripts" / "hygiene.py"
SHIPPED_DENYLIST: Final[Path] = REPOSITORY_ROOT / "scripts" / "hygiene_denylist.txt"
SHIPPED_ALLOWLIST: Final[Path] = REPOSITORY_ROOT / "scripts" / "hygiene_allowlist.txt"


def _load_gate() -> ModuleType:
    """Load the gate from its script path, since scripts form no import package."""
    specification = importlib.util.spec_from_file_location(
        "molt_hygiene_under_property", GATE_SOURCE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    # Registration precedes execution because the gate defines slotted
    # dataclasses, and that decorator resolves its own module by name.
    sys.modules[specification.name] = module
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
CLEAN_EXIT: Final[int] = 0
FINDING_EXIT: Final[int] = 1
MALFORMED_EXIT: Final[int] = 2

# The marker standing in for a pattern class when an example carries none, so the
# permitted-term case is drawn from the same pool as the eight prohibited ones.
ALLOWLISTED_ONLY: Final[str] = "allowlisted_only"

# Suffixes the gate scans. The licence name is deliberately absent: the shape
# classes are not asserted against a file the licence exemption applies to.
SCANNED_NAMES: Final[tuple[str, ...]] = (
    "notes.md",
    "module.py",
    "schema.sql",
    "settings.toml",
    "workflow.yml",
    "install.sh",
    "console.js",
    "console.css",
    "page.html",
)

LOWER: Final[str] = string.ascii_lowercase


# ---------------------------------------------------------------------------
# Fragments that match nothing alone, fused only at generation time
# ---------------------------------------------------------------------------


def _fuse(*parts: str) -> str:
    """Join fragments into one span, so no forbidden literal appears in source."""
    return "".join(parts)


def _collapsed(span: str) -> str:
    """Render a span the way the gate reports it: whitespace collapsed, then cut."""
    return " ".join(span.split())[:SPAN_LIMIT]


# Host and top-level labels an address is composed from. None of them resolves,
# and none of them names anybody.
HOST_LABELS: Final[tuple[str, ...]] = ("example", "test", "localdomain", "internal")
TOP_LABELS: Final[tuple[str, ...]] = ("invalid", "test", "example", "localhost")

# The release-history phrases, each fused from fragments. The gate recognises the
# phrase at the start of a line, optionally behind a heading or list marker.
HISTORY_PHRASES: Final[tuple[str, ...]] = (
    _fuse("chang", "elog"),
    _fuse("chang", "e log"),
    _fuse("releas", "e notes"),
    _fuse("revision", " histor", "y"),
    _fuse("version", " histor", "y"),
    _fuse("unrelea", "sed"),
)
HISTORY_MARKERS: Final[tuple[str, ...]] = ("", "# ", "## ", "- ", "* ", "1. ")
HEADING_MARKERS: Final[tuple[str, ...]] = ("# ", "## ", "### ")

# The ownership verbs the gate recognises ahead of the agency word, each fused.
ATTRIBUTION_VERBS: Final[tuple[str, ...]] = (
    _fuse("writt", "en"),
    _fuse("author", "ed"),
    _fuse("creat", "ed"),
    _fuse("contribut", "ed"),
    _fuse("maintain", "ed"),
)
ATTRIBUTION_ROLES: Final[tuple[str, ...]] = (_fuse("autho", "r"), _fuse("maintaine", "r"))
OWNERSHIP_WORD: Final[str] = _fuse("copyrigh", "t")
OWNERSHIP_MARKER: Final[str] = _fuse("(", "c", ")")

# The prefix every generated denied token carries. No shipped list holds it, so a
# token an example installs matches nothing outside that example's own tree.
DENIED_PREFIX: Final[str] = "zzq"

# Lines that carry nothing prohibited, used as the surrounding content of a
# fixture so a finding has to be located rather than merely present.
CLEAN_LINES: Final[tuple[str, ...]] = (
    "this file states behaviour and intent only",
    "the gate reads the tree and reports what it found",
    "nothing here names a person or a product",
    "the scan opens each retained file exactly once",
)

# Phrases a span is placed behind. None ends in a character that would extend a
# span backwards, and none carries a formatting marker that would put a span in
# the format context the gate excludes.
LEAD_PHRASES: Final[tuple[str, ...]] = (
    "the recorded value is",
    "reach the operator at",
    "a note about",
    "the entry reads",
)

# The permitted terms, read from the shipped allowlist at import so this module
# names no platform or vendor of its own.
_ALLOWED_GROUPS: dict[str, list[str]] = GATE._read_list_file(
    SHIPPED_ALLOWLIST, GATE.ALLOWLIST_SECTIONS
)
ALLOWED_TERMS: Final[tuple[str, ...]] = tuple(
    term for section in sorted(_ALLOWED_GROUPS) for term in _ALLOWED_GROUPS[section]
)


# ---------------------------------------------------------------------------
# What the hygiene generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HygieneFixture:
    """One generated tree, the lists to scan it with, and what is expected back.

    Attributes:
        file_name: The single scanned file the tree holds.
        lines: That file's content, one entry per line.
        expected_class: The pattern class the gate must name, or the
            permitted-term marker when the content carries nothing prohibited.
        expected_span: The span the gate must report, as it renders it.
        expected_line: Which line of the file the span sits on, counting from one.
        denied_person: The token installed in the personal-name section.
        denied_project: The token installed in the reading-material section.
        shipped_lists: Whether the scan runs against the shipped list files, which
            is what the permitted-term case needs and what the prohibited cases
            must not depend on.
    """

    file_name: str
    lines: tuple[str, ...]
    expected_class: str
    expected_span: str
    expected_line: int
    denied_person: str
    denied_project: str
    shipped_lists: bool

    @property
    def prohibited(self) -> bool:
        """Whether this example's content is expected to produce a finding."""
        return self.expected_class != ALLOWLISTED_ONLY


# ---------------------------------------------------------------------------
# One span builder per pattern class, every part of every span drawn
# ---------------------------------------------------------------------------


@st.composite
def _address_spans(draw: st.DrawFn) -> str:
    """A span of the electronic-address class."""
    local = draw(st.text(alphabet=LOWER, min_size=1, max_size=8))
    return _fuse(
        local, "@", draw(st.sampled_from(HOST_LABELS)), ".", draw(st.sampled_from(TOP_LABELS))
    )


@st.composite
def _calendar_spans(draw: st.DrawFn) -> str:
    """A span of the calendar class, in either separator the gate recognises."""
    separator = draw(st.sampled_from(("-", "/")))
    year = draw(st.integers(min_value=1000, max_value=9999))
    month = draw(st.integers(min_value=1, max_value=12))
    day = draw(st.integers(min_value=1, max_value=28))
    return f"{year:04d}{separator}{month:02d}{separator}{day:02d}"


@st.composite
def _clock_spans(draw: st.DrawFn) -> str:
    """A span of the wall-reading class, with the seconds field drawn."""
    hour = draw(st.integers(min_value=0, max_value=23))
    minute = draw(st.integers(min_value=0, max_value=59))
    reading = f"{hour:02d}:{minute:02d}"
    if draw(st.booleans()):
        reading = f"{reading}:{draw(st.integers(min_value=0, max_value=59)):02d}"
    return reading


@st.composite
def _instant_spans(draw: st.DrawFn) -> str:
    """A span of the combined instant class, offset drawn against the zone letter."""
    year = draw(st.integers(min_value=1000, max_value=9999))
    month = draw(st.integers(min_value=1, max_value=12))
    day = draw(st.integers(min_value=1, max_value=28))
    hour = draw(st.integers(min_value=0, max_value=23))
    minute = draw(st.integers(min_value=0, max_value=59))
    second = draw(st.integers(min_value=0, max_value=59))
    date = f"{year:04d}-{month:02d}-{day:02d}"
    reading = f"{hour:02d}:{minute:02d}:{second:02d}"
    # The zone is fused from drawn parts for the same reason every other span is:
    # a written-out offset would itself be a wall reading this file must not hold.
    if draw(st.booleans()):
        zone = draw(st.sampled_from(("Z", "z")))
    else:
        sign = draw(st.sampled_from(("+", "-")))
        offset_hour = draw(st.integers(min_value=0, max_value=14))
        offset_minute = draw(st.sampled_from((0, 30, 45)))
        separator = draw(st.sampled_from((":", "")))
        zone = _fuse(sign, f"{offset_hour:02d}", separator, f"{offset_minute:02d}")
    return _fuse(date, draw(st.sampled_from(("T", " "))), reading, zone)


@st.composite
def _history_spans(draw: st.DrawFn) -> str:
    """A span of the release-history class, either phrase form or version heading.

    The gap inside a marked phrase is drawn from a space and a tab, because the
    gate collapses interior whitespace before it reports a span and a fixture
    that only ever used a space would not reach that.
    """
    if draw(st.booleans()):
        marker = draw(st.sampled_from(HISTORY_MARKERS))
        gap = draw(st.sampled_from(("", "\t"))) if marker else ""
        return _fuse(marker, gap, draw(st.sampled_from(HISTORY_PHRASES)))
    major = draw(st.integers(min_value=0, max_value=99))
    minor = draw(st.integers(min_value=0, max_value=99))
    patch = draw(st.integers(min_value=0, max_value=99))
    lead = draw(st.sampled_from(("", "v", "[", "[v")))
    return _fuse(draw(st.sampled_from(HEADING_MARKERS)), lead, f"{major}.{minor}.{patch}")


@st.composite
def _ownership_spans(draw: st.DrawFn) -> str:
    """A span of the ownership-attribution class, in one of its three forms."""
    form = draw(st.integers(min_value=0, max_value=2))
    if form == 0:
        return OWNERSHIP_WORD
    if form == 1:
        year = draw(st.integers(min_value=1000, max_value=9999))
        return _fuse(OWNERSHIP_MARKER, draw(st.sampled_from((" ", "  ", ""))), f"{year:04d}")
    if draw(st.booleans()):
        return _fuse(draw(st.sampled_from(ATTRIBUTION_VERBS)), " b", "y")
    plural = "s" if draw(st.booleans()) else ""
    gap = draw(st.sampled_from(("", " ")))
    return _fuse(
        draw(st.sampled_from(ATTRIBUTION_ROLES)), plural, gap, draw(st.sampled_from((":", "=")))
    )


CLASS_EMAIL: Final[str] = str(GATE.CLASS_EMAIL)
CLASS_DATE: Final[str] = str(GATE.CLASS_DATE)
CLASS_TIME: Final[str] = str(GATE.CLASS_TIME)
CLASS_TIMESTAMP: Final[str] = str(GATE.CLASS_TIMESTAMP)
CLASS_VERSION: Final[str] = str(GATE.CLASS_VERSION)
CLASS_ATTRIBUTION: Final[str] = str(GATE.CLASS_ATTRIBUTION)
CLASS_PERSONAL: Final[str] = str(GATE.CLASS_PERSONAL)
CLASS_PROJECT: Final[str] = str(GATE.CLASS_PROJECT)


@st.composite
def _denied_tokens(draw: st.DrawFn) -> tuple[str, str]:
    """Two distinct tokens to install, one per token-driven class.

    The two carry different infixes rather than being drawn under a uniqueness
    filter, so no draw is ever rejected and the pair is distinct by construction.
    A denylist holding the same entry twice is malformed, and this is what keeps
    an example from generating one.
    """
    bodies = st.text(alphabet=LOWER, min_size=6, max_size=10)
    return (
        _fuse(DENIED_PREFIX, "a", draw(bodies)),
        _fuse(DENIED_PREFIX, "b", draw(bodies)),
    )


def _spans_of(class_name: str, person: str, project: str) -> st.SearchStrategy[tuple[str, bool]]:
    """The span strategy for one class, paired with whether it must start a line.

    Only the release-history class is recognised at the start of a line, so only
    that one is anchored; every other span is placed behind a lead phrase, which
    is what makes the reported column-independent location worth asserting.
    """
    if class_name == CLASS_EMAIL:
        return st.tuples(_address_spans(), st.just(False))
    if class_name == CLASS_DATE:
        return st.tuples(_calendar_spans(), st.just(False))
    if class_name == CLASS_TIME:
        return st.tuples(_clock_spans(), st.just(False))
    if class_name == CLASS_TIMESTAMP:
        return st.tuples(_instant_spans(), st.just(False))
    if class_name == CLASS_VERSION:
        return st.tuples(_history_spans(), st.just(True))
    if class_name == CLASS_ATTRIBUTION:
        return st.tuples(_ownership_spans(), st.just(False))
    if class_name == CLASS_PERSONAL:
        return st.tuples(st.just(person), st.just(False))
    if class_name == CLASS_PROJECT:
        return st.tuples(st.just(project), st.just(False))
    raise AssertionError(f"no span builder covers the pattern class {class_name}")


@st.composite
def hygiene_fixtures(draw: st.DrawFn, chosen: str) -> HygieneFixture:
    """Draw one scan target: content carrying one class, or permitted terms only.

    The class is a parameter rather than a draw. Drawing it would spend the
    example budget unevenly across the nine cases, and the eight prohibited
    classes are not interchangeable: each has its own span forms, and the
    release-history class alone is recognised at the start of a line. Passing it
    in gives every case the whole budget.

    The permitted-term case is scanned with the shipped list files in place and
    its content is composed of terms read from that allowlist at run time, which
    is the arrangement the documentation actually ships under.
    """
    person, project = draw(_denied_tokens())
    before = draw(st.lists(st.sampled_from(CLEAN_LINES), max_size=3))
    after = draw(st.lists(st.sampled_from(CLEAN_LINES), max_size=3))
    name = draw(st.sampled_from(SCANNED_NAMES))

    if chosen == ALLOWLISTED_ONLY:
        terms = draw(st.lists(st.sampled_from(ALLOWED_TERMS), min_size=1, max_size=6, unique=True))
        body = tuple(f"the deployment names {term}" for term in terms)
        return HygieneFixture(
            file_name=name,
            lines=(*before, *body, *after),
            expected_class=ALLOWLISTED_ONLY,
            expected_span="",
            expected_line=0,
            denied_person=person,
            denied_project=project,
            shipped_lists=True,
        )

    span, anchored = draw(_spans_of(chosen, person, project))
    lead = draw(st.sampled_from(LEAD_PHRASES))
    return HygieneFixture(
        file_name=name,
        lines=(*before, span if anchored else f"{lead} {span}", *after),
        expected_class=chosen,
        expected_span=_collapsed(span),
        expected_line=len(before) + 1,
        denied_person=person,
        denied_project=project,
        shipped_lists=False,
    )


# ---------------------------------------------------------------------------
# Materialising a tree and running the gate over it
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def scan_area(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory every example builds its own tree beneath.

    Session scope is deliberate: a function-scoped temporary directory would be
    shared across the examples of one property rather than created per example,
    and building a fresh subdirectory here is both correct and cheaper.
    """
    return tmp_path_factory.mktemp("molt_p39_hygiene")


def _denylist_text(person: str, project: str) -> str:
    """Render a well-formed denylist holding one token per token-driven class."""
    return "\n".join(("[personal-name]", person, "", "[reference-project]", project, ""))


def _allowlist_text() -> str:
    """Render a well-formed allowlist holding every section and no term.

    The prohibited cases run against this rather than against the shipped
    permitted terms, so a finding they assert cannot be suppressed by a term
    somebody adds to the shipped file later.
    """
    sections = sorted(str(name) for name in GATE.ALLOWLIST_SECTIONS)
    return "\n".join([*(_fuse("[", section, "]") for section in sections), ""])


def _materialise(root: Path, fixture: HygieneFixture) -> tuple[Path, Path, Path]:
    """Write one example's tree and its list files, returning the three paths."""
    tree = root / "tree"
    tree.mkdir(parents=True)
    tree.joinpath(fixture.file_name).write_text("\n".join(fixture.lines), encoding="utf-8")
    if fixture.shipped_lists:
        return tree, SHIPPED_DENYLIST, SHIPPED_ALLOWLIST
    denylist = root / "deny.txt"
    allowlist = root / "allow.txt"
    denylist.write_text(
        _denylist_text(fixture.denied_person, fixture.denied_project), encoding="utf-8"
    )
    allowlist.write_text(_allowlist_text(), encoding="utf-8")
    return tree, denylist, allowlist


def _run(tree: Path, denylist: Path, allowlist: Path) -> tuple[int, str, str]:
    """Run the gate over one tree and return its status and both streams.

    The streams are captured by redirection rather than through the capture
    fixture, because that fixture is function scoped and a property drives many
    examples inside one function call.
    """
    argv = [str(tree), "--denylist", str(denylist), "--allowlist", str(allowlist)]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = int(GATE.main(argv))
    return status, out.getvalue(), err.getvalue()


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


# Feature: molt, Property 39: For any generated file content containing a
# prohibited metadata pattern, the hygiene check exits with a non-zero status code
# and names the pattern class; for any content containing only allowlisted
# platform and vendor names, it exits 0.
@pytest.mark.parametrize("class_name", [*CLASS_ORDER, ALLOWLISTED_ONLY])
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(data=st.data())
def test_the_check_names_the_class_and_exits_non_zero_on_prohibited_content(
    scan_area: Path, class_name: str, data: st.DataObject
) -> None:
    fixture = data.draw(hygiene_fixtures(class_name))
    event(f"suffix={Path(fixture.file_name).suffix}")

    root = scan_area / token_hex(8)
    try:
        _assert_scan(root, fixture)
    finally:
        # The example's tree is removed as soon as it has been judged, so a run
        # of nine hundred examples leaves one empty directory rather than nine
        # hundred trees.
        rmtree(root, ignore_errors=True)


def _assert_scan(root: Path, fixture: HygieneFixture) -> None:
    """Run the gate over one example's tree and judge what it reported."""
    tree, denylist, allowlist = _materialise(root, fixture)
    status, out, err = _run(tree, denylist, allowlist)

    if not fixture.prohibited:
        assert status == CLEAN_EXIT, (
            "content holding only permitted platform and vendor terms exited "
            f"{status} rather than {CLEAN_EXIT}: {out}{err}"
        )
        assert "no findings" in out
        return

    assert status != CLEAN_EXIT, (
        f"content carrying the {fixture.expected_class} class exited {CLEAN_EXIT}, "
        f"so the pattern went undetected: {out}"
    )
    assert status == FINDING_EXIT, (
        f"the scan exited {status} rather than {FINDING_EXIT}; exit "
        f"{MALFORMED_EXIT} would mean the list files were rejected rather than "
        f"the content: {err}"
    )
    assert err == "", f"a scan reporting findings wrote to the error stream: {err}"
    assert fixture.expected_span in _spans_for(out, fixture.expected_class), (
        f"the scan reported {_findings(out)} rather than the span "
        f"{fixture.expected_span!r} under the class {fixture.expected_class}"
    )
    located = _fuse(
        fixture.file_name,
        ":",
        str(fixture.expected_line),
        ":",
        fixture.expected_class,
        ":",
        fixture.expected_span,
    )
    assert located in out.splitlines(), (
        f"the finding was not reported at line {fixture.expected_line} of "
        f"{fixture.file_name}: {out}"
    )


# ---------------------------------------------------------------------------
# The adversarial value generator
#
# Four content categories are named by the property, and every drawn value set
# carries all four: quote sequences, statement terminators, comment markers, and
# whole statement fragments. Each category has one value guaranteed to hold it so
# that no example can pass by drawing a benign set, and every value additionally
# draws further fragments from the union of the four pools, so the categories are
# also exercised in combination.
# ---------------------------------------------------------------------------

QUOTE_FRAGMENTS: Final[tuple[str, ...]] = (
    "'",
    '"',
    "''",
    '""',
    "\\'",
    '\\"',
    "`",
    "')",
    "\"')",
    "' OR '",
)
TERMINATOR_FRAGMENTS: Final[tuple[str, ...]] = (";", ";;", " ; ", "');", ";--", "; )")
COMMENT_FRAGMENTS: Final[tuple[str, ...]] = ("--", "-- ", "/*", "*/", "/* */", "#", "--/*")
STATEMENT_FRAGMENTS: Final[tuple[str, ...]] = (
    "DROP TABLE session",
    "DROP SCHEMA public CASCADE",
    "DELETE FROM ledger WHERE 1 = 1",
    "SELECT 1",
    "UNION ALL SELECT NULL",
    "OR 1 = 1",
    "ALTER TABLE session ADD COLUMN injected INT",
    "GRANT ALL ON session TO PUBLIC",
    "SET search_path TO public",
    "COMMIT",
    "ROLLBACK",
    "%s",
    "$1",
    "\\",
)
ALL_FRAGMENTS: Final[tuple[str, ...]] = (
    *QUOTE_FRAGMENTS,
    *TERMINATOR_FRAGMENTS,
    *COMMENT_FRAGMENTS,
    *STATEMENT_FRAGMENTS,
)

# Ordinary words the fragments are woven through, so a value is a plausible
# caller-supplied string rather than punctuation alone.
BENIGN_WORDS: Final[tuple[str, ...]] = ("agent", "workspace", "machine", "team", "run", "path")

# The four categories, for the coverage record a run prints.
CATEGORY_POOLS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("quote", QUOTE_FRAGMENTS),
    ("terminator", TERMINATOR_FRAGMENTS),
    ("comment", COMMENT_FRAGMENTS),
    ("statement", STATEMENT_FRAGMENTS),
)


@dataclass(frozen=True, slots=True)
class AdversarialValues:
    """One caller-supplied value per column the Session insert writes as text."""

    agent_cli: str
    machine_id: str
    team_id: str
    workspace_path: str
    attribution_key: str
    attribution_value: str

    @property
    def texts(self) -> tuple[str, ...]:
        """Every value, for the coverage record and the containment assertions."""
        return (
            self.agent_cli,
            self.machine_id,
            self.team_id,
            self.workspace_path,
            self.attribution_key,
            self.attribution_value,
        )

    def session_for(self, client_id: UUID) -> Session:
        """The Session record these values are carried into the cluster on."""
        return Session(
            id=uuid4(),
            client_id=client_id,
            agent_cli=self.agent_cli,
            machine_id=self.machine_id,
            team_id=self.team_id,
            attribution={self.attribution_key: self.attribution_value},
            workspace_path=self.workspace_path,
            started_at=MOMENT,
            ended_at=None,
            outcome=SessionOutcome.IN_PROGRESS,
            parent_session_id=None,
            spawning_event_id=None,
            depth=0,
            tool_call_count=0,
            model_request_count=0,
            error_count=0,
            token_count=0,
            cost_usd=Decimal(0),
            halted=False,
            halted_at=None,
            halt_reason=None,
            halt_rule_id=None,
        )


@st.composite
def _woven(draw: st.DrawFn, required: str) -> str:
    """Weave one required fragment through further fragments and ordinary words."""
    extra = draw(st.lists(st.sampled_from(ALL_FRAGMENTS), max_size=3))
    filler = draw(st.lists(st.sampled_from(BENIGN_WORDS), max_size=3))
    ordered = draw(st.permutations([required, *extra, *filler]))
    return draw(st.sampled_from(("", " "))).join(ordered)


@st.composite
def adversarial_values(draw: st.DrawFn) -> AdversarialValues:
    """Draw one value per text column, the four content categories all present."""
    quote = draw(st.sampled_from(QUOTE_FRAGMENTS))
    terminator = draw(st.sampled_from(TERMINATOR_FRAGMENTS))
    marker = draw(st.sampled_from(COMMENT_FRAGMENTS))
    fragment = draw(st.sampled_from(STATEMENT_FRAGMENTS))
    return AdversarialValues(
        agent_cli=draw(_woven(quote)),
        machine_id=draw(_woven(terminator)),
        team_id=draw(_woven(marker)),
        workspace_path=draw(_woven(fragment)),
        attribution_key=draw(_woven(draw(st.sampled_from(ALL_FRAGMENTS)))),
        attribution_value=draw(_woven(draw(st.sampled_from(ALL_FRAGMENTS)))),
    )


# ---------------------------------------------------------------------------
# The cluster the values are carried to
# ---------------------------------------------------------------------------

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The fixture's own writes and reads. This module owns no Client insert, so that
# row is placed here, with every value bound.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
COUNT_SESSIONS: Final[str] = "SELECT count(*) FROM session"

# The one read that asks whether the stored value is data: every text column is
# matched by equality against the value that was sent, so a row comes back only
# if the cluster stored the value verbatim and compares it as a scalar.
MATCH_BY_VALUE: Final[str] = (
    "SELECT count(*) FROM session WHERE id = %s AND client_id = %s AND agent_cli = %s "
    "AND machine_id = %s AND team_id = %s AND workspace_path = %s "
    "AND attribution = %s::JSONB"
)

# The catalog reading the schema is compared by, taken both sides of a write. It
# spans columns and their declared types, constraints, check clauses, indexes and
# their column order, and views, which together are what "the schema" means here.
# A reading taken on both sides cannot miss an object the write altered, which a
# list of objects named by hand can.
CATALOG_STATEMENT: Final[str] = (
    "SELECT 'column' AS aspect, table_name::STRING AS first, column_name::STRING AS second, "
    "data_type::STRING AS third, crdb_sql_type::STRING AS fourth, is_nullable::STRING AS fifth "
    "FROM information_schema.columns WHERE table_schema = %s "
    "UNION ALL "
    "SELECT 'constraint', table_name::STRING, constraint_name::STRING, "
    "constraint_type::STRING, '', '' "
    "FROM information_schema.table_constraints WHERE table_schema = %s "
    "UNION ALL "
    "SELECT 'check', constraint_name::STRING, '', check_clause::STRING, '', '' "
    "FROM information_schema.check_constraints WHERE constraint_schema = %s "
    "UNION ALL "
    "SELECT 'index', table_name::STRING, index_name::STRING, column_name::STRING, "
    "seq_in_index::STRING, non_unique::STRING "
    "FROM information_schema.statistics WHERE table_schema = %s "
    "UNION ALL "
    "SELECT 'view', table_name::STRING, '', view_definition::STRING, '', '' "
    "FROM information_schema.views WHERE table_schema = %s "
    "ORDER BY 1, 2, 3, 4, 5, 6"
)

# An instant with an offset, derived from the epoch rather than written as a
# literal, so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any

# What the catalog reading is held as.
CatalogReading = tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and one tenant."""

    store: MemoryStore
    connection: DriverConnection
    schema: str
    client_id: UUID

    def catalog(self) -> CatalogReading:
        """Read the whole shape of this schema, as rows of text."""
        with self.connection.cursor() as cursor:
            cursor.execute(CATALOG_STATEMENT, (self.schema,) * 5)
            return tuple(tuple(str(column) for column in row) for row in cursor.fetchall())

    def session_count(self) -> int:
        """How many Session rows this schema holds."""
        with self.connection.cursor() as cursor:
            cursor.execute(COUNT_SESSIONS, ())
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def matched_by_value(self, record: Session, attribution: str) -> int:
        """How many rows match every text column by equality against what was sent."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                MATCH_BY_VALUE,
                (
                    record.id,
                    record.client_id,
                    record.agent_cli,
                    record.machine_id,
                    record.team_id,
                    record.workspace_path,
                    attribution,
                ),
            )
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store bound to that schema.

    Module scope keeps the schema cost paid once. Examples are isolated from each
    other by minting a Session identifier of their own rather than by a schema of
    their own, so the row count assertion is made against the count this example
    observed rather than against an empty table.
    """
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    client_id = uuid4()
    with fresh_schema.cursor() as cursor:
        cursor.execute(INSERT_CLIENT, (client_id, f"tenant-{client_id.hex[:8]}", "Tenant", "eu"))

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema, schema=schema, client_id=client_id)


def _json_text(payload: JsonObject) -> str:
    """Render an attribution mapping as JSON text for the equality read.

    Any equivalent rendering serves: the column holds the parsed document and the
    comparison the cluster makes is over that document rather than over the text
    it arrived as.
    """
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _categories_of(values: AdversarialValues) -> tuple[str, ...]:
    """Which content categories a drawn value set actually carries."""
    joined = "\n".join(values.texts)
    return tuple(
        name for name, pool in CATEGORY_POOLS if any(fragment in joined for fragment in pool)
    )


# Feature: molt, Property 39: For any caller-supplied string containing SQL
# metacharacters, quote sequences, or statement terminators, the value round-trips
# through Memory_Store as data and alters no schema or query semantics.
@pytest.mark.integration
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(values=adversarial_values())
def test_adversarial_values_round_trip_as_data_and_alter_no_schema(
    cluster: Cluster, values: AdversarialValues
) -> None:
    carried = _categories_of(values)
    for name in carried:
        event(f"carries={name}")
    assert len(carried) == len(CATEGORY_POOLS), (
        "a drawn value set must carry a quote sequence, a statement terminator, a "
        f"comment marker, and a statement fragment; it carried {carried}"
    )

    before_catalog = cluster.catalog()
    before_rows = cluster.session_count()

    record = values.session_for(cluster.client_id)
    assert upsert_session(cluster.store, record) == 0

    stored = session_of_client(cluster.store, record.id, cluster.client_id)
    assert stored is not None, "the Session written under adversarial values did not read back"
    for label, sent, read in (
        ("the agent name", values.agent_cli, stored.agent_cli),
        ("the machine identifier", values.machine_id, stored.machine_id),
        ("the team identifier", values.team_id, stored.team_id),
        ("the workspace path", values.workspace_path, stored.workspace_path),
    ):
        assert read == sent, f"{label} came back as {read!r} rather than {sent!r}"
        assert read is not None
        assert read.encode() == sent.encode(), (
            f"{label} came back with different bytes than it was sent as"
        )
    assert stored.attribution == record.attribution, (
        "the attribution mapping came back as "
        f"{stored.attribution!r} rather than {record.attribution!r}"
    )

    # The value is data: the row is found by matching every text column against
    # what was sent, which no amount of statement text inside a value can arrange.
    attribution_text = _json_text(record.attribution)
    assert cluster.matched_by_value(record, attribution_text) == 1, (
        "the stored row was not found by equality against the values that were "
        "sent, so something other than those values was stored"
    )

    # Query semantics are unchanged: exactly one row was added, and nothing the
    # statement fragments name was executed.
    assert cluster.session_count() == before_rows + 1, (
        "the write changed the Session row count by something other than one"
    )
    after_catalog = cluster.catalog()
    assert after_catalog == before_catalog, (
        "the catalog reading taken after the write differs from the one taken "
        f"before it, by {tuple(set(after_catalog) ^ set(before_catalog))}"
    )
