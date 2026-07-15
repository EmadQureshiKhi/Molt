"""Unit tests for registry resolution and for the refusal of an unregistered name.

Selection is by name, and these tests pin down what that promise actually means.

**Every registered name resolves to the implementation it claims.** Each entry is
checked to name a module path rather than to hold an object, the builder loaded
for a name is checked to be the very attribute that module exposes, and both
delivered names are then driven all the way through selection so the object the
configuration surface produces is the one the registry names. That is the whole
switching-by-configuration story: if a name resolved to something else, changing
one configuration value would not change which model is called.

**An unregistered name is a configuration fault, not a provider fault.** The
refusal names the role, the offending value, and the registry keys, because the
operator's next action is to pick one of the keys. The class sits inside the
configuration hierarchy and deliberately outside the failure tree Molt raises for
its own decisions, so a handler catching Molt's tree cannot swallow a
misconfiguration, and it is refused before any implementation module is imported,
which is what keeps a name check runnable in a process holding no client library.

Every model identifier, credential value, and file path here is synthetic, and
the operator credential directory is created inside the temporary tree with the
working directory moved to it, so nothing reads the repository's own directory.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest

from molt.config.resolve import ConfigError, Configuration
from molt.errors import MoltError, UnknownProviderError
from molt.providers import SCHEMA_VECTOR_DIMENSIONS
from molt.providers.registry import (
    EMBEDDING_PROVIDERS,
    EMBEDDING_ROLE,
    TEXT_PROVIDERS,
    TEXT_ROLE,
    ProviderEntry,
    embedding_entry,
    embedding_provider_names,
    load_embedding_builder,
    load_text_builder,
    text_entry,
    text_provider_names,
)
from molt.providers.selector import (
    select_embedding_provider,
    select_text_provider,
    validate_selected_names,
)

# The two names the configuration surface accepts per role, each with the module
# it must resolve to. Restating the pairing here rather than reading it off the
# registry is the point: a test that asked the registry what it holds would agree
# with any answer at all.
EXPECTED_EMBEDDING_MODULES: Final[Mapping[str, str]] = {
    "bedrock": "molt.providers.bedrock",
    "external": "molt.providers.external_embedding",
}
EXPECTED_TEXT_MODULES: Final[Mapping[str, str]] = {
    "bedrock": "molt.providers.bedrock",
    "external": "molt.providers.external_text",
}

# Synthetic throughout. A model identifier is a configuration value, so no real
# identifier belongs in a tracked file.
FAKE_REGION: Final[str] = "fake-region"
FAKE_EMBEDDING_MODEL: Final[str] = "fake-embedding-model"
FAKE_TEXT_MODEL: Final[str] = "fake-text-model"
FAKE_CREDENTIAL: Final[str] = "fake-value-1"

# Names no registry holds, including the shapes a mistyped value actually takes.
UNREGISTERED_NAMES: Final[tuple[str, ...]] = ("", "bedroc", "Bedrock", " external", "openai")

# Owner-only, which is what the credential file accessor requires.
OWNER_ONLY_MODE: Final[int] = 0o600


@pytest.fixture
def credential_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An operator credential directory inside the temporary tree.

    The file accessor resolves a credential file against a directory named
    relative to the working directory, so the working directory is moved into the
    temporary tree rather than the repository's own directory being read.
    """
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".secrets"
    directory.mkdir(mode=0o700)
    return directory


def _credential_file(directory: Path, name: str) -> Path:
    """Write one owner-only credential file holding the synthetic value."""
    path = directory / name
    path.write_text(FAKE_CREDENTIAL, encoding="utf-8")
    path.chmod(OWNER_ONLY_MODE)
    return path


def _configuration(**environ: str) -> Configuration:
    """A resolved configuration over an explicit environment and no file values."""
    return Configuration(environ=environ, file_values={})


def _default_configuration() -> Configuration:
    """A configuration selecting the documented default name for both roles."""
    return _configuration(
        MOLT_EMBEDDING_PROVIDER="bedrock",
        MOLT_TEXT_PROVIDER="bedrock",
        MOLT_BEDROCK_REGION=FAKE_REGION,
        MOLT_EMBEDDING_MODEL_ID=FAKE_EMBEDDING_MODEL,
        MOLT_ADJUDICATION_MODEL_ID=FAKE_TEXT_MODEL,
    )


def _delivered_configuration(directory: Path) -> Configuration:
    """A configuration selecting the delivered name for both roles."""
    return _configuration(
        MOLT_EMBEDDING_PROVIDER="external",
        MOLT_TEXT_PROVIDER="external",
        MOLT_EMBEDDING_MODEL_ID=FAKE_EMBEDDING_MODEL,
        MOLT_ADJUDICATION_MODEL_ID=FAKE_TEXT_MODEL,
        MOLT_EMBEDDING_CREDENTIAL_FILE=str(_credential_file(directory, "embedding")),
        MOLT_TEXT_CREDENTIAL_FILE=str(_credential_file(directory, "text")),
    )


# --------------------------------------------------------------------------
# Each registered name resolves to its implementation
# --------------------------------------------------------------------------


def test_the_registered_names_are_the_names_the_configuration_surface_accepts() -> None:
    assert embedding_provider_names() == tuple(EXPECTED_EMBEDDING_MODULES)
    assert text_provider_names() == tuple(EXPECTED_TEXT_MODULES)


@pytest.mark.parametrize("name", tuple(EXPECTED_EMBEDDING_MODULES))
def test_each_embedding_name_names_the_module_and_the_builder_it_resolves_to(name: str) -> None:
    entry = embedding_entry(name)
    assert entry is EMBEDDING_PROVIDERS[name]
    assert entry.name == name
    assert entry.module == EXPECTED_EMBEDDING_MODULES[name]
    assert entry.attribute == "build"


@pytest.mark.parametrize("name", tuple(EXPECTED_TEXT_MODULES))
def test_each_text_name_names_the_module_and_the_builder_it_resolves_to(name: str) -> None:
    entry = text_entry(name)
    assert entry is TEXT_PROVIDERS[name]
    assert entry.name == name
    assert entry.module == EXPECTED_TEXT_MODULES[name]
    assert entry.attribute == "build"


@pytest.mark.parametrize("name", tuple(EXPECTED_EMBEDDING_MODULES))
def test_each_embedding_name_loads_the_builder_its_module_exposes(name: str) -> None:
    module = importlib.import_module(EXPECTED_EMBEDDING_MODULES[name])
    assert load_embedding_builder(name) is module.build


@pytest.mark.parametrize("name", tuple(EXPECTED_TEXT_MODULES))
def test_each_text_name_loads_the_builder_its_module_exposes(name: str) -> None:
    module = importlib.import_module(EXPECTED_TEXT_MODULES[name])
    assert load_text_builder(name) is module.build


def test_the_default_name_selects_the_default_implementation_for_both_roles() -> None:
    configuration = _default_configuration()
    embedding = select_embedding_provider(configuration)
    text = select_text_provider(configuration)
    assert embedding.name == "bedrock"
    assert embedding.model_id == FAKE_EMBEDDING_MODEL
    assert embedding.dimensions == SCHEMA_VECTOR_DIMENSIONS
    assert text.name == "bedrock"
    # One module answers for both roles here, and the identifier it reports is the
    # one for the role in play, which is why the text role's own identifier is
    # asserted through a configuration where this name serves that role alone.
    assert text.supports_prompt_cache is False


def test_the_default_name_reports_the_text_identifier_where_it_serves_that_role() -> None:
    configuration = _configuration(
        MOLT_EMBEDDING_PROVIDER="external",
        MOLT_TEXT_PROVIDER="bedrock",
        MOLT_BEDROCK_REGION=FAKE_REGION,
        MOLT_ADJUDICATION_MODEL_ID=FAKE_TEXT_MODEL,
    )
    text = select_text_provider(configuration)
    assert text.name == "bedrock"
    assert text.model_id == FAKE_TEXT_MODEL


def test_the_delivered_name_selects_the_delivered_implementation_for_both_roles(
    credential_directory: Path,
) -> None:
    configuration = _delivered_configuration(credential_directory)
    embedding = select_embedding_provider(configuration)
    text = select_text_provider(configuration)
    assert embedding.name == "external"
    assert embedding.model_id == FAKE_EMBEDDING_MODEL
    assert embedding.dimensions == SCHEMA_VECTOR_DIMENSIONS
    assert text.name == "external"
    assert text.model_id == FAKE_TEXT_MODEL
    assert text.supports_prompt_cache is True


def test_the_two_names_resolve_to_two_different_implementations() -> None:
    assert embedding_entry("bedrock").module != embedding_entry("external").module
    assert text_entry("bedrock").module != text_entry("external").module
    # One module answers for both roles under the default name, and two separate
    # modules answer under the delivered name, which is exactly why an entry is
    # held per role rather than per provider.
    assert embedding_entry("bedrock").module == text_entry("bedrock").module
    assert embedding_entry("external").module != text_entry("external").module


def test_validating_both_configured_names_answers_the_two_entries() -> None:
    embedding, text = validate_selected_names(_default_configuration())
    assert embedding is EMBEDDING_PROVIDERS["bedrock"]
    assert text is TEXT_PROVIDERS["bedrock"]


def test_no_name_can_be_added_to_either_registry_at_runtime() -> None:
    # A registry a caller could extend would make the registered set a runtime
    # question rather than a source one.
    assert isinstance(EMBEDDING_PROVIDERS, MappingProxyType)
    assert isinstance(TEXT_PROVIDERS, MappingProxyType)
    assert not hasattr(EMBEDDING_PROVIDERS, "__setitem__")
    assert not hasattr(TEXT_PROVIDERS, "__setitem__")


# --------------------------------------------------------------------------
# An unregistered name is a configuration fault
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", UNREGISTERED_NAMES)
def test_an_unregistered_embedding_name_names_the_role_the_value_and_the_keys(name: str) -> None:
    with pytest.raises(UnknownProviderError) as caught:
        embedding_entry(name)
    refusal = caught.value
    assert refusal.role == EMBEDDING_ROLE
    assert refusal.name == name
    assert refusal.registered == embedding_provider_names()
    message = str(refusal)
    assert EMBEDDING_ROLE in message
    assert repr(name) in message
    for registered in embedding_provider_names():
        assert registered in message


@pytest.mark.parametrize("name", UNREGISTERED_NAMES)
def test_an_unregistered_text_name_names_the_role_the_value_and_the_keys(name: str) -> None:
    with pytest.raises(UnknownProviderError) as caught:
        text_entry(name)
    refusal = caught.value
    assert refusal.role == TEXT_ROLE
    assert refusal.name == name
    assert refusal.registered == text_provider_names()
    message = str(refusal)
    assert TEXT_ROLE in message
    assert repr(name) in message
    for registered in text_provider_names():
        assert registered in message


def test_the_refusal_is_a_configuration_fault_outside_the_failure_tree() -> None:
    refusal = UnknownProviderError(EMBEDDING_ROLE, "openai", embedding_provider_names())
    assert isinstance(refusal, ConfigError)
    assert not isinstance(refusal, MoltError)
    assert issubclass(UnknownProviderError, ConfigError)
    assert not issubclass(UnknownProviderError, MoltError)


def test_selection_refuses_an_unregistered_name_for_either_role() -> None:
    embedding_unknown = _configuration(
        MOLT_EMBEDDING_PROVIDER="openai",
        MOLT_TEXT_PROVIDER="bedrock",
        MOLT_BEDROCK_REGION=FAKE_REGION,
    )
    with pytest.raises(UnknownProviderError) as embedding_caught:
        select_embedding_provider(embedding_unknown)
    assert embedding_caught.value.role == EMBEDDING_ROLE

    text_unknown = _configuration(
        MOLT_EMBEDDING_PROVIDER="bedrock",
        MOLT_TEXT_PROVIDER="openai",
        MOLT_BEDROCK_REGION=FAKE_REGION,
    )
    with pytest.raises(UnknownProviderError) as text_caught:
        select_text_provider(text_unknown)
    assert text_caught.value.role == TEXT_ROLE


def test_validating_the_names_refuses_the_embedding_name_before_the_text_name() -> None:
    both_unknown = _configuration(MOLT_EMBEDDING_PROVIDER="openai", MOLT_TEXT_PROVIDER="anthropic")
    with pytest.raises(UnknownProviderError) as caught:
        validate_selected_names(both_unknown)
    assert caught.value.role == EMBEDDING_ROLE
    assert caught.value.name == "openai"


def test_an_unregistered_name_is_refused_before_any_module_is_imported() -> None:
    before = set(sys.modules)
    with pytest.raises(UnknownProviderError):
        select_embedding_provider(_configuration(MOLT_EMBEDDING_PROVIDER="openai"))
    with pytest.raises(UnknownProviderError):
        select_text_provider(_configuration(MOLT_TEXT_PROVIDER="anthropic"))
    imported = {name for name in set(sys.modules) - before if name.startswith("molt.providers")}
    assert imported == set()


def test_an_entry_holds_a_module_path_rather_than_an_object() -> None:
    # Holding a path is what keeps resolution lazy, so a process selecting one
    # provider pays the import cost of no other.
    for entry in (*EMBEDDING_PROVIDERS.values(), *TEXT_PROVIDERS.values()):
        assert isinstance(entry, ProviderEntry)
        assert isinstance(entry.module, str)
        assert entry.module.startswith("molt.providers.")
