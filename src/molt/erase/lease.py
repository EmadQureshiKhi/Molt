"""The Lease_Manager: who owns an erasure for a Client, and under which generation.

Exactly one worker may own an erasure for a Client at a time, and a worker that has
lost ownership must not be able to record evidence. This module owns the first half
of that: granting ownership, refusing a contended acquisition, renewing a held
lease, transferring an expired one, and making a run's finalisation a thing that
happens once. The second half is the fence, which guards each evidence write with
the generation a grant recorded, and this module writes through it rather than
around it, so a lease's own mutations are refused for supersession by the same
predicate that refuses a disposition.

Seven decisions carry this module.

**A generation is the tenant's historical maximum plus one, read and written in one
serialisable transaction.** The maximum spans closed leases as well as the current
one, so a generation never repeats even after many takeovers, and the read joins
the granting transaction's read set. Two workers racing to acquire read the same
maximum; one commits, the other conflicts and retries, and the retry finds a
current lease and is refused rather than granted a duplicate generation. Where the
platform reports the collision as a uniqueness refusal instead of a conflict, the
outcome is the same refusal, arrived at by reading who won: either way exactly one
grant exists and the loser learns whose it is.

**A refusal names the winner.** `LeaseRefused` carries the current owner and the
current generation, and a note carries the lease and the instant its window closes,
so the loser of a contest learns who won and when it may ask again rather than only
that it lost.

**A repeat of one granting attempt is that attempt, not a second one.** The
idempotency key identifies the attempt and the schema holds it unique. An
acquisition presenting the key and the owner a current lease already records
returns that lease and writes nothing, so a worker whose grant committed and whose
acknowledgement was lost does not contend with itself.

**Takeover turns on the cluster's clock and on nothing else.** A lease is takeable
exactly when the cluster, reading the row inside the transaction that would
supersede it, reports its expiry as already past. A worker's own reading never
enters the decision, so neither a fast clock nor a slow one can move ownership. The
transfer is the ordered supersession the schema is shaped for: close the current
lease naming the successor's generated identifier, then insert the successor, both
in one transaction, and the successor's generation comes from the same historical
maximum rule as a first grant. So takeover generations strictly increase.

**A renewal by a superseded owner is refused, not silently ignored.** Renewal
extends the window of a lease the caller believes it holds, which is a claim about
ownership, so it goes through the fence: the generation is read in the renewal's
own transaction, and a caller whose generation is no longer current is told it was
superseded, by both generations, and extends nothing. A caller holding no lease at
all is told that instead. The refusal propagates rather than looping, because a
superseded renewal cannot become admissible by being run again.

**A clean release surrenders the window rather than closing the lease.** A closed
lease names the lease that replaced it, and a worker finishing cleanly has no
successor to name, so releasing means giving the remainder of the window back: the
row stays current and becomes takeable at once. An abort releases nothing at all
and lets the window run out. Both endings therefore reach the same state by the
same rule, and a crashed worker is not a case of its own — which is the point, since
the case a graceful path would hide is exactly the one the fence exists for.

**Finalisation is idempotent by a state transition, not by a caller's care.** The
run records the attempt's key when it begins, and the marking statement matches the
run only while its finalisation instant is absent. A second finalisation matches no
row, mutates nothing, and returns the recorded outcome. That is safe under the
retry wrapper for the same reason it is safe under a duplicate request: an attempt
that rolled back left the instant absent, so its retry writes it, and an attempt
whose competitor got there first matches no row and reports what the competitor
recorded. The outcome is attributable because the ownership record wrote the
generation when the run began, and finalisation never restates it.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from molt.config.resolve import Configuration, load_configuration
from molt.errors import LeaseRefused, LeaseRefusedError, StoreError
from molt.models.event import JsonObject
from molt.store import Cursor, MemoryStore
from molt.store.erasure_lease import (
    NO_GENERATION,
    FinalisationRecord,
    LeaseInsert,
    LeaseInterval,
    LeaseRecord,
    LeaseState,
    close_lease,
    extend_lease,
    insert_lease,
    is_unique_violation,
    mark_finalised,
    read_current_lease,
    read_finalisation,
    record_run_key,
    select_current_lease,
    select_finalisation,
    select_highest_generation,
    surrender_lease,
)
from molt.store.fencing import FIRST_GENERATION, fenced
from molt.telemetry import Severity, log, metric

__all__ = [
    "ACQUIRE_LABEL",
    "COMPONENT",
    "FINALISE_LABEL",
    "LEASE_OWNER_KEY",
    "LEASE_TAKEOVER_METRIC",
    "RELEASE_LABEL",
    "RENEW_LABEL",
    "RUN_OWNERSHIP_LABEL",
    "LeaseGrant",
    "acquire",
    "current",
    "finalisation_for",
    "finalise",
    "next_generation",
    "owner_identifier",
    "register_run",
    "release",
    "renew",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "erase"

# The configuration surface key the owner identity is read from. An empty value
# means derive one from the host and the process, which is what a deployment that
# names no owner gets rather than a shared default that two workers would collide
# under.
LEASE_OWNER_KEY: Final[str] = "MOLT_LEASE_OWNER"

# The measurement each transfer of ownership is counted by. Undimensioned: the
# tenant and the owner are both unbounded, so attaching either would turn one
# billable metric into as many as there are workers, and both belong in the log
# record instead, where naming them costs nothing.
LEASE_TAKEOVER_METRIC: Final[str] = "erasure.lease_takeovers"

# The labels this module's transactions appear under in a log record and in the
# note an exhausted retry attaches. Four labels rather than one, so an operator
# reading a log record learns which act of the lifecycle kept losing.
ACQUIRE_LABEL: Final[str] = "lease_acquire"
RENEW_LABEL: Final[str] = "lease_renew"
RELEASE_LABEL: Final[str] = "lease_release"
RUN_OWNERSHIP_LABEL: Final[str] = "lease_run_ownership"
FINALISE_LABEL: Final[str] = "lease_finalise"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """Ownership as it was granted, and the lease it superseded if it superseded one.

    The stored lease is carried whole rather than picked apart, because every field
    a holder needs afterwards is a fact the cluster recorded: the generation it
    presents on every guarded write, the window it must renew inside, and the key
    its finalisation is identified by.
    """

    lease: LeaseRecord
    superseded: UUID | None = None

    @property
    def lease_id(self) -> UUID:
        """The lease this grant records."""
        return self.lease.lease_id

    @property
    def client_id(self) -> UUID:
        """The tenant this grant holds erasure for."""
        return self.lease.client_id

    @property
    def owner(self) -> str:
        """The worker identity this grant belongs to."""
        return self.lease.owner

    @property
    def generation(self) -> int:
        """The fence this owner presents on every guarded write."""
        return self.lease.generation

    @property
    def idempotency_key(self) -> str:
        """The key identifying the attempt this grant belongs to."""
        return self.lease.idempotency_key

    @property
    def expires_at(self) -> datetime:
        """The instant this ownership stops holding unless it is renewed."""
        return self.lease.expires_at

    @property
    def took_over(self) -> bool:
        """Whether this grant transferred ownership from an expired lease."""
        return self.superseded is not None


# ---------------------------------------------------------------------------
# The generation, and the identity that holds one
# ---------------------------------------------------------------------------


def next_generation(highest: int) -> int:
    """The generation a grant takes, given the highest ever recorded for the tenant.

    One rule for a first grant and for every takeover, which is what makes the
    sequence strictly increasing over a tenant's whole history rather than over its
    current leases: a takeover reads a maximum that includes the lease it is about
    to close, so the successor cannot repeat it.

    Args:
        highest: The historical maximum, or the no-generation floor where the
            tenant has never held a lease.

    Returns:
        The generation to record, never below the first generation the schema
        admits.

    Raises:
        ValueError: The maximum presented is below the floor, so it came from
            neither a stored lease nor an empty history.
    """
    if highest < NO_GENERATION:
        raise ValueError(
            f"a recorded fencing generation is at least {FIRST_GENERATION} and an empty "
            f"history reports {NO_GENERATION}, so nothing below that was ever recorded"
        )
    return highest + 1


def owner_identifier(configuration: Configuration | None = None) -> str:
    """The identity this process holds leases under.

    The configured value is used as it stands when there is one, so an operator
    running workers under names of their own choosing gets those names in every
    refusal. Where none is configured, one is derived from the host and the
    process, because two workers on one host must not share an identity: a refusal
    naming a shared identity would tell an operator nothing about which process to
    look at, and an acquisition by the same identity is treated as the same
    attempt.
    """
    resolved = load_configuration() if configuration is None else configuration
    configured = resolved.text(LEASE_OWNER_KEY).strip()
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}"


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


def acquire(
    store: MemoryStore,
    client_id: UUID,
    owner: str,
    idempotency_key: str,
    *,
    interval: LeaseInterval | None = None,
    now: datetime | None = None,
    lease_id: UUID | None = None,
) -> LeaseGrant:
    """Take ownership of erasure for one Client, or be refused by whoever holds it.

    The whole decision is one serialisable transaction: read the current lease and
    the cluster's verdict on its window, read the tenant's historical generation
    maximum, and then either refuse, return the attempt already recorded, insert a
    first lease, or supersede an expired one and insert its successor.

    Args:
        store: The connection surface the transaction is framed by.
        client_id: The tenant to take erasure ownership of.
        owner: The identity to record ownership under.
        idempotency_key: The key identifying this attempt, unique across attempts.
        interval: How long the window runs for, read from the configuration
            surface when absent.
        now: The anchor the written window is measured from, or None to leave it to
            the cluster's reading. Takeover admission never reads this.
        lease_id: The identifier to record the grant under, generated when absent.
            It is settled before the transaction opens either way, because the
            closing half of a supersession names the successor before the successor
            exists, and a retry of the transaction must name the same successor it
            named the first time.

    Returns:
        The ownership granted, naming the lease it superseded where it took one
        over, or the ownership this same attempt already held.

    Raises:
        LeaseRefusedError: A lease for this Client is current and belongs to
            another attempt. The refusal names the current owner and generation.
        SerializationExhaustedError: The grant conflicted on every attempt the
            policy permitted. Nothing it wrote is committed.
    """
    if not owner:
        raise ValueError("an acquisition names the owner it is requested for")
    if not idempotency_key:
        raise ValueError("an acquisition names the key identifying its attempt")
    chosen = LeaseInterval.from_configuration() if interval is None else interval
    successor_id = uuid4() if lease_id is None else lease_id

    def body(cursor: Cursor) -> LeaseGrant:
        held = select_current_lease(cursor, client_id)
        if held is not None:
            recorded = _same_attempt(held, owner, idempotency_key)
            if recorded is not None:
                return recorded
            if not held.takeable:
                raise _refusal(client_id, held)
        highest = select_highest_generation(cursor, client_id)
        superseded = None if held is None else _close(cursor, held, successor_id, now=now)
        granted = insert_lease(
            cursor,
            LeaseInsert(
                lease_id=successor_id,
                client_id=client_id,
                owner=owner,
                generation=next_generation(highest),
                idempotency_key=idempotency_key,
            ),
            interval=chosen,
            now=now,
        )
        return LeaseGrant(lease=granted, superseded=superseded)

    grant = _granted(store, client_id, body)
    if grant.took_over:
        _record_takeover(grant)
    return grant


def _same_attempt(held: LeaseState, owner: str, idempotency_key: str) -> LeaseGrant | None:
    """The grant to return where a current lease is this same attempt's, else None.

    A granting attempt is identified once, and the schema holds that identity
    unique outright, so a repeat of one attempt cannot produce a second lease. It
    is answered with the lease the attempt already holds, whether or not that
    lease's window has run out, because the alternative is a worker contending with
    itself over a grant that already committed.
    """
    if held.owner == owner and held.lease.idempotency_key == idempotency_key:
        return LeaseGrant(lease=held.lease)
    return None


def _close(
    cursor: Cursor,
    held: LeaseState,
    successor_id: UUID,
    *,
    now: datetime | None,
) -> UUID:
    """Close an expired lease ahead of its successor's insert, or refuse the transfer."""
    closed = close_lease(cursor, held.lease_id, successor_id, now=now)
    if closed is None:
        raise StoreError(
            "the expired lease this takeover would supersede is no longer current, "
            "so no successor was inserted and ownership did not move"
        )
    return closed.lease_id


def _granted(
    store: MemoryStore,
    client_id: UUID,
    body: Callable[[Cursor], LeaseGrant],
) -> LeaseGrant:
    """Run one granting transaction, reporting a uniqueness collision as a refusal.

    The platform may report two workers racing for one tenant either as a
    serialization conflict, which the wrapper retries into a refusal, or as a
    refusal of the uniqueness that admits one current lease per tenant. Both mean
    the same thing and both are reported the same way: read who holds erasure now,
    and refuse naming them. The losing attempt commits nothing in either case.
    """
    try:
        return store.in_serializable(body, label=ACQUIRE_LABEL)
    except StoreError:
        raise
    except Exception as error:
        if not is_unique_violation(error):
            raise
        held = read_current_lease(store, client_id)
        if held is None:
            raise
        raise _refusal(client_id, held) from error


def _refusal(client_id: UUID, held: LeaseState) -> LeaseRefusedError:
    """The contention failure to raise, with the refusal recorded as it is built."""
    log(
        Severity.WARNING,
        COMPONENT,
        "an erasure lease acquisition was refused because a lease is current",
        client_id=str(client_id),
        current_owner=held.owner,
        current_generation=held.generation,
    )
    refused = LeaseRefused(held.owner, held.generation)
    refused.add_note(
        f"erasure for this Client is held under lease {held.lease_id} until "
        f"{held.lease.expires_at.isoformat()}, after which it may be taken over"
    )
    return refused


def _record_takeover(grant: LeaseGrant) -> None:
    """Count and record one transfer of ownership, once its transaction committed.

    Emitted here rather than inside the transaction, because a conflict runs the
    body again and a measurement taken inside it would count a transfer that was
    rolled back.
    """
    metric(LEASE_TAKEOVER_METRIC)
    log(
        Severity.INFO,
        COMPONENT,
        "erasure ownership was taken over from a lease whose window had run out",
        client_id=str(grant.client_id),
        owner=grant.owner,
        generation=grant.generation,
        superseded_lease=str(grant.superseded),
    )


# ---------------------------------------------------------------------------
# Renewal, release, and reading
# ---------------------------------------------------------------------------


def renew(
    store: MemoryStore,
    grant: LeaseGrant,
    *,
    interval: LeaseInterval | None = None,
    now: datetime | None = None,
) -> LeaseGrant:
    """Extend a held lease's window, or learn that the ownership has moved on.

    The extension runs behind the fence, so the generation is read in the renewal's
    own transaction and a caller whose generation is no longer current extends
    nothing.

    Returns:
        The same ownership with its window extended.

    Raises:
        LeaseNotHeldError: The Client holds no current lease, so there is nothing
            to renew.
        StaleFencingGenerationError: This owner has been superseded. The failure
            carries both generations and the window was not extended.
        StoreError: The lease named is no longer current, so it has no window to
            extend.
    """
    chosen = LeaseInterval.from_configuration() if interval is None else interval

    def body(cursor: Cursor) -> LeaseRecord:
        extended = extend_lease(cursor, grant.lease_id, interval=chosen, now=now)
        if extended is None:
            raise StoreError(
                "the lease this renewal names is no longer current, so its window was "
                "not extended and ownership no longer stands"
            )
        return extended

    renewed = fenced(store, grant.client_id, grant.generation, body, label=RENEW_LABEL)
    log(
        Severity.DEBUG,
        COMPONENT,
        "an erasure lease window was extended by its holder",
        client_id=str(renewed.client_id),
        owner=renewed.owner,
        generation=renewed.generation,
    )
    return LeaseGrant(lease=renewed, superseded=grant.superseded)


def release(
    store: MemoryStore,
    grant: LeaseGrant,
    *,
    now: datetime | None = None,
) -> None:
    """Give back the remainder of a held lease's window, leaving it takeable at once.

    This is the clean ending, and it is an acceleration of the ordinary one rather
    than a second mechanism: the lease stays current and the next acquirer
    supersedes it through the ordered transfer, exactly as it would have once the
    window ran out on its own. An aborting run calls nothing at all and lets the
    window run out, so a crashed worker and a cleanly aborted worker release
    ownership by the same rule, on the cluster's clock.

    Raises:
        LeaseNotHeldError: The Client holds no current lease, so there is nothing
            to give back.
        StaleFencingGenerationError: This owner has been superseded, so the window
            it is giving back is not its own to shorten. Nothing was written.
        StoreError: The lease named is no longer current.
    """

    def body(cursor: Cursor) -> LeaseRecord:
        surrendered = surrender_lease(cursor, grant.lease_id, now=now)
        if surrendered is None:
            raise StoreError(
                "the lease this release names is no longer current, so it has no "
                "window left to give back"
            )
        return surrendered

    released = fenced(store, grant.client_id, grant.generation, body, label=RELEASE_LABEL)
    log(
        Severity.INFO,
        COMPONENT,
        "erasure ownership was released, so the lease is takeable at once",
        client_id=str(released.client_id),
        owner=released.owner,
        generation=released.generation,
    )


def current(store: MemoryStore, client_id: UUID) -> LeaseState | None:
    """The lease currently held for one Client, or None where none is held.

    The reading carries the cluster's own verdict on whether the window has run
    out, so a caller reporting who owns an erasure and whether it may be taken over
    reports what the cluster said rather than what its own clock thinks.
    """
    return read_current_lease(store, client_id)


# ---------------------------------------------------------------------------
# Idempotent finalisation
# ---------------------------------------------------------------------------


def register_run(store: MemoryStore, grant: LeaseGrant, run_id: UUID) -> str:
    """Record one run's idempotency key and the ownership it is performed under.

    This is what makes a later finalisation identifiable and attributable: the key
    says which attempt the run belongs to, and the generation says which owner's
    fence its evidence was written under. Recording the same key again is the same
    recording, so a restarted attempt reports what already stands.

    Returns:
        The key the run now records.

    Raises:
        LeaseNotHeldError: The Client holds no current lease, so this run belongs
            to no owner.
        StaleFencingGenerationError: This owner has been superseded, so the run is
            not its to claim. Nothing was written.
        StoreError: The run named belongs to another Client or to another attempt.
    """

    def body(cursor: Cursor) -> str:
        recorded = record_run_key(
            cursor,
            run_id,
            grant.client_id,
            idempotency_key=grant.idempotency_key,
            lease_id=grant.lease_id,
            generation=grant.generation,
        )
        if recorded is None:
            raise StoreError(
                "the run this ownership record names belongs to another Client or "
                "already carries another attempt's idempotency key, so nothing was written"
            )
        return recorded.idempotency_key

    return fenced(store, grant.client_id, grant.generation, body, label=RUN_OWNERSHIP_LABEL)


def finalise(
    store: MemoryStore,
    grant: LeaseGrant,
    run_id: UUID,
    result: JsonObject,
    *,
    now: datetime | None = None,
) -> FinalisationRecord:
    """Finalise one run once, and report the recorded outcome however often it is asked.

    The marking and the read of an already-recorded outcome are one transaction
    behind the fence, so a run cannot be finalised by an owner that lost the lease,
    and a repeat cannot observe a half-finalised state. A repeat mutates nothing:
    the marking statement matches no row once the finalisation instant is present,
    and what comes back is the outcome that was recorded then.

    Returns:
        The recorded finalisation, whether this call wrote it or an earlier one did.

    Raises:
        LeaseNotHeldError: The Client holds no current lease, so this run has no
            owner to finalise it.
        StaleFencingGenerationError: This owner has been superseded, so it may not
            declare the run finished. Nothing was written.
        StoreError: The run named carries no recorded attempt matching this grant,
            so there is nothing to finalise and nothing recorded to report.
    """

    def body(cursor: Cursor) -> tuple[FinalisationRecord, bool]:
        written = mark_finalised(
            cursor,
            run_id,
            grant.client_id,
            idempotency_key=grant.idempotency_key,
            result=result,
            now=now,
        )
        if written is not None:
            return written, True
        recorded = select_finalisation(cursor, grant.idempotency_key)
        if recorded is None:
            raise StoreError(
                "the run this finalisation names carries no recorded attempt matching "
                "this lease, so nothing was finalised and nothing recorded can be reported"
            )
        return recorded, False

    record, written = fenced(store, grant.client_id, grant.generation, body, label=FINALISE_LABEL)
    log(
        Severity.INFO if written else Severity.WARNING,
        COMPONENT,
        "a run was finalised"
        if written
        else "a repeated finalisation returned the recorded outcome and mutated nothing",
        client_id=str(grant.client_id),
        run_id=str(record.run_id),
        generation=record.generation,
    )
    return record


def finalisation_for(store: MemoryStore, idempotency_key: str) -> FinalisationRecord | None:
    """The recorded finalisation of one attempt, or None where none is recorded.

    A caller resuming an attempt asks this before doing any work, so a run that was
    finalised by an earlier attempt of the same key is reported rather than
    performed again.
    """
    return read_finalisation(store, idempotency_key)
