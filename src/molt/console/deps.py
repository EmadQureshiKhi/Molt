"""What the console resolves once, and the seams the views reach it through.

One object holds everything a request handler may need: the configured surface,
the store the views read with, the two credentials authentication needs, the
template environment, and the clock. A handler reaches it through
`console_of(request)` rather than through a module-level global, so a test drives
the same application object against its own store and its own credentials with no
cloud call and no cluster.

**Credentials resolve at cold start and are never held revealed.** The credential
record and the session key come from Parameter_Store through the accessor whose
cache is per process, wrapped in `Credential`, so an accidental interpolation into
a log record or a template renders the placeholder. Each is revealed inside the one
call that needs it.

**The role is reported rather than enforced.** The views read, and the deployed
function is given the eraser role because the erasure console runs erasures from
the same function; refusing anything but the reader role here would refuse the
deployment. The resolved role is logged at startup and reported by the health
route, so the privilege the console actually holds is observable rather than
assumed.

**The web assets live outside the package.** Templates and the stylesheet live
under `web/` while the code lives under `src/molt/console/`, so the package stays
importable and the assets stay where the requirement puts them. The directories
are resolved from the repository root at startup and reported as absent rather
than guessed at request time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from molt.config.resolve import ConfigError, Configuration, load_configuration
from molt.config.secrets import Credential, ParameterReader, get_parameter
from molt.console.auth import DEFAULT_SESSION_LIFETIME
from molt.errors import MoltError
from molt.store import (
    READER_DSN_PARAM_KEY,
    READER_ROLE_NAMES,
    ROLE_KEY,
    MemoryStore,
)
from molt.telemetry import Severity, log

# The primary connection's own parameter key, and the role name a reader handle is
# opened under. The role is fixed here rather than read from the surface: a handle
# opened for an analysis that insists on the read-only role must not be able to claim
# another because a key was set wrongly.
DSN_PARAM_KEY: Final[str] = "MOLT_DSN_PARAM"
READ_ONLY_ROLE_NAME: Final[str] = "reader"

__all__ = [
    "COMPONENT",
    "CREDENTIAL_PARAM_KEY",
    "DEFAULT_STATIC_PATH",
    "DEFAULT_TEMPLATE_PATH",
    "SESSION_KEY_PARAM_KEY",
    "STATIC_MOUNT",
    "Console",
    "ConsoleSettings",
    "ReaderRoleUnavailableError",
    "reader_store_factory",
    "resolve_console_credential",
    "resolve_console_session_key",
    "web_root",
]


class ReaderRoleUnavailableError(MoltError):
    """No read-only connection is configured for an analysis that insists on one.

    Its own condition rather than a store error, because the answer a view gives for it
    is *this analysis is unavailable in this deployment* and not *the database refused*.
    Collapsing the two would let a provisioning gap read as a cluster fault.
    """


# The component name every record from the console carries.
COMPONENT: Final[str] = "console"

# The configuration keys naming the two parameters. Both are read from the parameter
# store and neither is accepted from the environment: the configuration surface
# declares no environment-only console secret, and inventing one here would put a
# console credential one shell profile away from a deployment. A credential-free test
# builds a `Console` directly with credentials of its own instead.
CREDENTIAL_PARAM_KEY: Final[str] = "MOLT_CONSOLE_CREDENTIAL_PARAM"
SESSION_KEY_PARAM_KEY: Final[str] = "MOLT_CONSOLE_SESSION_KEY_PARAM"

# Where the web assets live, relative to the repository root, and the path the
# stylesheet is served under.
DEFAULT_TEMPLATE_PATH: Final[Path] = Path("web/templates")
DEFAULT_STATIC_PATH: Final[Path] = Path("web/static")
STATIC_MOUNT: Final[str] = "/static"

# The configuration keys the surface below is read from.
_BIND_KEY: Final[str] = "MOLT_CONSOLE_BIND"
_DEMO_KEY: Final[str] = "MOLT_DEMO_MODE"
_SPEC_KEY: Final[str] = "MOLT_INTERFACE_SPEC_PATH"

_DEFAULT_HOST: Final[str] = "127.0.0.1"
_DEFAULT_PORT: Final[int] = 8080


# How far above this module the asset root may sit, in the layouts this package is run
# from. A checkout keeps the package under a source directory, so the root is three
# levels up; a deployment archive holds the package at its own root, so it is two. The
# candidates are ordered outermost first, and each is tested by whether it actually holds
# the templates rather than assumed from the count.
_ROOT_DEPTHS: Final[tuple[int, ...]] = (3, 2)


def web_root() -> Path:
    """The root the web assets are resolved against, in whichever layout is running.

    Derived from this module's own location rather than from the working directory,
    because a function invocation and a local run have different working directories.
    What cannot be derived from the location alone is *how far up* the root sits: a
    checkout has the package under a source directory and a deployment archive has it at
    the archive's own root, one level shallower, so a single fixed count is right in one
    layout and points above the root in the other.

    It pointed above the root in the deployed one. Every page of the console answered
    that its templates were unavailable, because the count that is correct for a checkout
    resolved to the filesystem root inside a function. So each candidate is tested by
    whether the templates are actually there, and the first that holds them wins. The
    outermost is tried first so a checkout keeps resolving to the repository root exactly
    as before.

    Falling back to the outermost candidate when none holds the templates keeps the
    failure the one it was: a console reporting that its assets are missing, from the
    place they were expected, rather than a resolver raising somewhere further from the
    cause.
    """
    here = Path(__file__).resolve()
    for depth in _ROOT_DEPTHS:
        candidate = here.parents[depth]
        if (candidate / DEFAULT_TEMPLATE_PATH).is_dir():
            return candidate
    return here.parents[_ROOT_DEPTHS[0]]


def _split_bind(text: str) -> tuple[str, int]:
    """Read a `host:port` bind, falling back to the documented default on either half."""
    host, separator, port_text = text.strip().rpartition(":")
    if not separator:
        return (text.strip() or _DEFAULT_HOST, _DEFAULT_PORT)
    try:
        port = int(port_text, 10)
    except ValueError:
        port = _DEFAULT_PORT
    return (host or _DEFAULT_HOST, port)


@dataclass(frozen=True, slots=True)
class ConsoleSettings:
    """The console's configured surface, resolved once at startup."""

    host: str
    port: int
    demo_mode: bool
    interface_spec_path: Path
    template_directory: Path
    static_directory: Path
    session_lifetime: timedelta = DEFAULT_SESSION_LIFETIME

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> ConsoleSettings:
        """Read the surface from the resolved configuration and nothing else."""
        host, port = _split_bind(configuration.text(_BIND_KEY))
        root = web_root()
        return cls(
            host=host,
            port=port,
            demo_mode=configuration.flag(_DEMO_KEY),
            interface_spec_path=_absolute(configuration.path(_SPEC_KEY), root),
            template_directory=root / DEFAULT_TEMPLATE_PATH,
            static_directory=root / DEFAULT_STATIC_PATH,
        )

    @property
    def bind(self) -> str:
        """The bind as one string, which is what the local server is given."""
        return f"{self.host}:{self.port}"


def _absolute(path: Path, root: Path) -> Path:
    """A configured path as an absolute one, resolved against the repository root."""
    return path if path.is_absolute() else root / path


def reader_store_factory(
    configuration: Configuration,
    *,
    reader: ParameterReader | None = None,
) -> Callable[[], MemoryStore] | None:
    """A factory for the read-only connection, or None where none is configured.

    The connection string comes from its own parameter, resolved through the same
    accessors the primary one is, and the role is fixed to the read-only name rather
    than read from the surface: a handle opened for an analysis that insists on the
    read-only role must not be able to claim a different one because a key was set
    wrongly.
    """
    # The key carries no default, because a default on a key naming where a credential
    # lives would ship a credential reference no operator chose. So an unset key is a
    # refusal from the surface rather than an empty string, and absence here means the
    # deployment configured no read-only connection.
    try:
        named = configuration.text(READER_DSN_PARAM_KEY).strip()
    except ConfigError:
        return None
    if not named:
        return None

    def build() -> MemoryStore:
        return MemoryStore.from_configuration(
            configuration.replacing({DSN_PARAM_KEY: named, ROLE_KEY: READ_ONLY_ROLE_NAME}),
            reader=reader,
        )

    return build


def resolve_console_credential(
    configuration: Configuration,
    *,
    reader: ParameterReader | None = None,
) -> Credential:
    """The stored credential record: the hash the presented value is verified against.

    Read once per process through the accessor whose cache is the container lifetime,
    and returned still wrapped, so nothing downstream can render it by accident.
    """
    return get_parameter(configuration.text(CREDENTIAL_PARAM_KEY), reader=reader)


def resolve_console_session_key(
    configuration: Configuration,
    *,
    reader: ParameterReader | None = None,
) -> Credential:
    """The key the session cookie's signature is keyed with."""
    return get_parameter(configuration.text(SESSION_KEY_PARAM_KEY), reader=reader)


class Console:
    """One process's console: the settings, the stores, the credentials, the clock.

    Two store handles rather than one, and the reason is a privilege rather than a
    convenience. The deployed function holds the eraser role, because the erasure
    console runs erasures from it; but the Sensitivity_Analyzer insists on a connection
    that authenticates as the read-only role, so that its no-mutation claim is something
    the cluster refuses to break rather than something this code promises not to. Those
    two facts cannot both be served by one connection, so the reader handle is opened
    separately, lazily, and only where a view asks for it.
    """

    __slots__ = (
        "_clock",
        "_credential",
        "_reader",
        "_reader_factory",
        "_session_key",
        "_settings",
        "_store",
    )

    def __init__(
        self,
        *,
        settings: ConsoleSettings,
        store: MemoryStore,
        credential: Credential,
        session_key: Credential,
        clock: Callable[[], datetime] | None = None,
        reader_factory: Callable[[], MemoryStore] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._credential = credential
        self._session_key = session_key
        self._clock = _now if clock is None else clock
        self._reader_factory = reader_factory
        self._reader: MemoryStore | None = None

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration | None = None,
        *,
        reader: ParameterReader | None = None,
        store: MemoryStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> Console:
        """Build the console for this process, resolving both credentials once."""
        resolved = load_configuration() if configuration is None else configuration
        settings = ConsoleSettings.from_configuration(resolved)
        built = MemoryStore.from_configuration(resolved, reader=reader) if store is None else store
        credential = resolve_console_credential(resolved, reader=reader)
        session_key = resolve_console_session_key(resolved, reader=reader)
        factory = reader_store_factory(resolved, reader=reader)
        log(
            Severity.INFO,
            COMPONENT,
            "resolved the console cold-start configuration",
            database_role=built.role,
            reader_connection=(
                "own_role"
                if built.role in READER_ROLE_NAMES
                else "configured"
                if factory is not None
                else "absent"
            ),
            demo_mode=settings.demo_mode,
            credential_source=credential.source_name,
            session_key_source=session_key.source_name,
            templates_present=settings.template_directory.is_dir(),
            specification_present=settings.interface_spec_path.is_file(),
        )
        return cls(
            settings=settings,
            store=built,
            credential=credential,
            session_key=session_key,
            clock=clock,
            reader_factory=factory,
        )

    @property
    def settings(self) -> ConsoleSettings:
        """The configured surface this console resolved at startup."""
        return self._settings

    @property
    def store(self) -> MemoryStore:
        """The connection surface every view reads through."""
        return self._store

    def reader_store(self) -> MemoryStore:
        """A connection authenticating as the read-only role, opened on first use.

        The analysis this exists for refuses any wider role by design, so a console
        holding the eraser role cannot run it on its own handle. Where the deployment's
        own store is already read-only that handle is returned unchanged, so a
        read-only deployment opens no second connection at all.

        Raises:
            ReaderRoleUnavailableError: No read-only connection is configured, so the
                analysis is reported as unavailable rather than run under a role that
                can write.
        """
        if self._store.role in READER_ROLE_NAMES:
            return self._store
        if self._reader is not None:
            return self._reader
        factory = self._reader_factory
        if factory is None:
            raise ReaderRoleUnavailableError(
                "this console authenticates as "
                f"{self._store.role or 'an unnamed role'} and no read-only connection "
                f"is configured, so {READER_DSN_PARAM_KEY} names the value to provision"
            )
        opened = factory()
        if opened.role not in READER_ROLE_NAMES:
            raise ReaderRoleUnavailableError(
                f"the configured read-only connection authenticates as {opened.role!r}, "
                "which is not the read-only role the analysis requires"
            )
        self._reader = opened
        log(
            Severity.INFO,
            COMPONENT,
            "opened a read-only connection for the analysis that requires one",
            database_role=opened.role,
        )
        return opened

    def read_only_store(self) -> MemoryStore:
        """The narrowest handle available to a view that only reads.

        Every view that registers no mutation route reaches its store through this
        rather than through `store`, so least privilege is the default a new read-only
        view inherits instead of a choice each one makes again. Where a read-only
        connection is configured this is that connection, and the reads a view issues
        are ones the cluster would refuse to let write.

        Unlike `reader_store` this does not refuse when none is configured: it answers
        with the primary handle instead. The difference is whose requirement is being
        served. The Sensitivity_Analyzer refuses a wider role itself, so an analysis run
        on a wider handle would be a false claim and is better reported as unavailable.
        A fleet listing makes no such claim, and a single-connection deployment — a
        local run, or a demonstration with one connection string — would lose the view
        entirely for a privilege it never depended on. The handle returned reports its
        own role, so a caller that needs to state which one it read with can.
        """
        try:
            return self.reader_store()
        except ReaderRoleUnavailableError:
            return self._store

    @property
    def demo_mode(self) -> bool:
        """Whether read-only demonstration mode is configured."""
        return self._settings.demo_mode

    @property
    def credential(self) -> Credential:
        """The stored credential record, still wrapped."""
        return self._credential

    @property
    def session_key(self) -> Credential:
        """The session-signing key, still wrapped."""
        return self._session_key

    def now(self) -> datetime:
        """The instant this request is judged against, with an offset."""
        return self._clock()


def _now() -> datetime:
    """The default clock: the current instant with an offset so it has one reading."""
    return datetime.now(UTC)
