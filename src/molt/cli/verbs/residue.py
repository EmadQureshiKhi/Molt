"""The residue verb: the read-only walk, reported and never recorded.

The walk is the same one the erasure path runs, taken with recording suppressed
against a synthetic run identifier, so a reported band and a recorded band cannot
come to mean different things. Nothing about this verb mutates memory content.
"""

from __future__ import annotations

from molt.cli.context import VerbContext, client_id_for
from molt.cli.exits import ExitCode
from molt.cli.verbs.common import (
    QUERY_LIMIT_KEY,
    TOP_K_KEY,
    integer_overrides,
    synthetic_run_id,
    threshold_overrides,
)
from molt.erase.residue import ResiduePolicy, residue_report
from molt.models.event import JsonObject
from molt.store import MemoryStore

__all__ = ["run"]


def run(context: VerbContext) -> ExitCode:
    """Report residue candidates with their distances, bands, and decisions."""
    emitter = context.emitter
    overrides = threshold_overrides(context)
    overrides.update(
        integer_overrides(context, {"limit": QUERY_LIMIT_KEY, "top_k": TOP_K_KEY}),
    )
    configuration = context.configuration_for(overrides)
    policy = ResiduePolicy.from_configuration(configuration)

    with MemoryStore.from_configuration(configuration) as store:
        client_id = client_id_for(store, context.required_text("client"))
        report = residue_report(
            store,
            synthetic_run_id(),
            policy,
            permitted_clients=(client_id,),
            adjudicator=None,
        )

    findings: list[JsonObject] = []
    for finding in report.findings:
        emitter.narrate(
            f"{finding.artifact_id} {finding.cosine_distance:.4f} {finding.band} "
            f"{'included' if finding.included else 'retained'} {finding.decision_reason}"
        )
        findings.append(
            {
                "artifact_id": str(finding.artifact_id),
                "artifact_kind": str(finding.artifact_kind),
                "cosine_distance": finding.cosine_distance,
                "band": str(finding.band),
                "included": finding.included,
                "adjudicated": finding.adjudicated,
                "decision_reason": finding.decision_reason,
            }
        )
    return emitter.succeed(
        context.name,
        {
            "read_only": report.read_only,
            "auto_include_threshold": policy.auto_include_threshold,
            "review_threshold": policy.review_threshold,
            "candidate_count": len(report.candidate_ids),
            "included_count": len(report.included_ids),
            "adjudication_batches": report.adjudication_batches,
            "findings": findings,
        },
    )
