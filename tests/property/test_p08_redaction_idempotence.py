"""Property 8: redacting a redacted payload changes nothing further.

Idempotence is what makes redaction safe to apply more than once, and more than
one path applies it: a capture adapter redacts, a re-processing path redacts
again, and a payload read back out of storage has already been through it. If a
second pass could rewrite anything, the stored value would depend on how many
times it happened to be processed.

The generated payloads carry already-replaced spans on purpose. The placeholder
belongs to no alphabet a value shape admits, and the two classes that keep part
of a matched span carry an explicit guard against it; a payload that has already
been redacted is the only thing that reaches those guards, so a generator
producing only fresh secrets would assert far less than it appears to.

**Validates: Requirements 4.1, 4.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from tests.property.strategies import payloads

from molt.models.event import JsonObject
from molt.redact import redact_payload


# Feature: molt, Property 8: For any nested payload containing embedded
# secret-shaped strings, applying the Redactor twice produces the same value as
# applying it once.
@given(payload=payloads())
@settings(max_examples=100)
def test_redaction_is_idempotent(payload: JsonObject) -> None:
    once = redact_payload(payload)
    twice = redact_payload(once.payload)
    assert twice.payload == once.payload
