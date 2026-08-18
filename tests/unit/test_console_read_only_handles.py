"""A console handler that writes nothing reads through the narrow handle, checked here.

The deployed console function authenticates as the eraser role, because the erasure
console runs erasures from that same function. Most of the console erases nothing: it
lists Sessions, walks lineage, counts tiers, renders a queue, streams a run's phases.
Those handlers read through whichever handle happened to be in scope, which made their
read-only-ness a habit of each module rather than a privilege the connection holds them
to. `Console.read_only_store()` is the seam that fixes it: the read-only connection where
a deployment configures one, and the primary handle where it configures none, so a
one-connection deployment keeps the view instead of losing it to a privilege the view
never depended on. `Console.reader_store()` is the strict form, for the one analysis that
refuses a wider role itself.

This gate exists because the invariant already decayed once: the seam was added, ten
modules were converted, and three handlers kept reaching for the wide handle afterwards —
the same lapse surviving in three places after being fixed in ten. An invariant that has
to be remembered is one that lapses, so it is derived and checked.

**The scope is one handler at a time, which is the difference between this gate and the
module-scoped one.** Every route the table declares is resolved to its claimed handler,
and each handler that records no row is held to the rule inside its own body, so a module
holding both a listing and a submission is answered precisely rather than exempted whole.
The companion gate under `tests/quality/` reads whole module sources instead, which is what
catches a shared helper reaching for the wider handle on behalf of every view that calls
it. Both scopes are kept because they catch different lapses.

**`mutation=True` does not mean the route records rows, and the demonstration disposition
stopped saying which is which.** A route is classified as a mutation when it is a form
submission that must carry this session's CSRF token, and a submission can carry a token
while storing nothing at all: a live certificate verification recomputes a digest, which is
a submission of exactly that kind. This gate used to read the disposition to tell the two
apart, because demonstration mode had to. Then the table grew an import-time invariant
requiring every route declaring `mutation=True` to declare `BLOCKED`, on the grounds that a
demonstration exposes no mutation route at all — so the verification now carries the same
disposition as the route that records an erasure run, and reading the disposition dropped
the certificates module out of scope without a single assertion failing there.

**What answers instead is `ROW_RECORDING_ROUTES`, named below with the row each member
writes.** An explicit set is the thing that decays into an unrechecked exception list, so
it is held to the table: every route it names must be declared a mutation there, which is
the table's own statement that the route is a submission. A set naming a listing, or a name
the table does not declare, fails rather than exempts.

**The skeleton's own routes are out of scope, and that is a boundary rather than an
exception.** The property under test is about views that read memory rows, which live in
the view package. `GET /health` reports the role the primary connection authenticates as
and probes that connection's reachability, so the wide handle is its subject rather than
its habit; holding it to this rule would be asking it to report something it is not
looking at.

The check reads parsed source rather than behaviour. Reaching a handle is a spelling — an
attribute named `store` — and a static answer covers every branch of a handler, including
the ones a test could only reach with a cluster. Nothing here opens a connection.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Final

import pytest

from molt.console import routes, routing

# The view package whose modules this gate covers, and the attribute a wider handle is
# reached through.
VIEW_PACKAGE: Final[str] = "molt.console.routes"
WIDE_HANDLE_ATTRIBUTE: Final[str] = "store"

# The two accessors that are permitted instead: the one that falls back to the primary
# handle, and the strict one that refuses it.
READ_ONLY_ACCESSOR: Final[str] = "read_only_store"
STRICT_ACCESSOR: Final[str] = "reader_store"

# A floor under the derived scope rather than its definition. Three of these are the
# handlers that drifted, so a derivation that stopped answering fails here instead of
# passing over an empty set. `certificate_verify` is the fourth for the opposite reason: it
# is the handler the disposition reading dropped, and a floor is what makes that kind of
# silent narrowing fail.
COVERED_FLOOR: Final[frozenset[str]] = frozenset(
    {"approvals", "certificate_verify", "erase_console", "erase_stream"}
)

# The routes that record rows, which is what needs the handle that can write:
#
# * `erase_start` records the Erasure_Run and everything the engine commits under it —
#   the candidate set, the residue bands, and the per-Artifact Dispositions.
# * `approval_resolve` writes the principal, the decision, and the resolution instant onto
#   the `approval_queue` row it names.
#
# The other two mutations the table declares record nothing and are deliberately absent:
# the live certificate verification recomputes a digest from the stored certificate, and
# the logout route clears the session cookie without touching a row. Each carries
# `mutation=True` because a submission must carry this session's CSRF token, which is a
# different fact about a route than whether it stores anything.
ROW_RECORDING_ROUTES: Final[frozenset[str]] = frozenset({"erase_start", "approval_resolve"})

# The pre-fix spelling, kept as text so the detector is shown to answer on it without any
# module in the tree having to carry it.
PRE_FIX_SOURCE: Final[str] = (
    "async def erase_stream(request):\n    return progress_of(console.store, attempt)\n"
)

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def records_rows(spec: routing.RouteSpec) -> bool:
    """Whether a route stores anything, which is what earns the handle that can write.

    Named routes rather than a reading of the demonstration disposition: every mutating
    route declares the blocking disposition now, so that field can no longer separate a
    recorded run from a submission that recomputes a digest.
    """
    return spec.name in ROW_RECORDING_ROUTES


def non_writing_view_routes() -> tuple[str, ...]:
    """Every declared route that records no row and is claimed inside the view package.

    Importing the view package attaches the handlers, so the registry read here is the one
    the application is built from rather than a list assembled beside it. A route the
    table declares but nothing has claimed yet is left out: it answers `501` until a view
    exists, and there is no source to hold to anything.
    """
    assert routes.__all__, "the view package names no module, so nothing registered"
    claimed: list[str] = []
    for spec in routing.ROUTE_TABLE:
        handler = routing.HANDLERS.get(spec.name)
        if handler is None or records_rows(spec):
            continue
        if handler.__module__.startswith(f"{VIEW_PACKAGE}."):
            claimed.append(spec.name)
    return tuple(sorted(claimed))


def handler_source(route_name: str) -> tuple[Path, _FunctionNode]:
    """The file one route's handler lives in, and that handler's own parsed definition."""
    handler = routing.HANDLERS[route_name]
    origin = inspect.getsourcefile(handler)
    assert origin is not None, f"{route_name} resolves to a handler with no source file"
    path = Path(origin)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == handler.__name__
        ):
            return (path, node)
    raise AssertionError(f"{path.name} defines no module-level {handler.__name__}")


def wide_handle_lines(node: ast.AST) -> tuple[int, ...]:
    """Every line inside one definition that reaches an attribute named `store`."""
    return tuple(
        found.lineno
        for found in ast.walk(node)
        if isinstance(found, ast.Attribute) and found.attr == WIDE_HANDLE_ATTRIBUTE
    )


def narrow_accessors(node: ast.AST) -> frozenset[str]:
    """The narrow store accessors one definition calls, by name."""
    return frozenset(
        found.func.attr
        for found in ast.walk(node)
        if isinstance(found, ast.Call)
        and isinstance(found.func, ast.Attribute)
        and found.func.attr in {READ_ONLY_ACCESSOR, STRICT_ACCESSOR}
    )


NON_WRITING_VIEW_ROUTES: Final[tuple[str, ...]] = non_writing_view_routes()


def test_the_scope_covers_every_handler_this_gate_is_known_to_be_needed_for() -> None:
    """The gate asserts something: the derivation still names the handlers it is for."""
    missing = sorted(COVERED_FLOOR - set(NON_WRITING_VIEW_ROUTES))
    assert not missing, (
        f"the derivation no longer covers {missing}, so this gate stopped enforcing the "
        "invariant on handlers it is known to be needed for"
    )


def test_every_route_named_as_recording_rows_is_declared_a_mutation() -> None:
    """The exempting set is held to the table, so it cannot become an exception list.

    A route that stores something is a submission, and the table is what says which routes
    are submissions. Naming a listing here — or a name the table does not declare — would
    exempt a handler from the rule for a reason the table denies.
    """
    declared = {spec.name: spec for spec in routing.ROUTE_TABLE}
    undeclared = sorted(ROW_RECORDING_ROUTES - declared.keys())
    assert not undeclared, f"{undeclared} record rows according to this gate and no route"
    listings = sorted(name for name in ROW_RECORDING_ROUTES if not declared[name].mutation)
    assert not listings, (
        f"the route table declares {listings} as carrying no mutation, so naming them here "
        "exempts their handlers from the rule for a reason the table denies"
    )


def test_the_demonstration_disposition_cannot_tell_a_recorded_row_from_a_token() -> None:
    """Why the disposition is no longer read: it answers the same for every mutation.

    The table requires a route declaring `mutation=True` to declare the blocking
    disposition, so that field distinguishes nothing among the mutations. Stated here so
    the next reader does not restore the old reading and quietly drop a module from scope.
    """
    dispositions = {spec.demo for spec in routing.ROUTE_TABLE if spec.mutation}
    assert dispositions == {routing.DemoDisposition.BLOCKED}, (
        "the mutations no longer all declare the blocking disposition, so the table has "
        "changed in a way this classification's reasoning depends on"
    )


def test_a_submission_that_records_nothing_stays_inside_the_covered_set() -> None:
    """A token-carrying route that stores nothing is held to the read-only rule.

    This is the case the old disposition reading lost: a mutation recomputing a digest is
    a read as far as the handle is concerned, and its handler must stay covered.
    """
    covered = set(NON_WRITING_VIEW_ROUTES)
    missing = sorted(
        spec.name
        for spec in routing.ROUTE_TABLE
        if spec.mutation
        and spec.name not in ROW_RECORDING_ROUTES
        and (handler := routing.HANDLERS.get(spec.name)) is not None
        and handler.__module__.startswith(f"{VIEW_PACKAGE}.")
        and spec.name not in covered
    )
    assert not missing, (
        f"{missing} carry a CSRF token and record no row, so each must be held to the "
        "read-only rule rather than exempted from it"
    )


def test_the_routes_that_record_rows_are_left_the_handle_that_can_write() -> None:
    """A route recording a row needs the wider handle, so the scope excludes it."""
    overreach = sorted(ROW_RECORDING_ROUTES & set(NON_WRITING_VIEW_ROUTES))
    assert not overreach, f"{overreach} record rows, so each needs the handle that can write"


@pytest.mark.parametrize("route_name", NON_WRITING_VIEW_ROUTES)
def test_a_handler_that_writes_nothing_never_reaches_the_wider_handle(route_name: str) -> None:
    """No non-writing handler names `store`, so none reads through a role that can write."""
    path, node = handler_source(route_name)
    offending = wide_handle_lines(node)
    assert not offending, (
        f"the {route_name} handler in {path.name} reaches the wider handle at line(s) "
        f"{list(offending)}. A handler that writes nothing reads through "
        f"{READ_ONLY_ACCESSOR}() — or {STRICT_ACCESSOR}() where the analysis refuses a "
        "wider role — so its read-only posture is the connection's privilege rather than "
        "the module's habit."
    )


def test_the_detector_answers_on_the_spelling_this_gate_was_written_against() -> None:
    """The pre-fix spelling is flagged, so a passing suite is not a silent detector.

    A gate that reported nothing whatever it read would pass over the very lapse it exists
    for. The source below is the shape the stream handler carried before the fix.
    """
    flagged = wide_handle_lines(ast.parse(PRE_FIX_SOURCE))
    assert flagged, "the detector does not flag the wide handle it exists to find"
    assert not narrow_accessors(ast.parse(PRE_FIX_SOURCE)), (
        "the pre-fix source names no narrow accessor, so nothing here should find one"
    )
