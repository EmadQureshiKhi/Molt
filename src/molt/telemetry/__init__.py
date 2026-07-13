"""Metrics, structured log records, correlation, and the billable cardinality bound.

Three surfaces are exported: `metric` records a measurement, `log` writes one
structured record, and `correlation` establishes the identifier every record
inside a block carries. All three are thin wrappers over a `Telemetry` instance,
so a test can hold its own instance with its own stream and its own bound while
production code calls the module-level surfaces and never passes a handle around.

**Cardinality is bounded before emission, and the bound is the cost control.**
Each distinct combination of a metric name and its dimension values becomes a
separate billable metric, so the emitter tracks the combinations it has already
published and stops at the configured maximum. Beyond the bound the measurement
is written as a structured log record carrying the same name, the same value, and
the same dimensions, so observability is retained while the billable count is
not.

The overflow signal `telemetry.cardinality_overflow` counts the diversions. It is
undimensioned, which is the whole reason it is safe: a dimensioned overflow
counter would multiply into the very budget it exists to protect, whereas one
undimensioned counter is a single fixed addition that cannot grow with load. It
sits outside the bound the caller's metrics are measured against, and it must: a
diversion happens only once the bound is already saturated, and a published
combination is already billed and so cannot be withdrawn, so a guard sharing the
bound could never be published at all. The bound therefore governs the caller's
combinations, which `combinations` reports and which never exceeds the maximum,
and the guard is one fixed counter reported by `overflow_count` alongside them.

**The field filter drops content rather than truncating it.** Memory content
bodies, credential values, and embedding vectors are removed from a log record
before serialisation, by key, at every depth. A value of no JSON type is rendered
through its own text conversion, which is what makes a wrapped credential appear
as its placeholder rather than as itself.

Records are single-line JSON on standard error, with severity, component name,
message, and correlation identifier always present and always first. A delivery
backend can be attached to the buffered counters later; nothing here reaches a
metrics service, and nothing here fails a caller because telemetry failed.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, TextIO, TypeAlias

from molt.config.resolve import Configuration
from molt.models.event import JsonObject, JsonValue

__all__ = [
    "CONTENT_KEYS",
    "CONTENT_KEY_MARKERS",
    "DEFAULT_CARDINALITY_MAX",
    "DEFAULT_NAMESPACE",
    "LOG_KEY_ORDER",
    "OVERFLOW_METRIC",
    "UNIT_COUNT",
    "UNSET_CORRELATION",
    "Combination",
    "Dimensions",
    "MetricDelivery",
    "MetricSample",
    "Severity",
    "Telemetry",
    "configure",
    "correlation",
    "current",
    "current_correlation",
    "is_content_key",
    "log",
    "metric",
    "reset",
]

# The four keys every log record carries, in the order they are written.
LOG_KEY_ORDER: Final[tuple[str, ...]] = ("severity", "component", "message", "correlation_id")

# The correlation identifier written when no correlation block is open. A fixed
# token rather than an omission, because the field is always present.
UNSET_CORRELATION: Final[str] = "unset"

# The undimensioned counter incremented whenever a measurement is diverted.
OVERFLOW_METRIC: Final[str] = "telemetry.cardinality_overflow"

# Defaults matching the configuration surface, for an instance built without one.
DEFAULT_NAMESPACE: Final[str] = "Molt"
DEFAULT_CARDINALITY_MAX: Final[int] = 10
UNIT_COUNT: Final[str] = "Count"

# How many samples the in-process buffer holds before the oldest are discarded.
# Bounded on purpose: with no delivery backend attached the buffer must not grow
# without limit, and the counters remain the durable in-process record.
_PENDING_SAMPLE_MAX: Final[int] = 1024

# Field names a log record never carries, matched exactly and case-insensitively.
CONTENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "authorization",
        "body",
        "connection_string",
        "content",
        "credential",
        "dsn",
        "embedding",
        "embeddings",
        "excerpt",
        "fragment",
        "password",
        "payload",
        "private_key",
        "prompt",
        "replacement_body",
        "response",
        "session_key",
        "text_body",
        "vector",
        "vectors",
    }
)

# Substrings that mark a field name as carrying content, so a name the exact set
# does not list is still dropped. Dropping a harmless field is a cost worth
# paying; letting a content body reach a log record is not.
CONTENT_KEY_MARKERS: Final[tuple[str, ...]] = (
    "_body",
    "body_",
    "credential",
    "embedding",
    "password",
    "payload",
    "secret",
    "vector",
)

# A dimension set, normalised to a sorted tuple of pairs so that the same
# dimensions supplied in a different order are the same combination.
Dimensions: TypeAlias = tuple[tuple[str, str], ...]

# The identity of one billable metric: its name together with its dimensions.
Combination: TypeAlias = tuple[str, Dimensions]

# The sentinel a filtered field is replaced by, meaning drop this field entirely.
_DROPPED: Final[object] = object()


class Severity(StrEnum):
    """Log severities, in ascending order of importance."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


_SEVERITY_RANK: Final[Mapping[Severity, int]] = MappingProxyType(
    {
        Severity.DEBUG: 10,
        Severity.INFO: 20,
        Severity.WARNING: 30,
        Severity.ERROR: 40,
    }
)


@dataclass(frozen=True, slots=True)
class MetricSample:
    """One measurement, as a delivery backend will receive it."""

    name: str
    value: float
    unit: str
    dimensions: Dimensions


class MetricDelivery(Protocol):
    """The seam a delivery backend attaches to.

    Nothing in this module reaches a metrics service. A backend implementing this
    receives the buffered samples on flush, which is the whole extent of the
    coupling.
    """

    def deliver(self, namespace: str, samples: Sequence[MetricSample]) -> None:
        """Publish a batch of samples under a namespace."""


_correlation_id: ContextVar[str | None] = ContextVar("molt_correlation_id", default=None)


def current_correlation() -> str:
    """The identifier every record written inside the current block carries."""
    return _correlation_id.get() or UNSET_CORRELATION


@contextmanager
def correlation(identifier: str) -> Iterator[str]:
    """Establish the correlation identifier for the duration of a block.

    Nothing has to pass the identifier explicitly: an Erasure_Run sets it once at
    run start and every record produced during the run carries it. The previous
    identifier is restored on exit, so nesting is well behaved.
    """
    token = _correlation_id.set(identifier or UNSET_CORRELATION)
    try:
        yield current_correlation()
    finally:
        _correlation_id.reset(token)


# Recursion bound on a log field: a structure deeper than this is a diagnostic
# that has stopped being readable, so it is dropped rather than walked.
_MAX_FIELD_DEPTH: Final[int] = 32


def _is_content_key(name: str) -> bool:
    """Whether a field name is one a log record never carries."""
    lowered = name.lower()
    if lowered in CONTENT_KEYS:
        return True
    return any(marker in lowered for marker in CONTENT_KEY_MARKERS)


def is_content_key(name: str) -> bool:
    """Whether a name is one telemetry never carries, as a field or as a dimension.

    The same predicate governs both, because a delivery backend that turned a
    content-bearing key into a metric dimension would publish the very value the
    field filter exists to drop, and publish it where a log filter cannot reach.
    """
    return _is_content_key(name)


def _filter_field(value: object, depth: int = 0) -> JsonValue | object:
    """Filter one field value, returning the drop sentinel when it must not appear.

    Content keys are dropped at every depth rather than only at the top level,
    because a body nested inside a diagnostic mapping is still a body. A value of
    no JSON type is rendered through its own text conversion, which is what makes
    a wrapped credential appear as its fixed placeholder.
    """
    if depth > _MAX_FIELD_DEPTH:
        return _DROPPED
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        nested: JsonObject = {}
        for key, item in value.items():
            name = str(key)
            if _is_content_key(name):
                continue
            filtered = _filter_field(item, depth + 1)
            if filtered is _DROPPED:
                continue
            nested[name] = _as_json(filtered)
        return nested
    if isinstance(value, (list, tuple)):
        entries: list[JsonValue] = []
        for item in value:
            filtered = _filter_field(item, depth + 1)
            if filtered is _DROPPED:
                continue
            entries.append(_as_json(filtered))
        return entries
    return str(value)


def _as_json(value: JsonValue | object) -> JsonValue:
    """Narrow an already-filtered value to the JSON shape a record is built from."""
    if value is None or isinstance(value, (bool, int, float, str, list, dict)):
        return value
    return str(value)


def _normalise_dimensions(dimensions: Mapping[str, str]) -> Dimensions:
    """Sort dimensions by name so combination identity is order-independent."""
    return tuple(sorted((str(name), str(value)) for name, value in dimensions.items()))


class Telemetry:
    """Buffered in-process counters, a standard-error record sink, and the bound.

    An instance owns its stream, its bound, and its published-combination set, so
    a test drives one directly and production code drives the module-level
    default. Nothing here raises on an emission path: telemetry that failed must
    not fail its caller.
    """

    __slots__ = (
        "_cardinality_max",
        "_counters",
        "_delivery",
        "_disabled",
        "_log_level",
        "_namespace",
        "_overflow",
        "_pending",
        "_stream",
    )

    def __init__(
        self,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        cardinality_max: int = DEFAULT_CARDINALITY_MAX,
        log_level: Severity = Severity.INFO,
        disabled: bool = False,
        stream: TextIO | None = None,
        delivery: MetricDelivery | None = None,
    ) -> None:
        if cardinality_max < 1:
            raise ValueError("the billable metric cardinality maximum must be at least 1")
        self._namespace = namespace
        self._cardinality_max = cardinality_max
        self._log_level = log_level
        self._disabled = disabled
        self._stream = stream
        self._delivery = delivery
        self._counters: dict[Combination, float] = {}
        self._overflow: float = 0.0
        self._pending: deque[MetricSample] = deque(maxlen=_PENDING_SAMPLE_MAX)

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration,
        *,
        stream: TextIO | None = None,
        delivery: MetricDelivery | None = None,
    ) -> Telemetry:
        """Build an instance from the resolved configuration surface."""
        level_text = configuration.text("MOLT_LOG_LEVEL").strip().lower()
        try:
            level = Severity(level_text)
        except ValueError:
            level = Severity.INFO
        return cls(
            namespace=configuration.text("MOLT_METRIC_NAMESPACE"),
            cardinality_max=configuration.integer("MOLT_METRIC_CARDINALITY_MAX"),
            log_level=level,
            disabled=configuration.flag("MOLT_TELEMETRY_DISABLED"),
            stream=stream,
            delivery=delivery,
        )

    # -- properties ------------------------------------------------------

    @property
    def namespace(self) -> str:
        """The namespace published measurements are grouped under."""
        return self._namespace

    @property
    def cardinality_max(self) -> int:
        """The ceiling on distinct billable metric-and-dimension combinations."""
        return self._cardinality_max

    @property
    def log_level(self) -> Severity:
        """The lowest severity written."""
        return self._log_level

    @property
    def disabled(self) -> bool:
        """Whether emission is suppressed entirely, for local tests."""
        return self._disabled

    # -- metrics ---------------------------------------------------------

    def metric(
        self,
        name: str,
        value: float = 1.0,
        /,
        *,
        unit: str = UNIT_COUNT,
        **dimensions: str,
    ) -> None:
        """Record one measurement, diverting it to a log record beyond the bound.

        The name and the value are positional-only so that a dimension may be
        named anything at all, including the words this signature already uses,
        without colliding with a parameter.

        A combination already published is always admitted, because it costs no
        further billable metric. A new combination is admitted only while the
        published count is below the maximum. Everything else is diverted, which
        is what makes the bound hold by construction rather than by discipline.
        """
        if self._disabled:
            return
        dimension_pairs = _normalise_dimensions(dimensions)
        combination: Combination = (name, dimension_pairs)
        if combination in self._counters or len(self._counters) < self._cardinality_max:
            self._counters[combination] = self._counters.get(combination, 0.0) + float(value)
            self._pending.append(
                MetricSample(name=name, value=float(value), unit=unit, dimensions=dimension_pairs)
            )
            return

        self._divert(name, float(value), unit, dimension_pairs)
        self._overflow += 1.0
        self._pending.append(
            MetricSample(name=OVERFLOW_METRIC, value=1.0, unit=UNIT_COUNT, dimensions=())
        )

    def emit_suppressed(self, name: str, value: float, unit: str, dimensions: Dimensions) -> None:
        """Report a measurement a delivery backend suppressed, as this surface does.

        A backend applies the same bound on its own path, and a combination it
        suppresses must reach the same record shape a diversion here reaches,
        including the bypass of the severity threshold: a suppressed measurement is
        its own delivery path rather than a diagnostic, so raising the configured
        level must not make it disappear.
        """
        self._divert(name, float(value), unit, dimensions)

    def _divert(self, name: str, value: float, unit: str, dimensions: Dimensions) -> None:
        """Write a suppressed measurement as a log record carrying the same content.

        The record bypasses the severity threshold because it is the measurement's
        own delivery path rather than a diagnostic: raising the configured level
        must not make a suppressed measurement disappear entirely.
        """
        record: JsonObject = self._fixed_keys(
            Severity.WARNING, "telemetry", "metric diverted to a log record"
        )
        rendered: JsonObject = {}
        for key, item in dimensions:
            rendered[key] = item
        record["metric"] = name
        record["value"] = value
        record["unit"] = unit
        record["dimensions"] = rendered
        record["cardinality_max"] = self._cardinality_max
        self._write(record)

    def counters(self) -> Mapping[Combination, float]:
        """The buffered in-process counters, keyed by published combination.

        These are the combinations the bound governs. The overflow guard is not
        among them; it is reported by `overflow_count`.
        """
        return MappingProxyType(dict(self._counters))

    def combinations(self) -> tuple[Combination, ...]:
        """Every published combination, in the order it was first published.

        The length of this never exceeds the configured maximum.
        """
        return tuple(self._counters)

    def overflow_count(self) -> float:
        """How many measurements have been diverted, as one undimensioned counter."""
        return self._overflow

    def pending_samples(self) -> tuple[MetricSample, ...]:
        """The samples awaiting delivery."""
        return tuple(self._pending)

    def set_delivery(self, delivery: MetricDelivery | None) -> None:
        """Attach or detach the delivery backend."""
        self._delivery = delivery

    def flush(self) -> None:
        """Hand buffered samples to the delivery backend, if one is attached.

        With no backend attached the buffer is cleared and the counters remain the
        in-process record. A backend that raises is swallowed and reported as a log
        record, because a telemetry failure must not fail its caller.
        """
        samples = tuple(self._pending)
        self._pending.clear()
        if self._delivery is None or not samples:
            return
        try:
            self._delivery.deliver(self._namespace, samples)
        except Exception as error:
            self.log(
                Severity.WARNING,
                "telemetry",
                "metric delivery failed",
                error_type=type(error).__name__,
                sample_count=len(samples),
            )

    # -- log records -----------------------------------------------------

    def log(
        self,
        severity: Severity,
        component: str,
        message: str,
        /,
        **fields: object,
    ) -> None:
        """Write one single-line JSON record, with content-bearing fields dropped.

        The three leading parameters are positional-only so that a caller may pass
        a field of any name, including one of the fixed keys, without colliding
        with a parameter. A field named after a fixed key is then dropped rather
        than overwriting it, so the four fixed keys always mean what they say.
        """
        if self._disabled:
            return
        level = Severity(severity)
        if _SEVERITY_RANK[level] < _SEVERITY_RANK[self._log_level]:
            return
        record = self._fixed_keys(level, component, message)
        for name, value in fields.items():
            if name in LOG_KEY_ORDER or _is_content_key(name):
                continue
            filtered = _filter_field(value)
            if filtered is _DROPPED:
                continue
            record[name] = _as_json(filtered)
        self._write(record)

    def _fixed_keys(self, severity: Severity, component: str, message: str) -> JsonObject:
        """The four always-present keys, written first and never overwritten."""
        return {
            "severity": str(severity),
            "component": component,
            "message": message,
            "correlation_id": current_correlation(),
        }

    def _write(self, record: JsonObject) -> None:
        """Serialise one record onto the sink as a single line.

        The stream is resolved at write time rather than captured at construction,
        so a redirected standard error is honoured.
        """
        stream = sys.stderr if self._stream is None else self._stream
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        try:
            stream.write(line + "\n")
            stream.flush()
        except (OSError, ValueError):
            # A closed or unwritable sink is not a reason to fail a caller.
            return


_default: Telemetry | None = None


def current() -> Telemetry:
    """The process-wide instance, built with the defaults on first use."""
    global _default
    if _default is None:
        _default = Telemetry()
    return _default


def configure(
    configuration: Configuration,
    *,
    stream: TextIO | None = None,
    delivery: MetricDelivery | None = None,
) -> Telemetry:
    """Replace the process-wide instance with one built from the configuration."""
    global _default
    _default = Telemetry.from_configuration(configuration, stream=stream, delivery=delivery)
    return _default


def reset() -> None:
    """Discard the process-wide instance, so the next use builds a fresh one."""
    global _default
    _default = None


def metric(name: str, value: float = 1.0, /, *, unit: str = UNIT_COUNT, **dimensions: str) -> None:
    """Record one measurement on the process-wide instance."""
    current().metric(name, value, unit=unit, **dimensions)


def log(severity: Severity, component: str, message: str, /, **fields: object) -> None:
    """Write one structured record on the process-wide instance."""
    current().log(severity, component, message, **fields)
