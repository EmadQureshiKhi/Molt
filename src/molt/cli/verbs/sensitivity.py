"""The sensitivity verb: the threshold grid, printed as a table and as one object.

The store is opened as the read-only role explicitly rather than as whatever the
surface names, because the no-mutation guarantee of this analysis is the privilege
and not the code path. An inapplicable pair prints its reason and no counts, so a
reader cannot mistake a meaningless cell for an empty one.
"""

from __future__ import annotations

from molt.cli.context import READER_ROLE, VerbContext, client_id_for
from molt.cli.exits import ExitCode
from molt.cli.verbs.common import synthetic_run_id
from molt.erase.sensitivity import (
    ThresholdGrid,
    analyse_client,
    default_grid,
    load_ground_truth,
)
from molt.models.event import JsonObject

__all__ = ["run"]

_HEADER = "auto  review  candidates  auto-included  referred  recovered"


def run(context: VerbContext) -> ExitCode:
    """Report every threshold pair's consequence over one Client's corpus."""
    emitter = context.emitter
    configuration = context.configuration
    grid = _grid(context)
    truth_path = context.path("ground_truth")
    ground_truth = None if truth_path is None else load_ground_truth(truth_path)

    with context.store(role=READER_ROLE) as store:
        client_id = client_id_for(store, context.required_text("client"))
        report = analyse_client(
            store,
            synthetic_run_id(),
            permitted_clients=(client_id,),
            configuration=configuration,
            grid=grid,
            ground_truth=ground_truth,
        )

    emitter.narrate(_HEADER)
    rows: list[JsonObject] = []
    for outcome in report.outcomes:
        if outcome.applicable:
            emitter.narrate(
                f"{outcome.auto_include_threshold:.2f}  {outcome.review_threshold:.2f}  "
                f"{outcome.candidate_count}  {outcome.auto_included_count}  "
                f"{outcome.referred_count}  {outcome.recovered_count}"
            )
        else:
            emitter.narrate(
                f"{outcome.auto_include_threshold:.2f}  {outcome.review_threshold:.2f}  "
                f"inapplicable: {outcome.inapplicable_reason}"
            )
        rows.append(
            {
                "auto_include_threshold": outcome.auto_include_threshold,
                "review_threshold": outcome.review_threshold,
                "candidate_count": outcome.candidate_count,
                "auto_included_count": outcome.auto_included_count,
                "referred_count": outcome.referred_count,
                "recovered_count": outcome.recovered_count,
                "inapplicable_reason": outcome.inapplicable_reason,
            }
        )
    return emitter.succeed(
        context.name,
        {
            "searches": report.searches,
            "ground_truth_available": report.ground_truth_available,
            "pairs": rows,
        },
    )


def _grid(context: VerbContext) -> ThresholdGrid | None:
    """The grid the flags name, or None to take the configured axes.

    A flag that names one axis alone leaves the other axis configured, so an
    operator can widen one dimension without restating the whole grid.
    """
    auto = context.numbers("auto_include_thresholds")
    review = context.numbers("review_thresholds")
    if not auto and not review:
        return None
    configured = default_grid(context.configuration)
    return ThresholdGrid.from_axes(
        auto_include_thresholds=auto or configured.auto_include_axis,
        review_thresholds=review or configured.review_axis,
    )
