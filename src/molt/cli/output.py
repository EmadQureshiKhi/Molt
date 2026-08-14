"""The one formatter every verb's output goes through.

Three rules hold here, and each is enforced by the shape rather than by asking a
verb to be careful.

**Nothing is printed by a verb.** A verb narrates and hands back a document; this
module decides which stream each of those goes to. Under the machine-readable
flag the narration is diverted to standard error and exactly one object is
written to standard output, so a caller parsing that stream never has to skip a
progress line.

**A value whose key matches the sensitive-name set is replaced before it is
written.** The set is the configured one, which extends the built-in set rather
than replacing it, so a deployment naming a further key does not lose the names
every deployment holds. Redaction runs on the document, on every nesting level,
which is what makes the no-secret guarantee a property of this module rather than
a review of every verb.

**A loaded credential renders as its placeholder wherever it appears.** The
secret accessors wrap a loaded value in a type whose every rendering path yields
one fixed token, so a credential reaching a document is already unprintable; the
formatter converts it through that rendering rather than reaching for its value.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, TextIO

from molt.cli.exits import ExitCode
from molt.config.resolve import BUILTIN_SENSITIVE_NAMES, Configuration
from molt.config.secrets import CREDENTIAL_PLACEHOLDER, Credential

__all__ = [
    "MAX_DOCUMENT_DEPTH",
    "REDACTED",
    "SENSITIVE_NAMES_KEY",
    "Emitter",
    "VerbOutcome",
    "redacted",
    "sensitive_names",
]

# What a value under a sensitive key is written as. One fixed token rather than a
# shortened value, because a prefix of a secret is still a disclosure.
REDACTED: Final[str] = "[REDACTED]"

# The recursion bound applied while redacting. A document deeper than this is
# truncated rather than walked, so a cyclic or adversarially nested structure
# cannot cost the process its stack.
MAX_DOCUMENT_DEPTH: Final[int] = 32

# The configuration key holding the sensitive-name set the operator extends.
SENSITIVE_NAMES_KEY: Final[str] = "MOLT_REDACTION_SENSITIVE_NAMES"


@dataclass(frozen=True, slots=True)
class VerbOutcome:
    """What one verb concluded: the status, and the document it reports.

    Attributes:
        code: The status the process ends with.
        document: The machine-readable object the invocation reports, already
            free of any value the verb read from a credential source.
    """

    code: ExitCode
    document: Mapping[str, object]


def sensitive_names(configuration: Configuration | None = None) -> tuple[str, ...]:
    """The names a value is redacted under: the built-in set, plus the configured one.

    The configured list extends rather than replaces, and an unreadable or absent
    configuration falls back to the built-in set, because a formatter that could
    not resolve its configuration must still redact.
    """
    names = {name.lower() for name in BUILTIN_SENSITIVE_NAMES}
    if configuration is not None:
        # A formatter redacts even when its configuration cannot be read, so an
        # unresolvable list narrows the set to the built-in names rather than
        # failing the verb that was about to write redacted output.
        with suppress(Exception):
            names.update(name.lower() for name in configuration.text_list(SENSITIVE_NAMES_KEY))
    return tuple(sorted(names))


def _is_sensitive(key: str, names: Sequence[str]) -> bool:
    """Whether a key names a value that must not be written."""
    lowered = key.lower()
    return any(name in lowered for name in names)


def redacted(value: object, names: Sequence[str], *, depth: int = 0) -> object:
    """One document with every sensitive value and every credential replaced.

    A mapping's key decides; a sequence inherits the decision its key already
    made, so a list under a sensitive key is replaced whole rather than element by
    element.
    """
    if isinstance(value, Credential):
        return CREDENTIAL_PLACEHOLDER
    if depth >= MAX_DOCUMENT_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        rendered: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _is_sensitive(key, names):
                rendered[key] = REDACTED
            else:
                rendered[key] = redacted(item, names, depth=depth + 1)
        return rendered
    if isinstance(value, (str, bytes)):
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, Iterable):
        return [redacted(item, names, depth=depth + 1) for item in value]
    return str(value)


def _serialisable(value: object) -> object:
    """A value the JSON writer accepts, with anything else rendered as text."""
    return str(value)


class Emitter:
    """Where a verb's narration and a verb's document are written.

    Held as an object rather than passed as two streams because the choice of
    stream per kind of output is the contract: a caller reading standard output
    under the machine-readable flag reads one object and nothing else.
    """

    __slots__ = ("_document_written", "_err", "_json", "_names", "_out")

    def __init__(
        self,
        *,
        out: TextIO,
        err: TextIO,
        json_output: bool = False,
        configuration: Configuration | None = None,
    ) -> None:
        self._out = out
        self._err = err
        self._json = json_output
        self._names = sensitive_names(configuration)
        self._document_written = False

    @property
    def json_output(self) -> bool:
        """Whether this invocation writes one machine-readable object."""
        return self._json

    @property
    def document_written(self) -> bool:
        """Whether the one object of this invocation has been written."""
        return self._document_written

    def narrate(self, message: str) -> None:
        """Write one human-readable line, diverted from standard output under the flag."""
        stream = self._err if self._json else self._out
        print(message, file=stream)

    def warn(self, message: str) -> None:
        """Write one human-readable line that always goes to standard error."""
        print(message, file=self._err)

    def emit(self, document: Mapping[str, object]) -> None:
        """Write the one machine-readable object, when the flag asked for one.

        Called at most once per invocation. Outside the machine-readable mode this
        writes nothing at all, because the narration already carried the answer.
        """
        if not self._json or self._document_written:
            return
        self._document_written = True
        body = redacted(dict(document), self._names)
        print(
            json.dumps(body, default=_serialisable, sort_keys=True, separators=(",", ":")),
            file=self._out,
        )

    def fail(self, verb: str, message: str, code: ExitCode) -> ExitCode:
        """Report a failure on standard error, and as the one object under the flag."""
        self.warn(f"molt {verb}: {message}")
        self.emit({"verb": verb, "ok": False, "exit_code": int(code), "error": message})
        return code

    def succeed(self, verb: str, document: Mapping[str, object]) -> ExitCode:
        """Report a success as the one object under the flag, and hand back the status."""
        body: dict[str, object] = {"verb": verb, "ok": True, "exit_code": int(ExitCode.SUCCESS)}
        body.update(document)
        self.emit(body)
        return ExitCode.SUCCESS
