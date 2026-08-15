"""Unit tests for batched metric delivery and for the metric inventory.

Delivery is driven entirely through a recording client double, so no assertion
here needs a credential, a region, or a network. What the double records is the
whole contract: the namespace each put carried, and the data items each put held.

The bound is asserted on the delivery path specifically. The surface bounds what
it counts, and that is asserted elsewhere; what is asserted here is that a backend
handed samples from anywhere -- a replayed buffer, a component that built samples
directly -- publishes no more distinct combinations than the configured maximum,
diverts the rest to a record carrying the same name, value, and dimensions, and
publishes the undimensioned overflow counter outside the bound.

Two failure shapes are checked because they fail at different moments: a client
factory that cannot build a client at all, and a client whose put raises. Both are
counted and reported, neither is raised at a caller, and in both cases the
remaining work is unaffected.
"""

from __future__ import annotations

import io
import json
from typing import Final

import pytest

from molt.config.resolve import Configuration
from molt.telemetry import (
    CONTENT_KEYS,
    OVERFLOW_METRIC,
    UNIT_COUNT,
    MetricSample,
    Severity,
    Telemetry,
)
from molt.telemetry.delivery import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_DELIVERY_INTERVAL_SECONDS,
    SUPPORTED_UNITS,
    CloudWatchDelivery,
)
from molt.telemetry.inventory import (
    METRIC_INVENTORY,
    METRIC_NAMES,
    UNIT_NONE,
    declared_unit,
    undeclared_names,
)

# A namespace obviously not the default, so an assertion about the namespace an
# instance was configured with cannot pass by accident.
NAMESPACE: Final[str] = "MoltUnit"

# Two declared metric names and the dimension the design attaches to everything.
METRIC_NAME: Final[str] = "collector.events_accepted"
LATENCY_NAME: Final[str] = "recall.latency_ms"
DIMENSION_NAME: Final[str] = "component"

# A value obviously not a measurement body, used wherever a content-bearing
# dimension must be shown to have been dropped rather than merely renamed.
MARKER_TEXT: Final[str] = "dropped-if-seen"


class RecordingClient:
    """A metrics client that records every put and reaches no network."""

    def __init__(self, *, failures: int = 0) -> None:
        self.puts: list[tuple[str, list[dict[str, object]]]] = []
        self._failures = failures

    def put_metric_data(self, **request: object) -> object:
        namespace = request["Namespace"]
        data = request["MetricData"]
        assert isinstance(namespace, str)
        assert isinstance(data, list)
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("the metrics service refused the batch")
        self.puts.append((namespace, data))
        return {}


def _delivery(
    client: RecordingClient,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cardinality_max: int = 10,
    telemetry: Telemetry | None = None,
) -> CloudWatchDelivery:
    """A delivery backend wired to a recording client and nothing else."""
    return CloudWatchDelivery(
        namespace=NAMESPACE,
        batch_size=batch_size,
        cardinality_max=cardinality_max,
        client_factory=lambda: client,
        telemetry=telemetry,
    )


def _samples(count: int, *, name: str = METRIC_NAME) -> tuple[MetricSample, ...]:
    """Samples that are all one combination, so the bound is not what is measured."""
    return tuple(
        MetricSample(name=name, value=float(index), unit=UNIT_COUNT, dimensions=())
        for index in range(count)
    )


def _distinct_samples(count: int, *, name: str = METRIC_NAME) -> tuple[MetricSample, ...]:
    """Samples that are each a distinct combination, so the bound is what bites."""
    return tuple(
        MetricSample(
            name=name,
            value=1.0,
            unit=UNIT_COUNT,
            dimensions=((DIMENSION_NAME, f"c-{index}"),),
        )
        for index in range(count)
    )


def _records(sink: io.StringIO) -> list[dict[str, object]]:
    """Parse every record written to a sink back into a mapping."""
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def _names(data: list[dict[str, object]]) -> list[object]:
    """The metric name of every datum in one put."""
    return [datum["MetricName"] for datum in data]


# ---------------------------------------------------------------------------
# Batching and the namespace
# ---------------------------------------------------------------------------


def test_delivery_batches_at_the_configured_size() -> None:
    client = RecordingClient()
    delivery = _delivery(client, batch_size=3)
    delivery.deliver(NAMESPACE, _samples(7))
    assert [len(data) for _namespace, data in client.puts] == [3, 3, 1]
    assert delivery.put_count() == 3


@pytest.mark.parametrize("batch_size", [1, 2, 5, 20])
def test_no_put_ever_exceeds_the_configured_batch_size(batch_size: int) -> None:
    client = RecordingClient()
    _delivery(client, batch_size=batch_size).deliver(NAMESPACE, _samples(23))
    assert client.puts
    assert all(len(data) <= batch_size for _namespace, data in client.puts)
    assert sum(len(data) for _namespace, data in client.puts) == 23


def test_every_sample_reaches_exactly_one_batch() -> None:
    client = RecordingClient()
    _delivery(client, batch_size=4).deliver(NAMESPACE, _samples(10))
    delivered = [name for _namespace, data in client.puts for name in _names(data)]
    assert delivered == [METRIC_NAME] * 10


def test_the_configured_namespace_is_applied_when_the_caller_names_none() -> None:
    client = RecordingClient()
    _delivery(client).deliver("", _samples(2))
    assert [namespace for namespace, _data in client.puts] == [NAMESPACE]


def test_every_batch_carries_the_same_namespace() -> None:
    client = RecordingClient()
    _delivery(client, batch_size=1).deliver(NAMESPACE, _samples(3))
    assert {namespace for namespace, _data in client.puts} == {NAMESPACE}


def test_no_put_is_issued_for_an_empty_batch_of_samples() -> None:
    client = RecordingClient()
    delivery = _delivery(client)
    delivery.deliver(NAMESPACE, ())
    assert client.puts == []
    assert delivery.put_count() == 0


def test_a_batch_size_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="batch size"):
        CloudWatchDelivery(batch_size=0)


def test_a_datum_carries_the_value_the_unit_and_the_dimensions_of_its_sample() -> None:
    client = RecordingClient()
    _delivery(client).deliver(
        NAMESPACE,
        (
            MetricSample(
                name=LATENCY_NAME,
                value=12.5,
                unit="Milliseconds",
                dimensions=((DIMENSION_NAME, "recall_engine"),),
            ),
        ),
    )
    _namespace, data = client.puts[0]
    assert data[0]["MetricName"] == LATENCY_NAME
    assert data[0]["Value"] == pytest.approx(12.5)
    assert data[0]["Unit"] == "Milliseconds"
    assert data[0]["Dimensions"] == [{"Name": DIMENSION_NAME, "Value": "recall_engine"}]


def test_a_unit_the_service_does_not_accept_becomes_the_declared_or_no_unit_token() -> None:
    client = RecordingClient()
    _delivery(client).deliver(
        NAMESPACE,
        (
            MetricSample(name=METRIC_NAME, value=1.0, unit="Furlongs", dimensions=()),
            MetricSample(name="not.declared", value=1.0, unit="Furlongs", dimensions=()),
        ),
    )
    _namespace, data = client.puts[0]
    assert data[0]["Unit"] == declared_unit(METRIC_NAME)
    assert data[1]["Unit"] == UNIT_NONE
    assert UNIT_NONE in SUPPORTED_UNITS


# ---------------------------------------------------------------------------
# A delivery failure is counted and swallowed
# ---------------------------------------------------------------------------


def test_a_failed_put_is_counted_and_reported_rather_than_raised() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.DEBUG, stream=sink)
    client = RecordingClient(failures=1)
    delivery = _delivery(client, batch_size=2, telemetry=telemetry)

    delivery.deliver(NAMESPACE, _samples(2))

    assert delivery.failure_count() == 1
    assert delivery.failures() == {"RuntimeError": 1}
    assert delivery.put_count() == 0
    record = _records(sink)[0]
    assert record["message"] == "metric delivery failed"
    assert record["error_type"] == "RuntimeError"
    assert record["namespace"] == NAMESPACE
    assert record["severity"] == str(Severity.WARNING)


def test_a_failed_batch_does_not_stop_the_batches_after_it() -> None:
    client = RecordingClient(failures=1)
    delivery = _delivery(client, batch_size=2)
    delivery.deliver(NAMESPACE, _samples(6))
    assert delivery.failure_count() == 1
    assert delivery.put_count() == 2
    assert [len(data) for _namespace, data in client.puts] == [2, 2]


def test_a_client_that_cannot_be_built_is_counted_and_reported() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.DEBUG, stream=sink)

    def _refuse() -> RecordingClient:
        raise RuntimeError("no credential is available")

    delivery = CloudWatchDelivery(namespace=NAMESPACE, client_factory=_refuse, telemetry=telemetry)
    delivery.deliver(NAMESPACE, _samples(2))

    assert delivery.failure_count() == 1
    assert delivery.put_count() == 0
    assert _records(sink)[0]["message"] == "metric delivery client unavailable"


def test_a_failure_through_the_surface_flush_reaches_no_caller() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(cardinality_max=4, log_level=Severity.DEBUG, stream=sink)
    delivery = _delivery(RecordingClient(failures=1), batch_size=2, telemetry=telemetry)
    telemetry.set_delivery(delivery)

    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "collector"})
    telemetry.flush()

    assert delivery.failure_count() == 1
    assert telemetry.pending_samples() == ()


# ---------------------------------------------------------------------------
# The cardinality bound holds on the delivery path
# ---------------------------------------------------------------------------


def test_the_bound_holds_however_many_distinct_combinations_arrive() -> None:
    client = RecordingClient()
    delivery = _delivery(client, batch_size=50, cardinality_max=3)
    delivery.deliver(NAMESPACE, _distinct_samples(25))

    published = {
        datum["MetricName"]
        for _namespace, data in client.puts
        for datum in data
        if datum["MetricName"] != OVERFLOW_METRIC
    }
    assert published == {METRIC_NAME}
    assert len(delivery.combinations()) == 3
    assert delivery.overflow_count() == 22


def test_the_bound_holds_across_separate_deliveries() -> None:
    client = RecordingClient()
    delivery = _delivery(client, cardinality_max=2)
    delivery.deliver(NAMESPACE, _distinct_samples(2))
    delivery.deliver(NAMESPACE, _distinct_samples(4))
    assert len(delivery.combinations()) == 2
    assert delivery.overflow_count() == 2


def test_a_combination_already_published_is_readmitted_after_saturation() -> None:
    client = RecordingClient()
    delivery = _delivery(client, cardinality_max=1)
    first = _distinct_samples(1)
    delivery.deliver(NAMESPACE, first)
    delivery.deliver(NAMESPACE, first)
    assert delivery.overflow_count() == 0
    assert len(delivery.combinations()) == 1


def test_the_overflow_counter_is_published_undimensioned_and_outside_the_bound() -> None:
    client = RecordingClient()
    delivery = _delivery(client, batch_size=50, cardinality_max=1)
    delivery.deliver(NAMESPACE, _distinct_samples(3))

    overflow = [
        datum
        for _namespace, data in client.puts
        for datum in data
        if datum["MetricName"] == OVERFLOW_METRIC
    ]
    assert len(overflow) == 2
    assert all(datum["Dimensions"] == [] for datum in overflow)
    assert OVERFLOW_METRIC not in {name for name, _dimensions in delivery.combinations()}
    assert len(delivery.combinations()) == 1


def test_a_suppressed_combination_becomes_a_record_with_the_same_content() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.ERROR, stream=sink)
    delivery = _delivery(RecordingClient(), cardinality_max=1, telemetry=telemetry)

    delivery.deliver(
        NAMESPACE,
        (
            MetricSample(name=METRIC_NAME, value=1.0, unit=UNIT_COUNT, dimensions=()),
            MetricSample(
                name=LATENCY_NAME,
                value=7.5,
                unit="Milliseconds",
                dimensions=((DIMENSION_NAME, "recall_engine"),),
            ),
        ),
    )

    # The record is written even at a severity above its own, because it is the
    # measurement's delivery rather than a diagnostic about it.
    suppressed = [record for record in _records(sink) if record.get("metric") == LATENCY_NAME]
    assert len(suppressed) == 1
    assert suppressed[0]["value"] == pytest.approx(7.5)
    assert suppressed[0]["unit"] == "Milliseconds"
    assert suppressed[0]["dimensions"] == {DIMENSION_NAME: "recall_engine"}


# ---------------------------------------------------------------------------
# No content key ever reaches a dimension
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(CONTENT_KEYS))
def test_no_declared_content_key_ever_reaches_a_dimension(key: str) -> None:
    client = RecordingClient()
    _delivery(client).deliver(
        NAMESPACE,
        (
            MetricSample(
                name=METRIC_NAME,
                value=1.0,
                unit=UNIT_COUNT,
                dimensions=((DIMENSION_NAME, "collector"), (key, MARKER_TEXT)),
            ),
        ),
    )
    _namespace, data = client.puts[0]
    assert data[0]["Dimensions"] == [{"Name": DIMENSION_NAME, "Value": "collector"}]
    assert MARKER_TEXT not in json.dumps(data)


def test_a_content_bearing_dimension_is_dropped_before_the_bound_counts_it() -> None:
    client = RecordingClient()
    delivery = _delivery(client, cardinality_max=2)
    delivery.deliver(
        NAMESPACE,
        (
            MetricSample(name=METRIC_NAME, value=1.0, unit=UNIT_COUNT, dimensions=()),
            MetricSample(
                name=METRIC_NAME,
                value=1.0,
                unit=UNIT_COUNT,
                dimensions=(("payload", MARKER_TEXT),),
            ),
        ),
    )
    # Both samples are the same combination once the content key is gone, so the
    # bound is spent once rather than twice.
    assert delivery.combinations() == ((METRIC_NAME, ()),)
    assert delivery.overflow_count() == 0
    assert MARKER_TEXT not in json.dumps(client.puts[0][1])


# ---------------------------------------------------------------------------
# The inventory is one statement of the declared set
# ---------------------------------------------------------------------------


def test_the_inventory_declares_each_name_exactly_once() -> None:
    names = [entry.name for entry in METRIC_INVENTORY]
    assert sorted(names) == sorted(set(names))
    assert frozenset(names) == METRIC_NAMES


def test_every_declared_unit_is_one_the_service_accepts() -> None:
    assert {entry.unit for entry in METRIC_INVENTORY} <= SUPPORTED_UNITS


def test_the_inventory_attaches_no_unbounded_dimension() -> None:
    attached = {name for entry in METRIC_INVENTORY for name in entry.dimensions}
    assert "client_slug" not in attached
    assert "agent_cli" not in attached


def test_the_overflow_counter_is_declared_and_undimensioned() -> None:
    entry = next(item for item in METRIC_INVENTORY if item.name == OVERFLOW_METRIC)
    assert entry.dimensions == ()
    assert entry.unit == UNIT_COUNT


def test_an_undeclared_name_is_reported_and_still_published() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.DEBUG, stream=sink)
    client = RecordingClient()
    delivery = _delivery(client, telemetry=telemetry)

    delivery.deliver(
        NAMESPACE,
        (MetricSample(name="collector.not_declared", value=1.0, unit=UNIT_COUNT, dimensions=()),),
    )

    reported = [
        record
        for record in _records(sink)
        if record["message"] == "metric name is not in the inventory"
    ]
    assert reported[0]["metric_names"] == ["collector.not_declared"]
    assert _names(client.puts[0][1]) == ["collector.not_declared"]
    assert undeclared_names(["collector.not_declared", METRIC_NAME]) == ("collector.not_declared",)


def test_the_namespace_and_the_bound_are_read_from_the_configuration_surface() -> None:
    configuration = Configuration(
        environ={},
        file_values={
            "telemetry.namespace": "MoltConfigured",
            "telemetry.metric_cardinality_max": 4,
        },
    )
    client = RecordingClient()
    delivery = CloudWatchDelivery.from_configuration(configuration, client_factory=lambda: client)
    assert delivery.namespace == "MoltConfigured"
    assert delivery.cardinality_max == 4
    # The surface declares no batch size or cadence key yet, so both resolve to
    # the documented default rather than failing the build.
    assert delivery.batch_size == DEFAULT_BATCH_SIZE
    assert delivery.delivery_interval_seconds == DEFAULT_DELIVERY_INTERVAL_SECONDS

    delivery.deliver("", _samples(1))
    assert client.puts[0][0] == "MoltConfigured"


def test_a_delivery_built_with_no_configuration_carries_the_documented_defaults() -> None:
    delivery = CloudWatchDelivery()
    assert delivery.batch_size == DEFAULT_BATCH_SIZE
    assert delivery.delivery_interval_seconds == DEFAULT_DELIVERY_INTERVAL_SECONDS
