"""The non-normalising embedding provider stub the provider properties draw from.

This module exists for one reason, and the reason is worth stating in full
because it is the difference between a tested step and an untested one.

The delivered vector index orders by L2 distance. Every threshold in this design
— the auto-inclusion threshold, the review threshold, and the recall bound — is
expressed as a cosine distance. Those two orderings agree on unit vectors and
disagree on every other vector, so the Embedder scales every vector to unit
length before it is written, and that scaling is what keeps the thresholds
meaning what a certificate says they mean.

The embedding implementation the delivered configuration selects already returns
unit-normalised vectors. A property suite that drew only from stubs modelled on
that implementation would pass with the scaling removed entirely: the vectors
would arrive at unit length, be written at unit length, and every assertion about
the stored norm would hold without the Embedder having done anything. The step
would be untested and the suite would not notice.

The stub below therefore answers with vectors whose L2 norm is deliberately not
one. It is drawn from beside the faithful stubs so that a property asserting the
stored norm is asserting the Embedder's own scaling rather than inheriting a
provider's. The documented default implementation also returns vectors that are
not unit-normalised, so this stub is not an artificial case: it stands for the
selection under which the scaling is the whole of the reconciliation.

Two properties of the answers are deliberate. The magnitude varies per text, so a
property cannot pass by accident against a single scale factor. The direction is
reproducible from the text alone, so the same text always yields the same
direction and two different texts yield different directions, which is what lets
a distance-ordering property place known neighbours without a provider call.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from molt.models.artifact import EMBEDDING_DIMENSION

__all__ = [
    "MAGNITUDE_CEILING",
    "MAGNITUDE_FLOOR",
    "NonNormalisingEmbeddingProvider",
    "StubProbeReport",
    "non_unit_vector",
    "scale_factor",
]

# The range the magnitudes are drawn from. It spans two orders of magnitude
# either side of one and excludes one itself, so no answer is accidentally unit
# length and a scaling bug shows up as a norm far from one rather than as a
# rounding difference.
MAGNITUDE_FLOOR: Final[float] = 0.01
MAGNITUDE_CEILING: Final[float] = 100.0

# How far a magnitude must sit from one to count as deliberately not unit length.
# Wider than the tolerance the write path holds a stored vector to, so a vector
# from this stub can never be mistaken for one that needed no scaling.
MAGNITUDE_MARGIN: Final[float] = 0.5


def _byte_stream(seed: bytes, length: int) -> bytes:
    """Expand a seed into as many reproducible bytes as asked for."""
    chunks: list[bytes] = []
    produced = 0
    counter = 0
    while produced < length:
        block = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        chunks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(chunks)[:length]


def scale_factor(text: str) -> float:
    """A reproducible magnitude for one text, never within the margin of one.

    Drawn logarithmically across the range rather than uniformly, so the small
    magnitudes are as well represented as the large ones: a vector a hundred
    times too long and a vector a hundred times too short are both wrong, and a
    uniform draw would almost never produce the second.
    """
    raw = int.from_bytes(_byte_stream(f"magnitude:{text}".encode(), 8), "big")
    span = math.log(MAGNITUDE_CEILING) - math.log(MAGNITUDE_FLOOR)
    position = raw / float(1 << 64)
    factor = math.exp(math.log(MAGNITUDE_FLOOR) + span * position)
    if abs(factor - 1.0) < MAGNITUDE_MARGIN:
        return factor * MAGNITUDE_CEILING
    return factor


def non_unit_vector(text: str, dimensions: int = EMBEDDING_DIMENSION) -> tuple[float, ...]:
    """A reproducible vector for one text whose L2 norm is deliberately not one.

    The direction is derived from the text and the magnitude from the same text
    through a separate draw, so direction and magnitude are independent and a
    property can assert on either.
    """
    stream = _byte_stream(f"direction:{text}".encode(), dimensions * 4)
    raw = struct.unpack(f">{dimensions}i", stream)
    components = [value / 2147483648.0 for value in raw]
    norm = math.sqrt(math.fsum(component * component for component in components))
    if norm == 0.0:  # pragma: no cover - unreachable for any digest-derived vector
        components = [0.0] * dimensions
        components[0] = 1.0
        norm = 1.0
    factor = scale_factor(text) / norm
    return tuple(component * factor for component in components)


@dataclass(frozen=True, slots=True)
class StubProbeReport:
    """What this stub reports when asked to describe itself.

    A record of its own rather than the concrete probe shape, because the
    provider protocol declares the probe structurally and a stub that had to
    import the concrete shape would be coupled to it for no gain.
    """

    name: str
    model_id: str
    reachable: bool
    dimensions: int | None = None
    supports_prompt_cache: bool | None = None


@dataclass(slots=True)
class NonNormalisingEmbeddingProvider:
    """An embedding provider whose vectors are the declared width and not unit length.

    It reports the width the schema fixes, because the width and the norm are
    separate claims and a stub failing both would not tell a reader which one a
    property caught. Every text it is asked about is recorded, so a property
    asserts the batching bound without a network round trip.
    """

    name: str = "non-normalising-stub"
    model_id: str = "non-normalising-stub-embedding"
    dimensions: int = EMBEDDING_DIMENSION
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Return one vector per input text, in the input order, none unit length."""
        self.calls.append(tuple(texts))
        return [non_unit_vector(text, self.dimensions) for text in texts]

    def probe(self) -> StubProbeReport:
        """Report reachability and the declared width the startup gate checks."""
        return StubProbeReport(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )
