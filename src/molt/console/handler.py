"""The function entry point the console template declares, and its second caller.

Two callers reach this module and they arrive with different events. The function
endpoint delivers a request payload, which is translated by the adapter and served
by the application object. The scheduled rule delivers `{"entry_point": ...}`,
because the checkpoint signer is hosted in this function rather than in a task of its
own: the signing permission is granted to this one role, and a second signing
principal would end that exclusivity.

Dispatch is on the presence of the entry-point key rather than on the absence of a
request shape, so a request payload can never be mistaken for a scheduled
invocation, and an entry-point name this module does not know is refused by name.

The application object and the console are built once per container and reused, so
the parameter reads and the connection setup are paid at cold start rather than per
request.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, cast

from molt.attest.checkpoint import CheckpointPolicy, DigestSigner, take_checkpoint
from molt.attest.keys import signer_from_configuration
from molt.config.resolve import Configuration, load_configuration
from molt.console.app import build_app
from molt.console.deps import COMPONENT, Console
from molt.console.lambda_adapter import AsgiApplication, LambdaEvent, LambdaResponse, invoke
from molt.console.routes.erasure import (
    ERASURE_ENTRY_POINT,
    PLAN_FIELD,
    perform_dispatched,
)
from molt.errors import MoltError, SigningUnavailableError
from molt.store import MemoryStore
from molt.telemetry import Severity, log

__all__ = [
    "CHECKPOINT_ENTRY_POINT",
    "ENTRY_POINT_KEY",
    "ERASURE_ENTRY_POINT",
    "NO_SIGNING_KEY_REASON",
    "SCHEDULED_TIME_KEY",
    "SIGNED_STATUS",
    "UNCONFIGURED_STATUS",
    "UnknownEntryPointError",
    "application",
    "checkpoint_signer",
    "console",
    "erasure_worker",
    "handler",
    "reset",
]

# The key the scheduled rule carries, and the one entry-point name it may name.
ENTRY_POINT_KEY: Final[str] = "entry_point"
CHECKPOINT_ENTRY_POINT: Final[str] = "checkpoint_signer"

# The field a scheduled event carries the instant the rule fired in. The window a
# checkpoint covers closes at that instant rather than at the instant this code
# reached the clock, so consecutive scheduled windows meet exactly at the schedule
# boundary instead of at boundaries jittered by invocation latency.
SCHEDULED_TIME_KEY: Final[str] = "time"

# The two outcomes a scheduled invocation reports, and why the second one happened.
# The reason is a field of its own rather than prose inside the status, because the
# answer an operator gives for an unprovisioned key is *provision one* and the answer
# for anything else is *look at the failure*, and a status that carried both would
# make the two indistinguishable in a log query.
SIGNED_STATUS: Final[str] = "signed"
UNCONFIGURED_STATUS: Final[str] = "unconfigured"
NO_SIGNING_KEY_REASON: Final[str] = "no_signing_key_configured"

_application: AsgiApplication | None = None
_console: Console | None = None


class UnknownEntryPointError(MoltError):
    """The event named an entry point this function does not host."""


def console() -> Console:
    """The one console this container holds, resolved on first use.

    Both callers reach the cluster through this. The scheduled entry point does not
    build a second store of its own: a second construction would resolve the
    connection parameter twice, open a second connection, and could authenticate as
    a role the served requests are not using, so the checkpoint would be taken
    against a handle nothing else in the container shares.
    """
    global _console
    if _console is None:
        _console = Console.from_configuration()
    return _console


def application() -> AsgiApplication:
    """The one application object this container serves, built on first use."""
    global _application
    if _application is None:
        _application = cast(AsgiApplication, build_app(console()))
    return _application


def reset() -> None:
    """Forget the container-scoped application, for a test that needs a clean one."""
    global _application, _console
    _application = None
    _console = None


def _closing_instant(event: LambdaEvent) -> datetime:
    """The instant the window a scheduled invocation covers closes at.

    The rule's own firing instant where the event carries one, so a consecutive
    series of invocations partitions the Ledger's history with no gap and no overlap.
    An event carrying no readable instant falls back to the clock, because a window
    an invocation latency wide is a far smaller inaccuracy than no checkpoint at all.
    """
    carried = event.get(SCHEDULED_TIME_KEY)
    if not isinstance(carried, str):
        return datetime.now(UTC)
    try:
        fired = datetime.fromisoformat(carried)
    except ValueError:
        return datetime.now(UTC)
    return fired if fired.tzinfo is not None else fired.replace(tzinfo=UTC)


def _signing(
    signer: DigestSigner | None,
    policy: CheckpointPolicy | None,
    configuration: Configuration | None,
) -> tuple[DigestSigner, CheckpointPolicy]:
    """The signer and the policy, read from the configuration surface where absent.

    The signer is resolved before the policy on purpose. Both reads want the key
    identifier, and the signer's read is the one that reports an unprovisioned key as
    the condition it is — no signing key — rather than as a bare missing-value
    refusal, so resolving it first is what lets the caller answer the unconfigured
    case by name.
    """
    if signer is not None and policy is not None:
        return signer, policy
    resolved = load_configuration() if configuration is None else configuration
    return (
        signer_from_configuration(resolved) if signer is None else signer,
        CheckpointPolicy.from_configuration(resolved) if policy is None else policy,
    )


def checkpoint_signer(
    event: LambdaEvent,
    *,
    signer: DigestSigner | None = None,
    policy: CheckpointPolicy | None = None,
    store: MemoryStore | None = None,
    configuration: Configuration | None = None,
) -> LambdaResponse:
    """The scheduled entry point that computes and signs a Ledger_Checkpoint.

    The checkpoint is hosted here rather than in a task of its own because the
    signing permission is granted to this one execution role, so this function is the
    only principal that can produce the signature at all. The work itself belongs to
    the checkpoint component: the policy and the signer are resolved from the
    configuration surface, the store is the container's own, and the response names
    the checkpoint identifier and the covered Session count so that a scheduled
    invocation's record is evidence a checkpoint was taken rather than a claim.

    Two outcomes are distinguished, and the distinction is the point.

    A deployment that provisioned no signing key cannot sign, and that is a real
    state rather than a fault: a local run reaches no key service. So the absent key
    is reported as `unconfigured`, carrying the reason, and nothing is raised into the
    scheduler over a condition retrying cannot change. It is reported *because the
    resolution refused*, so the word carries the meaning that the key is absent: a
    deployment holding a key never reaches that branch, and one that does not is told
    which value to provision.

    Anything else raises, and the raise is deliberate. A signing call that failed, a
    cluster that refused the transaction, or a serialization conflict that exhausted
    its retries are all conditions where a checkpoint was owed and not taken, and
    swallowing them is exactly how a scheduled rule comes to produce nothing while
    every document says checkpoints are being taken. Raising puts the invocation in
    the scheduler's own failure count and lets its retry take the next attempt, and
    the window is derived from the firing instant rather than accumulated, so a retry
    covers the interval it was owed rather than a shifted one.

    The `checkpoint.computed` measurement is emitted once per stored checkpoint by
    the checkpoint component's own signed-storage path, which is on the path this
    entry point takes; it is not incremented a second time here, because two
    increments for one checkpoint would misreport the rate an operator alerts on.

    The keyword parameters are injection seams, not configuration: a test drives both
    outcomes with a stand-in store and a stand-in signer, reaching no cluster and no
    key service. The event parameter and this function's name are fixed by the
    template's scheduled rule.
    """
    try:
        resolved_signer, resolved_policy = _signing(signer, policy, configuration)
    except SigningUnavailableError as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "no signing key is provisioned, so the scheduled checkpoint signed nothing",
            entry_point=CHECKPOINT_ENTRY_POINT,
            reason=NO_SIGNING_KEY_REASON,
            detail=str(error),
        )
        return {
            "entry_point": CHECKPOINT_ENTRY_POINT,
            "status": UNCONFIGURED_STATUS,
            "signed": False,
            "reason": NO_SIGNING_KEY_REASON,
        }

    closing = _closing_instant(event)
    try:
        stored = take_checkpoint(
            console().store if store is None else store,
            signer=resolved_signer,
            policy=resolved_policy,
            now=closing,
        )
    except MoltError as error:
        log(
            Severity.ERROR,
            COMPONENT,
            "the scheduled checkpoint failed, so the invocation is left to fail with it",
            entry_point=CHECKPOINT_ENTRY_POINT,
            error_type=type(error).__name__,
            window_end=closing.isoformat(),
        )
        raise

    log(
        Severity.INFO,
        COMPONENT,
        "the scheduled invocation took a signed ledger checkpoint",
        entry_point=CHECKPOINT_ENTRY_POINT,
        checkpoint_id=str(stored.checkpoint_id),
        covered_session_count=stored.covered_session_count,
    )
    return {
        "entry_point": CHECKPOINT_ENTRY_POINT,
        "status": SIGNED_STATUS,
        "signed": True,
        "checkpoint_id": str(stored.checkpoint_id),
        "covered_session_count": stored.covered_session_count,
        "root_digest": stored.root_digest,
        "window_start": stored.window.start.isoformat(),
        "window_end": stored.window.end.isoformat(),
    }


def erasure_worker(event: LambdaEvent) -> LambdaResponse:
    """Perform one dispatched Erasure_Run. The far end of the console's own handoff.

    The console cannot run an erasure inside the invocation that starts it: the host
    suspends the environment once a response is written, so a thread started to outlive
    the request is frozen mid-run. The start route therefore invokes this function again,
    asynchronously, and that invocation gets an environment and a timeout of its own.

    It is this function rather than another because the console's execution role is the
    one principal permitted to sign a certificate and write to the evidence bucket, and
    duplicating that grant elsewhere would give this deployment two signing principals
    where it asserts one.

    The reply is for the platform's own record. Nothing waits for it: the operator was
    answered when the invocation was accepted, and the run's progress is durable in the
    run row the console's stream reads.
    """
    document = event.get(PLAN_FIELD)
    if not isinstance(document, Mapping):
        raise UnknownEntryPointError("a dispatched erasure carries no plan to perform")
    perform_dispatched(document, load_configuration())
    return {"entry_point": ERASURE_ENTRY_POINT, "performed": True}


def handler(event: LambdaEvent, context: object | None = None) -> LambdaResponse:  # noqa: ARG001
    """Serve one invocation: a console request, or one of the two named entry points."""
    named = event.get(ENTRY_POINT_KEY)
    if isinstance(named, str):
        if named == CHECKPOINT_ENTRY_POINT:
            return checkpoint_signer(event)
        if named == ERASURE_ENTRY_POINT:
            return erasure_worker(event)
        raise UnknownEntryPointError(f"this function hosts no entry point named {named!r}")
    return invoke(application(), event)
