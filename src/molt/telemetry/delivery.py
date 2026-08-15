"""Batched metric delivery to the cloud metrics service, behind an injected client.

This sits *beneath* the telemetry surface rather than beside it. The surface holds
the buffered counters, the cardinality bound, the field filter, and the
standard-error records; this module takes the samples that surface hands it on
flush and publishes them, in batches of the configured size, under the configured
namespace.

**The client is a seam, never a construction.** The one call made on it is
`put_metric_data`, declared as a protocol here, and the default factory imports
the AWS library lazily inside the call. So this module imports with no credential
present, and a unit suite drives delivery through a recording double that reaches
no network at all.

**The cardinality bound is applied again on the delivery path.** The surface
bounds what it counts, but a backend can be handed samples from anywhere -- a
replayed buffer, a component that built samples directly -- and a bound that held
only at one of the two points would not be a bound. A combination beyond the
maximum is not published: it becomes a structured log record carrying the same
name, the same value, and the same dimensions, and the undimensioned overflow
counter is incremented and published outside the bound, for the reason the surface
documents.

**A content-bearing key never becomes a dimension.** Dimension names run through
the surface's own content predicate before a datum is built, because a dimension
is published where no log filter can reach it afterwards.

**A failed put loses telemetry and nothing else.** The exception is caught,
counted per exception type, and reported as a log record; the remaining batches are
still attempted, and the caller is never raised at. Telemetry is on the critical
path of no write.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from typing import Final, Protocol, cast

from molt.config.resolve import Configuration, MissingConfigError, UnknownSettingError
from molt.telemetry import (
    DEFAULT_CARDINALITY_MAX,
    DEFAULT_NAMESPACE,
    OVERFLOW_METRIC,
    UNIT_COUNT,
    Combination,
    Dimensions,
    MetricSample,
    Severity,
    Telemetry,
    is_content_key,
    log,
)
from molt.telemetry.inventory import UNIT_NONE, declared_unit, undeclared_names

__all__ = [
    "BATCH_SIZE_ENV",
    "COMPONENT",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_DELIVERY_INTERVAL_SECONDS",
    "DELIVERY_INTERVAL_ENV",
    "SERVICE_NAME",
    "SUPPORTED_UNITS",
    "CloudWatchClient",
    "CloudWatchDelivery",
    "cloudwatch_client",
]

# What this module calls itself in a log record.
COMPONENT: Final[str] = "telemetry"

# The metrics service the default factory builds a client for.
SERVICE_NAME: Final[str] = "cloudwatch"

# The service accepts at most this many data items in one put, so a configured
# batch size above it is clamped rather than being sent and refused.
MAX_BATCH_SIZE: Final[int] = 1000

# The configuration keys the batch size and the delivery cadence are read from,
# and the values used when the resolved surface declares neither key.
BATCH_SIZE_ENV: Final[str] = "MOLT_METRIC_BATCH_SIZE"
DELIVERY_INTERVAL_ENV: Final[str] = "MOLT_METRIC_DELIVERY_INTERVAL_SECONDS"
DEFAULT_BATCH_SIZE: Final[int] = 20
DEFAULT_DELIVERY_INTERVAL_SECONDS: Final[int] = 60

# The units a datum may carry. An inventory unit is one of these by construction;
# a unit from anywhere else is coerced to the no-unit token, because an unknown
# unit would otherwise make the service refuse the whole batch it travelled in.
SUPPORTED_UNITS: Final[frozenset[str]] = frozenset(
    {
        "Count",
        "Count/Second",
        "Milliseconds",
        "Microseconds",
        "Seconds",
        "Bytes",
        "Kilobytes",
        "Megabytes",
        "Gigabytes",
        "Percent",
        UNIT_NONE,
    }
)

# The keys one datum and one dimension are built under, fixed by the service.
_DATUM_NAME: Final[str] = "MetricName"
_DATUM_VALUE: Final[str] = "Value"
_DATUM_UNIT: Final[str] = "Unit"
_DATUM_DIMENSIONS: Final[str] = "Dimensions"
_DIMENSION_NAME: Final[str] = "Name"
_DIMENSION_VALUE: Final[str] = "Value"

# The request keys the put is made under, likewise fixed by the service.
_REQUEST_NAMESPACE: Final[str] = "Namespace"
_REQUEST_DATA: Final[str] = "MetricData"

# One datum, as the service receives it.
Datum = dict[str, object]


class CloudWatchClient(Protocol):
    """The one call this module makes on a metrics client.

    Declared as a protocol so the delivery path depends on a shape rather than on
    a library, which is what lets a test supply a recording double and what keeps
    the AWS import inside the default factory.
    """

    def put_metric_data(self, **request: object) -> object:
        """Publish a batch of metric data under a namespace."""


def cloudwatch_client() -> CloudWatchClient:
    """Build a real metrics client, importing the AWS library at call time.

    The import is here rather than at module scope on purpose: the module must be
    importable, and the unit suite runnable, with no credential and no AWS library
    resolution at import time. Region and credentials resolve through the
    library's own chain, so nothing about them is restated here.
    """
    module = import_module("boto3")
    return cast(CloudWatchClient, module.client(SERVICE_NAME))


def _configured_integer(configuration: Configuration, env: str, fallback: int) -> int:
    """Read a whole number from the configuration surface, falling back to a default.

    The batch size and the delivery cadence are read through the same resolved
    view as the namespace and the bound. A surface that declares neither key yet
    resolves to the documented default rather than to a failure, so delivery is
    configurable the moment the key is declared and is never blocked on it.
    """
    try:
        return configuration.integer(env)
    except (UnknownSettingError, MissingConfigError):
        return fallback


def _safe_dimensions(dimensions: Dimensions) -> Dimensions:
    """Drop any dimension whose name is one telemetry never carries.

    A dimension is published where the log field filter cannot reach it, so the
    same predicate that drops a content-bearing log field drops a content-bearing
    dimension name here, before the combination is even counted.
    """
    return tuple((name, value) for name, value in dimensions if not is_content_key(name))


def _unit(sample: MetricSample) -> str:
    """The unit a datum carries: the sample's, if the service accepts it."""
    declared = declared_unit(sample.name)
    if declared is not None and sample.unit not in SUPPORTED_UNITS:
        return declared
    return sample.unit if sample.unit in SUPPORTED_UNITS else UNIT_NONE


def _batches(data: Sequence[Datum], size: int) -> Iterator[tuple[Datum, ...]]:
    """Split data into consecutive batches of at most the configured size."""
    for start in range(0, len(data), size):
        yield tuple(data[start : start + size])


@dataclass(frozen=True, slots=True, eq=False)
class CloudWatchDelivery:
    """Buffered samples, bounded and batched, published under one namespace.

    An instance owns the set of combinations it has published, so the bound holds
    across every flush of the process rather than within one, and it owns its
    failure tally, so a delivery outage is a counted and reported condition rather
    than an exception a caller has to handle.
    """

    namespace: str = DEFAULT_NAMESPACE
    batch_size: int = DEFAULT_BATCH_SIZE
    cardinality_max: int = DEFAULT_CARDINALITY_MAX
    delivery_interval_seconds: int = DEFAULT_DELIVERY_INTERVAL_SECONDS
    client_factory: Callable[[], CloudWatchClient] = cloudwatch_client
    telemetry: Telemetry | None = None
    _published: dict[Combination, None] = field(default_factory=dict)
    _failures: dict[str, int] = field(default_factory=dict)
    _client: list[CloudWatchClient] = field(default_factory=list)
    _overflow: list[None] = field(default_factory=list)
    _put_count: list[None] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError("the metric delivery batch size must be at least 1")
        if self.cardinality_max < 1:
            raise ValueError("the billable metric cardinality maximum must be at least 1")
        if self.delivery_interval_seconds < 1:
            raise ValueError("the metric delivery interval must be at least one second")

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration,
        *,
        client_factory: Callable[[], CloudWatchClient] | None = None,
        telemetry: Telemetry | None = None,
    ) -> CloudWatchDelivery:
        """Build an instance from the resolved configuration surface.

        Nothing about the namespace, the batch size, the bound, or the cadence is
        stated at a call site: all four are read here, from the same view the rest
        of the process resolves against.
        """
        size = min(
            _configured_integer(configuration, BATCH_SIZE_ENV, DEFAULT_BATCH_SIZE), MAX_BATCH_SIZE
        )
        return cls(
            namespace=configuration.text("MOLT_METRIC_NAMESPACE"),
            batch_size=max(size, 1),
            cardinality_max=configuration.integer("MOLT_METRIC_CARDINALITY_MAX"),
            delivery_interval_seconds=_configured_integer(
                configuration, DELIVERY_INTERVAL_ENV, DEFAULT_DELIVERY_INTERVAL_SECONDS
            ),
            client_factory=cloudwatch_client if client_factory is None else client_factory,
            telemetry=telemetry,
        )

    # -- reporting -------------------------------------------------------

    def combinations(self) -> tuple[Combination, ...]:
        """Every combination published, in the order it was first published.

        The length of this never exceeds the configured maximum. The overflow
        counter is not among them, for the reason the surface documents: it is one
        fixed undimensioned addition that sits outside the bound it protects.
        """
        return tuple(self._published)

    def overflow_count(self) -> int:
        """How many samples were suppressed before delivery."""
        return len(self._overflow)

    def failure_count(self) -> int:
        """How many puts failed, across every exception type."""
        return sum(self._failures.values())

    def failures(self) -> Mapping[str, int]:
        """The failed puts, tallied by exception type."""
        return dict(self._failures)

    def put_count(self) -> int:
        """How many puts were issued, which is how many batches were delivered."""
        return len(self._put_count)

    # -- delivery --------------------------------------------------------

    def deliver(self, namespace: str, samples: Sequence[MetricSample]) -> None:
        """Publish samples under a namespace, in batches of the configured size.

        This is the whole of the coupling to the telemetry surface: it is the
        method the surface's flush calls, and it raises at no caller for any
        reason, because a caller's work must not fail because telemetry did.
        """
        if not samples:
            return
        target = namespace or self.namespace
        data = self._admit(samples)
        self._report_undeclared(samples)
        if not data:
            return
        client = self._resolve_client(target, len(data))
        if client is None:
            return
        for batch in _batches(data, self.batch_size):
            self._put(client, target, batch)

    def _admit(self, samples: Sequence[MetricSample]) -> list[Datum]:
        """Apply the cardinality bound, returning the data that may be published.

        A combination already published is readmitted, because it costs no further
        billable metric. A new one is admitted only while the published count is
        below the maximum. Everything else becomes a log record carrying the same
        name, value, and dimensions, plus one undimensioned overflow datum, which
        is published outside the bound.
        """
        data: list[Datum] = []
        for sample in samples:
            dimensions = _safe_dimensions(sample.dimensions)
            if sample.name == OVERFLOW_METRIC and not dimensions:
                data.append(self._datum(sample, ()))
                continue
            combination: Combination = (sample.name, dimensions)
            if combination in self._published or len(self._published) < self.cardinality_max:
                self._published.setdefault(combination, None)
                data.append(self._datum(sample, dimensions))
                continue
            self._suppress(sample, dimensions)
            self._overflow.append(None)
            data.append(
                self._datum(
                    MetricSample(name=OVERFLOW_METRIC, value=1.0, unit=UNIT_COUNT, dimensions=()),
                    (),
                )
            )
        return data

    def _datum(self, sample: MetricSample, dimensions: Dimensions) -> Datum:
        """Build one datum, with the dimensions already filtered and bounded."""
        return {
            _DATUM_NAME: sample.name,
            _DATUM_VALUE: float(sample.value),
            _DATUM_UNIT: _unit(sample),
            _DATUM_DIMENSIONS: [
                {_DIMENSION_NAME: name, _DIMENSION_VALUE: value} for name, value in dimensions
            ],
        }

    def _suppress(self, sample: MetricSample, dimensions: Dimensions) -> None:
        """Report a suppressed combination as a record carrying the same content.

        The record goes through the surface's own suppression report when an
        instance is attached, so a suppression is written whatever the configured
        severity: the record *is* the measurement's delivery, not a diagnostic
        about it.
        """
        if self.telemetry is not None:
            self.telemetry.emit_suppressed(
                sample.name, float(sample.value), _unit(sample), dimensions
            )
            return
        self._log(
            "metric suppressed before delivery",
            metric=sample.name,
            value=float(sample.value),
            unit=_unit(sample),
            dimensions=dict(dimensions),
            cardinality_max=self.cardinality_max,
            namespace=self.namespace,
        )

    def _report_undeclared(self, samples: Sequence[MetricSample]) -> None:
        """Report any name the inventory does not declare, once per batch.

        The sample is still published. A name the inventory omits means the
        inventory and the emitting component disagree, and a disagreement that is
        reported can be reconciled, whereas a measurement quietly dropped here
        would leave the deployment alarming on a metric nothing publishes.
        """
        undeclared = undeclared_names(sample.name for sample in samples)
        if undeclared:
            self._log(
                "metric name is not in the inventory",
                metric_names=list(undeclared),
                namespace=self.namespace,
            )

    def _resolve_client(self, namespace: str, sample_count: int) -> CloudWatchClient | None:
        """The cached client, built on first use, or None when building it failed.

        A factory that raises -- no library, no credential, no region -- is the
        same class of condition as a failed put: counted, reported, and survived.
        """
        if self._client:
            return self._client[0]
        try:
            client = self.client_factory()
        except Exception as error:
            self._count(error)
            self._log(
                "metric delivery client unavailable",
                error_type=type(error).__name__,
                namespace=namespace,
                sample_count=sample_count,
            )
            return None
        self._client.append(client)
        return client

    def _put(self, client: CloudWatchClient, namespace: str, batch: Sequence[Datum]) -> None:
        """Issue one put, counting and reporting a failure rather than raising it."""
        try:
            client.put_metric_data(**{_REQUEST_NAMESPACE: namespace, _REQUEST_DATA: list(batch)})
        except Exception as error:
            self._count(error)
            self._log(
                "metric delivery failed",
                error_type=type(error).__name__,
                namespace=namespace,
                batch_size=len(batch),
            )
            return
        self._put_count.append(None)

    def _count(self, error: Exception) -> None:
        """Tally one failure under the exception type that caused it."""
        name = type(error).__name__
        self._failures[name] = self._failures.get(name, 0) + 1

    def _log(self, message: str, **fields: object) -> None:
        """Write one record, through the attached instance or the module surface."""
        if self.telemetry is None:
            log(Severity.WARNING, COMPONENT, message, **fields)
            return
        self.telemetry.log(Severity.WARNING, COMPONENT, message, **fields)
