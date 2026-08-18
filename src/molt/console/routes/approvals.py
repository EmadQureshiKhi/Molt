"""The Approval_Queue: the list an operator reads, and the resolution they submit.

Two routes the table already declares, claimed by name: `GET /approvals` and
`POST /approvals/{approval_id}`. Neither declares a path, a method, or an
authentication requirement of its own.

**The queue is read scoped to the permitted Client set, inside the statement.** An
entry names the Session it was raised for and a Session names its Client, so the
tenancy predicate is a bound identifier set in the read rather than a filter over its
answer. The roster those identifiers come from is the Client table, read the way every
other view reads it, and demonstration mode narrows what a visitor may read through the
same roster.

**The listing reads through the narrowest handle, the resolution through the wider one.**
The deployed function authenticates as the eraser role, so a view that writes nothing
would otherwise read through a role that can write for no reason beyond which handle was
in scope. `GET /approvals` therefore takes `read_only_store()`, which is the read-only
connection where the deployment configures one and the primary handle where it does not,
so a single-connection deployment keeps the queue rather than losing it to a privilege it
never needed. The resolution keeps the primary handle for both of its statements, the
tenancy check included: that check is the precondition of the write it guards rather than
a view of its own, so the row admitting the resolution is one the writing role can itself
see, and no read-only connection is opened inside a write path to read a single row. The
resolution's atomicity rests on the guarded update instead, which is the same choice
`POST /erase` makes for the fleet lookup its run depends on.

**A resolved entry stays on the list.** The principal, the decision, and the instant are
the evidence that the entry was answered, and a list that dropped a resolved entry would
leave nowhere to read that evidence from. Pending entries lead, because those are the
rows an operator came to act on.

**The vocabulary is the schema's, not this module's.** An entry is `pending` or
`resolved` and a decision is `approved` or `denied`, which is what the `approval_queue`
check constraints admit; those two enumerations are imported from the policy component,
which asserts at import that they match the constraint in its order. A third word
offered by a submission is refused rather than coerced to one of the two.

**Two refusals guard the resolution and neither is re-implemented here.** The route
table marks `approval_resolve` a mutation, so the authentication middleware refuses it
without this session's own CSRF token before this module is reached; and the table
classifies it as blocked in demonstration mode, so the demonstration middleware refuses
it ahead of routing. What this module adds is the tenancy check and the vocabulary
check, both of which are about *which* entry is resolved and *to what*, rather than
about who may resolve anything.

**A resolution that changed nothing is not reported as one that did.** An entry already
resolved keeps the first principal's decision, and the answer is the queue again, where
that standing decision is what the row shows. Overwriting it, or claiming a second
resolution happened, would misreport who is accountable for the entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from molt.console.app import console_of, form_values, session_of
from molt.console.demo import (
    BLOCKED_EXPLANATION,
    control_disabled,
    demonstration_context,
)
from molt.console.deps import COMPONENT
from molt.console.routes.tenancy import (
    ClientChoice,
    client_roster,
    identifier_of,
    permitted_ids,
    render,
)
from molt.console.routing import register
from molt.policy.apply import (
    QUEUE_PAGE_LIMIT,
    ApprovalDecision,
    QueuedApproval,
    queued_approval,
    queued_approvals,
    resolve_approval,
)
from molt.telemetry import Severity, log

__all__ = [
    "APPROVALS_PATH",
    "APPROVAL_TEMPLATE",
    "DECISION_FIELD",
    "RESOLVE_ROUTE_NAME",
    "QueueRow",
    "approval_resolve",
    "approvals_view",
    "queue_rows",
]

# The template this view renders, and where a resolution sends the operator back to.
APPROVAL_TEMPLATE: Final[str] = "approvals.html"
APPROVALS_PATH: Final[str] = "/approvals"

# The submitted field naming the decision, and the route whose disposition decides
# whether the resolution controls render disabled.
DECISION_FIELD: Final[str] = "decision"
RESOLVE_ROUTE_NAME: Final[str] = "approval_resolve"

_SEE_OTHER: Final[int] = 303
_BAD_REQUEST: Final[int] = 400
_FORBIDDEN_STATUS: Final[int] = 403
_NOT_FOUND_STATUS: Final[int] = 404

_NOT_FOUND: Final[dict[str, str]] = {"error": "no such queued approval"}
_FORBIDDEN: Final[dict[str, str]] = {"error": "forbidden"}


@dataclass(frozen=True, slots=True)
class QueueRow:
    """One rendered row: a queue entry beside the label of the Client it belongs to."""

    entry: QueuedApproval
    client_label: str

    @property
    def pending(self) -> bool:
        """Whether this row still awaits an operator, which is what carries controls."""
        return self.entry.pending


def queue_rows(
    entries: tuple[QueuedApproval, ...], roster: tuple[ClientChoice, ...]
) -> tuple[QueueRow, ...]:
    """Label each entry with its Client, from the roster the read was scoped by.

    The label comes from the roster rather than from a second read, so a rendered row
    names a tenant this console may see by construction.
    """
    labels = {choice.id: choice.label for choice in roster}
    return tuple(
        QueueRow(entry=entry, client_label=labels.get(entry.client_id, "")) for entry in entries
    )


@register("approvals")
async def approvals_view(request: Request) -> Response:
    """Render the queue of every permitted Client, pending entries first.

    In demonstration mode the resolution controls render disabled with the stated
    explanation rather than absent, so the queue shows what it would do without doing
    it.
    """
    console = console_of(request)
    roster = client_roster(console)
    entries: tuple[QueuedApproval, ...] = ()
    notice: str | None = None
    try:
        entries = queued_approvals(console.read_only_store(), permitted_ids(roster, None))
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "the approval queue could not be read, so no entry is rendered",
            error_type=type(error).__name__,
        )
        notice = "the approval queue could not be read from the cluster"
    rows = queue_rows(entries, roster)
    context: dict[str, object] = {
        "title": "Approvals",
        "roster": roster,
        "rows": rows,
        "pending_count": len([row for row in rows if row.pending]),
        "page_limit": QUEUE_PAGE_LIMIT,
        "decision_field": DECISION_FIELD,
        "approved_value": ApprovalDecision.APPROVED.value,
        "denied_value": ApprovalDecision.DENIED.value,
        "controls_disabled": control_disabled(RESOLVE_ROUTE_NAME, demo_mode=console.demo_mode),
        "blocked_explanation": BLOCKED_EXPLANATION,
        "notice": notice,
    }
    return render(request, APPROVAL_TEMPLATE, demonstration_context(request) | context)


@register("approval_resolve")
async def approval_resolve(request: Request) -> Response:
    """Resolve one queued approval and answer with the queue again.

    The principal recorded is the subject of the verified session, so the entry names
    who answered it rather than merely that it was answered.
    """
    console = console_of(request)
    identifier = identifier_of(str(request.path_params.get("approval_id", "")))
    if identifier is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    session = session_of(request)
    if session is None:
        # Unreachable through the built application, because the table declares this
        # route as requiring a session and the middleware refuses it without one. It is
        # answered rather than assumed away, because a resolution recorded against no
        # principal would be a decision nobody is accountable for.
        return JSONResponse(dict(_FORBIDDEN), status_code=_FORBIDDEN_STATUS)
    submitted = await form_values(request)
    decision = _decision_of(submitted.get(DECISION_FIELD))
    if decision is None:
        return JSONResponse(
            {
                "error": "a resolution names one of the decisions the schema admits",
                "decisions": [member.value for member in ApprovalDecision],
            },
            status_code=_BAD_REQUEST,
        )
    entry = _permitted_entry(request, identifier)
    if entry is None:
        log(Severity.INFO, COMPONENT, "a resolution named an approval outside the roster")
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    resolved = resolve_approval(
        console.store,
        identifier,
        principal=session.subject,
        decision=decision,
        clock=console.now,
    )
    log(
        Severity.INFO,
        COMPONENT,
        "a queued approval was resolved from the console"
        if resolved is not None
        else "a queued approval was already resolved, so the first decision stands",
        decision=decision.value,
        rule_id=str(entry.rule_id),
    )
    return RedirectResponse(APPROVALS_PATH, status_code=_SEE_OTHER)


def _permitted_entry(request: Request, approval_id: UUID) -> QueuedApproval | None:
    """The entry a resolution names, admitted by the roster, or None where none is.

    An entry no permitted Client's Session holds is answered exactly as one that does
    not exist, so a submitted identifier cannot report whether such an entry is stored.
    """
    console = console_of(request)
    roster = client_roster(console)
    # The primary handle, deliberately, though this reads and writes nothing. The rule
    # `Console.read_only_store()` states is a rule about views: a view registering no
    # mutation route reads through the narrow handle, so its read-only posture is a
    # privilege rather than a habit. This is not such a view — it is the precondition of
    # the resolution below, reached from that route and from nowhere else — and the
    # posture it would be claiming is one the same request breaks two statements later.
    # Keeping it here means the row admitting the resolution is a row the writing role
    # can itself see, rather than one a second connection's grants decided on; and it
    # opens no read-only connection inside a write path for the sake of one row. The
    # atomicity of the resolution does not rest on this read either way: the update is
    # guarded on the entry still being pending, inside its own serializable transaction.
    return queued_approval(console.store, approval_id, permitted_ids(roster, None))


def _decision_of(submitted: str | None) -> ApprovalDecision | None:
    """One submitted decision, or None when it is not one the schema admits."""
    if submitted is None:
        return None
    try:
        return ApprovalDecision(submitted.strip())
    except ValueError:
        return None
