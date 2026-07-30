"""Unit tests for the Embedder: batching, scaling, the retry bound, and the drain.

Nothing here calls a model and nothing here waits. Provider stubs answer from
computed vectors, a recording sink stands in for the store, and the sleeper is a
counter, so every claim below is asserted by reading what the Embedder did.

Six claims are checked.

A provider call carries at most twenty-five texts, whatever the caller asks for,
and a size above that ceiling is refused rather than honoured.

Every vector is scaled to unit length, including every vector from the
deliberately non-normalising stub, which is the stub that makes the scaling
observable at all: the delivered implementation already returns unit vectors, so a
suite drawing only from faithful stubs would pass with the scaling deleted.

A vector of zero length is refused rather than given an invented direction, and a
provider answering with one leaves its Artifacts owing a vector.

A failing call is attempted once and retried at most three times on a growing
schedule, and the waiting is driven rather than waited out.

A drain walks the sweep's ascending creation order across batch boundaries, and a
batch that will not answer stops the drain rather than letting a later batch
overtake an earlier one. Nothing is marked failed and nothing is lost.

Every row carries the selected provider name beside the model identifier, a
Derived_Artifact is moved out of the pending state once its vector lands, and an
Event is not, because no role holds `UPDATE` on the Ledger.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest
from tests.property.embedding_provider_stubs import (
    NonNormalisingEmbeddingProvider,
    StubProbeReport,
    non_unit_vector,
)

from molt.config.resolve import Configuration, InvalidConfigValueError
from molt.embed import (
    BACKOFF_SECONDS,
    CALLS_METRIC,
    FAILURES_METRIC,
    MAX_BATCH_TEXTS,
    MAX_RETRIES,
    PENDING_BACKLOG_METRIC,
    DrainOutcome,
    Embedder,
    EmbeddingSink,
    TextSource,
    batches,
    unit_scale,
)
from molt.errors import (
    EmbeddingAlreadyStoredError,
    ModelUnavailableError,
    ProviderWidthMismatchError,
    StoreError,
)
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind
from molt.models.event import EmbeddingState
from molt.providers import EmbeddingProvider
from molt.store.embeddings import EmbeddingWrite, PendingArtifact
from molt.telemetry import current, reset

# A fixed instant with an offset, so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[timedelta] = timedelta(days=90)

# How far a stored norm may sit from one, matching the write path's own tolerance.
TOLERANCE: Final[float] = 1e-6

# A width the schema does not hold, for the construction refusal.
NARROW_WIDTH: Final[int] = 512


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecordingSink:
    """A store surface that records writes and transitions instead of sending them.

    The pending set is held as a list so the sweep answers in the order it was
    given, which is the ascending creation order the real sweep guarantees.
    """

    owed: list[PendingArtifact] = field(default_factory=list)
    written: list[EmbeddingWrite] = field(default_factory=list)
    transitions: list[tuple[UUID, EmbeddingState]] = field(default_factory=list)
    stored: set[tuple[UUID, str, str]] = field(default_factory=set)
    refuse_write: bool = False

    def pending_artifacts(self, *, limit: int | None = None) -> Sequence[PendingArtifact]:
        """The Artifacts owing a vector, oldest first and bounded by the limit."""
        bound = len(self.owed) if limit is None else limit
        return tuple(self.owed[:bound])

    def write_embedding(self, request: EmbeddingWrite) -> UUID:
        """Record one vector, refusing a repeat of an artifact-provider-model triple.

        The repeat is refused with the named class the write path raises for the
        uniqueness constraint, and the unwritable cluster with the family base, so
        the drain's two branches are driven by the two types rather than by two
        wordings of one type.
        """
        if self.refuse_write:
            raise StoreError("the memory store could not be written to")
        key = (request.artifact_id, request.provider, request.model_id)
        if key in self.stored:
            raise EmbeddingAlreadyStoredError(
                request.artifact_id, request.provider, request.model_id
            )
        self.stored.add(key)
        self.written.append(request)
        return uuid4()

    def mark_embedding_state(
        self,
        artifact_id: UUID,
        client_id: UUID,
        state: EmbeddingState,
    ) -> EmbeddingState | None:
        """Record one state transition and report the state as taken."""
        assert client_id == CLIENT
        self.transitions.append((artifact_id, state))
        return state


@dataclass(slots=True)
class CountingSleeper:
    """A sleeper that records the delays it was asked for and waits for none."""

    waits: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        """Record one delay."""
        self.waits.append(seconds)


@dataclass(slots=True)
class FailingProvider:
    """A provider failing a stated number of calls before it answers.

    The count is per instance rather than per batch, which is what a provider
    recovering mid-drain looks like: the calls that fail are the first ones,
    whichever batch they carried.
    """

    failures: int
    name: str = "failing-stub"
    model_id: str = "failing-stub-embedding"
    dimensions: int = EMBEDDING_DIMENSION
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Answer with non-unit vectors once the stated failures are used up."""
        self.calls.append(tuple(texts))
        if len(self.calls) <= self.failures:
            raise RuntimeError("the provider refused the call")
        return [non_unit_vector(text, self.dimensions) for text in texts]

    def probe(self) -> StubProbeReport:
        """Report reachability and the declared width."""
        return StubProbeReport(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


@dataclass(slots=True)
class ZeroVectorProvider:
    """A provider answering with vectors that have no direction to scale."""

    name: str = "zero-stub"
    model_id: str = "zero-stub-embedding"
    dimensions: int = EMBEDDING_DIMENSION
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Answer one zero vector per text."""
        self.calls.append(tuple(texts))
        return [tuple([0.0] * self.dimensions) for _ in texts]

    def probe(self) -> StubProbeReport:
        """Report reachability and the declared width."""
        return StubProbeReport(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


@dataclass(slots=True)
class MiscountingProvider:
    """A provider answering with one vector fewer than it was asked for."""

    name: str = "miscounting-stub"
    model_id: str = "miscounting-stub-embedding"
    dimensions: int = EMBEDDING_DIMENSION
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Drop the last vector, so no vector pairs reliably with its text."""
        self.calls.append(tuple(texts))
        return [non_unit_vector(text, self.dimensions) for text in texts[:-1]]

    def probe(self) -> StubProbeReport:
        """Report reachability and the declared width."""
        return StubProbeReport(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


@dataclass(slots=True)
class NarrowProvider:
    """A provider declaring a width the schema does not hold."""

    name: str = "narrow-stub"
    model_id: str = "narrow-stub-embedding"
    dimensions: int = NARROW_WIDTH

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Answer at the declared width, which construction never reaches."""
        return [tuple([1.0] * self.dimensions) for _ in texts]

    def probe(self) -> StubProbeReport:
        """Report reachability and the width the startup gate refuses."""
        return StubProbeReport(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

CLIENT: Final[UUID] = UUID(int=1000)


def pending(index: int, *, kind: ArtifactKind = ArtifactKind.DERIVED_ARTIFACT) -> PendingArtifact:
    """One Artifact owing a vector, created `index` seconds after the fixed instant."""
    return PendingArtifact(
        artifact_id=UUID(int=index + 1),
        artifact_kind=kind,
        client_id=CLIENT,
        created_at=MOMENT + timedelta(seconds=index),
    )


def texts_for(group: Sequence[PendingArtifact]) -> Mapping[UUID, str]:
    """Text per Artifact, derived from the identifier so it is reproducible."""
    return {artifact.artifact_id: f"body of {artifact.artifact_id}" for artifact in group}


def build_embedder(
    provider: EmbeddingProvider,
    sink: EmbeddingSink,
    *,
    batch_size: int = MAX_BATCH_TEXTS,
    max_retries: int = MAX_RETRIES,
    sleeper: CountingSleeper | None = None,
    texts: TextSource = texts_for,
) -> Embedder:
    """An Embedder over stubs, with no waiting and no jitter."""
    return Embedder(
        provider=provider,
        sink=sink,
        texts=texts,
        expiry=EXPIRY,
        batch_size=batch_size,
        max_retries=max_retries,
        sleep=CountingSleeper() if sleeper is None else sleeper,
        jitter=lambda low, _: low,
    )


def norm_of(vec: Sequence[float]) -> float:
    """The L2 norm of a vector."""
    return math.sqrt(math.fsum(component * component for component in vec))


# ---------------------------------------------------------------------------
# The scaling
# ---------------------------------------------------------------------------


def test_a_non_unit_vector_is_scaled_to_unit_length() -> None:
    """The step that reconciles an L2-ordered index with cosine thresholds runs."""
    answered = non_unit_vector("a body of source code")

    assert abs(norm_of(answered) - 1.0) > TOLERANCE
    assert abs(norm_of(unit_scale(answered)) - 1.0) <= TOLERANCE


def test_scaling_preserves_direction() -> None:
    """Only the magnitude changes, so a neighbour ordering is unaffected by scaling."""
    answered = non_unit_vector("another body of source code")
    scaled = unit_scale(answered)
    factor = norm_of(answered)

    pairs = zip(scaled, answered, strict=True)
    assert all(abs(component * factor - original) <= 1e-9 for component, original in pairs)


def test_a_zero_vector_is_refused_rather_than_given_a_direction() -> None:
    """A vector with no direction gets none invented for it."""
    with pytest.raises(ValueError, match="zero length"):
        unit_scale(tuple([0.0] * EMBEDDING_DIMENSION))


def test_a_vector_holding_a_non_finite_component_is_refused() -> None:
    """A norm computed over an infinity is meaningless rather than merely wrong."""
    with pytest.raises(ValueError, match="finite"):
        unit_scale((float("inf"), *([0.0] * (EMBEDDING_DIMENSION - 1))))


# ---------------------------------------------------------------------------
# The batching bound
# ---------------------------------------------------------------------------


def test_texts_are_grouped_into_consecutive_runs_at_the_ceiling() -> None:
    """Sixty texts are three calls of twenty-five, twenty-five, and ten, in order."""
    texts = [f"text {index}" for index in range(60)]

    grouped = list(batches(texts, MAX_BATCH_TEXTS))

    assert [len(group) for group in grouped] == [25, 25, 10]
    assert [text for group in grouped for text in group] == texts


def test_a_batch_above_the_ceiling_is_refused() -> None:
    """The cost bound of the requirement is a maximum, so nothing may raise it."""
    with pytest.raises(ValueError, match="at most 25"):
        list(batches(["one text"], MAX_BATCH_TEXTS + 1))


def test_an_embedder_refuses_a_batch_size_above_the_ceiling() -> None:
    """The same ceiling holds at construction, before any call is assembled."""
    with pytest.raises(ValueError, match="1 to 25"):
        build_embedder(NonNormalisingEmbeddingProvider(), RecordingSink(), batch_size=26)


def test_an_embedder_refuses_a_provider_declaring_another_width() -> None:
    """A width the schema does not hold is refused before any vector is produced."""
    with pytest.raises(ProviderWidthMismatchError) as caught:
        build_embedder(NarrowProvider(), RecordingSink())

    assert caught.value.reported == NARROW_WIDTH
    assert caught.value.required == EMBEDDING_DIMENSION


def test_embedding_many_texts_sends_bounded_calls_and_keeps_the_order() -> None:
    """The vectors come back paired with their texts, all of them unit length."""
    provider = NonNormalisingEmbeddingProvider()
    embedder = build_embedder(provider, RecordingSink())
    texts = [f"text {index}" for index in range(MAX_BATCH_TEXTS + 3)]

    vectors = embedder.embed_texts(texts)

    assert [len(call) for call in provider.calls] == [MAX_BATCH_TEXTS, 3]
    assert len(vectors) == len(texts)
    assert all(abs(norm_of(vector) - 1.0) <= TOLERANCE for vector in vectors)
    assert vectors[0] == unit_scale(non_unit_vector(texts[0]))
    assert vectors[-1] == unit_scale(non_unit_vector(texts[-1]))


def test_a_provider_answering_a_different_count_is_refused() -> None:
    """A vector cannot be paired with a text positionally if the counts differ."""
    provider = MiscountingProvider()
    embedder = build_embedder(provider, RecordingSink(), max_retries=0)

    with pytest.raises(ModelUnavailableError):
        embedder.embed_texts(["one", "two", "three"])


# ---------------------------------------------------------------------------
# The retry bound and the backoff
# ---------------------------------------------------------------------------


def test_a_transient_failure_is_retried_and_then_answered() -> None:
    """Two refusals then an answer is three calls and two waits, and no failure."""
    provider = FailingProvider(failures=2)
    sleeper = CountingSleeper()
    embedder = build_embedder(provider, RecordingSink(), sleeper=sleeper)

    vectors = embedder.embed_texts(["one text"])

    assert len(provider.calls) == 3
    assert sleeper.waits == [BACKOFF_SECONDS[0], BACKOFF_SECONDS[1]]
    assert abs(norm_of(vectors[0]) - 1.0) <= TOLERANCE


def test_a_persistent_failure_stops_at_the_retry_bound() -> None:
    """One attempt and three retries, on the growing schedule, and then no vector."""
    provider = FailingProvider(failures=99)
    sleeper = CountingSleeper()
    embedder = build_embedder(provider, RecordingSink(), sleeper=sleeper)

    with pytest.raises(ModelUnavailableError):
        embedder.embed_texts(["one text"])

    assert len(provider.calls) == MAX_RETRIES + 1
    assert sleeper.waits == list(BACKOFF_SECONDS)
    assert sleeper.waits == sorted(sleeper.waits)


def test_the_calls_and_failures_are_both_measured() -> None:
    """The ratio is what an operator reads, so both sides of it are counted."""
    reset()
    provider = FailingProvider(failures=1)
    embedder = build_embedder(provider, RecordingSink())

    embedder.embed_texts(["one text"])

    counters = current().counters()
    assert counters.get((CALLS_METRIC, ()), 0.0) == 2.0
    assert counters.get((FAILURES_METRIC, ()), 0.0) == 1.0


# ---------------------------------------------------------------------------
# The drain
# ---------------------------------------------------------------------------


def test_the_drain_produces_vectors_in_ascending_creation_order() -> None:
    """Across batch boundaries as well as inside them, oldest first."""
    sink = RecordingSink(owed=[pending(index) for index in range(MAX_BATCH_TEXTS + 5)])
    embedder = build_embedder(NonNormalisingEmbeddingProvider(), sink)

    outcome = embedder.drain(len(sink.owed))

    assert outcome.written == len(sink.owed)
    assert [write.artifact_id for write in sink.written] == [
        artifact.artifact_id for artifact in sink.owed
    ]
    assert all(abs(norm_of(write.vec) - 1.0) <= TOLERANCE for write in sink.written)


def test_every_written_row_carries_the_provider_beside_the_model() -> None:
    """A corpus embedded across a provider change stays distinguishable row by row."""
    provider = NonNormalisingEmbeddingProvider()
    sink = RecordingSink(owed=[pending(0), pending(1)])
    embedder = build_embedder(provider, sink)

    embedder.drain(2)

    assert {(write.provider, write.model_id) for write in sink.written} == {
        (provider.name, provider.model_id)
    }
    assert embedder.provider_name == provider.name
    assert embedder.model_id == provider.model_id


def test_a_second_provider_may_hold_its_own_vector_for_the_same_artifact() -> None:
    """The uniqueness the schema holds spans the provider, so the pair is admitted."""
    sink = RecordingSink(owed=[pending(0)])
    build_embedder(NonNormalisingEmbeddingProvider(), sink).drain(1)
    other = NonNormalisingEmbeddingProvider(name="other-stub", model_id="other-stub-embedding")

    outcome = build_embedder(other, sink).drain(1)

    assert outcome.written == 1
    assert len(sink.written) == 2
    assert {write.provider for write in sink.written} == {"non-normalising-stub", "other-stub"}


def test_a_failing_batch_stops_the_drain_rather_than_being_stepped_over() -> None:
    """A later batch never overtakes an earlier one that is still owed."""
    provider = FailingProvider(failures=MAX_RETRIES + 1)
    sink = RecordingSink(owed=[pending(index) for index in range(6)])
    embedder = build_embedder(provider, sink, batch_size=2)

    outcome = embedder.drain(6)

    assert outcome.written == 0
    assert outcome.deferred == 6
    assert sink.written == []
    assert sink.transitions == []
    assert len(provider.calls) == MAX_RETRIES + 1


def test_a_recovered_provider_leaves_the_remainder_pending_for_the_next_pass() -> None:
    """The first batch lands, the second is owed, and nothing in between is lost."""
    provider = FailingProvider(failures=MAX_RETRIES + 1)
    sink = RecordingSink(owed=[pending(index) for index in range(4)])
    embedder = build_embedder(provider, sink, batch_size=2)

    first = embedder.drain(4)
    provider.failures = 0
    provider.calls.clear()
    second = embedder.drain(4)

    assert (first.written, first.deferred) == (0, 4)
    assert second.written == 4
    assert [write.artifact_id for write in sink.written] == [
        artifact.artifact_id for artifact in sink.owed
    ]


def test_a_provider_answering_zero_vectors_leaves_the_artifacts_pending() -> None:
    """Nothing is marked failed, so an unusable answer is a delay and not a loss."""
    provider = ZeroVectorProvider()
    sink = RecordingSink(owed=[pending(0), pending(1)])
    embedder = build_embedder(provider, sink)

    outcome = embedder.drain(2)

    assert outcome == DrainOutcome(considered=2, deferred=2)
    assert sink.written == []
    assert sink.transitions == []


def test_a_derived_artifact_is_moved_out_of_the_pending_state() -> None:
    """Its state column is writable, so the row leaves the sweep for every process."""
    sink = RecordingSink(owed=[pending(0)])
    embedder = build_embedder(NonNormalisingEmbeddingProvider(), sink)

    embedder.drain(1)

    assert sink.transitions == [(UUID(int=1), EmbeddingState.EMBEDDED)]


def test_an_event_is_not_moved_and_is_not_embedded_twice_in_one_process() -> None:
    """No role holds UPDATE on the Ledger, so the in-process record does the work."""
    provider = NonNormalisingEmbeddingProvider()
    sink = RecordingSink(owed=[pending(0, kind=ArtifactKind.EVENT)])
    embedder = build_embedder(provider, sink)

    first = embedder.drain(1)
    second = embedder.drain(1)

    assert first.written == 1
    assert sink.transitions == []
    assert second == DrainOutcome()
    assert len(provider.calls) == 1


def test_a_vector_already_stored_is_settled_rather_than_reported_as_a_failure() -> None:
    """The steady state on the Event path is work already done, not work refused."""
    provider = NonNormalisingEmbeddingProvider()
    sink = RecordingSink(owed=[pending(0, kind=ArtifactKind.EVENT)])
    build_embedder(provider, sink).drain(1)

    outcome = build_embedder(provider, sink).drain(1)

    assert (outcome.written, outcome.settled, outcome.deferred) == (0, 1, 0)


def test_an_unwritable_cluster_stops_the_drain_and_leaves_the_work_owed() -> None:
    """A vector that cannot be stored is not a reason to spend more provider calls."""
    provider = NonNormalisingEmbeddingProvider()
    sink = RecordingSink(owed=[pending(index) for index in range(4)], refuse_write=True)
    embedder = build_embedder(provider, sink, batch_size=2)

    outcome = embedder.drain(4)

    assert outcome.written == 0
    assert outcome.deferred == 4
    assert len(provider.calls) == 1


def test_an_artifact_with_no_text_is_skipped_without_a_provider_call() -> None:
    """A row owing a vector with no text to produce one from spends nothing."""
    provider = NonNormalisingEmbeddingProvider()
    sink = RecordingSink(owed=[pending(0), pending(1)])

    def no_texts(group: Sequence[PendingArtifact]) -> Mapping[UUID, str]:
        return {group[0].artifact_id: "the only body available"}

    outcome = build_embedder(provider, sink, texts=no_texts).drain(2)

    assert (outcome.written, outcome.skipped) == (1, 1)
    assert [len(call) for call in provider.calls] == [1]


def test_the_backlog_left_by_a_drain_is_measured() -> None:
    """What is still owed after a pass is what a backlog metric is for."""
    reset()
    sink = RecordingSink(owed=[pending(index) for index in range(3)], refuse_write=True)
    embedder = build_embedder(NonNormalisingEmbeddingProvider(), sink)

    outcome = embedder.drain(3)

    assert outcome.outstanding == 3
    assert current().counters().get((PENDING_BACKLOG_METRIC, ()), 0.0) == 3.0


def test_the_drain_reports_how_many_vectors_landed() -> None:
    """The stated surface answers with a count, which is what a caller logs."""
    sink = RecordingSink(owed=[pending(index) for index in range(3)])
    embedder = build_embedder(NonNormalisingEmbeddingProvider(), sink)

    assert embedder.drain_pending(3) == 3


# ---------------------------------------------------------------------------
# The configuration surface
# ---------------------------------------------------------------------------


def test_the_bounds_are_read_from_the_configuration_surface() -> None:
    """Both bounds resolve from their own keys, so neither is compiled in."""
    surface = Configuration(
        environ={
            "MOLT_EMBEDDING_BATCH_SIZE": "10",
            "MOLT_PROVIDER_MAX_RETRIES": "1",
        }
    )

    embedder = Embedder.from_configuration(
        surface,
        sink=RecordingSink(),
        texts=texts_for,
        provider=NonNormalisingEmbeddingProvider(),
    )

    assert (embedder.batch_size, embedder.max_retries) == (10, 1)


def test_a_configured_batch_size_above_the_ceiling_is_reported_against_its_key() -> None:
    """A cost bound stated as a maximum cannot be raised by a configuration value."""
    surface = Configuration(environ={"MOLT_EMBEDDING_BATCH_SIZE": str(MAX_BATCH_TEXTS + 1)})

    with pytest.raises(InvalidConfigValueError, match="MOLT_EMBEDDING_BATCH_SIZE"):
        Embedder.from_configuration(
            surface,
            sink=RecordingSink(),
            texts=texts_for,
            provider=NonNormalisingEmbeddingProvider(),
        )


def test_a_configured_retry_count_above_the_bound_is_reported_against_its_key() -> None:
    """The retry bound of the requirement is a maximum for the same reason."""
    surface = Configuration(environ={"MOLT_PROVIDER_MAX_RETRIES": str(MAX_RETRIES + 1)})

    with pytest.raises(InvalidConfigValueError, match="MOLT_PROVIDER_MAX_RETRIES"):
        Embedder.from_configuration(
            surface,
            sink=RecordingSink(),
            texts=texts_for,
            provider=NonNormalisingEmbeddingProvider(),
        )
