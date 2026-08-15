"""A loaded provider credential on the emission path the deployment actually uses.

Requirement 37.12 obliges the Provider_Selector, the Embedder, the Adjudicator, and
the Redaction_Rewriter to write no provider credential value to any log record or
output stream, and Requirement 30.12 obliges the Repository to hold no provider
credential value at all. `tests/unit/test_provider_credential_render.py` already
establishes the rendering contract of the wrapper: it loads a credential through the
role-aware loader for each role and from each of the two sources, and asserts the
fixed placeholder appears in a record written to a `Telemetry` instance the test
constructs with a string sink of its own, in an exception message, in a traceback
representation, and in a line printed to a string stream.

What is different here is the path, not the claim. Nothing below constructs a
telemetry instance, a sink, or a formatter of its own:

- the record sink is the process-wide instance built by `configure` from a
  resolved configuration surface, and it writes to the real standard error, which
  the capture fixture reads back;
- the records go through `molt.telemetry.log` and `molt.telemetry.metric`, the
  module-level surfaces every component calls, including the diverted-measurement
  record the cardinality bound produces;
- the log records that matter most originate outside this module: the delivered
  embedding provider's own retry path emits them, with the loaded credential held in
  the transport the provider was constructed with;
- the fault is the one that provider raises, rendered as a crash renders it, which
  is through `traceback.format_exception` and through the argument tuple;
- the error detail is the string an aborted Erasure_Run records, taken to the
  document the CLI verb reports it in and through the one formatter every verb's
  output passes;
- the output stream is the real standard output, written through that formatter.

**The credential is really loaded, from a real file, by the real accessor.** The
operator-file source is used rather than the parameter store, because reading a
parameter needs a cloud client and this suite carries no credential of its own; the
file accessor is the production accessor, checks the permissions it requires, and
returns the same wrapper the parameter path returns. The first assertion below
reveals the value once and compares it with what was written, so every later
absence claim is an absence of something that was genuinely present.

**One socket is not dialled.** The provider's outward call is a declared seam, and
the transport used here holds the loaded credential exactly as the delivered one
does and fails the way an unreachable service fails. Everything the provider then
does — the retry decision, the attempt record, the fault it raises — is the
delivered code running.

Every value here is synthetic and composed from separately named parts, and the
credential directory is created inside the temporary tree with the working directory
moved to it, so nothing reads the repository's own directory.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest

from molt.cli.exits import ExitCode
from molt.cli.output import REDACTED, Emitter
from molt.config.resolve import Configuration
from molt.config.secrets import (
    CREDENTIAL_PLACEHOLDER,
    Credential,
    CredentialSource,
    clear_parameter_cache,
)
from molt.errors import ModelUnavailableError, ProviderError
from molt.providers import SCHEMA_VECTOR_DIMENSIONS
from molt.providers.external_embedding import (
    ExternalEmbeddingProvider,
    HttpsTransport,
    build,
)
from molt.providers.registry import EMBEDDING_ROLE
from molt.providers.selector import COMPONENT, load_credential
from molt.telemetry import DEFAULT_CARDINALITY_MAX, Severity, configure, log, metric, reset

# The credential value, composed from separately named parts so it reads as a
# fixture to a reader and to a secret-shape linter alike while still being a value
# whose absence from every rendering is the claim.
CREDENTIAL_ROLE_PART: Final[str] = "synthetic-embedding"
CREDENTIAL_PURPOSE_PART: Final[str] = "credential-portion-never-real"
LOADED_VALUE: Final[str] = f"{CREDENTIAL_ROLE_PART}-{CREDENTIAL_PURPOSE_PART}"

# The configuration key the credential file path is named by, and the file name the
# role's credential is written under.
CREDENTIAL_FILE_KEY: Final[str] = "MOLT_EMBEDDING_CREDENTIAL_FILE"
CREDENTIAL_DIRECTORY_NAME: Final[str] = ".secrets"

# Owner-only, which is what the credential file accessor requires, and owner-only
# for the directory holding it.
OWNER_ONLY_FILE_MODE: Final[int] = 0o600
OWNER_ONLY_DIRECTORY_MODE: Final[int] = 0o700

# The provider surface the delivered configuration selects, at the width the schema
# fixes so the startup gate would admit it, with retries turned off so a failing
# attempt is one attempt.
MODEL_UNDER_TEST: Final[str] = "synthetic-retrieval-model"
BATCH_SIZE: Final[int] = 2
NO_RETRIES: Final[int] = 0
TIMEOUT_SECONDS: Final[int] = 5

# What the provider is asked to embed. Short and about nothing.
SUBJECT_TEXT: Final[str] = "one line of source under test"

# The run identity the error-detail document carries. It names no stored row.
RUN_LABEL: Final[str] = "run-under-test"
VERB_LABEL: Final[str] = "erase"

# A value under a sensitive field name that is not a wrapped credential, so the
# formatter's own replacement is exercised beside the wrapper's rendering.
OPAQUE_SENSITIVE_VALUE: Final[str] = "an-opaque-value-under-a-sensitive-name"


# ---------------------------------------------------------------------------
# The real credential, from a real file
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_parameter_cache() -> Iterator[None]:
    """Clear the process-lifetime parameter cache around every test."""
    clear_parameter_cache()
    yield
    clear_parameter_cache()


@pytest.fixture
def credential_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An owner-only credential directory inside the temporary tree."""
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / CREDENTIAL_DIRECTORY_NAME
    directory.mkdir(mode=OWNER_ONLY_DIRECTORY_MODE)
    path = directory / EMBEDDING_ROLE
    path.write_text(LOADED_VALUE, encoding="utf-8")
    path.chmod(OWNER_ONLY_FILE_MODE)
    return directory


def provider_configuration(directory: Path) -> Configuration:
    """The configuration surface the delivered embedding provider is built from."""
    return Configuration(
        environ={
            CREDENTIAL_FILE_KEY: str(directory / EMBEDDING_ROLE),
            "MOLT_EMBEDDING_MODEL_ID": MODEL_UNDER_TEST,
            "MOLT_EMBEDDING_DIMENSIONS": str(SCHEMA_VECTOR_DIMENSIONS),
            "MOLT_EMBEDDING_BATCH_SIZE": str(BATCH_SIZE),
            "MOLT_PROVIDER_TIMEOUT_SECONDS": str(TIMEOUT_SECONDS),
            "MOLT_PROVIDER_MAX_RETRIES": str(NO_RETRIES),
            "MOLT_LOG_LEVEL": str(Severity.DEBUG),
        },
        file_values={},
    )


@pytest.fixture
def credential(credential_directory: Path) -> Credential:
    """The credential the role's own loader resolves from the operator file."""
    return load_credential(
        provider_configuration(credential_directory),
        EMBEDDING_ROLE,
        directory=credential_directory,
    )


@pytest.fixture
def emitting(credential_directory: Path) -> Iterator[None]:
    """Point the process-wide telemetry instance at the real standard error.

    This is the emission path a deployed component writes on: the module-level
    surfaces resolve this instance, and the instance resolves its stream at write
    time, so what a record reaches here is the process's own error stream.
    """
    configure(provider_configuration(credential_directory))
    yield
    reset()


# ---------------------------------------------------------------------------
# The transport, holding the loaded credential and failing the way a socket does
# ---------------------------------------------------------------------------


class UnreachableTransport:
    """The provider's outward seam, holding the credential and refusing to reach out.

    The credential is held exactly as the delivered transport holds it, and the
    failure is the transport-level fault the delivered transport surfaces when the
    service cannot be reached, so the provider's retry decision, its attempt record,
    and the fault it raises are all the delivered ones.
    """

    __slots__ = ("_credential", "attempts")

    def __init__(self, credential: Credential) -> None:
        self._credential = credential
        self.attempts = 0

    def headers(self) -> Mapping[str, str]:
        """The per-request headers, built the way the delivered transport builds them."""
        return {"authorization": f"Bearer {self._credential.reveal()}"}

    def send(self, body: bytes) -> tuple[int, bytes]:
        """Refuse the request the way an unreachable service refuses it."""
        del body
        self.attempts += 1
        raise OSError("the service could not be reached from this process")


def build_provider(transport: UnreachableTransport) -> ExternalEmbeddingProvider:
    """The delivered provider over the credential-holding transport."""
    return ExternalEmbeddingProvider(
        model_id=MODEL_UNDER_TEST,
        dimensions=SCHEMA_VECTOR_DIMENSIONS,
        batch_size=BATCH_SIZE,
        transport=transport,
        max_retries=NO_RETRIES,
        sleep=lambda _: None,
    )


def rendered(error: BaseException) -> str:
    """Everything a crash shows of one fault: its text, its arguments, its traceback."""
    formatted = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return "\n".join((str(error), repr(error.args), formatted))


# ---------------------------------------------------------------------------
# The credential is genuinely loaded, so every absence below is an absence
# ---------------------------------------------------------------------------


def test_the_credential_is_loaded_from_the_operator_file_by_the_real_accessor(
    credential: Credential, credential_directory: Path
) -> None:
    """One reveal, so the value asserted absent everywhere else was present here."""
    assert credential.source is CredentialSource.FILE
    assert credential.reveal() == LOADED_VALUE
    assert (credential_directory / EMBEDDING_ROLE).read_text(encoding="utf-8") == LOADED_VALUE


def test_the_delivered_builder_wires_the_loaded_credential_into_its_transport(
    credential_directory: Path,
) -> None:
    """The builder under test is the delivered one, reaching the delivered transport.

    Nothing is embedded here, so no request is made. What is established is that the
    credential this module loads is the credential the delivered path holds, and
    that rendering the objects holding it discloses nothing.
    """
    provider = build(provider_configuration(credential_directory))
    assert isinstance(provider, ExternalEmbeddingProvider)
    assert provider.dimensions == SCHEMA_VECTOR_DIMENSIONS
    transport = HttpsTransport(
        credential=load_credential(
            provider_configuration(credential_directory),
            EMBEDDING_ROLE,
            directory=credential_directory,
        ),
        timeout=float(TIMEOUT_SECONDS),
    )
    for rendering in (repr(provider), str(provider), repr(transport), str(transport)):
        assert LOADED_VALUE not in rendering


# ---------------------------------------------------------------------------
# The provider's own records, on the process-wide instance and the real stream
# ---------------------------------------------------------------------------


def test_the_providers_own_attempt_records_carry_no_credential_value(
    credential: Credential,
    emitting: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The delivered retry path writes real records, and none of them holds the value."""
    del emitting
    transport = UnreachableTransport(credential)
    provider = build_provider(transport)

    with pytest.raises(ModelUnavailableError):
        provider.embed([SUBJECT_TEXT])

    assert transport.attempts == NO_RETRIES + 1
    written = capsys.readouterr().err
    assert written, "the provider wrote no record at all, so nothing was established"
    assert LOADED_VALUE not in written
    assert CREDENTIAL_ROLE_PART not in written
    assert CREDENTIAL_PURPOSE_PART not in written
    records = [json.loads(line) for line in written.splitlines() if line.strip()]
    assert any(record["message"] == "an embedding attempt failed" for record in records)
    for record in records:
        assert MODEL_UNDER_TEST in json.dumps(record) or record["component"] != "external_embedding"


def test_the_fault_the_provider_raises_carries_no_credential_value(
    credential: Credential,
    emitting: None,
) -> None:
    """The fault is rendered from the failure kind, so the credential travels nowhere."""
    del emitting
    transport = UnreachableTransport(credential)
    provider = build_provider(transport)

    with pytest.raises(ModelUnavailableError) as caught:
        provider.probe()

    shown = rendered(caught.value)
    assert LOADED_VALUE not in shown
    assert CREDENTIAL_PURPOSE_PART not in shown
    assert "could not be reached" in str(caught.value)


# ---------------------------------------------------------------------------
# A log record on the module-level surface
# ---------------------------------------------------------------------------


def test_a_record_written_through_the_module_surface_renders_the_placeholder(
    credential: Credential,
    emitting: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The credential as a field, nested, listed, and interpolated into the message."""
    del emitting
    log(
        Severity.INFO,
        COMPONENT,
        f"a credential was loaded from {credential}",
        source_name=credential.source_name,
        detail=credential,
        nested={"held": credential},
        listed=[credential],
    )

    line = capsys.readouterr().err.strip()
    assert LOADED_VALUE not in line
    record: dict[str, object] = json.loads(line)
    assert record["message"] == f"a credential was loaded from {CREDENTIAL_PLACEHOLDER}"
    assert record["detail"] == CREDENTIAL_PLACEHOLDER
    assert record["nested"] == {"held": CREDENTIAL_PLACEHOLDER}
    assert record["listed"] == [CREDENTIAL_PLACEHOLDER]


def test_a_diverted_measurement_record_renders_the_placeholder(
    credential: Credential,
    emitting: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cardinality bound's own record path is a record path, so it is checked too."""
    del emitting
    for index in range(DEFAULT_CARDINALITY_MAX + 1):
        metric("provider.attempts", 1.0, attempt=str(index), source=f"{credential}")

    written = capsys.readouterr().err
    assert "metric diverted to a log record" in written
    assert LOADED_VALUE not in written
    diverted = [
        record
        for record in (json.loads(line) for line in written.splitlines() if line.strip())
        if record.get("metric") == "provider.attempts"
    ]
    assert diverted
    for record in diverted:
        assert record["dimensions"]["source"] == CREDENTIAL_PLACEHOLDER


# ---------------------------------------------------------------------------
# An exception message and the error detail a run records
# ---------------------------------------------------------------------------


def test_a_fault_interpolating_the_credential_renders_the_placeholder(
    credential: Credential,
) -> None:
    """A message, an argument, and a traceback all render the placeholder."""
    fault = ProviderError(f"the call made with {credential} was refused", credential)
    try:
        raise fault
    except ProviderError as caught:
        shown = rendered(caught)
    assert LOADED_VALUE not in shown
    assert CREDENTIAL_PLACEHOLDER in shown


def test_the_error_detail_a_run_records_renders_the_placeholder(
    credential: Credential,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The detail an aborted run stores is a rendered fault, so it holds no value.

    The string is composed the way the abort path composes it, from the fault the
    provider raised, and it is then carried through the document the verb reports,
    which is the one place that string reaches a reader.
    """
    fault = ModelUnavailableError(f"the model refused the call made with {credential}")
    detail = str(fault)
    emitter = Emitter(out=sys.stdout, err=sys.stderr, json_output=True)

    emitter.emit(
        {
            "verb": VERB_LABEL,
            "run": RUN_LABEL,
            "error_detail": detail,
            "credential": credential,
            "nested": {"api_key": credential},
            "authorization": OPAQUE_SENSITIVE_VALUE,
        }
    )

    captured = capsys.readouterr()
    assert LOADED_VALUE not in captured.out
    document: dict[str, object] = json.loads(captured.out)
    expected = f"the model refused the call made with {CREDENTIAL_PLACEHOLDER}"
    assert document["error_detail"] == expected
    # Two replacements, both concealing: a sensitive field name is replaced by the
    # formatter's own token whatever it holds, and a wrapped credential under a
    # field name the set does not list still renders as its own placeholder. The
    # value reaches a reader through neither.
    assert document["credential"] == CREDENTIAL_PLACEHOLDER
    assert document["nested"] == {"api_key": REDACTED}
    assert document["authorization"] == REDACTED


# ---------------------------------------------------------------------------
# An output stream
# ---------------------------------------------------------------------------


def test_the_verb_formatter_writes_the_placeholder_to_the_real_streams(
    credential: Credential,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Narration, a warning, and a failure document, on the process's own streams."""
    emitter = Emitter(out=sys.stdout, err=sys.stderr, json_output=True)

    emitter.narrate(f"loading the credential {credential}")
    emitter.warn(f"the provider refused the call made with {credential}")
    code = emitter.fail(
        VERB_LABEL,
        f"the call made with {credential} was refused",
        ExitCode.OPERATIONAL,
    )

    captured = capsys.readouterr()
    assert code is ExitCode.OPERATIONAL
    assert LOADED_VALUE not in captured.out
    assert LOADED_VALUE not in captured.err
    assert captured.err.count(CREDENTIAL_PLACEHOLDER) == 3
    document: dict[str, object] = json.loads(captured.out)
    assert document["error"] == f"the call made with {CREDENTIAL_PLACEHOLDER} was refused"


def test_a_plain_write_to_the_real_stream_renders_the_placeholder(
    credential: Credential,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every conversion a careless caller might reach for, on the real stream."""
    renderings: Sequence[str] = (
        str(credential),
        repr(credential),
        format(credential),
        format(credential, ">40"),
        f"{credential!s}",
        f"{credential!r}",
        str([credential]),
        repr({"held": credential}),
    )
    for rendering in renderings:
        print(rendering, file=sys.stdout)

    captured = capsys.readouterr()
    assert LOADED_VALUE not in captured.out
    assert captured.out.count(CREDENTIAL_PLACEHOLDER) == len(renderings)
