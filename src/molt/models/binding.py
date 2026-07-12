"""The Attribution_Version model: attribution held as history rather than opinion.

A Client_Binding asserts that a named Artifact contains or derives from data
belonging to a named Client. It is stored as an immutable version carrying a
validity start, a validity end that is null while the version is current, and a
reference to the version that superseded it, also null while current.

Closure is total: a version is either current in both of those columns or closed
in both. The detection method, the confidence value, the Artifact identifier, and
the Client identifier are immutable once stored, which is why supersession
produces a second version rather than editing the first, and why the only
transition this model offers returns a new instance.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from molt.models.artifact import ArtifactKind, require_unit_interval
from molt.models.event import require_aware


class BindingMethod(StrEnum):
    """How the attribution was concluded.

    The method is part of the admission a version makes: a marker detection and
    an inherited attribution are materially different claims about the same row.
    """

    SCOPE = "scope"
    INHERITED = "inherited"
    MARKER = "marker"
    RESIDUE = "residue"


BINDING_METHOD_VALUES: Final[tuple[str, ...]] = tuple(member.value for member in BindingMethod)


@dataclass(frozen=True, slots=True)
class AttributionVersion:
    """One immutable version of a Client_Binding."""

    id: UUID
    artifact_id: UUID
    artifact_kind: ArtifactKind
    client_id: UUID
    method: BindingMethod
    confidence: float
    detected_at: datetime
    valid_from: datetime
    valid_to: datetime | None
    superseded_by: UUID | None

    def __post_init__(self) -> None:
        ArtifactKind(self.artifact_kind)
        BindingMethod(self.method)
        require_unit_interval(self.confidence, "an attribution confidence value")
        require_aware(self.detected_at, "an attribution detection timestamp")
        require_aware(self.valid_from, "an attribution validity start")
        if (self.valid_to is None) != (self.superseded_by is None):
            raise ValueError(
                "an attribution version is either current in both closure fields or closed in both"
            )
        if self.valid_to is not None:
            require_aware(self.valid_to, "an attribution validity end")
            if self.valid_to < self.valid_from:
                raise ValueError("an attribution validity end cannot precede its validity start")

    @property
    def is_current(self) -> bool:
        """Whether this version is the live claim for its Artifact and Client."""
        return self.superseded_by is None

    def superseded(self, successor_id: UUID, closed_at: datetime) -> "AttributionVersion":
        """Return the closed form of this version, naming the version that replaced it.

        Closing a version writes the validity end and the superseding reference
        and nothing else, which is the same pair of columns the writer role holds
        an update privilege on. A version already closed is refused, so a history
        cannot be rewritten by closing the same version twice.
        """
        if not self.is_current:
            raise ValueError("an attribution version is superseded at most once")
        require_aware(closed_at, "an attribution validity end")
        return replace(self, valid_to=closed_at, superseded_by=successor_id)
