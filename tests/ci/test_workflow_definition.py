"""Structural assertions over the continuous integration workflow definition.

The workflow is the thing that turns the repository's claims about typing,
hygiene, and correctness into executed checks, so the ordering it declares and
the credential-freeness it promises are asserted here rather than trusted.

Every assertion below reads the parsed document: the job mapping, the step
sequence, and the per-step fields. Nothing here matches the definition text, and
nothing here depends on comment placement, key order, or scalar folding style,
because a structural assertion over a definition outlives a text match. The
parser is the one declared in the dependency manifest, pinned exactly like every
other tool the checks depend on.

The suite reads one tracked file and runs no process, so it needs no cloud
provider credential and no cluster credential, which is the same property it
asserts of the workflow it reads.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW_PATH: Final[Path] = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"

# The invocations the workflow owes, named by role. The order of this tuple is the
# order the definition must declare them in: the four static checks, then the
# hygiene check, then the suites.
STRICT_TYPE_CHECK: Final[str] = "strict type check"
IGNORE_ALLOWLIST_CHECK: Final[str] = "type-ignore allowlist check"
LINTER_CHECK: Final[str] = "linter check"
FORMATTER_CHECK: Final[str] = "formatter check"
HYGIENE_CHECK: Final[str] = "metadata-hygiene check"
UNIT_SUITE: Final[str] = "unit suite"
QUALITY_SUITE: Final[str] = "quality suite"
PROPERTY_SUITE: Final[str] = "property suite"

# The quality suite sits between the other two deliberately. It holds the gates over
# the suites themselves, and one of them — the per-example deadline convention every
# property module owes — is a gate on the property suite, so a module that would fail
# on a busy machine has to be reported before the generative run rather than after it.
EXPECTED_ORDER: Final[tuple[str, ...]] = (
    STRICT_TYPE_CHECK,
    IGNORE_ALLOWLIST_CHECK,
    LINTER_CHECK,
    FORMATTER_CHECK,
    HYGIENE_CHECK,
    UNIT_SUITE,
    QUALITY_SUITE,
    PROPERTY_SUITE,
)

# The four static checks of the typing requirement. The suites are not listed here:
# they are read off the definition below, so a suite added to the workflow is covered
# by the credential-freeness assertions without this module being told about it twice.
STATIC_CHECKS: Final[tuple[str, ...]] = (
    STRICT_TYPE_CHECK,
    IGNORE_ALLOWLIST_CHECK,
    LINTER_CHECK,
    FORMATTER_CHECK,
)

# The directory each suite step names, by the role that step plays.
SUITE_DIRECTORIES: Final[Mapping[str, str]] = {
    "tests/unit": UNIT_SUITE,
    "tests/quality": QUALITY_SUITE,
    "tests/property": PROPERTY_SUITE,
}

# The markers that gate on a reachable instance, on cloud and model provider
# credentials, or on both. A suite step must deselect each of them, which is
# what lets the workflow run holding no credential.
GATED_MARKERS: Final[tuple[str, ...]] = (
    "integration",
    "services",
    "concurrency",
    "e2e",
    "perf",
)

# Credential identifiers no step may name: the cloud provider's own key,
# session, and role variables, the action that resolves them, and the cluster
# connection and password variables. Comparison happens on a normalised form,
# so a hyphenated spelling is caught by the same entry as an underscored one.
FORBIDDEN_CREDENTIAL_NAMES: Final[tuple[str, ...]] = (
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "aws_security_token",
    "aws_profile",
    "aws_role_arn",
    "aws_credentials",
    "role_to_assume",
    "web_identity_token",
    "ccloud_api_key",
    "cockroach_url",
    "cockroachdb_url",
    "cockroach_password",
    "cluster_password",
    "database_url",
    "molt_dsn",
    "pgpassword",
    "pguser",
    "pgsslcert",
    "pgsslkey",
)

# A repository-secret or repository-variable reference in any step field would
# make the workflow depend on material a reviewer holding a checkout has not
# got, so the expression contexts that read them are refused outright.
FORBIDDEN_EXPRESSION_CONTEXTS: Final[tuple[str, ...]] = ("secrets.", "vars.")

# Environment key shapes that carry a credential value rather than a setting.
CREDENTIAL_KEY_SUFFIXES: Final[tuple[str, ...]] = (
    "_key",
    "_token",
    "_secret",
    "_password",
    "_passwd",
    "_dsn",
    "_url",
    "_arn",
)

# Shell constructs that would let a failing command report success, which would
# defeat the obligation that any failing step fails the workflow.
FAILURE_SWALLOWING_TOKENS: Final[tuple[str, ...]] = ("||", "|&", "set")


@dataclass(frozen=True, slots=True)
class Step:
    """One step of one job, reduced to the fields the assertions walk."""

    job: str
    index: int
    name: str
    tokens: tuple[str, ...]
    fields: Mapping[str, object]


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    """Narrow a parsed node to a mapping with string keys."""
    assert isinstance(value, Mapping), f"{label} is no mapping"
    narrowed: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str), f"{label} carries a non-string key"
        narrowed[key] = item
    return narrowed


def _as_sequence(value: object, label: str) -> Sequence[object]:
    """Narrow a parsed node to a sequence, refusing a bare string."""
    assert isinstance(value, list), f"{label} is no sequence"
    return value


def _load_workflow() -> Mapping[str, object]:
    """Parse the tracked workflow definition into plain data."""
    reader = YAML(typ="safe")
    with WORKFLOW_PATH.open("r", encoding="utf-8") as handle:
        document: object = reader.load(handle)
    return _as_mapping(document, "the workflow definition")


def _collect_steps(workflow: Mapping[str, object]) -> tuple[Step, ...]:
    """Walk every job's step sequence in declaration order."""
    jobs = _as_mapping(workflow.get("jobs"), "the jobs node")
    collected: list[Step] = []
    for job_name, job_node in jobs.items():
        job = _as_mapping(job_node, f"job {job_name}")
        steps = _as_sequence(job.get("steps"), f"the step sequence of job {job_name}")
        for index, step_node in enumerate(steps):
            fields = _as_mapping(step_node, f"step {index} of job {job_name}")
            name_field = fields.get("name")
            run_field = fields.get("run")
            tokens: tuple[str, ...] = ()
            if isinstance(run_field, str):
                tokens = tuple(shlex.split(run_field))
            collected.append(
                Step(
                    job=job_name,
                    index=index,
                    name=name_field if isinstance(name_field, str) else "",
                    tokens=tokens,
                    fields=fields,
                )
            )
    return tuple(collected)


def _role_of(step: Step) -> str | None:
    """Classify a step by the command its run field declares."""
    tokens = step.tokens
    if not tokens:
        return None
    if "mypy" in tokens:
        return STRICT_TYPE_CHECK
    if any(token.endswith("check_type_ignores.py") for token in tokens):
        return IGNORE_ALLOWLIST_CHECK
    if "ruff" in tokens:
        if "format" in tokens:
            return FORMATTER_CHECK
        if "check" in tokens:
            return LINTER_CHECK
        return None
    if any(token.endswith("hygiene.py") for token in tokens):
        return HYGIENE_CHECK
    if "pytest" in tokens:
        for directory, role in SUITE_DIRECTORIES.items():
            if directory in tokens:
                return role
    return None


def _string_leaves(value: object) -> Iterator[str]:
    """Yield every string held anywhere inside a parsed node, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)


def _normalise(text: str) -> str:
    """Fold case and hyphenation so one entry catches both spellings."""
    return text.lower().replace("-", "_")


def _marker_expression(step: Step) -> str:
    """Read the marker expression a suite step passes to the test runner."""
    tokens = step.tokens
    runner = tokens.index("pytest")
    for position in range(runner + 1, len(tokens) - 1):
        if tokens[position] == "-m":
            return tokens[position + 1]
    return ""


def _declared_suites(steps: Sequence[Step]) -> tuple[str, ...]:
    """The suite roles the definition actually declares, in declaration order.

    Read off the steps rather than listed by hand, so a suite step added to the
    workflow is carried into the credential-freeness assertions by the definition
    itself. A step that runs the test runner over something this module cannot
    classify is a finding rather than a step quietly left out of those assertions.
    """
    found: list[str] = []
    for step in steps:
        if "pytest" not in step.tokens:
            continue
        role = _role_of(step)
        assert role is not None, (
            f"step {step.index} of job {step.job} runs a suite this module cannot name"
        )
        if role not in found:
            found.append(role)
    return tuple(found)


WORKFLOW: Final[Mapping[str, object]] = _load_workflow()
STEPS: Final[tuple[Step, ...]] = _collect_steps(WORKFLOW)
ROLE_POSITIONS: Final[Mapping[str, tuple[int, ...]]] = {
    role: tuple(position for position, step in enumerate(STEPS) if _role_of(step) == role)
    for role in EXPECTED_ORDER
}
TEST_SUITES: Final[tuple[str, ...]] = _declared_suites(STEPS)


def test_every_expected_invocation_appears_exactly_once() -> None:
    for role in EXPECTED_ORDER:
        assert len(ROLE_POSITIONS[role]) == 1, f"the {role} is not declared exactly once"


def test_every_expected_invocation_is_declared_in_the_fixed_order() -> None:
    positions = [ROLE_POSITIONS[role][0] for role in EXPECTED_ORDER]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(EXPECTED_ORDER)


def test_every_expected_invocation_shares_one_job() -> None:
    jobs = {STEPS[ROLE_POSITIONS[role][0]].job for role in EXPECTED_ORDER}
    assert len(jobs) == 1


def test_the_declared_suites_are_the_expected_ones() -> None:
    """The suites read off the definition are the suites the fixed order names."""
    declared = set(TEST_SUITES)
    assert tuple(role for role in EXPECTED_ORDER if role in declared) == TEST_SUITES
    assert declared == {UNIT_SUITE, QUALITY_SUITE, PROPERTY_SUITE}


def test_the_four_static_checks_precede_every_suite() -> None:
    latest_static = max(ROLE_POSITIONS[role][0] for role in STATIC_CHECKS)
    earliest_suite = min(ROLE_POSITIONS[role][0] for role in TEST_SUITES)
    assert latest_static < earliest_suite


def test_the_hygiene_check_precedes_every_suite() -> None:
    earliest_suite = min(ROLE_POSITIONS[role][0] for role in TEST_SUITES)
    assert ROLE_POSITIONS[HYGIENE_CHECK][0] < earliest_suite


def test_the_quality_suite_precedes_the_property_suite() -> None:
    """The gate on the property suite runs before the run it gates.

    The quality suite holds the per-example deadline convention every property module
    owes. A module that configures its examples and leaves the wall-clock deadline in
    place passes on an idle machine and fails on a busy one, so it has to be named
    before the generative run rather than after it.
    """
    assert ROLE_POSITIONS[QUALITY_SUITE][0] < ROLE_POSITIONS[PROPERTY_SUITE][0]


def test_the_formatter_runs_in_check_mode() -> None:
    formatter = STEPS[ROLE_POSITIONS[FORMATTER_CHECK][0]]
    assert "--check" in formatter.tokens


def test_no_step_is_permitted_to_continue_on_error() -> None:
    for step in STEPS:
        permission = step.fields.get("continue-on-error")
        assert permission is None or permission is False, (
            f"step {step.index} of job {step.job} may continue on error"
        )


def test_no_job_is_permitted_to_continue_on_error() -> None:
    jobs = _as_mapping(WORKFLOW.get("jobs"), "the jobs node")
    for job_name, job_node in jobs.items():
        job = _as_mapping(job_node, f"job {job_name}")
        permission = job.get("continue-on-error")
        assert permission is None or permission is False


def test_no_step_swallows_a_failing_command() -> None:
    for step in STEPS:
        for token in step.tokens:
            assert token not in FAILURE_SWALLOWING_TOKENS, (
                f"step {step.index} of job {step.job} can report success on failure"
            )


def test_no_step_references_a_cloud_or_cluster_credential_name() -> None:
    for step in STEPS:
        for leaf in _string_leaves(step.fields):
            folded = _normalise(leaf)
            for forbidden in FORBIDDEN_CREDENTIAL_NAMES:
                assert forbidden not in folded, (
                    f"step {step.index} of job {step.job} names a credential"
                )


def test_no_step_reads_a_repository_secret_or_variable() -> None:
    for step in STEPS:
        for leaf in _string_leaves(step.fields):
            folded = leaf.lower()
            for context in FORBIDDEN_EXPRESSION_CONTEXTS:
                assert context not in folded, (
                    f"step {step.index} of job {step.job} reads external material"
                )


def test_no_step_declares_a_credential_shaped_environment_key() -> None:
    for step in STEPS:
        environment = step.fields.get("env")
        if environment is None:
            continue
        for key in _as_mapping(environment, f"the env node of step {step.index}"):
            folded = _normalise(key)
            assert not folded.endswith(CREDENTIAL_KEY_SUFFIXES)
            assert "credential" not in folded


def test_every_suite_deselects_every_credential_gated_marker() -> None:
    for role in TEST_SUITES:
        step = STEPS[ROLE_POSITIONS[role][0]]
        expression = _normalise(_marker_expression(step))
        clauses = {clause.strip() for clause in expression.split(" and ")}
        for marker in GATED_MARKERS:
            assert f"not {marker}" in clauses, f"the {role} runs {marker} tests"


def test_the_workflow_grants_no_write_permission() -> None:
    permissions = _as_mapping(WORKFLOW.get("permissions"), "the permissions node")
    assert permissions == {"contents": "read"}
