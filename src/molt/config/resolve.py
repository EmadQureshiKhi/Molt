"""Configuration resolution over the whole configuration surface.

Resolution order for every key is the environment variable, then the
configuration file key, then the built-in default. The environment wins. A
required value that resolves from none of the three raises an error that prints
the environment variable name and the configuration file key, so an operator
learns what to set rather than what failed.

**No secret carries a default, and that is structural rather than conventional.**
Three secrets are accepted from the environment alone and are described by
`SecretSetting`, a shape with no default field at all, so a default cannot be
written for them. Every other credential-bearing key names *where* a secret
lives rather than holding one, and an import-time check refuses a table in
which any such key carries a default. A credential default is therefore not a
review finding, it is an unrepresentable state.

The table below is the single encoding of the configuration surface. The example
configuration file lists the same keys with non-secret placeholders, and a test
compares the two, so the surface and the example cannot drift apart.

The exception hierarchy lives here rather than in the shared error module
because configuration resolution is the first thing any process does and this
module must import with nothing else present. The shared taxonomy re-exports
these names rather than restating them, so one class means one failure.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, TypeAlias

from molt.models.event import JsonValue

__all__ = [
    "BUILTIN_SENSITIVE_NAMES",
    "BUILTIN_SENSITIVE_PATHS",
    "CREDENTIAL_MARKERS",
    "SECRET_SETTINGS",
    "SECTION_NAMES",
    "SETTINGS",
    "ConfigError",
    "ConfigValue",
    "Configuration",
    "InvalidConfigValueError",
    "Kind",
    "MissingConfigError",
    "SecretSetting",
    "Setting",
    "Source",
    "UnknownSettingError",
    "default_config_path",
    "load_config_file",
    "load_configuration",
]

# A resolved configuration value. Every key in the surface resolves to one of
# these; nothing resolves to a dynamic type, so a caller reads a value through
# the accessor matching its declared kind and gets that kind back.
ConfigValue: TypeAlias = str | int | float | bool | tuple[str, ...] | tuple[float, ...]


class ConfigError(Exception):
    """A configuration value is absent, unreadable, or not of its declared kind.

    Every message names the key and never the value, because a message reaches a
    log record and a value may be a credential.
    """


class MissingConfigError(ConfigError):
    """A required value resolved from neither the environment, the file, nor a default."""

    def __init__(self, env: str, key: str | None) -> None:
        self.env = env
        self.key = key
        where = (
            f"environment variable {env}" if key is None else f"{env} or configuration key {key}"
        )
        super().__init__(f"no configuration value is set for {where}")


class InvalidConfigValueError(ConfigError):
    """A value was present but is not of the kind its key declares."""

    def __init__(self, env: str, kind: Kind, detail: str) -> None:
        self.env = env
        self.kind = kind
        super().__init__(f"the configuration value for {env} must be {kind.article}: {detail}")


class UnknownSettingError(ConfigError):
    """A name was asked for, or a configuration file held a key, that the surface omits."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"the configuration surface holds no key named {name!r}")


class Kind(StrEnum):
    """The kind of value a key holds, which fixes how text is parsed."""

    TEXT = "text"
    PATH = "path"
    INTEGER = "integer"
    NUMBER = "number"
    FLAG = "flag"
    TEXT_LIST = "text_list"
    NUMBER_LIST = "number_list"

    @property
    def article(self) -> str:
        """The kind rendered for an error message."""
        return {
            Kind.TEXT: "text",
            Kind.PATH: "a filesystem path",
            Kind.INTEGER: "a whole number",
            Kind.NUMBER: "a number",
            Kind.FLAG: "a flag reading true or false",
            Kind.TEXT_LIST: "a comma-separated list of text values",
            Kind.NUMBER_LIST: "a comma-separated list of numbers",
        }[self]


class Source(StrEnum):
    """Where a resolved value came from, in precedence order."""

    ENVIRONMENT = "environment"
    FILE = "file"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class Setting:
    """One key of the configuration surface.

    Attributes:
        env: The environment variable, which wins over every other source.
        key: The dotted configuration file key.
        kind: The kind of value the key holds.
        default: The built-in default, or None when the key is required. A
            credential-bearing key must leave this None, which an import-time
            check enforces.
    """

    env: str
    key: str
    kind: Kind
    default: ConfigValue | None


@dataclass(frozen=True, slots=True)
class SecretSetting:
    """A secret accepted from the environment alone.

    This shape carries no default field and no configuration file key, so a
    secret can be neither defaulted nor committed. That is the whole reason it
    is a separate shape rather than a flag on `Setting`.

    Attributes:
        env: The environment variable the deployment injects the value into.
        purpose: What the value is for, for an error message.
    """

    env: str
    purpose: str


@dataclass(frozen=True, slots=True)
class Resolved:
    """A resolved value together with the source that supplied it."""

    value: ConfigValue
    source: Source


# The built-in sensitive-name set the Redactor extends rather than replaces.
BUILTIN_SENSITIVE_NAMES: Final[tuple[str, ...]] = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
)

# The built-in sensitive-path set the Policy_Watcher extends rather than replaces.
BUILTIN_SENSITIVE_PATHS: Final[tuple[str, ...]] = ("/etc/", "~/.ssh/", ".env")

# Substrings that mark a key as credential-bearing. A key whose environment
# variable or configuration key holds one of these names where a secret lives
# and must therefore carry no default. The import-time check below enforces it.
CREDENTIAL_MARKERS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "credential",
    "password",
    "dsn",
    "session_key",
    "private_key",
)

# The three values accepted from the environment alone, so that a bearer token,
# an ingress signing secret, and a direct connection string cannot be committed.
SECRET_SETTINGS: Final[tuple[SecretSetting, ...]] = (
    SecretSetting(
        env="MOLT_COLLECTOR_TOKEN",
        purpose="the bearer token the capture side presents to the Collector",
    ),
    SecretSetting(
        env="MOLT_INGRESS_SECRET",
        purpose="the shared secret the ingress signature is keyed with",
    ),
    SecretSetting(
        env="MOLT_DSN",
        purpose="a direct connection string, for local development and tests only",
    ),
)

# The configuration surface. Order is the order of the design table, grouped by
# section, and a test compares this table against the example configuration file
# key for key so neither can gain or lose a key alone.
SETTINGS: Final[tuple[Setting, ...]] = (
    # collector
    Setting("MOLT_COLLECTOR_URL", "collector.url", Kind.TEXT, None),
    Setting("MOLT_COLLECTOR_TOKEN_PARAM", "collector.token_param", Kind.TEXT, None),
    Setting("MOLT_COLLECTOR_MAX_BODY_BYTES", "collector.max_body_bytes", Kind.INTEGER, 5242880),
    Setting(
        "MOLT_COLLECTOR_RESERVED_CONCURRENCY", "collector.reserved_concurrency", Kind.INTEGER, 10
    ),
    Setting("MOLT_INGRESS_SECRET_PARAM", "collector.ingress_secret_param", Kind.TEXT, None),
    Setting("MOLT_INGRESS_MAX_AGE_SECONDS", "collector.ingress_max_age_seconds", Kind.INTEGER, 300),
    # store
    Setting("MOLT_DSN_PARAM", "store.dsn_param", Kind.TEXT, None),
    # The read-only role's own connection string, for a component that holds a wider
    # role but must perform one analysis under no write privilege at all. Empty by
    # default: a component whose own role is already read-only needs no second handle,
    # and a deployment that declares none is refused by the analysis rather than run
    # under the privilege it happens to hold.
    Setting("MOLT_READER_DSN_PARAM", "store.reader_dsn_param", Kind.TEXT, None),
    Setting("MOLT_DB_ROLE", "store.role", Kind.TEXT, "writer"),
    Setting("MOLT_DB_STATEMENT_TIMEOUT_MS", "store.statement_timeout_ms", Kind.INTEGER, 10000),
    Setting("MOLT_DB_MAX_RETRIES", "store.max_retries", Kind.INTEGER, 5),
    # capture
    Setting("MOLT_CLIENT_MAP", "capture.client_map", Kind.PATH, None),
    Setting("MOLT_SPOOL_DIR", "capture.spool_dir", Kind.PATH, "~/.molt/spool"),
    Setting("MOLT_SPOOL_MAX_BYTES", "capture.spool_max_bytes", Kind.INTEGER, 67108864),
    Setting("MOLT_HTTP_TIMEOUT_SECONDS", "capture.http_timeout_seconds", Kind.INTEGER, 5),
    Setting("MOLT_HTTP_RETRIES", "capture.http_retries", Kind.INTEGER, 3),
    Setting("MOLT_HOOK_SOFT_DEADLINE_MS", "capture.soft_deadline_ms", Kind.INTEGER, 1200),
    # An empty machine identifier means derive a stable one from the host.
    Setting("MOLT_MACHINE_ID", "capture.machine_id", Kind.TEXT, ""),
    Setting("MOLT_TEAM_ID", "capture.team_id", Kind.TEXT, None),
    # redaction
    Setting("MOLT_REDACTION_DISABLED", "redaction.disabled", Kind.FLAG, False),
    Setting("MOLT_REDACTION_MAX_DEPTH", "redaction.max_depth", Kind.INTEGER, 32),
    Setting(
        "MOLT_REDACTION_SENSITIVE_NAMES",
        "redaction.sensitive_names",
        Kind.TEXT_LIST,
        BUILTIN_SENSITIVE_NAMES,
    ),
    # providers
    Setting("MOLT_EMBEDDING_PROVIDER", "providers.embedding", Kind.TEXT, "bedrock"),
    Setting("MOLT_TEXT_PROVIDER", "providers.text", Kind.TEXT, "bedrock"),
    Setting(
        "MOLT_EMBEDDING_CREDENTIAL_PARAM",
        "providers.embedding_credential_param",
        Kind.TEXT,
        None,
    ),
    Setting("MOLT_TEXT_CREDENTIAL_PARAM", "providers.text_credential_param", Kind.TEXT, None),
    Setting(
        "MOLT_EMBEDDING_CREDENTIAL_FILE",
        "providers.embedding_credential_file",
        Kind.PATH,
        None,
    ),
    Setting("MOLT_TEXT_CREDENTIAL_FILE", "providers.text_credential_file", Kind.PATH, None),
    Setting("MOLT_EMBEDDING_MODEL_ID", "providers.embedding_model_id", Kind.TEXT, None),
    # The vector width the schema fixes. It is a startup gate, not a widenable knob.
    Setting("MOLT_EMBEDDING_DIMENSIONS", "providers.embedding_dimensions", Kind.INTEGER, 1024),
    Setting("MOLT_EMBEDDING_BATCH_SIZE", "providers.embedding_batch_size", Kind.INTEGER, 25),
    Setting("MOLT_ADJUDICATION_MODEL_ID", "providers.adjudication_model_id", Kind.TEXT, None),
    Setting("MOLT_REWRITE_MODEL_ID", "providers.rewrite_model_id", Kind.TEXT, None),
    Setting("MOLT_PROVIDER_MAX_RETRIES", "providers.max_retries", Kind.INTEGER, 3),
    Setting("MOLT_PROVIDER_TIMEOUT_SECONDS", "providers.timeout_seconds", Kind.INTEGER, 30),
    Setting("MOLT_BEDROCK_REGION", "providers.bedrock_region", Kind.TEXT, None),
    Setting("MOLT_PROMPT_CACHE_ENABLED", "providers.prompt_cache_enabled", Kind.TEXT, "auto"),
    # erasure
    Setting(
        "MOLT_ADJUDICATION_PREFIX_BUDGET_BYTES",
        "erasure.prefix_budget_bytes",
        Kind.INTEGER,
        32768,
    ),
    # The least stable-prefix length a cache boundary is marked at. A shorter
    # prefix is left unmarked because a cache write with no subsequent cache
    # read costs more than no caching.
    Setting(
        "MOLT_MINIMUM_CACHEABLE_PREFIX_BYTES",
        "erasure.minimum_cacheable_prefix_bytes",
        Kind.INTEGER,
        16384,
    ),
    Setting("MOLT_AUTO_INCLUDE_THRESHOLD", "erasure.auto_include_threshold", Kind.NUMBER, 0.20),
    Setting("MOLT_REVIEW_THRESHOLD", "erasure.review_threshold", Kind.NUMBER, 0.45),
    Setting("MOLT_RESIDUE_QUERY_LIMIT", "erasure.residue_query_limit", Kind.INTEGER, 50),
    Setting("MOLT_RESIDUE_TOP_K", "erasure.residue_top_k", Kind.INTEGER, 100),
    Setting("MOLT_ERASURE_BATCH_SIZE", "erasure.batch_size", Kind.INTEGER, 100),
    # The two ends of the length band a redaction rewrite is admitted inside. The
    # ceiling is a setting of its own rather than the floor inverted, so a
    # deployment can refuse a padded answer without loosening the floor that
    # refuses a degenerate one.
    Setting("MOLT_REWRITE_LENGTH_RATIO_MIN", "erasure.rewrite_ratio_min", Kind.NUMBER, 0.3),
    Setting(
        "MOLT_REWRITE_LENGTH_RATIO_MAX",
        "erasure.rewrite_length_ratio_max",
        Kind.NUMBER,
        2.0,
    ),
    Setting("MOLT_LEASE_INTERVAL_SECONDS", "erasure.lease_interval_seconds", Kind.INTEGER, 30),
    # An empty owner means derive one from the host and the process.
    Setting("MOLT_LEASE_OWNER", "erasure.lease_owner", Kind.TEXT, ""),
    Setting("MOLT_CONTEND_WORKERS", "erasure.contend_workers", Kind.INTEGER, 10),
    # sensitivity
    Setting(
        "MOLT_SENSITIVITY_AUTO_THRESHOLDS",
        "sensitivity.auto_include_thresholds",
        Kind.NUMBER_LIST,
        (0.10, 0.15, 0.20, 0.25, 0.30),
    ),
    Setting(
        "MOLT_SENSITIVITY_REVIEW_THRESHOLDS",
        "sensitivity.review_thresholds",
        Kind.NUMBER_LIST,
        (0.35, 0.40, 0.45, 0.50, 0.55),
    ),
    Setting("MOLT_SENSITIVITY_GROUND_TRUTH", "sensitivity.ground_truth_path", Kind.PATH, None),
    # procedures
    Setting("MOLT_PROCEDURE_CONFIDENCE_INITIAL", "procedures.confidence_initial", Kind.NUMBER, 0.5),
    Setting(
        "MOLT_PROCEDURE_CONFIDENCE_SUCCESS_DELTA",
        "procedures.success_delta",
        Kind.NUMBER,
        0.05,
    ),
    Setting(
        "MOLT_PROCEDURE_CONFIDENCE_FAILURE_DELTA",
        "procedures.failure_delta",
        Kind.NUMBER,
        0.10,
    ),
    Setting("MOLT_PROCEDURE_RECALL_FLOOR", "procedures.recall_floor", Kind.NUMBER, 0.15),
    # checkpoint
    Setting("MOLT_CHECKPOINT_INTERVAL_SECONDS", "checkpoint.interval_seconds", Kind.INTEGER, 3600),
    # retention
    Setting("MOLT_WORKING_TTL_SECONDS", "retention.working_ttl_seconds", Kind.INTEGER, 3600),
    Setting("MOLT_RETENTION_DEFAULT_INTERVAL", "retention.default_interval", Kind.TEXT, "90 days"),
    # console
    Setting(
        "MOLT_INTERFACE_SPEC_PATH",
        "console.interface_spec_path",
        Kind.PATH,
        "docs/interface.json",
    ),
    Setting("MOLT_CONSOLE_BIND", "console.bind", Kind.TEXT, "127.0.0.1:8080"),
    Setting("MOLT_DEMO_MODE", "console.demo_mode", Kind.FLAG, False),
    Setting("MOLT_CONSOLE_CREDENTIAL_PARAM", "console.credential_param", Kind.TEXT, None),
    Setting("MOLT_CONSOLE_SESSION_KEY_PARAM", "console.session_key_param", Kind.TEXT, None),
    # certificate
    Setting("MOLT_KMS_KEY_ID", "certificate.kms_key_id", Kind.TEXT, None),
    # Where a saved public half is read from, for a verification that calls no key
    # service. Absent by default, because a deployment verifying inside itself asks
    # the service; an auditor holding the file configures this and needs no account.
    Setting(
        "MOLT_CERTIFICATE_PUBLIC_KEY_PATH",
        "certificate.public_key_path",
        Kind.TEXT,
        "",
    ),
    Setting(
        "MOLT_KMS_SIGNING_ALGORITHM",
        "certificate.signing_algorithm",
        Kind.TEXT,
        "ECDSA_SHA_256",
    ),
    Setting("MOLT_CERT_BUCKET", "certificate.bucket", Kind.TEXT, None),
    Setting("MOLT_CERT_PREFIX", "certificate.prefix", Kind.TEXT, "certificates/"),
    Setting("MOLT_CERT_OBJECT_LOCK_DAYS", "certificate.object_lock_days", Kind.INTEGER, 1),
    # ccloud
    Setting("MOLT_CCLOUD_BIN", "ccloud.binary", Kind.TEXT, "ccloud"),
    Setting("MOLT_CCLOUD_CLUSTER_ID", "ccloud.cluster_id", Kind.TEXT, None),
    # backup
    # The operator-owned target a pre-erasure backup is written into. It has no
    # default on purpose: a default would write a cluster backup somewhere no
    # operator chose.
    Setting("MOLT_BACKUP_TARGET", "backup.target", Kind.TEXT, None),
    Setting("MOLT_BACKUP_TIMEOUT_SECONDS", "backup.timeout_seconds", Kind.INTEGER, 600),
    # watcher
    Setting("MOLT_WATCHER_MODE", "watcher.mode", Kind.TEXT, "auto"),
    Setting(
        "MOLT_WATCHER_POLL_INTERVAL_SECONDS",
        "watcher.poll_interval_seconds",
        Kind.INTEGER,
        2,
    ),
    Setting("MOLT_WATCHER_RESOLVED_INTERVAL", "watcher.resolved_interval", Kind.TEXT, "2s"),
    # An empty rules path means use the built-in rule set.
    Setting("MOLT_POLICY_RULES_PATH", "watcher.rules_path", Kind.PATH, ""),
    Setting(
        "MOLT_SENSITIVE_PATHS",
        "watcher.sensitive_paths",
        Kind.TEXT_LIST,
        BUILTIN_SENSITIVE_PATHS,
    ),
    # mcp
    Setting("MOLT_MCP_TRANSPORT", "mcp.transport", Kind.TEXT, "stdio"),
    Setting("MOLT_MCP_BIND", "mcp.bind", Kind.TEXT, "0.0.0.0:8090"),
    Setting("MOLT_MCP_PERMITTED_CLIENTS", "mcp.permitted_clients", Kind.TEXT_LIST, None),
    Setting("MOLT_MCP_MAX_RESULTS", "mcp.max_results", Kind.INTEGER, 50),
    # telemetry
    Setting("MOLT_METRIC_NAMESPACE", "telemetry.namespace", Kind.TEXT, "Molt"),
    Setting(
        "MOLT_METRIC_CARDINALITY_MAX",
        "telemetry.metric_cardinality_max",
        Kind.INTEGER,
        10,
    ),
    Setting("MOLT_METRIC_BATCH_SIZE", "telemetry.metric_batch_size", Kind.INTEGER, 20),
    Setting(
        "MOLT_METRIC_DELIVERY_INTERVAL_SECONDS",
        "telemetry.metric_delivery_interval_seconds",
        Kind.INTEGER,
        60,
    ),
    Setting("MOLT_LOG_LEVEL", "telemetry.log_level", Kind.TEXT, "info"),
    Setting("MOLT_TELEMETRY_DISABLED", "telemetry.disabled", Kind.FLAG, False),
)


def _is_credential_bearing(setting: Setting) -> bool:
    """Whether a key names where a credential lives and so may carry no default."""
    subject = f"{setting.env} {setting.key}".lower()
    return any(marker in subject for marker in CREDENTIAL_MARKERS)


def _validate_surface() -> None:
    """Refuse a surface in which a credential-bearing key carries a default.

    This runs at import so the refusal happens before any process reads a value.
    A default on a key that names where a credential lives would ship a
    credential reference no operator chose, which is the failure the whole
    secret-handling design exists to prevent.
    """
    seen_env: set[str] = set()
    seen_key: set[str] = set()
    for setting in SETTINGS:
        if setting.env in seen_env:
            raise ConfigError(f"the configuration surface declares {setting.env} twice")
        if setting.key in seen_key:
            raise ConfigError(f"the configuration surface declares {setting.key} twice")
        seen_env.add(setting.env)
        seen_key.add(setting.key)
        if _is_credential_bearing(setting) and setting.default is not None:
            raise ConfigError(
                f"the credential-bearing key {setting.key} must carry no default value"
            )
    for secret in SECRET_SETTINGS:
        if secret.env in seen_env:
            raise ConfigError(
                f"{secret.env} is accepted from the environment alone and may have no "
                "configuration file key"
            )


_validate_surface()

_BY_ENV: Final[Mapping[str, Setting]] = MappingProxyType(
    {setting.env: setting for setting in SETTINGS}
)
_BY_KEY: Final[Mapping[str, Setting]] = MappingProxyType(
    {setting.key: setting for setting in SETTINGS}
)
_SECRET_BY_ENV: Final[Mapping[str, SecretSetting]] = MappingProxyType(
    {secret.env: secret for secret in SECRET_SETTINGS}
)

# The configuration file sections, in declared order.
SECTION_NAMES: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(setting.key.split(".", 1)[0] for setting in SETTINGS)
)

_TRUE_TEXT: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on"})
_FALSE_TEXT: Final[frozenset[str]] = frozenset({"false", "0", "no", "off"})


def _parse_text(env: str, kind: Kind, raw: str) -> ConfigValue:
    """Parse an environment value into the kind its key declares."""
    text = raw.strip()
    if kind in (Kind.TEXT, Kind.PATH):
        return text
    if kind is Kind.INTEGER:
        try:
            return int(text, 10)
        except ValueError as exc:
            raise InvalidConfigValueError(env, kind, "the value is not a whole number") from exc
    if kind is Kind.NUMBER:
        try:
            return float(text)
        except ValueError as exc:
            raise InvalidConfigValueError(env, kind, "the value is not a number") from exc
    if kind is Kind.FLAG:
        lowered = text.lower()
        if lowered in _TRUE_TEXT:
            return True
        if lowered in _FALSE_TEXT:
            return False
        raise InvalidConfigValueError(env, kind, "the value reads neither true nor false")
    entries = tuple(item.strip() for item in text.split(",") if item.strip())
    if kind is Kind.TEXT_LIST:
        return entries
    numbers: list[float] = []
    for entry in entries:
        try:
            numbers.append(float(entry))
        except ValueError as exc:
            raise InvalidConfigValueError(env, kind, "an entry is not a number") from exc
    return tuple(numbers)


def _coerce_file_value(env: str, kind: Kind, raw: JsonValue) -> ConfigValue:
    """Convert a configuration file value into the kind its key declares.

    A file value arrives already typed, so this validates rather than parses. A
    boolean is refused where a number is expected even though a boolean is an
    integer in Python, because a flag written into a numeric key is a mistake
    worth reporting rather than silently reading as one or zero.
    """
    if kind in (Kind.TEXT, Kind.PATH):
        if not isinstance(raw, str):
            raise InvalidConfigValueError(env, kind, "the configuration file value is not text")
        return raw
    if kind is Kind.FLAG:
        if not isinstance(raw, bool):
            raise InvalidConfigValueError(
                env, kind, "the configuration file value is not a boolean"
            )
        return raw
    if kind is Kind.INTEGER:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InvalidConfigValueError(
                env, kind, "the configuration file value is not an integer"
            )
        return raw
    if kind is Kind.NUMBER:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidConfigValueError(env, kind, "the configuration file value is not a number")
        return float(raw)
    if not isinstance(raw, list):
        raise InvalidConfigValueError(env, kind, "the configuration file value is not a list")
    if kind is Kind.TEXT_LIST:
        for item in raw:
            if not isinstance(item, str):
                raise InvalidConfigValueError(env, kind, "a list entry is not text")
        return tuple(item for item in raw if isinstance(item, str))
    numbers: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise InvalidConfigValueError(env, kind, "a list entry is not a number")
        numbers.append(float(item))
    return tuple(numbers)


def load_config_file(path: Path) -> dict[str, JsonValue]:
    """Read a configuration file into dotted keys, refusing a key the surface omits.

    Refusing an unknown key is deliberate: a mistyped key that resolved silently
    to its default would present as a value that will not take effect, which is
    the hardest class of configuration fault to see.
    """
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"the configuration file {path} could not be read") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the configuration file {path} is not valid") from exc

    flattened: dict[str, JsonValue] = {}
    for section, body in document.items():
        if not isinstance(body, dict):
            raise ConfigError(f"the configuration file {path} holds a value outside any section")
        for name, value in body.items():
            key = f"{section}.{name}"
            if key not in _BY_KEY:
                raise UnknownSettingError(key)
            flattened[key] = value
    return flattened


def default_config_path() -> Path | None:
    """The configuration file in the working directory, when one is present."""
    candidate = Path("config.toml")
    return candidate if candidate.is_file() else None


class Configuration:
    """Resolved configuration over one environment and one configuration file.

    Both sources are held rather than read at call time, so a process resolves
    against one consistent view and a test can supply either source explicitly
    without touching the real environment.
    """

    __slots__ = ("_environ", "_file_values", "_path")

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        file_values: Mapping[str, JsonValue] | None = None,
        path: Path | None = None,
    ) -> None:
        self._environ: Mapping[str, str] = dict(os.environ if environ is None else environ)
        self._file_values: Mapping[str, JsonValue] = dict(file_values or {})
        self._path = path

    @property
    def config_path(self) -> Path | None:
        """The configuration file this view was read from, when there was one."""
        return self._path

    def replacing(self, overrides: Mapping[str, str]) -> Configuration:
        """This same surface with a few environment values replaced.

        The file half and the recorded path are carried over unchanged, so what changes
        is only the named environment values and the precedence between the two halves
        stays the surface's own. A caller with nothing to override gets this view back
        rather than a copy, because re-resolving an unchanged surface would invite the
        two to drift.

        This exists so that a component needing one value different — a second
        connection under another role, a threshold an operator submitted — asks the
        surface for a narrowed view rather than reaching past it into the environment.
        """
        if not overrides:
            return self
        return Configuration(
            environ={**self._environ, **overrides},
            file_values=self._file_values,
            path=self._path,
        )

    def settings(self) -> Iterator[Setting]:
        """Every key of the surface, in declared order."""
        yield from SETTINGS

    def environment_value(self, env: str) -> str | None:
        """The raw environment text for a name, or None when it is unset or empty.

        Secret-bearing names resolve through this as well, which is why it takes a
        name rather than a `Setting`: the three environment-only secrets have no
        configuration key and no default, and the secret accessors read them here.
        """
        if env not in _BY_ENV and env not in _SECRET_BY_ENV:
            raise UnknownSettingError(env)
        raw = self._environ.get(env, "")
        return raw if raw.strip() else None

    def deployment_flag(self, name: str) -> str | None:
        """The raw text of an environment-only deployment flag, or None when unset.

        A deployment flag is not part of the configuration surface: it selects how
        the surface is treated rather than holding a value the surface names. The
        production marker that refuses the local connection-string bypass is the
        one flag this exists for, and reading it through the same view as every
        other value is what keeps a caller from consulting two environments.
        """
        raw = self._environ.get(name, "")
        return raw if raw.strip() else None

    def resolve(self, env: str) -> Resolved:
        """Resolve one key: environment, then configuration file, then default."""
        setting = _BY_ENV.get(env)
        if setting is None:
            if env in _SECRET_BY_ENV:
                raise UnknownSettingError(
                    f"{env} is a secret read through the secret accessors rather than here"
                )
            raise UnknownSettingError(env)

        raw = self._environ.get(setting.env, "")
        if raw.strip():
            return Resolved(_parse_text(setting.env, setting.kind, raw), Source.ENVIRONMENT)

        if setting.key in self._file_values:
            from_file = self._file_values[setting.key]
            return Resolved(_coerce_file_value(setting.env, setting.kind, from_file), Source.FILE)

        if setting.default is None:
            raise MissingConfigError(setting.env, setting.key)
        return Resolved(setting.default, Source.DEFAULT)

    def source(self, env: str) -> Source:
        """Which of the three sources supplied a key's value."""
        return self.resolve(env).source

    def value(self, env: str) -> ConfigValue:
        """A key's resolved value, raising when a required key is absent."""
        return self.resolve(env).value

    def optional(self, env: str) -> ConfigValue | None:
        """A key's resolved value, or None when a required key is absent.

        This is what a caller uses for the pairs of keys that are alternatives to
        each other, such as a credential parameter name and a credential file
        path, where absence is a choice rather than a fault.
        """
        try:
            return self.resolve(env).value
        except MissingConfigError:
            return None

    def text(self, env: str) -> str:
        """A key's value as text."""
        value = self.value(env)
        if not isinstance(value, str):
            raise InvalidConfigValueError(env, Kind.TEXT, "the resolved value is not text")
        return value

    def optional_text(self, env: str) -> str | None:
        """A key's value as text, or None when absent or empty."""
        value = self.optional(env)
        if value is None:
            return None
        if not isinstance(value, str):
            raise InvalidConfigValueError(env, Kind.TEXT, "the resolved value is not text")
        return value or None

    def integer(self, env: str) -> int:
        """A key's value as a whole number."""
        value = self.value(env)
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidConfigValueError(
                env, Kind.INTEGER, "the resolved value is not a whole number"
            )
        return value

    def number(self, env: str) -> float:
        """A key's value as a number."""
        value = self.value(env)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidConfigValueError(env, Kind.NUMBER, "the resolved value is not a number")
        return float(value)

    def flag(self, env: str) -> bool:
        """A key's value as a flag."""
        value = self.value(env)
        if not isinstance(value, bool):
            raise InvalidConfigValueError(env, Kind.FLAG, "the resolved value is not a flag")
        return value

    def text_list(self, env: str) -> tuple[str, ...]:
        """A key's value as a list of text values."""
        value = self.value(env)
        if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
            raise InvalidConfigValueError(
                env, Kind.TEXT_LIST, "the resolved value is not a text list"
            )
        return tuple(item for item in value if isinstance(item, str))

    def number_list(self, env: str) -> tuple[float, ...]:
        """A key's value as a list of numbers."""
        value = self.value(env)
        if not isinstance(value, tuple) or any(not isinstance(item, float) for item in value):
            raise InvalidConfigValueError(
                env, Kind.NUMBER_LIST, "the resolved value is not a number list"
            )
        return tuple(item for item in value if isinstance(item, float))

    def path(self, env: str) -> Path:
        """A key's value as a filesystem path, with a leading home reference expanded.

        Paths are returned as path objects rather than text so that no caller
        joins path segments by concatenating strings.
        """
        text = self.text(env)
        if not text:
            raise MissingConfigError(env, _BY_ENV[env].key)
        return Path(text).expanduser()

    def optional_path(self, env: str) -> Path | None:
        """A key's value as a path, or None when absent or empty."""
        text = self.optional_text(env)
        return None if text is None else Path(text).expanduser()


def load_configuration(
    *,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Configuration:
    """Build a resolved view over the environment and an optional configuration file."""
    resolved_path = config_path if config_path is not None else default_config_path()
    file_values: Mapping[str, JsonValue] = {}
    if resolved_path is not None:
        file_values = load_config_file(resolved_path)
    return Configuration(environ=environ, file_values=file_values, path=resolved_path)


def secret_settings() -> Sequence[SecretSetting]:
    """The environment-only secrets, which carry no default and no file key."""
    return SECRET_SETTINGS
