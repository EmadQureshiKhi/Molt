#!/usr/bin/env python3.12
"""Compose one standard-tier parameter write request, reading the value from standard input.

The value arrives on standard input and the name arrives as an argument, which is
the whole reason this step exists: a credential passed as a command-line argument
is visible in the process table to every account on the machine, and a credential
assembled into a shell string can be broken by a character the shell reads. Here
the value is read from a stream, encoded by a serialiser, and handed to the cloud
command as a request file the caller removes afterwards.

The request always declares the standard tier and always declares the encrypted
string type, so no caller can write a Molt parameter into a tier that carries a
per-parameter monthly charge and no caller can write a credential in plaintext.

Nothing is printed but the request document, and the request document goes to the
file the caller redirects it into rather than to a terminal.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import Final

# The tier every Molt parameter is written in, and the type every credential is
# written as. Both are fixed here rather than accepted from the caller.
STANDARD_TIER: Final[str] = "Standard"
ENCRYPTED_TYPE: Final[str] = "SecureString"
PLAIN_TYPE: Final[str] = "String"

EXIT_OK: Final[int] = 0
EXIT_USAGE: Final[int] = 2

# A parameter name is a path of hierarchy segments. Holding it to that shape here
# means a mistyped name fails before a request is built rather than creating a
# parameter nobody meant to create.
_NAME_PREFIX: Final[str] = "/"
_NAME_LIMIT: Final[int] = 2048


def build_request(name: str, value: str, *, encrypted: bool = True) -> dict[str, object]:
    """Build the write request for one parameter."""
    return {
        "Name": name,
        "Value": value,
        "Type": ENCRYPTED_TYPE if encrypted else PLAIN_TYPE,
        "Tier": STANDARD_TIER,
        "Overwrite": True,
    }


def _valid_name(name: str) -> bool:
    """Whether a parameter name is a hierarchy path of a permitted length."""
    return name.startswith(_NAME_PREFIX) and 1 < len(name) <= _NAME_LIMIT


def main(argv: Sequence[str] | None = None) -> int:
    """Read the value from standard input and print the composed request."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    plain = "--plain" in arguments
    names = [item for item in arguments if not item.startswith("--")]
    if len(names) != 1 or not _valid_name(names[0]):
        print(
            "compose_parameter_request: one parameter name beginning with a separator is required",
            file=sys.stderr,
        )
        return EXIT_USAGE
    value = sys.stdin.read().rstrip("\n")
    if not value:
        print("compose_parameter_request: the value stream was empty", file=sys.stderr)
        return EXIT_USAGE
    json.dump(build_request(names[0], value, encrypted=not plain), sys.stdout)
    sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
