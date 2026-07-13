"""Unit tests for configuration resolution: precedence, naming, and no secret default.

Three obligations are checked here and each is checked in the way an operator
would feel it.

Precedence is asserted through the reported source as well as the resolved
value, for one representative key of every kind. A test that only compared
values would pass against an implementation that read the wrong source and
happened to find the same text there, so each case supplies three different
values and asserts which source answered.

The missing-value error is asserted to name both the environment variable and
the configuration file key, because those two names are the whole remedy an
operator has.

The absence of a secret default is asserted over the real surface rather than
over a fixture, so the invariant is a property of what ships. Every key naming
where a credential lives must carry no default, and the shape describing a
secret accepted from the environment alone must carry no default field at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from molt.config.resolve import (
    CREDENTIAL_MARKERS,
    SECRET_SETTINGS,
    SECTION_NAMES,
    SETTINGS,
    ConfigError,
    Configuration,
    ConfigValue,
    InvalidConfigValueError,
    Kind,
    MissingConfigError,
    SecretSetting,
    Source,
    UnknownSettingError,
    load_config_file,
    load_configuration,
)
from molt.models.event import JsonValue

# A required key carrying no default, used wherever absence is the subject.
REQUIRED_ENV: Final[str] = "MOLT_COLLECTOR_URL"
REQUIRED_KEY: Final[str] = "collector.url"


@dataclass(frozen=True, slots=True)
class PrecedenceCase:
    """One key of one kind, with a distinct value available from each source."""

    env: str
    kind: Kind
    env_text: str
    from_environment: ConfigValue
    file_value: JsonValue
    from_file: ConfigValue
    from_default: ConfigValue


# One representative key per kind. Each case supplies an environment value, a
# file value, and a built-in default that are all different, so a resolution
# reading the wrong source cannot coincidentally produce the right answer.
PRECEDENCE_CASES: Final[tuple[PrecedenceCase, ...]] = (
    PrecedenceCase(
        env="MOLT_DB_ROLE",
        kind=Kind.TEXT,
        env_text="eraser",
        from_environment="eraser",
        file_value="reader",
        from_file="reader",
        from_default="writer",
    ),
    PrecedenceCase(
        env="MOLT_INTERFACE_SPEC_PATH",
        kind=Kind.PATH,
        env_text="from-environment/interface.json",
        from_environment="from-environment/interface.json",
        file_value="from-file/interface.json",
        from_file="from-file/interface.json",
        from_default="docs/interface.json",
    ),
    PrecedenceCase(
        env="MOLT_DB_MAX_RETRIES",
        kind=Kind.INTEGER,
        env_text="9",
        from_environment=9,
        file_value=7,
        from_file=7,
        from_default=5,
    ),
    PrecedenceCase(
        env="MOLT_REVIEW_THRESHOLD",
        kind=Kind.NUMBER,
        env_text="0.60",
        from_environment=0.60,
        file_value=0.55,
        from_file=0.55,
        from_default=0.45,
    ),
    PrecedenceCase(
        env="MOLT_DEMO_MODE",
        kind=Kind.FLAG,
        env_text="true",
        from_environment=True,
        file_value=False,
        from_file=False,
        from_default=False,
    ),
    PrecedenceCase(
        env="MOLT_SENSITIVE_PATHS",
        kind=Kind.TEXT_LIST,
        env_text="/from/environment/, /also/environment/",
        from_environment=("/from/environment/", "/also/environment/"),
        file_value=["/from/file/"],
        from_file=("/from/file/",),
        from_default=("/etc/", "~/.ssh/", ".env"),
    ),
    PrecedenceCase(
        env="MOLT_SENSITIVITY_AUTO_THRESHOLDS",
        kind=Kind.NUMBER_LIST,
        env_text="0.11,0.22",
        from_environment=(0.11, 0.22),
        file_value=[0.33, 0.44],
        from_file=(0.33, 0.44),
        from_default=(0.10, 0.15, 0.20, 0.25, 0.30),
    ),
)

CASE_IDS: Final[tuple[str, ...]] = tuple(str(case.kind) for case in PRECEDENCE_CASES)


def _settings_by_env() -> dict[str, Kind]:
    """The declared kind of every key, so a case cannot name the wrong kind."""
    return {setting.env: setting.kind for setting in SETTINGS}


@pytest.mark.parametrize("case", PRECEDENCE_CASES, ids=CASE_IDS)
def test_every_case_names_the_kind_its_key_declares(case: PrecedenceCase) -> None:
    assert _settings_by_env()[case.env] is case.kind


@pytest.mark.parametrize("case", PRECEDENCE_CASES, ids=CASE_IDS)
def test_environment_wins_over_file_and_default(case: PrecedenceCase) -> None:
    configuration = Configuration(
        environ={case.env: case.env_text},
        file_values={_key_for(case.env): case.file_value},
    )
    assert configuration.source(case.env) is Source.ENVIRONMENT
    assert configuration.value(case.env) == case.from_environment


@pytest.mark.parametrize("case", PRECEDENCE_CASES, ids=CASE_IDS)
def test_file_wins_over_default_when_the_environment_is_unset(case: PrecedenceCase) -> None:
    configuration = Configuration(environ={}, file_values={_key_for(case.env): case.file_value})
    assert configuration.source(case.env) is Source.FILE
    assert configuration.value(case.env) == case.from_file


@pytest.mark.parametrize("case", PRECEDENCE_CASES, ids=CASE_IDS)
def test_default_answers_when_neither_other_source_does(case: PrecedenceCase) -> None:
    configuration = Configuration(environ={}, file_values={})
    assert configuration.source(case.env) is Source.DEFAULT
    assert configuration.value(case.env) == case.from_default


@pytest.mark.parametrize("case", PRECEDENCE_CASES, ids=CASE_IDS)
def test_an_environment_value_of_only_whitespace_does_not_answer(case: PrecedenceCase) -> None:
    # A variable exported empty is a variable the operator did not set, so it
    # must not shadow the file value that follows it.
    configuration = Configuration(
        environ={case.env: "   "},
        file_values={_key_for(case.env): case.file_value},
    )
    assert configuration.source(case.env) is Source.FILE
    assert configuration.value(case.env) == case.from_file


def _key_for(env: str) -> str:
    """The configuration file key belonging to an environment variable."""
    for setting in SETTINGS:
        if setting.env == env:
            return setting.key
    raise AssertionError(f"{env} is not part of the configuration surface")


def test_typed_accessors_return_the_declared_kind() -> None:
    configuration = Configuration(environ={}, file_values={})
    assert configuration.text("MOLT_DB_ROLE") == "writer"
    assert configuration.integer("MOLT_DB_MAX_RETRIES") == 5
    assert configuration.number("MOLT_REVIEW_THRESHOLD") == pytest.approx(0.45)
    assert configuration.flag("MOLT_DEMO_MODE") is False
    assert configuration.text_list("MOLT_SENSITIVE_PATHS") == ("/etc/", "~/.ssh/", ".env")
    assert configuration.number_list("MOLT_SENSITIVITY_AUTO_THRESHOLDS")[0] == pytest.approx(0.10)
    assert configuration.path("MOLT_INTERFACE_SPEC_PATH") == Path("docs/interface.json")


def test_a_home_reference_in_a_path_is_expanded() -> None:
    configuration = Configuration(environ={"MOLT_SPOOL_DIR": "~/spool"}, file_values={})
    resolved = configuration.path("MOLT_SPOOL_DIR")
    assert resolved.is_absolute()
    assert not str(resolved).startswith("~")


# ---------------------------------------------------------------------------
# The missing-value error names the key
# ---------------------------------------------------------------------------


def test_missing_value_error_names_both_the_variable_and_the_configuration_key() -> None:
    configuration = Configuration(environ={}, file_values={})
    with pytest.raises(MissingConfigError) as caught:
        configuration.value(REQUIRED_ENV)
    error = caught.value
    assert error.env == REQUIRED_ENV
    assert error.key == REQUIRED_KEY
    message = str(error)
    assert REQUIRED_ENV in message
    assert REQUIRED_KEY in message


def test_every_required_key_reports_its_own_two_names_when_absent() -> None:
    configuration = Configuration(environ={}, file_values={})
    required = [setting for setting in SETTINGS if setting.default is None]
    assert required
    for setting in required:
        with pytest.raises(MissingConfigError) as caught:
            configuration.value(setting.env)
        assert setting.env in str(caught.value)
        assert setting.key in str(caught.value)


def test_optional_reports_absence_rather_than_raising() -> None:
    configuration = Configuration(environ={}, file_values={})
    assert configuration.optional(REQUIRED_ENV) is None
    assert configuration.optional_text(REQUIRED_ENV) is None
    assert configuration.optional_path("MOLT_CLIENT_MAP") is None


def test_an_unknown_name_is_refused_and_named() -> None:
    configuration = Configuration(environ={}, file_values={})
    with pytest.raises(UnknownSettingError) as caught:
        configuration.value("MOLT_NOT_A_KEY")
    assert "MOLT_NOT_A_KEY" in str(caught.value)


def test_a_value_of_the_wrong_kind_is_refused_naming_the_variable() -> None:
    from_environment = Configuration(environ={"MOLT_DB_MAX_RETRIES": "many"}, file_values={})
    with pytest.raises(InvalidConfigValueError) as caught:
        from_environment.integer("MOLT_DB_MAX_RETRIES")
    assert "MOLT_DB_MAX_RETRIES" in str(caught.value)

    from_file = Configuration(environ={}, file_values={"store.max_retries": "many"})
    with pytest.raises(InvalidConfigValueError) as caught:
        from_file.integer("MOLT_DB_MAX_RETRIES")
    assert "MOLT_DB_MAX_RETRIES" in str(caught.value)


def test_a_configuration_file_key_the_surface_omits_is_refused_by_name(tmp_path: Path) -> None:
    document = tmp_path / "config.toml"
    document.write_text('[store]\nnot_a_key = "x"\n', encoding="utf-8")
    with pytest.raises(UnknownSettingError) as caught:
        load_config_file(document)
    assert "store.not_a_key" in str(caught.value)


def test_a_configuration_file_is_read_into_dotted_keys(tmp_path: Path) -> None:
    document = tmp_path / "config.toml"
    document.write_text('[store]\nrole = "reader"\nmax_retries = 4\n', encoding="utf-8")
    configuration = load_configuration(config_path=document, environ={})
    assert configuration.config_path == document
    assert configuration.source("MOLT_DB_ROLE") is Source.FILE
    assert configuration.text("MOLT_DB_ROLE") == "reader"
    assert configuration.integer("MOLT_DB_MAX_RETRIES") == 4


def test_an_unreadable_configuration_file_is_reported_by_path(tmp_path: Path) -> None:
    absent = tmp_path / "absent.toml"
    with pytest.raises(ConfigError) as caught:
        load_config_file(absent)
    assert str(absent) in str(caught.value)


# ---------------------------------------------------------------------------
# No secret carries a default
# ---------------------------------------------------------------------------


def test_no_credential_bearing_key_of_the_shipped_surface_carries_a_default() -> None:
    credential_bearing = [
        setting
        for setting in SETTINGS
        if any(marker in f"{setting.env} {setting.key}".lower() for marker in CREDENTIAL_MARKERS)
    ]
    # The surface really does name places where credentials live, so the
    # invariant below is asserted over a non-empty set.
    assert credential_bearing
    for setting in credential_bearing:
        assert setting.default is None, setting.key


def test_the_secret_shape_carries_no_default_field_and_no_file_key() -> None:
    field_names = set(SecretSetting.__dataclass_fields__)
    assert field_names == {"env", "purpose"}
    assert "default" not in field_names
    assert "key" not in field_names
    for secret in SECRET_SETTINGS:
        assert not hasattr(secret, "default")
        assert secret.purpose


def test_the_three_environment_only_secrets_are_exactly_the_expected_names() -> None:
    assert {secret.env for secret in SECRET_SETTINGS} == {
        "MOLT_COLLECTOR_TOKEN",
        "MOLT_INGRESS_SECRET",
        "MOLT_DSN",
    }


def test_a_secret_has_no_place_in_the_ordinary_surface() -> None:
    surface_names = {setting.env for setting in SETTINGS}
    for secret in SECRET_SETTINGS:
        assert secret.env not in surface_names


def test_resolving_a_secret_through_the_ordinary_path_is_refused() -> None:
    configuration = Configuration(environ={"MOLT_DSN": "fake-dsn-1"}, file_values={})
    for secret in SECRET_SETTINGS:
        with pytest.raises(UnknownSettingError) as caught:
            configuration.resolve(secret.env)
        assert secret.env in str(caught.value)


def test_a_secret_is_read_as_raw_environment_text_and_absence_reads_as_nothing() -> None:
    present = Configuration(environ={"MOLT_INGRESS_SECRET": "fake-key-1"}, file_values={})
    assert present.environment_value("MOLT_INGRESS_SECRET") == "fake-key-1"
    absent = Configuration(environ={}, file_values={})
    assert absent.environment_value("MOLT_INGRESS_SECRET") is None
    blank = Configuration(environ={"MOLT_INGRESS_SECRET": "  "}, file_values={})
    assert blank.environment_value("MOLT_INGRESS_SECRET") is None


def test_a_deployment_flag_is_read_outside_the_surface() -> None:
    configuration = Configuration(environ={"MOLT_ENV": "production"}, file_values={})
    assert configuration.deployment_flag("MOLT_ENV") == "production"
    assert configuration.deployment_flag("MOLT_ENV_NOT_SET") is None


# ---------------------------------------------------------------------------
# Shape of the surface itself
# ---------------------------------------------------------------------------


def test_the_surface_declares_each_variable_and_each_key_once() -> None:
    assert len({setting.env for setting in SETTINGS}) == len(SETTINGS)
    assert len({setting.key for setting in SETTINGS}) == len(SETTINGS)


def test_every_key_sits_in_a_declared_section() -> None:
    assert SECTION_NAMES
    for setting in SETTINGS:
        section, _, remainder = setting.key.partition(".")
        assert section in SECTION_NAMES
        assert remainder
    assert len(set(SECTION_NAMES)) == len(SECTION_NAMES)


def test_the_settings_table_and_the_secret_table_are_immutable_tuples() -> None:
    assert isinstance(SETTINGS, tuple)
    assert isinstance(SECRET_SETTINGS, tuple)
    for setting in SETTINGS:
        assert not hasattr(setting, "__dict__")
        with pytest.raises(AttributeError):
            setting.__setattr__("default", "changed")


def test_a_configuration_holds_its_sources_rather_than_reading_them_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = Configuration(environ={"MOLT_DB_ROLE": "eraser"}, file_values={})
    monkeypatch.setenv("MOLT_DB_ROLE", "reader")
    # The view was built over the mapping it was given, so a later change to the
    # real environment cannot move a value out from under a running process.
    assert configuration.text("MOLT_DB_ROLE") == "eraser"
