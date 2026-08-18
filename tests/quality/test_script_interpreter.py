"""Gate assertion on how every shell script of this repository resolves an interpreter.

A script that runs one of this repository's own Python helpers has to run it under an
interpreter holding the pinned dependencies and the installed package. The platform
interpreter holds neither. Naming it unconditionally is therefore not a default that
degrades gracefully: the helper dies on an absent module, and what the operator sees
is a missing import rather than anything about the deployment or the provisioning that
was under way.

That defect was found and fixed twice in the same shape. The deployment script pinned
the platform interpreter, so the parameter validator crashed on every stack; the fix
was the resolution order the test runner already used. Five more scripts held the same
line, unnoticed, because nothing compared them — including the two provisioning
scripts, where the capability probe imports the package and would have left a cluster
provisioned with no capability record.

So the assertion here is not that a particular script is right. It is that every
script resolving an interpreter resolves it the same way, in the same order, and that
none of them pins one unconditionally. A sixth copy of the defect fails this, and so
does a seventh script that introduces a different order — which is the drift that made
the first two fixes look like isolated repairs.

The scripts are read as tracked text. Nothing is executed, so this needs no
credential, no cluster, and no cloud account.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

# Every directory holding shell scripts. Both are read, because the defect appeared in
# each: the deployment and teardown scripts sit beside the templates, and the
# provisioning and packaging scripts sit with the tools.
SCRIPT_DIRECTORIES: Final[tuple[Path, ...]] = (
    REPOSITORY_ROOT / "scripts",
    REPOSITORY_ROOT / "infra",
)

# The assignment an interpreter is named by. Read as an assignment rather than by
# matching a whole conditional, so a script that resolves the interpreter some other
# way is still read here rather than skipped for not matching a shape.
ASSIGNMENT: Final[re.Pattern[str]] = re.compile(r'^\s*(?:readonly\s+)?PYTHON="([^"]*)"', re.M)

# The guard the override branch has to be written with. Every script sets the
# unset-variable option, so testing the override without a default expansion aborts
# the script on a machine that has not set it — which is most machines.
OVERRIDE_GUARD: Final[str] = '"${MOLT_PYTHON:-}"'


class Candidate(StrEnum):
    """One interpreter a script may resolve to, named by where it comes from."""

    OVERRIDE = "an interpreter named by the environment"
    SHARED_ENVIRONMENT = "the environment outside the working tree"
    TREE_ENVIRONMENT = "an environment inside the working tree"
    PLATFORM = "the platform interpreter"


# The order every script resolves in, most specific first. The platform interpreter is
# last and is reached only when nothing else exists, which is the whole point: it is a
# fallback that reports its own absence of dependencies, not a default.
RESOLUTION_ORDER: Final[tuple[Candidate, ...]] = (
    Candidate.OVERRIDE,
    Candidate.SHARED_ENVIRONMENT,
    Candidate.TREE_ENVIRONMENT,
    Candidate.PLATFORM,
)

# How each candidate is recognised in an assignment's value, most specific first. The
# shared environment's marker is tested before the working tree's because the first
# contains the second as a substring.
MARKERS: Final[tuple[tuple[str, Candidate], ...]] = (
    ("MOLT_PYTHON", Candidate.OVERRIDE),
    (".molt-venv/bin/python", Candidate.SHARED_ENVIRONMENT),
    (".venv/bin/python", Candidate.TREE_ENVIRONMENT),
)
PLATFORM_INTERPRETER: Final[str] = "python3.12"


@dataclass(frozen=True, slots=True)
class Script:
    """One shell script, reduced to the interpreters it resolves between."""

    name: str
    text: str

    @property
    def assigned(self) -> tuple[str, ...]:
        """Every value the interpreter is assigned, in the order the file states them."""
        return tuple(ASSIGNMENT.findall(self.text))

    @property
    def resolved(self) -> tuple[Candidate, ...]:
        """The same values read as the candidates they name."""
        return tuple(_candidate(value) for value in self.assigned)


def _candidate(value: str) -> Candidate:
    """Which interpreter one assigned value names."""
    for marker, candidate in MARKERS:
        if marker in value:
            return candidate
    return Candidate.PLATFORM


def _scripts() -> tuple[Script, ...]:
    """Every shell script of the repository, by name, read once."""
    found = [
        Script(path.name, path.read_text(encoding="utf-8"))
        for directory in SCRIPT_DIRECTORIES
        for path in sorted(directory.glob("*.sh"))
    ]
    assert found, "no shell script was found, so this gate would pass over nothing"
    return tuple(found)


SCRIPTS: Final[tuple[Script, ...]] = _scripts()
RESOLVING: Final[tuple[Script, ...]] = tuple(script for script in SCRIPTS if script.assigned)


def test_some_script_resolves_an_interpreter_at_all() -> None:
    """The detector is checked before what it detects.

    Every assertion below iterates the scripts that name an interpreter. A change to
    the assignment this reads for — a different variable name, a different quoting
    style — would empty that set and leave the rest of this file passing while
    checking nothing. So the set is asserted to be non-empty, and its size is
    asserted to be more than one, because the claim the gate makes is about agreement
    between scripts and one script agrees with itself trivially.
    """
    assert len(RESOLVING) > 1, (
        "fewer than two scripts name an interpreter, so either the repository stopped "
        "resolving one or the assignment this gate reads for changed shape; the "
        f"scripts read were {', '.join(script.name for script in SCRIPTS)}"
    )


def test_no_script_pins_an_interpreter_unconditionally() -> None:
    """The defect itself, stated as the thing that fails.

    One assignment and no alternatives is a pin. It is the line that shipped in six
    scripts, and what makes it worse than a wrong default is that it cannot be
    overridden: an operator with a working environment has no way to name it, so the
    only fix is editing the script.
    """
    for script in RESOLVING:
        assert len(script.assigned) > 1, (
            f"{script.name} names {script.assigned[0]} and nothing else, so it pins an "
            "interpreter that need hold none of the pinned dependencies and offers no "
            "way to name another; resolve in the shared order instead"
        )


def test_every_script_resolves_its_interpreter_in_the_same_order() -> None:
    """The agreement that makes one machine behave one way.

    Order is the substance, not the set. A script preferring the platform interpreter
    ahead of an environment would hold all four candidates and still fail every helper
    on a machine where both exist, which is every machine a helper has ever run on.
    Asserting the sequence rather than the membership is what refuses that.

    An override comes first because a machine whose environment is somewhere else has
    no other way to say so. The shared environment comes before the working tree's
    because it is the one the test runner creates and the one a checkout under a path
    holding a space can still name. The platform interpreter comes last.
    """
    for script in RESOLVING:
        assert script.resolved == RESOLUTION_ORDER, (
            f"{script.name} resolves its interpreter as "
            f"{' then '.join(script.resolved)}, where every other script resolves "
            f"{' then '.join(RESOLUTION_ORDER)}; a script that differs makes the "
            "interpreter depend on which tool was invoked"
        )


def test_every_override_is_read_with_a_default_expansion() -> None:
    """The branch that would abort the script it is meant to make configurable.

    Every one of these scripts sets the unset-variable option, which is what makes a
    typo in a variable name a failure rather than an empty string. The cost is that
    testing an optional variable without a default expansion aborts on the test
    itself, so the override branch reads the variable with one. Without it the
    scripts fail on any machine that has not set the override, which is the common
    case rather than the rare one.
    """
    for script in RESOLVING:
        assert OVERRIDE_GUARD in script.text, (
            f"{script.name} resolves an override without reading it as "
            f"{OVERRIDE_GUARD}, so the script aborts on the unset variable it means "
            "to treat as optional"
        )


def test_the_platform_interpreter_is_named_once_and_last() -> None:
    """The fallback stays a fallback.

    Naming the platform interpreter twice, or naming it before an environment, is how
    a resolution chain quietly becomes a pin again: the earlier branch wins and the
    later ones are unreachable. This reads the assignments rather than the branch
    conditions, so an unreachable branch is caught by position instead of being
    trusted to be ordered correctly.
    """
    for script in RESOLVING:
        platform = tuple(
            index for index, value in enumerate(script.assigned) if value == PLATFORM_INTERPRETER
        )
        assert platform == (len(script.assigned) - 1,), (
            f"{script.name} names {PLATFORM_INTERPRETER} at position(s) {platform} of "
            f"{len(script.assigned)}, rather than once and last; every branch after it "
            "is unreachable"
        )
