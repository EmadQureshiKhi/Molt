"""Unit tests for the secret accessors: suppression, refusal, files, and one retry.

Every value used here is synthetic, short, and shaped like nothing real, because
a tracked test file is scanned for credential shapes exactly as any other source
file is.

The suppression tests are deliberately exhaustive about rendering paths rather
than about values. A wrapped credential has to render as the placeholder through
text conversion, representation, format with and without a specification, an
exception message, a structured log record, and an output stream, because those
are the six ways a value has historically escaped. Equality and hashing are
asserted absent as well: a comparison failure message and a set membership report
are two more rendering paths, and the way to close them is to leave the two
methods unimplemented rather than to override them.

The parameter reader is driven through a stub so the attempt count is observable.
One transient failure is retried once and the second attempt is the last; there
is no loop. The cache is asserted to make a second read cost no further call, and
it is cleared around every test because it lives for the process otherwise.
"""

from __future__ import annotations

import io
import json
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

import pytest

from molt.config.resolve import Configuration, MissingConfigError
from molt.config.secrets import (
    CREDENTIAL_PLACEHOLDER,
    DEFAULT_CREDENTIAL_DIRECTORY,
    FORBIDDEN_FILE_BITS,
    PARAMETER_TIER,
    PRODUCTION_FLAG_ENV,
    PRODUCTION_FLAG_VALUE,
    Credential,
    CredentialFileError,
    CredentialSource,
    LocalBypassRefusedError,
    ParameterMissingError,
    ParameterReader,
    ParameterUnavailableError,
    clear_parameter_cache,
    get_parameter,
    is_production,
    load_credential,
    read_credential_file,
    resolve_collector_bearer,
    resolve_dsn,
    resolve_ingress_signing_key,
)
from molt.telemetry import Severity, Telemetry

# Obviously fake, short, and shaped like no real credential of any kind.
FAKE_VALUE: Final[str] = "fake-value-1"
FAKE_OTHER: Final[str] = "fake-value-2"

# Parameter names the stub reader answers for. A name is not a secret.
PARAMETER_NAME: Final[str] = "/molt/test/one"
OTHER_PARAMETER_NAME: Final[str] = "/molt/test/two"

# Failure codes the reader classifies: one worth a single retry, one not, and the
# one that means the parameter does not exist.
TRANSIENT_CODE: Final[str] = "ThrottlingException"
PERMANENT_CODE: Final[str] = "AccessDeniedException"
MISSING_CODE: Final[str] = "ParameterNotFound"

# Owner-only, and two modes granting access beyond the owner.
OWNER_ONLY_MODE: Final[int] = 0o600
GROUP_READABLE_MODE: Final[int] = 0o640
OTHER_READABLE_MODE: Final[int] = 0o604


@pytest.fixture(autouse=True)
def _clean_parameter_cache() -> Iterator[None]:
    """Clear the process-lifetime parameter cache around every test.

    The cache is deliberately never invalidated at runtime, so without this a
    value read by one test would answer a read in the next one.
    """
    clear_parameter_cache()
    yield
    clear_parameter_cache()


class StubCloudError(Exception):
    """A failure carrying the error code shape the reader classifies against."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response: Mapping[str, object] = {"Error": {"Code": code}}


class StubReader:
    """A parameter reader that counts attempts and fails on a script.

    The first `failures` attempts raise with `code`; every later attempt answers
    with the value. Counting attempts on the stub is what makes "one retry, and
    no loop" observable rather than inferred.
    """

    def __init__(self, *, value: str = FAKE_VALUE, failures: int = 0, code: str = "") -> None:
        self.value = value
        self.failures = failures
        self.code = code
        self.attempts = 0
        self.names: list[str] = []
        self.decryption: list[object] = []

    def get_parameter(self, **kwargs: str | bool) -> Mapping[str, object]:
        """Answer one read, or fail while the script still says to."""
        self.attempts += 1
        self.names.append(str(kwargs.get("Name", "")))
        self.decryption.append(kwargs.get("WithDecryption"))
        if self.attempts <= self.failures:
            raise StubCloudError(self.code)
        return {"Parameter": {"Name": kwargs.get("Name"), "Value": self.value}}


def _reader(*, value: str = FAKE_VALUE, failures: int = 0, code: str = "") -> StubReader:
    """Build a stub and confirm it satisfies the reader shape the module declares."""
    stub = StubReader(value=value, failures=failures, code=code)
    reader: ParameterReader = stub
    assert reader is stub
    return stub


# ---------------------------------------------------------------------------
# A loaded credential never renders
# ---------------------------------------------------------------------------


def _credential() -> Credential:
    """A credential wrapping the synthetic value, from the environment source."""
    return Credential(FAKE_VALUE, source_name="MOLT_TEST", source=CredentialSource.ENVIRONMENT)


def test_text_representation_and_format_all_render_the_placeholder() -> None:
    credential = _credential()
    assert str(credential) == CREDENTIAL_PLACEHOLDER
    assert repr(credential) == CREDENTIAL_PLACEHOLDER
    assert format(credential) == CREDENTIAL_PLACEHOLDER
    assert format(credential, ">40") == CREDENTIAL_PLACEHOLDER
    assert format(credential, "s") == CREDENTIAL_PLACEHOLDER
    assert f"{credential}" == CREDENTIAL_PLACEHOLDER
    assert f"{credential!r}" == CREDENTIAL_PLACEHOLDER
    assert f"{credential:>40}" == CREDENTIAL_PLACEHOLDER
    for rendering in (str(credential), repr(credential), format(credential, ">40")):
        assert FAKE_VALUE not in rendering


def test_the_value_is_reachable_only_through_the_explicit_call() -> None:
    credential = _credential()
    assert credential.reveal() == FAKE_VALUE
    assert credential.source_name == "MOLT_TEST"
    assert credential.source is CredentialSource.ENVIRONMENT
    assert not hasattr(credential, "__dict__")


def test_an_exception_message_built_from_a_credential_carries_the_placeholder() -> None:
    credential = _credential()
    with pytest.raises(RuntimeError) as caught:
        raise RuntimeError(f"the read failed while holding {credential}")
    assert CREDENTIAL_PLACEHOLDER in str(caught.value)
    assert FAKE_VALUE not in str(caught.value)
    assert FAKE_VALUE not in repr(caught.value)


def test_a_log_record_carries_the_placeholder_and_drops_a_credential_named_field() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.DEBUG, stream=sink)
    telemetry.log(
        Severity.ERROR,
        "secrets",
        "a read failed",
        detail=_credential(),
        credential=_credential(),
        nested={"inner": _credential()},
    )
    line = sink.getvalue().strip()
    assert FAKE_VALUE not in line
    record: dict[str, object] = json.loads(line)
    assert record["detail"] == CREDENTIAL_PLACEHOLDER
    assert record["nested"] == {"inner": CREDENTIAL_PLACEHOLDER}
    # The field filter drops a field named after a credential outright, so the
    # placeholder is the second line of defence rather than the only one.
    assert "credential" not in record


def test_an_output_stream_carries_the_placeholder() -> None:
    credential = _credential()
    stream = io.StringIO()
    print(credential, file=stream)
    print(f"the value is {credential}", file=stream)
    written = stream.getvalue()
    assert written.count(CREDENTIAL_PLACEHOLDER) == 2
    assert FAKE_VALUE not in written


def test_neither_equality_nor_hashing_is_implemented() -> None:
    # Leaving both unimplemented is what keeps a value out of a comparison
    # failure message and out of a membership report.
    assert "__eq__" not in Credential.__dict__
    assert "__hash__" not in Credential.__dict__
    first = _credential()
    second = _credential()
    assert first != second
    assert first == first
    holder = {first}
    assert second not in holder
    assert FAKE_VALUE not in repr(holder)
    assert FAKE_VALUE not in str([first, second])
    assert FAKE_VALUE not in repr({"held": first})


def test_the_parameter_tier_named_is_the_one_carrying_no_monthly_charge() -> None:
    assert PARAMETER_TIER == "Standard"


# ---------------------------------------------------------------------------
# The production bypass refusal
# ---------------------------------------------------------------------------


def test_the_local_connection_string_resolves_in_development() -> None:
    configuration = Configuration(environ={"MOLT_DSN": FAKE_VALUE}, file_values={})
    credential = resolve_dsn(configuration)
    assert credential.reveal() == FAKE_VALUE
    assert credential.source is CredentialSource.ENVIRONMENT
    assert credential.source_name == "MOLT_DSN"


def test_the_local_connection_string_bypass_is_refused_in_production() -> None:
    configuration = Configuration(
        environ={"MOLT_DSN": FAKE_VALUE, PRODUCTION_FLAG_ENV: PRODUCTION_FLAG_VALUE},
        file_values={},
    )
    with pytest.raises(LocalBypassRefusedError) as caught:
        resolve_dsn(configuration)
    message = str(caught.value)
    assert "MOLT_DSN" in message
    assert PRODUCTION_FLAG_ENV in message
    assert PRODUCTION_FLAG_VALUE in message
    assert FAKE_VALUE not in message


@pytest.mark.parametrize("marker", ["production", "PRODUCTION", " Production "])
def test_the_production_marker_is_recognised_however_it_is_spelled(marker: str) -> None:
    configuration = Configuration(environ={PRODUCTION_FLAG_ENV: marker}, file_values={})
    assert is_production(configuration) is True


@pytest.mark.parametrize("marker", ["", "  ", "development", "staging", "prod"])
def test_nothing_but_the_production_marker_counts_as_production(marker: str) -> None:
    configuration = Configuration(environ={PRODUCTION_FLAG_ENV: marker}, file_values={})
    assert is_production(configuration) is False


def test_without_the_local_bypass_the_connection_string_comes_from_the_parameter() -> None:
    configuration = Configuration(
        environ={"MOLT_DSN_PARAM": PARAMETER_NAME, PRODUCTION_FLAG_ENV: PRODUCTION_FLAG_VALUE},
        file_values={},
    )
    reader = _reader()
    credential = resolve_dsn(configuration, reader=reader)
    assert credential.source is CredentialSource.PARAMETER
    assert credential.source_name == PARAMETER_NAME
    assert reader.names == [PARAMETER_NAME]


def test_the_bearer_and_the_signing_value_resolve_from_their_own_variables() -> None:
    configuration = Configuration(
        environ={"MOLT_COLLECTOR_TOKEN": FAKE_VALUE, "MOLT_INGRESS_SECRET": FAKE_OTHER},
        file_values={},
    )
    bearer = resolve_collector_bearer(configuration)
    signing = resolve_ingress_signing_key(configuration)
    assert bearer.reveal() == FAKE_VALUE
    assert signing.reveal() == FAKE_OTHER
    assert bearer.source is CredentialSource.ENVIRONMENT
    assert signing.source is CredentialSource.ENVIRONMENT


# ---------------------------------------------------------------------------
# The operator credential file accessor
# ---------------------------------------------------------------------------


@pytest.fixture
def credential_directory(tmp_path: Path) -> Path:
    """A credential directory under the temporary tree, never the real one."""
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    return directory


def _write_file(path: Path, text: str, mode: int) -> Path:
    """Write a credential file and set its mode explicitly."""
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_the_default_directory_is_relative_and_is_not_read_here() -> None:
    # Every test in this module names its own directory under the temporary tree,
    # so nothing here opens the real one.
    assert Path(".secrets") == DEFAULT_CREDENTIAL_DIRECTORY
    assert not DEFAULT_CREDENTIAL_DIRECTORY.is_absolute()


def test_an_owner_only_file_is_accepted(credential_directory: Path) -> None:
    path = _write_file(credential_directory / "one", f"  {FAKE_VALUE}\n", OWNER_ONLY_MODE)
    credential = read_credential_file(path, directory=credential_directory)
    assert credential.reveal() == FAKE_VALUE
    assert credential.source is CredentialSource.FILE
    assert credential.source_name == str(path)
    assert str(credential) == CREDENTIAL_PLACEHOLDER


@pytest.mark.parametrize("mode", [GROUP_READABLE_MODE, OTHER_READABLE_MODE, 0o666, 0o700 | 0o070])
def test_a_file_granting_access_beyond_the_owner_is_refused_naming_the_mode(
    credential_directory: Path, mode: int
) -> None:
    path = _write_file(credential_directory / "loose", FAKE_VALUE, mode)
    with pytest.raises(CredentialFileError) as caught:
        read_credential_file(path, directory=credential_directory)
    message = str(caught.value)
    assert f"{mode:04o}" in message
    assert str(path) in message
    assert FAKE_VALUE not in message
    assert stat.S_IMODE(path.stat().st_mode) & FORBIDDEN_FILE_BITS


def test_a_file_outside_the_configured_directory_is_refused(
    tmp_path: Path, credential_directory: Path
) -> None:
    outside = _write_file(tmp_path / "elsewhere", FAKE_VALUE, OWNER_ONLY_MODE)
    with pytest.raises(CredentialFileError) as caught:
        read_credential_file(outside, directory=credential_directory)
    message = str(caught.value)
    assert str(outside) in message
    assert str(credential_directory) in message
    assert FAKE_VALUE not in message


def test_a_file_that_does_not_exist_is_refused(credential_directory: Path) -> None:
    with pytest.raises(CredentialFileError) as caught:
        read_credential_file(credential_directory / "absent", directory=credential_directory)
    assert "does not exist" in str(caught.value)


@pytest.mark.parametrize("body", ["", "   ", "\n\n"])
def test_an_empty_file_is_refused(credential_directory: Path, body: str) -> None:
    path = _write_file(credential_directory / "empty", body, OWNER_ONLY_MODE)
    with pytest.raises(CredentialFileError) as caught:
        read_credential_file(path, directory=credential_directory)
    assert "empty" in str(caught.value)


def test_a_path_that_is_not_a_regular_file_is_refused(credential_directory: Path) -> None:
    nested = credential_directory / "nested"
    nested.mkdir(mode=0o700)
    with pytest.raises(CredentialFileError) as caught:
        read_credential_file(nested, directory=credential_directory)
    assert "not a regular file" in str(caught.value)


def test_a_file_of_invalid_text_is_refused(credential_directory: Path) -> None:
    path = credential_directory / "binary"
    path.write_bytes(b"\xff\xfe\x00")
    path.chmod(OWNER_ONLY_MODE)
    with pytest.raises(CredentialFileError) as caught:
        read_credential_file(path, directory=credential_directory)
    assert str(path) in str(caught.value)


# ---------------------------------------------------------------------------
# One retry on a transient failure, and no loop
# ---------------------------------------------------------------------------


def test_a_transient_failure_is_retried_exactly_once_and_then_succeeds() -> None:
    reader = _reader(failures=1, code=TRANSIENT_CODE)
    credential = get_parameter(PARAMETER_NAME, reader=reader)
    assert credential.reveal() == FAKE_VALUE
    assert credential.source is CredentialSource.PARAMETER
    assert reader.attempts == 2


def test_a_transient_failure_that_never_clears_stops_after_the_second_attempt() -> None:
    reader = _reader(failures=99, code=TRANSIENT_CODE)
    with pytest.raises(ParameterUnavailableError) as caught:
        get_parameter(PARAMETER_NAME, reader=reader)
    # Two attempts, not a loop: an unreachable parameter store is a startup
    # failure to report rather than a condition to wait out.
    assert reader.attempts == 2
    assert PARAMETER_NAME in str(caught.value)


def test_a_permanent_failure_is_not_retried() -> None:
    reader = _reader(failures=99, code=PERMANENT_CODE)
    with pytest.raises(ParameterUnavailableError) as caught:
        get_parameter(PARAMETER_NAME, reader=reader)
    assert reader.attempts == 1
    assert PARAMETER_NAME in str(caught.value)


def test_an_absent_parameter_is_reported_by_name_without_a_retry() -> None:
    reader = _reader(failures=99, code=MISSING_CODE)
    with pytest.raises(ParameterMissingError) as caught:
        get_parameter(PARAMETER_NAME, reader=reader)
    assert reader.attempts == 1
    assert caught.value.parameter_name == PARAMETER_NAME
    assert PARAMETER_NAME in str(caught.value)


def test_a_second_read_of_the_same_name_makes_no_further_call() -> None:
    reader = _reader()
    first = get_parameter(PARAMETER_NAME, reader=reader)
    second = get_parameter(PARAMETER_NAME, reader=reader)
    assert reader.attempts == 1
    assert first is second


def test_clearing_the_cache_makes_the_next_read_call_again() -> None:
    reader = _reader()
    get_parameter(PARAMETER_NAME, reader=reader)
    clear_parameter_cache()
    get_parameter(PARAMETER_NAME, reader=reader)
    assert reader.attempts == 2


def test_each_distinct_name_is_read_once_and_decryption_is_always_asked_for() -> None:
    reader = _reader()
    get_parameter(PARAMETER_NAME, reader=reader)
    get_parameter(OTHER_PARAMETER_NAME, reader=reader)
    get_parameter(PARAMETER_NAME, reader=reader)
    assert reader.names == [PARAMETER_NAME, OTHER_PARAMETER_NAME]
    assert reader.decryption == [True, True]


def test_a_response_carrying_no_value_is_reported_by_name() -> None:
    reader = _reader(value="")
    with pytest.raises(ParameterUnavailableError) as caught:
        get_parameter(PARAMETER_NAME, reader=reader)
    assert PARAMETER_NAME in str(caught.value)


# ---------------------------------------------------------------------------
# Choosing between a parameter name and a file path
# ---------------------------------------------------------------------------


def test_a_configured_parameter_name_wins_over_a_configured_file_path(
    credential_directory: Path,
) -> None:
    path = _write_file(credential_directory / "provider", FAKE_OTHER, OWNER_ONLY_MODE)
    configuration = Configuration(
        environ={
            "MOLT_TEXT_CREDENTIAL_PARAM": PARAMETER_NAME,
            "MOLT_TEXT_CREDENTIAL_FILE": str(path),
        },
        file_values={},
    )
    reader = _reader()
    credential = load_credential(
        configuration,
        parameter_env="MOLT_TEXT_CREDENTIAL_PARAM",
        file_env="MOLT_TEXT_CREDENTIAL_FILE",
        directory=credential_directory,
        reader=reader,
    )
    assert credential.source is CredentialSource.PARAMETER
    assert credential.reveal() == FAKE_VALUE
    assert reader.attempts == 1


def test_the_file_path_answers_when_no_parameter_name_is_configured(
    credential_directory: Path,
) -> None:
    path = _write_file(credential_directory / "provider", FAKE_OTHER, OWNER_ONLY_MODE)
    configuration = Configuration(environ={"MOLT_TEXT_CREDENTIAL_FILE": str(path)}, file_values={})
    credential = load_credential(
        configuration,
        parameter_env="MOLT_TEXT_CREDENTIAL_PARAM",
        file_env="MOLT_TEXT_CREDENTIAL_FILE",
        directory=credential_directory,
    )
    assert credential.source is CredentialSource.FILE
    assert credential.reveal() == FAKE_OTHER


def test_neither_source_configured_names_both_variables() -> None:
    configuration = Configuration(environ={}, file_values={})
    with pytest.raises(MissingConfigError) as caught:
        load_credential(
            configuration,
            parameter_env="MOLT_TEXT_CREDENTIAL_PARAM",
            file_env="MOLT_TEXT_CREDENTIAL_FILE",
        )
    message = str(caught.value)
    assert "MOLT_TEXT_CREDENTIAL_PARAM" in message
    assert "MOLT_TEXT_CREDENTIAL_FILE" in message
