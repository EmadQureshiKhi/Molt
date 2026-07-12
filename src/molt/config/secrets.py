"""Secret accessors: a parameter store reader and an operator credential file reader.

Four rules shape this module, and each is enforced structurally rather than by
asking callers to be careful.

**A loaded credential never renders.** Every value returned here is wrapped in
`Credential`, whose text, representation, and format all yield one fixed
placeholder. An accidental interpolation into a log record, an exception message,
an error-detail column, or an output stream therefore yields the placeholder
rather than the value, and reading the value takes an explicit call that is easy
to find and easy to review.

**Every error names the source and never the value.** A failure message carries
the parameter name or the file path, because that is what an operator needs, and
carries nothing read from either.

**One client and one cache per process.** The parameter reader builds its client
once and caches by parameter name for the lifetime of the process, because a cold
start reads the same handful of names and a repeated read is a repeated charge. A
transient failure is retried exactly once; there is no loop, because an
unreachable parameter store is a startup failure rather than something to wait
out.

**Parameters live in the tier carrying no per-parameter monthly charge.** The
reader asks for a plain name with decryption and nothing else, so no request
takes the advanced-tier path. The cost ceiling of the whole deployment depends on
that, which is why the reader offers no tier argument to get wrong.

**The local connection-string bypass is refused in production.** Reading a
connection string from the environment is a development convenience. When the
deployment marks itself as production the bypass raises rather than resolving, so
the convenience is unreachable exactly where it would be dangerous.

The cloud client is imported lazily through the import machinery rather than by a
module-level import, so this module imports and every credential-free suite
collects with no cloud package installed. The client is used through a narrow
structural protocol declared here, so the type check has a real shape to check
against without depending on the package shipping one.
"""

from __future__ import annotations

import importlib
import stat
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, cast

from molt.config.resolve import ConfigError, Configuration, MissingConfigError

__all__ = [
    "CREDENTIAL_PLACEHOLDER",
    "DEFAULT_CREDENTIAL_DIRECTORY",
    "PARAMETER_TIER",
    "PRODUCTION_FLAG_ENV",
    "PRODUCTION_FLAG_VALUE",
    "Credential",
    "CredentialFileError",
    "CredentialSource",
    "LocalBypassRefusedError",
    "ParameterMissingError",
    "ParameterReader",
    "ParameterUnavailableError",
    "clear_parameter_cache",
    "get_parameter",
    "is_production",
    "load_credential",
    "read_credential_file",
    "resolve_collector_bearer",
    "resolve_dsn",
    "resolve_ingress_signing_key",
]

# What a loaded credential renders as, everywhere, always. One fixed token rather
# than an elided prefix, because a prefix is still disclosure.
CREDENTIAL_PLACEHOLDER: Final[str] = "[MOLT_CREDENTIAL]"

# The parameter tier every Molt parameter is written in. It carries no
# per-parameter monthly charge, which the cost ceiling depends on. The reader
# below takes no tier argument at all; this constant states the contract the
# write side honours and the provisioning scripts assert.
PARAMETER_TIER: Final[str] = "Standard"

# Where operator-provided credential files live unless a caller names another
# directory. Files here are expected to be readable by their owner alone.
DEFAULT_CREDENTIAL_DIRECTORY: Final[Path] = Path(".secrets")

# The permission bits that must be clear on a credential file: any group or other
# access at all. A world-readable credential file is a finding, not a warning.
FORBIDDEN_FILE_BITS: Final[int] = stat.S_IRWXG | stat.S_IRWXO

# The environment-only deployment marker, and the value that means production.
PRODUCTION_FLAG_ENV: Final[str] = "MOLT_ENV"
PRODUCTION_FLAG_VALUE: Final[str] = "production"

# Failure kinds worth one retry: a throttle, a timeout, or a transport fault.
# Classification is by exception type name and by the error code the cloud
# client attaches, because the cloud package is imported lazily and its
# exception classes are therefore not available to name directly.
TRANSIENT_FAILURE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "connectionerror",
        "connecttimeouterror",
        "endpointconnectionerror",
        "internalfailure",
        "internalservererror",
        "readtimeouterror",
        "requesttimeout",
        "serviceunavailable",
        "throttlingexception",
        "toomanyupdates",
    }
)

# The cloud package and the parameter service the reader speaks to.
_CLOUD_PACKAGE: Final[str] = "boto3"
_PARAMETER_SERVICE: Final[str] = "ssm"

# One attempt plus one retry. Not a loop: an unreachable parameter store is a
# startup failure to report rather than a condition to wait out.
_PARAMETER_ATTEMPTS: Final[int] = 2


class CredentialSource(StrEnum):
    """Where a credential was read from."""

    PARAMETER = "parameter"
    FILE = "file"
    ENVIRONMENT = "environment"


class Credential:
    """A loaded secret value that renders as a fixed placeholder.

    The name of the source is public because an operator needs it and it holds no
    secret. The value is reachable only through `reveal`, and every rendering
    path yields the placeholder: text conversion, representation, and format
    specification alike. Equality and hashing are not implemented, so a value
    cannot leak through a comparison message or a set membership report either.
    """

    __slots__ = ("_source", "_source_name", "_value")

    def __init__(self, value: str, *, source_name: str, source: CredentialSource) -> None:
        self._value = value
        self._source_name = source_name
        self._source = source

    @property
    def source_name(self) -> str:
        """The parameter name, file path, or environment variable it was read from."""
        return self._source_name

    @property
    def source(self) -> CredentialSource:
        """Which accessor produced the value."""
        return self._source

    def reveal(self) -> str:
        """Return the value itself. Every call site is a place to review."""
        return self._value

    def __str__(self) -> str:
        """Render the placeholder, so an interpolation discloses nothing."""
        return CREDENTIAL_PLACEHOLDER

    def __repr__(self) -> str:
        """Render the placeholder, so a debugger or a traceback discloses nothing."""
        return CREDENTIAL_PLACEHOLDER

    def __format__(self, format_spec: str) -> str:
        """Render the placeholder, ignoring any format specification."""
        return CREDENTIAL_PLACEHOLDER


class ParameterMissingError(ConfigError):
    """The named parameter does not exist in the parameter store."""

    def __init__(self, name: str) -> None:
        self.parameter_name = name
        super().__init__(f"the parameter {name} is absent from the parameter store")


class ParameterUnavailableError(ConfigError):
    """The parameter store could not be read, after one retry."""

    def __init__(self, name: str, reason: str) -> None:
        self.parameter_name = name
        super().__init__(f"the parameter {name} could not be read: {reason}")


class CredentialFileError(ConfigError):
    """An operator-provided credential file was refused rather than read."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        super().__init__(f"the credential file {path} was refused: {reason}")


class LocalBypassRefusedError(ConfigError):
    """The local connection-string bypass was reached with production marked."""

    def __init__(self, env: str) -> None:
        self.env = env
        super().__init__(
            f"{env} is accepted for local development only and is refused while "
            f"{PRODUCTION_FLAG_ENV} reads {PRODUCTION_FLAG_VALUE}; configure the "
            "parameter name instead"
        )


class ParameterReader(Protocol):
    """The one call the parameter reader makes on the cloud client.

    Declared structurally rather than imported, because the cloud package is
    imported lazily and ships no shape the type check can follow. Keyword
    arguments are accepted as a mapping so that the cloud client's own argument
    spelling stays at the call site rather than being restated here.
    """

    def get_parameter(self, **kwargs: str | bool) -> Mapping[str, object]:
        """Read one parameter by name, decrypting a secure value."""


_reader: ParameterReader | None = None
_cache: dict[str, Credential] = {}


def _build_reader() -> ParameterReader:
    """Construct the per-process cloud client, importing the package on first use."""
    try:
        package = importlib.import_module(_CLOUD_PACKAGE)
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "the cloud client package is not installed, so no parameter can be read; "
            "install the project dependencies or configure a local credential file"
        ) from exc
    created: object = package.client(_PARAMETER_SERVICE)
    return cast(ParameterReader, created)


def _reader_for(reader: ParameterReader | None) -> ParameterReader:
    """Return the caller's reader, or the cached per-process one."""
    global _reader
    if reader is not None:
        return reader
    if _reader is None:
        _reader = _build_reader()
    return _reader


def clear_parameter_cache() -> None:
    """Forget the cached parameters and the cached client.

    The cache is a process-lifetime cache by design, so this exists for a test
    that needs a clean process rather than for any runtime path.
    """
    global _reader
    _cache.clear()
    _reader = None


def _failure_names(error: BaseException) -> frozenset[str]:
    """The lowercased names by which a cloud failure might be recognised."""
    names = {type(error).__name__.lower()}
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        detail = response.get("Error")
        if isinstance(detail, Mapping):
            code = detail.get("Code")
            if isinstance(code, str):
                names.add(code.lower())
    return frozenset(names)


def _is_missing(error: BaseException) -> bool:
    """Whether the failure says the parameter does not exist."""
    return "parameternotfound" in _failure_names(error)


def _is_transient(error: BaseException) -> bool:
    """Whether the failure is worth exactly one more attempt."""
    return bool(_failure_names(error) & TRANSIENT_FAILURE_NAMES)


def _extract_value(name: str, response: Mapping[str, object]) -> str:
    """Pull the parameter value out of a response, reporting a shape fault by name."""
    parameter = response.get("Parameter")
    if not isinstance(parameter, Mapping):
        raise ParameterUnavailableError(name, "the response carried no parameter")
    value = parameter.get("Value")
    if not isinstance(value, str) or not value:
        raise ParameterUnavailableError(name, "the response carried no value")
    return value


def get_parameter(name: str, *, reader: ParameterReader | None = None) -> Credential:
    """Read one parameter, caching for the process lifetime and retrying once.

    The value is returned wrapped, so nothing downstream can render it by
    accident. A failure raises an error naming the parameter and nothing else.
    """
    if not name:
        raise ConfigError("a parameter name is required to read a parameter")
    cached = _cache.get(name)
    if cached is not None:
        return cached

    client = _reader_for(reader)
    for attempt in range(_PARAMETER_ATTEMPTS):
        final_attempt = attempt + 1 >= _PARAMETER_ATTEMPTS
        try:
            response = client.get_parameter(Name=name, WithDecryption=True)
        except Exception as error:
            # The cloud package is imported lazily, so its exception classes cannot
            # be named here; the failure is classified by name and code instead.
            if _is_missing(error):
                raise ParameterMissingError(name) from error
            if final_attempt or not _is_transient(error):
                raise ParameterUnavailableError(name, type(error).__name__) from error
            continue
        credential = Credential(
            _extract_value(name, response),
            source_name=name,
            source=CredentialSource.PARAMETER,
        )
        _cache[name] = credential
        return credential
    raise ParameterUnavailableError(name, "the read did not complete")


def read_credential_file(path: Path, *, directory: Path | None = None) -> Credential:
    """Read an operator-provided credential file, checking its permissions first.

    The file must sit inside the configured credential directory and must grant no
    group or other access. Both checks refuse rather than warn: a credential file
    outside the directory an operator configured is not the file that was meant,
    and a credential readable by anyone on the machine has already leaked.
    """
    base = (DEFAULT_CREDENTIAL_DIRECTORY if directory is None else directory).expanduser()
    candidate = path.expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CredentialFileError(candidate, "the file does not exist") from exc

    base_resolved = base.resolve() if base.exists() else base.absolute()
    if not resolved.is_relative_to(base_resolved):
        raise CredentialFileError(
            candidate, f"the file lies outside the credential directory {base}"
        )

    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise CredentialFileError(candidate, "the path is not a regular file")
    offending = stat.S_IMODE(info.st_mode) & FORBIDDEN_FILE_BITS
    if offending:
        raise CredentialFileError(
            candidate,
            f"the mode {stat.S_IMODE(info.st_mode):04o} grants group or other access; "
            "restrict it to the owner",
        )

    try:
        text = resolved.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CredentialFileError(candidate, "the file could not be read") from exc
    except UnicodeDecodeError as exc:
        raise CredentialFileError(candidate, "the file is not valid text") from exc
    if not text:
        raise CredentialFileError(candidate, "the file is empty")
    return Credential(text, source_name=str(candidate), source=CredentialSource.FILE)


def is_production(configuration: Configuration) -> bool:
    """Whether the deployment marks itself as production."""
    marker = configuration.deployment_flag(PRODUCTION_FLAG_ENV)
    return marker is not None and marker.strip().lower() == PRODUCTION_FLAG_VALUE


def load_credential(
    configuration: Configuration,
    *,
    parameter_env: str,
    file_env: str,
    directory: Path | None = None,
    reader: ParameterReader | None = None,
) -> Credential:
    """Load a credential from a configured parameter name or a configured file path.

    Nothing else is accepted: no source constant, no configuration file value, and
    no default. When both a parameter name and a file path are configured the
    parameter wins, because the deployed path is the authoritative one and a stale
    local file should not silently take precedence over it.
    """
    parameter_name = configuration.optional_text(parameter_env)
    if parameter_name is not None:
        return get_parameter(parameter_name, reader=reader)
    file_path = configuration.optional_path(file_env)
    if file_path is not None:
        return read_credential_file(file_path, directory=directory)
    raise MissingConfigError(f"{parameter_env} or {file_env}", None)


def resolve_dsn(
    configuration: Configuration,
    *,
    reader: ParameterReader | None = None,
) -> Credential:
    """Resolve the cluster connection string, refusing the local bypass in production."""
    direct = configuration.environment_value("MOLT_DSN")
    if direct is not None:
        if is_production(configuration):
            raise LocalBypassRefusedError("MOLT_DSN")
        return Credential(direct, source_name="MOLT_DSN", source=CredentialSource.ENVIRONMENT)
    return get_parameter(configuration.text("MOLT_DSN_PARAM"), reader=reader)


def resolve_collector_bearer(
    configuration: Configuration,
    *,
    reader: ParameterReader | None = None,
) -> Credential:
    """Resolve the Collector bearer value from the environment or the parameter store.

    The capture side is given the value by the operator's shell profile and has no
    parameter access; the Collector reads the parameter at cold start.
    """
    injected = configuration.environment_value("MOLT_COLLECTOR_TOKEN")
    if injected is not None:
        return Credential(
            injected,
            source_name="MOLT_COLLECTOR_TOKEN",
            source=CredentialSource.ENVIRONMENT,
        )
    return get_parameter(configuration.text("MOLT_COLLECTOR_TOKEN_PARAM"), reader=reader)


def resolve_ingress_signing_key(
    configuration: Configuration,
    *,
    reader: ParameterReader | None = None,
) -> Credential:
    """Resolve the shared value the ingress signature is keyed with."""
    injected = configuration.environment_value("MOLT_INGRESS_SECRET")
    if injected is not None:
        return Credential(
            injected,
            source_name="MOLT_INGRESS_SECRET",
            source=CredentialSource.ENVIRONMENT,
        )
    return get_parameter(configuration.text("MOLT_INGRESS_SECRET_PARAM"), reader=reader)
