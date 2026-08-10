"""The pure evaluation of one mutation against a rule set.

`evaluate` is a function of its two arguments and nothing else. It opens no
connection, reads no clock, reads no environment, and touches no filesystem. Every
value a match needs is carried on the `Mutation`: the path or the command from the
payload, the Client the mutation belongs to, the Session's accrued cost, and the
Session's recent Event categories. That is why the two session-scoped kinds take
the shape they do — a cost read inside this function would be a database read, and
a database read would make order-independence untestable.

**Five match kinds, the schema's five.** A path pattern is matched against
`payload.path` on a file read, a file write, and a tool call whose payload carries
a path. A command pattern is matched against `payload.command` on a shell command.
A Client rule compares the mutation's Client. A cost rule compares the Session's
accrued cost against a threshold. An error-rate rule divides the error Events by
the total Events over the trailing window the rule names.

**Resolution is order-independent by construction, not by care.** Three things
make it so. Disabled rules are dropped before anything else, so the enabled subset
is what is evaluated. Outcomes are keyed by rule identifier, so the same rule
offered twice produces one outcome. The returned list is sorted by a key that is
total over a well-formed rule set — severity, then rule name, then identifier — so
the result is a canonical form of a set rather than a trace of a walk. Permuting
the rule list, or the mutation stream, therefore cannot change what comes out,
which is the property the confluence test asserts.

**The most severe outcome governs.** `governing_outcome` is the minimum under the
same total order, so a mutation matching a halt rule and a warn rule halts. The
severity order itself is fixed in the rule module: halt, require approval, warn,
allow.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from molt.models.event import EventCategory, JsonObject, require_aware
from molt.policy.rules import (
    MatchKind,
    PatternMode,
    PolicyAction,
    PolicyRule,
    compile_pattern,
    normalise_path,
    severity_rank,
)

__all__ = [
    "COMMAND_PAYLOAD_KEY",
    "PATH_BEARING_CATEGORIES",
    "PATH_PAYLOAD_KEY",
    "Mutation",
    "MutationTable",
    "PolicyOutcome",
    "error_rate_over",
    "evaluate",
    "governing_action",
    "governing_outcome",
    "triggered_actions",
]

# The payload keys the two pattern kinds read.
PATH_PAYLOAD_KEY: Final[str] = "path"
COMMAND_PAYLOAD_KEY: Final[str] = "command"

# The Event categories whose payload may carry a path. A tool call is included
# because a tool that operates on a path records the path it operated on, and
# excluding it would leave the largest class of file access unmatched.
PATH_BEARING_CATEGORIES: Final[frozenset[EventCategory]] = frozenset(
    {
        EventCategory.FILE_READ,
        EventCategory.FILE_WRITE,
        EventCategory.TOOL_CALL,
    }
)

# The category a rate rule counts.
_ERROR_CATEGORY: Final[EventCategory] = EventCategory.ERROR


class MutationTable(StrEnum):
    """The two tables the changefeed covers."""

    LEDGER = "ledger"
    DERIVED_ARTIFACT = "derived_artifact"


@dataclass(frozen=True, slots=True)
class Mutation:
    """One consumed row change, carrying everything an evaluation may read.

    Attributes:
        table: Which of the two covered tables the row belongs to.
        row_id: The row's primary key. For a Ledger row this is the Event
            identifier a match row records; for a Derived_Artifact row there is no
            Event, and `event_id` reads None.
        session_id: The Session the mutation is attributed to. A Derived_Artifact
            row holds no Session column, so the consuming side supplies the Session
            the artifact was derived within — the match row's Session is not
            nullable, and an outcome with no Session could not be recorded.
        client_id: The Client the mutation belongs to.
        occurred_at: When the observation happened, carried so an outcome can name
            the triggering mutation without this function reading a clock.
        category: The Ledger category, absent for a Derived_Artifact row.
        payload: The row's payload, read by the two pattern kinds.
        session_cost_usd: The Session's accrued cost as at this mutation.
        recent_categories: The Session's Event categories in chronological order,
            oldest first, ending with this mutation's own category where the
            mutation is a Ledger row. A rate rule reads the trailing window of it.
    """

    table: MutationTable
    row_id: UUID
    session_id: UUID
    client_id: UUID
    occurred_at: datetime
    category: EventCategory | None = None
    payload: JsonObject = field(default_factory=dict)
    session_cost_usd: Decimal = Decimal(0)
    recent_categories: tuple[EventCategory, ...] = ()

    def __post_init__(self) -> None:
        table = MutationTable(self.table)
        require_aware(self.occurred_at, "a mutation timestamp")
        if table is MutationTable.LEDGER and self.category is None:
            raise ValueError("a Ledger mutation must carry its Event category")
        if table is MutationTable.DERIVED_ARTIFACT and self.category is not None:
            raise ValueError("a Derived_Artifact mutation carries no Event category")
        if self.category is not None:
            EventCategory(self.category)
        if self.session_cost_usd < Decimal(0):
            raise ValueError("an accrued Session cost must be non-negative")

    @property
    def event_id(self) -> UUID | None:
        """The Event this mutation is, where it is one."""
        return self.row_id if MutationTable(self.table) is MutationTable.LEDGER else None

    @property
    def path_subject(self) -> str | None:
        """The path a file path rule is matched against, where the mutation carries one."""
        if self.category is None or EventCategory(self.category) not in PATH_BEARING_CATEGORIES:
            return None
        return _payload_text(self.payload, PATH_PAYLOAD_KEY)

    @property
    def command_subject(self) -> str | None:
        """The command a shell command rule is matched against, where there is one."""
        if self.category is None or EventCategory(self.category) is not EventCategory.SHELL_COMMAND:
            return None
        return _payload_text(self.payload, COMMAND_PAYLOAD_KEY)


def _payload_text(payload: JsonObject, key: str) -> str | None:
    """A payload's value under a key, when it is non-empty text."""
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """One rule's verdict on one mutation, shaped as the match row it becomes.

    Attributes:
        rule_id: The rule that matched.
        rule_name: That rule's name, carried so a report needs no second lookup.
        match_kind: Which of the five kinds matched.
        action: What the rule asks for.
        session_id: The Session the offending mutation belongs to.
        event_id: The triggering Event, absent for a Derived_Artifact mutation.
        detail: What the match row records about why the rule matched. It holds the
            pattern and the subject, or the threshold and the observed value; it
            never holds a payload body.
    """

    rule_id: UUID
    rule_name: str
    match_kind: MatchKind
    action: PolicyAction
    session_id: UUID
    event_id: UUID | None
    detail: JsonObject

    @property
    def severity(self) -> int:
        """This outcome's position in the fixed severity order."""
        return severity_rank(self.action)


def _outcome_order(outcome: PolicyOutcome) -> tuple[int, str, str]:
    """The total order outcomes are canonicalised under."""
    return (severity_rank(outcome.action), outcome.rule_name, str(outcome.rule_id))


def evaluate(mutation: Mutation, rules: Sequence[PolicyRule]) -> list[PolicyOutcome]:
    """Every enabled rule's verdict on one mutation, in canonical severity order.

    The result depends on the *set* of enabled rules rather than on the sequence
    they arrived in: a rule offered twice contributes one outcome, and the list is
    sorted under a total order rather than emitted in arrival order.
    """
    matched: dict[UUID, PolicyOutcome] = {}
    for rule in rules:
        if not rule.enabled:
            continue
        detail = _match(mutation, rule)
        if detail is None:
            continue
        candidate = PolicyOutcome(
            rule_id=rule.id,
            rule_name=rule.name,
            match_kind=MatchKind(rule.match_kind),
            action=PolicyAction(rule.action),
            session_id=mutation.session_id,
            event_id=mutation.event_id,
            detail=detail,
        )
        held = matched.get(rule.id)
        # Two rules sharing an identifier are a malformed set. Keeping the
        # canonically smaller of the two resolves it the same way whichever
        # arrived first, so even a malformed set evaluates order-independently.
        if held is None or _outcome_order(candidate) < _outcome_order(held):
            matched[rule.id] = candidate
    return sorted(matched.values(), key=_outcome_order)


def governing_outcome(outcomes: Sequence[PolicyOutcome]) -> PolicyOutcome | None:
    """The most severe outcome, or None when nothing matched.

    Ties are broken by the same total order the list is sorted under, so a mutation
    matching two halt rules names one of them deterministically.
    """
    return min(outcomes, key=_outcome_order, default=None)


def governing_action(outcomes: Sequence[PolicyOutcome]) -> PolicyAction | None:
    """The action the most severe outcome asks for, or None when nothing matched."""
    outcome = governing_outcome(outcomes)
    return None if outcome is None else PolicyAction(outcome.action)


def triggered_actions(outcomes: Sequence[PolicyOutcome]) -> tuple[PolicyAction, ...]:
    """The distinct actions a set of outcomes triggered, in severity order."""
    present = {PolicyAction(outcome.action) for outcome in outcomes}
    return tuple(sorted(present, key=severity_rank))


def error_rate_over(
    categories: Sequence[EventCategory],
    window_events: int | None,
) -> float | None:
    """The error share of the trailing window, or None when the window is empty.

    A window with no Events in it has no rate rather than a rate of zero, which is
    why this returns None instead of a number a threshold could be compared against.
    """
    window = tuple(categories) if window_events is None else tuple(categories)[-window_events:]
    if not window:
        return None
    errors = sum(1 for category in window if EventCategory(category) is _ERROR_CATEGORY)
    return errors / len(window)


def _match(mutation: Mutation, rule: PolicyRule) -> JsonObject | None:
    """Why a rule matched a mutation, or None when it did not."""
    kind = MatchKind(rule.match_kind)
    if kind is MatchKind.FILE_PATH:
        return _match_file_path(mutation, rule)
    if kind is MatchKind.SHELL_COMMAND:
        return _match_shell_command(mutation, rule)
    if kind is MatchKind.CLIENT:
        return _match_client(mutation, rule)
    if kind is MatchKind.SESSION_COST:
        return _match_session_cost(mutation, rule)
    return _match_error_rate(mutation, rule)


def _match_file_path(mutation: Mutation, rule: PolicyRule) -> JsonObject | None:
    subject = mutation.path_subject
    if subject is None or rule.pattern is None:
        return None
    if not compile_pattern(rule.pattern, PatternMode.PATH).matches(subject):
        return None
    return {
        "match_kind": MatchKind.FILE_PATH.value,
        "pattern": rule.pattern,
        "path": normalise_path(subject),
    }


def _match_shell_command(mutation: Mutation, rule: PolicyRule) -> JsonObject | None:
    subject = mutation.command_subject
    if subject is None or rule.pattern is None:
        return None
    if not compile_pattern(rule.pattern, PatternMode.TEXT).matches(subject):
        return None
    return {
        "match_kind": MatchKind.SHELL_COMMAND.value,
        "pattern": rule.pattern,
        "command": subject,
    }


def _match_client(mutation: Mutation, rule: PolicyRule) -> JsonObject | None:
    if rule.client_id is None or rule.client_id != mutation.client_id:
        return None
    return {
        "match_kind": MatchKind.CLIENT.value,
        "client_id": str(mutation.client_id),
    }


def _match_session_cost(mutation: Mutation, rule: PolicyRule) -> JsonObject | None:
    if rule.threshold is None or mutation.session_cost_usd <= rule.threshold:
        return None
    return {
        "match_kind": MatchKind.SESSION_COST.value,
        "threshold": rule.threshold,
        # The accrued cost is rendered as text so the recorded value is the exact
        # decimal the Session holds rather than a binary approximation of it.
        "cost_usd": str(mutation.session_cost_usd),
    }


def _match_error_rate(mutation: Mutation, rule: PolicyRule) -> JsonObject | None:
    if rule.threshold is None:
        return None
    rate = error_rate_over(mutation.recent_categories, rule.window_events)
    if rate is None or rate <= rule.threshold:
        return None
    window = (
        len(mutation.recent_categories)
        if rule.window_events is None
        else min(rule.window_events, len(mutation.recent_categories))
    )
    return {
        "match_kind": MatchKind.ERROR_RATE.value,
        "threshold": rule.threshold,
        "error_rate": rate,
        "window_events": window,
    }
