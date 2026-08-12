"""The residue view: a read-only semantic search that mutates nothing.

The whole point of this view is what it does *not* do. It runs the Residue_Detector's
own walk through `residue_report`, which is the same walk the erasure path takes with
recording suppressed, so a band shown here and a band recorded by a run cannot come
to mean different things. No transaction is opened for a write, no finding row is
inserted, and the run identifier the walk is given belongs to no stored run unless
the operator names one, in which case the walk reads that run's candidate set and
still records nothing.

**The thresholds are the operator's, resolved through the configuration surface.**
Both arrive as native number inputs and are applied by replacing the resolved policy
values rather than by a second default spelled here, so the surface stays the one
place a default lives and a submitted value out of range is refused by the policy's
own validation rather than by a check written twice.

**The Client is a native select over the fleet.** The permitted set the neighbour
query may return content for is the selected Client, which is what the CLI verb
passes as well.

Every statement is a whole module-level literal with bound parameters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final
from uuid import UUID, uuid4

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from molt.console import routing
from molt.console.app import console_of, session_of
from molt.console.routes.erasure_common import (
    UNAVAILABLE_STATUS,
    ClientRow,
    configuration_of,
    fleet,
    templates_of,
)
from molt.erase.residue import ResidueFinding, ResiduePolicy, residue_report
from molt.store import MemoryStore
from molt.telemetry import Severity, log

__all__ = ["RESIDUE_TEMPLATE", "ResidueView", "residue", "residue_view"]

# The template this view renders, named here so a test asserts one name.
RESIDUE_TEMPLATE: Final[str] = "residue.html"

# The submitted field names, as fixed strings so the template and the handler cannot
# disagree about what a control is called.
CLIENT_FIELD: Final[str] = "client"
RUN_FIELD: Final[str] = "run"
AUTO_INCLUDE_FIELD: Final[str] = "auto_include_threshold"
REVIEW_FIELD: Final[str] = "review_threshold"

_COMPONENT: Final[str] = "console"


@dataclass(frozen=True, slots=True)
class ResidueView:
    """Everything the template renders, assembled before any rendering happens.

    Held as a value rather than as a mapping built inline so the view is assertable
    without a template: a test drives `residue_view` and reads the findings, and the
    template then has nothing in it but presentation.
    """

    clients: tuple[ClientRow, ...]
    selected_client: str
    run_text: str
    policy: ResiduePolicy
    findings: tuple[Mapping[str, object], ...]
    candidate_count: int
    included_count: int
    read_only: bool
    searched: bool
    refusal: str | None


def residue_view(
    store: MemoryStore,
    policy: ResiduePolicy,
    submitted: Mapping[str, str],
) -> ResidueView:
    """Assemble the view from the submitted search, reading and writing nothing else.

    A search runs only when a Client of the fleet is named. Anything else renders the
    form with the reason stated, because a search over an unnamed Client would either
    have to guess a tenant or read the whole fleet.
    """
    clients = fleet(store)
    slug = submitted.get(CLIENT_FIELD, "").strip()
    run_text = submitted.get(RUN_FIELD, "").strip()
    resolved = _resolved_policy(policy, submitted)
    if isinstance(resolved, str):
        return _unsearched(clients, slug, run_text, policy, resolved)
    if not slug:
        return _unsearched(clients, slug, run_text, resolved, None)
    chosen = next((row for row in clients if row.slug == slug), None)
    if chosen is None:
        return _unsearched(clients, slug, run_text, resolved, "no Client carries that slug")
    run_id = _run_identifier(run_text)
    if run_id is None:
        return _unsearched(clients, slug, run_text, resolved, "that run identifier is not one")

    report = residue_report(
        store,
        run_id,
        resolved,
        permitted_clients=(chosen.client_id,),
        adjudicator=None,
    )
    log(
        Severity.INFO,
        _COMPONENT,
        "ran a read-only residue search",
        client_slug=slug,
        candidate_count=len(report.candidate_ids),
        read_only=report.read_only,
    )
    return ResidueView(
        clients=clients,
        selected_client=slug,
        run_text=run_text,
        policy=resolved,
        findings=tuple(_finding_row(finding) for finding in report.findings),
        candidate_count=len(report.candidate_ids),
        included_count=len(report.included_ids),
        read_only=report.read_only,
        searched=True,
        refusal=None,
    )


def _unsearched(
    clients: Sequence[ClientRow],
    slug: str,
    run_text: str,
    policy: ResiduePolicy,
    refusal: str | None,
) -> ResidueView:
    """The form on its own, with the reason no search ran where there is one."""
    return ResidueView(
        clients=tuple(clients),
        selected_client=slug,
        run_text=run_text,
        policy=policy,
        findings=(),
        candidate_count=0,
        included_count=0,
        read_only=True,
        searched=False,
        refusal=refusal,
    )


def _finding_row(finding: ResidueFinding) -> Mapping[str, object]:
    """One finding as the template reads it, with the distance already formatted."""
    return {
        "artifact_id": str(finding.artifact_id),
        "artifact_kind": str(finding.artifact_kind),
        "query_artifact_id": str(finding.query_artifact_id),
        "cosine_distance": format(finding.cosine_distance, ".6f"),
        "band": str(finding.band),
        "included": finding.included,
        "adjudicated": finding.adjudicated,
        "decision_reason": finding.decision_reason,
    }


def _resolved_policy(policy: ResiduePolicy, submitted: Mapping[str, str]) -> ResiduePolicy | str:
    """The policy with the submitted thresholds applied, or the reason they were refused.

    The policy's own validation is what refuses an unusable pair, so the ordering
    rule lives in one place rather than being restated here.
    """
    auto = _number(submitted.get(AUTO_INCLUDE_FIELD), policy.auto_include_threshold)
    review = _number(submitted.get(REVIEW_FIELD), policy.review_threshold)
    if auto is None or review is None:
        return "a threshold was not a number"
    try:
        return replace(policy, auto_include_threshold=auto, review_threshold=review)
    except ValueError:
        return "those thresholds describe no band"


def _number(text: str | None, held: float) -> float | None:
    """One submitted number, the held value where nothing was submitted, or None."""
    if text is None or not text.strip():
        return held
    try:
        return float(text)
    except ValueError:
        return None


def _run_identifier(text: str) -> UUID | None:
    """The named run's identifier, or a synthetic one naming no stored run.

    A read-only walk still takes a run identifier, because the walk reads the
    candidate set of a run. An operator naming none gets an identifier that belongs
    to no run at all, which is exactly what the CLI verb presents, and nothing is
    written under it by either path.
    """
    if not text:
        return uuid4()
    try:
        return UUID(text)
    except ValueError:
        return None


@routing.register("residue")
async def residue(request: Request) -> Response:
    """Render the residue search form and, where one was asked for, its results."""
    console = console_of(request)
    templates = templates_of(request)
    if templates is None:
        return PlainTextResponse(
            "the console templates are not available", status_code=UNAVAILABLE_STATUS
        )
    policy = ResiduePolicy.from_configuration(configuration_of(request))
    view = residue_view(console.store, policy, dict(request.query_params))
    session = session_of(request)
    return templates.TemplateResponse(
        request,
        RESIDUE_TEMPLATE,
        {
            "title": "Semantic residue",
            "demo_mode": console.demo_mode,
            "authenticated": session is not None,
            "csrf_token": "" if session is None else session.csrf_token,
            "view": view,
            "client_field": CLIENT_FIELD,
            "run_field": RUN_FIELD,
            "auto_include_field": AUTO_INCLUDE_FIELD,
            "review_field": REVIEW_FIELD,
        },
    )
