"""What every verb is handed, so no verb reads the environment for itself.

One object carries the parsed arguments, the resolved configuration, the
formatter, and the two sources configuration was resolved from. The last pair
matters: a verb that has to run under a different database role re-resolves the
same surface with one override rather than reaching for the process environment,
so a test that supplied an environment mapping keeps supplying it.

A verb reads an argument through the accessors here rather than off the namespace
directly, because a global flag is declared on the top-level parser and on every
subparser and is therefore absent from the namespace when it was not given. The
accessors treat absence and a default as the same thing, which is what lets
`molt --json erase` and `molt erase --json` mean one thing.
"""

from __future__ import annotations

import argparse
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from molt.cli.exits import UsageError
from molt.cli.output import Emitter
from molt.config.resolve import Configuration, load_configuration

if TYPE_CHECKING:  # pragma: no cover - imported for typing alone
    from molt.store import MemoryStore

__all__ = [
    "AGENT_CLI",
    "MACHINE_ID_KEY",
    "READER_ROLE",
    "ROLE_KEY",
    "VerbContext",
    "client_id_for",
    "machine_identifier",
]

# What the CLI calls itself wherever a component records which surface acted.
AGENT_CLI = "molt"

# The role a read-only verb connects as, and the key it is overridden through.
READER_ROLE = "reader"
ROLE_KEY = "MOLT_DB_ROLE"
MACHINE_ID_KEY = "MOLT_MACHINE_ID"


@dataclass(frozen=True, slots=True)
class VerbContext:
    """The one bundle a verb runs from.

    Attributes:
        name: The verb as it is reported, which is two words for `attest verify`.
        args: The parsed argument namespace.
        configuration: The resolved surface, with the global flags already
            applied as environment overrides.
        emitter: Where narration and the one machine-readable object go.
        environ: The environment the surface was resolved over.
        config_path: The configuration file the surface was read from, if any.
    """

    name: str
    args: argparse.Namespace
    configuration: Configuration
    emitter: Emitter
    environ: Mapping[str, str]
    config_path: Path | None

    def present(self, option: str) -> bool:
        """Whether an option was given at all."""
        return hasattr(self.args, option)

    def text(self, option: str, default: str | None = None) -> str | None:
        """One text option, or the default when it was not given."""
        value = getattr(self.args, option, None)
        if value is None:
            return default
        if not isinstance(value, str):
            raise UsageError(f"the value of --{option.replace('_', '-')} is not text")
        return value or default

    def required_text(self, option: str) -> str:
        """One text option a verb cannot run without, naming the flag when absent."""
        value = self.text(option)
        if value is None:
            raise UsageError(f"--{option.replace('_', '-')} is required")
        return value

    def integer(self, option: str, default: int) -> int:
        """One whole-number option, or the default when it was not given."""
        value = getattr(self.args, option, None)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise UsageError(f"the value of --{option.replace('_', '-')} is not a whole number")
        return value

    def number(self, option: str, default: float) -> float:
        """One numeric option, or the default when it was not given."""
        value = getattr(self.args, option, None)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UsageError(f"the value of --{option.replace('_', '-')} is not a number")
        return float(value)

    def flag(self, option: str) -> bool:
        """One switch, false when it was not given."""
        return bool(getattr(self.args, option, False))

    def repeated(self, option: str) -> tuple[str, ...]:
        """One repeatable text option as a tuple, empty when it was not given."""
        value = getattr(self.args, option, None)
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, Sequence):
            return tuple(str(item) for item in value)
        raise UsageError(f"the value of --{option.replace('_', '-')} is not a list")

    def numbers(self, option: str, default: Sequence[float] = ()) -> tuple[float, ...]:
        """One repeatable or comma-separated numeric list option."""
        raw = getattr(self.args, option, None)
        if raw is None:
            return tuple(default)
        entries: list[str] = []
        if isinstance(raw, str):
            entries = [item.strip() for item in raw.split(",") if item.strip()]
        elif isinstance(raw, Sequence):
            for item in raw:
                entries.extend(part.strip() for part in str(item).split(",") if part.strip())
        else:
            raise UsageError(f"the value of --{option.replace('_', '-')} is not a list of numbers")
        parsed: list[float] = []
        for entry in entries:
            try:
                parsed.append(float(entry))
            except ValueError as exc:
                raise UsageError(
                    f"an entry of --{option.replace('_', '-')} is not a number"
                ) from exc
        return tuple(parsed)

    def path(self, option: str) -> Path | None:
        """One filesystem-path option, or None when it was not given."""
        value = self.text(option)
        return None if value is None else Path(value).expanduser()

    def configuration_for(self, overrides: Mapping[str, str]) -> Configuration:
        """The same surface resolved again with a few environment values replaced."""
        if not overrides:
            return self.configuration
        return load_configuration(
            config_path=self.config_path,
            environ={**self.environ, **overrides},
        )

    def store(self, *, role: str | None = None) -> MemoryStore:
        """Open the cluster, optionally forcing the role the verb must connect as."""
        # Imported here rather than at module scope so the argument tree parses
        # without the database driver present.
        from molt.store import MemoryStore

        configuration = (
            self.configuration if role is None else self.configuration_for({ROLE_KEY: role})
        )
        return MemoryStore.from_configuration(configuration)


def machine_identifier(configuration: Configuration) -> str:
    """The machine an Event records, from the surface or derived from the host."""
    configured = configuration.optional_text(MACHINE_ID_KEY)
    return configured if configured else platform.node() or "unknown-host"


def client_id_for(store: MemoryStore, slug: str) -> UUID:
    """The Client identifier one slug names, refusing a slug naming no Client."""
    # The slug resolution lives with the tool registry's reads, so the CLI holds
    # no statement of its own for it.
    from molt.mcpserver.tools import permitted_client_ids

    resolved = permitted_client_ids(store, (slug,))
    if not resolved:
        raise UsageError("--client names no stored Client")
    return resolved[0]
