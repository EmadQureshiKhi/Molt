"""Shared fixtures for every suite.

Three guarantees shape this module.

First, importing it requires no credential of any kind and no database driver.
Nothing at module scope opens a socket, reads a secret, or imports a driver, so
collection succeeds on a bare checkout and the unit, property, quality, and
specification suites run anywhere.

Second, the gates are structural rather than per-test. A marked test that needs
a reachable instance or needs cloud and provider credentials is skipped with a
message naming exactly what was missing, so a contributor holding nothing still
runs everything else and the workflow stays credential-free.

Third, nothing here calls a model provider and nothing here sleeps. Vectors and
generated text come from deterministic stubs, and the clock the lease and
ingress properties drive is an injected time source that advances on request.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import os
import secrets as token_source
import socket
import struct
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import pytest

# The vector width the schema fixes. A stub reporting any other width would make
# the residue and recall properties meaningless, so the stub reports this one.
EMBEDDING_DIMENSIONS: Final[int] = 1024

# Connection string sources, in resolution order. The first is set by the local
# instance script for a test session; the second is the local development key of
# the configuration surface. Neither has a default and neither is ever stored.
TEST_DSN_KEYS: Final[tuple[str, ...]] = ("MOLT_TEST_DSN", "MOLT_DSN")

# Cloud access is considered present when a region and one credential source are
# configured. Only names are ever read here, never values.
CLOUD_REGION_KEYS: Final[tuple[str, ...]] = ("AWS_REGION", "AWS_DEFAULT_REGION")
CLOUD_CREDENTIAL_KEYS: Final[tuple[str, ...]] = (
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)

# Model provider credentials resolve from a parameter name or an operator file
# path, exactly as the configuration surface states. The fixture below reports
# which source is configured and never opens it.
EMBEDDING_CREDENTIAL_KEYS: Final[tuple[str, ...]] = (
    "MOLT_EMBEDDING_CREDENTIAL_PARAM",
    "MOLT_EMBEDDING_CREDENTIAL_FILE",
)
TEXT_CREDENTIAL_KEYS: Final[tuple[str, ...]] = (
    "MOLT_TEXT_CREDENTIAL_PARAM",
    "MOLT_TEXT_CREDENTIAL_FILE",
)

# Markers whose tests need a reachable instance, and the marker whose tests need
# cloud and model provider credentials.
#
# Membership here states a prerequisite, not a subject. Four suite markers name a
# suite that is instance-backed by definition, and `instance` states the same
# prerequisite on its own for a test whose suite marker does not imply it. That
# separation is what keeps `perf` out of this set: a performance test measures a
# bound, and whether the bound is measured against a cluster or in this process
# is a second, independent fact. A benchmark of in-process work therefore runs on
# a bare checkout, and a benchmark of a cluster's own work carries `perf` and
# `instance` together and skips with the message below when none is reachable.
INSTANCE_MARKERS: Final[frozenset[str]] = frozenset(
    {"integration", "e2e", "concurrency", "skills", "instance"}
)
SERVICE_MARKER: Final[str] = "services"

_PROBE_TIMEOUT_SECONDS: Final[float] = 1.5
_DEFAULT_SQL_PORT: Final[int] = 26257

# A connection is typed loosely because the driver is imported lazily, which is
# what keeps this module importable without it installed.
Connection = Any

_NO_DSN_MESSAGE: Final[str] = (
    "no local CockroachDB instance is configured: set one of "
    + ", ".join(TEST_DSN_KEYS)
    + " to a connection string, or run scripts/run_local_db.sh start and export "
    "its output. The credential-free suites need neither."
)


# ---------------------------------------------------------------------------
# Local instance discovery
# ---------------------------------------------------------------------------


def _configured_dsn() -> str | None:
    """Return the first configured connection string, or nothing when absent."""
    for key in TEST_DSN_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _endpoint(dsn: str) -> tuple[str, int]:
    """Split a connection string into the host and port to probe."""
    parts = urlsplit(dsn)
    host = parts.hostname or "localhost"
    port = parts.port or _DEFAULT_SQL_PORT
    return host, port


def _reachable(dsn: str) -> bool:
    """Report whether the connection string's endpoint accepts a connection.

    A plain socket connect is deliberate: reachability must be answerable
    without the driver installed, and no statement is sent, so a probe costs one
    round trip and leaves no session behind.
    """
    host, port = _endpoint(dsn)
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class InstanceProbe:
    """The outcome of looking for a usable local instance."""

    dsn: str | None
    reachable: bool

    @property
    def skip_reason(self) -> str | None:
        """The message to skip with, or nothing when the instance is usable."""
        if self.dsn is None:
            return _NO_DSN_MESSAGE
        if not self.reachable:
            host, port = _endpoint(self.dsn)
            return (
                f"the configured CockroachDB instance at {host}:{port} did not "
                "accept a connection; start it with scripts/run_local_db.sh start"
            )
        return None


_probe_cache: InstanceProbe | None = None


def _probe_instance() -> InstanceProbe:
    """Probe once per process and reuse the outcome for every later question."""
    global _probe_cache
    if _probe_cache is None:
        dsn = _configured_dsn()
        _probe_cache = InstanceProbe(dsn=dsn, reachable=dsn is not None and _reachable(dsn))
    return _probe_cache


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip marked tests whose prerequisite is absent, with a reason that says why.

    Applying the gate at collection makes the credential-free guarantee
    structural: a suite needing an instance or needing credentials cannot fail
    for their absence, it can only skip.
    """
    probe = _probe_instance()
    instance_reason = probe.skip_reason
    service_reason = _service_skip_reason()

    for item in items:
        markers = {mark.name for mark in item.iter_markers()}
        if instance_reason is not None and markers & INSTANCE_MARKERS:
            item.add_marker(pytest.mark.skip(reason=instance_reason))
        if service_reason is not None and SERVICE_MARKER in markers:
            item.add_marker(pytest.mark.skip(reason=service_reason))


@pytest.fixture(scope="session")
def local_instance_dsn(worker_database: str | None) -> str:
    """The connection string of the single-node instance the session runs against.

    Session scope is what keeps the instance cost paid once: examples are
    isolated by a fresh schema rather than by a fresh instance.

    Under parallel execution the database in the string is this worker's own. Creating
    and dropping a schema modifies the descriptor of the database holding it, so
    workers sharing one database contend on one row for every module they start, which
    made the parallel suites fail in proportion to how many workers were running. A
    database each moves that contention inside a worker, where modules run one at a
    time and the bounded replay below is enough.
    """
    probe = _probe_instance()
    reason = probe.skip_reason
    if reason is not None or probe.dsn is None:
        pytest.skip(reason or _NO_DSN_MESSAGE)
    if worker_database is None:
        return probe.dsn
    return _with_database(probe.dsn, worker_database)


def _with_database(dsn: str, database: str) -> str:
    """The same connection string against another database, path replaced not appended."""
    parts = urlsplit(dsn)
    return parts._replace(path=f"/{database}").geturl()


@pytest.fixture(scope="session")
def worker_database(database_driver: ModuleType) -> Iterator[str | None]:
    """This worker's own database under parallel execution, or None when serial.

    Created once for the worker and dropped when the worker ends. The name is derived
    from the worker identifier the parallel plugin sets rather than generated, so a
    crashed worker leaves one predictable database behind rather than an accumulating
    set of random ones.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "").strip()
    if not worker:
        yield None
        return
    probe = _probe_instance()
    if probe.dsn is None:
        yield None
        return
    database = f"{_WORKER_DATABASE_PREFIX}{worker}"
    connection = database_driver.connect(probe.dsn, autocommit=True)
    try:
        _apply_schema_change(connection, f"CREATE DATABASE IF NOT EXISTS {database}")
        yield database
        _apply_schema_change(connection, f"DROP DATABASE IF EXISTS {database} CASCADE")
    finally:
        connection.close()


@pytest.fixture(scope="session")
def database_driver() -> ModuleType:
    """The database driver module, imported lazily so collection needs it not."""
    try:
        return importlib.import_module("psycopg")
    except ModuleNotFoundError:  # pragma: no cover - environment dependent
        pytest.skip(
            "the database driver is not installed; install the project "
            "dependencies to run the instance-backed suites"
        )


# The state code the cluster reports when a transaction must be replayed, and the
# bounded schedule a schema change is replayed on. Creating or dropping a schema
# modifies the *database* descriptor, which every schema in that database shares, so
# two modules whose setup and teardown overlap contend on one row however unrelated
# their tables are. That is not a fault to surface: it is the ordinary conflict the
# cluster asks the caller to replay, and a schema change carries no state for a replay
# to spoil.
_SERIALIZATION_FAILURE: Final[str] = "40001"
_DDL_ATTEMPTS: Final[int] = 8
_DDL_BACKOFF_SECONDS: Final[float] = 0.05

# What a parallel worker's own database is named. The worker identifier is appended, so
# the name is derived rather than drawn: a crashed worker leaves one database a later
# run reuses instead of an accumulating set of random ones.
_WORKER_DATABASE_PREFIX: Final[str] = "molt_test_worker_"


def _is_serialization_failure(error: BaseException) -> bool:
    """Whether the cluster asked for this transaction to be replayed."""
    return getattr(error, "sqlstate", None) == _SERIALIZATION_FAILURE


def _apply_schema_change(connection: Connection, statement: str) -> None:
    """Send one schema change, replaying it while the cluster reports a conflict.

    Without this the suites are flaky in proportion to how much of them runs at once:
    a serial run loses a module whenever one module's teardown drop overlaps the next
    module's create, and a parallel run loses several. Both were observed. The retry is
    bounded and the wait grows, so a genuinely stuck descriptor still reports rather
    than spinning.
    """
    last: BaseException | None = None
    for attempt in range(_DDL_ATTEMPTS):
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
        except Exception as error:
            if not _is_serialization_failure(error):
                raise
            last = error
            time.sleep(_DDL_BACKOFF_SECONDS * (attempt + 1))
            continue
        return
    raise AssertionError(f"a schema change still conflicted after {_DDL_ATTEMPTS} attempts: {last}")


@pytest.fixture(scope="module")
def fresh_schema(local_instance_dsn: str, database_driver: ModuleType) -> Iterator[Connection]:
    """A connection whose search path is a schema created for this module alone.

    Module scope with a per-module schema is the isolation boundary the testing
    strategy names: every module sees an empty namespace, and the namespace is
    dropped afterwards, so no module observes another module's rows.

    The create and the drop are replayed on a conflict, because they touch the one
    descriptor every schema in the database shares and so contend with the setup and
    teardown of every other module.
    """
    schema = f"molt_test_{token_source.token_hex(6)}"
    connection = database_driver.connect(local_instance_dsn, autocommit=True)
    try:
        _apply_schema_change(connection, f"CREATE SCHEMA {schema}")
        with connection.cursor() as cursor:
            cursor.execute(f"SET search_path TO {schema}")
        yield connection
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET search_path TO public")
            _apply_schema_change(connection, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# Credential presence, reported by name and never by value
# ---------------------------------------------------------------------------


def _first_configured(keys: Sequence[str]) -> str | None:
    """Return the name of the first configured key, never the value behind it."""
    for key in keys:
        if os.environ.get(key, "").strip():
            return key
    return None


@dataclass(frozen=True, slots=True)
class CredentialSources:
    """The configured credential sources, held as names only.

    No value is read, so an instance of this can be printed into a failure
    message or a log record without disclosing anything.
    """

    region_key: str | None
    cloud_key: str | None
    embedding_key: str | None
    text_key: str | None

    @property
    def complete(self) -> bool:
        """Whether cloud access and both provider credentials are configured."""
        return None not in (self.region_key, self.cloud_key, self.embedding_key, self.text_key)

    @property
    def missing(self) -> tuple[str, ...]:
        """The names of the absent sources, for the skip message."""
        absent: list[str] = []
        if self.region_key is None:
            absent.append(" or ".join(CLOUD_REGION_KEYS))
        if self.cloud_key is None:
            absent.append(" or ".join(CLOUD_CREDENTIAL_KEYS))
        if self.embedding_key is None:
            absent.append(" or ".join(EMBEDDING_CREDENTIAL_KEYS))
        if self.text_key is None:
            absent.append(" or ".join(TEXT_CREDENTIAL_KEYS))
        return tuple(absent)


def _credential_sources() -> CredentialSources:
    """Read which credential sources are configured, opening none of them."""
    return CredentialSources(
        region_key=_first_configured(CLOUD_REGION_KEYS),
        cloud_key=_first_configured(CLOUD_CREDENTIAL_KEYS),
        embedding_key=_first_configured(EMBEDDING_CREDENTIAL_KEYS),
        text_key=_first_configured(TEXT_CREDENTIAL_KEYS),
    )


def _service_skip_reason() -> str | None:
    """The message service-marked tests skip with, or nothing when they can run."""
    sources = _credential_sources()
    if sources.complete:
        return None
    return (
        "the service suite needs cloud access and a credential source for each "
        "model provider role; nothing is configured for: "
        + "; ".join(sources.missing)
        + ". Every other suite needs none of them."
    )


@pytest.fixture(scope="session")
def credential_sources() -> CredentialSources:
    """The configured credential source names, or a skip naming what is absent."""
    reason = _service_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    return _credential_sources()


# ---------------------------------------------------------------------------
# Deterministic stub providers
# ---------------------------------------------------------------------------


def _byte_stream(seed: bytes, length: int) -> bytes:
    """Expand a seed into as many deterministic bytes as asked for."""
    chunks: list[bytes] = []
    produced = 0
    counter = 0
    while produced < length:
        block = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        chunks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(chunks)[:length]


def deterministic_vector(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> tuple[float, ...]:
    """Return a reproducible unit-length vector of the fixed width for a text.

    The same text always yields the same vector and different texts yield
    different vectors, which is what lets the residue and recall properties place
    known distances without a provider call. The result is normalised because the
    index the design relies on orders by squared distance while the thresholds
    are expressed in cosine space, and on unit vectors those two orderings agree.
    """
    raw = struct.unpack(f">{dimensions}i", _byte_stream(text.encode(), dimensions * 4))
    scaled = [value / 2147483648.0 for value in raw]
    norm = math.sqrt(sum(component * component for component in scaled))
    if norm == 0.0:  # pragma: no cover - unreachable for any real digest
        unit = [0.0] * dimensions
        unit[0] = 1.0
        return tuple(unit)
    return tuple(component / norm for component in scaled)


@dataclass(frozen=True, slots=True)
class StubProbe:
    """What a stub reports when asked to describe itself."""

    name: str
    model_id: str
    reachable: bool
    dimensions: int | None = None
    supports_prompt_cache: bool | None = None


@dataclass(frozen=True, slots=True)
class StubTextResult:
    """A generated result carrying the token fields the cache metrics read."""

    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int


@dataclass(slots=True)
class StubEmbeddingProvider:
    """An embedding provider that computes vectors instead of calling a model.

    It reports the fixed width the schema declares, returns unit-length vectors,
    and records every text it was asked about, so a test can assert the call
    count without a network round trip.
    """

    name: str = "stub"
    model_id: str = "stub-embedding"
    dimensions: int = EMBEDDING_DIMENSIONS
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Return one unit-length vector per input text, in the input order."""
        self.calls.append(tuple(texts))
        return [deterministic_vector(text, self.dimensions) for text in texts]

    def probe(self) -> StubProbe:
        """Report reachability and the declared width the startup gate checks."""
        return StubProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


class PromptLike(Protocol):
    """The two attributes the text stub reads off a structured prompt.

    Declaring the shape structurally rather than importing a concrete prompt
    type keeps the stub working both before that type exists and after it does:
    anything exposing a stable prefix and a variable suffix satisfies this, and
    no import couples the fixtures to the module that will define it.
    """

    @property
    def stable_prefix(self) -> str:
        """The portion of the prompt that repeats across calls."""

    @property
    def variable_suffix(self) -> str:
        """The portion of the prompt that differs on each call."""


@dataclass(slots=True)
class StubTextProvider:
    """A text provider that answers from a script instead of calling a model.

    Prompt-cache support is a settable attribute rather than a constant, because
    the cache-efficiency property toggles the capability and asserts what the
    token fields report in each state. When support is off, the whole prompt is
    charged as input and both cache fields read zero; when it is on, the first
    use of a stable prefix is charged as a cache write and every later use of the
    same prefix is charged as a cache read.
    """

    name: str = "stub"
    model_id: str = "stub-text"
    supports_prompt_cache: bool = True
    scripted: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    seen_prefixes: set[str] = field(default_factory=set)

    def generate(self, prompt: PromptLike | str) -> StubTextResult:
        """Answer a prompt, accounting for the prefix exactly once when cached."""
        prefix, suffix = _prompt_parts(prompt)
        self.calls.append((prefix, suffix))

        prefix_tokens = _token_estimate(prefix)
        suffix_tokens = _token_estimate(suffix)
        if not self.supports_prompt_cache:
            input_tokens = prefix_tokens + suffix_tokens
            creation_tokens = 0
            read_tokens = 0
        elif prefix in self.seen_prefixes:
            input_tokens = suffix_tokens
            creation_tokens = 0
            read_tokens = prefix_tokens
        else:
            self.seen_prefixes.add(prefix)
            input_tokens = suffix_tokens
            creation_tokens = prefix_tokens
            read_tokens = 0

        text = self.scripted.pop(0) if self.scripted else _deterministic_text(prefix, suffix)
        return StubTextResult(
            text=text,
            model_id=self.model_id,
            input_tokens=input_tokens,
            output_tokens=_token_estimate(text),
            cache_creation_tokens=creation_tokens,
            cache_read_tokens=read_tokens,
        )

    def probe(self) -> StubProbe:
        """Report reachability and the cache capability the selector records."""
        return StubProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            supports_prompt_cache=self.supports_prompt_cache,
        )


def _prompt_parts(prompt: PromptLike | str) -> tuple[str, str]:
    """Split a prompt into its stable prefix and its variable suffix.

    A plain string is treated as all suffix, so a caller that has no structured
    prompt yet still works and is charged no cache write.
    """
    prefix = getattr(prompt, "stable_prefix", None)
    suffix = getattr(prompt, "variable_suffix", None)
    if prefix is None and suffix is None:
        return "", str(prompt)
    return str(prefix or ""), str(suffix or "")


def _token_estimate(text: str) -> int:
    """A stable token count standing in for a provider's own accounting."""
    return (len(text) + 3) // 4


def _deterministic_text(prefix: str, suffix: str) -> str:
    """Return reproducible generated text for a prompt the script does not cover."""
    digest = hashlib.sha256(f"{prefix}\x00{suffix}".encode()).hexdigest()
    return f"stub-generated:{digest[:16]}"


@pytest.fixture
def stub_embedding_provider() -> StubEmbeddingProvider:
    """A fresh embedding stub, so call records do not leak between tests."""
    return StubEmbeddingProvider()


@pytest.fixture
def stub_text_provider() -> StubTextProvider:
    """A fresh text stub reporting prompt-cache support."""
    return StubTextProvider()


@pytest.fixture
def stub_text_provider_factory() -> Callable[[bool], StubTextProvider]:
    """Build a text stub with prompt-cache support on or off, as a test chooses."""

    def build(supports_prompt_cache: bool) -> StubTextProvider:
        return StubTextProvider(supports_prompt_cache=supports_prompt_cache)

    return build


# ---------------------------------------------------------------------------
# Injected time source
# ---------------------------------------------------------------------------

# The clock starts at the epoch instant so that a run embeds no calendar value
# and every example sees the same starting point.
_EPOCH_OFFSET_SECONDS: Final[float] = 0.0


@dataclass(slots=True)
class ManualTimeSource:
    """A clock a test sets and advances, rather than one a test waits for.

    Two readings move together: a timezone-aware wall reading for the values that
    reach the database, and a monotonic reading for the interval arithmetic that
    lease expiry and request-age bounds perform. Advancing moves both by the same
    amount, so an expiry that would take a lease interval of real seconds costs
    one call instead.

    The point of injecting this is that production code takes a time source as a
    dependency and reads it, rather than calling a wall clock of its own. The
    lease and ingress properties then drive expiry and staleness directly, which
    is what keeps a hundred examples affordable.
    """

    instant: datetime = field(
        default_factory=lambda: datetime.fromtimestamp(_EPOCH_OFFSET_SECONDS, tz=UTC)
    )
    ticks: float = _EPOCH_OFFSET_SECONDS

    def now(self) -> datetime:
        """The current wall reading, timezone aware at microsecond precision."""
        return self.instant

    def monotonic(self) -> float:
        """The current monotonic reading in seconds, never moving backwards."""
        return self.ticks

    def advance(self, seconds: float) -> None:
        """Move both readings forward by a non-negative number of seconds."""
        if seconds < 0.0:
            raise ValueError("a monotonic reading cannot be moved backwards")
        self.instant = self.instant + timedelta(seconds=seconds)
        self.ticks = self.ticks + seconds

    def set_now(self, instant: datetime) -> None:
        """Place the wall reading at a chosen timezone-aware instant.

        The monotonic reading is untouched, because the two answer different
        questions: one is what gets stored, the other is how much time passed.
        """
        if instant.tzinfo is None:
            raise ValueError("the wall reading must carry a timezone")
        self.instant = instant

    def sleep(self, seconds: float) -> None:
        """Stand in for waiting by advancing, so no test ever blocks on a clock."""
        self.advance(seconds)


@pytest.fixture
def time_source() -> ManualTimeSource:
    """A fresh manual clock per test, starting at the same deterministic reading."""
    return ManualTimeSource()
