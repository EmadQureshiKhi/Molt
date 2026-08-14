"""The command-line interface: one module per verb behind a single argument tree.

The entry point the console script resolves to is re-exported here rather than
living here, so `molt.cli:main` names one function and the argument tree stays in
a module of its own.
"""

from __future__ import annotations

from molt.cli.exits import ComponentUnavailableError, ExitCode, UsageError
from molt.cli.main import main, run

__all__ = [
    "ComponentUnavailableError",
    "ExitCode",
    "UsageError",
    "main",
    "run",
]
