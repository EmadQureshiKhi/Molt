"""Unit tests for connection handling: transport security, timeouts, and the pool.

Nothing here opens a socket. The connection factory is a stub that records the
statements each connection is sent, so the rules that govern a connection are
asserted by reading what the store did rather than by reaching a cluster.

Four claims are checked, and each is one a caller cannot verify for itself: a
connection string read from the parameter store carries full certificate
verification or is refused by name; the statement timeout reaches the cluster as a
bound parameter rather than as formatted statement text; a leased connection comes
back, is reset, and is discarded instead of reused when it comes back unusable;
and closing the store closes what it holds and refuses further leases.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol

import pytest

from molt.config.resolve import Configuration, InvalidConfigValueError
from molt.config.secrets import clear_parameter_cache
from molt.errors import StoreError
from molt.store import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    PLATFORM_AUTHORITY,
    REQUIRED_SSL_MODE,
    ROOT_AUTHORITY_PARAMETER,
    SSL_MODE_PARAMETER,
    STATEMENT_TIMEOUT_STATEMENT,
    Connection,
    MemoryStore,
    require_verified_tls,
    resolved_platform_authority,
    root_authority_of,
    ssl_mode_of,
)
from molt.store.retry import BEGIN_STATEMENT, COMMIT_STATEMENT, SERIALIZABLE_STATEMENT

# A connection string shaped like the deployed one, carrying no credential value.
CLUSTER_URI: Final[str] = "postgresql://cluster.example:26257/molt"

# The loopback connection string the local development bypass carries.
LOCAL_URI: Final[str] = "postgresql://localhost:26257/molt_test?sslmode=disable"

# The parameter name the deployed path reads the role-scoped connection string by.
DSN_PARAMETER: Final[str] = "/molt/dsn/writer"


class StubCursor:
    """A cursor recording every statement and every bound parameter set."""

    def __init__(self, owner: StubConnection) -> None:
        self._owner = owner
        self.released = False

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement with its parameters, failing when told to."""
        self._owner.sent.append((query, None if params is None else tuple(params)))
        if self._owner.failing is not None and self._owner.failing in query:
            raise RuntimeError("the statement was refused")
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return nothing; no test here reads rows."""
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return nothing; no test here reads rows."""
        return []

    def close(self) -> None:
        """Mark this cursor released."""
        self.released = True


class StubConnection:
    """A connection recording what it was sent, closable and markable as dead."""

    def __init__(self, *, failing: str | None = None) -> None:
        self.sent: list[tuple[str, tuple[object, ...] | None]] = []
        self.closed = False
        self.failing = failing

    def cursor(self) -> StubCursor:
        """Open a recording cursor on this connection."""
        return StubCursor(self)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True

    @property
    def statements(self) -> list[str]:
        """Every statement this connection was sent, in order."""
        return [query for query, _ in self.sent]


class StubFactory:
    """A connection factory handing out fresh stubs and remembering each one."""

    def __init__(self, *, failing: str | None = None) -> None:
        self.built: list[StubConnection] = []
        self.failing = failing

    def __call__(self) -> Connection:
        """Build one stub connection and record it."""
        connection = StubConnection(failing=self.failing)
        self.built.append(connection)
        return connection


class StubReader:
    """A parameter reader answering with one value for one name."""

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value

    def get_parameter(self, **kwargs: str | bool) -> Mapping[str, object]:
        """Answer the configured name and nothing else."""
        assert kwargs["Name"] == self.name
        assert kwargs["WithDecryption"] is True
        return {"Parameter": {"Value": self.value}}


class StoreBuilder(Protocol):
    """Build a store whose lifetime the fixture below bounds."""

    def __call__(
        self,
        *,
        connect_with: Callable[[], Connection],
        statement_timeout_ms: int = ...,
        max_connections: int = ...,
    ) -> MemoryStore:
        """Build one store with the pool settings a test chooses."""


@pytest.fixture
def store_factory() -> Iterator[StoreBuilder]:
    """Build stores that are closed when the test ends, however it ends."""
    built: list[MemoryStore] = []

    def build(
        *,
        connect_with: Callable[[], Connection],
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> MemoryStore:
        store = MemoryStore(
            connect_with=connect_with,
            statement_timeout_ms=statement_timeout_ms,
            max_connections=max_connections,
        )
        built.append(store)
        return store

    try:
        yield build
    finally:
        for store in built:
            store.close()


@pytest.fixture(autouse=True)
def _forget_parameters() -> Iterator[None]:
    """Clear the process-lifetime parameter cache around every test here."""
    clear_parameter_cache()
    try:
        yield
    finally:
        clear_parameter_cache()


# ---------------------------------------------------------------------------
# Transport security
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (CLUSTER_URI, None),
        (f"{CLUSTER_URI}?{SSL_MODE_PARAMETER}={REQUIRED_SSL_MODE}", REQUIRED_SSL_MODE),
        (f"{CLUSTER_URI}?{SSL_MODE_PARAMETER}=REQUIRE", "require"),
        ("host=cluster.example port=26257 sslmode=verify-full", REQUIRED_SSL_MODE),
        ("host=cluster.example sslmode='require'", "require"),
        ("host=cluster.example port=26257", None),
    ],
)
def test_the_security_mode_is_read_from_either_connection_string_form(
    dsn: str, expected: str | None
) -> None:
    assert ssl_mode_of(dsn) == expected


def test_a_connection_string_naming_no_mode_gains_full_verification() -> None:
    required = require_verified_tls(CLUSTER_URI, source_name=DSN_PARAMETER)

    assert ssl_mode_of(required) == REQUIRED_SSL_MODE
    assert required.startswith(CLUSTER_URI)


def test_a_connection_string_already_verifying_fully_keeps_its_mode_and_gains_an_authority() -> (
    None
):
    """Verification already required is left required, and the authority is still settled.

    This case previously asserted the string came back untouched, which described a
    connection that could not be made: full verification with no authority set sends
    the client looking for an authority file under the calling user's home directory,
    and no deployed runtime holds one. So the mode is asserted to be preserved rather
    than the string, and the authority is asserted to be present.
    """
    dsn = f"{CLUSTER_URI}?{SSL_MODE_PARAMETER}={REQUIRED_SSL_MODE}"

    required = require_verified_tls(dsn, source_name=DSN_PARAMETER)

    assert ssl_mode_of(required) == REQUIRED_SSL_MODE
    assert root_authority_of(required) == resolved_platform_authority()


def test_an_authority_the_operator_named_is_left_alone() -> None:
    """An explicit trust anchor is a decision this module was not asked to make.

    Resolving the platform keyword is a service; replacing a path an operator wrote is
    an override. A deployment pinning its own authority set — a private cluster with a
    private issuer — must keep it, or this module would silently widen what that
    deployment trusts.
    """
    own = "/own/roots.pem"
    dsn = f"{CLUSTER_URI}?{SSL_MODE_PARAMETER}={REQUIRED_SSL_MODE}&{ROOT_AUTHORITY_PARAMETER}={own}"

    required = require_verified_tls(dsn, source_name=DSN_PARAMETER)

    assert root_authority_of(required) == own


def test_the_keyword_form_gains_full_verification_and_an_authority_too() -> None:
    required = require_verified_tls("host=cluster.example port=26257", source_name=DSN_PARAMETER)

    assert ssl_mode_of(required) == REQUIRED_SSL_MODE
    assert root_authority_of(required) == resolved_platform_authority()
    assert required.startswith("host=cluster.example port=26257 ")


def test_the_platform_keyword_is_resolved_to_a_bundle_this_process_can_see() -> None:
    """The substitution that makes the portable declaration actually connect.

    The keyword is the portable way to name the platform's own roots, but whether it
    resolves depends on how the driver's bundled cryptography library was built rather
    than on the platform running it — a string carrying the keyword connected on one
    machine and was refused on another. Resolving it here, in the process about to
    connect, is what makes the composing step's portable declaration usable.
    """
    dsn = (
        f"{CLUSTER_URI}?{SSL_MODE_PARAMETER}={REQUIRED_SSL_MODE}"
        f"&{ROOT_AUTHORITY_PARAMETER}={PLATFORM_AUTHORITY}"
    )

    required = require_verified_tls(dsn, source_name=DSN_PARAMETER)
    resolved = root_authority_of(required)

    assert resolved == resolved_platform_authority()
    assert required.count(ROOT_AUTHORITY_PARAMETER) == 1, (
        "the authority was added alongside the keyword rather than replacing it, so "
        "which value applies is left to the driver"
    )
    if resolved != PLATFORM_AUTHORITY:
        assert Path(resolved or "").is_file(), (
            "the resolved authority names no file, so this platform was reported to "
            "hold a bundle it does not have"
        )


def test_no_bundle_present_leaves_the_portable_keyword_rather_than_a_missing_path() -> None:
    """With no bundle to find, the keyword stands rather than a path that names nothing.

    Naming a file that is absent would turn a question the driver could still answer
    into a certain refusal, so the fallback is the declaration rather than a guess.
    """
    assert resolved_platform_authority(bundles=()) == PLATFORM_AUTHORITY


def test_a_weaker_mode_is_refused_naming_the_source_and_the_mode() -> None:
    dsn = f"{CLUSTER_URI}?{SSL_MODE_PARAMETER}=require"

    with pytest.raises(InvalidConfigValueError) as raised:
        require_verified_tls(dsn, source_name=DSN_PARAMETER)

    message = str(raised.value)
    assert DSN_PARAMETER in message
    assert "require" in message
    assert REQUIRED_SSL_MODE in message
    assert "cluster.example" not in message


def test_a_parameter_store_connection_string_is_dialled_with_full_verification(
    monkeypatch: pytest.MonkeyPatch,
    store_factory: StoreBuilder,
) -> None:
    assert store_factory is not None
    dialled: list[str] = []

    def fake_connect(dsn: str) -> Connection:
        dialled.append(dsn)
        return StubConnection()

    monkeypatch.setattr("molt.store.connect", fake_connect)
    resolved = Configuration(environ={"MOLT_DSN_PARAM": DSN_PARAMETER}, file_values={})
    store = MemoryStore.from_configuration(resolved, reader=StubReader(DSN_PARAMETER, CLUSTER_URI))
    try:
        with store.lease():
            pass
    finally:
        store.close()

    assert len(dialled) == 1
    assert ssl_mode_of(dialled[0]) == REQUIRED_SSL_MODE


def test_the_local_development_connection_string_is_dialled_as_it_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialled: list[str] = []

    def fake_connect(dsn: str) -> Connection:
        dialled.append(dsn)
        return StubConnection()

    monkeypatch.setattr("molt.store.connect", fake_connect)
    resolved = Configuration(environ={"MOLT_DSN": LOCAL_URI}, file_values={})
    store = MemoryStore.from_configuration(resolved)
    try:
        with store.lease():
            pass
    finally:
        store.close()

    assert dialled == [LOCAL_URI]


def test_the_configured_role_and_timeout_reach_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_connect(dsn: str) -> Connection:
        assert dsn == LOCAL_URI
        return StubConnection()

    monkeypatch.setattr("molt.store.connect", fake_connect)
    resolved = Configuration(
        environ={
            "MOLT_DSN": LOCAL_URI,
            "MOLT_DB_ROLE": "eraser",
            "MOLT_DB_STATEMENT_TIMEOUT_MS": "7500",
            "MOLT_DB_MAX_RETRIES": "3",
        },
        file_values={},
    )

    store = MemoryStore.from_configuration(resolved)
    try:
        assert store.role == "eraser"
        assert store.statement_timeout_ms == 7500
        assert store.retry_policy.max_retries == 3
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The statement timeout
# ---------------------------------------------------------------------------


def test_the_statement_timeout_is_set_with_the_value_bound(
    store_factory: StoreBuilder,
) -> None:
    factory = StubFactory()
    store = store_factory(connect_with=factory, statement_timeout_ms=10000)

    with store.lease():
        pass

    connection = factory.built[0]
    assert connection.sent[0] == (STATEMENT_TIMEOUT_STATEMENT, ("10000",))
    assert "10000" not in STATEMENT_TIMEOUT_STATEMENT


def test_the_timeout_is_established_once_per_connection_rather_than_per_lease(
    store_factory: StoreBuilder,
) -> None:
    factory = StubFactory()
    store = store_factory(connect_with=factory)

    for _ in range(3):
        with store.lease():
            pass

    assert len(factory.built) == 1
    statements = factory.built[0].statements
    assert statements.count(STATEMENT_TIMEOUT_STATEMENT) == 1


def test_a_connection_whose_preparation_fails_is_closed_and_leaks_no_lease(
    store_factory: StoreBuilder,
) -> None:
    factory = StubFactory(failing="set_config")
    store = store_factory(connect_with=factory, max_connections=1)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="refused"), store.lease():
            pass

    assert len(factory.built) == 2
    assert all(connection.closed for connection in factory.built)
    assert store.pool_state() == (0, 0)


# ---------------------------------------------------------------------------
# Transactions through the store
# ---------------------------------------------------------------------------


def test_a_write_through_the_store_is_framed_explicitly(
    store_factory: StoreBuilder,
) -> None:
    factory = StubFactory()
    store = store_factory(connect_with=factory)

    answer = store.in_serializable(lambda cursor: cursor.execute("SELECT 1"), label="probe")

    assert answer is None
    statements = factory.built[0].statements
    # The timeout leads and the pool's reset trails; between them sits the frame.
    assert statements[1:-1] == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        "SELECT 1",
        COMMIT_STATEMENT,
    ]


def test_a_read_through_the_store_frames_no_transaction(
    store_factory: StoreBuilder,
) -> None:
    factory = StubFactory()
    store = store_factory(connect_with=factory)

    store.read(lambda cursor: cursor.execute("SELECT 1"))

    statements = factory.built[0].statements
    assert BEGIN_STATEMENT not in statements
    assert statements[1] == "SELECT 1"


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


def test_a_returned_connection_is_reset_and_reused(
    store_factory: StoreBuilder,
) -> None:
    factory = StubFactory()
    store = store_factory(connect_with=factory)

    with store.lease() as first:
        assert store.pool_state() == (0, 1)
    assert store.pool_state() == (1, 0)
    with store.lease() as second:
        assert second is first

    assert len(factory.built) == 1
    assert factory.built[0].statements.count("ROLLBACK") == 2


def test_a_connection_that_died_while_idle_is_not_handed_out_again(
    store_factory: StoreBuilder,
) -> None:
    factory = StubFactory()
    store = store_factory(connect_with=factory)

    with store.lease() as first:
        pass
    first.close()

    with store.lease() as second:
        assert second is not first

    assert len(factory.built) == 2


def test_a_saturated_pool_reports_the_bound_it_waited_for(
    store_factory: StoreBuilder,
) -> None:
    store = store_factory(connect_with=StubFactory(), max_connections=1, statement_timeout_ms=50)

    def take_a_second_lease() -> None:
        with store.lease():
            pass

    with store.lease(), pytest.raises(StoreError, match="no connection became available"):
        take_a_second_lease()


def test_closing_the_store_closes_what_it_holds_and_refuses_further_leases() -> None:
    factory = StubFactory()
    store = MemoryStore(connect_with=factory)
    with store.lease():
        pass

    store.close()

    assert store.closed
    assert factory.built[0].closed
    assert store.pool_state() == (0, 0)
    with pytest.raises(StoreError, match="closed"), store.lease():
        pass


def test_a_connection_leased_when_the_store_closes_is_shut_on_return() -> None:
    factory = StubFactory()
    with MemoryStore(connect_with=factory) as store, store.lease() as held:
        store.close()
        assert not held.closed

    assert factory.built[0].closed


def test_an_incoherent_pool_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="statement timeout"):
        MemoryStore(connect_with=StubFactory(), statement_timeout_ms=0)
    with pytest.raises(ValueError, match="at least one connection"):
        MemoryStore(connect_with=StubFactory(), max_connections=0)
