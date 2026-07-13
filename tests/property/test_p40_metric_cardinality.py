"""Property 40: the billable metric cardinality bound, under any arrival order.

Each distinct combination of a metric name and its dimension values becomes a
separately billed metric, so the bound is the deployment's cost control rather
than a tidiness rule. The property therefore drives emissions in randomised
arrival order from a dimension-value pool larger than the maximum and asserts
both halves of the contract: the sink never carries more combinations than the
configured maximum, and every emission the bound suppressed is recoverable from
the record sink carrying the same name, the same value, and the same dimensions.

The oracle here is deliberately independent of the emitter. It replays the same
arrival order under the three stated admission rules -- an already-published
combination is always admitted, a new one only while the published count is
below the maximum, everything else is diverted -- and treats a dimension set as
identity-bearing regardless of the order it was supplied in. An emitter that
compared dimensions order-sensitively, or that admitted one combination too
many, disagrees with that replay.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from molt.telemetry import OVERFLOW_METRIC, Combination, Dimensions, Severity, Telemetry

# Metric names drawn from the observability metric table. None of them resembles
# a credential and none collides with the content-key drop set, so no field is
# filtered out from under the assertions below.
METRIC_NAMES: Final[tuple[str, ...]] = (
    "collector.events_accepted",
    "recall.queries",
    "erasure.runs",
    "watcher.halts",
)

# The dimension names the design attaches to metrics. The first is present on
# every emission and the other two are the high-cardinality ones attached where
# a per-Client or per-tool breakdown earns its place. Three names together are
# enough for the reordering rule to have something to reorder.
PRIMARY_DIMENSION_NAME: Final[str] = "component"
OPTIONAL_DIMENSION_NAMES: Final[tuple[str, ...]] = ("client_slug", "agent_cli")
DIMENSION_NAMES: Final[tuple[str, ...]] = (PRIMARY_DIMENSION_NAME, *OPTIONAL_DIMENSION_NAMES)

# The bound is drawn across its interesting range, one included: at a maximum of
# one, the second distinct combination is already over the bound.
MAXIMUM_FLOOR: Final[int] = 1
MAXIMUM_CEILING: Final[int] = 20

# The emission count the generator produces, per the design generator table.
EMISSION_FLOOR: Final[int] = 1
EMISSION_CEILING: Final[int] = 200

# How far the dimension-value pool exceeds the maximum, and how many distinct
# combinations beyond the maximum are available to arrive. Both are at least one,
# which is what makes the pool larger than the bound and overflow reachable.
POOL_SURPLUS_FLOOR: Final[int] = 1
POOL_SURPLUS_CEILING: Final[int] = 4
COMBINATION_SURPLUS_FLOOR: Final[int] = 1
COMBINATION_SURPLUS_CEILING: Final[int] = 5

# Measurement values, kept finite and modest so a record serialises and parses
# back to the same float.
VALUE_BOUND: Final[float] = 1.0e6

# The component and message the emitter writes a diverted measurement under.
DIVERTED_COMPONENT: Final[str] = "telemetry"

# The threshold the instance is built with. It is above the severity a diverted
# record carries on purpose: the diversion is the measurement's own delivery
# path, so it must appear anyway.
THRESHOLD: Final[Severity] = Severity.ERROR
DIVERTED_SEVERITY: Final[str] = str(Severity.WARNING)


@dataclass(frozen=True, slots=True)
class Emission:
    """One measurement as the caller supplies it, dimension order included."""

    name: str
    value: float
    supplied: tuple[tuple[str, str], ...]

    @property
    def dimensions(self) -> Dimensions:
        """The dimension set as identity, independent of the supplied order."""
        return tuple(sorted(self.supplied))

    @property
    def combination(self) -> Combination:
        """The billable identity this emission would occupy."""
        return (self.name, self.dimensions)


@dataclass(frozen=True, slots=True)
class MetricStream:
    """A bound paired with the emissions that arrive against it."""

    maximum: int
    emissions: tuple[Emission, ...]


@dataclass(frozen=True, slots=True)
class DivertedRecord:
    """One parsed record standing for a measurement the bound suppressed."""

    severity: str
    component: str
    metric: str
    value: float
    dimensions: Dimensions
    maximum: int


def _reorder(pairs: Dimensions, rotation: int, backwards: bool) -> Dimensions:
    """Present the same dimension pairs in a different order.

    A rotation combined with a reversal reaches every ordering of a set of up to
    three pairs, which is what lets the property exercise the normalisation rule
    rather than assume it.
    """
    items = list(reversed(pairs)) if backwards else list(pairs)
    if not items:
        return ()
    offset = rotation % len(items)
    return tuple(items[offset:] + items[:offset])


@st.composite
def metric_streams(draw: st.DrawFn) -> MetricStream:
    """Emissions in randomised arrival order against a bound they overrun.

    Three coverage obligations are met by construction rather than by luck. The
    pool of dimension values is larger than the maximum and the set of distinct
    combinations available to arrive exceeds it too, so a stream long enough to
    carry them all reaches the bound. Whenever the emission count exceeds that
    set, the surplus emissions repeat combinations already in the stream, so the
    always-admitted rule is exercised. Every emission then presents its
    dimensions in an independently drawn order.
    """
    maximum = draw(st.integers(min_value=MAXIMUM_FLOOR, max_value=MAXIMUM_CEILING))
    surplus = draw(
        st.integers(min_value=COMBINATION_SURPLUS_FLOOR, max_value=COMBINATION_SURPLUS_CEILING)
    )
    pool_size = (
        maximum
        + surplus
        + draw(st.integers(min_value=POOL_SURPLUS_FLOOR, max_value=POOL_SURPLUS_CEILING))
    )
    pool = tuple(f"pool-{index:02d}" for index in range(pool_size))

    # Each available combination takes a distinct value for the always-present
    # dimension, so distinctness is a property of the construction rather than a
    # filter the generator has to retry against.
    available: list[tuple[str, dict[str, str]]] = []
    for index in range(maximum + surplus):
        name = draw(st.sampled_from(METRIC_NAMES))
        extra = draw(
            st.dictionaries(
                keys=st.sampled_from(OPTIONAL_DIMENSION_NAMES),
                values=st.sampled_from(pool),
                max_size=len(OPTIONAL_DIMENSION_NAMES),
            )
        )
        available.append((name, {PRIMARY_DIMENSION_NAME: pool[index], **extra}))

    indices = list(range(len(available)))
    count = draw(st.integers(min_value=EMISSION_FLOOR, max_value=EMISSION_CEILING))
    if count <= len(indices):
        arrival = list(draw(st.permutations(indices)))[:count]
    else:
        repeats = draw(
            st.lists(
                st.integers(min_value=0, max_value=len(indices) - 1),
                min_size=count - len(indices),
                max_size=count - len(indices),
            )
        )
        arrival = list(draw(st.permutations([*indices, *repeats])))

    values = draw(
        st.lists(
            st.floats(
                min_value=-VALUE_BOUND,
                max_value=VALUE_BOUND,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=len(arrival),
            max_size=len(arrival),
        )
    )
    rotations = draw(
        st.lists(
            st.integers(min_value=0, max_value=len(DIMENSION_NAMES) - 1),
            min_size=len(arrival),
            max_size=len(arrival),
        )
    )
    reversals = draw(
        st.lists(st.booleans(), min_size=len(arrival), max_size=len(arrival)),
    )

    emissions: list[Emission] = []
    for position, index in enumerate(arrival):
        name, dimensions = available[index]
        sorted_pairs: Dimensions = tuple(sorted(dimensions.items()))
        emissions.append(
            Emission(
                name=name,
                value=values[position],
                supplied=_reorder(sorted_pairs, rotations[position], reversals[position]),
            )
        )
    return MetricStream(maximum=maximum, emissions=tuple(emissions))


def _replay(stream: MetricStream) -> tuple[tuple[Combination, ...], tuple[Emission, ...]]:
    """Apply the admission rules independently of the emitter under test.

    Returns the combinations that should have been published, in first-published
    order, and the emissions that should have been diverted, in arrival order.
    """
    published: list[Combination] = []
    diverted: list[Emission] = []
    for emission in stream.emissions:
        combination = emission.combination
        if combination in published:
            continue
        if len(published) < stream.maximum:
            published.append(combination)
        else:
            diverted.append(emission)
    return tuple(published), tuple(diverted)


def _admitted_totals(
    stream: MetricStream, published: tuple[Combination, ...]
) -> dict[Combination, float]:
    """The value each published combination should have accumulated."""
    admitted = set(published)
    totals: dict[Combination, float] = {}
    for emission in stream.emissions:
        combination = emission.combination
        if combination in admitted:
            totals[combination] = totals.get(combination, 0.0) + emission.value
    return totals


def _parse(line: str) -> DivertedRecord:
    """Read one single-line record back into the fields the property compares."""
    payload: dict[str, object] = json.loads(line)
    severity = payload["severity"]
    component = payload["component"]
    metric = payload["metric"]
    value = payload["value"]
    maximum = payload["cardinality_max"]
    dimensions = payload["dimensions"]
    assert isinstance(severity, str)
    assert isinstance(component, str)
    assert isinstance(metric, str)
    assert isinstance(value, float)
    assert isinstance(maximum, int)
    assert isinstance(dimensions, dict)
    return DivertedRecord(
        severity=severity,
        component=component,
        metric=metric,
        value=value,
        dimensions=tuple(sorted((str(key), str(item)) for key, item in dimensions.items())),
        maximum=maximum,
    )


# Feature: molt, Property 40: For any sequence of metric emissions with arbitrary
# names and arbitrary dimension values, in any arrival order, the count of
# distinct metric-and-dimension combinations reaching the metric sink never
# exceeds the configured maximum, and every suppressed emission appears instead
# as a structured log record carrying the same name, value, and dimensions.
# Validates: Requirements 33.13, 33.14
@settings(max_examples=100)
@given(metric_streams())
def test_metric_cardinality_bound_holds_and_diversions_are_recoverable(
    stream: MetricStream,
) -> None:
    sink = io.StringIO()
    telemetry = Telemetry(
        namespace="Molt",
        cardinality_max=stream.maximum,
        log_level=THRESHOLD,
        stream=sink,
    )

    for emission in stream.emissions:
        telemetry.metric(emission.name, emission.value, **dict(emission.supplied))

    expected_published, expected_diverted = _replay(stream)

    # The bound holds: the caller's billable combinations never exceed it, and
    # they are exactly the ones the admission rules admit, in that order.
    assert len(telemetry.combinations()) <= stream.maximum
    assert telemetry.combinations() == expected_published

    # Dimensions are identity-bearing as a sorted set, so the same dimensions in
    # a different order occupy one combination rather than two.
    for _name, dimensions in telemetry.combinations():
        assert dimensions == tuple(sorted(dimensions))

    # Nothing admitted was silently dropped: each published combination carries
    # the sum of every emission that landed on it.
    counters = telemetry.counters()
    assert len(counters) == len(expected_published)
    totals = _admitted_totals(stream, expected_published)
    for combination, total in counters.items():
        assert total == totals[combination]

    # The overflow guard counts the diversions, stays undimensioned, and stays
    # outside the combinations the bound is measured against.
    assert telemetry.overflow_count() == float(len(expected_diverted))
    assert OVERFLOW_METRIC not in {name for name, _dimensions in telemetry.combinations()}
    overflow_samples = [
        sample for sample in telemetry.pending_samples() if sample.name == OVERFLOW_METRIC
    ]
    assert len(overflow_samples) == len(expected_diverted)
    for sample in overflow_samples:
        assert sample.dimensions == ()

    # Every suppressed emission is recoverable from the record sink, in arrival
    # order, carrying the same name, the same value, and the same dimensions.
    records = [_parse(line) for line in sink.getvalue().splitlines()]
    assert len(records) == len(expected_diverted)
    for record, emission in zip(records, expected_diverted, strict=True):
        assert record.metric == emission.name
        assert record.value == emission.value
        assert record.dimensions == emission.dimensions
        assert record.component == DIVERTED_COMPONENT
        assert record.maximum == stream.maximum
        # The record appears despite a threshold above its own severity, because
        # a suppressed measurement must not vanish when the level is raised.
        assert record.severity == DIVERTED_SEVERITY
