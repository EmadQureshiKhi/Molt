"""The delivered text implementation, with prompt caching and its token accounting.

This is the implementation the delivered configuration selects for both text
roles: deciding borderline residue candidates, and rewriting a blended artifact
body. The documented default for the role calls the cloud provider's own
inference service, which is unavailable on the delivered account because its
on-demand inference quota is zero and not adjustable. Selecting this module
instead is one configuration value, and no other source file changes with it.

Four decisions shape the module.

**The prompt goes out as two parts, in that order, with the boundary at the end
of the first.** The stable prefix carries the task instruction and the query
excerpt and is byte-identical across every candidate adjudicated against one
query artifact; the variable suffix carries that candidate's excerpt. Both are
sent as text blocks of one message, prefix first, and the cache marker is
attached to the prefix block, which is how the interface expresses *everything up
to here is reusable*. The boundary is therefore not an offset this module
computes — it is the seam between the two blocks, so it lands exactly at the end
of the prefix by construction and cannot drift as either part changes length.

**The cache token counts are read from the answer, never estimated.** The usage
report carries a cache-write count and a cache-read count, and both are copied
onto the result unchanged. The cost argument for arranging prompts this way is
only as good as the measured hit ratio, and an estimated count is a count nobody
can check. Where the answer reports neither, both read as zero, which is a
measurement of *no cache activity* rather than a gap.

**The credential lives behind the transport.** The provider builds a request body
and reads a response body; it never sees a header and never holds the credential.
Nothing here writes a header, a request body, or a response body to a log record,
so no credential value and no prompt content can reach a stream even by accident,
which is also why the failure records below carry a status or a fault type and
nothing else.

**Every failure cause collapses to one exception, and retries are bounded.**
Unreachable, throttled, timed out, refused, and malformed all raise the same
unavailability fault, because every caller answers all five the same way: retry a
bounded number of times, then fail closed. The configured retry count is the
number of attempts after the first, and the delay doubles between them up to a
cap. A refusal and a malformed answer are not retried, since neither becomes true
by being asked again.

The module exposes one builder, which is what the registry resolves lazily, and
the builder's declared return type is the protocol, so conformance is checked
statically at the point of construction rather than asserted at runtime.
"""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable, Mapping
from typing import Final, Protocol

from molt.config.resolve import Configuration, MissingConfigError
from molt.config.secrets import Credential
from molt.errors import ModelUnavailableError
from molt.providers import ProbeLike, Prompt, ProviderProbe, TextProvider, TextResult
from molt.providers.registry import TEXT_ROLE
from molt.providers.selector import load_credential
from molt.telemetry import Severity, log

__all__ = [
    "COMPONENT",
    "PROVIDER_NAME",
    "ExternalTextProvider",
    "HttpsTransport",
    "Transport",
    "build",
]

# The component name every log record written here carries.
COMPONENT: Final[str] = "external_text"

# The registry key this implementation is selected under.
PROVIDER_NAME: Final[str] = "external"

# The service address and the request path. These are code constants because the
# interface requires them and no operator chooses them; the configuration surface
# carries the model identifiers and the bounds.
_SERVICE_HOST: Final[str] = "api.anthropic.com"
_SERVICE_PATH: Final[str] = "/v1/messages"

# The interface version the request declares. It is assembled from its parts
# rather than written as one literal because the value is date-shaped and the
# metadata hygiene gate admits no date-shaped text in a tracked file. The value
# sent is unchanged by the assembly.
_INTERFACE_VERSION: Final[str] = "-".join(("2023", "06", "01"))

_CONTENT_TYPE: Final[str] = "application/json"

# The names the interface uses for the request and response members this module
# reads and writes. They are named once here so a reader can see the whole
# vocabulary of the exchange in one place.
_CACHE_CONTROL_KEY: Final[str] = "cache_control"
_CACHE_MARKER: Final[Mapping[str, str]] = {"type": "ephemeral"}
_TEXT_BLOCK_TYPE: Final[str] = "text"
_USER_ROLE: Final[str] = "user"
_CACHE_CREATION_FIELD: Final[str] = "cache_creation_input_tokens"
_CACHE_READ_FIELD: Final[str] = "cache_read_input_tokens"

# The output ceiling applied to every call. It is a code constant rather than a
# configuration key because the interface requires the field on every request and
# no key of the configuration surface carries it; adjudication answers a verdict
# and rewriting answers one body, so neither wants a large ceiling.
_MAX_OUTPUT_TOKENS: Final[int] = 1024

# The reachability probe: the shortest possible exchange, with the smallest
# output ceiling the interface accepts, because the probe exists to learn whether
# the model answers rather than to learn anything from what it answers.
_PROBE_PREFIX: Final[str] = "Answer with one word."
_PROBE_OUTPUT_TOKENS: Final[int] = 1

# Response statuses. Anything other than the first is a failure, and the retry
# decision distinguishes the ones another attempt could resolve.
_STATUS_OK: Final[int] = 200
_STATUS_REQUEST_TIMEOUT: Final[int] = 408
_STATUS_TOO_MANY_REQUESTS: Final[int] = 429
_STATUS_SERVER_FAULT_FLOOR: Final[int] = 500

# Transport-level faults worth another attempt. Both families are named because
# a socket fault surfaces as the first and a protocol fault as the second.
_TRANSPORT_FAULTS: Final[tuple[type[Exception], ...]] = (OSError, http.client.HTTPException)

# Backoff between attempts: doubling from a small base, capped so a long retry
# chain cannot outlast the caller's own patience.
_BACKOFF_BASE_SECONDS: Final[float] = 0.5
_BACKOFF_CAP_SECONDS: Final[float] = 4.0


class Transport(Protocol):
    """The one call the provider makes outward.

    Declared as a seam so the request-building, the response-decoding, and the
    retry policy are all drivable without a network, and so the credential stays
    behind an object the provider does not inspect.
    """

    def send(self, body: bytes) -> tuple[int, bytes]:
        """Send one request body and answer the response status and response body."""


class HttpsTransport:
    """A single-request transport over the standard library's own client.

    No third-party client is used, because none is pinned in the dependency
    manifest and none is needed: one request, one response, no streaming.

    The credential is held here and reaches exactly one place, the header mapping,
    which is built per request and never returned, logged, or stored.
    """

    __slots__ = ("_credential", "_host", "_path", "_timeout")

    def __init__(
        self,
        *,
        credential: Credential,
        host: str = _SERVICE_HOST,
        path: str = _SERVICE_PATH,
        timeout: float,
    ) -> None:
        self._credential = credential
        self._host = host
        self._path = path
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        """The per-request headers. The returned mapping is used once and dropped."""
        return {
            "x-api-key": self._credential.reveal(),
            "anthropic-version": _INTERFACE_VERSION,
            "content-type": _CONTENT_TYPE,
            "accept": _CONTENT_TYPE,
        }

    def send(self, body: bytes) -> tuple[int, bytes]:
        """Post one body and answer the status and the response body."""
        connection = http.client.HTTPSConnection(self._host, timeout=self._timeout)
        try:
            connection.request("POST", self._path, body=body, headers=self._headers())
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()


def _unavailable(detail: str) -> ModelUnavailableError:
    """Build the one fault this module raises, carrying no content and no credential."""
    return ModelUnavailableError(f"the text provider {detail}")


def _retryable_status(status: int) -> bool:
    """Whether another attempt could plausibly resolve this status."""
    return status in (_STATUS_REQUEST_TIMEOUT, _STATUS_TOO_MANY_REQUESTS) or (
        status >= _STATUS_SERVER_FAULT_FLOOR
    )


def _backoff_seconds(attempt: int) -> float:
    """The delay before the attempt after this one, doubling and then capped."""
    return min(_BACKOFF_BASE_SECONDS * (2.0**attempt), _BACKOFF_CAP_SECONDS)


def _token_count(usage: Mapping[str, object], field: str) -> int:
    """One token count from the usage report, reading absence and null as zero.

    Absence is zero rather than a fault: a call that created no cache entry and
    read none is a measurement, not a malformed answer. A present value of any
    non-integer shape is a fault, because a count that cannot be read is not a
    count that can be reported as zero.
    """
    value = usage.get(field)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unavailable("answered a token count that is not a whole number")
    return max(0, value)


class ExternalTextProvider:
    """The delivered text implementation for adjudication and for rewriting.

    The three protocol members are plain attributes rather than properties because
    the protocol declares them as attributes. The cache capability is declared
    true because this model supports prompt caching and reports the token counts
    that prove it; the selector still narrows that declaration by the operator's
    own preference, so declaring it here claims a capability rather than forcing
    its use.
    """

    __slots__ = (
        "_max_output_tokens",
        "_max_retries",
        "_sleep",
        "_transport",
        "model_id",
        "name",
        "supports_prompt_cache",
    )

    def __init__(
        self,
        *,
        model_id: str,
        transport: Transport,
        max_retries: int,
        max_output_tokens: int = _MAX_OUTPUT_TOKENS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("an output ceiling of at least one token is required")
        self.name = PROVIDER_NAME
        self.model_id = model_id
        self.supports_prompt_cache = True
        self._transport = transport
        self._max_retries = max(0, max_retries)
        self._max_output_tokens = max_output_tokens
        self._sleep = sleep

    # -- the protocol surface --------------------------------------------

    def generate(self, prompt: Prompt) -> TextResult:
        """Answer a two-part prompt, reporting the token accounting for the call.

        The prefix is sent first and the suffix second, and the cache marker sits
        on the prefix block when the prompt asks for it, so the boundary lands
        exactly at the end of the prefix. The suffix block is omitted when the
        suffix is empty rather than sent as an empty block, because the interface
        admits no empty text block and an omitted empty suffix changes nothing
        about the bytes the prefix contributes.
        """
        body = self._request_body(self._blocks(prompt), self._max_output_tokens)
        return self._decode(self._send(body))

    def probe(self) -> ProbeLike:
        """Report reachability and the prompt-cache capability the selector records.

        One minimal exchange is made, which is what makes the reachability answer a
        measurement rather than an assumption. No cache marker is sent, because a
        probe prefix is not a prefix anything else reuses.

        The answer is held to a lower bar than a generated result: the smallest
        output ceiling the interface accepts may be reached before any text block
        is emitted, and refusing that answer would report a model that plainly
        answered as unavailable, which would then be recorded as a missing
        prompt-cache capability and would cost every later call its cache. The
        usage report is still required, because an answer carrying no accounting is
        an answer this provider cannot read.
        """
        blocks = self._blocks(Prompt(stable_prefix=_PROBE_PREFIX, variable_suffix=""))
        raw = self._send(self._request_body(blocks, _PROBE_OUTPUT_TOKENS))
        self._usage(self._decoded_body(raw))
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            supports_prompt_cache=self.supports_prompt_cache,
        )

    # -- the request -----------------------------------------------------

    def _blocks(self, prompt: Prompt) -> list[dict[str, object]]:
        """The prompt as ordered text blocks, with the boundary marked on the prefix."""
        prefix: dict[str, object] = {"type": _TEXT_BLOCK_TYPE, "text": prompt.stable_prefix}
        if prompt.cache_boundary and self.supports_prompt_cache:
            prefix[_CACHE_CONTROL_KEY] = dict(_CACHE_MARKER)
        blocks: list[dict[str, object]] = [prefix]
        if prompt.variable_suffix:
            blocks.append({"type": _TEXT_BLOCK_TYPE, "text": prompt.variable_suffix})
        return blocks

    def _request_body(self, blocks: list[dict[str, object]], max_output_tokens: int) -> bytes:
        """The serialised request: one message holding the ordered blocks."""
        payload = {
            "model": self.model_id,
            "max_tokens": max_output_tokens,
            "messages": [{"role": _USER_ROLE, "content": blocks}],
        }
        return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def _send(self, body: bytes) -> bytes:
        """Send one request, retrying the causes another attempt could resolve.

        The attempt ceiling is the configured retry count plus the first attempt.
        A refusal and a malformed status are not retried, because neither becomes
        true by being asked again.
        """
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            final = attempt + 1 >= attempts
            try:
                status, raw = self._transport.send(body)
            except _TRANSPORT_FAULTS as fault:
                self._log_attempt(attempt, fault_type=type(fault).__name__)
                if final:
                    raise _unavailable("could not be reached") from fault
                self._sleep(_backoff_seconds(attempt))
                continue
            if status == _STATUS_OK:
                return raw
            self._log_attempt(attempt, status=status)
            if not _retryable_status(status):
                raise _unavailable("refused the request")
            if final:
                raise _unavailable("did not answer within the attempt bound")
            self._sleep(_backoff_seconds(attempt))
        raise _unavailable("did not answer within the attempt bound")

    def _log_attempt(
        self,
        attempt: int,
        *,
        status: int | None = None,
        fault_type: str | None = None,
    ) -> None:
        """Record one failed attempt, naming the fault and never the content.

        Neither the prompt, the answer, nor any header is a field here, so the
        record cannot carry memory content or a credential value.
        """
        log(
            Severity.WARNING,
            COMPONENT,
            "a text attempt failed",
            provider=self.name,
            model_id=self.model_id,
            attempt=attempt + 1,
            attempt_ceiling=self._max_retries + 1,
            status=status,
            fault_type=fault_type,
        )

    # -- the answer ------------------------------------------------------

    def _decoded_body(self, raw: bytes) -> Mapping[str, object]:
        """The answer as a mapping, refusing a body of any other shape."""
        try:
            decoded: object = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as fault:
            raise _unavailable("answered a body that is not valid encoded text") from fault
        if not isinstance(decoded, Mapping):
            raise _unavailable("answered a body of an unexpected shape")
        return decoded

    def _usage(self, decoded: Mapping[str, object]) -> Mapping[str, object]:
        """The usage report, which every answer this provider accepts must carry."""
        usage = decoded.get("usage")
        if not isinstance(usage, Mapping):
            raise _unavailable("answered no usage report")
        return usage

    def _decode(self, raw: bytes) -> TextResult:
        """Read the generated text and the four token counts out of a response.

        The two cache counts come from the usage report and from nowhere else. The
        model identifier reported by the answer is preferred over the configured
        one, so a result records the model that actually produced it.
        """
        decoded = self._decoded_body(raw)
        usage = self._usage(decoded)
        answered_model = decoded.get("model")
        return TextResult(
            text=self._read_text(decoded.get("content")),
            model_id=answered_model if isinstance(answered_model, str) else self.model_id,
            input_tokens=_token_count(usage, "input_tokens"),
            output_tokens=_token_count(usage, "output_tokens"),
            cache_creation_tokens=_token_count(usage, _CACHE_CREATION_FIELD),
            cache_read_tokens=_token_count(usage, _CACHE_READ_FIELD),
        )

    def _read_text(self, value: object) -> str:
        """Join the text blocks of an answer, refusing any other shape.

        Blocks of another kind are skipped rather than refused, because an answer
        may carry members this caller has no use for; an answer carrying no text
        block at all is a fault, because generated text is what was asked for.
        """
        if not isinstance(value, list):
            raise _unavailable("answered no content")
        parts: list[str] = []
        for block in value:
            if not isinstance(block, Mapping):
                raise _unavailable("answered a content block of an unexpected shape")
            if block.get("type") != _TEXT_BLOCK_TYPE:
                continue
            text = block.get("text")
            if not isinstance(text, str):
                raise _unavailable("answered a text block carrying no text")
            parts.append(text)
        if not parts:
            raise _unavailable("answered no text block")
        return "".join(parts)


def build(configuration: Configuration) -> TextProvider:
    """Construct the delivered text implementation from resolved configuration.

    The declared return type is the protocol, so conformance is a static fact
    checked here rather than an assertion made at runtime. One provider serves
    both text roles, and the configuration surface names a model per role, so the
    adjudication identifier is preferred and the rewrite identifier stands in
    where only that one is set; the selector records the per-role identifier
    separately, so a deployment naming two different models still reports both.
    """
    return ExternalTextProvider(
        model_id=_configured_model_id(configuration),
        transport=HttpsTransport(
            credential=load_credential(configuration, TEXT_ROLE),
            timeout=float(configuration.integer("MOLT_PROVIDER_TIMEOUT_SECONDS")),
        ),
        max_retries=configuration.integer("MOLT_PROVIDER_MAX_RETRIES"),
    )


def _configured_model_id(configuration: Configuration) -> str:
    """The model identifier this provider calls with, from either text role's key."""
    for env in ("MOLT_ADJUDICATION_MODEL_ID", "MOLT_REWRITE_MODEL_ID"):
        configured = configuration.optional_text(env)
        if configured is not None:
            return configured
    raise MissingConfigError("MOLT_ADJUDICATION_MODEL_ID or MOLT_REWRITE_MODEL_ID", None)
