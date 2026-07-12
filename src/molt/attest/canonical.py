"""The single canonical serialiser for every signed and digested payload.

A signature commits to bytes. An independent verifier recomputes those bytes
from the same logical payload and compares digests, so serialisation has to be
a total function of content alone: not of key insertion order, not of array
insertion order, not of platform floating-point formatting, not of the local
time zone. Nine rules make that true, and this module is the only place they
are implemented, so a signer and a verifier cannot drift apart.

The rules:

1. Output is UTF-8 carrying no byte order mark, and it is returned as bytes
   rather than as text so no later encoding step can introduce one.
2. Object keys are ordered ascending by Unicode code point at every nesting
   level. No locale collation and no case folding takes part.
3. No insignificant whitespace: the separators are a bare comma and a bare
   colon, nothing surrounds them, and the output carries no trailing newline.
4. Every number becomes a JSON string. An integer renders as a plain decimal
   string with no fractional part, because a count, a sequence number, and a
   fencing generation are exact. A real value, meaning a threshold, a cosine
   distance, or a confidence, renders with exactly six fractional digits,
   because the shortest round-trip form of a binary float differs between
   producers and that difference would change the bytes.
5. An absent optional value renders as an explicit null rather than being
   omitted, so a verifier and a signer cannot disagree over whether a key was
   present. Booleans render as the bare JSON literals.
6. A timestamp renders in RFC 3339 form with microsecond precision and a
   numeric offset. A value carrying no offset is refused rather than guessed
   at, because guessing would make the bytes depend on where they were built.
7. A UUID renders lowercase and hyphenated.
8. Every array is ordered by a declared rule, so array order is a function of
   content. An array reached under a key that no rule names is an error rather
   than a silent pass-through of insertion order.
9. A non-finite real value aborts serialisation. Not-a-number and the two
   infinities have no canonical decimal form, so any bytes produced for them
   would carry a signature nobody could reproduce.

Rules 2 through 7 and rule 9 need no declaration from the caller. Rule 8 does,
because only the payload's own shape says whether an array's order is content
or incident. The declarations for the certificate payload and for the Event
payload are module constants below.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import Final, Literal, TypeAlias
from uuid import UUID

from molt.errors import MoltError

__all__ = [
    "CERTIFICATE_ARRAY_RULES",
    "EVENT_ARRAY_RULES",
    "OPAQUE_SUBTREE",
    "PRESERVE_ORDER",
    "SORT_BY_CONTENT",
    "ArrayRule",
    "CanonicalValue",
    "CanonicalisationError",
    "MissingArraySortKeyError",
    "NaiveTimestampError",
    "NonFiniteValueError",
    "UndeclaredArraySortKeyError",
    "UnsupportedValueError",
    "canonicalise",
]

# --------------------------------------------------------------------------
# Accepted value domain
# --------------------------------------------------------------------------

CanonicalValue: TypeAlias = (
    bool
    | int
    | float
    | Decimal
    | str
    | UUID
    | datetime
    | Mapping[str, "CanonicalValue"]
    | Sequence["CanonicalValue"]
    | None
)

# --------------------------------------------------------------------------
# Array ordering declarations (rule 8)
# --------------------------------------------------------------------------

# Order the elements by their own canonical form. Total and content-determined
# for scalars and for structures alike, which is what a bag of slugs or a bag
# of audit records needs.
SORT_BY_CONTENT: Final = "content"

# Keep the given order because the order is itself content. A bound-parameter
# list is the case: the position of an element is what binds it to a
# placeholder, so reordering it would change meaning rather than presentation.
PRESERVE_ORDER: Final = "preserve"

# The value under this key is free-form content whose internal shape the
# payload does not describe. Keys are still ordered and scalars still follow
# rules 4 through 7 and rule 9, but every array below the key keeps its given
# order, because an array inside opaque content is a value rather than a
# collection this module is entitled to reorder.
OPAQUE_SUBTREE: Final = "opaque"

# A tuple names the fields to order mapping elements by, most significant
# first. Anything else is one of the three modes above.
ArrayRule: TypeAlias = tuple[str, ...] | Literal["content", "preserve", "opaque"]

# The declarations for the erasure certificate payload. The five collections
# the design names carry a field-based order; the remaining arrays of that
# payload are declared here as well, because leaving one undeclared would make
# a certificate unserialisable rather than making its order incidental.
CERTIFICATE_ARRAY_RULES: Final[Mapping[str, ArrayRule]] = MappingProxyType(
    {
        "dispositions": ("artifact_id",),
        "residue_candidates": ("artifact_id",),
        "lineage_subgraph": ("child_id", "parent_id"),
        "sessions": ("session_id",),
        "verification_queries": ("name",),
        "bindings_before": SORT_BY_CONTENT,
        "bindings_after": SORT_BY_CONTENT,
        "records": SORT_BY_CONTENT,
        "params": PRESERVE_ORDER,
    }
)

# The declarations for an Event. Everything below the payload key is content
# captured from a third-party tool, so its arrays are values whose order is
# preserved rather than collections to be reordered.
EVENT_ARRAY_RULES: Final[Mapping[str, ArrayRule]] = MappingProxyType(
    {
        "payload": OPAQUE_SUBTREE,
    }
)

_FRACTIONAL_DIGITS: Final[int] = 6
_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-_FRACTIONAL_DIGITS)
_ZERO_REAL: Final[str] = "0." + "0" * _FRACTIONAL_DIGITS
# Wide enough for the full fixed-point expansion of any finite binary float,
# so quantisation of a legitimate value never overflows the working precision.
_WORKING_PRECISION: Final[int] = 400
_ROOT_PATH: Final[str] = "$"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class CanonicalisationError(MoltError):
    """A payload cannot be reduced to canonical bytes.

    Every subclass names the position at fault rather than quoting the value,
    because these messages reach log records and a payload holds memory
    content.
    """


class NonFiniteValueError(CanonicalisationError):
    """Rule 9: not-a-number or an infinity has no canonical decimal form."""


class UnsupportedValueError(CanonicalisationError):
    """A value lies outside the accepted domain, so no rule governs it."""


class NaiveTimestampError(CanonicalisationError):
    """Rule 6: a timestamp carrying no offset would serialise ambiguously."""


class UndeclaredArraySortKeyError(CanonicalisationError):
    """Rule 8: an array was reached under a key that no rule orders."""


class MissingArraySortKeyError(CanonicalisationError):
    """Rule 8: an element does not carry the field its rule orders by."""


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------


def canonicalise(
    payload: Mapping[str, CanonicalValue],
    *,
    array_rules: Mapping[str, ArrayRule] = CERTIFICATE_ARRAY_RULES,
) -> bytes:
    """Reduce a payload to the canonical bytes a signature commits to.

    The result is byte-identical for any two payloads of equal content,
    whatever order their keys were inserted in and whatever order the elements
    of their declared collections arrived in. Any departure from the nine
    rules raises rather than producing bytes, because bytes that cannot be
    reproduced are worse than no bytes at all.
    """
    text = _render(payload, key=None, path=_ROOT_PATH, rules=array_rules, opaque=False)
    return text.encode("utf-8")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _render(
    value: object,
    *,
    key: str | None,
    path: str,
    rules: Mapping[str, ArrayRule],
    opaque: bool,
) -> str:
    """Render one value, dispatching on its kind rather than on its position.

    The parameter is deliberately untyped beyond `object` so that a value
    outside the accepted domain reaches the closing refusal instead of being
    assumed away by the type checker.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return _quote(f"{value:d}")
    if isinstance(value, float | Decimal):
        return _quote(_format_real(value, path))
    if isinstance(value, str):
        return _quote_text(value)
    if isinstance(value, UUID):
        return _quote(str(value).lower())
    if isinstance(value, datetime):
        return _quote(_format_timestamp(value, path))
    if isinstance(value, bytes | bytearray | memoryview):
        raise UnsupportedValueError(f"{path}: raw bytes carry no canonical JSON form")
    if isinstance(value, Mapping):
        return _render_mapping(value, path=path, rules=rules, opaque=opaque)
    if isinstance(value, Sequence):
        return _render_array(value, key=key, path=path, rules=rules, opaque=opaque)
    raise UnsupportedValueError(
        f"{path}: value of type {type(value).__name__} has no canonical form"
    )


def _render_mapping(
    value: Mapping[object, object],
    *,
    path: str,
    rules: Mapping[str, ArrayRule],
    opaque: bool,
) -> str:
    """Rule 2 and rule 3: code-point key order, no whitespace, explicit nulls."""
    fields: dict[str, object] = {}
    for raw_key, item in value.items():
        if not isinstance(raw_key, str):
            raise UnsupportedValueError(
                f"{path}: object key of type {type(raw_key).__name__} cannot be ordered"
            )
        fields[raw_key] = item
    parts: list[str] = []
    for name in sorted(fields):
        child_opaque = opaque or rules.get(name) == OPAQUE_SUBTREE
        rendered = _render(
            fields[name],
            key=name,
            path=f"{path}.{name}",
            rules=rules,
            opaque=child_opaque,
        )
        parts.append(f"{_quote_text(name)}:{rendered}")
    return "{" + ",".join(parts) + "}"


def _render_array(
    value: Sequence[object],
    *,
    key: str | None,
    path: str,
    rules: Mapping[str, ArrayRule],
    opaque: bool,
) -> str:
    """Rule 8: order by the declared rule, refusing an undeclared collection."""
    rendered = [
        _render(
            element,
            key=None,
            path=f"{path}[{index}]",
            rules=rules,
            opaque=opaque,
        )
        for index, element in enumerate(value)
    ]
    if opaque:
        return "[" + ",".join(rendered) + "]"
    rule = rules.get(key) if key is not None else None
    if rule is None:
        raise UndeclaredArraySortKeyError(
            f"{path}: array order is undeclared, so no reproducible order exists"
        )
    if isinstance(rule, tuple):
        ordered = _order_by_fields(value, rendered, keys=rule, path=path, rules=rules)
        return "[" + ",".join(ordered) + "]"
    if rule == PRESERVE_ORDER:
        return "[" + ",".join(rendered) + "]"
    return "[" + ",".join(sorted(rendered)) + "]"


def _order_by_fields(
    value: Sequence[object],
    rendered: Sequence[str],
    *,
    keys: tuple[str, ...],
    path: str,
    rules: Mapping[str, ArrayRule],
) -> list[str]:
    """Order mapping elements by their declared fields, ties broken by content.

    The tie-break on the element's own canonical form is what makes the order
    total: two elements agreeing on every declared field would otherwise keep
    whichever relative order they were inserted in.
    """
    decorated: list[tuple[tuple[str, ...], str]] = []
    for index, element in enumerate(value):
        position = f"{path}[{index}]"
        if not isinstance(element, Mapping):
            raise MissingArraySortKeyError(
                f"{position}: element is not an object, so it carries no sort field"
            )
        field_values: list[str] = []
        for name in keys:
            if name not in element:
                raise MissingArraySortKeyError(f"{position}: sort field {name} is absent")
            field_values.append(
                _render(
                    element[name],
                    key=name,
                    path=f"{position}.{name}",
                    rules=rules,
                    opaque=False,
                )
            )
        decorated.append((tuple(field_values), rendered[index]))
    decorated.sort(key=lambda entry: (entry[0], entry[1]))
    return [entry[1] for entry in decorated]


# --------------------------------------------------------------------------
# Scalar forms
# --------------------------------------------------------------------------


def _quote(text: str) -> str:
    """Wrap an already-safe token as a JSON string."""
    return f'"{text}"'


def _quote_text(text: str) -> str:
    """Escape and quote arbitrary text, leaving non-ASCII as UTF-8 content.

    Escaping is minimal and fixed: the quote, the backslash, and the control
    range only, so the same text yields the same bytes in every process.
    """
    return json.dumps(text, ensure_ascii=False)


def _format_real(value: float | Decimal, path: str) -> str:
    """Rule 4 and rule 9: exactly six fractional digits, or refuse.

    A binary float is read through its shortest round-trip decimal form before
    quantisation, so a value the caller wrote as two decimal places does not
    acquire the artefacts of its binary expansion. Half-way cases round to
    even, and a quantised zero renders unsigned, so no sign of a vanishing
    magnitude survives into the bytes.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonFiniteValueError(f"{path}: {_non_finite_name(value)} has no decimal form")
        source = Decimal(repr(value))
    else:
        if not value.is_finite():
            raise NonFiniteValueError(f"{path}: a non-finite decimal has no decimal form")
        source = value
    try:
        with localcontext() as context:
            context.prec = _WORKING_PRECISION
            quantised = source.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as error:
        raise UnsupportedValueError(
            f"{path}: magnitude exceeds the fixed-point form the rule requires"
        ) from error
    if quantised == 0:
        return _ZERO_REAL
    return f"{quantised:f}"


def _non_finite_name(value: float) -> str:
    """Name the offending class without quoting a platform-specific spelling."""
    if math.isnan(value):
        return "not-a-number"
    return "positive infinity" if value > 0 else "negative infinity"


def _format_timestamp(value: datetime, path: str) -> str:
    """Rule 6: RFC 3339 with microsecond precision and a numeric offset."""
    if value.utcoffset() is None:
        raise NaiveTimestampError(f"{path}: timestamp carries no offset")
    return value.isoformat(timespec="microseconds")
