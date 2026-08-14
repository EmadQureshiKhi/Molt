"""One module per verb, and the one table the argument tree dispatches through.

The table is keyed by the verb as it is written, which is why one key holds a
space: `attest verify` is two words at the command line and one entry here, so a
reader of this table sees the surface the design table states.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from molt.cli.context import VerbContext
from molt.cli.exits import ExitCode
from molt.cli.verbs import (
    attest,
    contend,
    erase,
    mcp,
    migrate,
    recall,
    residue,
    retention,
    seed,
    sensitivity,
    serve,
    verify_chain,
    watch,
)

__all__ = ["HANDLERS", "Handler"]

Handler = Callable[[VerbContext], ExitCode]

HANDLERS: Final[Mapping[str, Handler]] = MappingProxyType(
    {
        "erase": erase.run,
        "residue": residue.run,
        "sensitivity": sensitivity.run,
        "contend": contend.run,
        "attest verify": attest.verify,
        "recall": recall.run,
        "watch": watch.run,
        "serve": serve.run,
        "mcp": mcp.run,
        "seed": seed.run,
        "migrate": migrate.run,
        "verify-chain": verify_chain.run,
        "retention": retention.run,
    }
)
