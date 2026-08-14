"""The one argument tree, and the one place a verb's outcome becomes a status.

The tree is nested subparsers because two of the verbs are two words: `attest
verify` names the verifier and leaves room for the other things attestation will
grow, and a flat `attest-verify` would have spelled that as a hyphen.

Global flags are declared once and attached both to the top-level parser and to
every subparser, with an absent value suppressed rather than defaulted, so
`molt --json erase` and `molt erase --json` are the same invocation. Two of those
flags are applied by re-resolving the configuration surface with one environment
value replaced rather than by threading a parameter: precedence is the surface's
business, and a second precedence rule inside the CLI would be a second place an
operator's setting could fail to arrive.

Every failure class becomes exactly one status here. A usage or configuration
fault is 2, a verification whose answer is `failed` is 3, and anything else that
went wrong while running is 1, which includes a component this build does not yet
carry: the invocation was well formed and the capability was simply not there.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, NoReturn, TextIO

from molt.cli.context import VerbContext
from molt.cli.exits import ComponentUnavailableError, ExitCode, UsageError
from molt.cli.output import Emitter
from molt.cli.verbs import HANDLERS
from molt.config.resolve import ConfigError, load_configuration

__all__ = [
    "ATTEST_SUBCOMMANDS",
    "GLOBAL_FLAGS",
    "LOG_LEVEL_KEY",
    "PERMITTED_CLIENTS_KEY",
    "VERBS",
    "build_parser",
    "main",
    "run",
]

# The configuration keys the two surface-bearing global flags are applied through.
LOG_LEVEL_KEY: Final[str] = "MOLT_LOG_LEVEL"
PERMITTED_CLIENTS_KEY: Final[str] = "MOLT_MCP_PERMITTED_CLIENTS"

# The verb surface, in the order the design table lists it.
VERBS: Final[tuple[str, ...]] = (
    "erase",
    "residue",
    "sensitivity",
    "contend",
    "attest",
    "recall",
    "watch",
    "serve",
    "mcp",
    "seed",
    "migrate",
    "verify-chain",
    "retention",
)

# The second word of the one two-word verb.
ATTEST_SUBCOMMANDS: Final[tuple[str, ...]] = ("verify",)

# The flags every verb answers to, named here so a test can assert the surface.
GLOBAL_FLAGS: Final[tuple[str, ...]] = (
    "--json",
    "--config",
    "--client-set",
    "--log-level",
    "--yes",
)


class _Parser(argparse.ArgumentParser):
    """An argument parser whose faults are raised rather than exited on.

    argparse would call `sys.exit` from inside parsing, which would leave the one
    place that maps a failure onto a status with nothing to map. Raising instead
    keeps the mapping in one place and keeps `run` callable from a test.
    """

    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _global_flags() -> _Parser:
    """The flags shared by the top-level parser and every subparser."""
    shared = _Parser(add_help=False)
    shared.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print one machine-readable object on standard output",
    )
    shared.add_argument(
        "--config", metavar="PATH", default=argparse.SUPPRESS, help="the configuration file to read"
    )
    shared.add_argument(
        "--client-set",
        metavar="SLUGS",
        default=argparse.SUPPRESS,
        help="the comma-separated Client set this invocation is permitted",
    )
    shared.add_argument(
        "--log-level", metavar="LEVEL", default=argparse.SUPPRESS, help="the log level to emit at"
    )
    shared.add_argument(
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help="confirm without prompting",
    )
    return shared


def build_parser() -> argparse.ArgumentParser:
    """The whole argument tree: the global flags, every verb, and every verb's flags."""
    shared = _global_flags()
    parser = _Parser(
        prog="molt",
        description="Durable, distributed, governable memory for AI coding agents",
        parents=[shared],
    )
    # No parser-level defaults are set for the shared flags. The parent's action
    # objects are the same objects the subparsers hold, so a default written here
    # would be written onto the subparser's copy too and would then overwrite a
    # value given before the verb. Absence is read as the default instead.
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    erase = verbs.add_parser("erase", parents=[shared], help="erase one Client's memory")
    erase.add_argument("--client", metavar="SLUG", required=True)
    erase.add_argument("--requester", metavar="ID", required=True)
    erase.add_argument("--justification", metavar="TEXT", required=True)
    erase.add_argument("--dry-run", action="store_true")
    erase.add_argument("--skip-backup", action="store_true")
    erase.add_argument("--auto-include-threshold", type=float, metavar="FLOAT")
    erase.add_argument("--review-threshold", type=float, metavar="FLOAT")
    erase.add_argument("--batch-size", type=int, metavar="INT")

    residue = verbs.add_parser("residue", parents=[shared], help="report residue candidates")
    residue.add_argument("--client", metavar="SLUG", required=True)
    residue.add_argument("--auto-include-threshold", type=float, metavar="FLOAT")
    residue.add_argument("--review-threshold", type=float, metavar="FLOAT")
    residue.add_argument("--limit", type=int, metavar="INT")
    residue.add_argument("--top-k", type=int, metavar="INT")
    residue.add_argument("--no-adjudicate", action="store_true")

    sensitivity = verbs.add_parser(
        "sensitivity", parents=[shared], help="report the threshold grid"
    )
    sensitivity.add_argument("--client", metavar="SLUG", required=True)
    sensitivity.add_argument("--auto-include-thresholds", metavar="LIST")
    sensitivity.add_argument("--review-thresholds", metavar="LIST")
    sensitivity.add_argument("--ground-truth", metavar="PATH")

    contend = verbs.add_parser(
        "contend", parents=[shared], help="demonstrate lease contention and fencing"
    )
    contend.add_argument("--client", metavar="SLUG", required=True)
    contend.add_argument("--workers", type=int, default=10, metavar="INT")
    contend.add_argument("--lease-interval", type=int, metavar="SECONDS")

    attest = verbs.add_parser("attest", parents=[shared], help="work with erasure certificates")
    attest_commands = attest.add_subparsers(dest="attest_command", metavar="COMMAND")
    verify = attest_commands.add_parser(
        "verify", parents=[shared], help="verify a certificate or a checkpoint"
    )
    verify.add_argument("--certificate", metavar="PATH")
    verify.add_argument("--s3-key", metavar="KEY")
    verify.add_argument("--bucket", metavar="NAME")
    verify.add_argument("--skip-live-queries", action="store_true")
    verify.add_argument("--checkpoint", metavar="UUID")

    recall = verbs.add_parser("recall", parents=[shared], help="recall prior work semantically")
    recall.add_argument("query", metavar="QUERY")
    recall.add_argument("-k", type=int, dest="k", metavar="INT")
    recall.add_argument("--client", metavar="SLUG", action="append")
    recall.add_argument("--session-id", metavar="UUID")

    watch = verbs.add_parser("watch", parents=[shared], help="run the policy watcher")
    watch.add_argument("--interval", type=int, metavar="SECONDS")
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--rules", metavar="PATH")
    watch.add_argument("--bind", metavar="HOST:PORT")

    serve = verbs.add_parser("serve", parents=[shared], help="run the web console locally")
    serve.add_argument("--bind", metavar="HOST:PORT")
    serve.add_argument("--demo", action="store_true")

    mcp = verbs.add_parser("mcp", parents=[shared], help="run the tool server")
    mcp.add_argument("--transport", choices=("stdio", "http"))
    mcp.add_argument("--bind", metavar="HOST:PORT")
    mcp.add_argument("--client", metavar="SLUG", action="append")
    mcp.add_argument("--max-results", type=int, metavar="INT")

    seed = verbs.add_parser("seed", parents=[shared], help="generate a seeded corpus")
    seed.add_argument("--seed", type=int, metavar="INT", required=True)
    seed.add_argument("--clients", type=int, metavar="INT")
    seed.add_argument("--sessions", type=int, metavar="INT")
    seed.add_argument("--events", type=int, metavar="INT")
    seed.add_argument("--ground-truth", metavar="PATH")
    seed.add_argument("--reset", action="store_true")

    migrate = verbs.add_parser("migrate", parents=[shared], help="apply migrations in order")
    migrate.add_argument("--to", metavar="VERSION", type=int)
    migrate.add_argument("--dry-run", action="store_true")

    verify_chain = verbs.add_parser(
        "verify-chain", parents=[shared], help="verify one hash chain by recomputation"
    )
    verify_chain.add_argument("--session-id", metavar="UUID")
    verify_chain.add_argument("--client", metavar="SLUG")

    retention = verbs.add_parser("retention", parents=[shared], help="print the retention report")
    retention.add_argument("--client", metavar="SLUG")

    return parser


def _verb_name(args: argparse.Namespace) -> str:
    """The verb this invocation names, as one or two words."""
    verb = getattr(args, "verb", None)
    if not isinstance(verb, str) or not verb:
        raise UsageError("no verb was named")
    if verb == "attest":
        command = getattr(args, "attest_command", None)
        if not isinstance(command, str) or not command:
            raise UsageError("the attest verb names a command, which is verify")
        return f"attest {command}"
    return verb


def _overrides(args: argparse.Namespace) -> dict[str, str]:
    """The environment values the global flags replace before the surface resolves."""
    overrides: dict[str, str] = {}
    level = getattr(args, "log_level", None)
    if isinstance(level, str) and level:
        overrides[LOG_LEVEL_KEY] = level
    clients = getattr(args, "client_set", None)
    if isinstance(clients, str) and clients:
        overrides[PERMITTED_CLIENTS_KEY] = clients
    return overrides


def run(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO,
    err: TextIO,
    environ: Mapping[str, str] | None = None,
) -> ExitCode:
    """Parse, dispatch, and map the outcome onto a status without exiting.

    Every stream and the environment are parameters so a test drives the same
    path a shell drives, rather than a second path that happens to agree.
    """
    parser = build_parser()
    resolved_environ: Mapping[str, str] = dict(os.environ if environ is None else environ)
    raw = list(argv) if argv is not None else list(sys.argv[1:])
    # A fault found during parsing still has to be reported in the shape the
    # caller asked for, and the namespace does not exist yet at that point.
    early = Emitter(out=out, err=err, json_output="--json" in raw)
    try:
        args = parser.parse_args(raw)
        verb = _verb_name(args)
        handler = HANDLERS.get(verb)
        if handler is None:
            raise UsageError(f"there is no {verb} verb")
        config_text = getattr(args, "config", None)
        config_path = Path(config_text).expanduser() if isinstance(config_text, str) else None
        configuration = load_configuration(
            config_path=config_path,
            environ={**resolved_environ, **_overrides(args)},
        )
    except (UsageError, ConfigError) as error:
        return early.fail("molt", str(error), ExitCode.USAGE)

    emitter = Emitter(
        out=out,
        err=err,
        json_output=bool(getattr(args, "json", False)),
        configuration=configuration,
    )
    context = VerbContext(
        name=verb,
        args=args,
        configuration=configuration,
        emitter=emitter,
        environ=resolved_environ,
        config_path=configuration.config_path,
    )
    try:
        return handler(context)
    except (UsageError, ConfigError) as error:
        return emitter.fail(verb, str(error), ExitCode.USAGE)
    except ComponentUnavailableError as error:
        return emitter.fail(verb, str(error), ExitCode.OPERATIONAL)
    except Exception as error:
        # One status for every operational fault, so a caller branches on the
        # status rather than on a message.
        return emitter.fail(verb, f"{type(error).__name__}: {error}", ExitCode.OPERATIONAL)


def main(argv: Sequence[str] | None = None) -> int:
    """The console entry point `molt` resolves to."""
    try:
        return int(run(argv, out=sys.stdout, err=sys.stderr))
    except SystemExit as exit_request:  # the help and the version paths land here
        code = exit_request.code
        return code if isinstance(code, int) else 0
