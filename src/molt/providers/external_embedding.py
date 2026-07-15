"""The delivered embedding implementation: a code-specialised retrieval model.

This is the implementation the delivered configuration selects for the embedding
role. The documented default for the role calls the cloud provider's own
inference service; that service is unavailable on the delivered account because
its on-demand inference quota is zero and not adjustable, which is exactly the
condition the provider abstraction exists to absorb. Selecting this module
instead is one configuration value, and no other source file changes with it.

Four decisions shape the module.

**A code-specialised retrieval model, at the width the schema fixes.** Residue
detection is a search for semantically similar *source code*, so a retrieval
model trained on code is the right objective rather than a general-purpose one.
The model answers the width the stored column and the distributed vector index
are declared at, so selecting it changes neither. The width is not hard-coded
here: it is read from the configuration surface, declared on the instance, sent
on every request as the requested output width, and checked on every response. A
response of any other width is treated as a malformed answer rather than quietly
stored, which is what keeps the column constraint from being the first thing to
notice a provider change.

**The transport is a seam, and the credential lives behind it.** The provider
builds a request body and reads a response body; it never sees a header and never
holds the credential. The credential is loaded once, through the loader that
resolves a parameter name or an operator-provided file and wraps the value so it
renders as one fixed placeholder everywhere, and it is held by the transport
alone. Nothing in this module writes a header, a request body, or a response body
to a log record, so no credential value can reach a stream even by accident.

**Every failure cause collapses to one exception.** Unreachable, throttled, timed
out, refused, and malformed all raise the same unavailability fault. That is not
laziness: every caller's response is identical in each case — retry a bounded
number of times, then fail closed — so distinguishing them here would invite a
branch no requirement asks for. Only the retry decision looks at the cause, and
it looks at it while deciding whether another attempt is worth making, never
after the failure has been surfaced.

**Retries are bounded and back off.** The configured retry count is the number of
*additional* attempts after the first, so the configured three means at most four
attempts, and the delay doubles between them from a small base up to a cap. A
throttle, a timeout, a transport fault, and a server fault are worth another
attempt. A refusal and a malformed answer are not: neither becomes true by being
asked again, so both fail immediately and the caller's bounded retry loop is not
spent on a request that cannot succeed.

The module exposes one builder, which is what the registry resolves lazily, and
the builder's declared return type is the protocol, so conformance is checked
statically at the point of construction rather than asserted at runtime.
"""

from __future__ import annotations

import http.client
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Protocol

from molt.config.resolve import Configuration
from molt.config.secrets import Credential
from molt.errors import ModelUnavailableError
from molt.providers import EmbeddingProvider, ProbeLike, ProviderProbe
from molt.providers.registry import EMBEDDING_ROLE
from molt.providers.selector import load_credential
from molt.telemetry import Severity, log

__all__ = [
    "COMPONENT",
    "PROVIDER_NAME",
    "ExternalEmbeddingProvider",
    "HttpsTransport",
    "Transport",
    "build",
]

# The component name every log record written here carries.
COMPONENT: Final[str] = "external_embedding"

# The registry key this implementation is selected under, and the name stored
# alongside the model identifier on every vector row, so a corpus embedded across
# a provider change stays distinguishable row by row.
PROVIDER_NAME: Final[str] = "external"

# The service address and the request path. These are code constants because the
# interface requires them and no operator chooses them; the configuration surface
# carries the model identifier, the width, the batch size, and the bounds.
_SERVICE_HOST: Final[str] = "api.voyageai.com"
_SERVICE_PATH: Final[str] = "/v1/embeddings"

# The request headers, with the credential supplied at construction. The scheme
# word is the one the interface requires on the authorisation header.
_AUTHORIZATION_SCHEME: Final[str] = "Bearer"
_CONTENT_TYPE: Final[str] = "application/json"

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

# The text the reachability probe embeds. Short on purpose: the probe exists to
# learn whether the model answers, not to learn anything about the text.
_PROBE_TEXT: Final[str] = "probe"


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
    manifest and none is needed: one request, one response, no streaming, no
    connection reuse worth keeping across calls.

    The credential is held here and reaches exactly one place, the header
    mapping, which is built per request and never returned, logged, or stored.
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
            "authorization": f"{_AUTHORIZATION_SCHEME} {self._credential.reveal()}",
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
    return ModelUnavailableError(f"the embedding provider {detail}")


def _retryable_status(status: int) -> bool:
    """Whether another attempt could plausibly resolve this status."""
    return status in (_STATUS_REQUEST_TIMEOUT, _STATUS_TOO_MANY_REQUESTS) or (
        status >= _STATUS_SERVER_FAULT_FLOOR
    )


def _backoff_seconds(attempt: int) -> float:
    """The delay before the attempt after this one, doubling and then capped."""
    return min(_BACKOFF_BASE_SECONDS * (2.0**attempt), _BACKOFF_CAP_SECONDS)


class ExternalEmbeddingProvider:
    """The delivered embedding implementation.

    The three protocol members are plain attributes rather than properties
    because the protocol declares them as attributes, and because the name and
    the model identifier are both stored on every vector row and so are read far
    more often than they are set.
    """

    __slots__ = (
        "_batch_size",
        "_max_retries",
        "_sleep",
        "_transport",
        "dimensions",
        "model_id",
        "name",
    )

    def __init__(
        self,
        *,
        model_id: str,
        dimensions: int,
        batch_size: int,
        transport: Transport,
        max_retries: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if dimensions < 1:
            raise ValueError("an embedding width of at least one dimension is required")
        if batch_size < 1:
            raise ValueError("an embedding batch of at least one text is required")
        self.name = PROVIDER_NAME
        self.model_id = model_id
        self.dimensions = dimensions
        self._batch_size = batch_size
        self._transport = transport
        self._max_retries = max(0, max_retries)
        self._sleep = sleep

    # -- the protocol surface --------------------------------------------

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one vector per input text, in the input order.

        The input is split into batches of the configured size, and the batches
        are concatenated in order, so the caller's ordering assumption holds
        across any input length. Vectors are returned exactly as the model
        answered them: normalisation to unit length happens at the write path,
        which is the one place that has to agree with the index's distance
        function.
        """
        if not texts:
            return ()
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = tuple(texts[start : start + self._batch_size])
            vectors.extend(self._embed_batch(batch))
        return tuple(vectors)

    def probe(self) -> ProbeLike:
        """Report reachability and the width this provider declares.

        One short text is embedded, which is what makes the reachability answer a
        measurement rather than an assumption, and the width reported is the
        declared one. A response of any other width has already failed the
        response check by this point, so the two cannot disagree silently.
        """
        self._embed_batch((_PROBE_TEXT,))
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )

    # -- one call --------------------------------------------------------

    def _embed_batch(self, batch: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Embed one batch, raising the single unavailability fault on any failure."""
        payload = {
            "model": self.model_id,
            "input": list(batch),
            "output_dimension": self.dimensions,
        }
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return self._decode(self._send(body), len(batch))

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

        Neither the request body, the response body, nor any header is a field
        here, so the record cannot carry memory content or a credential value.
        """
        log(
            Severity.WARNING,
            COMPONENT,
            "an embedding attempt failed",
            provider=self.name,
            model_id=self.model_id,
            attempt=attempt + 1,
            attempt_ceiling=self._max_retries + 1,
            status=status,
            fault_type=fault_type,
        )

    # -- response shape --------------------------------------------------

    def _decode(self, raw: bytes, expected: int) -> tuple[tuple[float, ...], ...]:
        """Read the vectors out of a response, refusing any other shape.

        The response is placed back into request order by the index each entry
        carries rather than by its position in the answer, because the ordering
        guarantee this provider makes to its caller must not depend on the
        service's own serialisation order.
        """
        try:
            decoded: object = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as fault:
            raise _unavailable("answered a body that is not valid encoded text") from fault
        if not isinstance(decoded, Mapping):
            raise _unavailable("answered a body of an unexpected shape")
        entries = decoded.get("data")
        if not isinstance(entries, list) or len(entries) != expected:
            raise _unavailable("answered a vector count other than the requested one")

        vectors: list[tuple[float, ...] | None] = [None] * expected
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise _unavailable("answered an entry of an unexpected shape")
            index = entry.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < expected:
                raise _unavailable("answered an entry carrying no usable position")
            if vectors[index] is not None:
                raise _unavailable("answered one position twice")
            vectors[index] = self._read_vector(entry.get("embedding"))
        placed = tuple(vector for vector in vectors if vector is not None)
        if len(placed) != expected:
            raise _unavailable("answered fewer positions than were requested")
        return placed

    def _read_vector(self, value: object) -> tuple[float, ...]:
        """Read one vector, refusing a width other than the declared one.

        A width mismatch is a malformed answer rather than a storable vector. The
        startup gate compares the declared width against the width the schema
        fixes, so a provider whose answers drift from its declaration is caught
        here rather than by an insert failing one row at a time.
        """
        if not isinstance(value, list) or len(value) != self.dimensions:
            raise _unavailable("answered a vector of an unexpected width")
        values: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise _unavailable("answered a vector holding a value that is not a number")
            number = float(item)
            if not math.isfinite(number):
                raise _unavailable("answered a vector holding a value that is not finite")
            values.append(number)
        return tuple(values)


def build(configuration: Configuration) -> EmbeddingProvider:
    """Construct the delivered embedding implementation from resolved configuration.

    The declared return type is the protocol, so conformance is a static fact
    checked here rather than an assertion made at runtime. The credential is
    loaded through the role-aware loader, which reads a parameter name or an
    operator-provided file and nothing else, and the loaded value goes straight
    into the transport without passing through this module's own state.
    """
    return ExternalEmbeddingProvider(
        model_id=configuration.text("MOLT_EMBEDDING_MODEL_ID"),
        dimensions=configuration.integer("MOLT_EMBEDDING_DIMENSIONS"),
        batch_size=configuration.integer("MOLT_EMBEDDING_BATCH_SIZE"),
        transport=HttpsTransport(
            credential=load_credential(configuration, EMBEDDING_ROLE),
            timeout=float(configuration.integer("MOLT_PROVIDER_TIMEOUT_SECONDS")),
        ),
        max_retries=configuration.integer("MOLT_PROVIDER_MAX_RETRIES"),
    )
