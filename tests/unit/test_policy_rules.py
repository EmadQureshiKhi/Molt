"""Unit tests for the Policy_Rule set, the pattern language, and the pure evaluator.

Nothing here opens a connection or reads a clock. A rules file is written into a
temporary directory, a configuration is built from an explicit environment mapping,
and every mutation is constructed in the test, so each claim below is asserted over
what the evaluator decided rather than over a cluster's behaviour.

Seven groups of claims are checked.

The severity order is fixed and total. It ranks all four actions, most severe
first, and the reduction over two actions is the more severe of them.

The five match kinds each match what the design says they match. A path rule reads
the path of a file read, a file write, and a tool call; a command rule reads the
command of a shell command; a Client rule compares the Client; a cost rule compares
the accrued cost; a rate rule divides errors by the trailing window.

The pattern language behaves as stated. A short glob matches a file wherever it
sits, a rooted glob stays rooted, a trailing separator means everything beneath,
a home reference is stripped rather than expanded, a separator-crossing wildcard
crosses and a single one does not, and a regular expression is recognised by its
prefix.

The sensitive path pattern set covers the three classes the requirement names, and
the configuration surface's coarse default is contained in it.

Resolution is order-independent. Permuting the rule list leaves the outcome list
identical, a rule offered twice yields one outcome, and the most severe outcome
governs whichever order the rules arrived in.

A rule set comes from the configured path or the built-in set, and a file replaces
the built-in set rather than extending it.

A malformed rule is refused rather than silently dropped: an unknown key, an
unknown match kind, a missing pattern, a missing threshold, a duplicate name, and
an unusable expression each raise.

**Validates: Requirements 23.4, 23.5, 23.11**
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import BUILTIN_SENSITIVE_PATHS, ConfigError, Configuration
from molt.models.event import EventCategory
from molt.policy.evaluate import (
    Mutation,
    MutationTable,
    error_rate_over,
    evaluate,
    governing_action,
    triggered_actions,
)
from molt.policy.rules import (
    ACTION_SEVERITY_ORDER,
    BUILTIN_SENSITIVE_PATTERNS,
    MATCH_KIND_VALUES,
    POLICY_ACTION_VALUES,
    MatchKind,
    PatternMode,
    PolicyAction,
    PolicyRule,
    SensitiveGroup,
    builtin_rules,
    compile_pattern,
    load_rules,
    load_rules_file,
    more_severe,
    rule_identifier,
    severity_rank,
)

WORKSPACE = "/home/engineer/work/service"
CLIENT_ID = UUID("11111111-2222-4333-8444-555555555555")
OTHER_CLIENT_ID = UUID("99999999-8888-4777-8666-555555555555")


def _instant() -> datetime:
    """One timezone-aware instant, rendered at call time rather than written down."""
    return datetime.now(tz=UTC)


def _rule(
    name: str,
    kind: MatchKind,
    action: PolicyAction,
    *,
    pattern: str | None = None,
    client_id: UUID | None = None,
    threshold: float | None = None,
    window_events: int | None = None,
    enabled: bool = True,
) -> PolicyRule:
    return PolicyRule(
        id=rule_identifier(name),
        name=name,
        match_kind=kind,
        action=action,
        enabled=enabled,
        pattern=pattern,
        client_id=client_id,
        threshold=threshold,
        window_events=window_events,
    )


def _ledger(
    category: EventCategory,
    payload: dict[str, str] | None = None,
    *,
    client_id: UUID = CLIENT_ID,
    cost: Decimal = Decimal(0),
    recent: tuple[EventCategory, ...] = (),
) -> Mutation:
    return Mutation(
        table=MutationTable.LEDGER,
        row_id=uuid4(),
        session_id=uuid4(),
        client_id=client_id,
        occurred_at=_instant(),
        category=category,
        payload=dict(payload or {}),
        session_cost_usd=cost,
        recent_categories=recent,
    )


def _read(path: str) -> Mutation:
    return _ledger(EventCategory.FILE_READ, {"path": path})


def test_severity_order_is_fixed_and_total() -> None:
    """The four actions are ranked once each, halt first and allow last."""
    assert ACTION_SEVERITY_ORDER == (
        PolicyAction.HALT_AGENT,
        PolicyAction.REQUIRE_APPROVAL,
        PolicyAction.WARN,
        PolicyAction.ALLOW,
    )
    ranks = [severity_rank(action) for action in ACTION_SEVERITY_ORDER]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(POLICY_ACTION_VALUES)
    assert more_severe(PolicyAction.WARN, PolicyAction.HALT_AGENT) is PolicyAction.HALT_AGENT
    assert more_severe(PolicyAction.HALT_AGENT, PolicyAction.WARN) is PolicyAction.HALT_AGENT
    assert more_severe(PolicyAction.ALLOW, PolicyAction.WARN) is PolicyAction.WARN


def test_match_kind_values_are_the_schema_five() -> None:
    """The enumeration holds the five kinds the rule table's constraint holds."""
    assert MATCH_KIND_VALUES == (
        "file_path",
        "shell_command",
        "client",
        "session_cost",
        "error_rate",
    )


@pytest.mark.parametrize(
    "category",
    [EventCategory.FILE_READ, EventCategory.FILE_WRITE, EventCategory.TOOL_CALL],
)
def test_file_path_rule_matches_every_path_bearing_category(category: EventCategory) -> None:
    """A path rule reads the payload path of a read, a write, and a path-bearing tool call."""
    rule = _rule("secret_file", MatchKind.FILE_PATH, PolicyAction.HALT_AGENT, pattern="*.pem")
    mutation = _ledger(category, {"path": f"{WORKSPACE}/deploy/server.pem"})
    outcomes = evaluate(mutation, [rule])
    assert [outcome.rule_name for outcome in outcomes] == ["secret_file"]
    assert outcomes[0].detail["pattern"] == "*.pem"
    assert outcomes[0].event_id == mutation.row_id


def test_file_path_rule_ignores_a_category_carrying_no_path() -> None:
    """A model response naming a path in prose is not a file access."""
    rule = _rule("secret_file", MatchKind.FILE_PATH, PolicyAction.HALT_AGENT, pattern="*.pem")
    mutation = _ledger(EventCategory.MODEL_RESPONSE, {"path": f"{WORKSPACE}/server.pem"})
    assert evaluate(mutation, [rule]) == []


def test_shell_command_rule_matches_the_command_string() -> None:
    """A command rule reads the payload command of a shell command Event."""
    rule = _rule(
        "recursive_removal",
        MatchKind.SHELL_COMMAND,
        PolicyAction.HALT_AGENT,
        pattern="*rm -rf*",
    )
    matched = _ledger(EventCategory.SHELL_COMMAND, {"command": "sudo rm -rf /var/data"})
    ignored = _ledger(EventCategory.SHELL_COMMAND, {"command": "ls -la"})
    assert governing_action(evaluate(matched, [rule])) is PolicyAction.HALT_AGENT
    assert evaluate(ignored, [rule]) == []


def test_client_rule_compares_the_client() -> None:
    """A Client rule matches the mutation's Client and nothing else."""
    rule = _rule(
        "watched_client",
        MatchKind.CLIENT,
        PolicyAction.WARN,
        client_id=CLIENT_ID,
    )
    assert evaluate(_ledger(EventCategory.USER_PROMPT, client_id=CLIENT_ID), [rule]) != []
    assert evaluate(_ledger(EventCategory.USER_PROMPT, client_id=OTHER_CLIENT_ID), [rule]) == []


def test_session_cost_rule_compares_the_accrued_cost() -> None:
    """A cost rule fires above its threshold and not at it."""
    rule = _rule(
        "cost_ceiling",
        MatchKind.SESSION_COST,
        PolicyAction.REQUIRE_APPROVAL,
        threshold=5.0,
    )
    above = _ledger(EventCategory.COST_RECORD, cost=Decimal("5.01"))
    at = _ledger(EventCategory.COST_RECORD, cost=Decimal("5.00"))
    assert governing_action(evaluate(above, [rule])) is PolicyAction.REQUIRE_APPROVAL
    assert evaluate(at, [rule]) == []
    assert evaluate(above, [rule])[0].detail["cost_usd"] == "5.01"


def test_error_rate_rule_divides_over_the_trailing_window() -> None:
    """A rate rule counts errors over the last window Events, and an empty window has no rate."""
    rule = _rule(
        "error_ceiling",
        MatchKind.ERROR_RATE,
        PolicyAction.HALT_AGENT,
        threshold=0.5,
        window_events=4,
    )
    quiet = (EventCategory.TOOL_CALL,) * 6
    noisy = quiet + (EventCategory.ERROR,) * 3
    assert error_rate_over(noisy, 4) == pytest.approx(0.75)
    assert error_rate_over((), 4) is None
    assert evaluate(_ledger(EventCategory.ERROR, recent=quiet), [rule]) == []
    outcomes = evaluate(_ledger(EventCategory.ERROR, recent=noisy), [rule])
    assert governing_action(outcomes) is PolicyAction.HALT_AGENT
    assert outcomes[0].detail["window_events"] == 4


def test_a_derived_artifact_mutation_carries_no_event() -> None:
    """A Derived_Artifact mutation still matches a Client rule and records no Event."""
    rule = _rule("watched_client", MatchKind.CLIENT, PolicyAction.WARN, client_id=CLIENT_ID)
    mutation = Mutation(
        table=MutationTable.DERIVED_ARTIFACT,
        row_id=uuid4(),
        session_id=uuid4(),
        client_id=CLIENT_ID,
        occurred_at=_instant(),
    )
    outcomes = evaluate(mutation, [rule])
    assert outcomes[0].event_id is None
    assert mutation.path_subject is None


@pytest.mark.parametrize(
    ("pattern", "subject", "expected"),
    [
        # A short glob names a file wherever it sits in the workspace.
        (".env", f"{WORKSPACE}/.env", True),
        (".env", f"{WORKSPACE}/config/.env", True),
        (".env", f"{WORKSPACE}/.environment", False),
        # A rooted glob stays rooted.
        ("/etc/", "/etc/shadow", True),
        ("/etc/", f"{WORKSPACE}/etc/shadow", False),
        # A trailing separator means the directory and everything beneath it.
        (".ssh/", f"{WORKSPACE}/.ssh/config", True),
        (".ssh/", f"{WORKSPACE}/.ssh/keys/inner", True),
        # A home reference is stripped rather than expanded against this machine.
        ("~/.ssh/", "/home/other/.ssh/id_rsa", True),
        # A crossing wildcard crosses separators and a single one does not.
        ("secrets/**", f"{WORKSPACE}/secrets/a/b", True),
        ("secrets/*", f"{WORKSPACE}/secrets/a/b", False),
        # A reverse separator is normalised before the comparison.
        (".aws/credentials", "C:\\Users\\one\\.aws\\credentials", True),
        # A regular expression is recognised by its prefix and searched for.
        ("re:[.]env([.].*)?$", f"{WORKSPACE}/.env.production", True),
        ("re:[.]env([.].*)?$", f"{WORKSPACE}/settings.toml", False),
    ],
)
def test_path_pattern_language(pattern: str, subject: str, expected: bool) -> None:
    """The path glob and regular-expression conventions behave as the module states."""
    assert compile_pattern(pattern, PatternMode.PATH).matches(subject) is expected


def test_text_pattern_matches_the_whole_command() -> None:
    """A command glob is matched in full, so a bare word does not match a longer command."""
    assert compile_pattern("git push*", PatternMode.TEXT).matches("git push --force")
    assert not compile_pattern("git push", PatternMode.TEXT).matches("git push --force")
    assert compile_pattern("re:--force", PatternMode.TEXT).matches("git push --force")


def test_sensitive_pattern_set_covers_the_three_classes() -> None:
    """Credential files, key material, and environment files are each covered."""
    groups = {pattern.group for pattern in BUILTIN_SENSITIVE_PATTERNS}
    assert {
        SensitiveGroup.CREDENTIAL_FILE,
        SensitiveGroup.KEY_MATERIAL,
        SensitiveGroup.ENVIRONMENT_FILE,
    } <= groups
    texts = {pattern.pattern for pattern in BUILTIN_SENSITIVE_PATTERNS}
    # The configuration surface's coarse default is a subset, so a deployment that
    # configures nothing gets the richer set rather than the trio.
    assert frozenset(BUILTIN_SENSITIVE_PATHS) <= texts


@pytest.mark.parametrize(
    "path",
    [
        "/home/one/.aws/credentials",
        "/etc/shadow",
        f"{WORKSPACE}/.git-credentials",
        "/home/one/.ssh/id_ed25519",
        f"{WORKSPACE}/deploy/server.pem",
        f"{WORKSPACE}/keys/inner/material",
        f"{WORKSPACE}/.env",
        f"{WORKSPACE}/.env.production",
    ],
)
def test_builtin_rules_detect_sensitive_access(path: str) -> None:
    """Every sensitive class is detected by the built-in set with a non-allow action."""
    outcomes = evaluate(_read(path), builtin_rules())
    assert outcomes, path
    action = governing_action(outcomes)
    assert action is not None
    assert action is not PolicyAction.ALLOW


def test_builtin_rules_leave_an_ordinary_file_alone() -> None:
    """An ordinary source file matches nothing in the built-in set."""
    assert evaluate(_read(f"{WORKSPACE}/src/service/handler.py"), builtin_rules()) == []


def test_configured_sensitive_paths_extend_the_builtin_set() -> None:
    """An operator-supplied pattern is added without losing the built-in ones."""
    extended = builtin_rules(("vault/**",))
    assert len(extended) == len(builtin_rules()) + 1
    assert evaluate(_read(f"{WORKSPACE}/vault/token"), extended) != []
    assert evaluate(_read(f"{WORKSPACE}/vault/token"), builtin_rules()) == []


def test_resolution_is_independent_of_rule_order() -> None:
    """Permuting the rule list leaves the outcome list and the governing action identical."""
    rules = [
        _rule("warn_env", MatchKind.FILE_PATH, PolicyAction.WARN, pattern=".env"),
        _rule("halt_env", MatchKind.FILE_PATH, PolicyAction.HALT_AGENT, pattern="*.env*"),
        _rule(
            "approve_client", MatchKind.CLIENT, PolicyAction.REQUIRE_APPROVAL, client_id=CLIENT_ID
        ),
        _rule("allow_all", MatchKind.FILE_PATH, PolicyAction.ALLOW, pattern="**"),
    ]
    mutation = _read(f"{WORKSPACE}/.env")
    forward = evaluate(mutation, rules)
    reversed_order = evaluate(mutation, list(reversed(rules)))
    rotated = evaluate(mutation, rules[2:] + rules[:2])
    assert forward == reversed_order == rotated
    assert governing_action(forward) is PolicyAction.HALT_AGENT
    assert triggered_actions(forward) == (
        PolicyAction.HALT_AGENT,
        PolicyAction.REQUIRE_APPROVAL,
        PolicyAction.WARN,
        PolicyAction.ALLOW,
    )


def test_a_rule_offered_twice_yields_one_outcome() -> None:
    """Outcomes are keyed by rule identifier, so a repeated rule is not counted twice."""
    rule = _rule("halt_env", MatchKind.FILE_PATH, PolicyAction.HALT_AGENT, pattern=".env")
    assert len(evaluate(_read(f"{WORKSPACE}/.env"), [rule, rule, rule])) == 1


def test_a_disabled_rule_is_not_evaluated() -> None:
    """A disabled rule contributes nothing, which is what criterion 23.4 means by enabled."""
    rule = _rule(
        "halt_env",
        MatchKind.FILE_PATH,
        PolicyAction.HALT_AGENT,
        pattern=".env",
        enabled=False,
    )
    assert evaluate(_read(f"{WORKSPACE}/.env"), [rule]) == []


def _configuration(rules_path: str | None, sensitive: str | None = None) -> Configuration:
    environ: dict[str, str] = {}
    if rules_path is not None:
        environ["MOLT_POLICY_RULES_PATH"] = rules_path
    if sensitive is not None:
        environ["MOLT_SENSITIVE_PATHS"] = sensitive
    return Configuration(environ=environ)


def test_an_unconfigured_path_selects_the_builtin_set() -> None:
    """No configured path means the built-in set, extended by the configured paths."""
    assert load_rules(_configuration(None)) == builtin_rules()
    extended = load_rules(_configuration(None, "vault/**"))
    assert len(extended) == len(builtin_rules()) + 1


def test_a_rules_file_replaces_the_builtin_set(tmp_path: Path) -> None:
    """A configured file is the whole policy rather than an addition to the built-in set."""
    document = """
[[rule]]
name = "halt_on_production_write"
match_kind = "file_path"
pattern = "production/**"
action = "halt_agent"

[[rule]]
name = "approve_expensive_session"
match_kind = "session_cost"
threshold = 25.0
action = "require_approval"

[[rule]]
name = "retired_rule"
match_kind = "shell_command"
pattern = "*curl*"
action = "warn"
enabled = false
"""
    path = tmp_path / "rules.toml"
    path.write_text(document, encoding="utf-8")
    rules = load_rules(_configuration(str(path)))
    assert [rule.name for rule in rules] == [
        "halt_on_production_write",
        "approve_expensive_session",
        "retired_rule",
    ]
    assert rules[2].enabled is False
    # The built-in sensitive detections are gone, because the file is the policy.
    assert evaluate(_read(f"{WORKSPACE}/.env"), rules) == []
    assert evaluate(_read(f"{WORKSPACE}/production/config"), rules) != []


def test_a_file_rule_identifier_is_derived_from_its_name(tmp_path: Path) -> None:
    """A rule with no identifier gets the same one in every process."""
    document = f"""
[[rule]]
name = "one"
match_kind = "client"
client_id = "{CLIENT_ID}"
action = "warn"
"""
    path = tmp_path / "rules.toml"
    path.write_text(document, encoding="utf-8")
    rules = load_rules_file(path)
    assert rules[0].id == rule_identifier("one")
    assert rules[0].client_id == CLIENT_ID


# The malformed rules files, one fault each: an unknown top-level key, an unknown
# key inside a rule, an unknown match kind, an unknown action, a path rule with no
# pattern, a cost rule with no threshold, an unusable expression, a window naming
# no Events, and the same rule name twice.
UNKNOWN_SECTION = """
[[policy]]
name = "one"
"""

UNKNOWN_RULE_KEY = """
[[rule]]
name = "one"
match_kind = "client"
action = "warn"
speed = 1
"""

UNKNOWN_MATCH_KIND = """
[[rule]]
name = "one"
match_kind = "phase_of_moon"
action = "warn"
"""

UNKNOWN_ACTION = """
[[rule]]
name = "one"
match_kind = "client"
action = "shout"
"""

PATH_RULE_WITHOUT_PATTERN = """
[[rule]]
name = "one"
match_kind = "file_path"
action = "warn"
"""

COST_RULE_WITHOUT_THRESHOLD = """
[[rule]]
name = "one"
match_kind = "session_cost"
action = "warn"
"""

UNUSABLE_EXPRESSION = """
[[rule]]
name = "one"
match_kind = "file_path"
pattern = "re:[unclosed"
action = "warn"
"""

EMPTY_WINDOW = """
[[rule]]
name = "one"
match_kind = "error_rate"
threshold = 0.5
window_events = 0
action = "warn"
"""

DUPLICATE_NAME = """
[[rule]]
name = "one"
match_kind = "session_cost"
threshold = 1.0
action = "warn"

[[rule]]
name = "one"
match_kind = "session_cost"
threshold = 2.0
action = "warn"
"""


@pytest.mark.parametrize(
    "document",
    [
        UNKNOWN_SECTION,
        UNKNOWN_RULE_KEY,
        UNKNOWN_MATCH_KIND,
        UNKNOWN_ACTION,
        PATH_RULE_WITHOUT_PATTERN,
        COST_RULE_WITHOUT_THRESHOLD,
        UNUSABLE_EXPRESSION,
        EMPTY_WINDOW,
        DUPLICATE_NAME,
    ],
)
def test_a_malformed_rules_file_is_refused(tmp_path: Path, document: str) -> None:
    """Every malformed rule is a reported fault rather than a silently dropped rule."""
    path = tmp_path / "rules.toml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_rules_file(path)


def test_an_absent_rules_file_is_refused(tmp_path: Path) -> None:
    """A configured path that names nothing is a fault rather than a fallback."""
    with pytest.raises(ConfigError):
        load_rules_file(tmp_path / "absent.toml")


@pytest.mark.parametrize("kind", list(MatchKind))
def test_a_rule_missing_its_own_shape_is_refused(kind: MatchKind) -> None:
    """The rule shape check of migration 005 is restated in the model."""
    with pytest.raises(ValueError, match=r"must carry|must name"):
        _rule("incomplete", kind, PolicyAction.WARN)
