"""The Confidence_Tracker: what a procedure has earned, and the arithmetic of earning it.

A learned procedure that is only accumulated is a claim nobody has checked. A
learned procedure whose standing moves with how the Sessions that used it ended is
a claim the fleet has been testing continuously, and the recall path reads that
standing, so the next agent's decision is shaped by the recorded consequences of
the last agent's. This package is what makes the second thing true: the four
entry points below record use, record consequences, move the standing, and hand
back the audited history of every movement.

The statements live in the data-access layer and nothing here issues SQL. What
lives here is everything that is not a statement, and each piece is here for a
reason.

**The policy is read from the configuration surface in one place.** The initial
standing, the increment, the decrement, and the recall floor are four keys of that
surface and no number among them is written into this codebase twice. The floor in
particular is read from here by the recall path as well, because a floor the
tracker and the recall predicate disagreed about would exclude procedures one of
them believed were included, and the disagreement would be invisible in both.

**The arithmetic is a pure function of the prior value and the delta.** Whether an
adjustment moves a standing, how far, and in which direction is decided by a
function over two numbers, so the rule is testable without a cluster and the same
rule can be read off a stored transition afterwards. The authoritative arithmetic
is still the cluster's — the adjusting statement clamps over the row's own current
value — and this function is what predicts and interprets it. The two agreeing is
asserted against a live instance rather than assumed.

**The decrement is deliberately larger than the increment.** A procedure that
misleads an agent costs more than a procedure that helps one, so standing is lost
faster than it is earned, and a procedure that has started to mislead falls below
the recall floor after a couple of failures rather than after a dozen.

**A retrieval is evidence and an outcome is a verdict.** Recording that a
procedure was retrieved never moves the value: being shown to an agent is not
being right. Only a Session's terminal outcome moves it, and only two of the three
classifications do. An abandoned Session is a deliberate non-adjustment rather
than an oversight: a Session the engineer walked away from says nothing about
whether the procedure was sound, and treating it as a failure would let ordinary
interruption erode good procedures.

**A movement absorbed by a bound is still on the record, as an outcome without a
change record.** The change table admits no row whose endpoints are equal, so an
increment applied to a standing already at the ceiling writes no change record —
recording one would put a movement in the history that never happened. What an
operator needs to tell apart is then still there: an outcome row with no change
record beside it is an adjustment that was applied and absorbed, and no outcome row
at all is nothing having happened. The measurement counts movements rather than
attempts for the same reason, and the absorbed case is written as a log record
naming the bound it met.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from molt.config.resolve import Configuration, load_configuration
from molt.models.artifact import (
    CONFIDENCE_CEILING,
    CONFIDENCE_FLOOR,
    DerivedArtifactKind,
    require_unit_interval,
)
from molt.models.session import SessionOutcome
from molt.store import MemoryStore
from molt.store.confidence import (
    DEFAULT_HISTORY_LIMIT,
    ConfidenceChange,
    OutcomeApplication,
    OutcomeRecord,
    ProcedureStanding,
    RetrievalRecord,
    apply_outcome,
    change_history,
    procedure_standing,
)
from molt.store.confidence import record_retrieval as write_retrieval
from molt.telemetry import Severity, log, metric

__all__ = [
    "COMPONENT",
    "CONFIDENCE_CHANGES_METRIC",
    "DIRECTION_DIMENSION",
    "DIRECTION_DOWN",
    "DIRECTION_UP",
    "FAILURE_DELTA_KEY",
    "INITIAL_KEY",
    "RECALL_FLOOR_KEY",
    "SUCCESS_DELTA_KEY",
    "ConfidenceChange",
    "ConfidencePolicy",
    "Movement",
    "OutcomeApplication",
    "OutcomeRecord",
    "ProcedureStanding",
    "RetrievalRecord",
    "adjusted",
    "clamped",
    "direction_of",
    "history",
    "initial_standing",
    "movement",
    "record_outcome",
    "record_retrieval",
    "summary",
]

# The component name every record from this package carries.
COMPONENT: Final[str] = "confidence"

# The four configuration surface keys the policy is read from. The numbers behind
# them live on that surface and nowhere here: a constant of this package standing
# in for one would be a second place to change, and the one an operator did not
# change would be the one that took effect.
INITIAL_KEY: Final[str] = "MOLT_PROCEDURE_CONFIDENCE_INITIAL"
SUCCESS_DELTA_KEY: Final[str] = "MOLT_PROCEDURE_CONFIDENCE_SUCCESS_DELTA"
FAILURE_DELTA_KEY: Final[str] = "MOLT_PROCEDURE_CONFIDENCE_FAILURE_DELTA"

# The recall floor. It is a key of this package rather than of the recall path
# because two components read it -- the recall predicate excludes below it and the
# erasure sweep includes below it -- and a floor stated in two places is a floor
# two components can disagree about.
RECALL_FLOOR_KEY: Final[str] = "MOLT_PROCEDURE_RECALL_FLOOR"

# The measurement one committed movement produces, and the dimension telling the
# two directions apart. Two dimension values and no tenant dimension, so the
# billable combination count is a fixed pair rather than something that grows with
# the number of Clients or procedures.
CONFIDENCE_CHANGES_METRIC: Final[str] = "procedure.confidence_changes"
DIRECTION_DIMENSION: Final[str] = "direction"
DIRECTION_UP: Final[str] = "up"
DIRECTION_DOWN: Final[str] = "down"


# ---------------------------------------------------------------------------
# The configured policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfidencePolicy:
    """The four configured numbers procedural standing moves by.

    Both deltas are magnitudes rather than signed values, because the sign belongs
    to the classification rather than to the configuration: an operator raising the
    failure delta is saying failures cost more, not that they help.

    Attributes:
        initial: The standing every learned procedure is written with.
        success_delta: How far a succeeded outcome raises a standing.
        failure_delta: How far a failed outcome lowers one.
        recall_floor: The standing below which recall excludes a procedure while
            the store retains it and erasure still reaches it.
    """

    initial: float
    success_delta: float
    failure_delta: float
    recall_floor: float

    def __post_init__(self) -> None:
        """Refuse a policy naming a value the closed unit interval does not hold."""
        require_unit_interval(self.initial, "a configured initial procedure confidence")
        require_unit_interval(self.success_delta, "a configured confidence increment")
        require_unit_interval(self.failure_delta, "a configured confidence decrement")
        require_unit_interval(self.recall_floor, "a configured recall floor")

    @classmethod
    def from_configuration(cls, configuration: Configuration | None = None) -> ConfidencePolicy:
        """Read the policy from the configuration surface, resolving it if needed.

        A caller recording many outcomes resolves the configuration once and hands
        the policy to each call, so a batch costs one resolution rather than one
        per outcome.
        """
        resolved = load_configuration() if configuration is None else configuration
        return cls(
            initial=resolved.number(INITIAL_KEY),
            success_delta=resolved.number(SUCCESS_DELTA_KEY),
            failure_delta=resolved.number(FAILURE_DELTA_KEY),
            recall_floor=resolved.number(RECALL_FLOOR_KEY),
        )

    def delta_for(self, outcome: SessionOutcome) -> float | None:
        """The signed adjustment one classification asks for, or None for none.

        None is the abandoned case and means no adjustment is attempted at all,
        which is a different statement from a delta of zero: a zero delta would
        still read the standing and write it back unchanged, and the distinction
        matters to anybody reading the statements a recorded outcome sent.
        """
        member = SessionOutcome(outcome)
        if member is SessionOutcome.SUCCEEDED:
            return self.success_delta
        if member is SessionOutcome.FAILED:
            return -self.failure_delta
        if member is SessionOutcome.ABANDONED:
            return None
        raise ValueError("an outcome record names a terminal session classification")

    def below_floor(self, confidence: float) -> bool:
        """Whether a standing sits below the recall floor.

        Recall applies the same comparison inside its own predicate rather than
        calling this, because a floor applied after truncation would shrink a page
        of results. This is the form every other reader uses, so the comparison
        itself is stated once.
        """
        return confidence < self.recall_floor


def initial_standing(kind: DerivedArtifactKind, *, policy: ConfidencePolicy | None = None) -> float:
    """The standing a newly written Derived_Artifact of one kind carries.

    The learned-procedure kind carries the configured initial value and no other
    kind carries a standing at all, which is the equivalence the schema states and
    the model restates. Returning the value from here rather than from a caller's
    own constant is what keeps a procedure from ever being written with a standing
    this codebase chose instead of one an operator configured.

    Raises:
        ValueError: The kind carries no standing, so there is no initial value to
            report and a caller asking for one has confused two kinds.
    """
    if DerivedArtifactKind(kind) is not DerivedArtifactKind.LEARNED_PROCEDURE:
        raise ValueError("only a learned procedure carries a procedure confidence value")
    chosen = ConfidencePolicy.from_configuration() if policy is None else policy
    return chosen.initial


# ---------------------------------------------------------------------------
# The arithmetic, as a pure function of the prior value and the delta
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Movement:
    """One adjustment as arithmetic: what was asked for and what the bounds allowed.

    Attributes:
        prior: The standing the adjustment starts from.
        attempted_delta: The signed adjustment asked for, or None when none was.
        new_value: The standing after clamping, equal to the prior value when
            nothing was attempted or when a bound absorbed the whole delta.
    """

    prior: float
    attempted_delta: float | None
    new_value: float

    @property
    def applied_delta(self) -> float:
        """How far the standing actually moved, which is what a change record states."""
        return self.new_value - self.prior

    @property
    def moved(self) -> bool:
        """Whether the standing changed, which is whether a change record is written."""
        return self.new_value != self.prior

    @property
    def absorbed(self) -> bool:
        """Whether an adjustment was asked for and a bound took all of it."""
        return self.attempted_delta is not None and not self.moved

    @property
    def direction(self) -> str | None:
        """Which way the standing moved, or None when it did not move."""
        return direction_of(self.prior, self.new_value)


def direction_of(prior: float, new_value: float) -> str | None:
    """Which way a transition went, or None when it went nowhere.

    Stated as a function over the two endpoints so a transition read back out of
    the change history is named the same way as one that has just been applied.
    """
    if new_value == prior:
        return None
    return DIRECTION_UP if new_value > prior else DIRECTION_DOWN


def clamped(value: float) -> float:
    """A standing held inside the closed unit interval, both ends admitted."""
    return max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, value))


def adjusted(prior: float, delta: float) -> float:
    """The standing one delta produces from a prior value, clamped to the interval.

    This is the same arithmetic the adjusting statement performs, stated here so it
    can be driven over the whole interval without a cluster and so a stored
    transition can be checked against the rule that should have produced it.
    """
    return clamped(prior + delta)


def movement(prior: float, delta: float | None) -> Movement:
    """What one delta does to a prior standing, bounds included.

    A delta of None is the deliberate non-adjustment and produces a movement whose
    new value is the prior value, so a caller reads one shape whether an adjustment
    was attempted or not.
    """
    require_unit_interval(prior, "a prior procedure confidence")
    return Movement(
        prior=prior,
        attempted_delta=delta,
        new_value=prior if delta is None else adjusted(prior, delta),
    )


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------


def record_retrieval(store: MemoryStore, procedure_id: UUID, session_id: UUID) -> None:
    """Record that one Session was returned one procedure, moving no standing.

    Called once per returned procedure by the recall path, after the response has
    been composed, so it is not on the latency path. The value is untouched by
    design: retrievals are counted for the console, and counting them into the
    arithmetic would let a popular procedure earn standing it never demonstrated.
    """
    written = write_retrieval(store, procedure_id, session_id)
    log(
        Severity.DEBUG,
        COMPONENT,
        "recorded a learned procedure retrieval, moving no standing",
        procedure_id=str(written.procedure_id),
        session_id=str(written.session_id),
    )


def record_outcome(
    store: MemoryStore,
    procedure_id: UUID,
    session_id: UUID,
    outcome: SessionOutcome,
    *,
    policy: ConfidencePolicy | None = None,
) -> ConfidenceChange | None:
    """Record how a Session that consumed a procedure ended, and move the standing.

    The classification stored is the Session's own, read out of the Session row by
    the writing statement; the argument here is the assertion the cluster checks
    against it. An outcome for a procedure the Session never retrieved is refused,
    and a repeated report for a pair already on the record changes nothing.

    Args:
        store: The store the transaction runs on.
        procedure_id: The procedure the Session consumed.
        session_id: The Session whose terminal outcome this is.
        outcome: What that Session reached, drawn from the three terminal
            classifications.
        policy: The configured numbers to apply, resolved from the configuration
            surface when a caller names none.

    Returns:
        The audited change record, or None when the standing did not move: an
        abandoned outcome, an adjustment a bound absorbed entirely, or a report
        already on the record. The three are told apart in the log record and, for
        the first two, by the outcome row this call left behind.

    Raises:
        StoreError: The outcome was refused, and the message names the premise that
            failed.
    """
    chosen = ConfidencePolicy.from_configuration() if policy is None else policy
    classification = SessionOutcome(outcome)
    application = apply_outcome(
        store,
        procedure_id,
        session_id,
        classification,
        delta=chosen.delta_for(classification),
    )
    return _report(application, procedure_id, session_id, classification)


def history(
    store: MemoryStore,
    procedure_id: UUID,
    *,
    limit: int | None = None,
) -> tuple[ConfidenceChange, ...]:
    """One procedure's confidence change records, ordered by change timestamp.

    The bound is optional here and defaulted by the owning store module, so the one
    default lives in one place rather than being restated by this call.
    """
    return change_history(
        store,
        procedure_id,
        limit=DEFAULT_HISTORY_LIMIT if limit is None else limit,
    )


def summary(store: MemoryStore, procedure_id: UUID) -> ProcedureStanding:
    """One procedure's current standing, retrieval count, and outcome counts.

    The three numbers the console shows per procedure, read together so a rendered
    row cannot pair a standing with counts taken from a different instant.
    """
    return procedure_standing(store, procedure_id)


# ---------------------------------------------------------------------------
# Reporting a committed adjustment
# ---------------------------------------------------------------------------


def _report(
    application: OutcomeApplication,
    procedure_id: UUID,
    session_id: UUID,
    outcome: SessionOutcome,
) -> ConfidenceChange | None:
    """Emit the measurement and the record for a transaction that has committed.

    Separate from the transaction on purpose: a conflict runs that body again, and
    a measurement emitted from inside it would count an adjustment that was rolled
    back. So exactly one commit produces exactly one measurement, whatever the
    retry wrapper had to do to get there.
    """
    fields: dict[str, object] = {
        "procedure_id": str(procedure_id),
        "session_id": str(session_id),
        "outcome": outcome.value,
    }
    change = application.change
    if change is not None:
        direction = direction_of(change.prior_value, change.new_value)
        if direction is not None:
            metric(CONFIDENCE_CHANGES_METRIC, 1.0, **{DIRECTION_DIMENSION: direction})
        log(
            Severity.INFO,
            COMPONENT,
            "moved a learned procedure's standing and recorded the change",
            **fields,
            prior_value=change.prior_value,
            new_value=change.new_value,
            direction=direction,
        )
        return change

    if application.absorbed:
        log(
            Severity.INFO,
            COMPONENT,
            "applied an outcome to a standing already at a bound, so no change was recorded",
            **fields,
            standing=application.prior_value,
        )
    elif not application.recorded:
        log(
            Severity.INFO,
            COMPONENT,
            "left a standing alone because that session's outcome was already recorded",
            **fields,
        )
    else:
        log(
            Severity.INFO,
            COMPONENT,
            "recorded an outcome that adjusts no standing by design",
            **fields,
        )
    return None
