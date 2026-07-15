"""The documented default implementation of both model roles, through AWS Bedrock.

This module exists to be the default and to be structurally correct rather than to
be the path the delivered configuration takes. On-demand inference quota is zero
and non-adjustable on the delivered account, so every call here answers with a
refusal. That is precisely the condition the provider abstraction was built for: a
restored quota is a configuration change and no source change, and until then an
unreachable model is a runtime condition every calling component already fails
closed on rather than a precondition of the process starting.

Five decisions shape the module.

**One module-level builder answers for both roles.** The registry registers this
module under both role names and calls one builder for each, and a builder is
handed a configuration rather than a role, so it cannot be told which of the two
registrations it is answering. The builder therefore returns one object that
satisfies both protocols, composed of the two role implementations it delegates
to. Which of the two roles actually selected this provider is read from the
selection keys, so only the roles in play need a model identifier and the reported
identifier is the one the role in play names. Where both roles select this
provider, the reported identifier is the embedding model's, because that is the
identifier stored on every vector row and therefore the one an incorrect answer
would make durable; the text side's identifier is reported on every result the
text role returns and is separately readable on the object.

**Every failure cause collapses to one class.** A throttle, a timeout, a transport
fault, a refusal by the service, a malformed answer, a body that cannot be read,
a vector holding something that is no number, and an absent client package all
surface as `ModelUnavailable`. A caller's response to each is identical — retry a
bounded number of times, then fail closed — so distinguishing them would invite a
branch nothing asks for. The cause is named in the message and in a log record,
which is where a cause belongs.

**The prompt-cache capability is read from the model rather than assumed.** It
starts false, and the probe sets it from what the model reports: a probe call
carrying a cache marker whose usage answer carries the cache-token accounting
means the capability is present, and a refusal of that same call means it is
absent, confirmed by a second call without the marker to establish that the model
is reachable at all. Assuming the capability would either pay for a marker the
model ignores or skip one the model honours.

**The declared vector width is what the request asks for.** The width is sent in
the embedding request body and reported as the declared width, so the startup gate
has a correct answer with no call having been made. The probe reports the width
the model actually returned, so a model disagreeing with its own request is caught
by the gate rather than one insert at a time by a column constraint.

**The client package is imported lazily behind a narrow structural protocol.**
Importing this module therefore drags in no client library, so the credential-free
suites collect and the strict type check runs with none installed, and an absent
package surfaces as an unavailable model rather than as an import failure.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING, Final, Protocol, cast

from molt.config.resolve import Configuration, MissingConfigError
from molt.errors import ModelUnavailableError, ProviderError
from molt.providers import (
    SCHEMA_VECTOR_DIMENSIONS,
    ProbeLike,
    Prompt,
    PromptLike,
    ProviderProbe,
    TextResult,
)
from molt.providers.registry import EMBEDDING_ROLE, TEXT_ROLE
from molt.providers.selector import selected_embedding_name, selected_text_name
from molt.telemetry import Severity, log, metric

if TYPE_CHECKING:
    from molt.providers import EmbeddingProvider, TextProvider

__all__ = [
    "BATCH_SIZE_ENV",
    "CACHE_READ_TOKENS_KEY",
    "CACHE_WRITE_TOKENS_KEY",
    "COMPONENT",
    "DIMENSIONS_ENV",
    "EMBEDDING_MODEL_ENV",
    "INPUT_TOKENS_KEY",
    "MAX_RETRIES_ENV",
    "OUTPUT_TOKENS_KEY",
    "PROVIDER_NAME",
    "REGION_ENV",
    "TIMEOUT_ENV",
    "TRANSIENT_FAILURE_NAMES",
    "UNCONFIGURED_MODEL",
    "VALIDATION_FAILURE_NAMES",
    "BedrockEmbeddingProvider",
    "BedrockProvider",
    "BedrockSession",
    "BedrockTextProvider",
    "ResponseBody",
    "Role",
    "RuntimeClient",
    "build",
    "unavailable",
]

# The registry key this module is registered under for both roles, and the value
# stored alongside every vector this implementation produces.
PROVIDER_NAME: Final[str] = "bedrock"

# The component name every log record written here carries.
COMPONENT: Final[str] = "bedrock_provider"

# What a role reports as its model identifier when no identifier is configured for
# it. A role in play always has one, because the builder requires it; this is what
# the other role holds so that the object still answers every protocol member.
UNCONFIGURED_MODEL: Final[str] = "unconfigured"

# The configuration keys this implementation reads. They are restated here rather
# than imported because they are each other module's private detail, and reading
# them through the resolved configuration is what keeps a region and a model
# identifier out of every tracked file.
REGION_ENV: Final[str] = "MOLT_BEDROCK_REGION"
TIMEOUT_ENV: Final[str] = "MOLT_PROVIDER_TIMEOUT_SECONDS"
MAX_RETRIES_ENV: Final[str] = "MOLT_PROVIDER_MAX_RETRIES"
DIMENSIONS_ENV: Final[str] = "MOLT_EMBEDDING_DIMENSIONS"
BATCH_SIZE_ENV: Final[str] = "MOLT_EMBEDDING_BATCH_SIZE"
EMBEDDING_MODEL_ENV: Final[str] = "MOLT_EMBEDDING_MODEL_ID"
ADJUDICATION_MODEL_ENV: Final[str] = "MOLT_ADJUDICATION_MODEL_ID"
REWRITE_MODEL_ENV: Final[str] = "MOLT_REWRITE_MODEL_ID"

# The client package, the module holding its client settings, and the runtime
# service both roles call. All three are text rather than imports, because the
# package is loaded through the import machinery on first use.
CLOUD_PACKAGE: Final[str] = "boto3"
CLIENT_SETTINGS_MODULE: Final[str] = "botocore.config"
RUNTIME_SERVICE: Final[str] = "bedrock-runtime"

# The document type both request bodies and response bodies carry.
JSON_CONTENT_TYPE: Final[str] = "application/json"

# The usage fields a text answer's accounting is read from. The two cache fields
# are what the capability is read from as well: a model reporting cache accounting
# is a model that honoured the cache marker.
INPUT_TOKENS_KEY: Final[str] = "inputTokens"
OUTPUT_TOKENS_KEY: Final[str] = "outputTokens"
CACHE_WRITE_TOKENS_KEY: Final[str] = "cacheWriteInputTokens"
CACHE_READ_TOKENS_KEY: Final[str] = "cacheReadInputTokens"

# The marker that splits a prompt into a cached prefix and a variable suffix.
CACHE_POINT_KEY: Final[str] = "cachePoint"
CACHE_POINT_TYPE: Final[str] = "default"

# Failure kinds worth another bounded attempt: a throttle, a timeout, a transport
# fault, or a model that is not yet ready. Classification is by exception type
# name and by the error code the client attaches, because the client package is
# imported lazily and its exception classes are therefore not nameable here.
TRANSIENT_FAILURE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "connectionerror",
        "connecttimeouterror",
        "endpointconnectionerror",
        "internalfailure",
        "internalservererror",
        "modelnotreadyexception",
        "modeltimeoutexception",
        "readtimeouterror",
        "requesttimeout",
        "serviceunavailable",
        "serviceunavailableexception",
        "throttlingexception",
        "toomanyrequestsexception",
    }
)

# The failure kind that means the service refused the request as stated rather
# than failed to serve it. It is the one kind the capability probe reads a meaning
# from: a refusal of a call carrying the cache marker is the model reporting that
# it does not honour the marker.
VALIDATION_FAILURE_NAMES: Final[frozenset[str]] = frozenset({"validationexception"})

# The probe prompt and the probe's output ceiling. Both are as small as a call can
# be, because a probe exists to learn reachability and capability rather than to
# produce anything worth reading.
PROBE_PREFIX: Final[str] = "Answer with one word."
PROBE_SUFFIX: Final[str] = "Ready?"
PROBE_TEXT: Final[str] = "probe"
PROBE_MAX_OUTPUT_TOKENS: Final[int] = 8

# The defaults each constructor falls back to when a caller names no value. The
# configuration surface supplies all three in every deployed path.
DEFAULT_TIMEOUT_SECONDS: Final[int] = 30
DEFAULT_MAX_ATTEMPTS: Final[int] = 3
DEFAULT_BATCH_SIZE: Final[int] = 25
DEFAULT_MAX_OUTPUT_TOKENS: Final[int] = 1024


class Role(StrEnum):
    """The two roles this module answers for, named as the registry names them."""

    EMBEDDING = EMBEDDING_ROLE
    TEXT = TEXT_ROLE


# --------------------------------------------------------------------------
# The client, reached structurally rather than by import
# --------------------------------------------------------------------------


class ResponseBody(Protocol):
    """The one call an invoke response's streamed body is read through."""

    def read(self) -> bytes:
        """Return the whole body."""


class RuntimeClient(Protocol):
    """The two calls this implementation makes on the runtime client.

    Declared structurally rather than imported, because the client package is
    loaded through the import machinery and ships no shape the type check can
    follow. Arguments are accepted as keywords so the client's own argument
    spelling stays at the call site rather than being restated here.
    """

    def invoke_model(self, **kwargs: object) -> Mapping[str, object]:
        """Invoke a model with a document body, used by the embedding role."""

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        """Hold one exchange with a model, used by the text role."""


def unavailable(what: str, names: frozenset[str]) -> ModelUnavailableError:
    """Build the one failure class every cause collapses to, naming the cause."""
    detail = ", ".join(sorted(names)) if names else "no cause was reported"
    return ModelUnavailableError(f"the model did not answer the {what}: {detail}")


def _failure_names(error: BaseException) -> frozenset[str]:
    """The lowercased names by which a client failure might be recognised."""
    names = {type(error).__name__.lower()}
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        detail = response.get("Error")
        if isinstance(detail, Mapping):
            code = detail.get("Code")
            if isinstance(code, str):
                names.add(code.lower())
    return frozenset(names)


def _client_settings(timeout_seconds: int) -> object | None:
    """The client settings carrying the configured timeout, where they can be built.

    The settings live in a module of the client package, so an installation
    without it simply gets a client on the package's own defaults rather than no
    client at all. The bounded retry lives in this module, so the client is asked
    to make one attempt and leave the retrying alone.
    """
    try:
        module = importlib.import_module(CLIENT_SETTINGS_MODULE)
    except ImportError:
        return None
    built: object = module.Config(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
        retries={"max_attempts": 1, "mode": "standard"},
    )
    return built


def _create_client(region: str, timeout_seconds: int) -> RuntimeClient:
    """Construct the runtime client in the configured region, on first use.

    An absent client package is an unavailable model rather than an import
    failure, which is what lets this module be imported, type-checked, and
    registered on a machine with no client library installed.
    """
    try:
        package = importlib.import_module(CLOUD_PACKAGE)
    except ImportError as exc:
        raise ModelUnavailableError(
            "the cloud client package is not installed, so no model can be invoked"
        ) from exc
    settings = _client_settings(timeout_seconds)
    try:
        created: object = (
            package.client(RUNTIME_SERVICE, region_name=region)
            if settings is None
            else package.client(RUNTIME_SERVICE, region_name=region, config=settings)
        )
    except Exception as error:
        raise ModelUnavailableError(
            f"the runtime client could not be constructed: {type(error).__name__}"
        ) from error
    return cast(RuntimeClient, created)


class BedrockSession:
    """The lazily built client and the bounded retry the two roles share.

    One session serves both roles, so a provider answering for both holds one
    client rather than two. The client is built on first use rather than at
    construction, because construction happens at startup and an absent or
    unreachable client must not end a process there.
    """

    __slots__ = ("_client", "_max_attempts", "_region", "_timeout_seconds")

    def __init__(
        self,
        *,
        region: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        client: RuntimeClient | None = None,
    ) -> None:
        self._region = region
        self._timeout_seconds = max(1, timeout_seconds)
        self._max_attempts = max(1, max_attempts)
        self._client = client

    @property
    def region(self) -> str:
        """The region the client speaks to."""
        return self._region

    @property
    def max_attempts(self) -> int:
        """How many attempts a transient failure is given in total."""
        return self._max_attempts

    def client(self) -> RuntimeClient:
        """The client, built on first use and held for the session's lifetime."""
        if self._client is None:
            self._client = _create_client(self._region, self._timeout_seconds)
        return self._client

    def attempt(
        self,
        what: str,
        action: Callable[[RuntimeClient], Mapping[str, object]],
    ) -> tuple[Mapping[str, object] | None, frozenset[str]]:
        """Make a bounded attempt at one call, answering the failure rather than raising.

        This is the classified half of a call: a caller that reads a meaning from
        the kind of failure gets the recognised names back instead of one collapsed
        class. Every caller that reads no meaning from them uses `call` instead, and
        both end in the same place.
        """
        client = self.client()
        names: frozenset[str] = frozenset()
        for attempt in range(self._max_attempts):
            try:
                return action(client), frozenset()
            except Exception as error:
                # The client package is imported lazily, so its exception classes
                # cannot be named here; a failure is classified by name and code.
                names = _failure_names(error)
                metric("provider.call_failure", 1.0, provider=PROVIDER_NAME)
                log(
                    Severity.WARNING,
                    COMPONENT,
                    "a model call failed",
                    call=what,
                    attempt=attempt + 1,
                    attempts=self._max_attempts,
                    cause=type(error).__name__,
                )
                if not names & TRANSIENT_FAILURE_NAMES:
                    break
        return None, names

    def call(
        self,
        what: str,
        action: Callable[[RuntimeClient], Mapping[str, object]],
    ) -> Mapping[str, object]:
        """Make a bounded attempt at one call, collapsing every failure to one class."""
        response, names = self.attempt(what, action)
        if response is None:
            raise unavailable(what, names)
        return response


# --------------------------------------------------------------------------
# Reading an answer, where every malformed shape is one failure
# --------------------------------------------------------------------------


def _document_from(response: Mapping[str, object], what: str) -> Mapping[str, object]:
    """Read and decode an invoke response's body, refusing every unreadable shape."""
    body = response.get("body")
    if body is None or not callable(getattr(body, "read", None)):
        raise ModelUnavailableError(f"the {what} answer carried no readable body")
    try:
        raw = cast(ResponseBody, body).read()
    except Exception as error:
        raise ModelUnavailableError(
            f"the {what} answer body could not be read: {type(error).__name__}"
        ) from error
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ModelUnavailableError(f"the {what} answer body held no readable document") from error
    if not isinstance(decoded, Mapping):
        raise ModelUnavailableError(f"the {what} answer body held no document")
    return cast(Mapping[str, object], decoded)


def _numbers_from(candidate: object, what: str) -> tuple[float, ...]:
    """Read a sequence of finite numbers, refusing anything else."""
    if not isinstance(candidate, Sequence) or isinstance(candidate, (str, bytes, bytearray)):
        raise ModelUnavailableError(f"the {what} answer carried no vector")
    values: list[float] = []
    for element in candidate:
        if isinstance(element, bool) or not isinstance(element, (int, float)):
            raise ModelUnavailableError(f"the {what} answer carried a vector holding no number")
        number = float(element)
        if not isfinite(number):
            raise ModelUnavailableError(
                f"the {what} answer carried a vector holding a value that is not finite"
            )
        values.append(number)
    if not values:
        raise ModelUnavailableError(f"the {what} answer carried an empty vector")
    return tuple(values)


def _vector_from(document: Mapping[str, object], what: str) -> tuple[float, ...]:
    """Pull one vector out of an embedding answer, accepting either reported shape.

    A model answering one vector reports it directly; a model answering a batch
    reports a sequence of them. Accepting both is what keeps the request shape the
    only model-specific thing in this module.
    """
    single = document.get("embedding")
    if single is None:
        listed = document.get("embeddings")
        if isinstance(listed, Sequence) and not isinstance(listed, (str, bytes)) and listed:
            single = listed[0]
    return _numbers_from(single, what)


def _text_from(response: Mapping[str, object], what: str) -> str:
    """Join the text blocks of an exchange answer, refusing every other shape."""
    output = response.get("output")
    if not isinstance(output, Mapping):
        raise ModelUnavailableError(f"the {what} answer carried no output")
    message = output.get("message")
    if not isinstance(message, Mapping):
        raise ModelUnavailableError(f"the {what} answer carried no message")
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        raise ModelUnavailableError(f"the {what} answer carried no content")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise ModelUnavailableError(f"the {what} answer carried a content block of no shape")
        piece = block.get("text")
        if isinstance(piece, str):
            parts.append(piece)
    return "".join(parts)


def _usage_from(response: Mapping[str, object], what: str) -> Mapping[str, object]:
    """Pull the token accounting out of an exchange answer."""
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        raise ModelUnavailableError(f"the {what} answer carried no token accounting")
    return cast(Mapping[str, object], usage)


def _token_count(usage: Mapping[str, object], key: str, what: str, *, required: bool) -> int:
    """Read one token count, refusing a value that is no whole count.

    An absent optional count reads as zero, because a model reporting no cache
    accounting charged nothing for a cache. An absent required count is a
    malformed answer, because the accounting is what the cost story is read from
    and a total nobody reported is a total nobody can check.
    """
    value = usage.get(key)
    if value is None:
        if required:
            raise ModelUnavailableError(f"the {what} answer reported no {key}")
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelUnavailableError(f"the {what} answer reported an unreadable {key}")
    return value


def _reports_cache_accounting(usage: Mapping[str, object]) -> bool:
    """Whether the model reported cache accounting, which is how it reports support."""
    for key in (CACHE_WRITE_TOKENS_KEY, CACHE_READ_TOKENS_KEY):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return True
    return False


def _grouped(texts: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Split an input into groups of at most the configured size."""
    for start in range(0, len(texts), size):
        yield texts[start : start + size]


# --------------------------------------------------------------------------
# The embedding role
# --------------------------------------------------------------------------


class BedrockEmbeddingProvider:
    """The embedding role: one vector per text, at the width the request asks for.

    The declared width is sent in the request body and reported as the declared
    width, so the startup gate has a correct answer before any call is made. The
    probe reports the width the model actually returned, so a model disagreeing
    with its own request is refused by the gate rather than discovered one insert
    at a time by the stored column's constraint.
    """

    name: str
    model_id: str
    dimensions: int

    __slots__ = ("_batch_size", "_session", "dimensions", "model_id", "name")

    def __init__(
        self,
        *,
        session: BedrockSession,
        model_id: str | None,
        dimensions: int = SCHEMA_VECTOR_DIMENSIONS,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.name = PROVIDER_NAME
        self.model_id = model_id or UNCONFIGURED_MODEL
        self.dimensions = dimensions
        self._session = session
        self._batch_size = max(1, batch_size)

    @property
    def configured(self) -> bool:
        """Whether a model identifier is configured for this role."""
        return self.model_id != UNCONFIGURED_MODEL

    def _model(self) -> str:
        """The configured model identifier, naming the key when none is configured."""
        if not self.configured:
            raise MissingConfigError(EMBEDDING_MODEL_ENV, "providers.embedding_model_id")
        return self.model_id

    def _request(self, model: str, text: str) -> Callable[[RuntimeClient], Mapping[str, object]]:
        """The one call an embedding takes, with the declared width asked for."""
        body = json.dumps({"inputText": text, "dimensions": self.dimensions})

        def issue(client: RuntimeClient) -> Mapping[str, object]:
            return client.invoke_model(
                modelId=model,
                body=body,
                accept=JSON_CONTENT_TYPE,
                contentType=JSON_CONTENT_TYPE,
            )

        return issue

    def _embed_one(self, model: str, text: str, what: str) -> tuple[float, ...]:
        """Embed one text, collapsing every failure cause to one class."""
        response = self._session.call(what, self._request(model, text))
        return _vector_from(_document_from(response, what), what)

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one vector per input text, in the input order.

        The input is walked in groups of at most the configured batch size, and a
        group is the unit a telemetry record accounts for, because the request
        shape carries one text per call.
        """
        if not texts:
            return ()
        model = self._model()
        what = "embedding call"
        vectors: list[tuple[float, ...]] = []
        for group in _grouped(texts, self._batch_size):
            for text in group:
                vector = self._embed_one(model, text, what)
                if len(vector) != self.dimensions:
                    raise ModelUnavailableError(
                        f"the model answered a vector of width {len(vector)} where "
                        f"{self.dimensions} was asked for"
                    )
                vectors.append(vector)
            metric("provider.embedding_group", float(len(group)), provider=PROVIDER_NAME)
        return tuple(vectors)

    def probe(self) -> ProbeLike:
        """Report reachability and the width the model actually answered with."""
        what = "embedding probe"
        vector = self._embed_one(self._model(), PROBE_TEXT, what)
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=len(vector),
        )


# --------------------------------------------------------------------------
# The text role
# --------------------------------------------------------------------------


def _content_blocks(prompt: PromptLike, *, mark_boundary: bool) -> list[Mapping[str, object]]:
    """The prompt as content blocks, with the cache marker where one is warranted.

    The marker sits immediately after the stable prefix, which is the only place
    it can sit: the prefix is what repeats byte-identically across calls and the
    suffix is what differs, so the boundary between them is the only boundary
    worth caching at.
    """
    if not prompt.stable_prefix and not prompt.variable_suffix:
        raise ProviderError("a prompt carrying no text cannot be sent to a model")
    blocks: list[Mapping[str, object]] = []
    if prompt.stable_prefix:
        blocks.append({"text": prompt.stable_prefix})
    if mark_boundary:
        blocks.append({CACHE_POINT_KEY: {"type": CACHE_POINT_TYPE}})
    if prompt.variable_suffix:
        blocks.append({"text": prompt.variable_suffix})
    return blocks


class BedrockTextProvider:
    """The text role, with the prompt-cache capability read from the model.

    `supports_prompt_cache` starts false and is set by the probe from what the
    model reported. Nothing here assumes the capability, and nothing marks a cache
    boundary the model has not been observed to honour.
    """

    name: str
    model_id: str
    supports_prompt_cache: bool

    __slots__ = ("_max_output_tokens", "_session", "model_id", "name", "supports_prompt_cache")

    def __init__(
        self,
        *,
        session: BedrockSession,
        model_id: str | None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        self.name = PROVIDER_NAME
        self.model_id = model_id or UNCONFIGURED_MODEL
        # False until the model itself reports otherwise. This is the whole of the
        # capability's provenance: no default, no table of model identifiers, and
        # no inference from a name.
        self.supports_prompt_cache = False
        self._session = session
        self._max_output_tokens = max(1, max_output_tokens)

    @property
    def configured(self) -> bool:
        """Whether a model identifier is configured for this role."""
        return self.model_id != UNCONFIGURED_MODEL

    def _model(self) -> str:
        """The configured model identifier, naming the keys when none is configured."""
        if not self.configured:
            raise MissingConfigError(f"{ADJUDICATION_MODEL_ENV} or {REWRITE_MODEL_ENV}", None)
        return self.model_id

    def _request(
        self,
        model: str,
        prompt: PromptLike,
        *,
        mark_boundary: bool,
        max_output_tokens: int,
    ) -> Callable[[RuntimeClient], Mapping[str, object]]:
        """The one call a generation takes, as an exchange carrying content blocks."""
        blocks = _content_blocks(prompt, mark_boundary=mark_boundary)

        def issue(client: RuntimeClient) -> Mapping[str, object]:
            return client.converse(
                modelId=model,
                messages=[{"role": "user", "content": blocks}],
                inferenceConfig={"maxTokens": max_output_tokens, "temperature": 0.0},
            )

        return issue

    def generate(self, prompt: Prompt) -> TextResult:
        """Answer a two-part prompt, reporting the token accounting for the call.

        The cache boundary is marked only where the caller asked for it and the
        model has reported the capability, so a marker is never sent to a model
        that would refuse it or silently ignore it.
        """
        model = self._model()
        what = "generation call"
        response = self._session.call(
            what,
            self._request(
                model,
                prompt,
                mark_boundary=prompt.cache_boundary and self.supports_prompt_cache,
                max_output_tokens=self._max_output_tokens,
            ),
        )
        usage = _usage_from(response, what)
        return TextResult(
            text=_text_from(response, what),
            model_id=model,
            input_tokens=_token_count(usage, INPUT_TOKENS_KEY, what, required=True),
            output_tokens=_token_count(usage, OUTPUT_TOKENS_KEY, what, required=True),
            cache_creation_tokens=_token_count(usage, CACHE_WRITE_TOKENS_KEY, what, required=False),
            cache_read_tokens=_token_count(usage, CACHE_READ_TOKENS_KEY, what, required=False),
        )

    def probe(self) -> ProbeLike:
        """Report reachability and the cache capability the model itself reports.

        The probe asks once with the cache marker present. A model that answers
        and reports cache accounting supports the capability. A model that refuses
        the marked call is asked once more without the marker: an answer to that
        second call means the model is reachable and reports no cache support,
        and a failure of it means the model is simply unavailable.
        """
        model = self._model()
        prompt = Prompt(stable_prefix=PROBE_PREFIX, variable_suffix=PROBE_SUFFIX)
        marked = "capability probe"
        response, names = self._session.attempt(
            marked,
            self._request(
                model, prompt, mark_boundary=True, max_output_tokens=PROBE_MAX_OUTPUT_TOKENS
            ),
        )
        if response is not None:
            reported = _reports_cache_accounting(_usage_from(response, marked))
            self.supports_prompt_cache = reported
            return ProviderProbe(
                name=self.name,
                model_id=self.model_id,
                reachable=True,
                supports_prompt_cache=reported,
            )

        self.supports_prompt_cache = False
        if not names & VALIDATION_FAILURE_NAMES:
            raise unavailable(marked, names)

        plain = "reachability probe"
        second, second_names = self._session.attempt(
            plain,
            self._request(
                model, prompt, mark_boundary=False, max_output_tokens=PROBE_MAX_OUTPUT_TOKENS
            ),
        )
        if second is None:
            raise unavailable(plain, second_names)
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            supports_prompt_cache=False,
        )


# --------------------------------------------------------------------------
# One object answering for both roles
# --------------------------------------------------------------------------


class BedrockProvider:
    """Both roles behind one object, because the registry calls one builder.

    The registry holds one builder per implementation module and this module is
    registered for both roles, so the builder cannot be told which registration it
    is answering and the object it returns has to satisfy both protocols. It does
    that by delegation rather than by reimplementation: the two role
    implementations above hold all the behaviour, share one client session, and
    this object forwards to them.

    The single reported model identifier is the role in play's identifier. Where
    both roles are in play it is the embedding model's, because that identifier is
    stored on every vector row and is therefore the one an incorrect answer would
    make durable; the text role's identifier is separately readable here and is
    carried on every result the text role returns.
    """

    name: str
    model_id: str
    dimensions: int
    supports_prompt_cache: bool

    __slots__ = (
        "_embedding",
        "_text",
        "dimensions",
        "model_id",
        "name",
        "supports_prompt_cache",
    )

    def __init__(
        self,
        *,
        embedding: BedrockEmbeddingProvider,
        text: BedrockTextProvider,
        primary: Role = Role.EMBEDDING,
    ) -> None:
        self.name = PROVIDER_NAME
        self._embedding = embedding
        self._text = text
        self.dimensions = embedding.dimensions
        self.supports_prompt_cache = text.supports_prompt_cache
        preferred = (
            (text.model_id, embedding.model_id)
            if primary is Role.TEXT
            else (embedding.model_id, text.model_id)
        )
        self.model_id = next(
            (identifier for identifier in preferred if identifier != UNCONFIGURED_MODEL),
            UNCONFIGURED_MODEL,
        )

    @property
    def embedding_model_id(self) -> str:
        """The identifier the embedding role calls, so neither role's is lost."""
        return self._embedding.model_id

    @property
    def text_model_id(self) -> str:
        """The identifier the text role calls, so neither role's is lost."""
        return self._text.model_id

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one vector per input text, in the input order."""
        return self._embedding.embed(texts)

    def generate(self, prompt: Prompt) -> TextResult:
        """Answer a two-part prompt, honouring the capability recorded on this object."""
        self._text.supports_prompt_cache = self.supports_prompt_cache
        return self._text.generate(prompt)

    def probe(self) -> ProbeLike:
        """Probe every configured role and answer for both in one report.

        A role with no configured identifier is not probed, so a provider in play
        for one role only makes one call. Where both roles are configured, both are
        probed and both answers are carried: the width the embedding model
        returned and the capability the text model reported. The one failure class
        is raised only when no configured role answered at all, so one role's
        silence never hides the other role's answer.
        """
        embedding_probe: ProbeLike | None = None
        text_probe: ProbeLike | None = None
        first_failure: ModelUnavailableError | None = None

        if not self._embedding.configured and not self._text.configured:
            raise MissingConfigError(f"{EMBEDDING_MODEL_ENV} or {ADJUDICATION_MODEL_ENV}", None)

        if self._embedding.configured:
            try:
                embedding_probe = self._embedding.probe()
            except ModelUnavailableError as error:
                first_failure = first_failure or error
        if self._text.configured:
            try:
                text_probe = self._text.probe()
            except ModelUnavailableError as error:
                first_failure = first_failure or error

        answered = tuple(probe for probe in (embedding_probe, text_probe) if probe is not None)
        if not answered:
            raise ModelUnavailableError(
                "no configured model role answered its probe"
            ) from first_failure

        if text_probe is not None and text_probe.supports_prompt_cache is not None:
            self.supports_prompt_cache = text_probe.supports_prompt_cache

        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=first_failure is None and all(probe.reachable for probe in answered),
            dimensions=None if embedding_probe is None else embedding_probe.dimensions,
            supports_prompt_cache=None if text_probe is None else text_probe.supports_prompt_cache,
        )


# --------------------------------------------------------------------------
# The one builder the registry calls, for either role
# --------------------------------------------------------------------------


def _text_model_id(configuration: Configuration, *, required: bool) -> str | None:
    """The text model identifier, from whichever text-consuming role names one.

    The two text-consuming roles name their models under their own keys and one
    provider object serves both, so the identifier this object calls is the
    adjudication model where one is named and the rewrite model otherwise. A role
    naming its own model still reads its own key, so nothing here overrides either.
    """
    for env in (ADJUDICATION_MODEL_ENV, REWRITE_MODEL_ENV):
        named = configuration.optional_text(env)
        if named is not None:
            return named
    if required:
        raise MissingConfigError(f"{ADJUDICATION_MODEL_ENV} or {REWRITE_MODEL_ENV}", None)
    return None


def build(configuration: Configuration) -> BedrockProvider:
    """Construct the provider for whichever of the two roles selected it.

    This is the one module-level builder the registry calls, and it is called once
    per role that named this provider. It cannot be told which role it is
    answering, so it reads the two selection keys instead: only a role in play is
    required to name a model, and the identifier the object reports is the one the
    role in play named.

    Nothing here contacts a model and nothing here builds a client. Construction
    is pure configuration reading, so an unreachable model is a runtime condition
    the probe reports rather than a startup failure.
    """
    session = BedrockSession(
        region=configuration.text(REGION_ENV),
        timeout_seconds=configuration.integer(TIMEOUT_ENV),
        max_attempts=configuration.integer(MAX_RETRIES_ENV),
    )
    embedding_selected = selected_embedding_name(configuration) == PROVIDER_NAME
    text_selected = selected_text_name(configuration) == PROVIDER_NAME
    embedding_model = (
        configuration.text(EMBEDDING_MODEL_ENV)
        if embedding_selected
        else configuration.optional_text(EMBEDDING_MODEL_ENV)
    )
    return BedrockProvider(
        embedding=BedrockEmbeddingProvider(
            session=session,
            model_id=embedding_model,
            dimensions=configuration.integer(DIMENSIONS_ENV),
            batch_size=configuration.integer(BATCH_SIZE_ENV),
        ),
        text=BedrockTextProvider(
            session=session,
            model_id=_text_model_id(configuration, required=text_selected),
        ),
        primary=Role.TEXT if text_selected and not embedding_selected else Role.EMBEDDING,
    )


if TYPE_CHECKING:

    def _roles_satisfy_their_protocols(
        embedding: BedrockEmbeddingProvider,
        text: BedrockTextProvider,
        both: BedrockProvider,
    ) -> tuple[EmbeddingProvider, TextProvider, EmbeddingProvider, TextProvider]:
        """Standing evidence that each class satisfies the protocol its role requires.

        The return annotation is the assertion: the type check accepts this only
        while each role implementation satisfies its own protocol and the combined
        object satisfies both, which is what lets one builder answer for both
        registrations.
        """
        return embedding, text, both, both
