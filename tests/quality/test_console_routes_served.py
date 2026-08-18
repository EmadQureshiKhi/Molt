"""Gate assertion that every declared console route is actually served.

The console declares its routes in one table and implements each one in a view module
that claims the route by name at import time. The two halves are joined by importing the
view package, and for a long time nothing did. Every page therefore answered *not
implemented*: the table was complete, eighteen handlers were written and correct, and
the only missing thing was an import — so the console had no working page at all, in
every deployment and locally alike.

Nothing failed. A route with no claimed handler is answered by a placeholder that
returns a well-formed body and a status saying so, which is a reasonable thing to do for
a route someone has declared and not yet written, and an unreasonable thing to be true
of all of them at once. The suites that exercise the handlers import the view modules
themselves, directly, so they passed against handlers the application never attached.

So this asserts the join rather than the halves: every route the table declares resolves
to a claimed handler, and none resolves to the placeholder. It is built the way the
application builds it, through the same call, so an import the application does not
perform is an import this does not see either.
"""

from __future__ import annotations

from typing import Final

import pytest
from starlette.routing import Route

from molt.console.app import build_routes
from molt.console.routing import HANDLERS, ROUTE_TABLE

# Static gate over the declared surface: no reachable instance and no credential.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.quality

# What the placeholder answers, which is how an unserved route is recognised.
NOT_IMPLEMENTED_STATUS: Final[int] = 501


def test_the_route_table_declares_routes_at_all() -> None:
    """The detector is checked before what it detects.

    Both cases below iterate the route table. An empty table would leave them passing
    over nothing while the console served no page, which is the failure this file
    exists to catch, so the table is asserted to be populated first.
    """
    assert len(ROUTE_TABLE) > 1, (
        f"the route table declares {len(ROUTE_TABLE)} routes, so the cases below would "
        "check almost nothing"
    )


def test_every_declared_route_is_claimed_by_a_view_module() -> None:
    """The join between the table and the view modules, asserted as one claim.

    A route declared and unclaimed is a page that answers *not implemented*. That is
    the right answer for a route deliberately declared ahead of its handler, and the
    wrong answer for every route at once, which is what it was.

    The routes are built through the application's own call, so what is asserted is the
    state the application actually reaches rather than the state reachable by importing
    the view modules directly. That distinction is the whole point: the handlers were
    always importable, and the application never imported them.
    """
    built = build_routes()
    declared = {spec.name for spec in ROUTE_TABLE}
    unclaimed = sorted(name for name in declared if name not in HANDLERS)

    assert not unclaimed, (
        f"{len(unclaimed)} declared route(s) have no claimed handler, so each answers "
        f"not implemented: {', '.join(unclaimed)}; a view module that is written but "
        "not named in the view package's import list is not served"
    )
    assert len(built) == len(ROUTE_TABLE), (
        f"the application built {len(built)} routes for {len(ROUTE_TABLE)} declared"
    )


def test_no_built_route_is_answered_by_the_placeholder() -> None:
    """The same claim read off the built application rather than off the handler table.

    The case above compares two collections; this one asks what the application would
    actually run. They are separate because the placeholder is chosen inside the build:
    a future change that populated the handler table and still selected the placeholder
    — a lookup by the wrong key, a name normalised on one side only — would pass the
    comparison and serve nothing.

    The endpoint is identified by the module it was defined in rather than by calling
    it, so nothing here needs a request, a store, or a session.
    """
    placeheld: list[str] = []
    for route in build_routes():
        if not isinstance(route, Route):
            continue
        endpoint = route.endpoint
        closed = getattr(endpoint, "__closure__", None) or ()
        # The tagged wrapper closes over the endpoint it defers to, so the placeholder
        # is looked for one level in as well as at the surface.
        candidates = [endpoint, *(cell.cell_contents for cell in closed)]
        for candidate in candidates:
            if getattr(candidate, "__qualname__", "").startswith("_placeholder"):
                placeheld.append(route.name or "an unnamed route")
                break

    assert not placeheld, (
        f"{len(placeheld)} route(s) are served by the placeholder, so they answer "
        f"{NOT_IMPLEMENTED_STATUS} however complete their view module is: "
        f"{', '.join(sorted(placeheld))}"
    )
