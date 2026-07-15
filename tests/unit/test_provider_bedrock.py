"""Unit tests for the default provider implementation of both model roles.

The delivered account cannot invoke a model at all: on-demand inference quota is
zero and non-adjustable there, so every live call is refused. These tests are
therefore about shape and about failure handling rather than about a successful
call, which is exactly the split the provider abstraction exists to make possible.

Four things are pinned down.

**Every failure cause collapses to one class.** A throttle, a timeout, a transport
fault, a refusal by the service, and a malformed answer are each driven through a
scripted client and each is asserted to surface as the one unavailable-model
class. That collapse is the contract every calling component's fail-closed
behaviour rests on.

**The prompt-cache capability comes from the model.** It starts false, and both
states are driven: a model reporting cache accounting in its usage answer sets it
true, and a model refusing the marked call leaves it false while still reporting
the model as reachable.

**The declared width is correct with no call made.** The startup width gate falls
back to the declared width when a model never answers, so the declared width has
to be right on an object that has contacted nothing.

**One builder answers for both roles.** The registry holds one builder per module
and registers this module for both roles, so the builder is driven for each of the
three selection shapes and the identifier it reports is checked in each.

Every identifier, region, and value here is obviously synthetic. No real model
identifier and no real region appears in this file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Final

import pytest

from molt.config.resolve import Configuration, MissingConfigError
from molt.errors import ModelUnavailableError, ProviderError
from molt.providers import (
    SCHEMA_VECTOR_DIMENSIONS,
    EmbeddingProvider,
    Prompt,
    TextProvider,
)
from molt.providers.bedrock import (
    CACHE_POINT_KEY,
    CACHE_READ_TOKENS_KEY,
    CACHE_WRITE_TOKENS_KEY,
    INPUT_TOKENS_KEY,
    OUTPUT_TOKENS_KEY,
    PROVIDER_NAME,
    UNCONFIGURED_MODEL,
    BedrockEmbeddingProvider,
    BedrockProvider,
    BedrockSession,
    BedrockTextProvider,
    Role,
    build,
)

# Synthetic throughout. A region name and a model identifier are configuration
# values, so neither the real region nor any real identifier belongs here.
FAKE_REGION: Final[str] = "fake-region"
FAKE_EMBEDDING_MODEL: Final[str] = "fake-embedding-model"
FAKE_TEXT_MODEL: Final[str] = "fake-text-model"
OTHER_PROVIDER: Final[str] = "external"

# A narrow width, so a test vector is readable and a width disagreement is easy to
# state. The declared-width test uses the schema width instead.
NARROW_WIDTH: Final[int] = 4


class ClientFailureError(Exception):
    """A failure carrying a service error code, as the client library attaches one."""

    def __init__(self, code: str) -> None:
        super().__init__("the call was refused")
        self.response: Mapping[str, object] = {"Error": {"Code": code}}


class ReadTimeoutError(Exception):
    """A timeout, recognised by the name the client library gives it."""


class EndpointConnectionError(Exception):
    """A transport fault, recognised by the name the client library gives it."""


class StubBody:
    """A response body that answers bytes exactly as a streamed body does."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        """Return the whole body."""
        return self._raw


class StubClient:
    """A runtime client whose every answer is scripted.

    An outcome is either a response to answer with or a failure to raise. The last
    outcome repeats, so a retry bound can be observed without scripting one
    outcome per attempt.
    """

    def __init__(
        self,
        *,
        invoke: Sequence[Mapping[str, object] | Exception] = (),
        converse: Sequence[Mapping[str, object] | Exception] = (),
    ) -> None:
        self._invoke = list(invoke)
        self._converse = list(converse)
        self.invoke_calls: list[Mapping[str, object]] = []
        self.converse_calls: list[Mapping[str, object]] = []

    @staticmethod
    def _next(
        outcomes: list[Mapping[str, object] | Exception],
    ) -> Mapping[str, object]:
        if not outcomes:
            raise AssertionError("the stub client was called with no outcome scripted")
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def invoke_model(self, **kwargs: object) -> Mapping[str, object]:
        """Answer the next scripted embedding outcome."""
        self.invoke_calls.append(kwargs)
        return self._next(self._invoke)

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        """Answer the next scripted exchange outcome."""
        self.converse_calls.append(kwargs)
        return self._next(self._converse)


def embedding_answer(width: int) -> Mapping[str, object]:
    """A well-formed embedding answer holding a vector of one width."""
    vector = [float(index) / float(width) for index in range(width)]
    return {"body": StubBody(json.dumps({"embedding": vector}).encode())}


def exchange_answer(*, text: str = "ok", cache: bool) -> Mapping[str, object]:
    """A well-formed exchange answer, with or without the cache accounting."""
    usage: dict[str, object] = {INPUT_TOKENS_KEY: 11, OUTPUT_TOKENS_KEY: 3}
    if cache:
        usage[CACHE_WRITE_TOKENS_KEY] = 7
        usage[CACHE_READ_TOKENS_KEY] = 5
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": usage,
    }


def session_with(client: StubClient, *, max_attempts: int = 3) -> BedrockSession:
    """A session over a scripted client, so no client is ever constructed."""
    return BedrockSession(region=FAKE_REGION, max_attempts=max_attempts, client=client)


def embedding_over(
    client: StubClient,
    *,
    width: int = NARROW_WIDTH,
    batch_size: int = 25,
    max_attempts: int = 3,
) -> BedrockEmbeddingProvider:
    """The embedding role over a scripted client."""
    return BedrockEmbeddingProvider(
        session=session_with(client, max_attempts=max_attempts),
        model_id=FAKE_EMBEDDING_MODEL,
        dimensions=width,
        batch_size=batch_size,
    )


def text_over(client: StubClient, *, max_attempts: int = 3) -> BedrockTextProvider:
    """The text role over a scripted client."""
    return BedrockTextProvider(
        session=session_with(client, max_attempts=max_attempts),
        model_id=FAKE_TEXT_MODEL,
    )


def configuration_for(
    *,
    embedding_provider: str = PROVIDER_NAME,
    text_provider: str = PROVIDER_NAME,
    embedding_model: str | None = FAKE_EMBEDDING_MODEL,
    text_model: str | None = FAKE_TEXT_MODEL,
) -> Configuration:
    """A resolved configuration naming this provider for the chosen roles."""
    environ: dict[str, str] = {
        "MOLT_BEDROCK_REGION": FAKE_REGION,
        "MOLT_EMBEDDING_PROVIDER": embedding_provider,
        "MOLT_TEXT_PROVIDER": text_provider,
    }
    if embedding_model is not None:
        environ["MOLT_EMBEDDING_MODEL_ID"] = embedding_model
    if text_model is not None:
        environ["MOLT_ADJUDICATION_MODEL_ID"] = text_model
    return Configuration(environ=environ)


# --------------------------------------------------------------------------
# The declared width, which the startup gate falls back to
# --------------------------------------------------------------------------


def test_the_declared_width_is_the_schema_width_with_no_call_made() -> None:
    client = StubClient()
    provider = BedrockEmbeddingProvider(session=session_with(client), model_id=FAKE_EMBEDDING_MODEL)
    assert provider.dimensions == SCHEMA_VECTOR_DIMENSIONS
    assert provider.name == PROVIDER_NAME
    assert client.invoke_calls == []


def test_the_declared_width_is_what_the_request_asks_the_model_for() -> None:
    client = StubClient(invoke=[embedding_answer(NARROW_WIDTH)])
    provider = embedding_over(client)
    provider.embed(["one"])
    assert _request_body(client.invoke_calls[0])["dimensions"] == NARROW_WIDTH


def test_the_probe_reports_the_width_the_model_answered_with() -> None:
    client = StubClient(invoke=[embedding_answer(NARROW_WIDTH)])
    probe = embedding_over(client, width=SCHEMA_VECTOR_DIMENSIONS).probe()
    assert probe.reachable
    assert probe.dimensions == NARROW_WIDTH


# --------------------------------------------------------------------------
# Embedding behaviour
# --------------------------------------------------------------------------


def test_one_vector_is_returned_per_text_in_the_input_order() -> None:
    client = StubClient(invoke=[embedding_answer(NARROW_WIDTH)])
    vectors = embedding_over(client, batch_size=2).embed(["one", "two", "three"])
    assert len(vectors) == 3
    assert all(len(vector) == NARROW_WIDTH for vector in vectors)
    sent = [_request_body(call)["inputText"] for call in client.invoke_calls]
    assert sent == ["one", "two", "three"]


def test_an_empty_input_contacts_no_model() -> None:
    client = StubClient()
    assert embedding_over(client).embed([]) == ()
    assert client.invoke_calls == []


def test_a_width_the_model_disagrees_with_is_an_unavailable_model() -> None:
    client = StubClient(invoke=[embedding_answer(NARROW_WIDTH)])
    provider = embedding_over(client, width=NARROW_WIDTH + 1)
    with pytest.raises(ModelUnavailableError):
        provider.embed(["one"])


# --------------------------------------------------------------------------
# Every failure cause collapses to one class
# --------------------------------------------------------------------------

MALFORMED_ANSWERS: Final[tuple[Mapping[str, object], ...]] = (
    {},
    {"body": StubBody(b"not a document")},
    {"body": StubBody(json.dumps({"embedding": "text rather than numbers"}).encode())},
    {"body": StubBody(json.dumps({"embedding": [1.0, None]}).encode())},
    {"body": StubBody(json.dumps({"embedding": []}).encode())},
)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ClientFailureError("ThrottlingException"), id="throttle"),
        pytest.param(ReadTimeoutError(), id="timeout"),
        pytest.param(EndpointConnectionError(), id="transport"),
        pytest.param(ClientFailureError("ValidationException"), id="refusal"),
        pytest.param(RuntimeError("something else entirely"), id="unclassified"),
    ],
)
def test_every_embedding_failure_cause_is_one_unavailable_model(failure: Exception) -> None:
    provider = embedding_over(StubClient(invoke=[failure]))
    with pytest.raises(ModelUnavailableError):
        provider.embed(["one"])


@pytest.mark.parametrize(
    "answer",
    MALFORMED_ANSWERS,
    ids=["no-body", "no-document", "no-numbers", "a-value-that-is-none", "an-empty-vector"],
)
def test_every_malformed_embedding_answer_is_one_unavailable_model(
    answer: Mapping[str, object],
) -> None:
    provider = embedding_over(StubClient(invoke=[answer]))
    with pytest.raises(ModelUnavailableError):
        provider.embed(["one"])


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ClientFailureError("ThrottlingException"), id="throttle"),
        pytest.param(ReadTimeoutError(), id="timeout"),
        pytest.param(EndpointConnectionError(), id="transport"),
        pytest.param(ClientFailureError("ValidationException"), id="refusal"),
        pytest.param(RuntimeError("something else entirely"), id="unclassified"),
    ],
)
def test_every_text_failure_cause_is_one_unavailable_model(failure: Exception) -> None:
    provider = text_over(StubClient(converse=[failure]))
    with pytest.raises(ModelUnavailableError):
        provider.generate(Prompt(stable_prefix="prefix", variable_suffix="suffix"))


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({}, id="nothing"),
        pytest.param({"output": {}, "usage": {}}, id="no-message"),
        pytest.param(
            {"output": {"message": {"content": [{"text": "ok"}]}}},
            id="no-accounting",
        ),
        pytest.param(
            {
                "output": {"message": {"content": [{"text": "ok"}]}},
                "usage": {OUTPUT_TOKENS_KEY: 3},
            },
            id="missing-input-tokens",
        ),
        pytest.param(
            {
                "output": {"message": {"content": [{"text": "ok"}]}},
                "usage": {INPUT_TOKENS_KEY: "many", OUTPUT_TOKENS_KEY: 3},
            },
            id="unreadable-input-tokens",
        ),
    ],
)
def test_every_malformed_text_answer_is_one_unavailable_model(
    answer: Mapping[str, object],
) -> None:
    provider = text_over(StubClient(converse=[answer]))
    with pytest.raises(ModelUnavailableError):
        provider.generate(Prompt(stable_prefix="prefix", variable_suffix="suffix"))


def test_a_transient_failure_is_retried_to_the_bound_and_a_refusal_is_not() -> None:
    throttled = StubClient(invoke=[ClientFailureError("ThrottlingException")])
    with pytest.raises(ModelUnavailableError):
        embedding_over(throttled, max_attempts=3).embed(["one"])
    assert len(throttled.invoke_calls) == 3

    refused = StubClient(invoke=[ClientFailureError("ValidationException")])
    with pytest.raises(ModelUnavailableError):
        embedding_over(refused, max_attempts=3).embed(["one"])
    assert len(refused.invoke_calls) == 1


@pytest.mark.skipif(
    importlib.util.find_spec("boto3") is not None,
    reason="the client package is installed, so its absence cannot be observed here",
)
def test_an_absent_client_package_is_an_unavailable_model() -> None:
    provider = BedrockEmbeddingProvider(
        session=BedrockSession(region=FAKE_REGION),
        model_id=FAKE_EMBEDDING_MODEL,
    )
    with pytest.raises(ModelUnavailableError):
        provider.embed(["one"])


def test_importing_the_module_pulls_in_no_client_library() -> None:
    assert "molt.providers.bedrock" in sys.modules
    assert "boto3" not in sys.modules
    assert "botocore" not in sys.modules


# --------------------------------------------------------------------------
# The prompt-cache capability, read from the model
# --------------------------------------------------------------------------


def test_the_capability_starts_false_before_the_model_has_reported() -> None:
    assert text_over(StubClient()).supports_prompt_cache is False


def test_the_capability_is_true_when_the_model_reports_cache_accounting() -> None:
    client = StubClient(converse=[exchange_answer(cache=True)])
    provider = text_over(client)
    probe = provider.probe()
    assert probe.reachable
    assert probe.supports_prompt_cache is True
    assert provider.supports_prompt_cache is True
    assert any(CACHE_POINT_KEY in block for block in _blocks(client.converse_calls[0]))


def test_the_capability_is_false_when_the_model_reports_no_cache_accounting() -> None:
    client = StubClient(converse=[exchange_answer(cache=False)])
    provider = text_over(client)
    probe = provider.probe()
    assert probe.reachable
    assert probe.supports_prompt_cache is False
    assert provider.supports_prompt_cache is False


def test_a_model_refusing_the_marker_is_reachable_and_reports_no_capability() -> None:
    client = StubClient(
        converse=[ClientFailureError("ValidationException"), exchange_answer(cache=False)]
    )
    provider = text_over(client)
    probe = provider.probe()
    assert probe.reachable
    assert probe.supports_prompt_cache is False
    assert not any(CACHE_POINT_KEY in block for block in _blocks(client.converse_calls[-1]))


def test_a_model_answering_neither_probe_call_is_unavailable() -> None:
    client = StubClient(converse=[ClientFailureError("ValidationException")])
    with pytest.raises(ModelUnavailableError):
        text_over(client).probe()


def _request_body(call: Mapping[str, object]) -> Mapping[str, object]:
    """The decoded document one recorded embedding call carried."""
    body = call["body"]
    assert isinstance(body, str)
    decoded = json.loads(body)
    assert isinstance(decoded, Mapping)
    return decoded


def _blocks(call: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    """The content blocks one recorded exchange call carried."""
    messages = call["messages"]
    assert isinstance(messages, Sequence)
    first = messages[0]
    assert isinstance(first, Mapping)
    content = first["content"]
    assert isinstance(content, Sequence)
    return [block for block in content if isinstance(block, Mapping)]


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def test_generation_reports_the_token_accounting_including_the_cache_counts() -> None:
    client = StubClient(converse=[exchange_answer(text="answer", cache=True)])
    result = text_over(client).generate(Prompt(stable_prefix="prefix", variable_suffix="suffix"))
    assert result.text == "answer"
    assert result.model_id == FAKE_TEXT_MODEL
    assert result.input_tokens == 11
    assert result.output_tokens == 3
    assert result.cache_creation_tokens == 7
    assert result.cache_read_tokens == 5


def test_absent_cache_accounting_reads_as_no_cache_charge() -> None:
    client = StubClient(converse=[exchange_answer(cache=False)])
    result = text_over(client).generate(Prompt(stable_prefix="prefix", variable_suffix="suffix"))
    assert result.cache_creation_tokens == 0
    assert result.cache_read_tokens == 0


def test_the_marker_is_sent_only_when_the_caller_asks_and_the_model_reported_support() -> None:
    client = StubClient(converse=[exchange_answer(cache=True)])
    provider = text_over(client)
    asked = Prompt(stable_prefix="prefix", variable_suffix="suffix", cache_boundary=True)

    provider.generate(asked)
    assert not any(CACHE_POINT_KEY in block for block in _blocks(client.converse_calls[-1]))

    provider.supports_prompt_cache = True
    provider.generate(asked)
    marked = _blocks(client.converse_calls[-1])
    assert [CACHE_POINT_KEY in block for block in marked] == [False, True, False]

    provider.generate(Prompt(stable_prefix="prefix", variable_suffix="suffix"))
    assert not any(CACHE_POINT_KEY in block for block in _blocks(client.converse_calls[-1]))


def test_a_prompt_carrying_no_text_is_refused_before_any_call() -> None:
    client = StubClient()
    with pytest.raises(ProviderError):
        text_over(client).generate(Prompt(stable_prefix="", variable_suffix=""))
    assert client.converse_calls == []


# --------------------------------------------------------------------------
# One builder answering for both roles
# --------------------------------------------------------------------------


def test_the_builder_answers_both_protocols_from_one_module() -> None:
    provider = build(configuration_for())
    assert isinstance(provider, BedrockProvider)
    assert isinstance(provider, EmbeddingProvider)
    assert isinstance(provider, TextProvider)
    embedding_role: EmbeddingProvider = provider
    text_role: TextProvider = provider
    assert embedding_role.dimensions == SCHEMA_VECTOR_DIMENSIONS
    assert text_role.supports_prompt_cache is False


def test_both_role_identifiers_are_held_and_the_reported_one_is_the_role_in_play() -> None:
    both = build(configuration_for())
    assert both.embedding_model_id == FAKE_EMBEDDING_MODEL
    assert both.text_model_id == FAKE_TEXT_MODEL
    assert both.model_id == FAKE_EMBEDDING_MODEL

    text_only = build(configuration_for(embedding_provider=OTHER_PROVIDER, embedding_model=None))
    assert text_only.model_id == FAKE_TEXT_MODEL
    assert text_only.embedding_model_id == UNCONFIGURED_MODEL

    embedding_only = build(configuration_for(text_provider=OTHER_PROVIDER, text_model=None))
    assert embedding_only.model_id == FAKE_EMBEDDING_MODEL
    assert embedding_only.text_model_id == UNCONFIGURED_MODEL


def test_only_a_role_in_play_is_required_to_name_a_model() -> None:
    with pytest.raises(MissingConfigError):
        build(configuration_for(embedding_model=None))
    with pytest.raises(MissingConfigError):
        build(configuration_for(text_model=None))
    # A role another provider serves needs no identifier here.
    assert build(configuration_for(embedding_provider=OTHER_PROVIDER, embedding_model=None))


def test_an_unconfigured_role_is_not_probed_and_the_other_still_answers() -> None:
    client = StubClient(invoke=[embedding_answer(NARROW_WIDTH)])
    session = session_with(client)
    provider = BedrockProvider(
        embedding=BedrockEmbeddingProvider(
            session=session, model_id=FAKE_EMBEDDING_MODEL, dimensions=NARROW_WIDTH
        ),
        text=BedrockTextProvider(session=session, model_id=None),
        primary=Role.EMBEDDING,
    )
    probe = provider.probe()
    assert probe.reachable
    assert probe.dimensions == NARROW_WIDTH
    assert probe.supports_prompt_cache is None
    assert client.converse_calls == []


def test_one_role_failing_its_probe_does_not_hide_the_other_role_answering() -> None:
    client = StubClient(
        invoke=[ClientFailureError("ValidationException")],
        converse=[exchange_answer(cache=True)],
    )
    session = session_with(client)
    provider = BedrockProvider(
        embedding=BedrockEmbeddingProvider(
            session=session, model_id=FAKE_EMBEDDING_MODEL, dimensions=NARROW_WIDTH
        ),
        text=BedrockTextProvider(session=session, model_id=FAKE_TEXT_MODEL),
    )
    probe = provider.probe()
    assert probe.reachable is False
    assert probe.dimensions is None
    assert probe.supports_prompt_cache is True
    assert provider.supports_prompt_cache is True


def test_no_configured_role_answering_is_one_unavailable_model() -> None:
    client = StubClient(
        invoke=[ClientFailureError("ValidationException")],
        converse=[ClientFailureError("ValidationException")],
    )
    session = session_with(client)
    provider = BedrockProvider(
        embedding=BedrockEmbeddingProvider(
            session=session, model_id=FAKE_EMBEDDING_MODEL, dimensions=NARROW_WIDTH
        ),
        text=BedrockTextProvider(session=session, model_id=FAKE_TEXT_MODEL),
    )
    with pytest.raises(ModelUnavailableError):
        provider.probe()
