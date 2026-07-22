"""The erasure fence: the current-generation read and the guarded write wrapper.

Erasure ownership is a lease carrying a monotonic generation, and the point of the
fence is that a worker that lost ownership cannot record evidence. Four claims
arrange this module, and the first is the one the module exists for.

**The generation read sits inside the same transaction as the write, and that is
the whole mechanism.** A check in one transaction followed by a write in another
leaves a window between them in which the lease is taken over, and a write that
commits in that window is exactly the write the fence exists to refuse: evidence
recorded by a superseded owner, indistinguishable afterwards from evidence
recorded by the owner that really held the run. Reading the current generation on
the write's own cursor, inside the write's own transaction, closes the window
structurally. Under SERIALIZABLE the read joins the transaction's read set, which
orders the guarded write against every takeover that touches the row it matched:
either the write is ordered before the takeover, and it committed while its
generation was still the current one, or it is ordered after, and then the read
that admitted it is invalidated, the wrapper retries, the retry re-reads a bumped
generation, and the write is refused with nothing persisted. There is no ordering
in which a row from a superseded owner commits. A guard read taken before the
transaction was opened belongs to no such ordering at all: it reports what was
true at an instant the write does not share, so it would satisfy every assertion
about a refusal and still admit that row.

**A retry that now sees a bumped generation refuses rather than loops.** The
transaction wrapper retries a serialization conflict and runs the body again from
the beginning, so the generation read runs again too. When the re-read reports a
generation the caller does not hold, the refusal is raised, and because that
refusal carries no serialization state the wrapper propagates it on that attempt
instead of retrying. That is deliberate: a superseded write can never be admitted
by running it again, so looping would spend the whole retry budget to arrive at
the exhaustion failure, which names conflict where the truth is supersession, and
would report the wrong fault to an operator. The refusal is raised before the
body runs, so the refused transaction has sent no write statement at all and its
rollback discards nothing.

**A refusal names both generations, and the metric it emits carries no
dimensions.** The caller learns it was superseded rather than merely that it
failed, which is what lets a run abort rather than retry, and the current owner
travels as a note on the failure so an operator learns whom to ask. The
measurement is a bare counter: a per-Client or per-owner dimension would multiply
into the billable metric bound the telemetry surface exists to hold, and the
identities belong in the log record and the failure, which are not billed per
combination.

**Expiry is not this module's question, and neither is who may hold the lease.**
The predicate turns on the generation alone, because that is what the acceptance
criterion turns on: a lease whose expiry has passed but which nobody has taken
over still carries the current generation, and its holder's write is admitted
until a takeover bumps the generation. Deciding that a lease may be taken over,
evaluating expiry against the cluster's clock rather than a worker's, and
assigning the successor generation all belong to the lease lifecycle. This module
reads the generation that lifecycle recorded and guards writes with it.

Every statement here is a whole module-level literal, the tenant identifier is
the one bound value, and no identifier is interpolated. The wrapper composes with
the store's serializable retry wrapper rather than reimplementing any part of it,
and the three named forms differ from each other in the transaction label alone,
so the write statements of the Disposition record, the run completion, and the
certificate stay with the modules that own them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, TypeVar
from uuid import UUID

from molt.errors import (
    ErasureInFlightError,
    LeaseNotHeld,
    StaleFencingGeneration,
    StaleFencingGenerationError,
    StoreError,
)
from molt.models.event import require_aware
from molt.store import Cursor, MemoryStore
from molt.telemetry import Severity, log, metric

__all__ = [
    "ACTIVE_RUN_QUERY",
    "CERTIFICATE_LABEL",
    "COMPONENT",
    "CURRENT_GENERATION_QUERY",
    "DISPOSITION_LABEL",
    "ERASURE_IN_FLIGHT_METRIC",
    "FENCED_LABEL",
    "FIRST_GENERATION",
    "GUARDED_BINDING_LABEL",
    "RUN_COMPLETION_LABEL",
    "STALE_GENERATION_METRIC",
    "ActiveRun",
    "CurrentGeneration",
    "fenced",
    "fenced_certificate",
    "fenced_disposition",
    "fenced_run_completion",
    "guarded_binding_write",
    "require_current_generation",
    "require_no_active_run",
    "select_active_run",
    "select_current_generation",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The measurement emitted for each write the predicate refuses. Undimensioned on
# purpose: the tenant and the owner are unbounded, so attaching either would turn
# one billable metric into as many as there are workers.
STALE_GENERATION_METRIC: Final[str] = "erasure.stale_generation_refused"

# The lowest generation a lease can carry, which the schema also refuses to go
# below. A presented value under it names no lease that was ever granted, so it is
# refused before a statement is sent.
FIRST_GENERATION: Final[int] = 1

# The one statement. It reads the lease that is current for the tenant, which the
# partial uniqueness constraint admits at most one of, and it is sent on the
# write's own cursor so the row it matched joins the write's read set. The
# ownership columns are read alongside the generation because a refusal reports
# whom the caller was superseded by, not merely that it was.
CURRENT_GENERATION_QUERY: Final[str] = (
    "SELECT id, owner, generation FROM erasure_lease WHERE client_id = %s AND superseded_at IS NULL"
)

# The labels the guarded transactions appear under in a log record and in the note
# an exhausted retry attaches. The three named forms differ in this and in nothing
# else, so an operator reading a log record learns which evidence write kept
# losing.
FENCED_LABEL: Final[str] = "fenced_write"
DISPOSITION_LABEL: Final[str] = "fenced_disposition"
RUN_COMPLETION_LABEL: Final[str] = "fenced_run_completion"
CERTIFICATE_LABEL: Final[str] = "fenced_certificate"
GUARDED_BINDING_LABEL: Final[str] = "guarded_binding_write"

# The measurement emitted for each binding write the erasure guard refuses.
# Undimensioned for the same reason the supersession counter is: the tenant and the
# run are both unbounded, and the identities belong in the log record and on the
# failure, neither of which is billed per combination.
ERASURE_IN_FLIGHT_METRIC: Final[str] = "store.erasure_in_flight_refused"

# The erasure guard's one statement. It reads the run that is in flight for the
# tenant, and the partial index on the status makes it a single-row lookup rather
# than a scan, which matters because every binding write performs it. The status
# value is bound rather than written into the predicate so the admitted set stays a
# value this module states once.
ACTIVE_RUN_QUERY: Final[str] = (
    "SELECT id, phase, started_at FROM erasure_run WHERE client_id = %s AND status = %s"
)

# The one status the guard treats as in flight, which the schema also admits.
RUNNING_STATUS: Final[str] = "running"

# How many columns each read returns, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_GENERATION_ROW_WIDTH: Final[int] = 3
_ACTIVE_RUN_ROW_WIDTH: Final[int] = 3

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrentGeneration:
    """The ownership a Client's current lease records, as one reading of it.

    Attributes:
        lease_id: The lease the reading came from, so a caller may relate a
            refusal to the lease history rather than only to a number.
        owner: The worker identity that holds erasure for this Client now.
        generation: The fence every guarded write for this Client must present.
    """

    lease_id: UUID
    owner: str
    generation: int

    def __post_init__(self) -> None:
        """Refuse a reading that could not have come from a granted lease."""
        if not self.owner:
            raise ValueError("a current lease records the owner that holds it")
        if self.generation < FIRST_GENERATION:
            raise ValueError(
                f"a fencing generation is at least {FIRST_GENERATION}, "
                "so nothing below it names a granted lease"
            )

    def admits(self, presented: int) -> bool:
        """Whether a write presenting one generation is the current owner's."""
        return presented == self.generation


@dataclass(frozen=True, slots=True)
class ActiveRun:
    """The Erasure_Run the guard read found in flight for a Client.

    The phase travels with the identifier because it is what tells an operator
    whether the refusal was worth it: a write refused while the sweep is still
    selecting would have been swept had it landed, and a write refused during the
    certificate phase arrived after the evidence was fixed. The instant the run
    started is carried for the same reason, so a refusal that names a run stuck in
    one phase is legible as such rather than as an ordinary race.
    """

    run_id: UUID
    phase: str
    started_at: datetime

    def __post_init__(self) -> None:
        """Refuse a reading that could not have come from a recorded run."""
        if not self.phase:
            raise ValueError("a recorded Erasure_Run names the phase it reached")
        require_aware(self.started_at, "an Erasure_Run start instant")


# ---------------------------------------------------------------------------
# The generation read
# ---------------------------------------------------------------------------


def select_current_generation(cursor: Cursor, client_id: UUID) -> CurrentGeneration | None:
    """The ownership currently recorded for one Client, or None when none is.

    Sent on a caller's cursor, which is the whole point: inside a write's
    transaction this read joins that transaction's read set, so a takeover that
    bumps the generation conflicts with it rather than slipping between a check
    and a write. Outside a transaction it is an ordinary read of who holds erasure
    for the tenant.

    Args:
        cursor: The cursor the caller's transaction, if any, is running on.
        client_id: The tenant whose current lease is read.

    Returns:
        The current lease's ownership, or None where the Client holds no current
        lease at all.

    Raises:
        StoreError: The read returned a row of a shape the schema does not
            declare.
    """
    cursor.execute(CURRENT_GENERATION_QUERY, (client_id,))
    row = cursor.fetchone()
    return None if row is None else _generation_of(row)


def require_current_generation(
    cursor: Cursor,
    client_id: UUID,
    generation: int,
) -> CurrentGeneration:
    """The guarded write predicate: admit the caller's write, or refuse it by name.

    This runs on the cursor the write will run on, before the write, and inside
    the same transaction, so the generation it read and the row the write would
    persist commit or abort together. A refusal raises before any write statement
    is sent, so the abandoned transaction had nothing to discard.

    Args:
        cursor: The cursor the write's transaction is running on.
        client_id: The tenant the write records evidence for.
        generation: The generation the writing owner believes it holds.

    Returns:
        The current ownership, which the presented generation matched.

    Raises:
        ValueError: The presented generation names no granted lease.
        LeaseNotHeldError: The Client holds no current lease, so this write
            belongs to no owner. Nothing was written.
        StaleFencingGenerationError: The presented generation is not the current
            one, so the caller has been superseded. The failure carries both
            generations and nothing was written.
    """
    if generation < FIRST_GENERATION:
        raise ValueError(
            f"a presented fencing generation is at least {FIRST_GENERATION}, "
            "so nothing below it can be current"
        )
    held = select_current_generation(cursor, client_id)
    if held is None:
        log(
            Severity.WARNING,
            COMPONENT,
            "a fenced write was refused because the Client holds no current erasure lease",
            client_id=str(client_id),
            presented_generation=generation,
        )
        raise LeaseNotHeld(
            f"no current erasure lease is recorded for the Client, so the write "
            f"presenting generation {generation} belongs to no owner and nothing was persisted"
        )
    if not held.admits(generation):
        raise _refusal(client_id, presented=generation, held=held)
    return held


# ---------------------------------------------------------------------------
# The erasure guard read
# ---------------------------------------------------------------------------


def select_active_run(cursor: Cursor, client_id: UUID) -> ActiveRun | None:
    """The Erasure_Run in flight for one Client, or None when none is.

    Sent on a caller's cursor, which is the whole point, and for the same reason
    the generation read is: inside a binding write's transaction this read joins
    that transaction's read set, so a run that starts concurrently conflicts with
    it rather than slipping between a check and a write. That is what makes the two
    admissible outcomes the only outcomes. Either this read is ordered before the
    run's insert, in which case the binding committed before the sweep and the
    sweep finds it, or it is ordered after, in which case the read that admitted
    the write is invalidated and SERIALIZABLE aborts one of the two.

    Taken outside a transaction it is an ordinary report of whether an erasure is
    running, and it guards nothing: it would describe an instant the write does not
    share.

    Args:
        cursor: The cursor the caller's transaction, if any, is running on.
        client_id: The tenant whose in-flight run is read.

    Returns:
        The run in flight, or None where the Client has none.

    Raises:
        StoreError: The read returned a row of a shape the schema does not
            declare.
    """
    cursor.execute(ACTIVE_RUN_QUERY, (client_id, RUNNING_STATUS))
    row = cursor.fetchone()
    return None if row is None else _active_run_of(row)


def require_no_active_run(cursor: Cursor, client_id: UUID) -> None:
    """The erasure guard predicate: admit the binding write, or refuse it by name.

    This runs on the cursor the write will run on, before the write, and inside the
    same transaction, so the read that admitted the write and the row the write
    would persist commit or abort together. A refusal raises before any write
    statement is sent, so the abandoned transaction had nothing to discard.

    The refusal is a distinct failure from a serialization abort on purpose. A
    write refused here found a run already recorded and never raced it; a write
    aborted by the wrapper raced a run that started underneath it. Both keep the
    Artifact out of the gap the certificate would not account for, and an operator
    reading a refusal learns which of the two happened.

    Args:
        cursor: The cursor the write's transaction is running on.
        client_id: The tenant the binding would name.

    Raises:
        ErasureInFlightError: An Erasure_Run for this Client is in flight, so the
            write is refused and nothing was persisted.
        StoreError: The read returned a row of a shape the schema does not
            declare.
    """
    active = select_active_run(cursor, client_id)
    if active is not None:
        raise _in_flight_refusal(client_id, active=active)


def _in_flight_refusal(client_id: UUID, *, active: ActiveRun) -> ErasureInFlightError:
    """The in-flight failure to raise, with the refusal recorded as it is built.

    The measurement and the log record are emitted here rather than by the caller,
    so every path that refuses a binding write for an in-flight erasure is counted,
    and counted once.
    """
    metric(ERASURE_IN_FLIGHT_METRIC)
    log(
        Severity.WARNING,
        COMPONENT,
        "a binding write was refused because an erasure run is in flight for the Client",
        client_id=str(client_id),
        run_id=str(active.run_id),
        phase=active.phase,
    )
    refusal = ErasureInFlightError(
        "an erasure run is in flight for this Client, so a binding write naming it "
        "is refused and nothing was persisted"
    )
    refusal.add_note(
        f"the run {active.run_id} reached the {active.phase} phase, so this Artifact "
        "would have landed in a window the run's certificate could not account for"
    )
    return refusal


def guarded_binding_write(
    store: MemoryStore,
    client_id: UUID,
    body: Callable[[Cursor], T],
    *,
    label: str = GUARDED_BINDING_LABEL,
) -> T:
    """Run a binding write behind the erasure guard, in one transaction.

    The transaction, its isolation level, and the bounded jittered retry of a
    conflict are all the store's serializable wrapper's, inherited rather than
    restated. What this adds is one statement at the front of that transaction: the
    in-flight run read, whose result decides whether the body runs at all.

    Because the wrapper runs the body again on a conflict, the guard read runs again
    too, and a retry that now finds a recorded run refuses rather than looping, for
    the same reason a superseded generation does: a write cannot become admissible
    by being run again once the run it would have slipped past is on the record.

    Args:
        store: The connection surface the transaction is framed by.
        client_id: The tenant the binding would name.
        body: The write to perform once the guard admits it. It must be free of
            effects outside the transaction it is handed, because a conflict runs
            it again from the beginning.
        label: What to call this transaction in a log record and in the note an
            exhausted retry attaches.

    Returns:
        Whatever the body returned, once its transaction has committed.

    Raises:
        ErasureInFlightError: A run for this Client is in flight. Nothing was
            written.
        SerializationExhaustedError: The transaction conflicted on every attempt
            the policy permitted, which is what a write racing a starting run
            reports once the budget is spent. Nothing it wrote is committed.
    """

    def framed(cursor: Cursor) -> T:
        require_no_active_run(cursor, client_id)
        return body(cursor)

    return store.in_serializable(framed, label=label)


def _refusal(
    client_id: UUID,
    *,
    presented: int,
    held: CurrentGeneration,
) -> StaleFencingGenerationError:
    """The supersession failure to raise, with the refusal recorded as it is built.

    The measurement and the log record are emitted here rather than by the caller,
    so every path that refuses a write for supersession is counted, and counted
    once.
    """
    metric(STALE_GENERATION_METRIC)
    log(
        Severity.WARNING,
        COMPONENT,
        "a fenced write was refused because the presented fencing generation is superseded",
        client_id=str(client_id),
        presented_generation=presented,
        current_generation=held.generation,
        current_owner=held.owner,
    )
    refusal = StaleFencingGeneration(presented, held.generation)
    refusal.add_note(
        f"erasure for this Client is held by {held.owner} under lease {held.lease_id}, "
        "so this write records no evidence and the run it belongs to is over"
    )
    return refusal


# ---------------------------------------------------------------------------
# The guarded write
# ---------------------------------------------------------------------------


def fenced(
    store: MemoryStore,
    client_id: UUID,
    generation: int,
    body: Callable[[Cursor], T],
    *,
    label: str = FENCED_LABEL,
) -> T:
    """Run a write body behind the guarded write predicate, in one transaction.

    The transaction, its isolation level, and the bounded jittered retry of a
    conflict are all the store's serializable wrapper's, inherited rather than
    restated. What this adds is one statement at the front of that transaction:
    the generation read, whose result decides whether the body runs at all.

    Because the wrapper runs the body again on a conflict, the generation read
    runs again too. A re-read that reports a bumped generation refuses on that
    attempt rather than retrying, since the refusal carries no serialization state
    for the wrapper to recognise. That is the intended interaction: a superseded
    write cannot become admissible by being run again, so looping would exhaust
    the retry budget and then report a conflict where the truth is supersession.

    Args:
        store: The connection surface the transaction is framed by.
        client_id: The tenant the write records evidence for.
        generation: The generation the writing owner believes it holds.
        body: The write to perform once the predicate admits it. It must be free
            of effects outside the transaction it is handed, because a conflict
            runs it again from the beginning.
        label: What to call this transaction in a log record and in the note an
            exhausted retry attaches.

    Returns:
        Whatever the body returned, once its transaction has committed.

    Raises:
        LeaseNotHeldError: The Client holds no current lease. Nothing was written.
        StaleFencingGenerationError: The presented generation is superseded. The
            failure carries both generations and nothing was written.
        SerializationExhaustedError: The transaction conflicted on every attempt
            the policy permitted. Nothing it wrote is committed.
    """

    def framed(cursor: Cursor) -> T:
        require_current_generation(cursor, client_id, generation)
        return body(cursor)

    return store.in_serializable(framed, label=label)


def fenced_disposition(
    store: MemoryStore,
    client_id: UUID,
    generation: int,
    body: Callable[[Cursor], T],
) -> T:
    """Run a Disposition write behind the fence, so a stale owner records no evidence.

    The write statement belongs to the module that owns the Disposition record;
    this is the form that module writes through, and the label is what makes a
    refused disposition distinguishable in a log record from a refused
    finalisation.
    """
    return fenced(store, client_id, generation, body, label=DISPOSITION_LABEL)


def fenced_run_completion(
    store: MemoryStore,
    client_id: UUID,
    generation: int,
    body: Callable[[Cursor], T],
) -> T:
    """Run a completion record behind the fence, so a stale owner declares nothing finished.

    A run whose owner was superseded mid-run must not be able to mark itself
    complete: the completion is the claim the certificate is assembled from, so
    admitting it from a superseded owner would let a certificate describe a run
    two workers jointly performed.
    """
    return fenced(store, client_id, generation, body, label=RUN_COMPLETION_LABEL)


def fenced_certificate(
    store: MemoryStore,
    client_id: UUID,
    generation: int,
    body: Callable[[Cursor], T],
) -> T:
    """Run a certificate insert behind the fence, so a stale owner cannot sign for a run.

    The certificate carries the generation of the owner that finalised the run,
    which is what makes the fencing claim auditable from the document rather than
    only from the database. The same predicate guards the insert, so the
    generation the document states is one that was current when it was written.
    """
    return fenced(store, client_id, generation, body, label=CERTIFICATE_LABEL)


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------


def _generation_of(row: Sequence[object]) -> CurrentGeneration:
    """Build one reading from a stored row, refusing every other shape."""
    if len(row) != _GENERATION_ROW_WIDTH:
        raise StoreError(
            f"the lease read returned {len(row)} column(s) where {_GENERATION_ROW_WIDTH} are read"
        )
    return CurrentGeneration(
        lease_id=_as_uuid(row[0]),
        owner=_as_str(row[1]),
        generation=_as_int(row[2]),
    )


def _active_run_of(row: Sequence[object]) -> ActiveRun:
    """Build one in-flight reading from a stored row, refusing every other shape."""
    if len(row) != _ACTIVE_RUN_ROW_WIDTH:
        raise StoreError(
            f"the run read returned {len(row)} column(s) where {_ACTIVE_RUN_ROW_WIDTH} are read"
        )
    return ActiveRun(
        run_id=_as_uuid(row[0]),
        phase=_as_str(row[1]),
        started_at=_as_moment(row[2]),
    )


def _as_moment(value: object) -> datetime:
    """Read an aware instant out of a column, refusing anything else."""
    if not isinstance(value, datetime):
        raise _unexpected(value, "an instant")
    return require_aware(value, "a selected instant")


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a value whose type is not the one the schema declares.

    The type is named and the value is not, for the reason every decoder in this
    package names one: a message belongs in a log record and stored content does
    not.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a whole number")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a whole number")
