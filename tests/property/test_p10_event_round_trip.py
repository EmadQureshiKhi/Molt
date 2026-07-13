"""Property 10: the canonical Event wire form round trips.

**Validates: Requirements 7.5, 7.7, 5.6**

Equivalence here is the strong, field-by-field form rather than the equality the
frozen dataclass gives for free. Dataclass equality compares the timestamp by
instant, so two instants that name the same moment through different numeric
offsets compare equal; the stored column carries the offset and a verifier reads
it back, so losing the offset would be a defect this property must catch. The
assertions therefore pin the offset and the wall-clock reading separately, pin
the payload by structure so a boolean is not satisfied by the integer one, pin
which optional fields the wire form names, and pin that re-serialising the
recovered Event reproduces the same bytes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Final

from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.event import (
    Event,
    EventCategory,
    JsonObject,
    JsonValue,
    deserialise_event,
    serialise_event,
)

# The zero offset, kept as a named value so the coverage instrumentation can
# distinguish a plain zero-offset instant from one carrying a real offset.
NO_OFFSET: Final[timedelta] = timedelta(0)

# The widest whole-minute offset range the canonical numeric-offset form admits.
OFFSET_MINUTE_BOUND: Final[int] = 1439

# Bounds keeping generated integers inside the width a stored payload holds.
INTEGER_BOUND: Final[int] = 2**63


def _texts() -> st.SearchStrategy[str]:
    """Text spanning the ASCII range, text outside it, and the empty string."""
    return st.one_of(
        st.text(max_size=16),
        st.text(alphabet=st.characters(min_codepoint=0xA1, codec="utf-8"), max_size=8),
    )


def _offsets() -> st.SearchStrategy[timezone]:
    """Fixed whole-minute offsets, spanning both signs and including zero."""
    return st.integers(min_value=-OFFSET_MINUTE_BOUND, max_value=OFFSET_MINUTE_BOUND).map(
        lambda minutes: timezone(timedelta(minutes=minutes))
    )


def _at_offset(naive: datetime, offset: timezone) -> datetime:
    """Attach a fixed offset to a generated wall-clock reading."""
    return naive.replace(tzinfo=offset)


def _timestamps() -> st.SearchStrategy[datetime]:
    """Timezone-aware instants at microsecond precision, mostly not at zero offset."""
    return st.builds(_at_offset, st.datetimes(), _offsets())


def _json_values() -> st.SearchStrategy[JsonValue]:
    """Arbitrary JSON values: scalars, and containers nested to a real depth.

    Numbers are drawn finite because an Event refuses a non-finite one, and the
    container sizes start at zero so empty objects and empty arrays are drawn.
    """
    scalars: st.SearchStrategy[JsonValue] = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-INTEGER_BOUND, max_value=INTEGER_BOUND - 1),
        st.floats(allow_nan=False, allow_infinity=False),
        _texts(),
    )

    def _extend(children: st.SearchStrategy[JsonValue]) -> st.SearchStrategy[JsonValue]:
        return st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(_texts(), children, max_size=4),
        )

    return st.recursive(scalars, _extend, max_leaves=12)


def _payloads() -> st.SearchStrategy[JsonObject]:
    """A payload object, empty or holding arbitrary JSON values."""
    return st.dictionaries(_texts(), _json_values(), max_size=5)


def events() -> st.SearchStrategy[Event]:
    """Events over the whole category enumeration.

    The category strategy is drawn from `EventCategory` itself rather than from a
    chosen subset, so every member is in the domain by construction. Both
    optional fields are drawn present and absent, never as an explicit null,
    because the wire form omits an absent optional field rather than emitting one.
    """
    return st.builds(
        Event,
        id=st.uuids(),
        session_id=st.uuids(),
        client_id=st.uuids(),
        category=st.sampled_from(EventCategory),
        occurred_at=_timestamps(),
        agent_cli=_texts(),
        machine_id=_texts(),
        parent_event_id=st.one_of(st.none(), st.uuids()),
        payload=_payloads(),
        redacted=st.booleans(),
        text_body=st.one_of(st.none(), _texts()),
    )


def _wire_field_names(wire: str) -> frozenset[str]:
    """The field names one serialisation names, for the absence assertions."""
    decoded: object = json.loads(wire)
    if not isinstance(decoded, dict):
        raise TypeError("an Event serialisation must be one JSON object")
    return frozenset(str(name) for name in decoded)


def _same_shape(left: JsonValue, right: JsonValue) -> bool:
    """Compare two JSON values by structure and by exact scalar type.

    Value equality alone is too weak for a payload: a boolean and the integer
    one compare equal in Python, so a round trip that turned one into the other
    would pass. Comparing the concrete type at every position closes that gap.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(_same_shape(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_shape(one, other) for one, other in zip(left, right, strict=True)
        )
    return left == right


def _assert_round_trip(original: Event) -> None:
    """Assert the strong equivalence between one Event and its recovery."""
    wire = serialise_event(original)
    restored = deserialise_event(wire)

    # Every field recovers, and the wire form is stable under a second pass.
    assert restored == original
    assert serialise_event(restored) == wire

    # The timestamp keeps its offset and its wall-clock reading, not merely its
    # instant, and keeps microsecond precision.
    assert restored.occurred_at.utcoffset() == original.occurred_at.utcoffset()
    assert restored.occurred_at.replace(tzinfo=None) == original.occurred_at.replace(tzinfo=None)
    assert restored.occurred_at.microsecond == original.occurred_at.microsecond

    # Field-by-field equivalence, stated rather than inferred from the dataclass.
    assert restored.id == original.id
    assert restored.session_id == original.session_id
    assert restored.client_id == original.client_id
    assert restored.category is original.category
    assert restored.agent_cli == original.agent_cli
    assert restored.machine_id == original.machine_id
    assert restored.parent_event_id == original.parent_event_id
    assert restored.redacted is original.redacted
    assert restored.text_body == original.text_body

    # The payload survives by structure and by scalar type at every position.
    assert _same_shape(original.payload, restored.payload)

    # An absent optional field is omitted from the wire form rather than null.
    named = _wire_field_names(wire)
    assert ("parent_event_id" in named) == (original.parent_event_id is not None)
    assert ("text_body" in named) == (original.text_body is not None)


# Feature: molt, Property 10: For any Event across all Event categories with an
# arbitrary JSON-compatible payload, deserialising the canonical serialisation of
# that Event yields an equivalent Event, preserving timezone, microsecond
# precision, optional-field absence, and payload structure.
@settings(max_examples=100)
@given(original=events())
def test_event_round_trip(original: Event) -> None:
    offset = original.occurred_at.utcoffset()
    event("offset=zero" if offset == NO_OFFSET else "offset=non-zero")
    event(
        "parent_event_id=absent" if original.parent_event_id is None else "parent_event_id=present"
    )
    event("text_body=absent" if original.text_body is None else "text_body=present")

    # The category dimension is covered structurally rather than left to
    # sampling: the drawn Event is carried through every member of the
    # enumeration, so a hundred examples exercise all of them a hundred times
    # each. Sampling alone leaves several members undrawn at a hundred examples,
    # and the property is quantified over all categories rather than over most.
    for category in EventCategory:
        event(f"category={category.value}")
        _assert_round_trip(replace(original, category=category))
