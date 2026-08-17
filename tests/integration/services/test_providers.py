"""One live call per provider implementation per role, skipped where none answers.

Four calls are made here and nothing else is: an embedding call and a text call
against the documented default implementation of both roles, and an embedding call
and a text call against the two delivered implementations. Nothing is stubbed on
those four paths. A stub would make this module a second, worse copy of the unit
suite, and the whole point of it is to touch the thing the unit suite cannot.

**Every one of the four skips in this environment, and that is the correct
outcome.** On-demand inference quota is zero and non-adjustable on the delivered
account, a new-account restriction outside the operator's control, so no model
provider is reachable at all. What is therefore actually verified today is the
half that has to hold anyway: that an absent or refusing provider makes this
module skip with a message naming what was missing, and never makes it fail.
Nothing else in the suite depends on a provider being invocable, so a provider
that cannot be called must cost exactly this module its four tests and nothing
more.

**The gating is layered, and each layer skips at a different granularity.** The
`services` marker gates the four live calls on cloud access and a credential
source for each provider role, and the message names each absent key. That layer
is coarse by design: it answers *is anything configured at all*. Inside each
test the same question is then asked of one implementation alone, so an account
that gains quota for one provider and not the other exercises the one it can
rather than skipping both. The two stub-driven bound tests at the end carry no
marker, because a retry ceiling and a batch ceiling are observable without
touching a model and would be worth nothing if they only ran where a model
answers.

**Three conditions are distinguished, and all three skip.** A configuration fault
means nothing was called: no region, no model identifier, or no credential source
for the role, and the message names the key. A refusal means the credential was
configured and the model would not answer, and the message says so in those
words, because those are different operator problems and a single message for
both would send an operator looking in the wrong place. The refusal case is a
skip rather than a failure, deliberately: the zero quota is exactly a configured
credential in front of a refusing model, it is not adjustable, and turning it
into a failure would make one provider's availability a precondition of a green
run — which is the coupling the provider abstraction exists to remove. The third
condition, a provider that answers, is the one the assertions are about.

**The width assertion is the load-bearing one.** Both embedding implementations
are held to a returned width of exactly 1024, which is what makes the stored
column and the distributed vector index safe across a provider change, and it is
the same comparison the Provider_Selector's startup gate makes before a single
vector is written.

**The text assertions are about the token accounting rather than the text.** Both
implementations are asked to report a cache-creation count and a cache-read count,
because the Adjudicator records both per batch and a field nobody can read makes
the cache hit ratio unmeasurable and the recorded cost fiction. The stable prefix
is built to the configured minimum cacheable prefix length so the counts are
populated rather than zero, and the same prompt is sent twice, because a field
that only ever reads zero is not a field that is known to exist: the first call
has to charge a cache write and the second has to charge a cache read.

**What a full run costs, where one is possible.** Two embedding calls on one short
excerpt. Two text probes at the smallest output ceiling each interface accepts.
Four text calls, two per implementation, each carrying a prefix at the configured
minimum cacheable length of 16384 bytes, of which the two repeat calls are charged
largely as cache reads. The default implementation's probe answers for every role
that names a model, so where both roles select it the text probe costs one extra
minimal embedding call. Nothing here loops, nothing retries a live call beyond the
implementation's own bounded chain, and no test scales its call count with
anything.

Every model identifier, region, and address on the stub-driven paths is obviously
synthetic. No credential value is read anywhere here, and no configuration is
written back to the process environment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Final
from uuid import uuid4

import pytest

from molt.config.resolve import ConfigError, Configuration, load_configuration
from molt.errors import ModelUnavailableError
from molt.providers import (
    SCHEMA_VECTOR_DIMENSIONS,
    EmbeddingProvider,
    ProbeLike,
    Prompt,
    TextProvider,
    TextResultLike,
)
from molt.providers.external_embedding import ExternalEmbeddingProvider
from molt.providers.external_text import ExternalTextProvider
from molt.providers.registry import (
    EMBEDDING_ROLE,
    TEXT_ROLE,
    load_embedding_builder,
    load_text_builder,
)
from molt.providers.selector import EMBEDDING_PROVIDER_ENV, TEXT_PROVIDER_ENV

# The two registry keys, named once. The first is the documented default for both
# roles; the second is what the delivered configuration selects for both.
DEFAULT_NAME: Final[str] = "bedrock"
DELIVERED_NAME: Final[str] = "external"

# The width the schema fixes, written as the literal the requirement states so a
# reader sees the number rather than a constant standing in for it. The assertion
# below ties it to the constant the startup gate compares against, so the two
# cannot drift apart.
REQUIRED_WIDTH: Final[int] = 1024

# The configured minimum cacheable prefix length. A prefix this long is what makes
# the cache-creation and cache-read counts non-zero: below the delivered model's
# own token floor a marked boundary is ignored, the counts read zero, and an
# absent field and a zero field become indistinguishable.
MINIMUM_CACHEABLE_PREFIX_BYTES: Final[int] = 16384

# The configured retry ceiling and batch ceiling, both restated as the literals
# the requirement states. Each is checked against the configuration surface's own
# default in the test that uses it.
RETRY_CEILING: Final[int] = 3
BATCH_CEILING: Final[int] = 25

# One representative text, source-code shaped because residue detection searches
# for semantically similar source code and a representative call should look like
# the calls the running system makes.
REPRESENTATIVE_TEXT: Final[str] = "def merge(left, right):\n    return sorted(left + right)\n"

# The selection key each role is named under, so the key spellings stay in the
# module that owns them.
_SELECTION_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        EMBEDDING_ROLE: EMBEDDING_PROVIDER_ENV,
        TEXT_ROLE: TEXT_PROVIDER_ENV,
    }
)

# Values used only on the stub-driven paths. A region, a model identifier, and a
# service address are configuration values, so none of the real ones appears here.
SYNTHETIC_MODEL: Final[str] = "synthetic-model"

# A narrow width for the batching test, where the width is not the subject and a
# realistic one would only make the scripted answers large.
NARROW_WIDTH: Final[int] = 4

# The one status the delivered transports read as an answer.
STATUS_OK: Final[int] = 200

# How many texts the batching test asks for: more than twice the ceiling, so the
# final group is a partial one and the ceiling is observed rather than assumed.
BATCHED_TEXT_COUNT: Final[int] = 60


# ---------------------------------------------------------------------------
# Per-implementation gating
# ---------------------------------------------------------------------------


def _view(role: str, name: str) -> Configuration:
    """A configuration view naming one implementation as the one this role selected.

    The overlay is what makes a per-implementation call possible at all: a builder
    reads the selection keys to learn whether its role is in play, and only a role
    in play is required to name a model. Naming the implementation under test in
    its role's key therefore exercises it whichever implementation the delivered
    configuration actually selects, and it does so without writing anything back
    to the process environment and without reading any credential value.

    The overlay sits on top of a copy of the process environment rather than
    replacing it, so every other key — the region, the model identifiers, the
    credential source names — resolves exactly as it would in a running process.
    """
    overlay = dict(os.environ)
    overlay[_SELECTION_KEYS[role]] = name
    return load_configuration(environ=overlay)


def _unconfigured_reason(role: str, name: str, fault: ConfigError) -> str:
    """The skip message for an implementation nothing was configured for.

    Nothing was called. The fault names the absent key, and the key is what the
    operator sets, so the message carries it verbatim; no credential value can
    appear in it because every one of these faults is raised naming a source
    rather than a value.
    """
    return (
        f"the {name} {role} implementation is not configured, so no call was "
        f"made: {fault}. No other suite depends on this provider."
    )


def _refused_reason(role: str, name: str, fault: ModelUnavailableError) -> str:
    """The skip message for a configured implementation whose model would not answer.

    This is deliberately not the message above and deliberately not a failure. A
    credential was configured and the model refused, which on the delivered
    account is the zero and non-adjustable on-demand inference quota, and which
    for any other operator is a quota, a region, or a model-access problem rather
    than a missing setting.
    """
    return (
        f"the {name} {role} implementation is configured and the model refused "
        f"the call: {fault}. On-demand inference quota of zero on the account is "
        "this exact condition. No other suite depends on this provider."
    )


def _embedding_implementation(name: str) -> EmbeddingProvider:
    """The named embedding implementation, or a skip naming what is unconfigured."""
    try:
        return load_embedding_builder(name)(_view(EMBEDDING_ROLE, name))
    except ConfigError as fault:
        pytest.skip(_unconfigured_reason(EMBEDDING_ROLE, name, fault))


def _text_implementation(name: str) -> TextProvider:
    """The named text implementation, or a skip naming what is unconfigured."""
    try:
        return load_text_builder(name)(_view(TEXT_ROLE, name))
    except ConfigError as fault:
        pytest.skip(_unconfigured_reason(TEXT_ROLE, name, fault))


def _embed(provider: EmbeddingProvider, name: str, text: str) -> Sequence[float]:
    """One live embedding call, or a skip naming the refusal."""
    try:
        vectors = provider.embed((text,))
    except ModelUnavailableError as fault:
        pytest.skip(_refused_reason(EMBEDDING_ROLE, name, fault))
    assert len(vectors) == 1, "one text was sent, so one vector is owed"
    return vectors[0]


def _probe(provider: TextProvider, name: str) -> ProbeLike:
    """One live probe, or a skip naming the refusal.

    The probe is what the prompt-cache capability comes from: the default
    implementation starts with the capability false and sets it from what the model
    itself reported, so a boundary is marked only where marking it means something.
    Asserting the capability instead of probing for it would be asserting the one
    thing this module is here to find out.
    """
    try:
        return provider.probe()
    except ModelUnavailableError as fault:
        pytest.skip(_refused_reason(TEXT_ROLE, name, fault))


def _generate(provider: TextProvider, name: str, prompt: Prompt) -> TextResultLike:
    """One live text call, or a skip naming the refusal."""
    try:
        return provider.generate(prompt)
    except ModelUnavailableError as fault:
        pytest.skip(_refused_reason(TEXT_ROLE, name, fault))


def _cacheable_prefix() -> str:
    """A stable prefix at the configured minimum cacheable length, unique per run.

    The length is the point: at the configured floor the delivered model caches the
    prefix, so the first call charges a cache write and the second charges a cache
    read, and both counts come back non-zero. The per-run uniquifier is the other
    half of the same argument — a prefix left over from an earlier run would be
    read from the cache on the first call, and the cache-creation count would then
    read zero for a reason that has nothing to do with the field existing.
    """
    unit = f"Decide whether the excerpt restates the query excerpt. Case {uuid4().hex}. "
    unit_bytes = len(unit.encode())
    repeats = -(-MINIMUM_CACHEABLE_PREFIX_BYTES // unit_bytes)
    prefix = unit * repeats
    assert len(prefix.encode()) >= MINIMUM_CACHEABLE_PREFIX_BYTES
    return prefix


def _assert_cache_counts_are_reported(provider: TextProvider, name: str) -> None:
    """Two live calls sharing one prefix, asserting both cache counts are real.

    The prompt is byte-identical across the two calls and the boundary is asked
    for, which is the arrangement the Adjudicator uses for every candidate sharing
    one query artifact. Where the model reports the capability, the first call has
    to charge a cache write and the second a cache read; where it reports none,
    both counts read zero, which is a measurement of no cache activity rather than
    a gap, and the fields are still there to be read.
    """
    probe = _probe(provider, name)
    prompt = Prompt(
        stable_prefix=_cacheable_prefix(),
        variable_suffix=REPRESENTATIVE_TEXT,
        cache_boundary=True,
    )
    first = _generate(provider, name, prompt)
    second = _generate(provider, name, prompt)

    for result in (first, second):
        assert result.text, "a text call that completed answers some text"
        assert result.model_id, "a result records the model that produced it"
        assert result.input_tokens > 0, "a prompt this long is charged some input"
        assert result.output_tokens >= 0
        assert result.cache_creation_tokens >= 0
        assert result.cache_read_tokens >= 0

    if probe.supports_prompt_cache:
        assert first.cache_creation_tokens > 0, (
            "a marked boundary at the configured minimum cacheable prefix length "
            "charges a cache write, so the cache-creation field the Adjudicator "
            "records is populated rather than defaulted"
        )
        assert second.cache_read_tokens > 0, (
            "the second call sends a byte-identical prefix, so it charges a cache "
            "read, which is what makes the cache-read field the Adjudicator "
            "records known to exist"
        )
    else:
        assert first.cache_creation_tokens == 0
        assert first.cache_read_tokens == 0


# ---------------------------------------------------------------------------
# The four live calls
# ---------------------------------------------------------------------------


def test_the_schema_width_matches_the_width_the_startup_gate_compares_against() -> None:
    """The literal both embedding assertions use is the width the gate requires."""
    assert REQUIRED_WIDTH == SCHEMA_VECTOR_DIMENSIONS


@pytest.mark.services
def test_the_default_embedding_implementation_answers_the_schema_width() -> None:
    provider = _embedding_implementation(DEFAULT_NAME)
    assert provider.dimensions == REQUIRED_WIDTH
    vector = _embed(provider, DEFAULT_NAME, REPRESENTATIVE_TEXT)
    assert len(vector) == REQUIRED_WIDTH


@pytest.mark.services
def test_the_delivered_embedding_implementation_answers_the_schema_width() -> None:
    provider = _embedding_implementation(DELIVERED_NAME)
    assert provider.dimensions == REQUIRED_WIDTH
    vector = _embed(provider, DELIVERED_NAME, REPRESENTATIVE_TEXT)
    assert len(vector) == REQUIRED_WIDTH


@pytest.mark.services
def test_the_default_text_implementation_reports_the_cache_token_counts() -> None:
    _assert_cache_counts_are_reported(_text_implementation(DEFAULT_NAME), DEFAULT_NAME)


@pytest.mark.services
def test_the_delivered_text_implementation_reports_the_cache_token_counts() -> None:
    _assert_cache_counts_are_reported(_text_implementation(DELIVERED_NAME), DELIVERED_NAME)


# ---------------------------------------------------------------------------
# The two bounds, observed without a model
# ---------------------------------------------------------------------------


class FailingTransport:
    """A transport that never answers, counting the attempts made against it."""

    def __init__(self) -> None:
        self.attempts = 0

    def send(self, body: bytes) -> tuple[int, bytes]:
        """Fail as a transport fault does, which is the kind worth another attempt."""
        self.attempts += 1
        raise OSError(f"no route, on the attempt carrying {len(body)} bytes")


class RecordingTransport:
    """A transport recording how many texts each request carried, then answering it."""

    def __init__(self, *, width: int) -> None:
        self.batch_sizes: list[int] = []
        self._width = width

    def send(self, body: bytes) -> tuple[int, bytes]:
        """Answer one vector per input text, at the width the request asked for."""
        decoded: object = json.loads(body)
        assert isinstance(decoded, Mapping)
        texts = decoded.get("input")
        assert isinstance(texts, list)
        self.batch_sizes.append(len(texts))
        entries = [
            {"index": index, "embedding": [0.0] * self._width} for index in range(len(texts))
        ]
        return STATUS_OK, json.dumps({"data": entries}).encode()


def _recorded_sleep(delays: list[float]) -> Callable[[float], None]:
    """A stand-in for backing off, so a bounded retry chain costs no real seconds."""

    def record(seconds: float) -> None:
        delays.append(seconds)

    return record


def test_the_delivered_embedding_retries_are_bounded_by_the_configured_count() -> None:
    """The attempt chain ends at the configured retry count, and no sooner.

    The configured count is the number of attempts after the first, so three
    retries is four attempts in total and every one of them is a failure the
    caller is entitled to another go at. The bound is what keeps a fail-closed
    caller from waiting on an unbounded chain, and the delays are recorded rather
    than slept, so observing the bound costs no wall time.
    """
    assert Configuration(environ={}).integer("MOLT_PROVIDER_MAX_RETRIES") == RETRY_CEILING

    transport = FailingTransport()
    delays: list[float] = []
    provider = ExternalEmbeddingProvider(
        model_id=SYNTHETIC_MODEL,
        dimensions=REQUIRED_WIDTH,
        batch_size=BATCH_CEILING,
        transport=transport,
        max_retries=RETRY_CEILING,
        sleep=_recorded_sleep(delays),
    )

    with pytest.raises(ModelUnavailableError):
        provider.embed((REPRESENTATIVE_TEXT,))

    assert transport.attempts - 1 == RETRY_CEILING
    assert len(delays) == RETRY_CEILING
    assert delays == sorted(delays), "a backoff chain does not shorten"


def test_the_delivered_text_retries_are_bounded_by_the_configured_count() -> None:
    """The same bound on the text role, which carries its own attempt chain."""
    transport = FailingTransport()
    delays: list[float] = []
    provider = ExternalTextProvider(
        model_id=SYNTHETIC_MODEL,
        transport=transport,
        max_retries=RETRY_CEILING,
        sleep=_recorded_sleep(delays),
    )

    with pytest.raises(ModelUnavailableError):
        provider.generate(Prompt(stable_prefix="Decide.", variable_suffix=REPRESENTATIVE_TEXT))

    assert transport.attempts - 1 == RETRY_CEILING
    assert len(delays) == RETRY_CEILING


def test_the_delivered_embedding_batches_no_more_than_the_configured_size() -> None:
    """No request carries more than the configured number of texts, in input order.

    The ceiling is what keeps one call's failure from costing an unbounded number
    of texts, and the count of texts asked for here is more than twice the ceiling
    so the final group is a partial one rather than a coincidentally exact fit.
    """
    assert Configuration(environ={}).integer("MOLT_EMBEDDING_BATCH_SIZE") == BATCH_CEILING

    transport = RecordingTransport(width=NARROW_WIDTH)
    provider = ExternalEmbeddingProvider(
        model_id=SYNTHETIC_MODEL,
        dimensions=NARROW_WIDTH,
        batch_size=BATCH_CEILING,
        transport=transport,
        max_retries=RETRY_CEILING,
    )

    texts = tuple(f"case {index}" for index in range(BATCHED_TEXT_COUNT))
    vectors = provider.embed(texts)

    assert len(vectors) == BATCHED_TEXT_COUNT
    assert transport.batch_sizes == [BATCH_CEILING, BATCH_CEILING, 10]
    assert max(transport.batch_sizes) <= BATCH_CEILING
    assert sum(transport.batch_sizes) == BATCHED_TEXT_COUNT
