"""Property 9: redaction rewrites content and leaves shape alone.

A consumer of a redacted payload reasons about its shape: it reads a field by
name, it walks a sequence by position, and it treats a number as a number.
Redaction is entitled to change what a string says and is entitled to nothing
else, so the assertion here is stated over the whole document rather than over
one value: the same keys in the same order at every level, sequences of the same
length in the same order, and every non-string scalar unchanged in both value
and concrete type.

The type half of that is not implied by the value half. A boolean compares equal
to the integer it stands for, so a walk that compared values alone would accept
a flag silently becoming a count. Every position is therefore compared by
concrete type before it is compared by value.

The depth cut is the one place shape legitimately changes. At the configured
bound a container is emptied and its own type is kept, so a truncated branch is
still a mapping if it was a mapping and still a sequence if it was a sequence.
That is deliberate truncation rather than preservation, so the key-set and
length assertions are scoped to the levels above the bound and the assertion at
the bound is about container type alone. Reaching the bound cheaply is what the
deep payload variant and the small configured bounds are both for.

Deep documents are produced by burying a shallow payload under a chain of
single-child containers rather than by asking the shared generator for a deep
document directly. Raising that generator's depth argument raises the depth it
*admits*, and a freely branching recursion at that depth draws documents whose
size the generator's own budget rejects, so the examples that survive are the
shallow ones; measured over hundreds of examples, none reached even a quarter of
the nominal depth. A chain costs one node per level, so the depth is reached
every time and reached cheaply, and the shallow payload at the bottom keeps the
content variety the shared generator exists for.

The flag is asserted as a biconditional rather than as an implication in one
direction. Those come apart: a shape recogniser can fire on a span that already
holds the placeholder and produce text identical to its input, and a payload
that did not change must not be marked as redacted. The generator produces
payloads that change and payloads that do not, and both directions are asserted
against the same type-aware comparison the structure walk uses.

**Validates: Requirements 4.3, 4.4, 4.5**
"""

from __future__ import annotations

from typing import Final

from hypothesis import event, given, settings
from hypothesis import strategies as st
from tests.property.strategies import DEFAULT_PAYLOAD_DEPTH, payload_keys, payloads

from molt.models.event import JsonObject, JsonValue
from molt.redact import DEFAULT_MAX_DEPTH, RedactionSettings, redact_payload
from molt.redact.patterns import BUILTIN_SENSITIVE_KEY_PATTERN, is_sensitive_key

# The nesting the deep variant reaches. It sits above the default bound, so an
# example drawn from it reaches the cut with no configured bound at all, and far
# above the small bounds below, so the cut is reached at many levels.
DEEP_PAYLOAD_DEPTH: Final[int] = 40

# The levels of single-child containers a shallow payload is buried under. One
# level is the mapping the buried chain is returned inside, because a payload is
# a mapping, and the rest of the depth is the shallow payload's own.
BURIED_LEVELS: Final[int] = DEEP_PAYLOAD_DEPTH - DEFAULT_PAYLOAD_DEPTH - 1

# The largest of the small configured bounds. Drawing a bound from one upward
# puts the cut at several different levels, including immediately beneath the
# payload mapping itself, which is the cheapest place to reach it.
SMALL_BOUND_CEILING: Final[int] = 8


def _bury(base: JsonObject, keys: list[str], sequenced: list[bool], outer_key: str) -> JsonObject:
    """Return a shallow payload buried under further single-child containers.

    Each level is a mapping of one key or a sequence of one element, so both
    container types reach the cut, and the outermost level is a mapping because
    that is what a payload is. The drawn keys and container choices are cycled
    over the levels rather than drawn once per level, because what the chain is
    for is depth and a draw per level would cost more than the payload at the
    bottom does.
    """
    buried: JsonValue = base
    for level in range(BURIED_LEVELS):
        if sequenced[level % len(sequenced)]:
            buried = [buried]
        else:
            buried = {keys[level % len(keys)]: buried}
    return {outer_key: buried}


def _deep_payloads() -> st.SearchStrategy[JsonObject]:
    """Draw a payload nesting to the deep depth at one node per level."""
    return st.builds(
        _bury,
        payloads(),
        st.lists(payload_keys(), min_size=1, max_size=3),
        st.lists(st.booleans(), min_size=1, max_size=3),
        payload_keys(),
    )


def _identical(left: JsonValue, right: JsonValue) -> bool:
    """Report whether two values agree in shape, concrete type, and value.

    This is stricter than value equality on purpose, and it is what both the
    flag biconditional and the structure walk are stated against.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return list(left) == list(right) and all(_identical(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _identical(item, other) for item, other in zip(left, right, strict=True)
        )
    return left == right


def _assert_shape(
    original: JsonValue,
    produced: JsonValue,
    *,
    depth: int,
    limit: int,
    sensitive: bool,
) -> None:
    """Assert one position kept its concrete type, and recurse into containers."""
    assert type(produced) is type(original)
    if isinstance(original, dict) and isinstance(produced, dict):
        _assert_mapping_shape(original, produced, depth=depth, limit=limit, sensitive=sensitive)
    elif isinstance(original, list) and isinstance(produced, list):
        _assert_sequence_shape(original, produced, depth=depth, limit=limit, sensitive=sensitive)
    elif not isinstance(original, str):
        # A boolean, an integer, a real, or an absent value. Nothing about it
        # may move, whatever the key above it said, and a scalar under a
        # credential-shaped key is the case that would be easiest to get wrong.
        if sensitive:
            event(f"scalar of type {type(original).__name__} under a sensitive key")
        assert produced == original


def _assert_mapping_shape(
    original: JsonObject,
    produced: JsonObject,
    *,
    depth: int,
    limit: int,
    sensitive: bool,
) -> None:
    """Assert a mapping kept its keys, or was emptied because it sat at the bound."""
    if depth >= limit:
        if original:
            event(f"mapping cut at depth {depth}")
        assert produced == {}
        return
    assert list(produced) == list(original)
    for key, item in original.items():
        _assert_shape(
            item,
            produced[key],
            depth=depth + 1,
            limit=limit,
            sensitive=sensitive or is_sensitive_key(key, BUILTIN_SENSITIVE_KEY_PATTERN),
        )


def _assert_sequence_shape(
    original: list[JsonValue],
    produced: list[JsonValue],
    *,
    depth: int,
    limit: int,
    sensitive: bool,
) -> None:
    """Assert a sequence kept its length and order, or was emptied at the bound."""
    if depth >= limit:
        if original:
            event(f"sequence cut at depth {depth}")
        assert produced == []
        return
    assert len(produced) == len(original)
    for item, produced_item in zip(original, produced, strict=True):
        _assert_shape(item, produced_item, depth=depth + 1, limit=limit, sensitive=sensitive)


# Feature: molt, Property 9: For any nested payload, the redacted payload has
# the same key set at every level, the same sequence lengths, and the same value
# types as the input payload, and the `redacted` flag is true exactly when the
# output differs from the input.
@given(
    payload=st.one_of(payloads(), _deep_payloads()),
    max_depth=st.one_of(
        st.integers(min_value=1, max_value=SMALL_BOUND_CEILING),
        st.just(DEFAULT_MAX_DEPTH),
    ),
)
# No per-example deadline, as everywhere else in this suite: a wall-clock deadline
# fails an example for the load on the machine rather than for the property, which
# under parallel execution reports contention as a correctness failure. Latency
# bounds are stated deliberately in the performance suite.
@settings(max_examples=100, deadline=None)
def test_redaction_preserves_structure(payload: JsonObject, max_depth: int) -> None:
    result = redact_payload(payload, settings=RedactionSettings(max_depth=max_depth))
    _assert_shape(payload, result.payload, depth=0, limit=max_depth, sensitive=False)
    differs = not _identical(payload, result.payload)
    event("output differs from input" if differs else "output identical to input")
    assert result.modified == differs
