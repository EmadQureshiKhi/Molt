"""Secret redaction applied to event payloads before any payload leaves a machine.

This runs on the capture path, ahead of every write. The memory layer holds a
shadow copy of client source code, so a credential that reached it would make
it the single worst store in the system to lose. Removal here, on the machine
that produced the observation, is what keeps that from being possible.

Two guarantees are made by construction rather than by testing.

**Idempotence.** Redacting a redacted payload returns the same payload. The
placeholder belongs to no alphabet a value shape admits, and the shapes that
replace only part of a span guard against an existing placeholder, so a second
pass has nothing left to find.

**Structure preservation.** A redacted payload carries the same keys in the
same order at every level, sequences of the same length in the same order, and
non-string scalars unchanged in both value and type. A number is never redacted
into a string and a list of five never becomes a list of four, because a
consumer reasons about the shape of a payload and redaction is not entitled to
change what it is reasoning about. The one place shape does change is the depth
cut: below the configured bound a container is emptied and its own type is
preserved, so a truncated branch is still a mapping if it was a mapping.

The modified flag is set by comparing produced values against their inputs at
every node, not by observing that an expression fired. Those differ: a shape
can match a span that already holds the placeholder and produce identical text,
and a payload that did not change must not be marked as redacted.

This module holds no telemetry dependency. With redaction disabled the payload
passes through untouched and the warning the operator is owed travels back as
data on the result, for the component that owns the emission path to write. The
capture path's only side effect stays its return value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from molt.models.event import JsonObject, JsonValue
from molt.redact.patterns import (
    BUILTIN_SENSITIVE_NAMES,
    PATTERN_CLASS_NAMES,
    REDACTION_PLACEHOLDER,
    is_sensitive_key,
    sensitive_key_pattern,
    substitute_value_shapes,
)

__all__ = [
    "BUILTIN_SENSITIVE_NAMES",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_SETTINGS",
    "PATTERN_CLASS_NAMES",
    "REDACTION_DISABLED_RECORD",
    "REDACTION_PLACEHOLDER",
    "WARNING_LEVEL",
    "RedactionResult",
    "RedactionSettings",
    "RedactionWarning",
    "redact",
    "redact_payload",
    "redact_text",
]

# The recursion bound. Thirty-two levels is deeper than any hook payload
# observed in practice, and a bound is what keeps a cyclic or adversarially
# nested document from costing the capture path its latency budget.
DEFAULT_MAX_DEPTH: Final[int] = 32

# The name and level of the record a caller emits when redaction is disabled.
REDACTION_DISABLED_RECORD: Final[str] = "redact.disabled"
WARNING_LEVEL: Final[str] = "warning"


@dataclass(frozen=True, slots=True)
class RedactionSettings:
    """The operator-controlled inputs to redaction.

    These arrive as explicit values rather than being read from a configuration
    module, so this component depends on nothing and the wiring is the caller's
    concern. Configured names extend the built-in set rather than replacing it.
    """

    disabled: bool = False
    max_depth: int = DEFAULT_MAX_DEPTH
    sensitive_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ValueError("the redaction depth bound must admit at least one level")


DEFAULT_SETTINGS: Final[RedactionSettings] = RedactionSettings()


@dataclass(frozen=True, slots=True)
class RedactionWarning:
    """The record a caller emits, naming the Session redaction was skipped for."""

    record: str
    level: str
    session_id: UUID | None


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """A redacted payload, whether it changed, and any warning owed."""

    payload: JsonObject
    modified: bool
    warning: RedactionWarning | None


def redact_text(text: str, *, settings: RedactionSettings = DEFAULT_SETTINGS) -> tuple[str, bool]:
    """Redact one string, reporting whether the result differs from the input."""
    if settings.disabled:
        return text, False
    replaced = substitute_value_shapes(text)
    return replaced, replaced != text


def redact(
    value: JsonValue,
    depth: int = 0,
    *,
    settings: RedactionSettings = DEFAULT_SETTINGS,
) -> tuple[JsonValue, bool]:
    """Redact any JSON value, reporting whether the result differs from the input.

    The depth argument names the level the value already sits at, so a caller
    resuming a walk part way down keeps the same bound as one starting at a
    document root.
    """
    if settings.disabled:
        return value, False
    return _redact(
        value,
        depth=depth,
        limit=settings.max_depth,
        sensitive=False,
        keys=sensitive_key_pattern(settings.sensitive_names),
    )


def redact_payload(
    payload: JsonObject,
    *,
    session_id: UUID | None = None,
    settings: RedactionSettings = DEFAULT_SETTINGS,
) -> RedactionResult:
    """Redact one Event payload and report the flag the Event field is set from.

    With redaction disabled the payload is returned untouched, the flag is
    false, and the warning naming the Session is returned for the caller to
    emit, because this component reaches no telemetry surface itself.
    """
    if settings.disabled:
        return RedactionResult(
            payload=payload,
            modified=False,
            warning=RedactionWarning(
                record=REDACTION_DISABLED_RECORD,
                level=WARNING_LEVEL,
                session_id=session_id,
            ),
        )
    redacted, modified = _redact_mapping(
        payload,
        depth=0,
        limit=settings.max_depth,
        sensitive=False,
        keys=sensitive_key_pattern(settings.sensitive_names),
    )
    return RedactionResult(payload=redacted, modified=modified, warning=None)


# --------------------------------------------------------------------------
# The recursive walk
# --------------------------------------------------------------------------


def _redact(
    value: JsonValue,
    *,
    depth: int,
    limit: int,
    sensitive: bool,
    keys: re.Pattern[str],
) -> tuple[JsonValue, bool]:
    """Redact one value at a known depth, under a known key sensitivity."""
    if isinstance(value, str):
        return _redact_string(value, sensitive=sensitive)
    if isinstance(value, dict):
        if depth >= limit:
            return _truncate_mapping(value)
        return _redact_mapping(value, depth=depth, limit=limit, sensitive=sensitive, keys=keys)
    if isinstance(value, list):
        if depth >= limit:
            return _truncate_sequence(value)
        return _redact_sequence(value, depth=depth, limit=limit, sensitive=sensitive, keys=keys)
    # A boolean, an integer, a real, or an absent value. Returned as the same
    # object, so no scalar ever changes type or value.
    return value, False


def _redact_string(text: str, *, sensitive: bool) -> tuple[str, bool]:
    """Replace a whole string under a sensitive key, or its shapes otherwise.

    Under a sensitive key the string is replaced wholesale whatever it looks
    like, because the key already said the value is a credential and a
    credential that happens to look ordinary is the case that matters.
    """
    if sensitive:
        return REDACTION_PLACEHOLDER, text != REDACTION_PLACEHOLDER
    replaced = substitute_value_shapes(text)
    return replaced, replaced != text


def _redact_mapping(
    value: JsonObject,
    *,
    depth: int,
    limit: int,
    sensitive: bool,
    keys: re.Pattern[str],
) -> tuple[JsonObject, bool]:
    """Rebuild a mapping with the same keys in the same order.

    Sensitivity is inherited downward: once a key names a credential, every
    string anywhere beneath it is replaced, whatever the shape of the subtree.
    """
    redacted: JsonObject = {}
    modified = False
    for key, item in value.items():
        child_sensitive = sensitive or is_sensitive_key(key, keys)
        redacted_item, item_modified = _redact(
            item,
            depth=depth + 1,
            limit=limit,
            sensitive=child_sensitive,
            keys=keys,
        )
        redacted[key] = redacted_item
        modified = modified or item_modified
    return redacted, modified


def _redact_sequence(
    value: list[JsonValue],
    *,
    depth: int,
    limit: int,
    sensitive: bool,
    keys: re.Pattern[str],
) -> tuple[list[JsonValue], bool]:
    """Rebuild a sequence of the same length in the same order."""
    redacted: list[JsonValue] = []
    modified = False
    for item in value:
        redacted_item, item_modified = _redact(
            item,
            depth=depth + 1,
            limit=limit,
            sensitive=sensitive,
            keys=keys,
        )
        redacted.append(redacted_item)
        modified = modified or item_modified
    return redacted, modified


def _truncate_mapping(value: JsonObject) -> tuple[JsonObject, bool]:
    """Cut a mapping at the depth bound, keeping the fact that it is a mapping."""
    if not value:
        return value, False
    return {}, True


def _truncate_sequence(value: list[JsonValue]) -> tuple[list[JsonValue], bool]:
    """Cut a sequence at the depth bound, keeping the fact that it is a sequence."""
    if not value:
        return value, False
    return [], True
