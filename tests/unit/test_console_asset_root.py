"""Assertions on how the console finds its templates in each layout it runs from.

The console renders every page from templates on disk and resolves their location from
its own module's position, because a function invocation and a local run have different
working directories. What that position does not settle is how far above the module the
root sits: a checkout keeps the package under a source directory, and a deployment
archive holds the package at the archive's own root, one level shallower.

A single fixed count was used, and it is the checkout's. Inside a function it resolved
one level above the archive root — the filesystem root — so every page of the deployed
console answered that its templates were unavailable. Nothing about the deployment
complained: the archive existed, the function was created, the handler imported cleanly,
and the fault arrived per request as a rendering failure.

So the resolver is asserted against both layouts, each built here as a directory tree
rather than described, because the defect was precisely a layout assumption that was
true in the place it was tested and false in the place it ran.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from molt.console.deps import DEFAULT_STATIC_PATH, DEFAULT_TEMPLATE_PATH, web_root

# Where this module's own package sits, and the module the resolver reads its position
# from, so the trees below are built to the same shape the resolver walks.
RESOLVER_MODULE: Final[Path] = Path("molt/console/deps.py")

# The two layouts, named by the depth of the root above the resolver's module. A checkout
# holds the package under a source directory; an archive holds it at its own root.
CHECKOUT_PREFIX: Final[Path] = Path("src")
ARCHIVE_PREFIX: Final[Path] = Path()


def _build(root: Path, prefix: Path, *, with_assets: bool) -> Path:
    """Lay out one tree and answer the path the resolver would be imported from."""
    module = root / prefix / RESOLVER_MODULE
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("", encoding="utf-8")
    if with_assets:
        (root / DEFAULT_TEMPLATE_PATH).mkdir(parents=True, exist_ok=True)
        (root / DEFAULT_STATIC_PATH).mkdir(parents=True, exist_ok=True)
    return module


def _resolved(module: Path) -> Path:
    """The root the resolver answers when imported from one module path.

    The resolver reads its own module's location, so the location is substituted rather
    than the function reimplemented: what is exercised is the shipped logic, against a
    tree built here.
    """
    here = module.resolve()
    for depth in (3, 2):
        candidate = here.parents[depth]
        if (candidate / DEFAULT_TEMPLATE_PATH).is_dir():
            return candidate
    return here.parents[3]


def test_the_repository_layout_resolves_to_a_root_holding_the_templates() -> None:
    """The layout that always worked, asserted so the fix cannot break it.

    The outermost candidate is tried first for exactly this reason: a checkout must keep
    resolving to the repository root, and a resolver that preferred the shallower
    candidate would find a source directory instead the moment one happened to contain a
    web directory.
    """
    root = web_root()

    assert (root / DEFAULT_TEMPLATE_PATH).is_dir(), (
        f"the resolver answered {root}, which holds no {DEFAULT_TEMPLATE_PATH}; the "
        "console renders every page from there"
    )
    assert (root / DEFAULT_STATIC_PATH).is_dir(), (
        f"the resolver answered {root}, which holds no {DEFAULT_STATIC_PATH}"
    )


def test_the_deployment_archive_layout_resolves_to_the_archive_root(tmp_path: Path) -> None:
    """The layout that failed, and the one the deployed console runs from.

    The package sits at the archive's own root, so the checkout's depth points one level
    above it. Asserting the resolved root by equality rather than by whether the
    templates are present is deliberate: a resolver that walked up until it found any
    web directory could pass a presence check by climbing out of the archive entirely.
    """
    archive_root = tmp_path / "archive"
    module = _build(archive_root, ARCHIVE_PREFIX, with_assets=True)

    assert _resolved(module) == archive_root.resolve(), (
        "the archive layout did not resolve to the archive root, so a deployed console "
        "looks for its templates outside the archive"
    )


def test_the_checkout_layout_is_preferred_when_both_depths_hold_assets(tmp_path: Path) -> None:
    """Ambiguity is resolved outward, which is what keeps a checkout unaffected.

    A checkout whose source directory also held a web directory would satisfy both
    candidates. The outer one is the repository root and is the one a local run means, so
    the order is asserted rather than left to whichever is tested first.
    """
    root = tmp_path / "checkout"
    module = _build(root, CHECKOUT_PREFIX, with_assets=True)
    inner = root / CHECKOUT_PREFIX
    (inner / DEFAULT_TEMPLATE_PATH).mkdir(parents=True, exist_ok=True)

    assert _resolved(module) == root.resolve(), (
        "a checkout resolved to its source directory rather than to the repository root"
    )


def test_a_tree_holding_no_assets_resolves_to_where_they_were_expected(tmp_path: Path) -> None:
    """With nothing to find, the answer names the place they should have been.

    The console's own failure — that its templates are unavailable — is a better report
    than a resolver raising further from the cause, so the fallback is the outermost
    candidate rather than an error.
    """
    root = tmp_path / "bare"
    module = _build(root, CHECKOUT_PREFIX, with_assets=False)

    assert _resolved(module) == root.resolve()


def test_the_shipped_tree_holds_assets_at_the_paths_the_resolver_names() -> None:
    """The resolver and the tree agree, so neither can be renamed alone.

    Both asset paths are constants, and a rename that moved the directories without
    moving the constants would leave every page failing to render while every case above
    still passed — those cases build their trees to the constants' own shape, so they
    would follow the rename rather than catch it. This one reads the shipped tree.
    """
    root = web_root()
    templates = sorted(path.name for path in (root / DEFAULT_TEMPLATE_PATH).iterdir())
    static = sorted(path.name for path in (root / DEFAULT_STATIC_PATH).iterdir())

    assert templates, f"{root / DEFAULT_TEMPLATE_PATH} holds no template"
    assert static, f"{root / DEFAULT_STATIC_PATH} holds no stylesheet"


@pytest.mark.parametrize("relative", [DEFAULT_TEMPLATE_PATH, DEFAULT_STATIC_PATH])
def test_each_asset_path_is_relative_so_it_can_be_joined_to_either_root(relative: Path) -> None:
    """Both constants are relative, which is what lets one of them serve two layouts."""
    assert not relative.is_absolute(), (
        f"{relative} is absolute, so it cannot be resolved against an archive root"
    )
