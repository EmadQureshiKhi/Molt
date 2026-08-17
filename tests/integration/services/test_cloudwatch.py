"""Live batched metric delivery, a live refusal, and the field filter around both.

One service is touched here: the metrics service the delivery backend publishes
to. Three claims are made against it, and each is a claim the unit suite cannot
make because each turns on what the real service does with a real request.

**A batch that the service accepted is a batch the service accepted.** The unit
suite asserts the batching arithmetic against a recording double, which is the
right place for arithmetic. What it cannot show is that the datum shape this
codebase builds -- the name, the value, the unit, the dimension pairs -- is a shape
the service takes. That is what the first test is for, and it is asserted by the
puts completing with the failure tally still empty rather than by anything the
double could report.

**A refusal is survived rather than raised.** Telemetry sits on the critical path
of no write, so an emission that fails must be counted, reported, and swallowed.
The unit suite drives that with a double that raises on command. Here the real
service is asked to accept a datum it will not accept, and the same three
outcomes are asserted: nothing propagates to the caller, the failure is tallied
under the exception type the library raised, and the remaining batches are still
attempted.

**No content-bearing key becomes a dimension, and the live path is where that
matters most.** A dimension is published where no log filter can ever reach it, so
the delivery path applies the same predicate that drops a content-bearing log
field. The client is wrapped in a recording proxy that forwards to the real
service, so the request as sent is inspected and the assertion is over the bytes
that actually left this process rather than over a stub's idea of them.

**Every test here skips in this environment.** The `services` marker answers
whether cloud access and a credential source for each provider role are configured
at all, and each test additionally requires the deployment to have named its own
metric namespace: publishing into the built-in default namespace would be
publishing into a namespace no operator chose, and an unprovisioned deployment has
named none. The skip says which key to set.

**What a full run costs, where one is possible.** Four puts of a handful of data
items each, of which one is expected to be refused, and one metric name that the
inventory does not declare so it can be shown reported. Nothing here loops and no
test scales its call count with anything. Custom metric data is billable, so the
count is small and fixed and every datum carries the same two dimensions.

No namespace, region, credential value, or account identifier appears in this
file. The namespace is read from the configuration surface and every dimension
value is obviously synthetic.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Final

import pytest

from molt.config.resolve import (
    Configuration,
    MissingConfigError,
    Source,
    UnknownSettingError,
    load_configuration,
)
from molt.telemetry import (
    CONTENT_KEY_MARKERS,
    CONTENT_KEYS,
    UNIT_COUNT,
    MetricSample,
    Severity,
    Telemetry,
    is_content_key,
)
from molt.telemetry.delivery import CloudWatchClient, CloudWatchDelivery, cloudwatch_client

# Cloud access and a credential source for each provider role, which is what the
# marker gates on. Each test additionally names the configuration key whose value
# it needs, so a partly provisioned deployment skips one test rather than all.
pytestmark = pytest.mark.services

# The configuration key the metric namespace is read from.
NAMESPACE_KEY: Final[str] = "MOLT_METRIC_NAMESPACE"

# A declared metric name and the dimension the design attaches to everything, so a
# live put carries a name the inventory knows and a dimension of bounded width.
METRIC_NAME: Final[str] = "collector.events_accepted"
LATENCY_NAME: Final[str] = "recall.latency_ms"
DIMENSION_NAME: Final[str] = "component"
DIMENSION_VALUE: Final[str] = "service_probe"

# A value obviously not a measurement body, used wherever a content-bearing field
# or dimension must be shown to have been dropped rather than merely renamed.
MARKER_TEXT: Final[str] = "dropped-if-seen"

# How many samples one batching probe sends, and the batch size it sends them at.
# More than twice the size, so the final group is a partial one and the ceiling is
# observed rather than coincidentally matched.
PROBE_SAMPLE_COUNT: Final[int] = 7
PROBE_BATCH_SIZE: Final[int] = 3
EXPECTED_BATCH_SIZES: Final[tuple[int, ...]] = (3, 3, 1)

# The longest metric name the service accepts. A name past it is refused, which is
# how a real refusal is provoked without creating anything and without asking for a
# permission the deployment does not grant.
SERVICE_NAME_LIMIT: Final[int] = 255

# The request keys one put is made under, fixed by the service.
_REQUEST_NAMESPACE: Final[str] = "Namespace"
_REQUEST_DATA: Final[str] = "MetricData"


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def _configured_namespace() -> tuple[Configuration, str]:
    """The surface and the namespace it names, or a skip naming the key.

    A namespace resolving from the built-in default means no deployment named one,
    and publishing a live measurement into a namespace nobody chose is not what
    this test is for. So the default is treated as absent and the skip says which
    key to set.
    """
    configuration = load_configuration()
    try:
        source = configuration.source(NAMESPACE_KEY)
        namespace = configuration.text(NAMESPACE_KEY).strip()
    except (MissingConfigError, UnknownSettingError) as fault:
        pytest.skip(
            f"{NAMESPACE_KEY} names no value, so no metric namespace is provisioned "
            f"and no call was made: {fault}"
        )
    if source is Source.DEFAULT or not namespace:
        pytest.skip(
            f"{NAMESPACE_KEY} resolves from the built-in default, so this deployment "
            "named no metric namespace of its own and nothing was published"
        )
    return configuration, namespace


# ---------------------------------------------------------------------------
# The live client, behind a recording proxy
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class RecordingProxy:
    """The real metrics client, recording every request before forwarding it.

    Recording is what makes an assertion about the request possible: the service
    reports nothing about the dimensions it received, and a stub in its place would
    make this module a second copy of the unit suite.
    """

    inner: CloudWatchClient
    puts: list[tuple[str, list[object]]] = field(default_factory=list)

    def put_metric_data(self, **request: object) -> object:
        """Record one request, then let the real service answer it."""
        namespace = request.get(_REQUEST_NAMESPACE)
        data = request.get(_REQUEST_DATA)
        assert isinstance(namespace, str)
        assert isinstance(data, list)
        self.puts.append((namespace, list(data)))
        return self.inner.put_metric_data(**request)


def _proxy() -> RecordingProxy:
    """A recording proxy over a real client, confirmed to satisfy the client shape."""
    proxy = RecordingProxy(inner=cloudwatch_client())
    client: CloudWatchClient = proxy
    assert client is proxy
    return proxy


def _delivery(
    configuration: Configuration,
    proxy: RecordingProxy,
    *,
    telemetry: Telemetry | None = None,
) -> CloudWatchDelivery:
    """A delivery backend built from the surface and wired to the proxy.

    Nothing about the namespace, the bound, the batch size, or the cadence is
    stated here: all four come from the same view the running process resolves
    against, which is what makes this a test of the configured deployment.
    """
    return CloudWatchDelivery.from_configuration(
        configuration, client_factory=lambda: proxy, telemetry=telemetry
    )


def _samples(count: int, *, name: str = METRIC_NAME) -> tuple[MetricSample, ...]:
    """One combination repeated, so the cardinality bound is not what is measured."""
    return tuple(
        MetricSample(
            name=name,
            value=float(index),
            unit=UNIT_COUNT,
            dimensions=((DIMENSION_NAME, DIMENSION_VALUE),),
        )
        for index in range(count)
    )


def _records(sink: io.StringIO) -> list[dict[str, object]]:
    """Parse every record written to a sink back into a mapping."""
    return [json.loads(line) for line in sink.getvalue().splitlines()]


# ---------------------------------------------------------------------------
# Buffered, batched delivery under the configured namespace
# ---------------------------------------------------------------------------


def test_buffered_samples_are_delivered_in_batches_under_the_configured_namespace() -> None:
    """Every batch reaches the service, under the namespace the deployment named.

    The batch size is stated here rather than read from the surface, because what
    is being shown is that the batching the codebase performs survives contact with
    the service: the sizes are fixed so the final partial group is exercised, and
    the namespace is the configured one so nothing is published where no operator
    looks.
    """
    configuration, namespace = _configured_namespace()
    proxy = _proxy()
    delivery = CloudWatchDelivery(
        namespace=namespace,
        batch_size=PROBE_BATCH_SIZE,
        cardinality_max=configuration.integer("MOLT_METRIC_CARDINALITY_MAX"),
        client_factory=lambda: proxy,
    )

    delivery.deliver(namespace, _samples(PROBE_SAMPLE_COUNT))

    assert tuple(len(data) for _namespace, data in proxy.puts) == EXPECTED_BATCH_SIZES
    assert {sent for sent, _data in proxy.puts} == {namespace}
    assert delivery.put_count() == len(EXPECTED_BATCH_SIZES)
    assert delivery.failure_count() == 0, (
        "the datum shape this codebase builds is one the service accepts, which is "
        "the whole thing a live put establishes"
    )
    assert delivery.failures() == {}


def test_the_namespace_and_the_bound_come_from_the_configuration_surface() -> None:
    """A backend built from the surface publishes where the surface says.

    The caller passes an empty namespace, which is what the telemetry surface's own
    flush does when it has nothing to override with, so the configured value is the
    one that has to answer.
    """
    configuration, namespace = _configured_namespace()
    proxy = _proxy()
    delivery = _delivery(configuration, proxy)

    assert delivery.namespace == namespace
    assert delivery.cardinality_max == configuration.integer("MOLT_METRIC_CARDINALITY_MAX")

    delivery.deliver("", _samples(1, name=LATENCY_NAME))

    assert [sent for sent, _data in proxy.puts] == [namespace]
    assert delivery.failure_count() == 0


# ---------------------------------------------------------------------------
# A refusal by the service reaches no caller
# ---------------------------------------------------------------------------


def test_an_emission_the_service_refuses_does_not_raise_into_the_caller() -> None:
    """The service refuses one batch, and the caller learns of it from a tally.

    The refusal is provoked with a metric name longer than the service accepts,
    which creates nothing and needs no permission the deployment withholds. Three
    things are then asserted, and they are the three that make telemetry safe to
    put on a write path: nothing propagated, the failure is tallied under the type
    the library raised, and the batches after the refused one were still attempted.
    """
    configuration, namespace = _configured_namespace()
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.DEBUG, stream=sink)
    proxy = _proxy()
    delivery = CloudWatchDelivery(
        namespace=namespace,
        batch_size=1,
        cardinality_max=configuration.integer("MOLT_METRIC_CARDINALITY_MAX"),
        client_factory=lambda: proxy,
        telemetry=telemetry,
    )

    refused_name = "m" * (SERVICE_NAME_LIMIT + 1)
    delivery.deliver(
        namespace,
        (
            *_samples(1, name=refused_name),
            *_samples(1),
        ),
    )

    assert delivery.failure_count() == 1, "the refused batch is counted rather than raised"
    assert sum(delivery.failures().values()) == 1
    assert delivery.put_count() == 1, "the batch after the refused one was still attempted"

    reported = [
        record for record in _records(sink) if record["message"] == "metric delivery failed"
    ]
    assert len(reported) == 1
    assert reported[0]["namespace"] == namespace
    assert reported[0]["severity"] == str(Severity.WARNING)
    error_type = reported[0]["error_type"]
    assert isinstance(error_type, str) and error_type
    assert delivery.failures().get(error_type) == 1


# ---------------------------------------------------------------------------
# No content-bearing key reaches a dimension, at any depth
# ---------------------------------------------------------------------------


def test_no_content_bearing_dimension_reaches_the_service_or_a_log_record() -> None:
    """Every content-named dimension is gone from the request that left this process.

    Both halves of the predicate are exercised: the names the exact set declares
    and the names it omits but whose spelling carries a marker. The assertion is
    over the recorded request, so it is about the data the service received rather
    than about a stub's summary of it.
    """
    configuration, namespace = _configured_namespace()
    proxy = _proxy()
    delivery = _delivery(configuration, proxy)

    hostile_names = [
        *sorted(CONTENT_KEYS),
        *(f"outer{marker}inner" for marker in CONTENT_KEY_MARKERS),
    ]
    hostile = tuple((name, MARKER_TEXT) for name in hostile_names)
    delivery.deliver(
        namespace,
        (
            MetricSample(
                name=METRIC_NAME,
                value=1.0,
                unit=UNIT_COUNT,
                dimensions=((DIMENSION_NAME, DIMENSION_VALUE), *hostile),
            ),
        ),
    )

    assert proxy.puts, "one datum was admitted, so one put was issued"
    sent = json.dumps(proxy.puts, default=str)
    assert MARKER_TEXT not in sent, "a content-bearing dimension value left this process"
    for _namespace, data in proxy.puts:
        for datum in data:
            assert isinstance(datum, dict)
            names = [pair["Name"] for pair in datum["Dimensions"]]
            assert names == [DIMENSION_NAME]
            assert not any(is_content_key(str(name)) for name in names)
    assert delivery.failure_count() == 0


def test_a_body_a_credential_and_a_vector_reach_no_log_record_at_any_depth() -> None:
    """The filter that guards a dimension guards a log field at every depth too.

    A dimension and a log field are two ways the same value could escape, and the
    delivery path publishes a dimension where no log filter can reach it. So the
    same predicate is asserted over a record nested three deep, carrying a body, a
    credential-named field, and a vector, none of which may survive.
    """
    _configuration, namespace = _configured_namespace()
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.DEBUG, stream=sink)

    telemetry.log(
        Severity.WARNING,
        "telemetry",
        "a service probe",
        namespace=namespace,
        detail={
            "body": MARKER_TEXT,
            "kept_one": 1,
            "level_two": {
                "credential": MARKER_TEXT,
                "kept_two": 2,
                "level_three": {"vector": [0.5, 0.25], "kept_three": 3},
            },
        },
        entries=[{"payload": MARKER_TEXT, "index": 0}, {"embedding": [0.5], "index": 1}],
    )

    written = sink.getvalue()
    assert MARKER_TEXT not in written
    record = _records(sink)[0]
    assert record["detail"] == {
        "kept_one": 1,
        "level_two": {"kept_two": 2, "level_three": {"kept_three": 3}},
    }
    assert record["entries"] == [{"index": 0}, {"index": 1}]
    assert record["namespace"] == namespace
