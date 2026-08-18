"""Gate assertion that every configuration read names a setting the surface declares.

The configuration surface gives every setting two spellings: an environment variable
name and a dotted key. The accessors resolve by the environment name alone, so passing
the key is not a second way of asking the same question — it is a read that can never
succeed, and it fails at the moment the value is first needed rather than at start-up.

That defect shipped. The capability probe named two settings by their keys, so every
attempt to build a model provider raised an unknown-setting fault. The failure was quiet
rather than loud: the probe reports a provider it cannot build as a warning and still
exits successfully, so the prompt-cache capability was left unprobed on every cluster
the probe had ever run against, and the text provider always took the unprobed path.
Nothing failed, and the record simply had a hole in it.

Both spellings are legitimate strings to hold, which is why this reads the call sites
rather than searching for keys. What is asserted is narrow and mechanical: wherever a
literal string is passed to one of the accessors, that literal is an environment name
the surface declares. A key passed there fails, naming the setting and the spelling it
should have used.

The surface is imported and the sources are read as tracked text. Nothing is executed,
so this needs no cluster, no credential, and no cloud account.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from molt.config.resolve import SECRET_SETTINGS, SETTINGS

# Static gate over tracked files: no reachable instance and no credential.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.quality

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SEARCHED: Final[tuple[Path, ...]] = (
    REPOSITORY_ROOT / "src" / "molt",
    REPOSITORY_ROOT / "scripts",
)

# The accessors that resolve a setting by its environment name. Every one of them takes
# that name as its first argument, so a literal in that position is checkable.
ACCESSORS: Final[frozenset[str]] = frozenset(
    {
        "value",
        "optional",
        "text",
        "optional_text",
        "text_list",
        "integer",
        "flag",
        "path",
        "optional_path",
        "environment_value",
    }
)

# Every environment name a read may legitimately name, and the key-to-name mapping so a
# misuse can be reported as the exact substitution to make rather than as an
# unrecognised string.
#
# The secret settings are included alongside the general surface because they are read
# the same way and are deliberately absent from it: a bearer token, an ingress secret,
# and a direct connection string are accepted from the environment alone so that none of
# them can be committed to a configuration file. They have an environment name and no
# key, which is the whole point, so a read naming one is correct and this gate has to
# know that.
DECLARED_ENV: Final[frozenset[str]] = frozenset(setting.env for setting in SETTINGS) | frozenset(
    setting.env for setting in SECRET_SETTINGS
)
ENV_BY_KEY: Final[dict[str, str]] = {setting.key: setting.env for setting in SETTINGS}

# The prefix every environment name of this surface carries. A literal without it is
# not a setting name at all — it is some other string argument to a method that happens
# to share a name with an accessor — so it is not this gate's business.
ENVIRONMENT_PREFIX: Final[str] = "MOLT_"


@dataclass(frozen=True, slots=True)
class Read:
    """One configuration read written with a literal setting name."""

    module: str
    line: int
    accessor: str
    named: str


def _reads(path: Path) -> tuple[Read, ...]:
    """Every configuration read in one module whose setting name is a literal."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[Read] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ACCESSORS or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        found.append(
            Read(
                module=str(path.relative_to(REPOSITORY_ROOT)),
                line=node.lineno,
                accessor=node.func.attr,
                named=first.value,
            )
        )
    return tuple(found)


def _all_reads() -> tuple[Read, ...]:
    return tuple(
        read
        for directory in SEARCHED
        for path in sorted(directory.rglob("*.py"))
        for read in _reads(path)
    )


READS: Final[tuple[Read, ...]] = _all_reads()

# The reads this gate judges: those naming something that looks like a setting at all.
# A dotted key is included deliberately, because that is the defect being refused.
CANDIDATES: Final[tuple[Read, ...]] = tuple(
    read for read in READS if read.named.startswith(ENVIRONMENT_PREFIX) or read.named in ENV_BY_KEY
)


def test_the_detector_finds_configuration_reads_at_all() -> None:
    """The detector is checked before what it detects.

    Every assertion below iterates reads found by walking the syntax tree. A change to
    the accessor names, or to how a setting name is written at the call site, would
    empty that set and leave this file passing while checking nothing. So the set is
    asserted to be non-empty, and to be drawn from more than one module, since a gate
    that only ever sees one file would not have caught the defect that motivated it.
    """
    assert CANDIDATES, (
        "no configuration read naming a literal setting was found, so either the "
        "accessors were renamed or setting names stopped being written at the call site"
    )
    modules = {read.module for read in CANDIDATES}
    assert len(modules) > 1, f"configuration reads were found in only {modules}"


def test_every_configuration_read_names_an_environment_name_the_surface_declares() -> None:
    """The defect itself, stated as the thing that fails.

    A key in this position resolves to nothing. The report names the substitution
    rather than only the fault, because the two spellings sit side by side in the
    surface and the fix is always the same one line.
    """
    for read in CANDIDATES:
        if read.named in DECLARED_ENV:
            continue
        instead = ENV_BY_KEY.get(read.named)
        detail = (
            f"the key of {instead}, which is the spelling that resolves"
            if instead is not None
            else "no setting of the surface"
        )
        raise AssertionError(
            f"{read.module}:{read.line} reads configuration as "
            f"{read.accessor}({read.named!r}), which names {detail}; the accessors "
            "resolve by environment name, so this read raises rather than answering"
        )


def test_no_read_names_a_setting_by_its_dotted_key() -> None:
    """The narrower claim, so the report is unambiguous when both would fail.

    The case above would already fail on a key, but its message has to cover a name
    that is simply unknown as well. This one exists so that the common mistake — a
    spelling that is real, declared, and in the wrong position — is named as such.
    """
    misused = tuple(read for read in CANDIDATES if read.named in ENV_BY_KEY)
    assert not misused, (
        "these reads name a setting by its dotted key where the environment name is "
        "required: "
        + "; ".join(
            f"{read.module}:{read.line} {read.named} should be {ENV_BY_KEY[read.named]}"
            for read in misused
        )
    )
