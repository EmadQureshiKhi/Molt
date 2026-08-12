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

from typing import Final, cast

from molt.console.app import build_app
from molt.console.deps import COMPONENT, Console
from molt.console.lambda_adapter import AsgiApplication, LambdaEvent, LambdaResponse, invoke
from molt.errors import MoltError
from molt.telemetry import Severity, log

__all__ = [
    "CHECKPOINT_ENTRY_POINT",
    "ENTRY_POINT_KEY",
    "UnknownEntryPointError",
    "application",
    "checkpoint_signer",
    "handler",
    "reset",
]

# The key the scheduled rule carries, and the one entry-point name it may name.
ENTRY_POINT_KEY: Final[str] = "entry_point"
CHECKPOINT_ENTRY_POINT: Final[str] = "checkpoint_signer"

_application: AsgiApplication | None = None
_console: Console | None = None


class UnknownEntryPointError(MoltError):
    """The event named an entry point this function does not host."""


def application() -> AsgiApplication:
    """The one application object this container serves, built on first use."""
    global _application, _console
    if _application is None:
        _console = Console.from_configuration()
        _application = cast(AsgiApplication, build_app(_console))
    return _application


def reset() -> None:
    """Forget the container-scoped application, for a test that needs a clean one."""
    global _application, _console
    _application = None
    _console = None


def checkpoint_signer(event: LambdaEvent) -> LambdaResponse:  # noqa: ARG001
    """The scheduled entry point that signs a Ledger_Checkpoint.

    The signing work itself belongs to the checkpoint component, which is reached
    with a digest signer this skeleton does not construct: building one is the
    Certificate_Builder's own wiring. Until that wiring lands the entry point reports
    that it is unconfigured rather than reporting a checkpoint it did not take,
    because a scheduled invocation that silently signs nothing is worse than one that
    says so.
    """
    log(
        Severity.ERROR,
        COMPONENT,
        "the checkpoint entry point was invoked before its signer was wired",
        entry_point=CHECKPOINT_ENTRY_POINT,
    )
    return {"entry_point": CHECKPOINT_ENTRY_POINT, "status": "unconfigured", "signed": False}


def handler(event: LambdaEvent, context: object | None = None) -> LambdaResponse:  # noqa: ARG001
    """Serve one invocation: a console request, or the scheduled entry point."""
    named = event.get(ENTRY_POINT_KEY)
    if isinstance(named, str):
        if named != CHECKPOINT_ENTRY_POINT:
            raise UnknownEntryPointError(f"this function hosts no entry point named {named!r}")
        return checkpoint_signer(event)
    return invoke(application(), event)
