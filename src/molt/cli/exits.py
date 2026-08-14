"""The exit statuses every verb ends with, and the two failures they stand for.

Four statuses and no more, because an operator scripting around this interface
has to be able to branch on them: success, an operation that was attempted and
failed, an invocation or a configuration that was wrong before anything was
attempted, and a verification whose answer was `failed`. The last is separate
from an operational failure on purpose: a verification that ran to completion and
concluded `failed` is a successful verification with a negative answer, and a
caller that could not tell it from a cluster it could not reach would have to
treat evidence as an outage.

A component whose module is not yet present is an operational failure rather
than a usage error: the invocation was well formed and the capability was simply
not there to run, which is the same class of outcome as a cluster that refused
the connection.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "ComponentUnavailableError",
    "ExitCode",
    "UsageError",
]


class ExitCode(IntEnum):
    """The status a verb hands back to the shell."""

    SUCCESS = 0
    OPERATIONAL = 1
    USAGE = 2
    VERIFICATION_FAILED = 3


class UsageError(Exception):
    """The invocation or the configuration was wrong before anything ran.

    A message names the argument or the configuration key at fault and never the
    value, because a value may be a credential and a message reaches a stream.
    """


class ComponentUnavailableError(Exception):
    """A verb's component is not present in this build, so the verb cannot run.

    The message names what is missing so an operator learns which part of the
    system is not yet there rather than seeing an import failure. Verbs raise
    this instead of letting an import error escape, so the argument tree stays
    usable while the components behind it are still arriving.
    """

    def __init__(self, verb: str, component: str) -> None:
        self.verb = verb
        self.component = component
        super().__init__(f"the {verb} verb needs {component}, which this build does not provide")
