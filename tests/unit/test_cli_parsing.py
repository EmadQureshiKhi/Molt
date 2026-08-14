"""The command-line surface: the verbs, the flags, the statuses, and the streams.

Every assertion here runs the same entry point a shell runs, with the streams and
the environment supplied, so what is asserted is the behaviour an operator gets
rather than a parallel path that happens to agree. Nothing here reaches a cluster:
the verbs that would are asserted on their argument handling and on the status a
missing configuration value produces, which is settled before any connection is
opened.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence

import pytest

from molt.cli import main
from molt.cli.exits import ExitCode
from molt.cli.main import ATTEST_SUBCOMMANDS, GLOBAL_FLAGS, VERBS, build_parser, run
from molt.cli.output import REDACTED
from molt.cli.verbs import HANDLERS

# An environment holding no configuration at all, so a verb that needs a value
# fails on the value rather than on something a developer machine happened to set.
BARE_ENVIRONMENT: Mapping[str, str] = {}

# A fabricated value written under a key the redaction set names, used to assert no stream
# carries it. It is a fabricated value and names nothing real.
PLANTED_VALUE = "not-a-real-token-value"


def _invoke(
    argv: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> tuple[ExitCode, str, str]:
    """Run one invocation and hand back the status and both streams."""
    out = io.StringIO()
    err = io.StringIO()
    code = run(
        list(argv),
        out=out,
        err=err,
        environ=dict(BARE_ENVIRONMENT if environ is None else environ),
    )
    return code, out.getvalue(), err.getvalue()


def test_entry_point_is_the_console_script_target() -> None:
    assert callable(main)


def test_every_designed_verb_is_in_the_tree_and_dispatches() -> None:
    parser = build_parser()
    actions = [action for action in parser._actions if action.choices is not None]
    assert actions, "the tree declares subparsers"
    declared = set(actions[0].choices or {})
    assert declared == set(VERBS)
    assert set(HANDLERS) == {verb for verb in VERBS if verb != "attest"} | {
        f"attest {command}" for command in ATTEST_SUBCOMMANDS
    }


def test_the_two_word_attest_verify_form_parses() -> None:
    args = build_parser().parse_args(["attest", "verify", "--certificate", "cert.json"])
    assert args.verb == "attest"
    assert args.attest_command == "verify"
    assert args.certificate == "cert.json"


def test_sensitivity_and_contend_carry_their_designed_flags() -> None:
    parser = build_parser()
    sensitivity = parser.parse_args(
        [
            "sensitivity",
            "--client",
            "acme",
            "--auto-include-thresholds",
            "0.1,0.2",
            "--review-thresholds",
            "0.4",
            "--ground-truth",
            "truth.json",
        ]
    )
    assert sensitivity.verb == "sensitivity"
    assert sensitivity.auto_include_thresholds == "0.1,0.2"
    contend = parser.parse_args(["contend", "--client", "acme", "--lease-interval", "7"])
    assert contend.verb == "contend"
    assert contend.workers == 10
    assert contend.lease_interval == 7


@pytest.mark.parametrize("flag", GLOBAL_FLAGS)
def test_a_global_flag_is_accepted_before_and_after_the_verb(flag: str) -> None:
    parser = build_parser()
    value = [] if flag in ("--json", "--yes") else ["value"]
    before = parser.parse_args([flag, *value, "retention"])
    after = parser.parse_args(["retention", flag, *value])
    attribute = flag.removeprefix("--").replace("-", "_")
    expected = True if not value else "value"
    assert getattr(before, attribute, None) == expected
    assert getattr(after, attribute, None) == expected


def test_no_verb_is_a_usage_error() -> None:
    code, out, err = _invoke([])
    assert code == ExitCode.USAGE
    assert out == ""
    assert "verb" in err


def test_an_unknown_flag_is_a_usage_error() -> None:
    code, _, err = _invoke(["retention", "--nonesuch"])
    assert code == ExitCode.USAGE
    assert err


def test_attest_verify_refuses_two_sources_as_a_usage_error() -> None:
    code, _, err = _invoke(
        ["attest", "verify", "--certificate", "cert.json", "--checkpoint", "abc"]
    )
    assert code == ExitCode.USAGE
    assert "exactly one" in err


def test_an_erasure_without_confirmation_is_a_usage_error() -> None:
    code, _, err = _invoke(
        ["erase", "--client", "acme", "--requester", "op", "--justification", "why"]
    )
    assert code == ExitCode.USAGE
    assert "--yes" in err


def test_a_missing_configuration_value_names_the_key_and_exits_two() -> None:
    code, _, err = _invoke(["retention"])
    assert code == ExitCode.USAGE
    assert "MOLT_DSN_PARAM" in err or "store.dsn_param" in err


def test_the_json_flag_writes_one_object_on_standard_output() -> None:
    code, out, err = _invoke(["--json", "retention"])
    assert code == ExitCode.USAGE
    document = json.loads(out)
    assert document["ok"] is False
    assert document["exit_code"] == int(ExitCode.USAGE)
    assert len(out.strip().splitlines()) == 1
    assert err, "narration is diverted to standard error under the flag"


def test_the_distinct_statuses_are_four_and_ordered() -> None:
    assert int(ExitCode.SUCCESS) == 0
    assert int(ExitCode.OPERATIONAL) == 1
    assert int(ExitCode.USAGE) == 2
    assert int(ExitCode.VERIFICATION_FAILED) == 3
    assert len(set(ExitCode)) == 4


def test_a_verb_missing_a_required_configuration_value_is_a_usage_error() -> None:
    """The serve verb reaches the console and stops at the value it cannot resolve.

    The console application object now exists, so this verb no longer reports a
    missing component. What it reports instead is the configuration it cannot run
    without: the parameter naming the operator credential is not configured here, so
    the invocation ends as a configuration error naming that key rather than starting
    a console nobody could authenticate against.
    """
    code, _, err = _invoke(
        ["serve"],
        {"MOLT_DSN": "postgresql://localhost:26257/molt?sslmode=verify-full"},
    )
    assert code == ExitCode.USAGE
    assert "MOLT_CONSOLE_CREDENTIAL_PARAM" in err


def test_no_secret_value_reaches_either_stream() -> None:
    environment = {
        "MOLT_COLLECTOR_TOKEN": PLANTED_VALUE,
        "MOLT_INGRESS_SECRET": PLANTED_VALUE,
        "MOLT_MCP_PERMITTED_CLIENTS": "acme",
    }
    for argv in (["--json", "retention"], ["retention"], ["mcp", "--transport", "stdio"]):
        _, out, err = _invoke(argv, environment)
        assert PLANTED_VALUE not in out
        assert PLANTED_VALUE not in err


def test_a_document_value_under_a_secret_name_is_redacted() -> None:
    from molt.cli.output import Emitter

    out = io.StringIO()
    err = io.StringIO()
    Emitter(out=out, err=err, json_output=True).emit({"api_key": PLANTED_VALUE, "kept": "value"})
    document = json.loads(out.getvalue())
    assert document["api_key"] == REDACTED
    assert document["kept"] == "value"
    assert PLANTED_VALUE not in out.getvalue()
