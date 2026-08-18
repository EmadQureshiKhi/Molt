"""A console view module that only reads takes the narrowest handle, enforced here.

The deployed console function authenticates as the eraser role, because the erasure
console runs erasures from that same function. Most of the console does not erase
anything: it lists Sessions, walks lineage, counts tiers, reports retention. Those views
ran on the eraser handle for no reason other than that it was the handle in scope, which
made their read-only-ness a habit of each module rather than a privilege the cluster
holds them to.

`Console.read_only_store()` is the seam that fixes it, and this gate is the reason the
fix cannot decay. It decayed once already: the reader handle was added for the
Sensitivity_Analyzer, wired into one view, and three other read-only views kept reaching
for `store` afterwards — the same mistake surviving in three places after being fixed in
one. An invariant that has to be remembered is one that lapses, so it is checked.

**The scope is the whole module, which is the difference between this gate and the
handler-scoped one.** A module is checked when nothing it registers records rows, and
then every line of its source is held to the rule: module-level helpers, row decoders,
and the shared readers a view calls. A helper reaching for the wider handle takes the
choice away from every view that calls it, which is how the Client roster read came to
run on the eraser handle in every view that shows a picker. Modules registering no route
at all are checked for that reason. The companion gate under `tests/unit/` scopes each
non-writing handler's own body instead, which is the precise answer for a module holding
both a listing and a submission; the two scopes catch different lapses and both are kept.

**Recording rows is what exempts a module, and the demonstration disposition no longer
answers that question.** It used to: a mutation the table blocked in a demonstration
recorded rows, while a mutation it admitted as read-only was a form submission that
merely carries this session's CSRF token. The route table then grew an import-time
invariant requiring every route declaring `mutation=True` to declare `BLOCKED`, because a
demonstration exposes no mutation route at all. That reading went degenerate the moment it
landed: the live certificate verification, which recomputes a digest and stores nothing,
now declares the same disposition as the route that records an erasure run, and the
certificates module silently fell out of this gate's scope.

So the routes that record rows are named below as a set, with the row each one writes.
An explicit set risks becoming an exception list nobody rechecks, so it is held to the
table: every route it names must be declared a mutation there, which is the table's own
statement that the route is a submission rather than a listing. A set that named a
listing, or a route the table does not declare at all, fails rather than exempts.

The check reads source rather than behaviour. Reaching a store is a spelling — an
attribute named `store` — and a static answer covers every code path including the ones
a test would need a cluster to reach.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

from molt.console import routes, routing

pytestmark = pytest.mark.quality

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ROUTES_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "src" / "molt" / "console" / "routes"

# The attribute a wider handle is reached through, and the two accessors that are
# permitted instead. `read_only_store` prefers the read-only connection and answers with
# the primary one where a deployment configures none; `reader_store` refuses instead,
# which is what an analysis that itself rejects a wider role needs.
WIDE_HANDLE_ATTRIBUTE: Final[str] = "store"
READ_ONLY_ACCESSOR: Final[str] = "read_only_store"
STRICT_ACCESSOR: Final[str] = "reader_store"

# The view whose analyser refuses a wider role outright. It must take the strict
# accessor: falling back to the primary handle would leave the grid rendering under a
# role that can write, which is the guarantee the analysis exists to carry.
STRICT_MODULE: Final[str] = "sensitivity"

# The routes that record rows, which is what needs the handle that can write:
#
# * `erase_start` records the Erasure_Run and everything the engine commits under it —
#   the candidate set, the residue bands, and the per-Artifact Dispositions.
# * `approval_resolve` writes the principal, the decision, and the resolution instant
#   onto the `approval_queue` row it names.
#
# The other two mutations the table declares record nothing and are deliberately absent:
# the live certificate verification recomputes a digest from the stored certificate, and
# the logout route clears the session cookie without touching a row. Both carry
# `mutation=True` because a form submission must carry this session's CSRF token, which
# is a different fact about a route than whether it stores anything.
ROW_RECORDING_ROUTES: Final[frozenset[str]] = frozenset({"erase_start", "approval_resolve"})

# Modules that are expected to be in the checked set. Not the definition of the set —
# that is derived below — but a floor under it, so a bug that made the derivation
# return nothing would fail here rather than pass silently.
EXPECTED_READ_ONLY_MODULES: Final[frozenset[str]] = frozenset(
    {
        "certificates",
        "fleet",
        "lineage",
        "procedures",
        "residue",
        "retention",
        "runs",
        "sessions",
        "tiers",
    }
)


def _module_routes() -> dict[str, tuple[str, ...]]:
    """Every route each view module claims, keyed by the module's own name.

    Importing the package attaches every handler, so the registry is the deployed
    answer rather than a list assembled here.
    """
    assert routes.__all__, "the view package names no module, so nothing registered"
    claimed: dict[str, list[str]] = {}
    for name, handler in routing.HANDLERS.items():
        module = handler.__module__.rsplit(".", 1)[-1]
        claimed.setdefault(module, []).append(name)
    return {module: tuple(sorted(names)) for module, names in claimed.items()}


def read_only_modules() -> tuple[Path, ...]:
    """Every module under `routes/` that records no row, in a stable order.

    A module holding a route that records rows is exempt: it needs the handle that can
    write, and the precondition it reads beside its write is one connection rather than
    two. A module claiming nothing is a shared helper, and is checked for the reason
    given in this module's own docstring.

    Which routes record rows is `ROW_RECORDING_ROUTES` above rather than a reading of the
    demonstration disposition, which the table's fourth import-time invariant left unable
    to tell a recorded run apart from a token-carrying read.
    """
    claimed = _module_routes()
    return tuple(
        sorted(
            path
            for path in ROUTES_DIRECTORY.glob("*.py")
            if path.stem != "__init__"
            and not ROW_RECORDING_ROUTES.intersection(claimed.get(path.stem, ()))
        )
    )


def _wide_handle_lines(tree: ast.Module) -> tuple[int, ...]:
    """Every line reaching an attribute named `store`, which is the wider handle."""
    return tuple(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == WIDE_HANDLE_ATTRIBUTE
    )


def _called_accessors(tree: ast.Module) -> frozenset[str]:
    """The store accessors a module calls, by name."""
    return frozenset(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {READ_ONLY_ACCESSOR, STRICT_ACCESSOR}
    )


def test_every_route_named_as_recording_rows_is_declared_a_mutation() -> None:
    """The exempting set is held to the table, so it cannot become an exception list.

    A route that records rows is a submission, and the table says which routes are
    submissions. Naming a listing here — or a route the table does not declare at all —
    would exempt a module from the rule by an opinion the table does not share, which is
    exactly the decay an explicit set invites.
    """
    declared = {spec.name: spec for spec in routing.ROUTE_TABLE}
    undeclared = sorted(ROW_RECORDING_ROUTES - declared.keys())
    assert not undeclared, f"{undeclared} record rows according to this gate and no route"
    listings = sorted(name for name in ROW_RECORDING_ROUTES if not declared[name].mutation)
    assert not listings, (
        f"the route table declares {listings} as carrying no mutation, so naming them "
        "here exempts their modules from the rule for a reason the table denies"
    )


def test_the_derivation_finds_the_read_only_views_it_is_meant_to_cover() -> None:
    """The gate asserts nothing if the classification stopped answering."""
    found = {path.stem for path in read_only_modules()}
    missing = sorted(EXPECTED_READ_ONLY_MODULES - found)
    assert not missing, (
        f"the classification no longer treats {missing} as recording no row, so this "
        "gate stopped covering them"
    )


@pytest.mark.parametrize("module", read_only_modules(), ids=lambda path: path.stem)
def test_a_module_recording_no_row_never_reaches_the_wider_handle(module: Path) -> None:
    """No view that only reads names `store`, so none can read through a role that writes."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    offending = _wide_handle_lines(tree)
    assert not offending, (
        f"{module.name} reaches the wider handle at line(s) {list(offending)}. A module "
        f"claiming no route that records rows reads through {READ_ONLY_ACCESSOR}(), so "
        "the read-only posture is the privilege the connection holds rather than a habit "
        "of the module."
    )


def test_the_analysis_that_refuses_a_wider_role_takes_the_strict_accessor() -> None:
    """The one view whose component refuses a wider role must not fall back to one.

    `read_only_store()` answers with the primary handle where no read-only connection is
    configured, which is right for a listing and wrong here: the Sensitivity_Analyzer
    rejects that handle anyway, and a view that offered it would turn a provisioning gap
    into a stack trace instead of the stated refusal the grid renders.
    """
    module = ROUTES_DIRECTORY / f"{STRICT_MODULE}.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    called = _called_accessors(tree)
    assert STRICT_ACCESSOR in called, f"{module.name} does not take the read-only connection"
    assert READ_ONLY_ACCESSOR not in called, (
        f"{module.name} falls back to the primary handle, which the analyser refuses"
    )
