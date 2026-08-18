"""The rendered console pages, parsed as markup rather than searched as text.

Every claim here is about structure, so every claim is made against a parse rather than
against a substring. The parser is the standard library's own, and it records each
element with its attributes, the text inside it, and the table row it sits in, which is
what lets these cases ask whether a *control* carries a name and whether a *row* carries
a cell rather than merely whether a word occurs somewhere in a page.

Four structural obligations are asserted:

* every interactive control in the rendered erasure console and in the rendered approval
  queue carries a programmatically determinable name, whether from a bound label, from an
  explicit attribute, or from its own text;
* no control is a bare container carrying a handler, so nothing operable is reachable
  only by a pointer and nothing operable is invisible to assistive technology;
* the streaming region of the erasure console is a live region, so progress is announced
  without stealing focus;
* the two analysis views state absence as absence: an inapplicable grid cell renders the
  word and its reason instead of a count, and a below-floor procedure is marked as
  excluded from recall and retained in storage.

The Memory_Tier view is asserted three ways: one row per tier each carrying a text label,
the working tier's row carrying both figures that belong to it alone, and the rendered
tier set being the tier mapping's own rather than a second list a template spelled out.

Credential-free and cluster-free. The store is a stand-in answering the statements these
views send, and the application is driven through the Lambda adapter, which is the
deployed path, so what is parsed is what a browser would receive.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

import pytest

from molt.config.resolve import load_configuration
from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import build_app
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.console.routes import approvals as approvals_view
from molt.console.routes import procedures as procedures_view
from molt.console.routes import sensitivity as sensitivity_view
from molt.console.routes import tenancy
from molt.console.routes import tiers as tiers_view
from molt.console.routes.erasure_common import CONFIGURATION_STATE_KEY, SELECT_FLEET_STATEMENT
from molt.erase.sensitivity import (
    INAPPLICABLE_REASON,
    PairOutcome,
    SensitivityReport,
    ThresholdGrid,
)
from molt.models.tiers import MEMORY_TIERS, TIER_NAMES, WORKING_TIER
from molt.policy.apply import QUEUE_LIST_QUERY, ApprovalStatus
from molt.store import Cursor
from molt.store.capability import CapabilityRecord

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

CLIENT_ID: Final[UUID] = UUID(int=7)
CLIENT_SLUG: Final[str] = "a-tenant"
PROCEDURE_LOW: Final[UUID] = UUID(int=11)
PROCEDURE_HIGH: Final[UUID] = UUID(int=12)
OWNER: Final[UUID] = UUID(int=21)
OUTCOME_ROW: Final[UUID] = UUID(int=31)
QUEUE_ENTRY: Final[UUID] = UUID(int=41)
RULE_ID: Final[UUID] = UUID(int=51)
SESSION_ID: Final[UUID] = UUID(int=61)

WORKING_EXPIRED_COUNT: Final[int] = 3
TIER_ROW_COUNT: Final[int] = 7

# The creation statement the working table reports, carrying the schedule the migration
# applied. The view reads the sweep interval back from this rather than from a constant.
CREATE_WORKING: Final[str] = (
    "CREATE TABLE public.working_memory (session_id UUID NOT NULL) WITH "
    "(ttl_expiration_expression = 'expires_at', ttl_job_cron = '@hourly', "
    "ttl_delete_batch_size = 500)"
)

# The elements a caller may operate, and the roles a container would have to claim to
# pretend to be one of them.
NATIVE_CONTROLS: Final[frozenset[str]] = frozenset({"a", "button", "input", "select", "textarea"})
CONTROL_ROLES: Final[frozenset[str]] = frozenset(
    {"button", "checkbox", "link", "menuitem", "option", "radio", "switch", "textbox"}
)
VOID_TAGS: Final[frozenset[str]] = frozenset(
    {"area", "base", "br", "col", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


# ---------------------------------------------------------------------------
# The parse
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Element:
    """One element of a parsed page: its attributes, its text, and its table row."""

    tag: str
    attrs: dict[str, str]
    row: int | None
    text: str = ""

    @property
    def interactive(self) -> bool:
        """Whether a caller can operate this element at all.

        A hidden input carries no name because it presents nothing to operate, and a
        submit control inside a form does, which is the distinction this draws.
        """
        if self.tag not in NATIVE_CONTROLS:
            return False
        return self.attrs.get("type", "").lower() != "hidden"


class Page(HTMLParser):
    """A parse of one rendered page, recording elements with their text and their row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._open: list[Element] = []
        self._row = -1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record one opened element, and push it while its content is read."""
        element = self._recorded(tag, attrs)
        if tag not in VOID_TAGS:
            self._open.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record one self-closing element, which encloses nothing."""
        self._recorded(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        """Close the most recently opened element carrying this tag."""
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return

    def handle_data(self, data: str) -> None:
        """Give this text to every element it sits inside, not only to the innermost."""
        for element in self._open:
            element.text += data

    def _recorded(self, tag: str, attrs: list[tuple[str, str | None]]) -> Element:
        if tag == "tr":
            self._row += 1
        inside = tag == "tr" or any(open_element.tag == "tr" for open_element in self._open)
        element = Element(
            tag=tag,
            attrs={name: ("" if value is None else value) for name, value in attrs},
            row=self._row if inside else None,
        )
        self.elements.append(element)
        return element


def _parse(markup: str) -> Page:
    """The parse of one rendered page."""
    page = Page()
    page.feed(markup)
    page.close()
    return page


def _named(page: Page) -> Mapping[str, Element]:
    """Every element carrying an identifier, by that identifier."""
    return {
        element.attrs["id"]: element for element in page.elements if element.attrs.get("id", "")
    }


def _labels(page: Page) -> Mapping[str, str]:
    """The text of every label bound to a control, by the identifier it names."""
    return {
        element.attrs["for"]: element.text.strip()
        for element in page.elements
        if element.tag == "label" and element.attrs.get("for", "")
    }


def _controls(page: Page) -> tuple[Element, ...]:
    """Every element of a page a caller can operate."""
    return tuple(element for element in page.elements if element.interactive)


def _name_of(page: Page, control: Element) -> str:
    """The accessible name of one control, by the four routes a name can arrive by."""
    bound = _labels(page).get(control.attrs.get("id", ""), "")
    if bound:
        return bound
    explicit = control.attrs.get("aria-label", "").strip()
    if explicit:
        return explicit
    referenced = control.attrs.get("aria-labelledby", "").strip()
    if referenced:
        target = _named(page).get(referenced)
        if target is not None and target.text.strip():
            return target.text.strip()
    own = control.text.strip()
    return own if own else control.attrs.get("value", "").strip()


def _cells(page: Page, row: int, tag: str = "td") -> tuple[Element, ...]:
    """Every cell of one table row."""
    return tuple(element for element in page.elements if element.tag == tag and element.row == row)


def _row_headers(page: Page) -> tuple[Element, ...]:
    """Every row header of a page, which is what a data table names its rows with."""
    return tuple(
        element
        for element in page.elements
        if element.tag == "th" and element.attrs.get("scope") == "row"
    )


# ---------------------------------------------------------------------------
# The store these views read through
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubCursor:
    """A cursor answering each statement these views send from held rows."""

    statements: list[str] = field(default_factory=list)
    rows: list[tuple[object, ...]] = field(default_factory=list)

    def execute(self, statement: str, parameters: Sequence[object] | None = None) -> None:
        """Record the statement and hold the rows it is answered with."""
        self.statements.append(statement)
        self.rows = list(_answer(statement, () if parameters is None else tuple(parameters)))

    def fetchone(self) -> tuple[object, ...] | None:
        """The single row a statement of one row answers with."""
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row a listing statement answers with."""
        return list(self.rows)

    def close(self) -> None:
        """Close the cursor, which this stand-in holds nothing for."""


def _answer(statement: str, parameters: tuple[object, ...]) -> Sequence[tuple[object, ...]]:
    """The rows one statement is answered with, by the statement each view declares."""
    if statement in (SELECT_FLEET_STATEMENT, tenancy.CLIENT_ROSTER_STATEMENT):
        return ((CLIENT_ID, CLIENT_SLUG, "A Tenant"),)
    if statement == QUEUE_LIST_QUERY:
        return (
            (
                QUEUE_ENTRY,
                RULE_ID,
                "a-rule-asking-for-approval",
                "require_approval",
                SESSION_ID,
                CLIENT_ID,
                None,
                ApprovalStatus.PENDING.value,
                NOW,
                None,
                None,
                None,
            ),
        )
    if statement == tiers_view.CLUSTER_NOW_QUERY:
        return ((NOW,),)
    if statement == tiers_view.WORKING_TTL_CONFIGURATION_QUERY:
        return ((CREATE_WORKING,),)
    if statement == tiers_view.WORKING_EXPIRED_QUERY:
        return ((WORKING_EXPIRED_COUNT,),)
    if statement in set(tiers_view.TIER_COUNT_QUERIES.values()):
        return ((7,),)
    if statement == procedures_view.SELECT_PROCEDURES_QUERY:
        return (
            (PROCEDURE_LOW, OWNER, 0.05, 1, NOW),
            (PROCEDURE_HIGH, OWNER, 0.80, 2, NOW),
        )
    if statement.startswith("SELECT procedure_confidence FROM derived_artifact"):
        return ((0.05 if PROCEDURE_LOW in parameters else 0.80,),)
    if statement.startswith("SELECT count(*) FROM procedure_retrieval"):
        return ((4,),)
    if statement.startswith("SELECT outcome, count(*) FROM procedure_outcome"):
        return (("failed", 2), ("succeeded", 1))
    if statement.startswith("SELECT id, procedure_id, prior_value"):
        return ((UUID(int=71), PROCEDURE_LOW, 0.5, 0.35, OUTCOME_ROW, NOW),)
    return ()


class StubStore:
    """The read path these views use, and a write path that refuses.

    None of the pages parsed here may write, so a view that opened a write transaction
    fails the suite rather than merely being noticed in review.
    """

    role = "reader"

    def __init__(self) -> None:
        self.statements: list[str] = []

    def read(self, body: Callable[[Cursor], object]) -> object:
        """Run a read body on a cursor of this stand-in's own."""
        return body(cast(Cursor, StubCursor(self.statements)))

    def in_serializable(self, _body: object, **_: object) -> object:
        """Refuse: no page parsed here is rendered from a write transaction."""
        raise AssertionError("a read-only console view opened a write transaction")

    def known_capabilities(self) -> CapabilityRecord:
        """The capability record the health route reads and these views do not."""
        return CapabilityRecord()


def _console(store: StubStore, *, demo_mode: bool = False) -> Console:
    root = Path(__file__).resolve().parents[2]
    settings = ConsoleSettings(
        host="127.0.0.1",
        port=8080,
        demo_mode=demo_mode,
        interface_spec_path=root / "docs" / "interface.json",
        template_directory=root / "web" / "templates",
        static_directory=root / "web" / "static",
    )
    return Console(
        settings=settings,
        store=cast(Any, store),
        credential=Credential(
            auth.credential_record(CREDENTIAL, iterations=2),
            source_name="test",
            source=CredentialSource.ENVIRONMENT,
        ),
        session_key=Credential(
            SESSION_KEY, source_name="test", source=CredentialSource.ENVIRONMENT
        ),
        clock=lambda: NOW,
    )


def _serve(path: str, *, demo_mode: bool = False) -> LambdaResponse:
    """Serve one page through the deployed path, carrying a valid session."""
    _, cookie = auth.issue(SESSION_KEY, now=NOW)
    event: Mapping[str, object] = {
        "version": "2.0",
        "rawPath": path.partition("?")[0],
        "rawQueryString": path.partition("?")[2],
        "headers": {"accept": "text/html"},
        "cookies": [f"{auth.COOKIE_NAME}={cookie}"],
        "requestContext": {"http": {"method": "GET", "path": path.partition("?")[0]}},
        "body": "",
        "isBase64Encoded": False,
    }
    app = build_app(_console(StubStore(), demo_mode=demo_mode))
    setattr(app.state, CONFIGURATION_STATE_KEY, load_configuration(environ={}))
    return invoke(cast(Any, app), dict(event))


def _page(path: str, *, demo_mode: bool = False) -> Page:
    """The parse of one served page, refusing to parse a refusal."""
    answer = _serve(path, demo_mode=demo_mode)
    assert answer["statusCode"] == 200, answer["statusCode"]
    return _parse(cast(str, answer["body"]))


def _report() -> SensitivityReport:
    """A two-by-two report whose lower row is inapplicable, built without a cluster."""
    grid = ThresholdGrid.from_axes([0.2, 0.5], [0.3, 0.4])
    outcomes: list[PairOutcome] = []
    for pair in grid.pairs:
        if pair.applicable:
            outcomes.append(
                PairOutcome(
                    auto_include_threshold=pair.auto_include_threshold,
                    review_threshold=pair.review_threshold,
                    candidate_count=5,
                    auto_included_count=2,
                    referred_count=3,
                    recovered_count=1,
                )
            )
        else:
            outcomes.append(
                PairOutcome(
                    auto_include_threshold=pair.auto_include_threshold,
                    review_threshold=pair.review_threshold,
                    candidate_count=None,
                    auto_included_count=None,
                    referred_count=None,
                    inapplicable_reason=INAPPLICABLE_REASON,
                )
            )
    return SensitivityReport(
        grid=grid,
        outcomes=tuple(outcomes),
        retained=(),
        searched_at=0.4,
        query_artifact_ids=(UUID(int=81),),
        ground_truth_available=True,
    )


@pytest.fixture
def analysed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the grid from a hand-built report, so no corpus and no search is needed."""
    monkeypatch.setattr(sensitivity_view, "permitted_client_ids", lambda *_: (CLIENT_ID,))
    monkeypatch.setattr(sensitivity_view, "analyse_client", lambda *_, **__: _report())
    monkeypatch.setattr(sensitivity_view, "load_configuration", lambda: cast(Any, object()))
    monkeypatch.setattr(sensitivity_view, "default_grid", lambda *_: None)


# -- every control carries a name ------------------------------------------


def test_every_control_in_the_erasure_console_carries_a_name() -> None:
    page = _page("/erase")
    controls = _controls(page)
    assert controls
    for control in controls:
        assert _name_of(page, control), (control.tag, control.attrs)


def test_every_control_in_the_approval_queue_carries_a_name() -> None:
    page = _page("/approvals")
    controls = _controls(page)
    assert controls
    for control in controls:
        assert _name_of(page, control), (control.tag, control.attrs)


def test_each_resolution_control_names_the_entry_it_would_resolve() -> None:
    page = _page("/approvals")
    submits = [
        element
        for element in _controls(page)
        if element.attrs.get("name") == approvals_view.DECISION_FIELD
    ]
    assert len(submits) == 2
    for submit in submits:
        assert str(QUEUE_ENTRY) in _name_of(page, submit)


# -- nothing operable is a bare container ----------------------------------


def test_no_control_in_the_erasure_console_is_a_container_carrying_a_handler() -> None:
    for path in ("/erase", "/approvals"):
        page = _page(path)
        for element in page.elements:
            handlers = [name for name in element.attrs if name.startswith("on")]
            assert handlers == [], (path, element.tag, handlers)
            if element.tag not in NATIVE_CONTROLS:
                assert element.attrs.get("role", "") not in CONTROL_ROLES, (path, element.tag)


def test_the_erasure_console_submits_through_a_form_rather_than_a_script() -> None:
    page = _page("/erase")
    forms = [element for element in page.elements if element.tag == "form"]
    assert [form.attrs.get("action") for form in forms] == ["/logout", "/erase"]
    submits = [
        element
        for element in _controls(page)
        if element.tag == "button" and element.attrs.get("type") == "submit"
    ]
    assert submits


# -- the streaming region --------------------------------------------------


def test_the_streaming_region_of_the_erasure_console_is_a_live_region() -> None:
    page = _page("/erase")
    live = [element for element in page.elements if element.attrs.get("aria-live", "")]
    assert len(live) == 1
    assert live[0].attrs["aria-live"] == "polite"
    assert live[0].attrs.get("id") == "erase-progress"


def test_the_disabled_controls_of_a_demonstration_name_their_explanation() -> None:
    page = _page("/erase", demo_mode=True)
    disabled = [element for element in _controls(page) if "disabled" in element.attrs]
    assert disabled
    for element in disabled:
        described = element.attrs.get("aria-describedby", "")
        assert described
        target = _named(page).get(described)
        assert target is not None
        assert target.text.strip()


# -- the sensitivity grid --------------------------------------------------


@pytest.mark.usefixtures("analysed")
def test_an_inapplicable_grid_cell_renders_the_word_and_its_reason() -> None:
    page = _page("/sensitivity?client=one")
    inapplicable = [
        element
        for element in page.elements
        if element.tag == "td" and sensitivity_view.INAPPLICABLE_LABEL in element.text
    ]
    assert inapplicable
    for cell in inapplicable:
        assert INAPPLICABLE_REASON in cell.text
        assert "Candidates" not in cell.text


@pytest.mark.usefixtures("analysed")
def test_the_grid_names_both_header_directions() -> None:
    page = _page("/sensitivity?client=one")
    columns = [
        element
        for element in page.elements
        if element.tag == "th" and element.attrs.get("scope") == "col"
    ]
    assert columns
    assert _row_headers(page)


# -- the procedure standing view -------------------------------------------


def test_the_procedures_view_marks_a_below_floor_procedure_as_retained() -> None:
    page = _page("/procedures")
    marked = [
        element for element in page.elements if procedures_view.BELOW_FLOOR_MARKER in element.text
    ]
    assert marked
    rows = {element.row for element in marked if element.row is not None}
    assert rows
    for row in rows:
        carried = "".join(cell.text for cell in _cells(page, row)) + "".join(
            header.text for header in _cells(page, row, tag="th")
        )
        assert str(PROCEDURE_LOW) in carried


# -- the Memory_Tier view --------------------------------------------------


def test_the_tier_view_renders_one_row_per_tier_each_carrying_a_text_label() -> None:
    page = _page("/tiers")
    headers = _row_headers(page)
    assert [header.text.strip() for header in headers] == list(TIER_NAMES)


def test_the_working_tier_row_carries_its_expired_count_and_its_next_sweep_cell() -> None:
    page = _page("/tiers")
    working = [header for header in _row_headers(page) if header.text.strip() == WORKING_TIER]
    assert len(working) == 1
    row = working[0].row
    assert row is not None
    cells = _cells(page, row)
    assert len(cells) == TIER_ROW_COUNT - 1
    assert cells[-2].text.strip() == str(WORKING_EXPIRED_COUNT)
    assert "second(s)" in cells[-1].text


def test_a_tier_with_no_sweep_states_that_absence_rather_than_a_figure() -> None:
    page = _page("/tiers")
    for header in _row_headers(page):
        if header.text.strip() == WORKING_TIER or header.row is None:
            continue
        cells = _cells(page, header.row)
        assert "Not applicable" in cells[-1].text
        assert "Not applicable" in cells[-2].text


def test_the_tier_mapping_module_and_the_rendered_view_name_the_same_tier_set() -> None:
    page = _page("/tiers")
    rendered = {header.text.strip() for header in _row_headers(page)}
    assert rendered == set(TIER_NAMES)
    assert rendered == set(MEMORY_TIERS)
    assert tuple(tiers_view.TIER_COUNT_QUERIES) == TIER_NAMES
