"""The Embedder: provider calls in bounded batches, unit scaling, and the drain.

An Embedding is a fixed-width vector standing for one Artifact's text. This
module is the only place a vector is produced, and six claims carry it.

**Vectors come from the provider abstraction and from nothing else.** The
provider arrives already built, from the Provider_Selector or from a caller
holding one, and this module imports no provider client library and names no
vendor. Switching provider is therefore a configuration change with no source
change anywhere here (Requirements 10.1, 37.5, 34.3).

**A provider call carries at most twenty-five texts.** The bound is this module's
own constant, the configured value is read against it, and a configured value
above it is refused rather than quietly honoured, because a cost envelope stated
as a maximum is not a maximum if a configuration key can raise it
(Requirement 33.7).

**Every vector is scaled to unit L2 norm before it is written, and the step is
load-bearing rather than defensive.** The delivered vector index orders by L2
distance while every threshold in this design is expressed as a cosine distance.
Those two orderings agree on unit vectors and part company on any other, so a
vector of another length would be ranked by one measure and judged by another and
the thresholds would quietly stop meaning what a certificate says they mean. The
scaling is what keeps those two facts reconciled (Requirement 10.10).

Whether the scaling does any work depends on which provider is selected, which is
exactly why it stays: the delivered external embedding implementation already
returns unit vectors, so with that selection the step is a no-op, while the
documented default implementation does not, so with that selection the step is
the whole of the reconciliation. Two mechanisms keep that claim checked rather
than assumed. The stored row carries a unit-norm assertion the writing statement
sets, and the write path refuses a vector whose norm is away from one, so a
vector reaching the table through a path that bypassed the scaling is
identifiable from the row. And the property suite pairs the provider stubs with a
deliberately non-normalising stub, so the property exercises the scaling here
rather than inheriting a provider's (Requirements 10.14, 10.15, 37.16).

A vector of zero length is refused rather than scaled. It has no direction, so
any unit vector produced from it would be a direction this module invented, and
that invented direction would place the Artifact somewhere in the space and let
it be returned as somebody's nearest neighbour. A provider answering with one is
treated as a provider that did not answer.

**A failing provider is a delay and never a loss.** A call is attempted, then
retried at most three times with an exponentially growing delay drawn upwards by
jitter, and a batch that still will not answer leaves its Artifacts in the
pending state exactly as they were. Nothing is marked failed and nothing is
dropped, so when the provider returns the drain produces the outstanding vectors
in the same ascending creation order (Requirements 10.8, 32.1, 32.2).

**The drain is ordered, and a failure stops it rather than stepping over it.**
The sweep returns the Artifacts still owing a vector oldest first, and this module
groups that order into consecutive batches without reordering it. When one batch
will not answer, the drain stops there and leaves that batch and every later batch
pending rather than continuing to a batch that might succeed: continuing would
embed newer Artifacts ahead of older ones, which is the one thing the ascending
order exists to prevent, and a provider that failed one call is far more likely to
be unavailable than to be selective. Batches that already landed stay landed,
because a written vector is not made wrong by a later failure.

**Every row records which provider produced it beside which model.** A model
identifier alone does not say which service answered, and the schema holds one
vector per Artifact per provider-and-model pair, so a corpus embedded across a
provider change stays distinguishable row by row and two providers' vectors for
one Artifact are admitted rather than colliding (Requirement 37.15). The drain
writes under one selected provider per instance, so the second vector for an
Artifact arrives only from a differently configured Embedder, never from this one
running twice.

Two seams and one platform constraint are worth stating plainly.

The waiting and the jitter are injected, so a test drives the whole backoff
schedule in one call rather than waiting three delays out. The delivered
implementations are the host's own sleep and a system-seeded uniform draw, and
they are the defaults, so nothing has to be passed on the deployed path.

The text of a pending Artifact arrives through a reader the caller supplies. The
sweep answers with identity, kind, tenant, and creation instant, which is what
makes it cheap and ordered, and it carries no content; this module holds no SQL,
because every statement in this system lives in the data-access layer. The reader
is therefore a constructor argument, and the drain asks it only for the batch it
is about to send.

An Event's pending flag cannot be cleared, and the sweep rather than this module
is what accounts for it. No role holds `UPDATE` on the Ledger, which is what makes
the episodic record append-only and the hash chain worth having, so the state
column of an Event row is fixed by the statement that appended it: a
Derived_Artifact's state moves to `embedded` as soon as its vector lands, and an
Event's stays `pending` for as long as the row exists. The pending sweep therefore
asks whether an Embedding row stands for the Artifact as well as what the column
says, so an embedded Event leaves the sweep in every process rather than being
re-offered to every drain in every fresh container.

Two things here follow from that and are kept rather than removed. A vector
already stored for an Artifact under this provider and model is treated as work
already done rather than as a failure, because the sweep and the write are
separate reads of a cluster other writers are also using and the constraint is
the authority on which of them got there first. And what this instance has
settled is remembered for its lifetime, which costs one identifier per Artifact
and makes a second pass in the same process free of the round trip either way.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Protocol, TypeVar, runtime_checkable
from uuid import UUID

from molt.config.resolve import Configuration, InvalidConfigValueError, Kind
from molt.errors import (
    EmbeddingAlreadyStoredError,
    ModelUnavailableError,
    ProviderWidthMismatchError,
    StoreError,
)
from molt.models.artifact import ArtifactKind
from molt.models.event import EmbeddingState
from molt.providers import SCHEMA_VECTOR_DIMENSIONS, EmbeddingProvider
from molt.store.embeddings import EmbeddingWrite, PendingArtifact
from molt.telemetry import Severity, log, metric

__all__ = [
    "BACKOFF_SECONDS",
    "BATCH_SIZE_ENV",
    "CALLS_METRIC",
    "COMPONENT",
    "DEFAULT_DRAIN_LIMIT",
    "DEFAULT_JITTER",
    "DEFAULT_SLEEP",
    "FAILURES_METRIC",
    "JITTER_FRACTION",
    "MAX_BATCH_TEXTS",
    "MAX_RETRIES",
    "MAX_RETRIES_ENV",
    "NORM_TOLERANCE",
    "PENDING_BACKLOG_METRIC",
    "DrainOutcome",
    "Embedder",
    "EmbeddingSink",
    "Jitter",
    "Sleeper",
    "TextSource",
    "batches",
    "unit_scale",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "embedder"

# The most texts one provider call may carry. This is the ceiling of Requirement
# 33.7 rather than a preference, so the configured batch size is read against it
# and a larger configured value is refused.
MAX_BATCH_TEXTS: Final[int] = 25

# How many times a failing provider call is retried, and the delay before each
# retry in seconds. The schedule doubles, which is the exponential growth
# Requirement 10.8 asks for, and it has exactly one entry per permitted retry so
# the two cannot drift apart.
MAX_RETRIES: Final[int] = 3
BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.5, 1.0, 2.0)

# How much jitter is added to a delay, as a fraction of that delay. Additive
# rather than multiplicative, so the schedule above is the floor each delay is
# drawn upwards from and two containers that failed together do not retry in
# step and re-create the throttle they are backing off from.
JITTER_FRACTION: Final[float] = 0.25

# How far a scaled vector's norm may sit from one and still be written. The same
# tolerance the write path holds a vector to, because a value this module
# considered unit length and the write path did not would be a refusal after a
# provider call had already been paid for.
NORM_TOLERANCE: Final[float] = 1e-6

# How many pending Artifacts one drain considers when a caller names no bound.
# Four provider calls at the batch ceiling, which is a bounded amount of work for
# a step that runs inside another component's invocation.
DEFAULT_DRAIN_LIMIT: Final[int] = 100

# The two configuration keys this module reads, both already on the resolved
# configuration surface.
BATCH_SIZE_ENV: Final[str] = "MOLT_EMBEDDING_BATCH_SIZE"
MAX_RETRIES_ENV: Final[str] = "MOLT_PROVIDER_MAX_RETRIES"

# The measurements this module emits, all undimensioned. The billable
# combination bound is small and a per-Client breakdown of embedding work earns
# no place in it, so the component name in the record is the whole of the
# breakdown.
CALLS_METRIC: Final[str] = "embedder.calls"
FAILURES_METRIC: Final[str] = "embedder.failures"
PENDING_BACKLOG_METRIC: Final[str] = "embedder.pending_backlog"

# How a delay is drawn and how waiting is done. Both are injected so a test
# drives the schedule rather than waiting it out.
Sleeper = Callable[[float], None]
Jitter = Callable[[float, float], float]

# A system-seeded source, so a process that seeded the shared module generator
# for its own reasons does not put every container's retries back in step.
_jitter_source: Final[random.SystemRandom] = random.SystemRandom()

DEFAULT_SLEEP: Final[Sleeper] = time.sleep
DEFAULT_JITTER: Final[Jitter] = _jitter_source.uniform

# How the text of a swept Artifact is obtained: the caller hands the batch it is
# about to send and receives the text per Artifact identifier. An identifier the
# reader answers nothing for is an Artifact whose text is unavailable, which is
# not the same as an Artifact whose text is empty.
TextSource = Callable[[Sequence[PendingArtifact]], Mapping[UUID, str]]

# What a grouping helper carries, which is texts on one path and swept Artifacts
# on the other.
T = TypeVar("T")


@runtime_checkable
class EmbeddingSink(Protocol):
    """The store surface the drain uses, which is three calls and no more.

    Declared structurally rather than as the store class, because the Embedder
    needs the sweep, the vector write, and the state transition and nothing else
    about a connection pool. A test then drives the drain against a recording
    double with no driver installed, and the delivered store satisfies this by
    construction rather than by an adapter.
    """

    def pending_artifacts(self, *, limit: int | None = ...) -> Sequence[PendingArtifact]:
        """The Artifacts still owing a vector, oldest first and bounded."""
        ...

    def write_embedding(self, request: EmbeddingWrite) -> UUID:
        """Write one vector for an Artifact stored earlier owing one."""
        ...

    def mark_embedding_state(
        self,
        artifact_id: UUID,
        client_id: UUID,
        state: EmbeddingState,
    ) -> EmbeddingState | None:
        """Record a vector as owed, present, or unobtainable for one Artifact."""
        ...


@dataclass(frozen=True, slots=True)
class DrainOutcome:
    """What one drain did, in the four outcomes an Artifact can have.

    The four are reported separately because they lead to different operator
    conclusions. Written vectors are progress. Settled Artifacts already carried
    a vector for this provider and model, which on the Event path is the ordinary
    steady state rather than a fault. Deferred Artifacts are the ones a provider
    or cluster failure left owing a vector, and they are what a backlog is made
    of. Skipped Artifacts owed a vector that no text was available for, which is
    a data condition no retry will change.
    """

    considered: int = 0
    written: int = 0
    settled: int = 0
    deferred: int = 0
    skipped: int = 0

    @property
    def outstanding(self) -> int:
        """How many of the considered Artifacts still owe a vector."""
        return self.deferred + self.skipped


def unit_scale(vec: Sequence[float]) -> tuple[float, ...]:
    """Return a vector scaled to unit L2 norm, refusing one that has no direction.

    This is the step that lets the cosine thresholds stay expressed in cosine
    space while an L2-ordered index serves the ordering, so it runs on every
    vector regardless of what the selected provider already did to it.

    A component that is not a finite number is refused, because a norm computed
    over one is meaningless rather than merely wrong. A vector of zero length is
    refused for the reason the module docstring gives: it has no direction, and
    substituting one would invent a position in the space for the Artifact.

    A vector already of unit length is returned rescaled by a factor of one
    rather than returned untouched, so the delivered no-op provider and the
    non-normalising one take exactly the same path here.
    """
    norm = math.sqrt(math.fsum(component * component for component in vec))
    if not math.isfinite(norm):
        raise ValueError("an embedding vector component must be a finite number")
    if norm == 0.0:
        raise ValueError(
            "an embedding vector of zero length has no direction to scale, so no unit "
            "vector stands for it"
        )
    scaled = tuple(component / norm for component in vec)
    rescaled = math.sqrt(math.fsum(component * component for component in scaled))
    if abs(rescaled - 1.0) > NORM_TOLERANCE:
        raise ValueError(
            f"an embedding vector did not scale to unit length; the scaled vector has "
            f"an L2 norm of {rescaled}"
        )
    return scaled


def batches(texts: Sequence[str], size: int) -> Iterator[tuple[str, ...]]:
    """Group texts into consecutive runs of at most `size`, in the given order.

    Consecutive and in order, because the caller's order is the ascending
    creation order the drain owes and the pairing of a vector back to its
    Artifact is positional. The ceiling is checked here as well as at
    construction, so a caller reaching this directly cannot assemble a call above
    the bound Requirement 33.7 states.
    """
    if size < 1:
        raise ValueError("a provider call carries at least one text")
    if size > MAX_BATCH_TEXTS:
        raise ValueError(f"a provider call carries at most {MAX_BATCH_TEXTS} texts")
    return _chunks(texts, size)


class Embedder:
    """One process's Embedder: a selected provider, the bounds, and the two paths.

    An instance holds the provider, the store surface, the text reader, the batch
    bound, the retry bound, and the two injected seams. Constructing one costs no
    round trip and no provider call, so a test builds one against stubs while the
    deployed path builds one through `from_configuration`.
    """

    __slots__ = (
        "_batch_size",
        "_expiry",
        "_jitter",
        "_max_retries",
        "_provider",
        "_settled",
        "_sink",
        "_sleep",
        "_texts",
    )

    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        sink: EmbeddingSink,
        texts: TextSource,
        expiry: timedelta,
        batch_size: int = MAX_BATCH_TEXTS,
        max_retries: int = MAX_RETRIES,
        sleep: Sleeper = DEFAULT_SLEEP,
        jitter: Jitter = DEFAULT_JITTER,
    ) -> None:
        if provider.dimensions != SCHEMA_VECTOR_DIMENSIONS:
            raise ProviderWidthMismatchError(provider.dimensions, SCHEMA_VECTOR_DIMENSIONS)
        if not provider.name or not provider.model_id:
            raise ValueError("an embedding row records the provider name and the model identifier")
        if batch_size < 1 or batch_size > MAX_BATCH_TEXTS:
            raise ValueError(f"a provider call carries 1 to {MAX_BATCH_TEXTS} texts")
        if max_retries < 0 or max_retries > MAX_RETRIES:
            raise ValueError(f"a failing provider call is retried at most {MAX_RETRIES} times")
        if expiry <= timedelta(0):
            raise ValueError("a stored row's retention interval must be positive")
        self._provider = provider
        self._sink = sink
        self._texts = texts
        self._expiry = expiry
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter
        self._settled: set[UUID] = set()

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration,
        *,
        sink: EmbeddingSink,
        texts: TextSource,
        provider: EmbeddingProvider | None = None,
        sleep: Sleeper = DEFAULT_SLEEP,
        jitter: Jitter = DEFAULT_JITTER,
    ) -> Embedder:
        """Build the process's Embedder from the resolved configuration surface.

        The provider is obtained from the Provider_Selector, which is what makes
        the selection a configuration decision; a caller that already holds a
        selected provider passes it rather than having a second one built. The
        two bounds are read here so that a configured batch size above the
        ceiling of Requirement 33.7 is reported against its own key rather than
        as an argument fault.
        """
        from molt.collector.handler import retention_interval
        from molt.providers.selector import select_embedding_provider

        batch_size = configuration.integer(BATCH_SIZE_ENV)
        if batch_size < 1 or batch_size > MAX_BATCH_TEXTS:
            raise InvalidConfigValueError(
                BATCH_SIZE_ENV,
                Kind.INTEGER,
                f"a provider call carries 1 to {MAX_BATCH_TEXTS} texts",
            )
        retries = configuration.integer(MAX_RETRIES_ENV)
        if retries < 0 or retries > MAX_RETRIES:
            raise InvalidConfigValueError(
                MAX_RETRIES_ENV,
                Kind.INTEGER,
                f"a failing provider call is retried at most {MAX_RETRIES} times",
            )
        selected = select_embedding_provider(configuration) if provider is None else provider
        return cls(
            provider=selected,
            sink=sink,
            texts=texts,
            expiry=retention_interval(configuration),
            batch_size=batch_size,
            max_retries=retries,
            sleep=sleep,
            jitter=jitter,
        )

    # -- what this instance is bound by ----------------------------------

    @property
    def provider_name(self) -> str:
        """The provider name written on every row this instance writes."""
        return self._provider.name

    @property
    def model_id(self) -> str:
        """The model identifier written beside that provider name."""
        return self._provider.model_id

    @property
    def batch_size(self) -> int:
        """How many texts one provider call carries, at most the fixed ceiling."""
        return self._batch_size

    @property
    def max_retries(self) -> int:
        """How many times a failing provider call is retried before it is left owed."""
        return self._max_retries

    def backoff(self, retry: int) -> float:
        """The wait before one retry: the scheduled delay, drawn upwards by jitter."""
        base = BACKOFF_SECONDS[min(retry, len(BACKOFF_SECONDS) - 1)]
        return base + self._jitter(0.0, base * JITTER_FRACTION)

    # -- the vector path -------------------------------------------------

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Return one unit vector per text, in the input order.

        The texts are sent in consecutive batches of at most the configured size,
        each batch is retried on its own, and every vector is scaled here rather
        than trusted from the provider.

        Args:
            texts: The texts to embed, in the order the vectors are wanted.

        Returns:
            One unit-length vector per input text, in the input order.

        Raises:
            ModelUnavailableError: A batch would not answer usably within the
                retry bound. Vectors for earlier batches are discarded with it,
                because a caller asking for a vector per text is not served by
                some of them.
        """
        produced: list[tuple[float, ...]] = []
        for batch in batches(texts, self._batch_size):
            produced.extend(self._embed_batch(batch))
        return tuple(produced)

    def _embed_batch(self, batch: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """One provider call for one batch, retried on the bounded schedule.

        Every failure of the call and every unusable answer is treated the same
        way, because the response to an unreachable model, a throttle, a timeout,
        and a malformed answer is identical here: try again a bounded number of
        times, then report the model as unavailable and let the caller leave the
        work owed.
        """
        last: str = "the provider call was not attempted"
        for attempt in range(self._max_retries + 1):
            metric(CALLS_METRIC)
            try:
                return self._scaled(batch, self._provider.embed(batch))
            except Exception as error:
                metric(FAILURES_METRIC)
                last = type(error).__name__
                log(
                    Severity.WARNING,
                    COMPONENT,
                    "an embedding provider call did not produce usable vectors",
                    provider=self._provider.name,
                    model_id=self._provider.model_id,
                    texts=len(batch),
                    attempt=attempt + 1,
                    error_type=last,
                )
            if attempt >= self._max_retries:
                break
            self._sleep(self.backoff(attempt))
        raise ModelUnavailableError(
            f"the embedding provider {self._provider.name} did not produce vectors for "
            f"{len(batch)} text(s) in {self._max_retries + 1} attempt(s); the last failure "
            f"was {last}"
        )

    def _scaled(
        self,
        batch: tuple[str, ...],
        answered: Sequence[Sequence[float]],
    ) -> tuple[tuple[float, ...], ...]:
        """The provider's answer checked for pairing and width, then scaled.

        The count is checked before anything is scaled, because the pairing of a
        vector back to its text is positional: an answer of a different length
        would attach some Artifact's vector to another Artifact's text, which is
        a worse outcome than no vector at all. The width is checked per vector
        because a provider declaring one width and answering with another would
        otherwise be discovered by a column constraint one insert at a time.
        """
        if len(answered) != len(batch):
            raise ValueError(
                f"the embedding provider answered {len(answered)} vector(s) for "
                f"{len(batch)} text(s), so no vector pairs with its text"
            )
        for vector in answered:
            if len(vector) != SCHEMA_VECTOR_DIMENSIONS:
                raise ValueError(
                    f"an embedding vector carries {len(vector)} component(s) where the "
                    f"column holds exactly {SCHEMA_VECTOR_DIMENSIONS}"
                )
        return tuple(unit_scale(vector) for vector in answered)

    # -- the drain -------------------------------------------------------

    def drain_pending(self, limit: int = DEFAULT_DRAIN_LIMIT) -> int:
        """Produce the outstanding vectors, oldest first, and report how many landed.

        This is the form the design states and the form the Collector invocation
        and the console background step call. The count is the number of vectors
        written; `drain` answers the same question with the other three outcomes
        beside it.
        """
        return self.drain(limit).written

    def drain(self, limit: int = DEFAULT_DRAIN_LIMIT) -> DrainOutcome:
        """Produce the outstanding vectors in ascending creation order.

        The sweep is read once and its order is kept: the oldest Artifact still
        owing a vector is the one embedded next, across batch boundaries as well
        as inside them. A batch that will not answer stops the drain and leaves
        itself and everything after it pending, for the reason the module
        docstring gives.

        No provider failure and no cluster failure propagates out of here. The
        point of the pending state is that an unavailable provider delays
        embedding rather than stopping capture, so the failure is measured and
        recorded and the outstanding work stays outstanding.

        Args:
            limit: How many pending Artifacts to consider in this pass.

        Returns:
            What happened, in the four outcomes an Artifact can have.
        """
        owed = [
            artifact
            for artifact in self._sink.pending_artifacts(limit=limit)
            if artifact.artifact_id not in self._settled
        ]
        written = 0
        settled = 0
        skipped = 0
        deferred = 0
        halted = False
        for group in _chunks(owed, self._batch_size):
            if halted:
                deferred += len(group)
                continue
            resolved = self._resolve(group)
            skipped += len(group) - len(resolved)
            if not resolved:
                continue
            try:
                vectors = self._embed_batch(tuple(text for _, text in resolved))
            except ModelUnavailableError:
                deferred += len(resolved)
                halted = True
                continue
            for (artifact, _), vector in zip(resolved, vectors, strict=True):
                landed = self._store_vector(artifact, vector)
                if landed is None:
                    deferred += 1
                    halted = True
                elif landed:
                    written += 1
                else:
                    settled += 1
        outcome = DrainOutcome(
            considered=len(owed),
            written=written,
            settled=settled,
            deferred=deferred,
            skipped=skipped,
        )
        self._record(outcome)
        return outcome

    def _resolve(
        self,
        group: Sequence[PendingArtifact],
    ) -> tuple[tuple[PendingArtifact, str], ...]:
        """The Artifacts of one group paired with their text, in the group's order.

        An Artifact the reader answers nothing for, or answers empty text for, is
        left out: there is nothing to embed, and spending a provider call to
        learn that would spend it on every pass. It stays pending and is reported
        as skipped, because a row owing a vector with no text to produce one from
        is a condition an operator should see rather than one this module should
        resolve.
        """
        supplied = self._texts(group)
        paired: list[tuple[PendingArtifact, str]] = []
        for artifact in group:
            text = supplied.get(artifact.artifact_id)
            if not text:
                log(
                    Severity.WARNING,
                    COMPONENT,
                    "an artifact owing a vector carries no text to produce one from",
                    artifact_id=str(artifact.artifact_id),
                    artifact_kind=str(artifact.artifact_kind),
                )
                continue
            paired.append((artifact, text))
        return tuple(paired)

    def _store_vector(self, artifact: PendingArtifact, vector: tuple[float, ...]) -> bool | None:
        """Write one vector and move the state where the state can be moved.

        Returns True when a vector landed, False when one was already stored for
        this Artifact under this provider and model, and None when the cluster
        could not be written to at all, which is what stops the drain rather than
        letting it spend provider calls it cannot store the answers to.

        The three outcomes are told apart by the type of the refusal rather than
        by its message. The write path raises a class of its own for the
        uniqueness constraint, so *the vector is already there* and *the cluster
        cannot be written to* are distinguished by what was caught; matching the
        text of a message would have made the distinction depend on the wording of
        a sentence written for a log record.
        """
        request = EmbeddingWrite(
            artifact_id=artifact.artifact_id,
            artifact_kind=ArtifactKind(artifact.artifact_kind),
            client_id=artifact.client_id,
            provider=self._provider.name,
            model_id=self._provider.model_id,
            vec=vector,
            expires_at=artifact.created_at + self._expiry,
        )
        try:
            self._sink.write_embedding(request)
        except EmbeddingAlreadyStoredError:
            self._settle(artifact)
            return False
        except StoreError as error:
            log(
                Severity.ERROR,
                COMPONENT,
                "a produced vector could not be stored, so the artifact stays pending",
                artifact_id=str(artifact.artifact_id),
                error_type=type(error).__name__,
            )
            return None
        self._settle(artifact)
        return True

    def _settle(self, artifact: PendingArtifact) -> None:
        """Record an Artifact as no longer owing a vector, in the row and in memory.

        A Derived_Artifact's state column moves to `embedded`, which is what takes
        it out of the sweep for every later drain in every process. An Event's
        cannot move, because no role holds `UPDATE` on the Ledger, so what takes
        an Event out of the sweep is the stored vector the sweep's own existence
        test looks for; the in-process record saves the round trip rather than
        being the whole of the remedy. A failed transition is recorded and nothing
        else: the vector is stored either way, and the next pass reaches the same
        conclusion from the sweep.
        """
        self._settled.add(artifact.artifact_id)
        if ArtifactKind(artifact.artifact_kind) is not ArtifactKind.DERIVED_ARTIFACT:
            return
        try:
            self._sink.mark_embedding_state(
                artifact.artifact_id,
                artifact.client_id,
                EmbeddingState.EMBEDDED,
            )
        except StoreError as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "a stored vector's artifact was not moved out of the pending state",
                artifact_id=str(artifact.artifact_id),
                error_type=type(error).__name__,
            )

    def _record(self, outcome: DrainOutcome) -> None:
        """Measure what one drain left outstanding and record what it did."""
        metric(PENDING_BACKLOG_METRIC, float(outcome.outstanding))
        log(
            Severity.INFO,
            COMPONENT,
            "a pending-embedding drain completed",
            provider=self._provider.name,
            model_id=self._provider.model_id,
            considered=outcome.considered,
            written=outcome.written,
            settled=outcome.settled,
            deferred=outcome.deferred,
            skipped=outcome.skipped,
        )


def _chunks(items: Sequence[T], size: int) -> Iterator[tuple[T, ...]]:
    """Group a sequence into consecutive runs of at most `size`, in its own order.

    One grouping serves both the texts a call carries and the Artifacts a drain
    walks, so the two cannot come to disagree about what a batch boundary is.
    """
    for start in range(0, len(items), size):
        yield tuple(items[start : start + size])
