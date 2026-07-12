"""Artifact models: kinds, derived artifacts, lineage edges, and embeddings.

An Artifact is any row of stored memory content that can be erased. The four
kinds are the Event, the Session, the Derived_Artifact, and the Embedding, and
the polymorphic reference below mirrors the view the schema exposes over the
first three.

Procedure confidence lives on the learned-procedure kind and on no other. The
schema states that as an equivalence rather than an implication, so a summary can
never acquire a confidence value and a learned procedure can never be written
without one; the model states the same rule so the two agree before a write is
ever attempted.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from molt.models.event import EmbeddingState, require_aware

# The fixed vector width the schema column declares. An embedding provider
# reporting any other width is refused at startup rather than written.
EMBEDDING_DIMENSION: Final[int] = 1024

# The length of a hexadecimal SHA-256 digest.
DIGEST_LENGTH: Final[int] = 64

CONFIDENCE_FLOOR: Final[float] = 0.0
CONFIDENCE_CEILING: Final[float] = 1.0


class ArtifactKind(StrEnum):
    """Every kind of stored row an attribution or a disposition may name."""

    EVENT = "event"
    SESSION = "session"
    DERIVED_ARTIFACT = "derived_artifact"
    EMBEDDING = "embedding"


class DerivedArtifactKind(StrEnum):
    """The three delivered kinds of derived content."""

    SUMMARY = "summary"
    BEHAVIORAL_BASELINE = "behavioral_baseline"
    LEARNED_PROCEDURE = "learned_procedure"


ARTIFACT_KIND_VALUES: Final[tuple[str, ...]] = tuple(member.value for member in ArtifactKind)
DERIVED_KIND_VALUES: Final[tuple[str, ...]] = tuple(member.value for member in DerivedArtifactKind)

# A lineage parent is one of the three kinds the artifact reference view spans;
# an embedding is derived content and is never itself a lineage parent.
LINEAGE_PARENT_KINDS: Final[frozenset[ArtifactKind]] = frozenset(
    {ArtifactKind.EVENT, ArtifactKind.SESSION, ArtifactKind.DERIVED_ARTIFACT}
)

# Only these two kinds carry embeddable text.
EMBEDDABLE_KINDS: Final[frozenset[ArtifactKind]] = frozenset(
    {ArtifactKind.EVENT, ArtifactKind.DERIVED_ARTIFACT}
)


def require_digest(value: str, subject: str) -> str:
    """Return a hexadecimal digest unchanged, refusing any other shape."""
    if len(value) != DIGEST_LENGTH:
        raise ValueError(f"{subject} must be a hexadecimal SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{subject} must be a hexadecimal SHA-256 digest") from exc
    return value


def require_unit_interval(value: float, subject: str) -> float:
    """Return a value unchanged, refusing one outside the closed unit interval."""
    if not CONFIDENCE_FLOOR <= value <= CONFIDENCE_CEILING:
        raise ValueError(f"{subject} must lie in the closed interval from zero to one")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A polymorphic reference to one stored Artifact."""

    id: UUID
    kind: ArtifactKind
    client_id: UUID

    def __post_init__(self) -> None:
        ArtifactKind(self.kind)


@dataclass(frozen=True, slots=True)
class DerivedArtifact:
    """Content produced by summarising, distilling, or generalising Artifacts."""

    id: UUID
    kind: DerivedArtifactKind
    owner_client_id: UUID
    body: str
    content_digest: str
    derivation_method: str
    revision: int
    created_at: datetime
    updated_at: datetime
    redacted_at: datetime | None
    embedding_state: EmbeddingState
    expires_at: datetime
    procedure_confidence: float | None

    def __post_init__(self) -> None:
        kind = DerivedArtifactKind(self.kind)
        EmbeddingState(self.embedding_state)
        require_digest(self.content_digest, "a derived artifact content digest")
        if self.revision < 1:
            raise ValueError("a derived artifact revision starts at one")
        require_aware(self.created_at, "a derived artifact creation timestamp")
        require_aware(self.updated_at, "a derived artifact update timestamp")
        require_aware(self.expires_at, "a derived artifact expiry timestamp")
        if self.redacted_at is not None:
            require_aware(self.redacted_at, "a derived artifact redaction timestamp")
        is_procedure = kind is DerivedArtifactKind.LEARNED_PROCEDURE
        if is_procedure and self.procedure_confidence is None:
            raise ValueError("a learned procedure carries a procedure confidence value")
        if not is_procedure and self.procedure_confidence is not None:
            raise ValueError("only a learned procedure carries a procedure confidence value")
        if self.procedure_confidence is not None:
            require_unit_interval(self.procedure_confidence, "a procedure confidence value")


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """One edge from a derived artifact to a single Artifact it came from."""

    id: UUID
    child_id: UUID
    parent_id: UUID
    parent_kind: ArtifactKind
    derivation_method: str
    created_at: datetime

    def __post_init__(self) -> None:
        parent_kind = ArtifactKind(self.parent_kind)
        if parent_kind not in LINEAGE_PARENT_KINDS:
            raise ValueError("a lineage parent is an event, a session, or a derived artifact")
        if self.child_id == self.parent_id:
            raise ValueError("a lineage edge joins two distinct artifacts")
        require_aware(self.created_at, "a lineage edge creation timestamp")


@dataclass(frozen=True, slots=True)
class Embedding:
    """A fixed-width vector representation of one Artifact's text.

    The vector is held as a tuple so the record stays hashable and cannot be
    edited in place, and it is unit-normalised before a write because the
    distributed index orders by squared distance while the thresholds the design
    states are expressed in cosine terms.
    """

    id: UUID
    artifact_id: UUID
    artifact_kind: ArtifactKind
    client_id: UUID
    provider: str
    model_id: str
    dimension: int
    normalised: bool
    vec: tuple[float, ...]
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        kind = ArtifactKind(self.artifact_kind)
        if kind not in EMBEDDABLE_KINDS:
            raise ValueError("only an event or a derived artifact carries an embedding")
        if self.dimension != EMBEDDING_DIMENSION:
            raise ValueError(f"an embedding dimension is fixed at {EMBEDDING_DIMENSION}")
        if len(self.vec) != self.dimension:
            raise ValueError("an embedding vector length matches its declared dimension")
        require_aware(self.created_at, "an embedding creation timestamp")
        require_aware(self.expires_at, "an embedding expiry timestamp")
