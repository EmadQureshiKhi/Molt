"""The Policy_Rule shape, the pattern language, and where a rule set comes from.

This module holds everything about a rule that is *not* the act of evaluating one.
The split is deliberate: `molt.policy.evaluate` is a pure function of a mutation
and a rule set, so every impurity a rule set needs — reading the configured path,
reading a file, falling back to the built-in set — is collected here and nowhere
else.

**The five match kinds are the schema's five.** `MatchKind` and `PolicyAction`
carry the values of the `rule_match_kind_known` and `rule_action_known`
constraints of migration 005, in constraint order, and `PolicyRule.__post_init__`
restates `rule_shape_valid`: a path or command rule carries a pattern, a Client
rule carries a Client, and a cost or error-rate rule carries a threshold. A rule
that the cluster would refuse is therefore refused before it reaches an
evaluation.

**Severity is a fixed total order, not a per-rule number.** `halt_agent`,
`require_approval`, `warn`, `allow`, most severe first. It is total because a
mutation matching several rules must resolve to one outcome, and it is fixed
because a configurable ordering would make that resolution a property of a
deployment rather than of the system.

**The pattern language is stated rather than inherited.** A pattern is a glob
unless it opens with the regular-expression prefix. A path glob is
separator-aware: `**` crosses path separators, `*` and `?` do not, every other
character is literal. A glob that does not open at the root matches any trailing
run of whole segments, which is what lets a short pattern name a file wherever it
sits in a monitored workspace while a rooted pattern stays rooted. A trailing
separator means the directory and everything under it, and a leading home
reference is stripped rather than expanded, because the path being matched came
from someone else's machine and this process's home says nothing about it.

**The sensitive path pattern set covers three classes** — credential files, key
material, and environment files — and the coarse built-in trio of the
configuration surface is a subset of it, asserted by a test rather than by
comment, so the two cannot drift. Operator-supplied entries extend the set rather
than replacing it: a deployment adds the paths its own workspaces hold without
losing the ones every workspace holds.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Final
from uuid import UUID, uuid5

from molt.config.resolve import BUILTIN_SENSITIVE_PATHS, ConfigError, Configuration
from molt.models.event import JsonValue

__all__ = [
    "ACTION_SEVERITY_ORDER",
    "BUILTIN_SENSITIVE_PATTERNS",
    "CREDENTIAL_FILE_PATTERNS",
    "ENVIRONMENT_FILE_PATTERNS",
    "GROUP_ACTIONS",
    "KEY_MATERIAL_PATTERNS",
    "MATCH_KIND_VALUES",
    "PATTERN_MATCH_KINDS",
    "POLICY_ACTION_VALUES",
    "POLICY_RULE_NAMESPACE",
    "REGEX_PREFIX",
    "RULES_PATH_SETTING",
    "RULES_TABLE_NAME",
    "RULE_FILE_KEYS",
    "SENSITIVE_PATHS_SETTING",
    "SENSITIVE_RULE_PREFIX",
    "CompiledPattern",
    "MatchKind",
    "PatternMode",
    "PolicyAction",
    "PolicyRule",
    "SensitiveGroup",
    "SensitivePattern",
    "builtin_rules",
    "compile_pattern",
    "configured_sensitive_patterns",
    "load_rules",
    "load_rules_file",
    "more_severe",
    "normalise_path",
    "path_suffixes",
    "pattern_mode_for",
    "rule_identifier",
    "sensitive_path_rules",
    "severity_rank",
]

# The two configuration keys a rule set is drawn from. They are named here so a
# caller reads the surface through one spelling.
RULES_PATH_SETTING: Final[str] = "MOLT_POLICY_RULES_PATH"
SENSITIVE_PATHS_SETTING: Final[str] = "MOLT_SENSITIVE_PATHS"


class MatchKind(StrEnum):
    """What a Policy_Rule matches against, in schema-constraint order."""

    FILE_PATH = "file_path"
    SHELL_COMMAND = "shell_command"
    CLIENT = "client"
    SESSION_COST = "session_cost"
    ERROR_RATE = "error_rate"


class PolicyAction(StrEnum):
    """What a matched Policy_Rule asks for, in schema-constraint order."""

    ALLOW = "allow"
    WARN = "warn"
    REQUIRE_APPROVAL = "require_approval"
    HALT_AGENT = "halt_agent"


# The values in the order the schema check constraints list them. A migration
# test reads these tuples rather than restating the values.
MATCH_KIND_VALUES: Final[tuple[str, ...]] = tuple(member.value for member in MatchKind)
POLICY_ACTION_VALUES: Final[tuple[str, ...]] = tuple(member.value for member in PolicyAction)

# The severity order, most severe first. Fixed and total.
ACTION_SEVERITY_ORDER: Final[tuple[PolicyAction, ...]] = (
    PolicyAction.HALT_AGENT,
    PolicyAction.REQUIRE_APPROVAL,
    PolicyAction.WARN,
    PolicyAction.ALLOW,
)

# The match kinds that carry a pattern rather than a Client or a threshold.
PATTERN_MATCH_KINDS: Final[tuple[MatchKind, ...]] = (
    MatchKind.FILE_PATH,
    MatchKind.SHELL_COMMAND,
)

_SEVERITY_RANK: Final[Mapping[PolicyAction, int]] = MappingProxyType(
    {action: rank for rank, action in enumerate(ACTION_SEVERITY_ORDER)}
)

if len(_SEVERITY_RANK) != len(tuple(PolicyAction)):
    raise ValueError("the severity order must rank every Policy_Action exactly once")


def severity_rank(action: PolicyAction) -> int:
    """The position of an action in the fixed severity order, zero being most severe."""
    return _SEVERITY_RANK[PolicyAction(action)]


def more_severe(left: PolicyAction, right: PolicyAction) -> PolicyAction:
    """The more severe of two actions, the left one winning a tie.

    The tie rule costs nothing because a tie means the two actions are equal, and
    stating it is what makes the reduction over a set of actions associative and
    therefore independent of the order the set was walked in.
    """
    return left if severity_rank(left) <= severity_rank(right) else right


# --------------------------------------------------------------------------
# The pattern language
# --------------------------------------------------------------------------

# A pattern opening with this prefix is a regular expression; the remainder is
# searched for anywhere in the subject, so an unanchored expression reads as a
# containment test and a whole-subject match is spelled with its own anchors.
REGEX_PREFIX: Final[str] = "re:"

# One path segment: any run of characters other than the separator.
_SEGMENT: Final[str] = "[^/]"


class PatternMode(StrEnum):
    """Whether a pattern is matched against a path or against free text."""

    PATH = "path"
    TEXT = "text"


def pattern_mode_for(kind: MatchKind) -> PatternMode:
    """The pattern mode a pattern-carrying match kind is matched in."""
    resolved = MatchKind(kind)
    if resolved is MatchKind.FILE_PATH:
        return PatternMode.PATH
    if resolved is MatchKind.SHELL_COMMAND:
        return PatternMode.TEXT
    raise ValueError(f"the match kind {resolved.value} carries no pattern")


def normalise_path(subject: str) -> str:
    """Render a captured path in the one form patterns are matched against.

    Reverse separators become forward ones and repeated separators collapse, so a
    path captured on a machine with either convention matches the same pattern.
    Nothing else is changed: no case folding, because a path is case-sensitive
    where it matters, and no resolution against this filesystem, because the path
    describes a monitored workspace that may not exist here at all.
    """
    text = subject.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text


def path_suffixes(path: str) -> tuple[str, ...]:
    """The whole path followed by every trailing run of whole segments."""
    suffixes = [path]
    for index, character in enumerate(path):
        if character == "/" and index + 1 < len(path):
            suffixes.append(path[index + 1 :])
    return tuple(suffixes)


@dataclass(frozen=True, slots=True)
class CompiledPattern:
    """One pattern compiled to the expression and the matching discipline it implies.

    Attributes:
        mode: Whether the subject is a path or free text.
        is_regex: Whether the pattern was written as a regular expression.
        anchored: Whether a path glob opened at the root, in which case it is
            matched against the whole path rather than against trailing segments.
        expression: The compiled expression.
    """

    mode: PatternMode
    is_regex: bool
    anchored: bool
    expression: re.Pattern[str]

    def matches(self, subject: str) -> bool:
        """Whether this pattern matches a subject."""
        text = normalise_path(subject) if self.mode is PatternMode.PATH else subject
        if self.is_regex:
            return self.expression.search(text) is not None
        if self.mode is PatternMode.TEXT or self.anchored:
            return self.expression.fullmatch(text) is not None
        return any(
            self.expression.fullmatch(candidate) is not None for candidate in path_suffixes(text)
        )


def _translate_path_glob(pattern: str) -> str:
    """Translate a separator-aware glob into an expression matched in full."""
    parts: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        character = pattern[index]
        if character == "*":
            if pattern.startswith("**", index):
                index += 2
                if pattern.startswith("/", index):
                    index += 1
                    # A crossing wildcard followed by a separator also matches no
                    # segment at all, so one pattern covers a file at the root and
                    # the same file further down.
                    parts.append("(?:.*/)?")
                else:
                    parts.append(".*")
                continue
            parts.append(f"{_SEGMENT}*")
        elif character == "?":
            parts.append(_SEGMENT)
        else:
            parts.append(re.escape(character))
        index += 1
    return "".join(parts)


def _translate_text_glob(pattern: str) -> str:
    """Translate a glob over free text, where a wildcard crosses everything."""
    parts: list[str] = []
    for character in pattern:
        if character == "*":
            parts.append(".*")
        elif character == "?":
            parts.append(".")
        else:
            parts.append(re.escape(character))
    return "".join(parts)


def _normalise_glob(pattern: str) -> str:
    """Apply the two path-glob conventions: home reference and trailing separator."""
    text = pattern.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if text.startswith("~/"):
        text = text[2:]
    elif text == "~":
        text = ""
    if text.endswith("/"):
        text = f"{text}**"
    return text


@lru_cache(maxsize=512)
def compile_pattern(pattern: str, mode: PatternMode = PatternMode.PATH) -> CompiledPattern:
    """Compile a rule pattern, refusing an empty one and an invalid expression.

    Compilation is cached because a rule set is applied to every mutation and the
    same handful of patterns is compiled once per process rather than once per
    mutation. The cache holds no state a result depends on, so the function is
    still a function of its arguments alone.
    """
    if pattern.startswith(REGEX_PREFIX):
        body = pattern[len(REGEX_PREFIX) :]
        if not body:
            raise ValueError("a regular-expression pattern must carry an expression")
        try:
            expression = re.compile(body)
        except re.error as exc:
            raise ValueError(f"the pattern {pattern!r} is not a valid regular expression") from exc
        return CompiledPattern(mode=mode, is_regex=True, anchored=False, expression=expression)
    if not pattern:
        raise ValueError("a pattern must be non-empty")
    if mode is PatternMode.TEXT:
        return CompiledPattern(
            mode=mode,
            is_regex=False,
            anchored=False,
            expression=re.compile(_translate_text_glob(pattern)),
        )
    normalised = _normalise_glob(pattern)
    if not normalised:
        raise ValueError("a path pattern must name something after normalisation")
    return CompiledPattern(
        mode=mode,
        is_regex=False,
        anchored=normalised.startswith("/"),
        expression=re.compile(_translate_path_glob(normalised)),
    )


# --------------------------------------------------------------------------
# The rule shape
# --------------------------------------------------------------------------

# The namespace every derived rule identifier is generated in. A rule the
# operator did not give an identifier to gets one derived from its name, so the
# same rule carries the same identifier in every process and on every machine —
# which is what lets the deduplicating uniqueness constraints on `policy_match`
# and `approval_queue` recognise a redelivered mutation after a restart.
POLICY_RULE_NAMESPACE: Final[UUID] = UUID("b7c1a5e2-3d64-4f8a-9c0b-6e2d1f7a48c3")


def rule_identifier(name: str) -> UUID:
    """The identifier derived from a rule name."""
    return uuid5(POLICY_RULE_NAMESPACE, name)


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One enabled or disabled rule, shaped as the `policy_rule` row is shaped.

    The stored row's creation timestamp is absent on purpose: it records when the
    row was written and takes no part in deciding whether a mutation matches, and
    a rule set assembled in memory would have to read a clock to invent one.
    """

    id: UUID
    name: str
    match_kind: MatchKind
    action: PolicyAction
    enabled: bool = True
    pattern: str | None = None
    client_id: UUID | None = None
    threshold: float | None = None
    window_events: int | None = None

    def __post_init__(self) -> None:
        kind = MatchKind(self.match_kind)
        PolicyAction(self.action)
        if not self.name:
            raise ValueError("a Policy_Rule name must be non-empty")
        if kind in PATTERN_MATCH_KINDS:
            if not self.pattern:
                raise ValueError(f"the {kind.value} rule {self.name!r} must carry a pattern")
            # Compiling here rather than at match time means an unusable pattern
            # is a rule-set fault reported when the set is built, and evaluation
            # of a well-formed set cannot raise.
            compile_pattern(self.pattern, pattern_mode_for(kind))
        elif kind is MatchKind.CLIENT:
            if self.client_id is None:
                raise ValueError(f"the client rule {self.name!r} must name a Client")
        elif self.threshold is None:
            raise ValueError(f"the {kind.value} rule {self.name!r} must carry a threshold")
        if self.window_events is not None and self.window_events <= 0:
            raise ValueError(f"the rule {self.name!r} must carry a positive event window")

    @property
    def severity(self) -> int:
        """This rule's position in the fixed severity order."""
        return severity_rank(self.action)


# --------------------------------------------------------------------------
# The sensitive path pattern set
# --------------------------------------------------------------------------


class SensitiveGroup(StrEnum):
    """The classes of sensitive path the built-in set covers.

    The first three are the classes Requirement 23.11 names. The fourth holds
    whatever the deployment added, kept as its own class so a report can say which
    patterns are the system's and which are the operator's.
    """

    CREDENTIAL_FILE = "credential_file"
    KEY_MATERIAL = "key_material"
    ENVIRONMENT_FILE = "environment_file"
    CONFIGURED = "configured"


# The action each class asks for. Credential material asks a human, because a
# person deciding is the only proportionate response to an agent opening a private
# key. Environment files only warn, because an agent reads them constantly and
# blocking that would make the watcher the thing that broke the fleet.
GROUP_ACTIONS: Final[Mapping[SensitiveGroup, PolicyAction]] = MappingProxyType(
    {
        SensitiveGroup.CREDENTIAL_FILE: PolicyAction.REQUIRE_APPROVAL,
        SensitiveGroup.KEY_MATERIAL: PolicyAction.REQUIRE_APPROVAL,
        SensitiveGroup.ENVIRONMENT_FILE: PolicyAction.WARN,
        SensitiveGroup.CONFIGURED: PolicyAction.REQUIRE_APPROVAL,
    }
)

# The prefix every sensitive-path rule name opens with, so an operator reading a
# match row can tell a built-in detection from a rule they wrote.
SENSITIVE_RULE_PREFIX: Final[str] = "sensitive_path"


@dataclass(frozen=True, slots=True)
class SensitivePattern:
    """One sensitive path pattern, its class, and the slug its rule is named for."""

    slug: str
    group: SensitiveGroup
    pattern: str

    def __post_init__(self) -> None:
        SensitiveGroup(self.group)
        if not self.slug:
            raise ValueError("a sensitive path pattern must carry a slug")
        compile_pattern(self.pattern, PatternMode.PATH)

    @property
    def rule_name(self) -> str:
        """The name of the rule this pattern becomes."""
        return f"{SENSITIVE_RULE_PREFIX}.{self.group.value}.{self.slug}"

    @property
    def action(self) -> PolicyAction:
        """The action this pattern's class asks for."""
        return GROUP_ACTIONS[SensitiveGroup(self.group)]


# Credential files: the files a tool keeps a usable credential in, plus the system
# configuration directory, which holds the host's own credential databases.
CREDENTIAL_FILE_PATTERNS: Final[tuple[SensitivePattern, ...]] = (
    SensitivePattern("system_configuration", SensitiveGroup.CREDENTIAL_FILE, "/etc/"),
    SensitivePattern("network_login", SensitiveGroup.CREDENTIAL_FILE, ".netrc"),
    SensitivePattern("database_password", SensitiveGroup.CREDENTIAL_FILE, ".pgpass"),
    SensitivePattern("version_control", SensitiveGroup.CREDENTIAL_FILE, ".git-credentials"),
    SensitivePattern("cloud_credentials", SensitiveGroup.CREDENTIAL_FILE, ".aws/credentials"),
    SensitivePattern("package_registry", SensitiveGroup.CREDENTIAL_FILE, ".npmrc"),
    SensitivePattern("package_index", SensitiveGroup.CREDENTIAL_FILE, ".pypirc"),
    SensitivePattern("container_registry", SensitiveGroup.CREDENTIAL_FILE, ".docker/config.json"),
    SensitivePattern("orchestrator_context", SensitiveGroup.CREDENTIAL_FILE, ".kube/config"),
    SensitivePattern("service_account", SensitiveGroup.CREDENTIAL_FILE, "credentials.json"),
    SensitivePattern("web_server_password", SensitiveGroup.CREDENTIAL_FILE, "*.htpasswd"),
)

# Key material: the directories a key is conventionally kept in, and the file
# shapes a key is kept as, so a key written outside a conventional directory is
# still recognised.
KEY_MATERIAL_PATTERNS: Final[tuple[SensitivePattern, ...]] = (
    SensitivePattern("shell_key_directory", SensitiveGroup.KEY_MATERIAL, "~/.ssh/"),
    SensitivePattern("signing_key_directory", SensitiveGroup.KEY_MATERIAL, ".gnupg/"),
    SensitivePattern("secret_directory", SensitiveGroup.KEY_MATERIAL, ".secrets/"),
    SensitivePattern("plain_secret_directory", SensitiveGroup.KEY_MATERIAL, "secrets/"),
    SensitivePattern("key_directory", SensitiveGroup.KEY_MATERIAL, "keys/"),
    SensitivePattern("armoured_key", SensitiveGroup.KEY_MATERIAL, "*.pem"),
    SensitivePattern("bare_key", SensitiveGroup.KEY_MATERIAL, "*.key"),
    SensitivePattern("key_bundle", SensitiveGroup.KEY_MATERIAL, "*.p12"),
    SensitivePattern("exchange_bundle", SensitiveGroup.KEY_MATERIAL, "*.pfx"),
    SensitivePattern("key_store", SensitiveGroup.KEY_MATERIAL, "*.keystore"),
    SensitivePattern("identity_key_rsa", SensitiveGroup.KEY_MATERIAL, "id_rsa*"),
    SensitivePattern("identity_key_dsa", SensitiveGroup.KEY_MATERIAL, "id_dsa*"),
    SensitivePattern("identity_key_ecdsa", SensitiveGroup.KEY_MATERIAL, "id_ecdsa*"),
    SensitivePattern("identity_key_ed25519", SensitiveGroup.KEY_MATERIAL, "id_ed25519*"),
)

# Environment files: the files a project keeps its own configured secrets in.
ENVIRONMENT_FILE_PATTERNS: Final[tuple[SensitivePattern, ...]] = (
    SensitivePattern("environment", SensitiveGroup.ENVIRONMENT_FILE, ".env"),
    SensitivePattern("environment_variant", SensitiveGroup.ENVIRONMENT_FILE, ".env.*"),
    SensitivePattern("environment_suffixed", SensitiveGroup.ENVIRONMENT_FILE, "*.env"),
    SensitivePattern("environment_shell", SensitiveGroup.ENVIRONMENT_FILE, ".envrc"),
)

BUILTIN_SENSITIVE_PATTERNS: Final[tuple[SensitivePattern, ...]] = (
    *CREDENTIAL_FILE_PATTERNS,
    *KEY_MATERIAL_PATTERNS,
    *ENVIRONMENT_FILE_PATTERNS,
)


def _reject_duplicate_slugs(patterns: Iterable[SensitivePattern]) -> None:
    seen: set[str] = set()
    for pattern in patterns:
        if pattern.rule_name in seen:
            raise ValueError(f"the sensitive path pattern {pattern.rule_name} is declared twice")
        seen.add(pattern.rule_name)


_reject_duplicate_slugs(BUILTIN_SENSITIVE_PATTERNS)


def configured_sensitive_patterns(
    extra: Sequence[str] = (),
) -> tuple[SensitivePattern, ...]:
    """The built-in pattern set extended by operator-supplied patterns.

    An operator-supplied pattern is slugged from a digest of the pattern text
    rather than from its position, so the rule it becomes carries the same name and
    the same identifier however the configured list was ordered.
    """
    known = {pattern.pattern for pattern in BUILTIN_SENSITIVE_PATTERNS}
    added: list[SensitivePattern] = []
    for text in extra:
        candidate = text.strip()
        if not candidate or candidate in known:
            continue
        known.add(candidate)
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
        added.append(SensitivePattern(digest, SensitiveGroup.CONFIGURED, candidate))
    return (*BUILTIN_SENSITIVE_PATTERNS, *sorted(added, key=lambda item: item.slug))


def sensitive_path_rules(patterns: Sequence[SensitivePattern]) -> tuple[PolicyRule, ...]:
    """One enabled `file_path` rule per sensitive path pattern."""
    return tuple(
        PolicyRule(
            id=rule_identifier(pattern.rule_name),
            name=pattern.rule_name,
            match_kind=MatchKind.FILE_PATH,
            action=pattern.action,
            pattern=pattern.pattern,
        )
        for pattern in patterns
    )


def builtin_rules(sensitive_paths: Sequence[str] = ()) -> tuple[PolicyRule, ...]:
    """The rule set a deployment gets when it configures no rules file.

    The set is the sensitive path detections and nothing else. That is not
    minimalism for its own sake: a sensitive path detection is obliged by a
    requirement, whereas a cost ceiling and an error-rate ceiling are numbers only
    a deployment can choose, and a built-in value for either would be a policy
    this codebase invented and every operator inherited.
    """
    return sensitive_path_rules(configured_sensitive_patterns(sensitive_paths))


# --------------------------------------------------------------------------
# Loading a rule set
# --------------------------------------------------------------------------

# The one array of tables a rules file holds.
RULES_TABLE_NAME: Final[str] = "rule"

# The keys a rule table admits, matching the columns of `policy_rule` that
# describe a rule rather than record its writing.
RULE_FILE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "action",
        "client_id",
        "enabled",
        "id",
        "match_kind",
        "name",
        "pattern",
        "threshold",
        "window_events",
    }
)


def load_rules_file(path: Path) -> tuple[PolicyRule, ...]:
    """Read a rules file, refusing anything the rule shape or the schema disallows.

    A rules file replaces the built-in set rather than extending it, which is what
    the configuration surface means by naming a path: an operator who writes the
    file is stating the whole policy. Every fault names the rule and never a value,
    because the message reaches a log record.
    """
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"the policy rules file {path} could not be read") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the policy rules file {path} is not valid") from exc

    unknown = sorted(set(document) - {RULES_TABLE_NAME})
    if unknown:
        raise ConfigError(f"the policy rules file {path} holds no key named {unknown[0]!r}")
    entries = document.get(RULES_TABLE_NAME, [])
    if not isinstance(entries, list):
        raise ConfigError(f"the policy rules file {path} must hold an array of rule tables")

    rules: list[PolicyRule] = []
    names: set[str] = set()
    identifiers: set[UUID] = set()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"rule {position} of the policy rules file {path} is not a table")
        rule = _rule_from_entry({str(key): value for key, value in entry.items()}, path, position)
        if rule.name in names:
            raise ConfigError(f"the policy rules file {path} declares {rule.name!r} twice")
        if rule.id in identifiers:
            raise ConfigError(
                f"the policy rules file {path} gives two rules one identifier, "
                f"the second being {rule.name!r}"
            )
        names.add(rule.name)
        identifiers.add(rule.id)
        rules.append(rule)
    return tuple(rules)


def _rule_from_entry(entry: dict[str, JsonValue], path: Path, position: int) -> PolicyRule:
    """Build one rule from one table of a rules file."""
    where = f"rule {position} of the policy rules file {path}"
    unknown = sorted(set(entry) - RULE_FILE_KEYS)
    if unknown:
        raise ConfigError(f"{where} holds no key named {unknown[0]!r}")
    name = _required_text(entry, "name", where)
    described = f"the rule {name!r} of the policy rules file {path}"
    kind_text = _required_text(entry, "match_kind", described)
    action_text = _required_text(entry, "action", described)
    if kind_text not in MATCH_KIND_VALUES:
        raise ConfigError(f"{described} names no known match kind")
    if action_text not in POLICY_ACTION_VALUES:
        raise ConfigError(f"{described} names no known action")
    identifier_text = _text(entry, "id", described)
    client_text = _text(entry, "client_id", described)
    try:
        return PolicyRule(
            id=rule_identifier(name) if identifier_text is None else UUID(identifier_text),
            name=name,
            match_kind=MatchKind(kind_text),
            action=PolicyAction(action_text),
            enabled=_flag(entry, "enabled", described),
            pattern=_text(entry, "pattern", described),
            client_id=None if client_text is None else UUID(client_text),
            threshold=_number(entry, "threshold", described),
            window_events=_integer(entry, "window_events", described),
        )
    except ValueError as exc:
        raise ConfigError(f"{described} is not a usable rule: {exc}") from exc


def _text(entry: dict[str, JsonValue], key: str, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where} must give {key!r} as non-empty text")
    return value


def _required_text(entry: dict[str, JsonValue], key: str, where: str) -> str:
    value = _text(entry, key, where)
    if value is None:
        raise ConfigError(f"{where} omits the required key {key!r}")
    return value


def _flag(entry: dict[str, JsonValue], key: str, where: str) -> bool:
    value = entry.get(key)
    if value is None:
        return True
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must give {key!r} as a boolean")
    return value


def _number(entry: dict[str, JsonValue], key: str, where: str) -> float | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must give {key!r} as a number")
    return float(value)


def _integer(entry: dict[str, JsonValue], key: str, where: str) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must give {key!r} as a whole number")
    return value


def load_rules(configuration: Configuration) -> tuple[PolicyRule, ...]:
    """The rule set the configuration selects: the file it names, or the built-in set.

    This is the one impure entry point of the policy layer's rule handling. It reads
    the configured path and it reads a file; it reads no clock and no database, and
    it decides nothing about a mutation. Evaluation is a separate function of the
    set this returns, which is what makes evaluation testable without a filesystem.
    """
    path = configuration.optional_path(RULES_PATH_SETTING)
    if path is None:
        return builtin_rules(configuration.text_list(SENSITIVE_PATHS_SETTING))
    return load_rules_file(path)


# The coarse trio the configuration surface carries as its default is a subset of
# the built-in pattern set, so a deployment that leaves the surface alone gets the
# richer set rather than the trio. A unit test asserts the containment; this check
# states the intent at import so the two cannot silently diverge.
_BUILTIN_PATTERN_TEXTS: Final[frozenset[str]] = frozenset(
    pattern.pattern for pattern in BUILTIN_SENSITIVE_PATTERNS
)
if not frozenset(BUILTIN_SENSITIVE_PATHS) <= _BUILTIN_PATTERN_TEXTS:
    raise ValueError(
        "the built-in sensitive path pattern set must contain the configuration surface's default"
    )
