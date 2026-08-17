"""The parameter store and the operator credential file, read live where configured.

One service is touched here and one local file convention beside it. The service
reads are made through the delivered accessor rather than through a client this
module builds, so what is exercised is the accessor's own behaviour against the
real store: the tier it reads in, the decryption it always asks for, the cache that
makes a second read free, the single retry, and the shape of every refusal.

**The tier claim is asserted by what the request does not carry.** Parameters live
in the tier with no per-parameter monthly charge, and the cost ceiling of the whole
deployment depends on that. The accessor takes no tier argument at all, which is
the structural half; the live half is that the request as sent names a parameter and
asks for decryption and names nothing else, so no request can take the advanced
tier path. A proxy over the real client records the arguments and forwards them, so
that assertion is over the call that was actually made.

**One retry, and no loop, asserted against the real store.** The unit suite drives
the retry with a stub, which is where an attempt count belongs. What it cannot show
is that the retry reaches the real store and the real store answers on the second
attempt. Here the first attempt is failed by the proxy with a throttle-shaped fault
and the second is forwarded, and the value comes back from the service.

**Every refusal names the source and never the value.** A missing parameter names
the parameter. A credential file granting access beyond its owner names the path
and the mode. The local connection-string bypass reached with production marked
names the variable and the marker. All three are asserted to carry nothing read
from the source they name, and the value used in the file case is an obviously
synthetic marker so nothing real is written to a temporary path.

**The production refusal needs no service at all**, so it is driven over an overlay
of the resolved surface rather than over the process environment: nothing is
written back, and the deployment's own marker is left exactly as it was.

**Every test here skips in this environment.** The `services` marker answers
whether cloud access and a credential source for each provider role are configured,
and each test additionally names the configuration key holding the parameter name
or the file path it needs. No parameter store entry exists, so every one of them
skips saying which key an operator sets next.

**What a full run costs, where one is possible.** Three parameter reads of one name
each, of which one is served from the per-process cache, one read of a name
deliberately absent, and no write of any kind. Nothing here loops and no test
scales its call count with anything.

No parameter path, credential value, region, or account identifier appears in this
file. Every one is read from the configuration surface at run time.
"""

from __future__ import annotations

import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Final, cast

import pytest

from molt.config.resolve import (
    Configuration,
    MissingConfigError,
    UnknownSettingError,
    load_configuration,
)
from molt.config.secrets import (
    CREDENTIAL_PLACEHOLDER,
    FORBIDDEN_FILE_BITS,
    PARAMETER_TIER,
    PRODUCTION_FLAG_ENV,
    PRODUCTION_FLAG_VALUE,
    CredentialFileError,
    CredentialSource,
    LocalBypassRefusedError,
    ParameterMissingError,
    ParameterReader,
    clear_parameter_cache,
    get_parameter,
    is_production,
    load_credential,
    read_credential_file,
    resolve_dsn,
)

# Cloud access and a credential source for each provider role. Each test names the
# configuration key it needs beyond that.
pytestmark = pytest.mark.services

# The keys a parameter name may be configured under, in the order a live read
# prefers them. The connection-string key comes last so a read that has any other
# choice does not pull a connection string into this process.
PARAMETER_NAME_KEYS: Final[tuple[str, ...]] = (
    "MOLT_INGRESS_SECRET_PARAM",
    "MOLT_COLLECTOR_TOKEN_PARAM",
    "MOLT_EMBEDDING_CREDENTIAL_PARAM",
    "MOLT_TEXT_CREDENTIAL_PARAM",
    "MOLT_CONSOLE_CREDENTIAL_PARAM",
    "MOLT_DSN_PARAM",
)

# The keys an operator credential file may be configured under.
CREDENTIAL_FILE_KEYS: Final[tuple[str, ...]] = (
    "MOLT_EMBEDDING_CREDENTIAL_FILE",
    "MOLT_TEXT_CREDENTIAL_FILE",
)

# The two request arguments a read is allowed to carry, and nothing else. A third
# argument would be the way a request left the tier this deployment pays for.
PERMITTED_REQUEST_ARGUMENTS: Final[frozenset[str]] = frozenset({"Name", "WithDecryption"})

# What a name that exists is not. Appended to a configured name so the absent-read
# probe asks for a path derived from configuration rather than one written here.
ABSENT_SUFFIX: Final[str] = "/molt-service-probe-absent"

# The error code a throttle arrives under. Naming it is what lets the retry be
# provoked without waiting for the store to throttle of its own accord.
THROTTLE_CODE: Final[str] = "ThrottlingException"

# The cloud package and the parameter service a live read is made through.
CLOUD_PACKAGE: Final[str] = "boto3"
PARAMETER_SERVICE: Final[str] = "ssm"

# An obviously synthetic value, short and shaped like no real credential. Used
# wherever a file has to hold something and nothing real may be written.
MARKER_VALUE: Final[str] = "fake-value-1"

# Owner-only, and a mode granting the group read access that is a finding.
OWNER_ONLY_MODE: Final[int] = 0o600
GROUP_READABLE_MODE: Final[int] = 0o640


@pytest.fixture(autouse=True)
def _clean_parameter_cache() -> None:
    """Clear the process-lifetime cache before every test in this module.

    The cache is never invalidated at runtime by design, so without this a value
    one test read would answer the next test's read and the attempt counts below
    would measure nothing.
    """
    clear_parameter_cache()


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def _configuration() -> Configuration:
    """The resolved surface every value below is read from."""
    return load_configuration()


def _configured_parameter(configuration: Configuration) -> tuple[str, str]:
    """The first configured parameter key and the name it holds, or a skip.

    The skip names every key that could have answered, because which of them an
    operator sets is their choice and a message naming one of six would send them
    looking in the wrong place.
    """
    for key in PARAMETER_NAME_KEYS:
        try:
            name = configuration.optional_text(key)
        except UnknownSettingError:  # pragma: no cover - the surface declares them all
            continue
        if name:
            return key, name
    pytest.skip(
        "no parameter name is configured, so no parameter store entry is provisioned "
        "and no call was made; set one of " + ", ".join(PARAMETER_NAME_KEYS)
    )


def _configured_credential_file(configuration: Configuration) -> tuple[str, Path]:
    """The first configured credential file key and its path, or a skip naming both."""
    for key in CREDENTIAL_FILE_KEYS:
        path = configuration.optional_path(key)
        if path is not None and path.is_file():
            return key, path
    pytest.skip(
        "no operator credential file is configured and present, so nothing was read; "
        "set one of " + ", ".join(CREDENTIAL_FILE_KEYS)
    )


# ---------------------------------------------------------------------------
# The live reader, behind a recording proxy
# ---------------------------------------------------------------------------


class ThrottleError(Exception):
    """A failure carrying the code shape the accessor classifies a throttle by.

    Raised by the proxy rather than waited for, because a retry that only happens
    when the store happens to throttle is a retry nobody can observe.
    """

    def __init__(self) -> None:
        super().__init__(THROTTLE_CODE)
        self.response: Mapping[str, object] = {"Error": {"Code": THROTTLE_CODE}}


@dataclass(eq=False)
class RecordingReader:
    """The real parameter client, recording each request and failing on a script.

    The first `failures` attempts raise a throttle-shaped fault and every later
    attempt is forwarded to the service, so the attempt count and the arguments of
    each attempt are both observable while the value still comes from the store.
    """

    inner: ParameterReader
    failures: int = 0
    attempts: int = 0
    arguments: list[frozenset[str]] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    decryption: list[object] = field(default_factory=list)

    def get_parameter(self, **kwargs: str | bool) -> Mapping[str, object]:
        """Record one attempt, fail it while the script says to, then forward it."""
        self.attempts += 1
        self.arguments.append(frozenset(kwargs))
        self.names.append(str(kwargs.get("Name", "")))
        self.decryption.append(kwargs.get("WithDecryption"))
        if self.attempts <= self.failures:
            raise ThrottleError
        return self.inner.get_parameter(**kwargs)


def _parameter_client() -> ParameterReader:
    """Build a real parameter client, importing the cloud library at call time.

    The import happens here rather than at module scope so collection needs no
    library resolution and no credential chain, which is what lets this module be
    collected and skipped on a bare checkout. Region and credentials resolve
    through the library's own chain, so nothing about either is restated.
    """
    module = import_module(CLOUD_PACKAGE)
    return cast(ParameterReader, module.client(PARAMETER_SERVICE))


def _live_reader(*, failures: int = 0) -> RecordingReader:
    """Build a proxy over a real client, confirmed to satisfy the reader shape."""
    proxy = RecordingReader(inner=_parameter_client(), failures=failures)
    reader: ParameterReader = proxy
    assert reader is proxy
    return proxy


# ---------------------------------------------------------------------------
# Standard-tier retrieval
# ---------------------------------------------------------------------------


def test_a_configured_parameter_is_read_and_never_renders_its_value() -> None:
    """One live read, reported by source, and a value that renders as the placeholder."""
    configuration = _configuration()
    key, name = _configured_parameter(configuration)
    reader = _live_reader()

    credential = get_parameter(name, reader=reader)

    assert credential.source is CredentialSource.PARAMETER
    assert credential.source_name == name
    assert credential.reveal(), f"the parameter named by {key} holds a value"
    assert str(credential) == CREDENTIAL_PLACEHOLDER
    assert repr(credential) == CREDENTIAL_PLACEHOLDER

    # Every rendering path yields the placeholder, so the count is the number of
    # renderings and the value itself never enters this comparison.
    rendered = f"{credential} {credential!r} {credential:>40}"
    assert rendered.count(CREDENTIAL_PLACEHOLDER) == 3
    assert reader.attempts == 1


def test_every_read_asks_for_decryption_and_names_no_tier() -> None:
    """The request carries a name and a decryption flag, and nothing else.

    A third argument is how a request would leave the tier carrying no
    per-parameter monthly charge, which the deployment's cost ceiling depends on.
    The accessor offers no tier argument to get wrong, and the live request is
    asserted to carry none.
    """
    configuration = _configuration()
    _key, name = _configured_parameter(configuration)
    reader = _live_reader()

    get_parameter(name, reader=reader)

    assert reader.arguments == [PERMITTED_REQUEST_ARGUMENTS]
    assert reader.names == [name]
    assert reader.decryption == [True]
    assert PARAMETER_TIER == "Standard"


def test_a_second_read_of_the_same_name_makes_no_further_call() -> None:
    """The per-process cache answers the second read, so a cold start pays once."""
    configuration = _configuration()
    _key, name = _configured_parameter(configuration)
    reader = _live_reader()

    first = get_parameter(name, reader=reader)
    second = get_parameter(name, reader=reader)

    assert first is second
    assert reader.attempts == 1


def test_a_transient_fault_is_retried_exactly_once_and_the_store_answers() -> None:
    """One retry, then the real store answers. Not a loop.

    An unreachable parameter store is a startup failure to report rather than a
    condition to wait out, so the attempt count is asserted exactly rather than
    bounded.
    """
    configuration = _configuration()
    _key, name = _configured_parameter(configuration)
    reader = _live_reader(failures=1)

    credential = get_parameter(name, reader=reader)

    assert credential.source is CredentialSource.PARAMETER
    assert credential.reveal()
    assert reader.attempts == 2


def test_a_read_of_a_name_that_does_not_exist_names_the_parameter_and_no_value() -> None:
    """An absent parameter is reported by name, with nothing read from anywhere.

    The name is derived from a configured one rather than written here, so this
    module names no parameter path of its own, and the read creates nothing.
    """
    configuration = _configuration()
    _key, name = _configured_parameter(configuration)
    absent = f"{name}{ABSENT_SUFFIX}"
    reader = _live_reader()

    with pytest.raises(ParameterMissingError) as caught:
        get_parameter(absent, reader=reader)

    assert caught.value.parameter_name == absent
    message = str(caught.value)
    assert absent in message
    assert CREDENTIAL_PLACEHOLDER not in message
    assert reader.attempts == 1, "an absent parameter is not worth a second attempt"


# ---------------------------------------------------------------------------
# The local bypass, refused where it would be dangerous
# ---------------------------------------------------------------------------


def test_the_local_connection_string_bypass_is_refused_with_production_marked() -> None:
    """The development convenience is unreachable exactly where it would be dangerous.

    Driven over an overlay of the resolved surface, so the deployment's own marker
    is left as it is and nothing is written back to the process environment. No
    service is called on this path at all: the refusal happens before any read.
    """
    configuration = _configuration()
    overlaid = configuration.replacing(
        {"MOLT_DSN": MARKER_VALUE, PRODUCTION_FLAG_ENV: PRODUCTION_FLAG_VALUE}
    )

    assert is_production(overlaid) is True
    with pytest.raises(LocalBypassRefusedError) as caught:
        resolve_dsn(overlaid)

    message = str(caught.value)
    assert "MOLT_DSN" in message
    assert PRODUCTION_FLAG_ENV in message
    assert MARKER_VALUE not in message, "a refusal names the variable rather than the value"

    # Without the marker the same overlay resolves, which is what makes the refusal
    # a production posture rather than a broken accessor.
    permitted = configuration.replacing({"MOLT_DSN": MARKER_VALUE, PRODUCTION_FLAG_ENV: "local"})
    assert resolve_dsn(permitted).reveal() == MARKER_VALUE


# ---------------------------------------------------------------------------
# The operator credential file
# ---------------------------------------------------------------------------


def test_the_configured_credential_file_is_read_and_reported_by_source() -> None:
    """A configured file answers through the same accessor a parameter does."""
    configuration = _configuration()
    key, path = _configured_credential_file(configuration)
    directory = path.parent

    credential = read_credential_file(path, directory=directory)

    assert credential.source is CredentialSource.FILE
    assert credential.source_name == str(path)
    assert credential.reveal(), f"the file {key} names holds a value"
    assert str(credential) == CREDENTIAL_PLACEHOLDER

    # The same file resolves through the pair-of-alternatives accessor, which is the
    # entry point the provider roles actually use.
    loaded = load_credential(
        configuration.replacing({"MOLT_TEXT_CREDENTIAL_PARAM": ""}),
        parameter_env="MOLT_TEXT_CREDENTIAL_PARAM",
        file_env=key,
        directory=directory,
    )
    assert loaded.source is CredentialSource.FILE


def test_a_credential_file_granting_access_beyond_its_owner_is_refused(tmp_path: Path) -> None:
    """A credential readable by anyone on the machine has already leaked.

    The file is written under the temporary tree and holds an obviously synthetic
    marker, so nothing real is ever placed at a widened mode. The refusal names the
    path and the mode, which is what an operator fixes, and carries nothing read
    from the file.
    """
    directory = tmp_path / "secrets"
    directory.mkdir(mode=OWNER_ONLY_MODE | stat.S_IXUSR)
    path = directory / "operator"
    path.write_text(MARKER_VALUE, encoding="utf-8")
    path.chmod(GROUP_READABLE_MODE)

    with pytest.raises(CredentialFileError) as caught:
        read_credential_file(path, directory=directory)

    message = str(caught.value)
    assert str(path) in message
    assert f"{GROUP_READABLE_MODE:04o}" in message
    assert MARKER_VALUE not in message
    assert stat.S_IMODE(path.stat().st_mode) & FORBIDDEN_FILE_BITS

    # Restricted to the owner, the same file is accepted, so the refusal is about
    # the mode rather than about the path or the content.
    path.chmod(OWNER_ONLY_MODE)
    assert read_credential_file(path, directory=directory).reveal() == MARKER_VALUE


def test_a_credential_file_outside_the_configured_directory_is_refused(tmp_path: Path) -> None:
    """A file outside the directory an operator configured is not the file meant."""
    directory = tmp_path / "secrets"
    directory.mkdir(mode=OWNER_ONLY_MODE | stat.S_IXUSR)
    outside = tmp_path / "elsewhere"
    outside.write_text(MARKER_VALUE, encoding="utf-8")
    outside.chmod(OWNER_ONLY_MODE)

    with pytest.raises(CredentialFileError) as caught:
        read_credential_file(outside, directory=directory)

    message = str(caught.value)
    assert str(outside) in message
    assert str(directory) in message
    assert MARKER_VALUE not in message


def test_neither_a_parameter_name_nor_a_file_path_names_both_keys() -> None:
    """With neither alternative configured the refusal names both of them."""
    configuration = _configuration().replacing(
        {"MOLT_TEXT_CREDENTIAL_PARAM": "", "MOLT_TEXT_CREDENTIAL_FILE": ""}
    )
    with pytest.raises(MissingConfigError) as caught:
        load_credential(
            configuration,
            parameter_env="MOLT_TEXT_CREDENTIAL_PARAM",
            file_env="MOLT_TEXT_CREDENTIAL_FILE",
        )
    message = str(caught.value)
    assert "MOLT_TEXT_CREDENTIAL_PARAM" in message
    assert "MOLT_TEXT_CREDENTIAL_FILE" in message
