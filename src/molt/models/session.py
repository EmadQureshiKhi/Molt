"""The Client and Session models.

A Session is one bounded run of an agent command-line tool, owning an ordered
stream of Events. A Client is the tenant that Session's memory belongs to and the
data subject of an erasure request. Both are frozen and slotted: a captured
Session record is replaced rather than mutated in place, and the reserved
unassigned Client identifier is a constant rather than a lookup.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from molt.models.event import JsonObject, require_aware

# The Client every Session falls back to when the workspace mapping names none.
# The value matches the reserved row the first migration inserts.
UNASSIGNED_CLIENT_ID: Final[UUID] = UUID("00000000-0000-4000-8000-000000000000")
UNASSIGNED_CLIENT_SLUG: Final[str] = "unassigned"

DEFAULT_JURISDICTION: Final[str] = "default"


class SessionOutcome(StrEnum):
    """How a Session ended, or that it has not."""

    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


# The outcome values in the order the schema check constraint lists them.
SESSION_OUTCOME_VALUES: Final[tuple[str, ...]] = tuple(member.value for member in SessionOutcome)


@dataclass(frozen=True, slots=True)
class Client:
    """One tenant of the consultancy and the subject of an erasure request."""

    id: UUID
    slug: str
    display_name: str
    jurisdiction: str
    retention_interval: timedelta
    content_markers: tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.slug:
            raise ValueError("a Client slug must be non-empty")
        if self.retention_interval <= timedelta(0):
            raise ValueError("a Client retention interval must be positive")
        require_aware(self.created_at, "a Client creation timestamp")


@dataclass(frozen=True, slots=True)
class Session:
    """One bounded run of an agent command-line tool.

    The depth invariant is encoded here in the half the model can know: a Session
    with no parent sits at depth zero. The parent-plus-one half is derived by the
    inserting statement from the parent row rather than trusted from a caller.
    """

    id: UUID
    client_id: UUID
    agent_cli: str
    machine_id: str
    team_id: str | None
    attribution: JsonObject
    workspace_path: str | None
    started_at: datetime
    ended_at: datetime | None
    outcome: SessionOutcome
    parent_session_id: UUID | None
    spawning_event_id: UUID | None
    depth: int
    tool_call_count: int
    model_request_count: int
    error_count: int
    token_count: int
    cost_usd: Decimal
    halted: bool
    halted_at: datetime | None
    halt_reason: str | None
    halt_rule_id: UUID | None

    def __post_init__(self) -> None:
        SessionOutcome(self.outcome)
        require_aware(self.started_at, "a Session start timestamp")
        for label, moment in (
            ("a Session end timestamp", self.ended_at),
            ("a Session halt timestamp", self.halted_at),
        ):
            if moment is not None:
                require_aware(moment, label)
        if self.depth < 0:
            raise ValueError("a Session nesting depth must be non-negative")
        if self.parent_session_id is None and self.depth != 0:
            raise ValueError("a Session with no parent sits at nesting depth zero")
        for label, count in (
            ("tool call count", self.tool_call_count),
            ("model request count", self.model_request_count),
            ("error count", self.error_count),
            ("token count", self.token_count),
        ):
            if count < 0:
                raise ValueError(f"a Session {label} must be non-negative")
        if self.cost_usd < Decimal(0):
            raise ValueError("a Session accrued cost must be non-negative")

    @property
    def is_root(self) -> bool:
        """Whether this Session was started directly rather than spawned."""
        return self.parent_session_id is None
