"""Unit tests for the delivery backend's configured bounds and its dimension filter.

This is the complement of the delivery suite beside it rather than a second copy of
it. That suite asserts the batching arithmetic, the cardinality bound, the two
failure shapes, and the inventory; what it leaves unasserted is the arithmetic of
the *configuration* those behaviours are parameterised by, and one half of the
dimension filter. Both are here.

**A configured batch size is clamped rather than sent and refused.** The metrics
service accepts a fixed maximum number of data items in one put, so a deployment
that configures more than that must have the ceiling applied for it: sending the
configured number and letting the service refuse the whole batch would lose every
datum in it. A configured size below one is raised to one for the same reason in
reverse -- a batch of nothing is not a delivery.

**Every bound below one is refused at construction.** The delivery suite asserts
that for the batch size. The cardinality maximum and the delivery cadence carry the
same refusal and neither was asserted, so a regression that admitted a zero bound
would have gone unnoticed on two of the three.

**The dimension filter is asserted on its marker half.** The delivery suite
parametrises over the exact content-key set and names one marker case. The predicate
also drops any name whose spelling merely carries a marker, and a dimension is
published where no log filter can reach it, so every marker is exercised here.

**The overflow counter is only passed through undimensioned.** The delivery path
lets an overflow datum bypass the bound precisely because it carries no dimensions,
which is what makes it one fixed addition rather than something that grows with
load. An overflow-named sample arriving *with* dimensions is therefore not the guard
and must be measured against the bound like anything else. That branch was
unasserted.

**The pending buffer is bounded.** With no backend attached the buffer must not grow
without limit, and the counters remain the durable in-process record. The bound is
asserted as a bound rather than as a number, so the assertion says what the design
promises rather than restating a constant.

Nothing here reaches a network, a credential, or a clock.
"""

from __future__ import annotations

import io
from typing import Final

import pytest

from molt.config.resolve import Configuration
from molt.models.event import JsonValue
from molt.telemetry import (
    CONTENT_KEY_MARKERS,
    OVERFLOW_METRIC,
    UNIT_COUNT,
    MetricSample,
    Severity,
    Telemetry,
)
from molt.telemetry.delivery import (
    BATCH_SIZE_ENV,
    DEFAULT_BATCH_SIZE,
    DELIVERY_INTERVAL_ENV,
    MAX_BATCH_SIZE,
    CloudWatchDelivery,
)

# A namespace obviously not the default, so an assertion about a configured
# namespace cannot pass by accident.
NAMESPACE: Final[str] = "MoltUnitBounds"

# A declared metric name and the dimension the design attaches to everything.
METRIC_NAME: Final[str] = "collector.events_accepted"
DIMENSION_NAME: Final[str] = "component"

# A value obviously not a measurement body, used wherever a content-bearing
# dimension must be shown to have been dropped rather than merely renamed.
MARKER_TEXT: Final[str] = "dropped-if-seen"

# A configured batch size well past the service ceiling, and one below any batch.
OVERSIZED_BATCH: Final[int] = MAX_BATCH_SIZE * 4
UNDERSIZED_BATCH: Final[int] = 0

# A configured batch size and cadence the surface can hold, chosen so neither
# coincides with the documented default.
CONFIGURED_BATCH: Final[int] = 7
CONFIGURED_INTERVAL_SECONDS: Final[int] = 15

# More samples than any buffer here is expected to hold, so the buffer's own bound
# is what stops the count rather than the number emitted.
FLOODED_SAMPLE_COUNT: Final[int] = 4096


class RecordingClient:
    """A metrics client that records every put and reaches no network."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, list[dict[str, object]]]] = []

    def put_metric_data(self, **request: object) -> object:
        """Record one put and answer it."""
        namespace = request["Namespace"]
        data = request["MetricData"]
        assert isinstance(namespace, str)
        assert isinstance(data, list)
        self.puts.append((namespace, data))
        return {}


def _surface(**values: int) -> Configuration:
    """A configuration surface holding a namespace, a bound, and whatever else is given."""
    file_values: dict[str, JsonValue] = {
        "telemetry.namespace": NAMESPACE,
        "telemetry.metric_cardinality_max": 4,
    }
    keys = {
        BATCH_SIZE_ENV: "telemetry.metric_batch_size",
        DELIVERY_INTERVAL_ENV: "telemetry.metric_delivery_interval_seconds",
    }
    for env, key in keys.items():
        if env in values:
            file_values[key] = values[env]
    return Configuration(environ={}, file_values=file_values)


# ---------------------------------------------------------------------------
# The configured batch size, clamped at both ends
# ---------------------------------------------------------------------------


def test_a_configured_batch_size_above_the_service_ceiling_is_clamped_to_it() -> None:
    """The ceiling is applied for the deployment rather than by the service.

    A put carrying more data items than the service accepts is refused whole, so a
    configured size past the ceiling would lose every datum in the batch instead of
    delivering the ceiling's worth.
    """
    delivery = CloudWatchDelivery.from_configuration(
        _surface(**{BATCH_SIZE_ENV: OVERSIZED_BATCH}),
        client_factory=RecordingClient,
    )
    assert delivery.batch_size == MAX_BATCH_SIZE
    assert delivery.batch_size < OVERSIZED_BATCH


def test_a_configured_batch_size_below_one_is_raised_to_one() -> None:
    """A batch of nothing is not a delivery, so the floor is one rather than a refusal.

    The floor applies here and the refusal applies at construction, which is not an
    inconsistency: a value read from a surface is corrected because a deployment
    should not fail to start over it, and a value passed at a call site is refused
    because a caller wrote it deliberately.
    """
    delivery = CloudWatchDelivery.from_configuration(
        _surface(**{BATCH_SIZE_ENV: UNDERSIZED_BATCH}),
        client_factory=RecordingClient,
    )
    assert delivery.batch_size == 1


def test_the_configured_batch_size_and_cadence_are_read_from_the_surface() -> None:
    """Where the surface declares both keys, both are read rather than defaulted."""
    delivery = CloudWatchDelivery.from_configuration(
        _surface(
            **{
                BATCH_SIZE_ENV: CONFIGURED_BATCH,
                DELIVERY_INTERVAL_ENV: CONFIGURED_INTERVAL_SECONDS,
            }
        ),
        client_factory=RecordingClient,
    )
    assert delivery.batch_size == CONFIGURED_BATCH
    assert delivery.batch_size != DEFAULT_BATCH_SIZE
    assert delivery.delivery_interval_seconds == CONFIGURED_INTERVAL_SECONDS
    assert delivery.namespace == NAMESPACE


# ---------------------------------------------------------------------------
# Every bound below one is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("maximum", [0, -1])
def test_a_cardinality_maximum_below_one_is_refused(maximum: int) -> None:
    with pytest.raises(ValueError, match="cardinality"):
        CloudWatchDelivery(cardinality_max=maximum)


@pytest.mark.parametrize("seconds", [0, -1])
def test_a_delivery_interval_below_one_second_is_refused(seconds: int) -> None:
    with pytest.raises(ValueError, match="interval"):
        CloudWatchDelivery(delivery_interval_seconds=seconds)


# ---------------------------------------------------------------------------
# The dimension filter, on its marker half
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", CONTENT_KEY_MARKERS)
def test_a_dimension_named_only_by_a_marker_is_dropped_before_the_put(marker: str) -> None:
    """A name the exact set omits is dropped on its marker, at the dimension seam.

    Dropping a harmless dimension is a cost worth paying: a dimension is published
    where no log filter can reach it afterwards, so the predicate is deliberately
    wider than the exact set.
    """
    name = f"outer{marker}inner".replace("-", "_")
    client = RecordingClient()
    delivery = CloudWatchDelivery(
        namespace=NAMESPACE, cardinality_max=4, client_factory=lambda: client
    )

    delivery.deliver(
        NAMESPACE,
        (
            MetricSample(
                name=METRIC_NAME,
                value=1.0,
                unit=UNIT_COUNT,
                dimensions=((DIMENSION_NAME, "collector"), (name, MARKER_TEXT)),
            ),
        ),
    )

    _namespace, data = client.puts[0]
    assert data[0]["Dimensions"] == [{"Name": DIMENSION_NAME, "Value": "collector"}]
    assert delivery.combinations() == ((METRIC_NAME, ((DIMENSION_NAME, "collector"),)),)


# ---------------------------------------------------------------------------
# The overflow guard is only a guard while it stays undimensioned
# ---------------------------------------------------------------------------


def test_an_overflow_sample_arriving_dimensioned_is_measured_against_the_bound() -> None:
    """A dimensioned overflow datum is not the guard, so it does not bypass the bound.

    The guard bypasses the bound precisely because it carries no dimensions, which is
    what makes it one fixed addition that cannot grow with load. A sample arriving
    under the same name with dimensions attached is something else and is counted like
    anything else.
    """
    client = RecordingClient()
    delivery = CloudWatchDelivery(
        namespace=NAMESPACE, batch_size=50, cardinality_max=1, client_factory=lambda: client
    )

    delivery.deliver(
        NAMESPACE,
        (
            MetricSample(
                name=OVERFLOW_METRIC,
                value=1.0,
                unit=UNIT_COUNT,
                dimensions=((DIMENSION_NAME, "attached"),),
            ),
            MetricSample(
                name=OVERFLOW_METRIC,
                value=1.0,
                unit=UNIT_COUNT,
                dimensions=((DIMENSION_NAME, "second"),),
            ),
        ),
    )

    # The first dimensioned sample spent the bound; the second was diverted.
    assert delivery.combinations() == ((OVERFLOW_METRIC, ((DIMENSION_NAME, "attached"),)),)
    assert delivery.overflow_count() == 1


def test_an_undimensioned_overflow_sample_bypasses_the_bound_entirely() -> None:
    """The guard itself is published however saturated the bound already is."""
    client = RecordingClient()
    delivery = CloudWatchDelivery(
        namespace=NAMESPACE, batch_size=50, cardinality_max=1, client_factory=lambda: client
    )

    delivery.deliver(
        NAMESPACE,
        (
            MetricSample(name=METRIC_NAME, value=1.0, unit=UNIT_COUNT, dimensions=()),
            MetricSample(name=OVERFLOW_METRIC, value=1.0, unit=UNIT_COUNT, dimensions=()),
        ),
    )

    assert delivery.combinations() == ((METRIC_NAME, ()),)
    assert delivery.overflow_count() == 0
    published = [datum["MetricName"] for _namespace, data in client.puts for datum in data]
    assert published == [METRIC_NAME, OVERFLOW_METRIC]


# ---------------------------------------------------------------------------
# The pending buffer is bounded
# ---------------------------------------------------------------------------


def test_the_pending_buffer_is_bounded_with_no_backend_attached() -> None:
    """The buffer discards the oldest rather than growing without limit.

    With no backend attached the counters remain the durable in-process record, which
    is why discarding a buffered sample loses nothing that is not still counted.
    """
    sink = io.StringIO()
    telemetry = Telemetry(cardinality_max=1, log_level=Severity.DEBUG, stream=sink)

    for _ in range(FLOODED_SAMPLE_COUNT):
        telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "one"})

    pending = telemetry.pending_samples()
    assert 0 < len(pending) < FLOODED_SAMPLE_COUNT
    assert telemetry.counters()[(METRIC_NAME, ((DIMENSION_NAME, "one"),))] == float(
        FLOODED_SAMPLE_COUNT
    )


def test_a_flush_with_a_backend_attached_and_nothing_pending_issues_no_put() -> None:
    """An empty flush costs no call, so a quiet interval costs no request."""
    client = RecordingClient()
    delivery = CloudWatchDelivery(namespace=NAMESPACE, client_factory=lambda: client)
    telemetry = Telemetry(namespace=NAMESPACE, delivery=delivery)

    telemetry.flush()
    telemetry.flush()

    assert client.puts == []
    assert delivery.put_count() == 0
    assert delivery.failure_count() == 0
