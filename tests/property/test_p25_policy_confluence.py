"""Property 25: policy evaluation is confluent over independent mutations.

**Validates: Requirements 23.3, 23.4, 23.5**

A watcher consumes the same mutations two ways. The primary path reads a
changefeed, which yields row changes per range with no promise about the order two
ranges arrive in, and which replays the unresolved tail after a restart. The
fallback path polls by recorded timestamp and identifier, which yields the same
mutations in one total order. If the triggered action set depended on which of
those a deployment happened to be running, a policy would mean different things on
two clusters, and a restart would be able to change a verdict.

Five decisions shape what is generated and what is asserted.

**Nothing here reaches a cluster or a model provider, because the function under
test reaches neither.** `evaluate` is a function of one mutation and the rule set:
the accrued Session cost and the trailing category window are carried on the
mutation rather than read at match time, so a whole stream can be evaluated in
process. That is the reason the two session-scoped match kinds take the shape they
do, and it is what makes order-independence assertable at all rather than only
observable against a live watcher.

**Independence is defined and then respected, rather than assumed of everything.**
Two mutations are independent when they belong to different Sessions. Within one
Session the recorded order is the order the accrued cost and the trailing window
were captured in, so permuting it would be permuting a Session's own history rather
than reordering independent work. The permutation selector therefore draws an
interleaving of the per-Session subsequences: every mutation moves relative to
other Sessions' mutations and none moves relative to its own Session's.

**Both consumption mechanisms are modelled as delivery orders over one stream.**
The changefeed arm delivers the drawn interleaving and then redelivers the tail
from a drawn restart point, which is what resuming from a watermark does. The
polling arm delivers the same mutations ordered by recorded instant then
identifier, which is the ordering its statement asks for. Instants are drawn from a
small range so ties are ordinary and the identifier tiebreak is exercised rather
than bypassed.

**Application is keyed the way the schema keys it.** A match is recorded per
mutation and rule, which is the uniqueness the deduplicating constraints on
`policy_match` and `approval_queue` hold, so a redelivered mutation contributes no
second match and a rule offered twice contributes no second outcome. A redelivery
that produced a *different* outcome for a key already held is a failure rather than
a value quietly kept, so the dedup cannot mask a divergence.

**The rule set is a set, so its arrival order is permuted too.** A rule list is
drawn with distinct identifiers, some entries are offered a second time, and the
whole list is permuted. Identifiers stay distinct on purpose: two different rules
sharing one identifier is a malformed set, and asserting a canonical outcome for a
set the loader refuses would be asserting about an input the system never accepts.

The example budget is 100 with no per-example deadline. A one-mutation stream
against no rules and a twelve-mutation stream against eight pattern rules differ in
cost by more than an order of magnitude, and a deadline would fail the large end
for being large rather than for being wrong.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.event import EventCategory, JsonObject
from molt.policy.evaluate import (
    Mutation,
    MutationTable,
    PolicyOutcome,
    evaluate,
    governing_action,
    triggered_actions,
)
from molt.policy.rules import MatchKind, PolicyAction, PolicyRule, rule_identifier

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

# How many mutations one stream carries. The upper end is large enough that three
# Sessions interleave several times over, which is what gives the permutation
# selector something to move.
MIN_MUTATIONS: Final[int] = 1
MAX_MUTATIONS: Final[int] = 12

# How many rules a set may hold. Eight covers all five match kinds at once with
# room for a repeat of each.
MAX_RULES: Final[int] = 8

# The instant every mutation is placed relative to, read from a fixed offset rather
# than from the host so no run embeds a reading of the machine it ran on.
FIXED_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# How far apart two mutations may be recorded. Small, so several mutations share an
# instant and the polling order falls through to the identifier tiebreak its
# statement names.
MAX_OFFSET_SECONDS: Final[int] = 3

# The Sessions and Clients a stream is drawn over. Three Sessions so a stream
# ordinarily interleaves more than two independent histories, and two Clients so a
# Client rule matches some mutations and not others.
SESSION_IDS: Final[tuple[UUID, ...]] = (
    UUID(int=(1 << 100) + 1),
    UUID(int=(1 << 100) + 2),
    UUID(int=(1 << 100) + 3),
)
CLIENT_IDS: Final[tuple[UUID, ...]] = (UUID(int=(1 << 101) + 1), UUID(int=(1 << 101) + 2))

# The paths a mutation's payload may carry, and the patterns rules match them with.
# The two sets overlap partially on purpose: some drawn pairings match and some do
# not, so an example distinguishes a rule that fired from a rule that was offered.
PATHS: Final[tuple[str, ...]] = (
    "workspace/service/main.py",
    "workspace/.env",
    "home/operator/.ssh/id_rsa",
    "workspace/deploy/secrets/signing.pem",
    "workspace/notes.txt",
)
PATH_PATTERNS: Final[tuple[str, ...]] = (
    "*.py",
    ".env",
    "id_rsa*",
    "secrets/",
    "re:[.]pem$",
)

# The commands a shell mutation may carry, and the patterns rules match them with.
# Invented rather than drawn from any real tool, because the subject of this
# property is the matching discipline and not any particular command surface.
COMMANDS: Final[tuple[str, ...]] = (
    "build --release",
    "remove-tree --recursive ./scratch",
    "publish --force",
    "fetch-secret --name signing",
)
COMMAND_PATTERNS: Final[tuple[str, ...]] = (
    "build *",
    "*--force",
    "re:^fetch-secret",
    "remove-tree*",
)

# The accrued costs a Session may carry and the ceilings a cost rule may name. Both
# spread across the range, so a drawn pairing sits above the ceiling about as often
# as below it.
COSTS: Final[tuple[Decimal, ...]] = (
    Decimal("0"),
    Decimal("0.50"),
    Decimal("12.00"),
    Decimal("250.00"),
)
COST_CEILINGS: Final[tuple[float, ...]] = (0.0, 1.0, 100.0)

# The error shares a rate rule may name, and the trailing windows it may read.
RATE_CEILINGS: Final[tuple[float, ...]] = (0.0, 0.25, 0.5, 0.9)
RATE_WINDOWS: Final[tuple[int | None, ...]] = (None, 1, 4, 8)

# The trailing category windows a mutation may carry, from an empty window through
# to one holding no error at all. The empty window is the case a rate rule has no
# rate for rather than a rate of zero.
RECENT_WINDOWS: Final[tuple[tuple[EventCategory, ...], ...]] = (
    (),
    (EventCategory.ERROR,),
    (EventCategory.TOOL_CALL, EventCategory.TOOL_CALL, EventCategory.ERROR, EventCategory.ERROR),
    (EventCategory.ERROR, EventCategory.ERROR, EventCategory.ERROR, EventCategory.TOOL_CALL),
    (EventCategory.TOOL_CALL,) * 8,
)

# How a payload is shaped: a path alone, a command alone, both, or neither. The
# empty form matters because a rule of a pattern kind must not match a mutation
# carrying no subject for it.
PAYLOAD_FORMS: Final[int] = 4

# How many variants one mutation plan is drawn across. The count is the size of the
# payload cross product, so every form is paired with every path and every command
# equally often, and the cost and window choices decompose the same number.
MUTATION_VARIANTS: Final[int] = PAYLOAD_FORMS * len(PATHS) * len(COMMANDS)

# How many variants one rule plan is drawn across, likewise the size of the largest
# cross product a single kind decomposes it into.
RULE_VARIANTS: Final[int] = len(RATE_CEILINGS) * len(RATE_WINDOWS)

# How often a rule is drawn disabled. Weighted rather than drawn by a coin, because
# a disabled rule is one arm that needs reaching rather than half of every set.
ENABLED_SHARE: Final[tuple[bool, ...]] = (True, True, True, False)


class ConsumptionMode(StrEnum):
    """The two mechanisms a mutation stream may be consumed by."""

    CHANGEFEED = "changefeed"
    POLLING = "polling"


# ---------------------------------------------------------------------------
# What a drawn example is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RulePlan:
    """One rule before it is built: which kind, which action, which variant."""

    index: int
    kind: MatchKind
    action: PolicyAction
    variant: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class MutationPlan:
    """One mutation before it is built, decomposed into one variant plus its keys."""

    position: int
    table: MutationTable
    category: EventCategory
    variant: int
    session_index: int
    client_index: int
    offset_seconds: int


@dataclass(frozen=True, slots=True)
class Example:
    """One drawn stream, the rule set offered against it, and two reorderings.

    Attributes:
        rules: The rule set as it was offered, repeats included.
        permuted_rules: The same set in a drawn arrival order.
        stream: The mutations in the order they were recorded.
        reordered: An interleaving of the per-Session subsequences of the stream,
            so every mutation keeps its place within its own Session and moves
            freely against every other Session's.
        replay_from: Where a restart resumes, so the tail from here is redelivered.
    """

    rules: tuple[PolicyRule, ...]
    permuted_rules: tuple[PolicyRule, ...]
    stream: tuple[Mutation, ...]
    reordered: tuple[Mutation, ...]
    replay_from: int

    @property
    def kinds(self) -> frozenset[MatchKind]:
        """The match kinds this example's enabled rules cover."""
        return frozenset(MatchKind(rule.match_kind) for rule in self.rules if rule.enabled)

    @property
    def sessions(self) -> int:
        """How many Sessions this stream spans."""
        return len({item.session_id for item in self.stream})


@dataclass(frozen=True, slots=True)
class Applied:
    """What one delivery order produced, keyed the way the schema keys it.

    Attributes:
        matches: One outcome per mutation and rule, which is the uniqueness the
            deduplicating constraints hold, so a redelivery adds nothing.
        governing: The action that governs each mutation, or None where nothing
            matched it.
        actions: The distinct actions the whole stream triggered, in severity
            order, as the evaluation module's own canonicalisation reports them.
    """

    matches: Mapping[tuple[UUID, UUID], PolicyOutcome]
    governing: Mapping[UUID, PolicyAction | None]
    actions: tuple[PolicyAction, ...]


# ---------------------------------------------------------------------------
# Building one rule and one mutation
# ---------------------------------------------------------------------------


def rule_name(index: int) -> str:
    """The name a rule of one index carries, and therefore the identifier it takes."""
    return f"policy-rule-{index:02d}"


def realise_rule(plan: RulePlan) -> PolicyRule:
    """Build one rule from a drawn plan, filling the field its kind requires.

    The identifier is derived from the name, so a rule offered twice is the same
    rule rather than two rules that happen to look alike. Distinct indices keep
    distinct identifiers, which is what keeps every drawn set well formed.
    """
    name = rule_name(plan.index)
    kind = plan.kind
    pattern: str | None = None
    client_id: UUID | None = None
    threshold: float | None = None
    window_events: int | None = None
    if kind is MatchKind.FILE_PATH:
        pattern = PATH_PATTERNS[plan.variant % len(PATH_PATTERNS)]
    elif kind is MatchKind.SHELL_COMMAND:
        pattern = COMMAND_PATTERNS[plan.variant % len(COMMAND_PATTERNS)]
    elif kind is MatchKind.CLIENT:
        client_id = CLIENT_IDS[plan.variant % len(CLIENT_IDS)]
    elif kind is MatchKind.SESSION_COST:
        threshold = COST_CEILINGS[plan.variant % len(COST_CEILINGS)]
    else:
        threshold = RATE_CEILINGS[plan.variant % len(RATE_CEILINGS)]
        window_events = RATE_WINDOWS[plan.variant // len(RATE_CEILINGS) % len(RATE_WINDOWS)]
    return PolicyRule(
        id=rule_identifier(name),
        name=name,
        match_kind=kind,
        action=plan.action,
        enabled=plan.enabled,
        pattern=pattern,
        client_id=client_id,
        threshold=threshold,
        window_events=window_events,
    )


def payload_for(variant: int) -> JsonObject:
    """The payload one variant implies: a path, a command, both, or neither."""
    form = variant % PAYLOAD_FORMS
    path = PATHS[variant // PAYLOAD_FORMS % len(PATHS)]
    command = COMMANDS[variant // (PAYLOAD_FORMS * len(PATHS)) % len(COMMANDS)]
    if form == 0:
        return {"path": path}
    if form == 1:
        return {"command": command}
    if form == 2:
        return {"path": path, "command": command}
    return {}


def realise_mutation(plan: MutationPlan) -> Mutation:
    """Build one mutation from a drawn plan.

    The identifier is derived from the position rather than drawn, so a failing
    example replays to the same stream and the polling order's identifier tiebreak
    is stable across the two deliveries of one example.
    """
    table = plan.table
    ledger = table is MutationTable.LEDGER
    return Mutation(
        table=table,
        row_id=UUID(int=plan.position + 1),
        session_id=SESSION_IDS[plan.session_index],
        client_id=CLIENT_IDS[plan.client_index],
        occurred_at=FIXED_INSTANT + timedelta(seconds=plan.offset_seconds),
        category=plan.category if ledger else None,
        payload=payload_for(plan.variant),
        session_cost_usd=COSTS[plan.variant % len(COSTS)],
        recent_categories=RECENT_WINDOWS[plan.variant % len(RECENT_WINDOWS)],
    )


def interleaved(stream: Sequence[Mutation], labels: Sequence[UUID]) -> tuple[Mutation, ...]:
    """Reorder a stream by a drawn label sequence, keeping each Session's own order.

    The labels are a permutation of the stream's Session identifiers taken with
    multiplicity, so walking them and taking the next unconsumed mutation of each
    Session yields an interleaving: independent mutations move against each other
    and no mutation overtakes another of its own Session.
    """
    queues: dict[UUID, list[Mutation]] = {}
    for item in stream:
        queues.setdefault(item.session_id, []).append(item)
    taken: dict[UUID, int] = dict.fromkeys(queues, 0)
    reordered: list[Mutation] = []
    for label in labels:
        reordered.append(queues[label][taken[label]])
        taken[label] += 1
    return tuple(reordered)


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def rule_plans(index: int) -> st.SearchStrategy[RulePlan]:
    """Draw one rule of a fixed index, so its identifier is settled before its shape."""
    return st.builds(
        RulePlan,
        st.just(index),
        st.sampled_from(MatchKind),
        st.sampled_from(PolicyAction),
        st.integers(min_value=0, max_value=RULE_VARIANTS - 1),
        st.sampled_from(ENABLED_SHARE),
    )


def mutation_plans(position: int) -> st.SearchStrategy[MutationPlan]:
    """Draw one mutation of a fixed position, so its identifier is settled likewise."""
    return st.builds(
        MutationPlan,
        st.just(position),
        st.sampled_from(MutationTable),
        st.sampled_from(EventCategory),
        st.integers(min_value=0, max_value=MUTATION_VARIANTS - 1),
        st.integers(min_value=0, max_value=len(SESSION_IDS) - 1),
        st.integers(min_value=0, max_value=len(CLIENT_IDS) - 1),
        st.integers(min_value=0, max_value=MAX_OFFSET_SECONDS),
    )


@st.composite
def mutation_streams_and_rules(draw: st.DrawFn) -> Example:
    """Draw a mutation stream, a rule set, and a permutation over independent mutations.

    The rule indices are drawn distinct, so no two different rules share an
    identifier and every drawn set is one the loader would accept. Repeats are then
    added as whole entries of the same rule, which is the redundancy a set really
    exhibits: the same rule reached twice through the same identifier.
    """
    indices = draw(
        st.lists(
            st.integers(min_value=0, max_value=MAX_RULES - 1),
            unique=True,
            max_size=MAX_RULES,
        )
    )
    declared = tuple(realise_rule(draw(rule_plans(index))) for index in sorted(indices))
    repeats = draw(st.integers(min_value=0, max_value=len(declared)))
    offered = (*declared, *declared[:repeats])
    permuted_rules = tuple(draw(st.permutations(list(offered))))

    size = draw(st.integers(min_value=MIN_MUTATIONS, max_value=MAX_MUTATIONS))
    stream = tuple(realise_mutation(draw(mutation_plans(position))) for position in range(size))
    labels = draw(st.permutations([item.session_id for item in stream]))
    return Example(
        rules=offered,
        permuted_rules=permuted_rules,
        stream=stream,
        reordered=interleaved(stream, labels),
        replay_from=draw(st.integers(min_value=0, max_value=size)),
    )


# ---------------------------------------------------------------------------
# The two consumption mechanisms, as delivery orders
# ---------------------------------------------------------------------------


def changefeed_delivery(example: Example) -> tuple[Mutation, ...]:
    """What the primary path delivers: the drawn interleaving, then the replayed tail.

    A changefeed makes no promise about the order two ranges arrive in, which the
    interleaving stands for, and a restart resumes from the persisted watermark,
    which redelivers everything after it. A resolved row carries no row payload and
    so contributes no mutation, which is why nothing here stands for one.
    """
    return (*example.reordered, *example.reordered[example.replay_from :])


def polling_delivery(example: Example) -> tuple[Mutation, ...]:
    """What the fallback path delivers: the stream by recorded instant then identifier.

    This is the ordering the fallback statement asks for, so the arm is the real
    difference between the two mechanisms rather than a relabelling of one order.
    """
    return tuple(sorted(example.stream, key=lambda item: (item.occurred_at, item.row_id)))


def delivery_for(mode: ConsumptionMode, example: Example) -> tuple[Mutation, ...]:
    """The delivery order one mechanism produces for a drawn example."""
    if mode is ConsumptionMode.CHANGEFEED:
        return changefeed_delivery(example)
    return polling_delivery(example)


def applied(delivered: Sequence[Mutation], rules: Sequence[PolicyRule]) -> Applied:
    """Evaluate a delivery order and record what it applied, keyed as the schema keys it.

    A key already held is asserted to carry the identical outcome rather than
    silently kept, so the deduplication that models the uniqueness constraints
    cannot hide a divergence between two deliveries of one mutation.
    """
    matches: dict[tuple[UUID, UUID], PolicyOutcome] = {}
    governing: dict[UUID, PolicyAction | None] = {}
    for mutation in delivered:
        outcomes = evaluate(mutation, rules)
        for outcome in outcomes:
            key = (mutation.row_id, outcome.rule_id)
            held = matches.get(key)
            assert held is None or held == outcome, (
                f"one mutation and rule pair produced two different outcomes, "
                f"{held.action} then {outcome.action}"
            )
            matches[key] = outcome
        decided = governing_action(outcomes)
        held_action = governing.get(mutation.row_id, decided)
        assert held_action == decided, (
            f"one mutation was governed by {held_action} on one delivery and by "
            f"{decided} on another"
        )
        governing[mutation.row_id] = decided
    return Applied(
        matches=matches,
        governing=governing,
        actions=triggered_actions(tuple(matches.values())),
    )


# ---------------------------------------------------------------------------
# The coverage record
# ---------------------------------------------------------------------------


def size_band(size: int) -> str:
    """Which part of the size range a stream sits in, for the coverage record."""
    if size == MIN_MUTATIONS:
        return "one mutation"
    if size < MAX_MUTATIONS // 2:
        return "under half the bound"
    return "half the bound or more"


def record_coverage(example: Example, outcome: Applied) -> None:
    """Report what one example covered, so the arms can be seen to be reached."""
    event(f"stream size={size_band(len(example.stream))}")
    event(f"sessions spanned={example.sessions}")
    event(f"rules offered={len(example.rules)}")
    event(f"reordering moved the stream={example.reordered != example.stream}")
    event(f"tail redelivered={example.replay_from < len(example.stream)}")
    event(f"anything matched={bool(outcome.matches)}")
    for kind in sorted(example.kinds):
        event(f"enabled rule kind={kind}")
    for action in outcome.actions:
        event(f"triggered action={action}")


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 25: For any mutation stream and any Policy_Rule set, the
# set of triggered Policy_Actions is independent of the order in which independent
# mutations are evaluated, and is identical whether the stream was consumed by
# changefeed or by polling.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(example=mutation_streams_and_rules())
def test_the_triggered_action_set_is_independent_of_order_and_of_consumption(
    example: Example,
) -> None:
    recorded = applied(example.stream, example.rules)
    record_coverage(example, recorded)

    changefeed = applied(delivery_for(ConsumptionMode.CHANGEFEED, example), example.rules)
    polling = applied(delivery_for(ConsumptionMode.POLLING, example), example.rules)

    # Requirements 23.4 and 23.5, order independence: the interleaving moved every
    # mutation relative to every other Session's, and the triggered action set came
    # out the same.
    assert changefeed.actions == recorded.actions, (
        f"the reordered stream triggered {changefeed.actions} where the recorded "
        f"order triggered {recorded.actions}"
    )

    # Requirement 23.3, mechanism independence: the polling order is a different
    # order of the same mutations, and it triggers the same set.
    assert polling.actions == recorded.actions, (
        f"the polled stream triggered {polling.actions} where the changefeed order "
        f"triggered {recorded.actions}"
    )

    # The stronger claim the action set alone could not carry: every mutation is
    # matched by the same rules, with the same recorded detail, under both
    # mechanisms. An action set can agree by coincidence; a match set cannot.
    assert changefeed.matches == recorded.matches
    assert polling.matches == recorded.matches

    # And each mutation resolves to one governing action, the same one either way,
    # so the kill switch and the approval queue see one verdict per mutation
    # rather than whichever the arriving order suggested.
    assert changefeed.governing == recorded.governing
    assert polling.governing == recorded.governing

    # The redelivered tail a restart replays contributes no second match, which is
    # what the deduplicating uniqueness constraints hold, so the delivery really was
    # longer than the stream and the applied set really was not.
    delivered = delivery_for(ConsumptionMode.CHANGEFEED, example)
    assert len(delivered) == len(example.stream) + (len(example.stream) - example.replay_from)
    assert len(changefeed.matches) == len(recorded.matches)

    # The rule set is a set: permuting the order the rules arrived in, and offering
    # some of them a second time, changes nothing about what fires.
    assert applied(example.stream, example.permuted_rules) == recorded

    # Per mutation, the canonical outcome list is identical under both rule orders,
    # rather than merely holding the same outcomes. That is what makes a match row
    # sequence reproducible rather than only its content.
    for mutation in example.stream:
        assert evaluate(mutation, example.rules) == evaluate(mutation, example.permuted_rules)
