"""Unit tests for how a provider credential renders once the selector has loaded it.

A credential loaded for a provider role reaches three places that leave a trace: a
structured log record, an exception message, and an output stream. Each is driven
here for each role and for each of the two sources a credential may come from, and
each is asserted to carry the fixed placeholder and never the value.

The assertions are about rendering paths rather than about a value, because a value
escapes through a path rather than through a decision. A record written with the
credential as an ordinary field, a record written with it nested inside a
diagnostic mapping, an exception message built by interpolation, a traceback
representation, and a line printed to a stream are the paths that exist, so all of
them are exercised.

The role-aware loader is what is driven rather than the accessor beneath it, because
the mapping from a provider role to the pair of configuration keys its credential is
named by lives in the selector, and a role that resolved to the wrong pair would
load the wrong credential while still rendering perfectly safely.

Every value here is synthetic and shaped like nothing real, and the operator
credential directory is created inside the temporary tree with the working
directory moved to it, so nothing reads the repository's own directory.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

import pytest

from molt.config.resolve import Configuration
from molt.config.secrets import (
    CREDENTIAL_PLACEHOLDER,
    Credential,
    CredentialSource,
    clear_parameter_cache,
)
from molt.errors import ModelUnavailableError, ProviderError
from molt.providers.registry import EMBEDDING_ROLE, TEXT_ROLE
from molt.providers.selector import COMPONENT, load_credential
from molt.telemetry import Severity, Telemetry

# Obviously fake, short, and shaped like no real credential of any kind.
FAKE_EMBEDDING_VALUE: Final[str] = "fake-value-1"
FAKE_TEXT_VALUE: Final[str] = "fake-value-2"

# Parameter names the stub reader answers for. A name is not a secret.
EMBEDDING_PARAMETER: Final[str] = "/molt/test/embedding"
TEXT_PARAMETER: Final[str] = "/molt/test/text"

# The pair of configuration keys per role, which is the mapping under test.
ROLE_KEYS: Final[Mapping[str, tuple[str, str]]] = {
    EMBEDDING_ROLE: ("MOLT_EMBEDDING_CREDENTIAL_PARAM", "MOLT_EMBEDDING_CREDENTIAL_FILE"),
    TEXT_ROLE: ("MOLT_TEXT_CREDENTIAL_PARAM", "MOLT_TEXT_CREDENTIAL_FILE"),
}

ROLE_VALUES: Final[Mapping[str, str]] = {
    EMBEDDING_ROLE: FAKE_EMBEDDING_VALUE,
    TEXT_ROLE: FAKE_TEXT_VALUE,
}

ROLE_PARAMETERS: Final[Mapping[str, str]] = {
    EMBEDDING_ROLE: EMBEDDING_PARAMETER,
    TEXT_ROLE: TEXT_PARAMETER,
}

# Owner-only, which is what the credential file accessor requires.
OWNER_ONLY_MODE: Final[int] = 0o600

ROLES: Final[tuple[str, ...]] = (EMBEDDING_ROLE, TEXT_ROLE)


@pytest.fixture(autouse=True)
def _clean_parameter_cache() -> Iterator[None]:
    """Clear the process-lifetime parameter cache around every test."""
    clear_parameter_cache()
    yield
    clear_parameter_cache()


class StubReader:
    """A parameter reader answering one value per name and recording the names read."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self.names: list[str] = []

    def get_parameter(self, **kwargs: str | bool) -> Mapping[str, object]:
        """Answer the value scripted for a name, and record that the name was read."""
        name = str(kwargs.get("Name", ""))
        self.names.append(name)
        return {"Parameter": {"Name": name, "Value": self._values[name]}}


@pytest.fixture
def credential_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An operator credential directory inside the temporary tree."""
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".secrets"
    directory.mkdir(mode=0o700)
    return directory


def _credential_file(directory: Path, role: str) -> Path:
    """Write one owner-only credential file holding a role's synthetic value."""
    path = directory / role
    path.write_text(ROLE_VALUES[role], encoding="utf-8")
    path.chmod(OWNER_ONLY_MODE)
    return path


def _from_parameter(role: str) -> Credential:
    """Load a role's credential from a configured parameter name."""
    parameter_env, _ = ROLE_KEYS[role]
    configuration = Configuration(
        environ={parameter_env: ROLE_PARAMETERS[role]},
        file_values={},
    )
    reader = StubReader({ROLE_PARAMETERS[role]: ROLE_VALUES[role]})
    credential = load_credential(configuration, role, reader=reader)
    assert reader.names == [ROLE_PARAMETERS[role]]
    return credential


def _from_file(role: str, directory: Path) -> Credential:
    """Load a role's credential from a configured operator file path."""
    _, file_env = ROLE_KEYS[role]
    path = _credential_file(directory, role)
    configuration = Configuration(environ={file_env: str(path)}, file_values={})
    return load_credential(configuration, role, directory=directory)


# --------------------------------------------------------------------------
# The loader reads the pair of keys its role names
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_each_role_loads_from_its_own_parameter_name(role: str) -> None:
    credential = _from_parameter(role)
    assert credential.source is CredentialSource.PARAMETER
    assert credential.source_name == ROLE_PARAMETERS[role]
    assert credential.reveal() == ROLE_VALUES[role]


@pytest.mark.parametrize("role", ROLES)
def test_each_role_loads_from_its_own_operator_file(role: str, credential_directory: Path) -> None:
    credential = _from_file(role, credential_directory)
    assert credential.source is CredentialSource.FILE
    assert credential.reveal() == ROLE_VALUES[role]


def test_a_role_carrying_no_credential_is_refused_naming_the_roles() -> None:
    with pytest.raises(ProviderError) as caught:
        load_credential(Configuration(environ={}, file_values={}), "adjudicator")
    message = str(caught.value)
    assert "adjudicator" in message
    for role in ROLES:
        assert role in message


# --------------------------------------------------------------------------
# A log record carries the placeholder
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_a_log_record_built_from_a_loaded_credential_carries_the_placeholder(role: str) -> None:
    credential = _from_parameter(role)
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.DEBUG, stream=sink)

    telemetry.log(
        Severity.INFO,
        COMPONENT,
        f"a credential was loaded for the {role} role",
        source_name=credential.source_name,
        detail=credential,
        nested={"held": credential},
        listed=[credential],
    )

    line = sink.getvalue().strip()
    assert ROLE_VALUES[role] not in line
    record: dict[str, object] = json.loads(line)
    assert record["component"] == COMPONENT
    assert record["source_name"] == ROLE_PARAMETERS[role]
    assert record["detail"] == CREDENTIAL_PLACEHOLDER
    assert record["nested"] == {"held": CREDENTIAL_PLACEHOLDER}
    assert record["listed"] == [CREDENTIAL_PLACEHOLDER]


@pytest.mark.parametrize("role", ROLES)
def test_a_record_message_interpolating_a_loaded_credential_carries_the_placeholder(
    role: str,
    credential_directory: Path,
) -> None:
    credential = _from_file(role, credential_directory)
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.DEBUG, stream=sink)

    telemetry.log(Severity.WARNING, COMPONENT, f"the provider refused {credential}")

    line = sink.getvalue().strip()
    assert ROLE_VALUES[role] not in line
    record: dict[str, object] = json.loads(line)
    assert record["message"] == f"the provider refused {CREDENTIAL_PLACEHOLDER}"


# --------------------------------------------------------------------------
# An exception message carries the placeholder
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_a_provider_fault_built_from_a_loaded_credential_carries_the_placeholder(
    role: str,
) -> None:
    credential = _from_parameter(role)
    with pytest.raises(ModelUnavailableError) as caught:
        raise ModelUnavailableError(f"the model refused the call made with {credential}")
    assert str(caught.value) == f"the model refused the call made with {CREDENTIAL_PLACEHOLDER}"
    assert ROLE_VALUES[role] not in str(caught.value)
    assert ROLE_VALUES[role] not in repr(caught.value)


@pytest.mark.parametrize("role", ROLES)
def test_a_fault_carrying_the_credential_as_an_argument_still_renders_the_placeholder(
    role: str,
) -> None:
    credential = _from_parameter(role)
    fault = ProviderError("the call was refused", credential)
    assert ROLE_VALUES[role] not in str(fault)
    assert ROLE_VALUES[role] not in repr(fault)
    assert CREDENTIAL_PLACEHOLDER in str(fault)


# --------------------------------------------------------------------------
# An output stream carries the placeholder
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_an_output_stream_written_with_a_loaded_credential_carries_the_placeholder(
    role: str,
    credential_directory: Path,
) -> None:
    credential = _from_file(role, credential_directory)
    stream = io.StringIO()

    print(credential, file=stream)
    print(f"the {role} credential is {credential}", file=stream)
    print(f"{credential!r}", file=stream)
    print(f"{credential:>40}", file=stream)

    written = stream.getvalue()
    assert ROLE_VALUES[role] not in written
    assert written.count(CREDENTIAL_PLACEHOLDER) == 4


@pytest.mark.parametrize("role", ROLES)
def test_a_loaded_credential_renders_the_placeholder_through_every_conversion(role: str) -> None:
    credential = _from_parameter(role)
    renderings = (
        str(credential),
        repr(credential),
        format(credential),
        format(credential, ">40"),
        f"{credential}",
        f"{credential!s}",
        f"{credential!r}",
        str([credential]),
        repr({"held": credential}),
    )
    for rendering in renderings:
        assert CREDENTIAL_PLACEHOLDER in rendering
        assert ROLE_VALUES[role] not in rendering
