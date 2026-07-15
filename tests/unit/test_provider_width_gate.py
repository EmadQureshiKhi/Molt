"""Unit tests for the startup width gate.

The gate exists because a width disagreement discovered by the stored column's own
constraint is discovered one insert at a time, after a run has already begun
writing. These tests drive the shared embedding stub at a width other than the one
the schema fixes and pin down three things.

**The refusal carries both widths.** The reported width and the required width are
each present as an attribute and each appear in what reaches the error stream,
because the operator has to see what was reported and what is required in order to
choose a different model. A message naming only the fault would leave the next
action unclear.

**The process is left with a non-zero status.** The gate is expressed twice: one
form raises so a caller can handle the refusal, and one form prints and leaves the
process non-zero. The second is driven here through the interpreter's own exit
rather than through a subprocess, which is what lets the printed text be read.

**No vector exists by the time the refusal is reached.** The stub records every
text it was asked about, so an untouched record is evidence that nothing was
embedded; a helper that would embed immediately after the gate is driven as well,
and its collected vectors stay empty. The text provider is wrapped in a
probe-counting delegate, so the ordering claim — the width comparison happens
before the other provider is contacted — is observed rather than assumed.

The stub is the shared fixture rather than a stub of this module's own, and its
width is the one thing moved, because everything else about it is what the
protocols were shaped to match.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Final, Protocol

import pytest

from molt.config.resolve import Configuration
from molt.errors import ProviderWidthMismatchError
from molt.providers import (
    SCHEMA_VECTOR_DIMENSIONS,
    EmbeddingProvider,
    ProbeLike,
    Prompt,
    ProviderProbe,
    TextProvider,
    TextResultLike,
)
from molt.providers.selector import (
    CONFIGURATION_EXIT_STATUS,
    PROMPT_CACHE_CAPABILITY,
    ConsumingRole,
    validate_at_startup,
    validate_at_startup_or_exit,
)

# Widths a misconfigured model could report: narrower, wider, degenerate, and the
# two immediately either side of the required one, which are the ones a column
# constraint would catch latest.
WRONG_WIDTHS: Final[tuple[int, ...]] = (1, 256, 768, 1023, 1025, 1536, 3072)


class EmbeddingStub(EmbeddingProvider, Protocol):
    """The shared embedding fixture: the protocol, plus the record of its calls."""

    calls: list[tuple[str, ...]]


class TextStub(TextProvider, Protocol):
    """The shared text fixture: the protocol, plus the record of its calls."""

    calls: list[tuple[str, str]]


class ProbeCountingText:
    """A text provider counting probes and delegating every answer to the one it wraps.

    Wrapping the shared fixture rather than replacing it keeps the answers the
    suites are driven with intact while making the one question this module asks —
    was the other provider contacted at all — observable.
    """

    __slots__ = ("_inner", "model_id", "name", "probes", "supports_prompt_cache")

    def __init__(self, inner: TextProvider) -> None:
        self.name = inner.name
        self.model_id = inner.model_id
        self.supports_prompt_cache = inner.supports_prompt_cache
        self._inner = inner
        self.probes = 0

    def generate(self, prompt: Prompt) -> TextResultLike:
        """Answer a prompt exactly as the wrapped provider does."""
        return self._inner.generate(prompt)

    def probe(self) -> ProbeLike:
        """Count this probe, then answer as the wrapped provider does."""
        self.probes += 1
        return self._inner.probe()


class SilentWidth:
    """An embedding provider whose probe reports reachability and no width at all.

    It delegates its declared width to the provider it wraps, so the gate has only
    the declaration to go on, and it counts embedding calls so a call made before
    the comparison would be visible rather than silent.
    """

    __slots__ = ("_inner", "dimensions", "embed_calls", "model_id", "name")

    def __init__(self, inner: EmbeddingProvider) -> None:
        self.name = inner.name
        self.model_id = inner.model_id
        self.dimensions = inner.dimensions
        self._inner = inner
        self.embed_calls = 0

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Answer as the wrapped provider does, counting the call."""
        self.embed_calls += 1
        return self._inner.embed(texts)

    def probe(self) -> ProbeLike:
        """Report reachability with the width left absent."""
        return ProviderProbe(name=self.name, model_id=self.model_id, reachable=True)


def _configuration(**environ: str) -> Configuration:
    """A resolved configuration over an explicit environment and no file values."""
    return Configuration(environ=environ, file_values={})


def _embed_after_the_gate(
    configuration: Configuration,
    embedding: EmbeddingStub,
    text: TextProvider,
    written: list[Sequence[float]],
) -> None:
    """Run the gate and only then embed, collecting whatever vectors result.

    This is the shape every calling component has: the gate runs at startup and
    the embedding work happens after it. Driving it here is what makes "no
    Embedding was written" an observation rather than an inference.
    """
    validate_at_startup_or_exit(configuration, embedding, text, stream=io.StringIO())
    written.extend(embedding.embed(["one", "two"]))


# --------------------------------------------------------------------------
# A width other than the one the schema fixes is refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize("width", WRONG_WIDTHS)
def test_a_reported_width_other_than_the_required_one_is_refused_carrying_both(
    width: int,
    stub_embedding_provider: EmbeddingStub,
    stub_text_provider: TextStub,
) -> None:
    stub_embedding_provider.dimensions = width
    counting = ProbeCountingText(stub_text_provider)

    with pytest.raises(ProviderWidthMismatchError) as caught:
        validate_at_startup(_configuration(), stub_embedding_provider, counting)

    refusal = caught.value
    assert refusal.reported == width
    assert refusal.required == SCHEMA_VECTOR_DIMENSIONS
    message = str(refusal)
    assert str(width) in message
    assert str(SCHEMA_VECTOR_DIMENSIONS) in message
    assert stub_embedding_provider.calls == []
    assert counting.probes == 0


@pytest.mark.parametrize("width", WRONG_WIDTHS)
def test_the_refusal_leaves_a_non_zero_status_and_prints_both_widths(
    width: int,
    stub_embedding_provider: EmbeddingStub,
    stub_text_provider: TextStub,
) -> None:
    stub_embedding_provider.dimensions = width
    counting = ProbeCountingText(stub_text_provider)
    stream = io.StringIO()

    with pytest.raises(SystemExit) as caught:
        validate_at_startup_or_exit(
            _configuration(), stub_embedding_provider, counting, stream=stream
        )

    assert caught.value.code == CONFIGURATION_EXIT_STATUS
    assert caught.value.code != 0
    printed = stream.getvalue()
    assert str(width) in printed
    assert str(SCHEMA_VECTOR_DIMENSIONS) in printed
    assert "reported width" in printed
    assert "required width" in printed
    assert stub_embedding_provider.calls == []
    assert counting.probes == 0


@pytest.mark.parametrize("width", WRONG_WIDTHS)
def test_no_embedding_is_produced_when_the_gate_refuses(
    width: int,
    stub_embedding_provider: EmbeddingStub,
    stub_text_provider: TextStub,
) -> None:
    stub_embedding_provider.dimensions = width
    written: list[Sequence[float]] = []

    with pytest.raises(SystemExit):
        _embed_after_the_gate(
            _configuration(), stub_embedding_provider, stub_text_provider, written
        )

    assert written == []
    assert stub_embedding_provider.calls == []


@pytest.mark.parametrize("width", WRONG_WIDTHS)
def test_the_declared_width_is_refused_even_where_the_probe_answers_none(
    width: int,
    stub_embedding_provider: EmbeddingStub,
    stub_text_provider: TextStub,
) -> None:
    # A provider answering no width at all is held to the width it declares,
    # because the declared width is what every later call would produce.
    stub_embedding_provider.dimensions = width
    silent = SilentWidth(stub_embedding_provider)
    with pytest.raises(ProviderWidthMismatchError) as caught:
        validate_at_startup(_configuration(), silent, stub_text_provider)
    assert caught.value.reported == width
    assert caught.value.required == SCHEMA_VECTOR_DIMENSIONS
    assert silent.embed_calls == 0


# --------------------------------------------------------------------------
# The required width passes, so the refusal is about the width and nothing else
# --------------------------------------------------------------------------


def test_the_required_width_passes_and_the_startup_report_records_every_role(
    stub_embedding_provider: EmbeddingStub,
    stub_text_provider: TextStub,
) -> None:
    counting = ProbeCountingText(stub_text_provider)
    report = validate_at_startup(_configuration(), stub_embedding_provider, counting)

    assert stub_embedding_provider.dimensions == SCHEMA_VECTOR_DIMENSIONS
    assert report.embedding.dimensions == SCHEMA_VECTOR_DIMENSIONS
    assert report.embedding.reachable is True
    assert report.text.reachable is True
    assert counting.probes == 1
    assert {selection.role for selection in report.roles} == set(ConsumingRole)
    assert report.capability(PROMPT_CACHE_CAPABILITY).available is True
    assert report.prompt_cache_available is True
    # The gate probes rather than embeds, so passing it writes nothing either.
    assert stub_embedding_provider.calls == []


def test_the_configured_model_identifier_is_recorded_per_consuming_role(
    stub_embedding_provider: EmbeddingStub,
    stub_text_provider: TextStub,
) -> None:
    configuration = _configuration(
        MOLT_EMBEDDING_MODEL_ID="fake-embedding-model",
        MOLT_ADJUDICATION_MODEL_ID="fake-adjudication-model",
        MOLT_REWRITE_MODEL_ID="fake-rewrite-model",
    )
    report = validate_at_startup(configuration, stub_embedding_provider, stub_text_provider)
    assert report.selection(ConsumingRole.EMBEDDER).model_id == "fake-embedding-model"
    assert report.selection(ConsumingRole.ADJUDICATOR).model_id == "fake-adjudication-model"
    assert report.selection(ConsumingRole.REDACTION_REWRITER).model_id == "fake-rewrite-model"
