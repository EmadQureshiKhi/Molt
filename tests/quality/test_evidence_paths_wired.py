"""Gate assertion that the module producing a certificate is reached by something.

The Certificate_Builder was written, tested, and unreachable. Every completed erasure in
every deployment ended with its evidence committed to the cluster, no signed document
anywhere, and an exit code saying the run had gone well: the engine records a completion
and deliberately assembles no certificate, saying so in its own docstring, and nothing
performed the step the docstring hands off to. The command-line verb reported the object
key a certificate *would* have taken, which read exactly like a certificate having been
written.

Nothing failed, and that is the shape worth naming. The builder's own suites drive it
directly, so they passed against a module no production path imported; the erasure suites
assert what a run commits, which was correct; and the one claim nobody made was that the
two halves were joined. It is the same failure as a route table whose handlers were never
imported, and the same failure as a recall engine that was never attached to the seam it
was written for — three times now, in this codebase, the missing thing was an import.

So this gate asserts reachability rather than behaviour. For each module that produces
evidence a governance claim rests on, some other production module must import it, and
the object protocol it writes through must have an implementation outside the tests. It is
a weak claim deliberately: a strong one would restate what the builder's own suites check,
and the defect this exists for is not a wrong certificate but no certificate.

The import graph is read from the source rather than by importing, so a module that is
reachable only from a test does not count as reachable and no cloud library is resolved to
find out.
"""

from __future__ import annotations

import pathlib
import re
from typing import Final

import pytest

# Static gate over the source: no reachable instance and no credential.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.quality

# Where production code lives. A module reached only from the suites is not reached.
SOURCE_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parents[2] / "src" / "molt"

# Each module whose absence from the import graph is a governance gap, and what it
# produces. The reason is carried here so a failure says what stops working rather than
# only which import is missing.
EVIDENCE_MODULES: Final[dict[str, str]] = {
    "molt.attest.builder": (
        "the signed Erasure_Certificate a completed run is attested by, which is the "
        "deliverable a departing tenant's reviewer reads"
    ),
    "molt.attest.checkpoint": (
        "the externally signed Ledger_Checkpoint, which is the whole of the tamper "
        "evidence that extends beyond a cluster administrator"
    ),
    "molt.attest.objects": (
        "the object write that puts a certificate under Object Lock, without which a "
        "certificate is signed and stored nowhere a reviewer can fetch it"
    ),
}

# The protocol a certificate is written through, and the call that satisfies it. A protocol
# with no implementation outside the tests is the state this codebase was in: the surface
# took the write as a parameter and the deployment had nothing to pass it.
OBJECT_WRITE_CALL: Final[str] = "def put_certificate"


def _production_sources() -> tuple[pathlib.Path, ...]:
    """Every production source file, which is the only place an import counts."""
    return tuple(sorted(SOURCE_ROOT.rglob("*.py")))


def _module_of(path: pathlib.Path) -> str:
    """The dotted name of one production source file."""
    relative = path.relative_to(SOURCE_ROOT.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def test_the_source_tree_was_found_at_all() -> None:
    """The detector is checked before what it detects.

    Every case below scans the production tree. An empty scan would leave them passing
    over nothing while the deployment issued no certificate, which is the failure this
    file exists to catch.
    """
    sources = _production_sources()
    assert len(sources) > 1, f"no production sources were found under {SOURCE_ROOT}"


@pytest.mark.parametrize("module", sorted(EVIDENCE_MODULES))
def test_an_evidence_module_is_imported_by_production_code(module: str) -> None:
    """Some production module imports the module, so the path it implements is reachable."""
    target = module.rsplit(".", 1)[-1]
    # Both spellings an import takes, so a module reached either way counts as reached.
    patterns = (
        re.compile(rf"^\s*from\s+{re.escape(module)}\s+import\b", re.MULTILINE),
        re.compile(rf"^\s*import\s+{re.escape(module)}\b", re.MULTILINE),
        # Resolved by name at the point of use, which this codebase does deliberately to
        # keep a cold start from paying an import it may not need.
        re.compile(rf"[\"']{re.escape(module)}[\"']"),
    )
    importers = [
        _module_of(path)
        for path in _production_sources()
        if _module_of(path) != module
        and any(pattern.search(path.read_text(encoding="utf-8")) for pattern in patterns)
        and not target.startswith("_")
    ]
    assert importers != [], (
        f"no production module imports {module}, so nothing in a deployment produces "
        f"{EVIDENCE_MODULES[module]}. Its own suites drive it directly and pass either way."
    )


def test_the_certificate_object_write_has_a_production_implementation() -> None:
    """The write a certificate is stored through is implemented outside the suites.

    The surface declares it as a structural protocol and takes it as a parameter, which is
    what lets the issuing path run with no credential. That is only a seam if something in
    the deployment can be passed to it: with the protocol alone, a run that completes signs
    a document and stores it nowhere.
    """
    implementers = [
        _module_of(path)
        for path in _production_sources()
        if OBJECT_WRITE_CALL in path.read_text(encoding="utf-8")
        and _module_of(path) != "molt.attest.builder"
    ]
    assert implementers != [], (
        "no production module implements the certificate object write, so a signed "
        "certificate has nowhere to be stored. The protocol on molt.attest.builder "
        "declares the call; something in the deployment has to satisfy it."
    )
