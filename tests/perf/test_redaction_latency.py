"""Redaction of a 256 KiB payload stays inside its 50 millisecond bound.

Why the bound exists. Redaction runs on the capture path, inside the hook
invocation, before an Event leaves the machine. The whole hook has a 250
millisecond budget at the 95th percentile, so redaction owes a fraction of that
however large the payload it is handed. Instrumentation that costs an engineer
their session is the one failure the capture design refuses, and a slow scan is
how that failure would arrive.

Why three shapes rather than one. The bound is stated per payload, and a payload
of one size can be three different workloads:

- **One long neutral string** is the case the requirement literally names, and it
  is dominated by scanning: the alternation rejects nearly every position, so the
  cost is the walk itself. Near misses are deliberately dense in the text -- a
  short uppercase run, a dash run, the bearer keyword ahead of a short word, a
  scheme separator with no user information -- because a branch that starts and
  then fails is dearer than a position rejected on its first character.
- **One long secret-dense string** is dominated by substitution instead: nearly
  every position matches, and the work moves into building the replacement.
- **Many small strings across a deep structure** is dominated by neither. It pays
  per-node cost: a mapping rebuilt at every level, a key normalised and matched
  at every entry, and a function call per leaf. A payload can meet the bound as
  one string and miss it as four thousand, so the shape is covered separately.

Why best-of-N. Each shape is timed several times and the assertion is made
against the fastest sample. Background load on the measuring machine can only
add time to a sample, never remove it, so the minimum is the least noisy
estimate of what the code itself costs, and it does not drift when the machine
is busy. A median would be defensible but is strictly noisier here, and a timing
assertion that fails because something else was running is worse than no
assertion at all. The full spread is reported on every run so the headroom stays
visible rather than being asserted away, and the worst sample is reported beside
the best so a genuine regression is not hidden behind one lucky run.

A discarded warm-up call precedes the timed samples. The alternation is compiled
at import and the key matcher is cached, so the first call pays costs no later
call does, and charging those to the bound would measure import rather than
redaction.

No span of any recognised secret shape is written as a literal below. Each shape
is assembled from its alphabet at construction time, exactly as the shared
generators do, because a plausible credential in a tracked file is
indistinguishable from a real leak to anything scanning the tree. The shapes are
built directly here rather than drawn from the shared Hypothesis generators:
this measures a fixed workload, and a timing sample is only comparable across
runs when the bytes being timed are identical.

Each measurement also asserts what redaction found, so the numbers describe real
work: the neutral payload must come back unmodified, and the two payloads
carrying shapes must come back modified. Without those, a scan that silently
matched nothing would post excellent times.

**Validates: Requirements 4.7**
"""

from __future__ import annotations

import string
import time
from dataclasses import dataclass
from statistics import median
from typing import Final

import pytest

from molt.models.event import JsonObject, JsonValue
from molt.redact import redact_payload

# A bound measured in this process and nothing else: the payloads are built here,
# redaction runs here, and no socket and no connection is opened. The performance
# marker is therefore the only one, with no instance marker beside it, so these
# samples are taken on a bare checkout rather than skipped away as unreachable.
pytestmark = pytest.mark.perf

# The payload size the bound is stated for, and the bound itself.
TARGET_PAYLOAD_BYTES: Final[int] = 256 * 1024
LATENCY_BOUND_MS: Final[float] = 50.0

# Timed samples per shape, plus the discarded call ahead of them. Five samples of
# a workload measured in single-digit milliseconds keeps the whole module well
# under a second while still showing a spread.
TIMED_SAMPLES: Final[int] = 5
WARMUP_CALLS: Final[int] = 1

# --------------------------------------------------------------------------
# Alphabets, held as classes rather than as example spans
# --------------------------------------------------------------------------

_UPPER_ALNUM: Final[str] = string.ascii_uppercase + string.digits
_BASE64: Final[str] = string.ascii_letters + string.digits + "/+="
_UNRESERVED: Final[str] = string.ascii_letters + string.digits + "-._~+/"
_AUTHORITY_NAME: Final[str] = string.ascii_letters + string.digits + "-_."
_AUTHORITY_CREDENTIAL: Final[str] = string.ascii_letters + string.digits + "-_.!$%*"


def _run_from(alphabet: str, length: int, seed: int) -> str:
    """Fill a run of the given length from the given alphabet, reproducibly.

    The stride is coprime with neither alphabet in particular, which is all this
    needs: consecutive runs differ from one another, and the same seed always
    yields the same run, so two invocations of this module time the same bytes.
    """
    size = len(alphabet)
    return "".join(alphabet[(seed * 7 + index * 13) % size] for index in range(length))


# --------------------------------------------------------------------------
# Neutral text: dense in near misses, carrying no recognised shape
# --------------------------------------------------------------------------

# Each fragment ends in a separator, so joining them cannot accidentally compose
# a shape across a boundary. Every fragment is a near miss for one branch of the
# alternation: an uppercase run too short to be a key identifier, a dash run
# with no armour tail, the keyword ahead of a word too short to be a credential,
# and a scheme separator with no user information behind it.
_NEUTRAL_FRAGMENTS: Final[tuple[str, ...]] = (
    "the run completed with no findings and the file was written ",
    "AKIA SHORT UPPER RUN ",
    "----- BEGIN NOTHING AT ALL ----- ",
    "bearer word ",
    "https://localhost/health and nothing follows it ",
    "src/molt/redact/patterns.py exit status 0 ",
    "def redact(value): return value ",
)


def _neutral_text(length: int) -> str:
    """Build neutral text of at least the given length in bytes."""
    parts: list[str] = []
    produced = 0
    index = 0
    while produced < length:
        fragment = _NEUTRAL_FRAGMENTS[index % len(_NEUTRAL_FRAGMENTS)]
        parts.append(fragment)
        produced += len(fragment)
        index += 1
    return "".join(parts)


# --------------------------------------------------------------------------
# The five value shapes, each assembled from its alphabet
# --------------------------------------------------------------------------

_ACCESS_KEY_PREFIXES: Final[tuple[str, ...]] = ("AKIA", "ASIA", "AROA", "AIDA")
_CREDENTIAL_NAME: Final[str] = "aws_secret_access_key"
_BEARER_KEYWORD: Final[str] = "Bearer"

# The armour fragments and the scheme separator are held apart, so neither an
# armoured block nor a credentialed authority appears as one span in this file.
_ARMOUR_OPEN: Final[str] = "-----BEGIN"
_ARMOUR_CLOSE: Final[str] = "-----END"
_ARMOUR_TAIL: Final[str] = "PRIVATE KEY-----"
_SCHEME_SEPARATOR: Final[str] = "://"


def _access_key_identifier(seed: int) -> str:
    """A resource-type prefix followed by sixteen uppercase alphanumerics."""
    prefix = _ACCESS_KEY_PREFIXES[seed % len(_ACCESS_KEY_PREFIXES)]
    return prefix + _run_from(_UPPER_ALNUM, 16, seed)


def _credential_assignment(seed: int) -> str:
    """The provider secret-key name assigned a forty-character value."""
    return f"{_CREDENTIAL_NAME}={_run_from(_BASE64, 40, seed)}"


def _bearer_credential(seed: int) -> str:
    """The keyword followed by a long run of unreserved characters."""
    return f"{_BEARER_KEYWORD} {_run_from(_UNRESERVED, 32, seed)}"


def _connection_string_credential(seed: int) -> str:
    """A scheme, credentialed user information, an authority, and a path."""
    user = _run_from(_AUTHORITY_NAME, 10, seed)
    credential = _run_from(_AUTHORITY_CREDENTIAL, 20, seed + 1)
    return f"postgres{_SCHEME_SEPARATOR}{user}:{credential}@localhost/molt"


def _private_key_block(seed: int) -> str:
    """An armoured block with a generated body of modest length."""
    body = _run_from(_BASE64, 64, seed)
    return f"{_ARMOUR_OPEN} {_ARMOUR_TAIL}\n{body}\n{_ARMOUR_CLOSE} {_ARMOUR_TAIL}"


# The five value classes the alternation recognises, rotated through in order.
_VALUE_CLASS_COUNT: Final[int] = 5


def _shape(index: int) -> str:
    """Draw the next shape in a fixed rotation over all five value classes."""
    which = index % _VALUE_CLASS_COUNT
    if which == 0:
        return _access_key_identifier(index)
    if which == 1:
        return _credential_assignment(index)
    if which == 2:
        return _bearer_credential(index)
    if which == 3:
        return _connection_string_credential(index)
    return _private_key_block(index)


def _secret_dense_text(length: int) -> str:
    """Build text of at least the given length in which nearly every span matches.

    The rotation covers all five value classes rather than repeating the cheapest
    one, so the measurement is not an accident of which branch happens to be
    tried first in the alternation.
    """
    parts: list[str] = []
    produced = 0
    index = 0
    while produced < length:
        span = _shape(index)
        parts.append(span)
        produced += len(span) + 1
        index += 1
    return " ".join(parts)


# --------------------------------------------------------------------------
# Payload shapes
# --------------------------------------------------------------------------

# The depth of the fan-out payload, and the size of each of its leaves. Eight
# levels sit inside the recursion bound, so the walk exercises replacement at
# every level rather than being cut short.
_FANOUT_DEPTH: Final[int] = 8
_FANOUT_LEAF_BYTES: Final[int] = 64

# One leaf in every run of this many carries a secret shape, so the fan-out case
# pays substitution cost as well as walk cost.
_SECRET_LEAF_STRIDE: Final[int] = 8


def single_neutral_string_payload() -> JsonObject:
    """One long neutral string under a neutral key: the scanning-dominated case."""
    return {"transcript": _neutral_text(TARGET_PAYLOAD_BYTES)}


def single_secret_dense_string_payload() -> JsonObject:
    """One long string of secret shapes: the substitution-dominated case."""
    return {"transcript": _secret_dense_text(TARGET_PAYLOAD_BYTES)}


def _fanout_leaf(index: int) -> str:
    """A leaf of the fixed leaf size, every eighth one carrying a secret shape.

    A leaf carrying a shape is padded to the same size as a neutral one, so the
    payload reaches the stated size whatever the mixture is. The padding is
    separated from the shape by a blank, which is outside every alphabet the
    shapes are built from, so padding cannot extend a span or truncate one.
    """
    if index % _SECRET_LEAF_STRIDE == 0:
        span = _access_key_identifier(index)
        padding = _neutral_text(_FANOUT_LEAF_BYTES)
        return f"{span} {padding}"[:_FANOUT_LEAF_BYTES]
    return _neutral_text(_FANOUT_LEAF_BYTES)[:_FANOUT_LEAF_BYTES]


def deep_structure_payload() -> JsonObject:
    """Many small strings spread across a deep nesting: the per-node case.

    Each level carries a list of small leaves, a value under a sensitive key so
    key-driven replacement fires at every level, and the level beneath it. Built
    innermost outward, so the total content is placed once rather than grown.
    """
    leaves_per_level = TARGET_PAYLOAD_BYTES // (_FANOUT_DEPTH * _FANOUT_LEAF_BYTES)
    child: JsonValue = None
    counter = 0
    for level in range(_FANOUT_DEPTH):
        items: list[JsonValue] = []
        for _ in range(leaves_per_level):
            items.append(_fanout_leaf(counter))
            counter += 1
        level_body: JsonObject = {
            "lines": items,
            "session_key": _run_from(_UNRESERVED, 24, level),
            "exit_code": level,
            "nested": child,
        }
        child = level_body
    if not isinstance(child, dict):  # pragma: no cover - the loop always builds one
        raise AssertionError("the fan-out payload must be a mapping")
    return child


def payload_content_bytes(payload: JsonObject) -> int:
    """Sum the encoded length of every key and every string value in a payload.

    Size is measured over content rather than over a serialised form, because
    what redaction walks is the keys and the strings; punctuation a serialiser
    would add is not work this component does.
    """
    return _content_bytes(payload)


def _content_bytes(value: JsonValue) -> int:
    """Recursive helper behind the content measurement."""
    if isinstance(value, str):
        return len(value.encode())
    if isinstance(value, dict):
        return sum(len(key.encode()) + _content_bytes(item) for key, item in value.items())
    if isinstance(value, list):
        return sum(_content_bytes(item) for item in value)
    return 0


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measurement:
    """The samples taken for one payload shape, in milliseconds."""

    label: str
    content_bytes: int
    samples: tuple[float, ...]

    @property
    def best(self) -> float:
        """The fastest sample, which is what the bound is asserted against."""
        return min(self.samples)

    @property
    def worst(self) -> float:
        """The slowest sample, reported so a regression is not hidden."""
        return max(self.samples)

    @property
    def middle(self) -> float:
        """The median sample, reported beside the best for context."""
        return median(self.samples)

    def summary(self) -> str:
        """One line naming the shape, its size, and the whole spread."""
        spread = ", ".join(f"{sample:.2f}" for sample in self.samples)
        return (
            f"{self.label}: {self.content_bytes} content bytes, "
            f"best {self.best:.2f} ms, median {self.middle:.2f} ms, "
            f"worst {self.worst:.2f} ms, bound {LATENCY_BOUND_MS:.0f} ms "
            f"[samples {spread}]"
        )


def measure(label: str, payload: JsonObject, *, expect_modified: bool) -> Measurement:
    """Time redaction of one payload, discarding a warm-up call first."""
    for _ in range(WARMUP_CALLS):
        warmed = redact_payload(payload)
        assert warmed.modified is expect_modified, (
            f"{label}: redaction reported modified={warmed.modified}, so the "
            "measurement would not describe the work the bound is about"
        )

    samples: list[float] = []
    for _ in range(TIMED_SAMPLES):
        started = time.perf_counter()
        redact_payload(payload)
        samples.append((time.perf_counter() - started) * 1000.0)

    return Measurement(
        label=label,
        content_bytes=payload_content_bytes(payload),
        samples=tuple(samples),
    )


def assert_within_bound(measurement: Measurement) -> None:
    """Assert the bound against the fastest sample and report the whole spread."""
    print(measurement.summary())
    assert measurement.best <= LATENCY_BOUND_MS, measurement.summary()


def assert_target_size(payload: JsonObject, label: str) -> None:
    """Assert the payload is at least the size the bound is stated for."""
    size = payload_content_bytes(payload)
    assert size >= TARGET_PAYLOAD_BYTES, (
        f"{label}: the payload holds {size} content bytes, which is under the "
        f"{TARGET_PAYLOAD_BYTES} the bound is stated for"
    )


# --------------------------------------------------------------------------
# The three shapes
# --------------------------------------------------------------------------


def test_single_neutral_string_within_bound() -> None:
    """A 256 KiB neutral string is scanned inside the bound.

    This is the case the requirement names literally, and it is the expensive
    one: no match short-circuits the walk, so every byte is examined.
    """
    payload = single_neutral_string_payload()
    assert_target_size(payload, "neutral single string")
    assert_within_bound(measure("neutral single string", payload, expect_modified=False))


def test_single_secret_dense_string_within_bound() -> None:
    """A 256 KiB string of secret shapes is rewritten inside the bound.

    Here the work is substitution rather than scanning, over all five value
    classes rather than whichever one the alternation reaches first.
    """
    payload = single_secret_dense_string_payload()
    assert_target_size(payload, "secret-dense single string")
    assert_within_bound(measure("secret-dense single string", payload, expect_modified=True))


def test_many_small_strings_across_deep_structure_within_bound() -> None:
    """256 KiB spread over thousands of small strings stays inside the bound.

    The bound is stated per payload, not per string, and this shape pays the
    per-node costs the single-string cases never reach: a mapping rebuilt at
    every level and a key normalised and matched at every entry.
    """
    payload = deep_structure_payload()
    assert_target_size(payload, "deep structure of small strings")
    assert_within_bound(measure("deep structure of small strings", payload, expect_modified=True))
