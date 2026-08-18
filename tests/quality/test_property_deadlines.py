"""Every property module states no per-example deadline, and this is why.

A Hypothesis deadline fails an example that took too long in wall-clock time. That is a
latency assertion, and wall-clock time in this suite is a function of how much else is
running: the same example took a second under parallel execution and twenty milliseconds
on its own. So a deadline here does not make a property stricter, it makes the property
suite report load as a correctness failure — which is exactly what happened to two
modules once the suites were run in parallel, both of which assert properties that held.

Latency is measured deliberately elsewhere. The performance suite states each bound the
requirements name, times only the section the bound is about, and reports the figure. A
property module has no business asserting a duration as a side effect of asserting an
invariant.

So the convention is that every property module passes `deadline=None`, and this module
is the reason the convention cannot quietly lapse: a module added without it would pass
on an idle machine and fail on a busy one, which is the least useful failure a suite can
produce.

**Absence of a settings decorator is the failure, not an exemption.** This gate used to
treat a property carrying no `settings` decorator as taking the library's defaults
deliberately, and that reading was exactly backwards: the default carries a wall-clock
deadline, so an unconfigured property is precisely the module that passes idle and fails
busy, with no line in it a reviewer could point at. The two claims below are therefore
separate. Every property — every function carrying `@given` — must carry a `settings`
decorator, and every `settings` decorator anywhere in the module must disable the
deadline. Neither claim implies the other: a module could configure one property and
leave a second unconfigured, and a module could configure every property while leaving
one decorator's deadline in place.

The check reads the source rather than the runtime settings. A decorator's arguments are
what a reviewer sees and what a new module is copied from, and reading them as text needs
no module import, so a property module that cannot be imported without a cluster is still
covered.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.quality

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PROPERTY_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "tests" / "property"

# The decorator that configures a property, the decorator that makes a function one, and
# the argument that must appear in the configuration.
SETTINGS_NAME: Final[str] = "settings"
GIVEN_NAME: Final[str] = "given"
DEADLINE_ARGUMENT: Final[str] = "deadline"

_Function = ast.FunctionDef | ast.AsyncFunctionDef


def property_modules() -> tuple[Path, ...]:
    """Every property module, in a stable order."""
    return tuple(sorted(PROPERTY_DIRECTORY.glob("test_p*.py")))


def _named(decorator: ast.expr, name: str) -> bool:
    """Whether one decorator names a callable of this name, called or bare."""
    reached = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(reached, ast.Name):
        return reached.id == name
    return isinstance(reached, ast.Attribute) and reached.attr == name


def _functions(tree: ast.Module) -> tuple[_Function, ...]:
    """Every function definition anywhere in a parsed module."""
    return tuple(node for node in ast.walk(tree) if isinstance(node, _Function))


def _settings_calls(tree: ast.Module) -> tuple[ast.Call, ...]:
    """Every `settings(...)` decorator call anywhere in a parsed module."""
    return tuple(
        decorator
        for node in _functions(tree)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call) and _named(decorator, SETTINGS_NAME)
    )


def _unconfigured_properties(tree: ast.Module) -> tuple[tuple[str, int], ...]:
    """Every `@given` function carrying no `settings` decorator, by name and line.

    A bare `@given` is included as well as a called one, because either form makes the
    function a property and neither form carries a deadline of its own.
    """
    return tuple(
        (node.name, node.lineno)
        for node in _functions(tree)
        if any(_named(decorator, GIVEN_NAME) for decorator in node.decorator_list)
        and not any(_named(decorator, SETTINGS_NAME) for decorator in node.decorator_list)
    )


def _disables_deadline(call: ast.Call) -> bool:
    """Whether one settings call passes `deadline=None`."""
    for keyword in call.keywords:
        if keyword.arg == DEADLINE_ARGUMENT:
            return isinstance(keyword.value, ast.Constant) and keyword.value.value is None
    return False


def test_the_property_directory_holds_modules_to_check() -> None:
    """The check is worthless if it silently found nothing to check."""
    assert property_modules(), "no property module was found, so this gate asserts nothing"


@pytest.mark.parametrize("module", property_modules(), ids=lambda path: path.stem)
def test_every_configured_property_disables_the_deadline(module: Path) -> None:
    """A property module states `deadline=None` on every settings decorator it carries.

    What is refused is a decorator that configures examples and leaves the wall-clock
    deadline in place, because that is the combination that passes idle and fails busy.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    calls = _settings_calls(tree)
    offending = [call.lineno for call in calls if not _disables_deadline(call)]
    assert not offending, (
        f"{module.name} configures a property at line(s) {offending} without "
        f"{DEADLINE_ARGUMENT}=None. A wall-clock deadline in this suite fails on a busy "
        "machine and passes on an idle one; state the bound in the performance suite "
        "instead."
    )


@pytest.mark.parametrize("module", property_modules(), ids=lambda path: path.stem)
def test_every_property_carries_a_settings_decorator_at_all(module: Path) -> None:
    """No property is left taking the library's default, which carries a deadline.

    This is the case the gate used to exempt, on the reading that an unconfigured
    property took the defaults deliberately. The default deadline is a wall-clock bound,
    so an unconfigured property is the exact failure mode the convention exists to
    prevent — and the worst-shaped one, because there is no argument in the module for a
    reviewer to question. A property carrying `settings` is then held to the deadline
    claim above, so the two cases together leave no property unbounded and none bounded
    by the clock.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    unconfigured = _unconfigured_properties(tree)
    assert not unconfigured, (
        f"{module.name} carries {[name for name, _ in unconfigured]} at line(s) "
        f"{[line for _, line in unconfigured]} decorated with {GIVEN_NAME} and no "
        f"{SETTINGS_NAME} decorator, so each takes the default per-example deadline. "
        f"State {SETTINGS_NAME}(max_examples=..., {DEADLINE_ARGUMENT}=None) on it."
    )


def test_the_detector_finds_a_property_that_configures_nothing() -> None:
    """The gate is shown to answer on the shape it exists to catch.

    A gate whose new case reported nothing whatever it read would pass over the very
    exemption it replaces, so the two shapes are put to it here as text rather than
    left to whichever module happens to carry them.
    """
    unconfigured = (
        "@given(value=integers())\ndef test_a_property(value):\n    assert value == value\n"
    )
    configured = (
        "@settings(max_examples=100, deadline=None)\n"
        "@given(value=integers())\n"
        "def test_a_property(value):\n"
        "    assert value == value\n"
    )
    assert _unconfigured_properties(ast.parse(unconfigured)) == (("test_a_property", 2),)
    assert _unconfigured_properties(ast.parse(configured)) == ()
