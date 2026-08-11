"""The deterministic local vector function the seed embeds every Artifact with.

Seeding must produce vectors without reaching a model provider, and it must
produce the *same* vectors from the same seed on any machine, so the embedder
here is a pure function of text.

Two properties make it usable for what the seed exists to demonstrate.

**It is content-bearing rather than content-blind.** A digest of the whole text
would give two texts sharing a vocabulary two unrelated vectors, and a residue
search over such vectors would recover nothing by meaning. So the text is split
into tokens and each token is hashed into a fixed-width accumulator with a signed
contribution, which is the ordinary hashing-trick construction: texts that share
tokens land near each other, and texts that share none do not. The contamination
the seed plants is therefore recoverable by distance rather than by a label.

**Every vector is unit length at the fixed schema width.** The delivered vector
index orders by L2 distance while every threshold in this design is a cosine
distance, and the two agree only on unit vectors, so normalising here is what
makes a seeded corpus rankable by the same rules a provider-embedded one is.

Nothing here reads a credential, opens a socket, or consults a clock.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from molt.models.artifact import EMBEDDING_DIMENSION

__all__ = [
    "SEED_PROVIDER_NAME",
    "SEED_VECTOR_MODEL_ID",
    "TOKEN_PATTERN",
    "SeedEmbedder",
    "seed_vector",
    "tokens_of",
]

# What an Embedding row records as the source of a seeded vector. It names this
# function rather than a service, so a seeded row is never mistaken for one a
# provider produced.
SEED_PROVIDER_NAME: Final[str] = "seed_local"
SEED_VECTOR_MODEL_ID: Final[str] = "seed-token-hash"

# What counts as a token. Word characters run together, which keeps an identifier
# written with underscores as one token and splits punctuation away from it.
TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]+")

# How many buckets a token may land in, and the sign it may contribute with. Two
# hash outputs are read off one digest so a token costs one hash rather than two.
_SIGN_POSITIVE: Final[float] = 1.0
_SIGN_NEGATIVE: Final[float] = -1.0

# The component of the fallback vector for a text holding no token at all, so the
# result is always unit length and never the zero vector.
_FALLBACK_INDEX: Final[int] = 0


def tokens_of(text: str) -> tuple[str, ...]:
    """Split text into the lowercase tokens the vector is accumulated over."""
    return tuple(match.group(0).lower() for match in TOKEN_PATTERN.finditer(text))


def seed_vector(text: str, dimensions: int = EMBEDDING_DIMENSION) -> tuple[float, ...]:
    """Return the unit-length vector standing for one text, at a fixed width.

    Each token contributes to one bucket with one sign, both derived from the
    token's own digest, so the mapping is stable across processes and platforms.
    The count of a repeated token contributes as its weight, which is what makes
    a long fragment sit nearer another fragment of the same vocabulary than a
    short prompt does.

    Raises:
        ValueError: The requested width is not positive, so no vector exists.
    """
    if dimensions <= 0:
        raise ValueError("a vector width must be positive")
    accumulator = [0.0] * dimensions
    for token in tokens_of(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") % dimensions
        sign = _SIGN_POSITIVE if digest[8] % 2 == 0 else _SIGN_NEGATIVE
        accumulator[bucket] += sign
    norm = math.sqrt(sum(component * component for component in accumulator))
    if norm == 0.0:
        unit = [0.0] * dimensions
        unit[_FALLBACK_INDEX] = 1.0
        return tuple(unit)
    return tuple(component / norm for component in accumulator)


@dataclass(frozen=True, slots=True)
class SeedEmbedder:
    """The embedding surface the seed writes vectors through.

    It carries the same two attributes an Embedding row records for a provider,
    so a seeded row is self-describing, and it exposes the batch shape the
    ordinary embedder path uses, so a caller that already holds a provider can be
    passed instead of this one without any other change.
    """

    dimensions: int = EMBEDDING_DIMENSION
    provider: str = SEED_PROVIDER_NAME
    model_id: str = SEED_VECTOR_MODEL_ID

    def __post_init__(self) -> None:
        """Refuse a width the schema's vector column could not hold."""
        if self.dimensions != EMBEDDING_DIMENSION:
            raise ValueError("a seeded vector is written at the width the schema fixes")

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Return one unit-length vector per text, in the order the texts arrived."""
        return [seed_vector(text, self.dimensions) for text in texts]

    def embed_one(self, text: str) -> tuple[float, ...]:
        """Return the vector for a single text."""
        return seed_vector(text, self.dimensions)
