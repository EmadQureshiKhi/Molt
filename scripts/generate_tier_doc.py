#!/usr/bin/env python3.12
"""Emit the Memory_Tier matrix in `docs/memory-tiers.md` from the tier mapping module.

The taxonomy is encoded once, in `src/molt/models/tiers.py`. The console tier view
reads that mapping to render its descriptive columns, and this script reads the same
mapping to render the documentation's matrix, so the rendered view and the document
state one taxonomy rather than two copies of one that drift apart at the first tier
whose mutability is restated in only one of them.

The generated material occupies one delimited region of the document. Everything
outside the two marker comments is prose written for a reader and is never touched;
everything between them is replaced wholesale on every write. Splicing rather than
rewriting the whole file is what lets the document carry reasoning the mapping has no
place for while still holding a matrix nobody edits.

Two modes, and the second is the point. Write mode splices the current region into the
document and reports whether the bytes moved. Verify mode splices into a copy and
compares, exiting non-zero when the document no longer matches the mapping, so "the
document cannot drift from the module" is a check a reviewer can run rather than a
claim the document makes about itself.

The script imports the mapping module, so the source path must be importable. Invoke
it from the repository root with the source directory on the import path:

    PYTHONPATH=src python3.12 scripts/generate_tier_doc.py
    PYTHONPATH=src python3.12 scripts/generate_tier_doc.py --check

Exit status is 0 when the document matches the mapping or has been brought to match
it, 1 in verify mode when it does not, and 2 when the document is missing or carries
no usable marker pair, so a malformed document is never reported as a current one.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from molt.models.tiers import MEMORY_TIERS, TIER_NAMES, MemoryTierSpec

EXIT_OK: Final[int] = 0
EXIT_STALE: Final[int] = 1
EXIT_INVALID: Final[int] = 2

# The region delimiters. Both are comments in the document's own markup, so they are
# invisible when the document is rendered and stable when it is edited.
BEGIN_MARKER: Final[str] = "<!-- generated:tier-matrix begin -->"
END_MARKER: Final[str] = "<!-- generated:tier-matrix end -->"

DEFAULT_DOCUMENT: Final[str] = "docs/memory-tiers.md"

# Stated inside the region rather than beside it, so the sentence telling a reader
# not to edit the region is itself part of what a write replaces.
REGION_NOTICE: Final[str] = (
    "*The matrix and the tier diagram below are emitted from"
    " `src/molt/models/tiers.py` by `scripts/generate_tier_doc.py`."
    " Change the mapping, not this region.*"
)

MATRIX_HEADING: Final[tuple[str, ...]] = (
    "Memory_Tier",
    "Tables held",
    "Mutability",
    "CockroachDB capability relied on",
)

TIER_NODE_PREFIX: Final[str] = "tier_"
TABLE_NODE_PREFIX: Final[str] = "table_"


class MarkerError(Exception):
    """The document carries no usable pair of region markers."""


def ordered_specs(
    tiers: Mapping[str, MemoryTierSpec],
    order: Sequence[str],
) -> tuple[MemoryTierSpec, ...]:
    """The tier specifications in declared order.

    Declaration order rather than key order, because the mapping's own order is the
    order the console renders, and a matrix listing the tiers differently would put a
    reader comparing the two documents to work reconciling nothing.
    """
    return tuple(tiers[name] for name in order)


def _flatten(text: str) -> str:
    """One line of cell text: runs of whitespace collapsed, table delimiters escaped.

    The mapping stores its prose wrapped across source lines and a cell holds one
    line, so the wrapping is removed here rather than in the mapping. A delimiter
    inside a cell is escaped so that prose gaining one later widens no row.
    """
    return " ".join(text.replace("|", "\\|").split())


def _sentence(text: str) -> str:
    """Cell prose closed with a full stop, since the mapping stores clauses unclosed."""
    flattened = _flatten(text)
    return flattened if flattened.endswith(".") else f"{flattened}."


def _tables_cell(spec: MemoryTierSpec) -> str:
    """The tables a tier holds, with the qualification the mapping records for it.

    A tier holding something narrower than whole rows of every table it names carries
    that qualification in the mapping, and it belongs in the cell beside the names
    rather than in a footnote a reader has to find.
    """
    names = ", ".join(f"`{table}`" for table in spec.tables)
    if spec.table_note is None:
        return names
    return f"{names} ({_flatten(spec.table_note)})"


def render_matrix(specs: Sequence[MemoryTierSpec]) -> str:
    """The four descriptive columns of every tier, one row per tier."""
    lines = [
        "| " + " | ".join(MATRIX_HEADING) + " |",
        "| " + " | ".join("---" for _ in MATRIX_HEADING) + " |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            (
                f"`{spec.name}`",
                _tables_cell(spec),
                _sentence(spec.mutability),
                _sentence(spec.capability),
            )
        )
        + " |"
        for spec in specs
    )
    return "\n".join(lines)


def render_diagram(specs: Sequence[MemoryTierSpec]) -> str:
    """The tiers and the tables each holds, as one graph.

    A table named by two tiers is one node with two edges into it rather than two
    nodes of the same name, so the sharing is visible in the drawing instead of being
    a coincidence of two labels a reader has to notice.
    """
    lines = ["```mermaid", "flowchart LR"]
    for spec in specs:
        lines.append(f'    {TIER_NODE_PREFIX}{spec.name}["{spec.name}"]')
    declared: list[str] = []
    for spec in specs:
        for table in spec.tables:
            if table not in declared:
                declared.append(table)
                lines.append(f'    {TABLE_NODE_PREFIX}{table}["{table}"]')
    for spec in specs:
        for table in spec.tables:
            lines.append(f"    {TIER_NODE_PREFIX}{spec.name} --> {TABLE_NODE_PREFIX}{table}")
    lines.append("```")
    return "\n".join(lines)


def render_region(specs: Sequence[MemoryTierSpec]) -> str:
    """Everything the generated region holds, in the order it is written."""
    return "\n\n".join((REGION_NOTICE, render_matrix(specs), render_diagram(specs)))


def splice(document: str, region: str) -> str:
    """Replace the marked region of a document, leaving every other byte alone."""
    begin = document.find(BEGIN_MARKER)
    end = document.find(END_MARKER)
    if begin < 0:
        raise MarkerError(f"the document carries no {BEGIN_MARKER} marker")
    if end < 0:
        raise MarkerError(f"the document carries no {END_MARKER} marker")
    if end < begin + len(BEGIN_MARKER):
        raise MarkerError("the region markers are out of order")
    if document.find(BEGIN_MARKER, begin + len(BEGIN_MARKER)) >= 0:
        raise MarkerError(f"the document carries more than one {BEGIN_MARKER} marker")
    head = document[: begin + len(BEGIN_MARKER)]
    tail = document[end:]
    return f"{head}\n\n{region}\n\n{tail}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_tier_doc",
        description="Emit the Memory_Tier matrix into the memory-tier documentation.",
    )
    parser.add_argument(
        "--document",
        default=DEFAULT_DOCUMENT,
        help=f"path to the document to write or verify; defaults to {DEFAULT_DOCUMENT}",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the document matches the mapping and write nothing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Write or verify the generated region of one document."""
    arguments = _build_parser().parse_args(argv)
    path = Path(arguments.document)
    region = render_region(ordered_specs(MEMORY_TIERS, TIER_NAMES))
    try:
        current = path.read_text(encoding="utf-8")
        intended = splice(current, region)
    except (MarkerError, OSError) as error:
        print(f"tier-doc: {error}", file=sys.stderr)
        return EXIT_INVALID

    if arguments.check:
        if intended != current:
            print(
                f"tier-doc: {path} no longer matches the tier mapping;"
                " run the generator without --check",
                file=sys.stderr,
            )
            return EXIT_STALE
        print(f"tier-doc: {path} is current against the tier mapping")
        return EXIT_OK

    if intended == current:
        print(f"tier-doc: {path} already current, nothing written")
        return EXIT_OK
    try:
        path.write_text(intended, encoding="utf-8")
    except OSError as error:
        print(f"tier-doc: {error}", file=sys.stderr)
        return EXIT_INVALID
    print(f"tier-doc: {path} rewritten from the tier mapping")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
