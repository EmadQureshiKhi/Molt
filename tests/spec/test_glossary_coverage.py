"""Every component name the design and the README use is defined in the glossary.

Requirement 51.5 obliges a glossary under `docs/` defining every domain term, every
system component name, and every external service name the Repository documentation
uses. That is a coverage claim between documents, so it is checked between documents
rather than reviewed.

Three term sets are taken, each from a source that names it rather than from a list
written here:

1. **The design's own component inventory.** The design opens by naming every
   component in one sentence, so that sentence is parsed and each name is required
   to have an entry. This is the authoritative component list, and it includes the
   names that carry no underscore — the Collector, the Redactor, the Embedder, the
   Adjudicator, the CLI, the Telemetry component, and the rest — which no shape rule
   could recognise.
2. **The underscored spellings the specification documents use.** The spec spells its
   own vocabulary with underscore-joined capitalised words, so every such token in
   the design and in the requirements is required to have an entry. This is what
   makes a term added to either document later covered without this test being
   edited.
3. **The underscored spellings the README uses.** Taken separately from the same
   rule, because the README is the document a reader meets first and Requirement
   51.5 names the Repository documentation rather than the specification alone.

**How the glossary is read.** Entries are bolded lemmas, so the bolded spans are
what is parsed; the section index, the quick-reference table, and the near-neighbour
table are read as well, since a term defined in any of them is defined. Fenced blocks
are skipped in every document, because a diagram or a statement inside one carries
identifiers rather than prose, and backticks are removed before matching, because the
same term is written with and without them.

**A plural is the same term.** The documents write `Erasure_Leases` where the
glossary defines `Erasure_Lease`. A used term is therefore covered by an entry for
its singular form, and by nothing looser than that: no prefix matching, no case
folding, and no stemming beyond the two plural suffixes.

**A genuine gap fails.** If a component is named in the design or the README with no
glossary entry, the assertion reports the missing terms and the source that used
each. Nothing is trimmed to fit, and the glossary itself is never written to.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.spec

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
GLOSSARY: Final[Path] = REPOSITORY_ROOT / "docs" / "glossary.md"
DESIGN: Final[Path] = REPOSITORY_ROOT / ".kiro" / "specs" / "molt" / "design.md"
REQUIREMENTS: Final[Path] = REPOSITORY_ROOT / ".kiro" / "specs" / "molt" / "requirements.md"
README: Final[Path] = REPOSITORY_ROOT / "README.md"

# The documents the used-term sets are taken from, by the name a report gives them.
SPECIFICATION_SOURCES: Final[Mapping[str, Path]] = {
    "design": DESIGN,
    "requirements": REQUIREMENTS,
}
README_SOURCE: Final[str] = "README"

# A fence opens or closes a block whose content is not prose.
FENCE: Final[re.Pattern[str]] = re.compile(r"^\s*```")

# One glossary entry's lemma: the bolded span of a bold run.
BOLD: Final[re.Pattern[str]] = re.compile(r"\*\*([^*]+)\*\*")

# An underscored spec spelling: capitalised parts joined by underscores. The token
# must carry a lowercase letter somewhere, which is what separates a term such as
# `Erasure_Lease` from a configuration key or a statement constant, whose parts are
# capitalised throughout.
SPEC_TERM: Final[re.Pattern[str]] = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:_[A-Z][A-Za-z0-9]*)+\b")

# Where the design names every component in one sentence, and how that sentence ends.
INVENTORY_LEAD: Final[str] = "Component names are used exactly as defined in"
INVENTORY_SEPARATOR: Final[str] = ","

# The plural suffixes a used term may carry over its glossary entry.
PLURAL_SUFFIXES: Final[tuple[str, ...]] = ("s", "es")

# How many entries the glossary is expected to hold at the least. A parse that
# silently stopped finding lemmas would otherwise make every coverage claim vacuous.
MINIMUM_ENTRIES: Final[int] = 150

# How many components the design's inventory sentence is expected to name at the
# least, for the same reason.
MINIMUM_COMPONENTS: Final[int] = 25


def prose(path: Path) -> str:
    """One document's prose: fenced blocks removed and backticks stripped."""
    kept: list[str] = []
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line.replace("`", ""))
    return "\n".join(kept)


def glossary_lemmas() -> frozenset[str]:
    """Every term the glossary defines, read from its bolded lemmas."""
    return frozenset(
        match.group(1).strip() for match in BOLD.finditer(prose(GLOSSARY)) if match.group(1).strip()
    )


def inventory_components() -> tuple[str, ...]:
    """Every component the design's own inventory sentence names, in its own order."""
    for line in prose(DESIGN).splitlines():
        if INVENTORY_LEAD not in line:
            continue
        _, _, listed = line.partition(":")
        names = [name.strip().rstrip(".").strip() for name in listed.split(INVENTORY_SEPARATOR)]
        return tuple(name for name in names if name)
    raise AssertionError("the design carries no component inventory sentence to read")


def spec_terms(path: Path) -> frozenset[str]:
    """Every underscored spec spelling one document uses."""
    return frozenset(
        match.group(0)
        for match in SPEC_TERM.finditer(prose(path))
        if any(character.islower() for character in match.group(0))
    )


def covered(term: str, lemmas: frozenset[str]) -> bool:
    """Whether the glossary defines a term, allowing for a plural spelling."""
    if term in lemmas:
        return True
    return any(
        term.endswith(suffix) and term[: -len(suffix)] in lemmas for suffix in PLURAL_SUFFIXES
    )


def report(missing: Mapping[str, Sequence[str]]) -> str:
    """Name every term with no entry, and the source that used it."""
    lines = [f"{len(missing)} term(s) are used with no glossary entry:"]
    lines.extend(
        f"  {term} (used in {', '.join(sources)})" for term, sources in sorted(missing.items())
    )
    lines.append("The glossary is not edited by this test; the missing entries are the finding.")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def lemmas() -> frozenset[str]:
    """The glossary's own entry set, parsed once for the whole module."""
    return glossary_lemmas()


# ---------------------------------------------------------------------------
# The parses reach something, so a coverage claim is not vacuous
# ---------------------------------------------------------------------------


def test_the_glossary_parse_reaches_its_entries(lemmas: frozenset[str]) -> None:
    """The bolded lemmas are found, and the sections a reader expects are among them."""
    assert len(lemmas) >= MINIMUM_ENTRIES
    for expected in ("Collector", "Molt_MCP_Server", "Web_Console", "Provider_Selector"):
        assert expected in lemmas


def test_the_component_inventory_parse_reaches_its_names() -> None:
    """The design's inventory sentence is read, and it names the components it should."""
    components = inventory_components()
    assert len(components) >= MINIMUM_COMPONENTS
    assert len(set(components)) == len(components), "the inventory names a component twice"
    for expected in ("Capture_Hook", "Collector", "Redactor", "CLI", "Telemetry"):
        assert expected in components


def test_the_specification_term_parse_reaches_its_terms() -> None:
    """Underscored spellings are found in both specification documents."""
    for name, path in SPECIFICATION_SOURCES.items():
        terms = spec_terms(path)
        assert terms, f"no underscored term was read from the {name}"
        assert "Erasure_Lease" in terms or "Erasure_Leases" in terms


def test_the_readme_exists_and_is_read() -> None:
    """The README is a source of this claim, so its absence is a failure and not a skip."""
    assert README.is_file()
    assert spec_terms(README), "no underscored term was read from the README"


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("component", inventory_components(), ids=lambda name: name)
def test_every_component_the_design_names_has_a_glossary_entry(
    component: str, lemmas: frozenset[str]
) -> None:
    """One case per component, so a report names the component that is missing."""
    assert covered(component, lemmas), f"{component} is named in the design inventory"


def test_every_underscored_term_the_specification_uses_has_a_glossary_entry(
    lemmas: frozenset[str],
) -> None:
    """The design and the requirements, over their whole vocabulary at once."""
    missing: dict[str, list[str]] = {}
    for name, path in SPECIFICATION_SOURCES.items():
        for term in sorted(spec_terms(path)):
            if not covered(term, lemmas):
                missing.setdefault(term, []).append(name)
    assert not missing, report(missing)


def test_every_underscored_term_the_readme_uses_has_a_glossary_entry(
    lemmas: frozenset[str],
) -> None:
    """The document a reader meets first, held to the same coverage."""
    missing: dict[str, list[str]] = {
        term: [README_SOURCE] for term in sorted(spec_terms(README)) if not covered(term, lemmas)
    }
    assert not missing, report(missing)


def test_the_component_inventory_is_covered_as_a_whole(lemmas: frozenset[str]) -> None:
    """The same claim as the per-component cases, reported in one place."""
    missing: dict[str, list[str]] = {
        component: ["design inventory"]
        for component in inventory_components()
        if not covered(component, lemmas)
    }
    assert not missing, report(missing)
