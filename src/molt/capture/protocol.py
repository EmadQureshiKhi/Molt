"""The shapes the capture surfaces share: the adapter protocol and its vocabulary.

Five per-tool adapters, the MCP proxy, and the hook entry point all speak about
the same handful of things — an invocation, the identity it belongs to, the
Events it produced, what transmission did, and what the Collector said back. This
module is the one declaration of those things, so a vendor adapter and the entry
point that drives it cannot disagree about the shape of what passes between them.

Four claims arrange the module.

**The adapter surface is a protocol rather than a base class.** An adapter is
written from one vendor's published hook specification and shares no parsing code
with another (Requirement 1.9), so there is nothing for a base class to hold. A
structural protocol states the five calls the entry point makes and leaves each
adapter free to be a plain object with no inheritance at all.

**A Session identifier is derived rather than remembered.** A hook fires as a
fresh process, so nothing carries state between the invocations of one agent run.
The vendor payload does carry a stable session key, and `derive_session_id` turns
that key and the tool name into one identifier by a name-based derivation, so
every invocation of one run computes the same Session identifier without a
lookup, a file, or a database.

**A pending approval decides for itself whether it matches.** The Collector
returns queued approvals with the rule identity and what the rule matches on
(Requirement 23.9), and the entry point holds the Events the current action
produced. Putting the comparison on the approval keeps the entry point from
restating a rule's matching semantics, and keeps an approval for an unrelated rule
from blocking an unrelated action.

**Nothing here imports the redactor or a driver.** The hook process has a latency
budget that a driver import alone would spend (Requirement 1.8), and the
redaction pattern table is imported lazily by the adapter that needs it. The
redaction settings travel on the capture context as a value, so this module names
the type without importing the module that defines it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID, uuid5

from molt.models.event import Event, JsonObject
from molt.models.session import UNASSIGNED_CLIENT_ID, UNASSIGNED_CLIENT_SLUG

if TYPE_CHECKING:  # pragma: no cover - imported for the annotation alone
    from molt.redact import RedactionSettings

__all__ = [
    "SESSION_NAMESPACE",
    "UNASSIGNED_CLIENT",
    "AdapterCapabilities",
    "CaptureContext",
    "ClientRef",
    "Clock",
    "HookAdapter",
    "HookInvocation",
    "HookOutcome",
    "PendingApproval",
    "RecallResult",
    "SubagentSpawn",
    "SystemClock",
    "TransmitResult",
    "derive_session_id",
]

# The namespace every derived Session identifier is drawn from. A fixed
# version-4 value rather than a hash of anything, so the derivation depends on
# the tool name and the vendor's session key alone.
SESSION_NAMESPACE: Final[UUID] = UUID("6f2a1c48-7b3e-4a52-9d10-4c8ef0b7a915")


# ---------------------------------------------------------------------------
# The injected clock
# ---------------------------------------------------------------------------


class Clock(Protocol):
    """The two readings the capture surfaces take, declared structurally.

    A wall reading places an Event on the timeline and stamps a request; a
    monotonic reading measures the elapsed interval a soft deadline is compared
    against. They are separate calls because only one of the two is safe to
    subtract, and both are injected so a test drives a deadline rather than
    waiting one out.
    """

    def now(self) -> datetime:
        """The current instant, timezone aware."""

    def monotonic(self) -> float:
        """The current monotonic reading in seconds, never moving backwards."""


@dataclass(frozen=True, slots=True)
class SystemClock:
    """The delivered clock, reading the host."""

    def now(self) -> datetime:
        """The host's current instant, with an explicit offset attached."""
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        """The host's monotonic reading, which no clock correction moves."""
        return time.monotonic()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientRef:
    """The Client a Session's memory belongs to, and whether it was mapped.

    The flag is carried rather than inferred from the identifier so a caller
    reports the fallback without comparing against the reserved value, and so a
    workspace deliberately mapped to the reserved Client is distinguishable from
    one that matched no entry at all (Requirements 1.5, 1.6).
    """

    id: UUID
    slug: str
    assigned: bool


# What a workspace matching no mapping entry resolves to. The identifier matches
# the reserved row the first migration inserts.
UNASSIGNED_CLIENT: Final[ClientRef] = ClientRef(
    id=UNASSIGNED_CLIENT_ID,
    slug=UNASSIGNED_CLIENT_SLUG,
    assigned=False,
)


def derive_session_id(tool: str, session_key: str) -> UUID:
    """Derive one Session identifier from a tool name and a vendor session key.

    The derivation is name-based, so two hook processes of the same agent run
    compute the same identifier with no shared state between them. The tool name
    participates so that two tools that happen to mint the same session key text
    do not collide onto one Session.
    """
    if not session_key:
        raise ValueError("a Session key must be non-empty to derive an identifier")
    return uuid5(SESSION_NAMESPACE, f"{tool}\x1f{session_key}")


# ---------------------------------------------------------------------------
# What an adapter reads and what it is given
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubagentSpawn:
    """The parentage a payload naming a spawned subagent carries.

    The parent is named by the vendor's own session key rather than by an
    identifier, because the parent Session identifier is derived from that key
    the same way the child's is (Requirement 1.4).
    """

    parent_session_key: str
    spawning_event_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class HookInvocation:
    """One parsed hook payload, reduced to what every adapter agrees about.

    An adapter's `parse` performs the vendor-specific reading and reports the
    result here. The payload itself is carried unredacted: redaction happens where
    fields are lifted into an Event, so an adapter can still consult a field the
    Event does not carry.

    Attributes:
        tool: The token the shim was invoked with, which identifies the Agent_CLI.
        event_name: The vendor's own name for the hook event that fired.
        payload: The decoded payload, with byte values already decoded.
        session_key: The vendor's stable key for the run, or None when the payload
            names none and the invocation therefore cannot be placed in a Session.
        workspace_path: The workspace the run is rooted at, for Client resolution.
        correlation_id: The vendor's identifier linking a result to its call.
        subagent: The parentage, when the payload names a spawned subagent.
        recall_query: The intended action to query memory with, on a hook event
            that precedes an action and admits injected context.
    """

    tool: str
    event_name: str
    payload: JsonObject = field(default_factory=dict)
    session_key: str | None = None
    workspace_path: str | None = None
    correlation_id: str | None = None
    subagent: SubagentSpawn | None = None
    recall_query: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureContext:
    """The identity and the settings an adapter builds Events against.

    Everything here is resolved once per invocation by the entry point, so an
    adapter performs no configuration reading, no identifier derivation, and no
    clock reading of its own.
    """

    session_id: UUID
    client: ClientRef
    machine_id: str
    agent_cli: str
    clock: Clock
    team_id: str | None = None
    workspace_path: str | None = None
    parent_session_id: UUID | None = None
    spawning_event_id: UUID | None = None
    depth: int = 0
    redaction: RedactionSettings | None = None


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """The three degradation flags a tool's specification decides.

    Each is false where the vendor specification defines no channel for the
    behaviour, and the entry point degrades rather than refuses: recall results
    become advisory text, and a halt becomes the tool's non-zero-exit convention.
    """

    structured_stdout: bool
    context_injection: bool
    blocking_decision: bool


# ---------------------------------------------------------------------------
# What memory answers with
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecallResult:
    """One prior Artifact a recall query returned, with its provenance.

    The fields are exactly what every adapter renders from, so the wording and the
    ranking of an injected block are identical across tools and only the envelope
    differs (Requirement 13.6).
    """

    artifact_id: UUID
    distance: float
    outcome: str
    session_id: UUID
    machine_id: str
    occurred_at: datetime
    excerpt: str
    kind: str = ""
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One queued approval the Collector reports for the current Session.

    An approval with neither categories nor patterns is a hold on the whole
    Session and matches every action. An approval naming either restricts the
    block to the actions its rule matches, so an unrelated action proceeds
    (Requirement 23.9).

    Attributes:
        rule_id: The Policy_Rule the approval was queued by, when the envelope
            named one.
        rule_name: The rule's name, which is what a blocking response reports.
        categories: The Event categories the rule applies to.
        patterns: The path or command fragments the rule matches on.
    """

    rule_id: UUID | None
    rule_name: str
    categories: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()

    @property
    def session_wide(self) -> bool:
        """Whether the approval holds the Session rather than one kind of action."""
        return not self.categories and not self.patterns

    def matches(self, events: tuple[Event, ...]) -> bool:
        """Whether any of the current action's Events falls under this approval."""
        if self.session_wide:
            return True
        for event in events:
            if self.categories and str(event.category) in self.categories:
                return True
            if self.patterns and _payload_mentions(event, self.patterns):
                return True
        return False


# The payload fields a path or command pattern is compared against. A rule
# matches on a path or a command (Requirement 23.5), and those are the field
# names the Event mapping table writes them under.
_MATCHED_PAYLOAD_KEYS: Final[tuple[str, ...]] = ("command", "path", "tool_name")


def _payload_mentions(event: Event, patterns: tuple[str, ...]) -> bool:
    """Whether a matched payload field of an Event holds any of the fragments."""
    for key in _MATCHED_PAYLOAD_KEYS:
        value = event.payload.get(key)
        if isinstance(value, str) and any(fragment in value for fragment in patterns):
            return True
    return False


# ---------------------------------------------------------------------------
# What transmission and one invocation produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransmitResult:
    """What one attempt to place a batch with the Collector achieved.

    `observed` is the field the halt path turns on. It is false whenever the
    Collector said nothing — unreachable, refused, or never asked because the
    shared secret is absent — and a false reading means the halt state of the
    Session is unknown rather than clear, which is what the diagnostic line
    reports and what keeps the hook from blocking on a guess (Requirement 6.1).

    Attributes:
        transmitted: How many Events the Collector accepted.
        spooled: How many Events were written to the local spool instead.
        attempts: How many network attempts were made, including the first.
        observed: Whether a response envelope was read.
        halted: Whether the envelope marks the Session halted.
        halt_reason: The reason the envelope gave, when it marks a halt.
        pending_approvals: The queued approvals the envelope reported.
        note: One short phrase naming why transmission did not complete, or None.
    """

    transmitted: int = 0
    spooled: int = 0
    attempts: int = 0
    observed: bool = False
    halted: bool = False
    halt_reason: str | None = None
    pending_approvals: tuple[PendingApproval, ...] = ()
    note: str | None = None

    def blocking_reason(self, events: tuple[Event, ...]) -> str | None:
        """The reason the current action is refused, or None when it proceeds.

        A halt outranks an approval because a halt holds the whole Session while
        an approval may hold one kind of action. Nothing is refused on an
        unobserved envelope.
        """
        if not self.observed:
            return None
        if self.halted:
            return self.halt_reason or "the Session is halted"
        for approval in self.pending_approvals:
            if approval.matches(events):
                return f"an approval is pending for the rule {approval.rule_name}"
        return None


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """What one hook invocation decided, ready for the entry point to write out.

    Standard output is the vendor's decision channel, so `stdout` is empty on
    every path that reached no structured decision. `notes` are joined into the
    one diagnostic line standard error receives, so a caller writes at most one
    line however many notes accumulated.
    """

    stdout: bytes = b""
    notes: tuple[str, ...] = ()
    events: tuple[Event, ...] = ()
    transmitted: int = 0
    spooled: int = 0
    blocked: bool = False

    @property
    def diagnostic(self) -> str | None:
        """The single line standard error receives, or None when there is nothing."""
        if not self.notes:
            return None
        return "; ".join(self.notes)


# ---------------------------------------------------------------------------
# The adapter surface
# ---------------------------------------------------------------------------


class HookAdapter(Protocol):
    """The five calls the entry point makes on a per-tool adapter.

    Each implementation lives at `molt.capture.adapters.<tool>` and is bound to
    the module attribute `ADAPTER`, which is how the entry point finds it without
    importing the four adapters it does not need.
    """

    tool: str

    def parse(self, raw: bytes) -> HookInvocation:
        """Read one vendor payload, raising on a payload the specification refuses."""

    def to_events(self, inv: HookInvocation, ctx: CaptureContext) -> list[Event]:
        """Build the Events the vendor hook event maps to, with fields redacted."""

    def context_injection(self, results: list[RecallResult]) -> bytes:
        """Render recall results in the vendor's documented injection envelope."""

    def blocking_response(self, reason: str) -> bytes:
        """Render the vendor's documented refusal of the pending action."""

    def capabilities(self) -> AdapterCapabilities:
        """The three flags this tool's specification decides."""
