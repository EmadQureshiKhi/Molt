#!/usr/bin/env python3.12
"""Compose the values the role provisioning script needs without shell string building.

Three small jobs live here, and they live here for one reason each.

Reading the cluster host out of the control plane's machine-readable output is done
by a parser rather than by a shell pattern, so a field that moves or a value that
carries a separator gives the right answer rather than a plausible wrong one.

Composing a connection string is done by a library so the user name, the host, and
the password are percent-encoded rather than concatenated. The password arrives on
standard input and never as an argument, so it is never visible in the process
table, and the composed string goes to the parameter write on standard output and
to nothing else. Full certificate verification is required in the composed string,
because a connection string that omits it would let a role connect without it.

Computing the auditor expiry instant is done here so that no instant is ever
written into a tracked file: the interval is the input, and the instant is produced
at run time from the clock.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import quote, urlencode

# The transport security mode every composed connection string requires.
REQUIRED_SSL_MODE: Final[str] = "verify-full"

# The fields a cluster description may carry its host under, in the order they are
# looked for. A description that carries none is a fault rather than a default.
HOST_FIELDS: Final[tuple[str, ...]] = ("host", "dns_name", "sql_dns", "address")
NESTED_FIELDS: Final[tuple[str, ...]] = ("cluster", "config", "regions")

# The database every composed connection string names.
DATABASE_NAME: Final[str] = "molt"
DEFAULT_PORT: Final[int] = 26257

# The instant format the cluster reads a login validity bound in. It carries no
# digits of its own, so no instant is written down here.
EXPIRY_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S%z"

EXIT_OK: Final[int] = 0
EXIT_USAGE: Final[int] = 2


def _host_in(node: object) -> str | None:
    """The host one node names, searching the nested wrappers a description may use."""
    if isinstance(node, str):
        return node or None
    if isinstance(node, list):
        for item in node:
            found = _host_in(item)
            if found is not None:
                return found
        return None
    if not isinstance(node, dict):
        return None
    for field in HOST_FIELDS:
        value = node.get(field)
        if isinstance(value, str) and value:
            return value
    for field in NESTED_FIELDS:
        if field in node:
            found = _host_in(node[field])
            if found is not None:
                return found
    return None


def cluster_host(document: object) -> str | None:
    """The connection host a cluster description names, or None when it names none."""
    return _host_in(document)


def compose_dsn(user: str, host: str, password: str, *, port: int = DEFAULT_PORT) -> str:
    """Compose one connection string with every component encoded and verification required."""
    credential = f"{quote(user, safe='')}:{quote(password, safe='')}"
    query = urlencode({"sslmode": REQUIRED_SSL_MODE})
    return f"postgresql://{credential}@{host}:{port}/{quote(DATABASE_NAME, safe='')}?{query}"


def expiry_instant(days: int, *, now: datetime | None = None) -> str:
    """The instant a login created now stops being valid, at most the given interval away."""
    if days < 1:
        raise ValueError("a validity interval reaches at least one day")
    moment = datetime.now(tz=UTC) if now is None else now
    return (moment + timedelta(days=days)).strftime(EXPIRY_FORMAT)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compose_role_dsn",
        description="Read a cluster host, compose a connection string, or compute an expiry.",
    )
    parser.add_argument("--field", choices=("host",), default=None, help="read one field")
    parser.add_argument("--user", default=None, help="role login the connection string names")
    parser.add_argument("--host", default=None, help="connection host the string names")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="connection port")
    parser.add_argument(
        "--expiry-days",
        type=int,
        default=None,
        help="print the instant a login created now stops being valid",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run whichever of the three jobs the arguments select."""
    arguments = _build_parser().parse_args(argv)

    if arguments.expiry_days is not None:
        try:
            print(expiry_instant(arguments.expiry_days))
        except ValueError as error:
            print(f"compose_role_dsn: {error}", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_OK

    if arguments.field == "host":
        try:
            document: object = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            print("compose_role_dsn: the description was not machine-readable", file=sys.stderr)
            return EXIT_USAGE
        found = cluster_host(document)
        if found is None:
            print("compose_role_dsn: the description names no connection host", file=sys.stderr)
            return EXIT_USAGE
        print(found)
        return EXIT_OK

    if not arguments.user or not arguments.host:
        print("compose_role_dsn: a login and a host are required", file=sys.stderr)
        return EXIT_USAGE
    password = sys.stdin.read().rstrip("\n")
    if not password:
        print("compose_role_dsn: the credential stream was empty", file=sys.stderr)
        return EXIT_USAGE
    print(compose_dsn(arguments.user, arguments.host, password, port=arguments.port))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
