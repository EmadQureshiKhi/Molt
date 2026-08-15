"""The metric inventory: every metric name the design's metrics table declares.

The inventory exists so that a metric a component emits and a metric a deployment
alarms on cannot disagree. One tuple states the name, the unit, the component that
emits it, and the dimensions it carries beyond `component`, and everything else --
delivery, alarm definitions, and the cross-check a test performs -- reads that one
tuple rather than restating a name.

Nothing here filters delivery. A name the inventory omits is still published, and
reported as a structured log record, because silently dropping a measurement a
component emits would hide the disagreement rather than surface it. The inventory
is the declaration; the report is the enforcement.

**High-cardinality dimensions are attached only where the breakdown earns its
place.** Each distinct combination of a name and its dimension values is a
separate billable metric, so `client_slug` and `agent_cli` appear on no entry
below, and the per-metric dimensions named here are the bounded ones: a
disposition, a residue band, a tool name, a verification outcome, a confidence
direction, and whether a checkpoint disagreement was explained.

**Two groups, and the boundary between them is the point.** The first group is the
design's metrics table, name for name and unit for unit: that table is what a
deployment alarms on, so a component that wanted a different spelling changes its
spelling rather than the table. The second group, declared separately below, is the
set of measurements components emit that the table does not name -- the per-batch
adjudication prefix count, the dropped-proxy-event count, the certificate issue and
object-write failure counts, the residue adjudication batch count, the two
sensitivity-grid counts, the two provider counts, the three recall degradation
counts, and the in-flight refusal count. Each is a real measurement at a real call
site, so declaring it is how the inventory stops reporting a disagreement that is
not one; keeping it out of the first group is how the table stays the authority on
what an alarm may be built against.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from molt.telemetry import UNIT_COUNT

__all__ = [
    "METRIC_INVENTORY",
    "METRIC_NAMES",
    "UNIT_BYTES",
    "UNIT_MILLISECONDS",
    "UNIT_NONE",
    "MetricDefinition",
    "declared_unit",
    "undeclared_names",
]

# The remaining units the inventory uses, spelled as the metrics service spells
# them. The count unit is the telemetry surface's own, imported rather than
# restated so one spelling governs both the surface and the inventory.
UNIT_MILLISECONDS: Final[str] = "Milliseconds"
UNIT_BYTES: Final[str] = "Bytes"

# The token meaning *this measurement carries no unit*, which is what an unknown
# unit is coerced to rather than being sent through and rejected.
UNIT_NONE: Final[str] = "None"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """One declared metric: its name, its unit, its emitter, and its dimensions."""

    name: str
    unit: str
    component: str
    dimensions: tuple[str, ...] = ()


_EXTENSIONS: Final[tuple[MetricDefinition, ...]] = (
    MetricDefinition("adjudication.prefix_below_floor_batches", UNIT_COUNT, "adjudicator"),
    MetricDefinition("capture.mcp_dropped_events", UNIT_COUNT, "capture_hook"),
    MetricDefinition("certificate.issued", UNIT_COUNT, "certificate_builder"),
    MetricDefinition("certificate.storage_failures", UNIT_COUNT, "certificate_builder"),
    MetricDefinition("erasure.residue_adjudication_batches", UNIT_COUNT, "residue_detector"),
    MetricDefinition("erasure.sensitivity_candidates", UNIT_COUNT, "sensitivity_analyzer"),
    MetricDefinition("erasure.sensitivity_pairs", UNIT_COUNT, "sensitivity_analyzer"),
    MetricDefinition("provider.call_failure", UNIT_COUNT, "provider", ("provider",)),
    MetricDefinition("provider.embedding_group", UNIT_COUNT, "provider", ("provider",)),
    MetricDefinition("recall.candidate_pool_saturated", UNIT_COUNT, "recall_engine"),
    MetricDefinition("recall.event_write_failures", UNIT_COUNT, "recall_engine"),
    MetricDefinition("recall.unavailable", UNIT_COUNT, "recall_engine"),
    MetricDefinition("store.erasure_in_flight_refused", UNIT_COUNT, "store"),
)


METRIC_INVENTORY: Final[tuple[MetricDefinition, ...]] = (
    MetricDefinition("collector.events_accepted", UNIT_COUNT, "collector"),
    MetricDefinition("collector.events_rejected", UNIT_COUNT, "collector"),
    MetricDefinition("collector.write_failure", UNIT_COUNT, "collector"),
    MetricDefinition("collector.batch_latency_ms", UNIT_MILLISECONDS, "collector"),
    MetricDefinition("collector.signature_rejected", UNIT_COUNT, "collector"),
    MetricDefinition("capture.spool_bytes", UNIT_BYTES, "capture_hook"),
    MetricDefinition("capture.spool_discarded", UNIT_COUNT, "capture_hook"),
    MetricDefinition("embedder.calls", UNIT_COUNT, "embedder"),
    MetricDefinition("embedder.failures", UNIT_COUNT, "embedder"),
    MetricDefinition("embedder.pending_backlog", UNIT_COUNT, "embedder"),
    MetricDefinition("adjudication.cache_creation_tokens", UNIT_COUNT, "adjudicator"),
    MetricDefinition("adjudication.cache_read_tokens", UNIT_COUNT, "adjudicator"),
    MetricDefinition("store.vector_index_unavailable", UNIT_COUNT, "store"),
    MetricDefinition("store.serialization_retries", UNIT_COUNT, "store"),
    MetricDefinition("store.serialization_exhausted", UNIT_COUNT, "store"),
    MetricDefinition("mcp.tool_invocations", UNIT_COUNT, "mcp_server", ("tool",)),
    MetricDefinition("telemetry.cardinality_overflow", UNIT_COUNT, "telemetry"),
    MetricDefinition("recall.queries", UNIT_COUNT, "recall_engine"),
    MetricDefinition("recall.latency_ms", UNIT_MILLISECONDS, "recall_engine"),
    MetricDefinition("erasure.runs", UNIT_COUNT, "erasure_engine"),
    MetricDefinition("erasure.run_duration_ms", UNIT_MILLISECONDS, "erasure_engine"),
    MetricDefinition("erasure.dispositions", UNIT_COUNT, "erasure_engine", ("disposition",)),
    MetricDefinition("erasure.residue_candidates", UNIT_COUNT, "residue_detector", ("band",)),
    MetricDefinition("erasure.adjudication_fail_closed", UNIT_COUNT, "adjudicator"),
    MetricDefinition("erasure.redaction_fail_closed", UNIT_COUNT, "redaction_rewriter"),
    MetricDefinition("erasure.stale_generation_refused", UNIT_COUNT, "store"),
    MetricDefinition("erasure.lease_takeovers", UNIT_COUNT, "lease_manager"),
    MetricDefinition("erasure.working_rows_deleted", UNIT_COUNT, "erasure_engine"),
    MetricDefinition("attribution.supersessions", UNIT_COUNT, "store"),
    MetricDefinition("checkpoint.computed", UNIT_COUNT, "checkpoint_signer"),
    MetricDefinition(
        "checkpoint.verification_disagreements", UNIT_COUNT, "checkpoint_signer", ("explained",)
    ),
    MetricDefinition(
        "procedure.confidence_changes", UNIT_COUNT, "confidence_tracker", ("direction",)
    ),
    MetricDefinition("procedure.recall_floor_exclusions", UNIT_COUNT, "recall_engine"),
    MetricDefinition("certificate.verifications", UNIT_COUNT, "certificate_verifier", ("outcome",)),
    MetricDefinition("watcher.mutations_consumed", UNIT_COUNT, "policy_watcher"),
    MetricDefinition("watcher.degraded_to_polling", UNIT_COUNT, "policy_watcher"),
    MetricDefinition("watcher.halts", UNIT_COUNT, "policy_watcher"),
    MetricDefinition("watcher.approvals_raised", UNIT_COUNT, "policy_watcher"),
    *_EXTENSIONS,
)

# Every declared name, for the membership question a caller actually asks.
METRIC_NAMES: Final[frozenset[str]] = frozenset(entry.name for entry in METRIC_INVENTORY)

_BY_NAME: Final[dict[str, MetricDefinition]] = {entry.name: entry for entry in METRIC_INVENTORY}


def declared_unit(name: str) -> str | None:
    """The unit the inventory declares for a name, or None when it declares none."""
    entry = _BY_NAME.get(name)
    return None if entry is None else entry.unit


def undeclared_names(names: Iterable[str]) -> tuple[str, ...]:
    """The names among those given that the inventory does not declare, sorted."""
    return tuple(sorted({name for name in names if name not in METRIC_NAMES}))
