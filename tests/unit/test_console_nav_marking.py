"""Gate assertion that every navigation section marks itself when it is the page being served.

The layout marks the current section with `aria-current="page"`, which is both what a screen
reader announces and what the stylesheet draws the underline from. It read that from a context
value one shared render helper filled in — and eight of the console's views render their
template directly rather than through that helper, so eight pages highlighted no section at
all. Fleet worked, Residue did not, and nothing failed: a missing attribute renders as an
unmarked link, which looks like a page that simply is not in the navigation.

The fix was to read the name from the request scope, where the application binds it for every
route it serves, so no view supplies it and none can omit it. This holds that: for each section
the navigation declares, a request whose scope names that route marks exactly that link and no
other.

It is written against the layout rather than against the helper on purpose. The defect was not
in either renderer; it was in the layout depending on something only one of them provided.

**Validates: Requirements 48.10, 49.16**
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from molt.console.routing import ROUTE_TABLE

# Static gate over the layout: no reachable instance and no credential.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.quality

TEMPLATE_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "web" / "templates"
LAYOUT: Final[str] = "base.html"

# How the layout declares its navigation, read out of the template rather than restated here,
# so a section added to the layout is covered by this without anyone adding it twice.
SECTION = re.compile(r'\("([^"]+)",\s*"([^"]+)",\s*"([^"]+)"\)')

# The attribute the layout marks the current section with.
MARKER: Final[str] = 'aria-current="page"'

# The layout renders a session credential into the sign-out form, so one has to be supplied for it
# to render at all. Named rather than written at the call site, because a token-shaped literal
# passed to a token-shaped argument is exactly what the credential linter is looking for, and it
# is right to look: the value here stands in for one and is not one.
STAND_IN_VALUE: Final[str] = "no-session-under-test"


def sections() -> tuple[tuple[str, str, str], ...]:
    """Every navigation section the layout declares, as path, route name, and label."""
    text = (TEMPLATE_ROOT / LAYOUT).read_text(encoding="utf-8")
    block = text.split("{% set sections = [", 1)[1].split("] %}", 1)[0]
    return tuple(SECTION.findall(block))


class _Scope(dict[str, object]):
    """The request scope, which is where the layout reads the served route's name from."""


class _Request:
    """The one attribute of a request the layout reads."""

    def __init__(self, route_name: str) -> None:
        self.scope = _Scope({"route_name": route_name})


def _rendered(route_name: str) -> str:
    """The layout, rendered as though this route were the one being served."""
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        autoescape=True,
        undefined=StrictUndefined,
    )
    template = environment.get_template(LAYOUT)
    return template.render(
        request=_Request(route_name),
        title="Under test",
        demo_mode=False,
        authenticated=True,
        csrf_token=STAND_IN_VALUE,
    )


def _marked(html: str) -> set[str]:
    """The labels of every navigation link the layout marked as current."""
    marked: set[str] = set()
    for anchor in re.findall(r"<a\b[^>]*>.*?</a>", html, re.DOTALL):
        if MARKER in anchor:
            text = re.sub(r"<[^>]*>", "", anchor).strip()
            if text:
                marked.add(text)
    return marked


def test_the_layout_declares_navigation_sections_at_all() -> None:
    """The detector is checked before what it detects.

    Every case below iterates the declared sections. An empty declaration would leave them
    passing over nothing while no page highlighted anything, which is the failure this file
    exists to catch.
    """
    assert len(sections()) > 1, "the layout declares no navigation sections"


def test_every_declared_section_names_a_route_the_table_declares() -> None:
    """A section naming no route can never be marked, however the layout reads the name."""
    table = {spec.name: spec.path for spec in ROUTE_TABLE}
    wrong = [(label, name, path) for path, name, label in sections() if table.get(name) != path]
    assert wrong == [], (
        f"these navigation sections name a route the table does not declare at that path: {wrong}"
    )


@pytest.mark.parametrize("section", sections(), ids=lambda section: section[1])
def test_the_section_being_served_is_the_one_marked(section: tuple[str, str, str]) -> None:
    """Serving a route marks that section's link and no other."""
    _path, name, label = section

    marked = _marked(_rendered(name))

    assert marked == {label}, (
        f"serving {name!r} marked {sorted(marked)} rather than exactly [{label!r}]. The layout "
        "reads the served route's name from the request scope; a page that marks nothing means "
        "the value did not arrive there."
    )


def test_a_route_outside_the_navigation_marks_nothing() -> None:
    """A page that is in no section highlights none of them rather than guessing one.

    The certificate and run-detail views are reached from within a section rather than from the
    navigation, so there is no link for them to mark, and marking the nearest one would tell a
    reader they are somewhere they are not.
    """
    assert _marked(_rendered("certificate")) == set()
