"""The Threshold_Grid view: what each threshold pair would have swept, as a table.

The grid's shape is the shape of the decision, so the rendering is a two-axis data
table rather than a list of pairs: auto-inclusion thresholds are the row headers and
review thresholds are the column headers, and a reader compares along either axis by
reading along a line. Both header directions are marked, so a screen reader announces
which pair a cell belongs to rather than reading four bare numbers.

**Nothing is computed here.** The report comes from the Sensitivity_Analyzer, which
runs one residue search, answers every pair by counting against the retained set, and
refuses a pass that recorded anything or adjudicated anything. This module resolves
the Client, asks for the report, and arranges the outcomes into rows.

**An inapplicable pair is rendered, not skipped.** A pair whose auto-inclusion
threshold stands above its review threshold carries a reason and no counts at all, and
the cell renders the word `inapplicable` with that reason. Rendering a zero there would
read as *this pair selected nothing* when the truth is *this pair is meaningless*, so
absence is rendered as absence. The grid therefore stays rectangular: every row carries
one cell per column, whatever the pair means.

**The view is read-only in every mode.** No handler here opens a write transaction,
and the analyser refuses a store whose connection does not authenticate as the
read-only role, so the no-mutation claim is a privilege fact rather than a discipline
of this module. A store holding a wider role is reported as a refusal naming the role
the analysis requires, rather than being run with the privilege it happens to hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast
from uuid import UUID

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.templating import Jinja2Templates

from molt.cli.verbs.common import synthetic_run_id
from molt.config.resolve import load_configuration
from molt.console import routing
from molt.console.app import console_of, session_of
from molt.erase.sensitivity import (
    READ_ONLY_ROLE,
    PairOutcome,
    SensitivityReport,
    ThresholdPair,
    analyse_client,
    default_grid,
)
from molt.errors import MoltError
from molt.mcpserver.tools import permitted_client_ids
from molt.telemetry import Severity, log

__all__ = [
    "CLIENT_FIELD",
    "COMPONENT",
    "INAPPLICABLE_LABEL",
    "TEMPLATE",
    "GridCell",
    "GridRow",
    "grid_rows",
    "sensitivity_view",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "console"

# The template this view renders, the query field naming the Client the corpus is
# drawn from, and the word an inapplicable cell renders. The word is a constant
# because the accessibility test asserts on it and a template spelling of its own
# would be a second place to change.
TEMPLATE: Final[str] = "sensitivity.html"
CLIENT_FIELD: Final[str] = "client"
INAPPLICABLE_LABEL: Final[str] = "inapplicable"

_UNAVAILABLE_STATUS: Final[int] = 503


@dataclass(frozen=True, slots=True)
class GridCell:
    """One rendered cell: either four counts, or the word inapplicable and its reason.

    The counts are optional rather than defaulted to zero, because the difference
    between *no candidate fell in this band* and *this band does not exist* is the one
    thing a reader of this grid must not lose.
    """

    review_threshold: float
    applicable: bool
    candidate_count: int | None
    auto_included_count: int | None
    referred_count: int | None
    recovered_count: int | None
    reason: str | None

    @classmethod
    def of(cls, outcome: PairOutcome) -> GridCell:
        """The cell one pair's outcome renders as."""
        return cls(
            review_threshold=outcome.review_threshold,
            applicable=outcome.applicable,
            candidate_count=outcome.candidate_count,
            auto_included_count=outcome.auto_included_count,
            referred_count=outcome.referred_count,
            recovered_count=outcome.recovered_count,
            reason=outcome.inapplicable_reason,
        )


@dataclass(frozen=True, slots=True)
class GridRow:
    """One rendered row: an auto-inclusion threshold and one cell per column."""

    auto_include_threshold: float
    cells: tuple[GridCell, ...]


def grid_rows(report: SensitivityReport) -> tuple[GridRow, ...]:
    """Arrange one report's outcomes into rows, one cell per column, in axis order.

    The lookup is by pair rather than by position, so a reordering of the outcomes
    could not silently transpose the table, and every row carries one cell per column
    because the columns are read off the grid the report answered.
    """
    columns = report.grid.review_axis
    rows: list[GridRow] = []
    for auto in report.grid.auto_include_axis:
        cells = tuple(
            GridCell.of(
                report.outcome_for(
                    ThresholdPair(auto_include_threshold=auto, review_threshold=review)
                )
            )
            for review in columns
        )
        rows.append(GridRow(auto_include_threshold=auto, cells=cells))
    return tuple(rows)


@routing.register("sensitivity")
async def sensitivity_view(request: Request) -> Response:
    """Render the grid for one Client, or the picker alone when none is named.

    The Client is a query field rather than a path parameter because the view is one
    page an operator re-asks with a different Client, and a GET form carries no
    mutation and needs no token.
    """
    slug = request.query_params.get(CLIENT_FIELD, "").strip()
    context: dict[str, object] = {
        "title": "Threshold sensitivity",
        "client_field": CLIENT_FIELD,
        "client_slug": slug,
        "inapplicable_label": INAPPLICABLE_LABEL,
        "read_only_role": READ_ONLY_ROLE,
        "report": None,
        "rows": (),
        "columns": (),
        "notice": None,
    }
    if slug:
        # `ReaderRoleUnavailableError` is a `MoltError`, so a deployment configuring no
        # read-only connection renders the reason in the notice rather than a stack
        # trace, and the grid says it is unavailable instead of claiming the analysis
        # found nothing.
        try:
            report = _report_for(request, slug)
        except MoltError as error:
            log(
                Severity.INFO,
                COMPONENT,
                "the sensitivity grid could not be rendered",
                error_type=type(error).__name__,
            )
            context["notice"] = str(error)
        else:
            context["report"] = report
            context["rows"] = grid_rows(report)
            context["columns"] = report.grid.review_axis
    return _page(request, context)


def _report_for(request: Request, slug: str) -> SensitivityReport:
    """The report for one Client, read through the analyser and nothing else.

    The run identifier is synthetic because nothing is being erased: the analysis
    needs a candidate set to draw its query Artifacts against and writes no run row.
    """
    # The analysis insists on a connection that authenticates as the read-only role, so
    # it is handed the console's reader handle rather than the console's own store: the
    # deployed function holds the eraser role because the erasure console shares it.
    # Where the console is already read-only this is the same handle.
    store = console_of(request).reader_store()
    configuration = load_configuration()
    resolved = permitted_client_ids(store, (slug,))
    if not resolved:
        raise MoltError("that name matches no stored Client")
    client_id: UUID = resolved[0]
    return analyse_client(
        store,
        synthetic_run_id(),
        permitted_clients=(client_id,),
        configuration=configuration,
        grid=default_grid(configuration),
    )


def _page(request: Request, context: dict[str, object]) -> Response:
    """Render this view's template with the fields the layout expects.

    The session and the mode flag are added here rather than by every caller, so the
    navigation and the mode banner of the shared layout render the same way on every
    page of this view.
    """
    templates = cast("Jinja2Templates | None", getattr(request.app.state, "molt_templates", None))
    if templates is None:
        return PlainTextResponse(
            "the console templates are not available", status_code=_UNAVAILABLE_STATUS
        )
    session = session_of(request)
    return templates.TemplateResponse(
        request,
        TEMPLATE,
        context
        | {
            "demo_mode": console_of(request).demo_mode,
            "authenticated": session is not None,
            "csrf_token": "" if session is None else session.csrf_token,
        },
    )
