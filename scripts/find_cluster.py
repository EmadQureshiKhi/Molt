#!/usr/bin/env python3.12
"""Report whether a control-plane cluster listing names a given cluster.

The provisioning script needs one yes-or-no answer about a listing it already
holds, and reading that answer out of machine-readable output with a parser rather
than with a shell pattern is what makes the answer exact: a name that is a prefix
of another name, or a listing whose field order changes, both give the wrong answer
to a text match and the right one here.

The listing arrives on standard input, so no value passes through a command line
and nothing is printed but the matched name. Exit status is 0 when the name is
present and 1 when it is not, which is what the caller branches on.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Sequence
from typing import Final

# The field a cluster's own name appears under, and the wrappers a listing may
# arrive inside. Both spellings of the collection wrapper are accepted, because a
# listing is either a bare sequence or a sequence under one key.
NAME_FIELD: Final[str] = "name"
COLLECTION_FIELDS: Final[tuple[str, ...]] = ("clusters", "items", "results")

EXIT_FOUND: Final[int] = 0
EXIT_ABSENT: Final[int] = 1
EXIT_USAGE: Final[int] = 2


def _entries(document: object) -> Iterator[object]:
    """Yield each listed record, whatever wrapper the listing arrived in."""
    if isinstance(document, list):
        yield from document
        return
    if isinstance(document, dict):
        for field in COLLECTION_FIELDS:
            nested = document.get(field)
            if isinstance(nested, list):
                yield from nested
                return
        yield document


def named_clusters(document: object) -> tuple[str, ...]:
    """Every cluster name the listing carries, in listed order."""
    found: list[str] = []
    for entry in _entries(document):
        if isinstance(entry, dict):
            name = entry.get(NAME_FIELD)
            if isinstance(name, str) and name:
                found.append(name)
    return tuple(found)


def main(argv: Sequence[str] | None = None) -> int:
    """Read a listing on standard input and report whether it names the wanted cluster."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or not arguments[0]:
        print("find_cluster: one cluster name is required", file=sys.stderr)
        return EXIT_USAGE
    wanted = arguments[0]
    try:
        document: object = json.loads(sys.stdin.read() or "[]")
    except json.JSONDecodeError:
        print("find_cluster: the listing was not machine-readable", file=sys.stderr)
        return EXIT_USAGE
    if wanted in named_clusters(document):
        print(wanted)
        return EXIT_FOUND
    return EXIT_ABSENT


if __name__ == "__main__":
    sys.exit(main())
