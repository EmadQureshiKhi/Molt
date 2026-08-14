"""The contend verb: the lease contention and fencing demonstration.

Three claims are demonstrated rather than asserted in prose, and each has its own
non-zero exit: exactly one worker wins the race, a takeover before expiry is
refused, and the superseded worker's disposition write is refused by the fence
rather than by the application remembering that it lost.

The stale write is attempted through the fenced disposition wrapper with a body
that would record the attempt. The fence reads the current generation before the
body runs, so a refusal means the body never ran, which is exactly the guarantee
being shown.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Final
from uuid import UUID

from molt.cli.context import VerbContext, client_id_for
from molt.cli.exits import ExitCode
from molt.erase.lease import LeaseGrant, acquire
from molt.errors import LeaseRefusedError, StaleFencingGenerationError
from molt.models.event import JsonObject
from molt.store import Cursor, MemoryStore
from molt.store.erasure_lease import LeaseInterval
from molt.store.fencing import fenced_disposition

__all__ = ["run"]

# The interval the demonstration takes a lease for when the operator names none.
# Short enough that waiting for expiry costs seconds rather than minutes.
_DEFAULT_INTERVAL_SECONDS: Final[int] = 5


def run(context: VerbContext) -> ExitCode:
    """Race the workers, take over after expiry, and refuse the revived worker."""
    emitter = context.emitter
    workers = max(context.integer("workers", 10), 2)
    interval_seconds = context.integer("lease_interval", _DEFAULT_INTERVAL_SECONDS)
    interval = LeaseInterval(seconds=interval_seconds)

    with MemoryStore.from_configuration(context.configuration) as store:
        client_id = client_id_for(store, context.required_text("client"))
        grants, refusals = _race(store, client_id, workers=workers, interval=interval)
        if len(grants) != 1:
            return emitter.fail(
                context.name,
                f"{len(grants)} workers won the race, and exactly one may",
                ExitCode.OPERATIONAL,
            )
        winner = grants[0]
        emitter.narrate(f"winning owner: {winner.owner}")
        emitter.narrate(f"refused workers: {refusals}")

        early = _takeover(store, client_id, interval=interval, owner="early-taker")
        if early is not None:
            return emitter.fail(
                context.name,
                "a takeover succeeded before the lease expired",
                ExitCode.OPERATIONAL,
            )
        emitter.narrate("a takeover before expiry was refused")

        time.sleep(interval_seconds + 1)
        successor = _takeover(store, client_id, interval=interval, owner="successor")
        if successor is None:
            return emitter.fail(
                context.name,
                "no takeover succeeded after the lease expired",
                ExitCode.OPERATIONAL,
            )
        emitter.narrate(f"generation at takeover: {successor.generation}")

        refused = _stale_write_refused(store, client_id, generation=winner.generation)
        if not refused:
            return emitter.fail(
                context.name,
                "the revived worker's disposition write was admitted, and it must be refused",
                ExitCode.OPERATIONAL,
            )
        emitter.narrate("the revived worker's disposition write was refused")

    document: JsonObject = {
        "workers": workers,
        "winning_owner": winner.owner,
        "refused_workers": refusals,
        "winning_generation": winner.generation,
        "takeover_before_expiry_refused": True,
        "takeover_generation": successor.generation,
        "stale_disposition_refused": True,
    }
    return emitter.succeed(context.name, document)


def _race(
    store: MemoryStore,
    client_id: UUID,
    *,
    workers: int,
    interval: LeaseInterval,
) -> tuple[tuple[LeaseGrant, ...], int]:
    """Every worker asks at once; the grants and the refusal count come back."""

    def attempt(index: int) -> LeaseGrant | None:
        try:
            return acquire(
                store,
                client_id,
                owner=f"contender-{index}",
                interval=interval,
                idempotency_key=f"contend-{index}",
            )
        except LeaseRefusedError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(attempt, range(workers)))
    grants = tuple(result for result in results if result is not None)
    return grants, len(results) - len(grants)


def _takeover(
    store: MemoryStore,
    client_id: UUID,
    *,
    interval: LeaseInterval,
    owner: str,
) -> LeaseGrant | None:
    """One takeover attempt, or None when the held lease refused it."""
    try:
        return acquire(
            store,
            client_id,
            owner=owner,
            interval=interval,
            idempotency_key=f"contend-{owner}",
        )
    except LeaseRefusedError:
        return None


def _stale_write_refused(store: MemoryStore, client_id: UUID, *, generation: int) -> bool:
    """Whether the fence refused a write presenting the superseded generation."""

    def body(cursor: Cursor) -> bool:
        # Reached only if the fence admitted the write, which is the failure this
        # demonstration exists to detect. The cursor is deliberately untouched.
        del cursor
        return True

    try:
        fenced_disposition(store, client_id, generation, body)
    except StaleFencingGenerationError:
        return True
    return False
