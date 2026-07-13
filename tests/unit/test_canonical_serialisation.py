"""Unit coverage for the nine canonical serialisation rules."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final, cast
from uuid import UUID

import pytest

from molt.attest.canonical import (
    CERTIFICATE_ARRAY_RULES,
    EVENT_ARRAY_RULES,
    CanonicalValue,
    MissingArraySortKeyError,
    NaiveTimestampError,
    NonFiniteValueError,
    UndeclaredArraySortKeyError,
    UnsupportedValueError,
    canonicalise,
)

BOM: Final[bytes] = b"\xef\xbb\xbf"
OFFSET: Final[timezone] = timezone(timedelta(hours=5, minutes=30))
FIRST: Final[UUID] = UUID("11111111-1111-4111-8111-111111111111")
SECOND: Final[UUID] = UUID("22222222-2222-4222-8222-222222222222")
# Shape of an RFC 3339 form with microsecond precision and a numeric offset.
TIMESTAMP_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}[+\-]\d{2}:\d{2}\Z"
)


def _moment() -> datetime:
    """A fixed aware instant carrying a non-zero offset and microseconds."""
    base = datetime.fromtimestamp(1_000_000, tz=UTC)
    return base.astimezone(OFFSET).replace(microsecond=123456)


def _field(rendered: bytes, name: str) -> str:
    """Read one rendered scalar field out of the canonical text."""
    return rendered.decode("utf-8").split(f'"{name}":"', 1)[1].split('"', 1)[0]


def test_key_insertion_order_does_not_change_the_bytes() -> None:
    forwards: dict[str, CanonicalValue] = {
        "alpha": "1",
        "beta": {"inner_a": "x", "inner_b": {"deep_a": 1, "deep_b": 2}},
        "gamma": None,
    }
    backwards: dict[str, CanonicalValue] = {
        "gamma": None,
        "beta": {"inner_b": {"deep_b": 2, "deep_a": 1}, "inner_a": "x"},
        "alpha": "1",
    }
    assert canonicalise(forwards) == canonicalise(backwards)


def test_keys_are_ordered_by_code_point_not_by_case_folding() -> None:
    payload: dict[str, CanonicalValue] = {"b": 1, "A": 1, "a": 1, "B": 1}
    rendered = canonicalise(payload).decode("utf-8")
    assert rendered.index('"A"') < rendered.index('"B"')
    assert rendered.index('"B"') < rendered.index('"a"')
    assert rendered.index('"a"') < rendered.index('"b"')


def test_array_order_does_not_change_the_bytes_for_a_declared_collection() -> None:
    one: dict[str, CanonicalValue] = {
        "dispositions": [{"artifact_id": FIRST}, {"artifact_id": SECOND}],
        "lineage_subgraph": [
            {"child_id": SECOND, "parent_id": FIRST},
            {"child_id": FIRST, "parent_id": SECOND},
        ],
        "bindings_before": ["borealis", "acme"],
    }
    other: dict[str, CanonicalValue] = {
        "dispositions": [{"artifact_id": SECOND}, {"artifact_id": FIRST}],
        "lineage_subgraph": [
            {"child_id": FIRST, "parent_id": SECOND},
            {"child_id": SECOND, "parent_id": FIRST},
        ],
        "bindings_before": ["acme", "borealis"],
    }
    rendered = canonicalise(one)
    assert rendered == canonicalise(other)
    assert rendered.index(b"acme") < rendered.index(b"borealis")


def test_declared_order_is_total_when_sort_fields_tie() -> None:
    one: dict[str, CanonicalValue] = {
        "sessions": [
            {"session_id": FIRST, "row_count": 2},
            {"session_id": FIRST, "row_count": 1},
        ]
    }
    other: dict[str, CanonicalValue] = {
        "sessions": [
            {"session_id": FIRST, "row_count": 1},
            {"session_id": FIRST, "row_count": 2},
        ]
    }
    assert canonicalise(one) == canonicalise(other)


def test_bound_parameter_order_is_preserved() -> None:
    payload: dict[str, CanonicalValue] = {
        "verification_queries": [{"name": "q", "params": ["second", "first"]}]
    }
    rendered = canonicalise(payload).decode("utf-8")
    assert rendered.index("second") < rendered.index("first")


def test_undeclared_array_is_refused() -> None:
    payload: dict[str, CanonicalValue] = {"unknown_collection": [{"artifact_id": FIRST}]}
    with pytest.raises(UndeclaredArraySortKeyError):
        canonicalise(payload)


def test_missing_declared_sort_field_is_refused() -> None:
    payload: dict[str, CanonicalValue] = {"sessions": [{"row_count": 1}]}
    with pytest.raises(MissingArraySortKeyError):
        canonicalise(payload)


def test_opaque_subtree_preserves_array_order() -> None:
    payload: dict[str, CanonicalValue] = {"payload": {"steps": ["second", "first"]}}
    rendered = canonicalise(payload, array_rules=EVENT_ARRAY_RULES).decode("utf-8")
    assert rendered == '{"payload":{"steps":["second","first"]}}'


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_float_is_refused(value: float) -> None:
    payload: dict[str, CanonicalValue] = {"run": {"auto_include_threshold": value}}
    with pytest.raises(NonFiniteValueError):
        canonicalise(payload)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_decimal_is_refused(literal: str) -> None:
    payload: dict[str, CanonicalValue] = {"run": {"review_threshold": Decimal(literal)}}
    with pytest.raises(NonFiniteValueError):
        canonicalise(payload)


def test_naive_timestamp_is_refused() -> None:
    payload: dict[str, CanonicalValue] = {"submitted_at": _moment().replace(tzinfo=None)}
    with pytest.raises(NaiveTimestampError):
        canonicalise(payload)


def test_a_non_text_object_key_is_refused() -> None:
    payload = cast("dict[str, CanonicalValue]", {"payload": {1: "integer key"}})
    with pytest.raises(UnsupportedValueError):
        canonicalise(payload, array_rules=EVENT_ARRAY_RULES)


def test_a_value_outside_the_domain_is_refused() -> None:
    payload = cast("dict[str, CanonicalValue]", {"payload": {"kinds": frozenset({"a"})}})
    with pytest.raises(UnsupportedValueError):
        canonicalise(payload, array_rules=EVENT_ARRAY_RULES)


def test_representative_payload_shows_every_scalar_rule() -> None:
    payload: dict[str, CanonicalValue] = {
        "run": {
            "auto_include_threshold": 0.2,
            "review_threshold": Decimal("0.45"),
            "unembedded_artifact_count": 0,
            "working_rows_deleted": 12,
            "dry_run": False,
        },
        "client": {"client_id": UUID("3F2B0C4A-1111-4222-8333-AAAABBBBCCCC")},
        "submitted_at": _moment(),
        "residue_candidates": [
            {"artifact_id": FIRST, "cosine_distance": 0.183041, "model_id": None}
        ],
    }
    rendered = canonicalise(payload)

    # Rule 1 and rule 3: no byte order mark, no insignificant whitespace.
    assert not rendered.startswith(BOM)
    assert b" " not in rendered
    assert b"\n" not in rendered
    assert not rendered.endswith(b"\n")

    text = rendered.decode("utf-8")
    # Rule 4: six fractional digits for a threshold and a distance, a plain
    # decimal string for a count, and every number quoted.
    assert '"auto_include_threshold":"0.200000"' in text
    assert '"review_threshold":"0.450000"' in text
    assert '"cosine_distance":"0.183041"' in text
    assert '"unembedded_artifact_count":"0"' in text
    assert '"working_rows_deleted":"12"' in text
    # Rule 5: bare boolean literals and an explicit null for an absent value.
    assert '"dry_run":false' in text
    assert '"model_id":null' in text
    # Rule 7: lowercase hyphenated.
    assert '"client_id":"3f2b0c4a-1111-4222-8333-aaaabbbbcccc"' in text
    # Rule 6: RFC 3339, microsecond precision, numeric offset rather than a
    # zone-designator suffix.
    stamp = _field(rendered, "submitted_at")
    assert TIMESTAMP_SHAPE.fullmatch(stamp)
    assert stamp == _moment().isoformat(timespec="microseconds")


def test_integer_and_real_classes_render_differently() -> None:
    payload: dict[str, CanonicalValue] = {"count": 7, "distance": 7}
    assert '"count":"7"' in canonicalise(payload).decode("utf-8")
    real: dict[str, CanonicalValue] = {"distance": 7.0}
    assert '"distance":"7.000000"' in canonicalise(real).decode("utf-8")


def test_a_vanishing_negative_magnitude_renders_unsigned() -> None:
    payload: dict[str, CanonicalValue] = {"distance": -1e-12}
    assert '"distance":"0.000000"' in canonicalise(payload).decode("utf-8")


def test_a_single_byte_alteration_changes_the_bytes() -> None:
    payload: dict[str, CanonicalValue] = {
        "dispositions": [{"artifact_id": FIRST, "reason": "blended_artifact_rewritten"}]
    }
    altered: dict[str, CanonicalValue] = {
        "dispositions": [{"artifact_id": FIRST, "reason": "blended_artifact_rewrittem"}]
    }
    assert canonicalise(payload) != canonicalise(altered)


def test_certificate_rules_are_the_default() -> None:
    payload: dict[str, CanonicalValue] = {"sessions": [{"session_id": FIRST}]}
    assert canonicalise(payload) == canonicalise(payload, array_rules=CERTIFICATE_ARRAY_RULES)
