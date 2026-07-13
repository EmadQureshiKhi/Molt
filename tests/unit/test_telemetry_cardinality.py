"""Unit tests for the log field filter and the cardinality overflow accounting.

Two behaviours are checked, and both are checked at their edges. The generative
bound on billable combinations is asserted elsewhere across many arrival orders;
what is asserted here is the handful of boundary cases a generator is unlikely to
name: the very first diversion, a maximum of one, a combination already published
being readmitted after saturation, and the shape of the overflow signal itself.

The overflow signal is the interesting one. It has to be undimensioned, because a
dimensioned guard would multiply into the very budget it protects, and it has to
sit outside the combinations the bound governs, because a diversion happens only
once the bound is already saturated. Both facts are asserted directly rather than
inferred from a count.

The field filter is checked key by key over the whole declared drop set and at
every depth, because a body nested inside a diagnostic mapping is still a body.
The four fixed keys are checked to be present, first, in order, and immune to a
caller-supplied field of the same name.
"""

from __future__ import annotations

import io
import json
from typing import Final

import pytest

from molt.telemetry import (
    CONTENT_KEY_MARKERS,
    CONTENT_KEYS,
    DEFAULT_CARDINALITY_MAX,
    DEFAULT_NAMESPACE,
    LOG_KEY_ORDER,
    OVERFLOW_METRIC,
    UNIT_COUNT,
    UNSET_CORRELATION,
    Combination,
    MetricSample,
    Severity,
    Telemetry,
    correlation,
    current,
    current_correlation,
    reset,
)

# A component and a message that carry no content and collide with no field name.
COMPONENT: Final[str] = "unit"
MESSAGE: Final[str] = "a record"

# A metric name from the observability table, and dimension names the design
# attaches to one.
METRIC_NAME: Final[str] = "collector.events_accepted"
DIMENSION_NAME: Final[str] = "component"
SECOND_DIMENSION_NAME: Final[str] = "client_slug"

# A value obviously not a credential, used wherever a field body must be shown to
# have been dropped rather than merely rewritten.
MARKER_TEXT: Final[str] = "dropped-if-seen"


def _records(sink: io.StringIO) -> list[dict[str, object]]:
    """Parse every record written to a sink back into a mapping."""
    parsed: list[dict[str, object]] = []
    for line in sink.getvalue().splitlines():
        record: dict[str, object] = json.loads(line)
        parsed.append(record)
    return parsed


def _one_record(sink: io.StringIO) -> dict[str, object]:
    """Parse the single record a sink is expected to hold."""
    parsed = _records(sink)
    assert len(parsed) == 1
    return parsed[0]


def _telemetry(sink: io.StringIO, *, cardinality_max: int = DEFAULT_CARDINALITY_MAX) -> Telemetry:
    """An instance writing to a sink, at the lowest severity, with a chosen bound."""
    return Telemetry(cardinality_max=cardinality_max, log_level=Severity.DEBUG, stream=sink)


# ---------------------------------------------------------------------------
# The field filter drops every content key, at every depth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(CONTENT_KEYS))
def test_every_declared_content_key_is_dropped_at_the_top_level(key: str) -> None:
    sink = io.StringIO()
    _telemetry(sink).log(Severity.INFO, COMPONENT, MESSAGE, **{key: MARKER_TEXT})
    record = _one_record(sink)
    assert key not in record
    assert MARKER_TEXT not in json.dumps(record)


@pytest.mark.parametrize("key", sorted(CONTENT_KEYS))
def test_every_declared_content_key_is_dropped_however_it_is_capitalised(key: str) -> None:
    sink = io.StringIO()
    _telemetry(sink).log(Severity.INFO, COMPONENT, MESSAGE, **{key.upper(): MARKER_TEXT})
    assert MARKER_TEXT not in json.dumps(_one_record(sink))


@pytest.mark.parametrize("marker", CONTENT_KEY_MARKERS)
def test_a_name_the_exact_set_omits_is_dropped_on_its_marker(marker: str) -> None:
    name = f"outer{marker}inner".replace("-", "_")
    sink = io.StringIO()
    _telemetry(sink).log(Severity.INFO, COMPONENT, MESSAGE, **{name: MARKER_TEXT})
    record = _one_record(sink)
    assert name not in record
    assert MARKER_TEXT not in json.dumps(record)


def test_a_content_key_is_dropped_at_every_depth_of_a_nested_field() -> None:
    sink = io.StringIO()
    _telemetry(sink).log(
        Severity.INFO,
        COMPONENT,
        MESSAGE,
        detail={
            "content": MARKER_TEXT,
            "keep_one": 1,
            "level_two": {
                "payload": MARKER_TEXT,
                "keep_two": 2,
                "level_three": {"vector": [1.0, 2.0], "keep_three": 3},
            },
        },
    )
    record = _one_record(sink)
    assert record["detail"] == {
        "keep_one": 1,
        "level_two": {"keep_two": 2, "level_three": {"keep_three": 3}},
    }
    assert MARKER_TEXT not in json.dumps(record)


def test_a_content_key_is_dropped_inside_a_sequence_of_mappings() -> None:
    sink = io.StringIO()
    _telemetry(sink).log(
        Severity.INFO,
        COMPONENT,
        MESSAGE,
        entries=[{"prompt": MARKER_TEXT, "index": 0}, {"index": 1, "embedding": [0.5]}],
    )
    record = _one_record(sink)
    assert record["entries"] == [{"index": 0}, {"index": 1}]
    assert MARKER_TEXT not in json.dumps(record)


def test_a_field_carrying_no_content_survives_with_its_own_type() -> None:
    sink = io.StringIO()
    _telemetry(sink).log(
        Severity.INFO,
        COMPONENT,
        MESSAGE,
        count=3,
        ratio=0.5,
        flag=True,
        absent=None,
        name="kept",
        names=["one", "two"],
    )
    record = _one_record(sink)
    assert record["count"] == 3
    assert record["ratio"] == pytest.approx(0.5)
    assert record["flag"] is True
    assert record["absent"] is None
    assert record["name"] == "kept"
    assert record["names"] == ["one", "two"]


def test_a_value_of_no_json_type_is_rendered_through_its_own_text_conversion() -> None:
    class Rendered:
        def __str__(self) -> str:
            return "rendered"

    sink = io.StringIO()
    _telemetry(sink).log(Severity.INFO, COMPONENT, MESSAGE, detail=Rendered())
    assert _one_record(sink)["detail"] == "rendered"


def test_a_structure_deeper_than_the_bound_is_dropped_rather_than_walked() -> None:
    deep: object = MARKER_TEXT
    for _ in range(64):
        deep = {"inner": deep}
    sink = io.StringIO()
    _telemetry(sink).log(Severity.INFO, COMPONENT, MESSAGE, detail=deep)
    record = _one_record(sink)
    assert MARKER_TEXT not in json.dumps(record)


# ---------------------------------------------------------------------------
# The four fixed keys are always present and never overwritten
# ---------------------------------------------------------------------------


def test_the_four_fixed_keys_are_present_first_and_in_order() -> None:
    sink = io.StringIO()
    _telemetry(sink).log(Severity.WARNING, COMPONENT, MESSAGE, extra="kept")
    record = _one_record(sink)
    assert tuple(record)[: len(LOG_KEY_ORDER)] == LOG_KEY_ORDER
    assert record["severity"] == str(Severity.WARNING)
    assert record["component"] == COMPONENT
    assert record["message"] == MESSAGE
    assert record["correlation_id"] == current_correlation()


@pytest.mark.parametrize("key", LOG_KEY_ORDER)
def test_a_caller_supplied_field_never_overwrites_a_fixed_key(key: str) -> None:
    sink = io.StringIO()
    _telemetry(sink).log(Severity.INFO, COMPONENT, MESSAGE, **{key: MARKER_TEXT})
    record = _one_record(sink)
    assert record[key] != MARKER_TEXT
    assert tuple(record)[: len(LOG_KEY_ORDER)] == LOG_KEY_ORDER
    assert MARKER_TEXT not in json.dumps(record)


def test_the_correlation_identifier_is_the_one_the_open_block_established() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink)
    with correlation("run-one"):
        telemetry.log(Severity.INFO, COMPONENT, MESSAGE)
        with correlation("run-two"):
            telemetry.log(Severity.INFO, COMPONENT, MESSAGE)
        telemetry.log(Severity.INFO, COMPONENT, MESSAGE)
    telemetry.log(Severity.INFO, COMPONENT, MESSAGE)
    identifiers = [record["correlation_id"] for record in _records(sink)]
    assert identifiers == ["run-one", "run-two", "run-one", UNSET_CORRELATION]


def test_a_record_below_the_configured_severity_is_not_written() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(log_level=Severity.ERROR, stream=sink)
    telemetry.log(Severity.INFO, COMPONENT, MESSAGE)
    telemetry.log(Severity.WARNING, COMPONENT, MESSAGE)
    telemetry.log(Severity.ERROR, COMPONENT, MESSAGE)
    assert [record["severity"] for record in _records(sink)] == [str(Severity.ERROR)]


def test_each_record_occupies_exactly_one_line() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink)
    telemetry.log(Severity.INFO, COMPONENT, "a message\nwith a newline", detail="one\ntwo")
    telemetry.log(Severity.INFO, COMPONENT, MESSAGE)
    assert len(sink.getvalue().splitlines()) == 2


# ---------------------------------------------------------------------------
# The overflow counter stays undimensioned and outside the bound
# ---------------------------------------------------------------------------


def _names(combinations: tuple[Combination, ...]) -> set[str]:
    """The metric names among a set of published combinations."""
    return {name for name, _dimensions in combinations}


def test_nothing_overflows_while_the_bound_is_unreached() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink, cardinality_max=3)
    for index in range(3):
        telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: f"one-{index}"})
    assert len(telemetry.combinations()) == 3
    assert telemetry.overflow_count() == 0.0
    assert _records(sink) == []


def test_the_first_diversion_increments_one_undimensioned_counter() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink, cardinality_max=1)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "admitted"})
    telemetry.metric(METRIC_NAME, 2.0, **{DIMENSION_NAME: "diverted"})

    assert telemetry.combinations() == ((METRIC_NAME, ((DIMENSION_NAME, "admitted"),)),)
    assert telemetry.overflow_count() == 1.0
    # The guard is not one of the combinations the bound governs, and it carries
    # no dimensions at all, so it cannot grow with load.
    assert OVERFLOW_METRIC not in _names(telemetry.combinations())
    assert OVERFLOW_METRIC not in {name for name, _dimensions in telemetry.counters()}
    overflow = [sample for sample in telemetry.pending_samples() if sample.name == OVERFLOW_METRIC]
    assert overflow == [
        MetricSample(name=OVERFLOW_METRIC, value=1.0, unit=UNIT_COUNT, dimensions=())
    ]


def test_the_overflow_counter_stays_undimensioned_however_the_diversions_are_dimensioned() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink, cardinality_max=1)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "admitted"})
    for index in range(25):
        telemetry.metric(
            f"{METRIC_NAME}.{index}",
            1.0,
            **{DIMENSION_NAME: f"c-{index}", SECOND_DIMENSION_NAME: f"s-{index}"},
        )
    assert len(telemetry.combinations()) == 1
    assert telemetry.overflow_count() == 25.0
    for sample in telemetry.pending_samples():
        if sample.name == OVERFLOW_METRIC:
            assert sample.dimensions == ()


def test_a_combination_already_published_is_readmitted_after_saturation() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink, cardinality_max=2)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "first"})
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "second"})
    telemetry.metric(METRIC_NAME, 4.0, **{DIMENSION_NAME: "third"})
    telemetry.metric(METRIC_NAME, 5.0, **{DIMENSION_NAME: "first"})

    counters = telemetry.counters()
    assert counters[(METRIC_NAME, ((DIMENSION_NAME, "first"),))] == 6.0
    assert len(telemetry.combinations()) == 2
    # Only the third emission was diverted: an already-published combination
    # costs no further billable metric, so it is always admitted.
    assert telemetry.overflow_count() == 1.0


def test_the_same_dimensions_in_a_different_order_are_one_combination() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink, cardinality_max=1)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "a", SECOND_DIMENSION_NAME: "b"})
    telemetry.metric(METRIC_NAME, 1.0, **{SECOND_DIMENSION_NAME: "b", DIMENSION_NAME: "a"})
    assert len(telemetry.combinations()) == 1
    assert telemetry.overflow_count() == 0.0


def test_an_undimensioned_measurement_is_its_own_combination() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink, cardinality_max=2)
    telemetry.metric(METRIC_NAME, 1.0)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "one"})
    assert telemetry.combinations() == (
        (METRIC_NAME, ()),
        (METRIC_NAME, ((DIMENSION_NAME, "one"),)),
    )


def test_a_diverted_measurement_keeps_its_name_value_dimensions_and_unit() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink, cardinality_max=1)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "admitted"})
    telemetry.metric(
        METRIC_NAME,
        3.5,
        unit="Milliseconds",
        **{DIMENSION_NAME: "diverted", SECOND_DIMENSION_NAME: "slug"},
    )
    record = _one_record(sink)
    assert record["metric"] == METRIC_NAME
    assert record["value"] == pytest.approx(3.5)
    assert record["unit"] == "Milliseconds"
    assert record["dimensions"] == {DIMENSION_NAME: "diverted", SECOND_DIMENSION_NAME: "slug"}
    assert record["cardinality_max"] == 1
    assert tuple(record)[: len(LOG_KEY_ORDER)] == LOG_KEY_ORDER


def test_a_diverted_measurement_appears_even_above_the_configured_severity() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(cardinality_max=1, log_level=Severity.ERROR, stream=sink)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "admitted"})
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "diverted"})
    assert len(_records(sink)) == 1


def test_a_bound_below_one_is_refused() -> None:
    for maximum in (0, -1):
        with pytest.raises(ValueError, match="cardinality"):
            Telemetry(cardinality_max=maximum)


def test_a_disabled_instance_records_nothing_at_all() -> None:
    sink = io.StringIO()
    telemetry = Telemetry(cardinality_max=1, disabled=True, stream=sink)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "one"})
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "two"})
    telemetry.log(Severity.ERROR, COMPONENT, MESSAGE)
    assert telemetry.combinations() == ()
    assert telemetry.overflow_count() == 0.0
    assert telemetry.pending_samples() == ()
    assert sink.getvalue() == ""


def test_the_counters_view_is_a_copy_rather_than_the_live_mapping() -> None:
    sink = io.StringIO()
    telemetry = _telemetry(sink, cardinality_max=2)
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "one"})
    before = telemetry.counters()
    telemetry.metric(METRIC_NAME, 1.0, **{DIMENSION_NAME: "two"})
    assert len(before) == 1
    assert len(telemetry.counters()) == 2


def test_the_process_wide_instance_carries_the_documented_defaults() -> None:
    reset()
    try:
        instance = current()
        assert instance is current()
        assert instance.namespace == DEFAULT_NAMESPACE
        assert instance.cardinality_max == DEFAULT_CARDINALITY_MAX
    finally:
        reset()
