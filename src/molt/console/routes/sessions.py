"""The per-Session view: the Event stream, the spawned children, and the chain status.

**The Session is found through the roster, not by identifier alone.** There is no read
in the store that fetches a Session by identifier without a Client, on purpose, and this
view keeps that property: it asks each permitted Client's scope in turn for the
identifier the path names, and the first scoped read that returns a row establishes both
the Session and the tenant it belongs to. A Session outside the roster is answered
exactly as one that does not exist, so the view discloses no more than a caller already
knew.

**The stream carries digests rather than payloads.** The tenancy-scoped Event read
returns the category, the timestamps, the linkage, the redaction flag, and the two
digests, which is what an operator needs to read a chain. No Event body is reached, so
the view cannot render memory content that the chain verification does not need.

**Chain verification is recomputed at request time.** The status shown is the outcome of
recomputing the chain over the rows the cluster holds now, including how far it held and
which stored field disagreed first, so the view reports evidence rather than a stored
claim. A cluster that does not answer the recomputation is reported as unverified rather
than as intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from molt.console.app import console_of
from molt.console.deps import COMPONENT, Console
from molt.console.routes.tenancy import (
    ClientChoice,
    client_roster,
    identifier_of,
    render,
)
from molt.console.routing import register
from molt.models.session import Session
from molt.store.chain import ChainReport, verify_chain
from molt.store.sessions import EventSummary, child_sessions, events_of_session, session_of_client
from molt.telemetry import Severity, log

__all__ = [
    "CHILDREN_LIMIT",
    "EVENTS_LIMIT",
    "SESSION_TEMPLATE",
    "SessionDetail",
    "chain_status",
    "detail_of",
    "session_detail",
]

SESSION_TEMPLATE: Final[str] = "session.html"

# How much of one Session's stream and progeny is read. Both are bounds on one
# Session rather than on the fleet, so the page stays a page.
EVENTS_LIMIT: Final[int] = 500
CHILDREN_LIMIT: Final[int] = 100

_NOT_FOUND: Final[dict[str, str]] = {"error": "no such Session"}
_NOT_FOUND_STATUS: Final[int] = 404

# What the view says when the recomputation could not be run at all. It is neither
# *intact* nor *broken*, and saying so is the honest third answer.
UNVERIFIED: Final[str] = "unverified"


@dataclass(frozen=True, slots=True)
class SessionDetail:
    """One Session as the view renders it: its tenant, its stream, and its children."""

    client: ClientChoice
    session: Session
    events: tuple[EventSummary, ...]
    children: tuple[Session, ...]
    report: ChainReport | None

    @property
    def chain_state(self) -> str:
        """The text label of the chain's standing, never a colour alone."""
        if self.report is None:
            return UNVERIFIED
        return "intact" if self.report.ok else "broken"


def detail_of(
    console: Console, session_id: UUID, roster: tuple[ClientChoice, ...]
) -> SessionDetail | None:
    """Read one Session through each permitted Client's scope until one answers.

    Every read is scoped: `session_of_client` names the tenant alongside the
    identifier, so an identifier belonging to a Client the roster does not hold matches
    no row in any of these reads.
    """
    for choice in roster:
        record = session_of_client(console.store, session_id, choice.id)
        if record is None:
            continue
        return SessionDetail(
            client=choice,
            session=record,
            events=events_of_session(console.store, session_id, choice.id, limit=EVENTS_LIMIT),
            children=child_sessions(console.store, session_id, choice.id, limit=CHILDREN_LIMIT),
            report=chain_status(console, session_id),
        )
    return None


def chain_status(console: Console, session_id: UUID) -> ChainReport | None:
    """Recompute this Session's chain, or report that it could not be recomputed.

    The recomputation is reached only after a scoped read established that the Session
    belongs to a permitted Client, so the unscoped verification is never the thing that
    decides what a caller may see.
    """
    try:
        return verify_chain(console.store, session_id)
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "a Session chain could not be verified, so the view reports it unverified",
            error_type=type(error).__name__,
        )
        return None


@register("session_detail")
async def session_detail(request: Request) -> Response:
    """Serve one Session's Event stream, its children, and its chain verification."""
    console = console_of(request)
    session_id = identifier_of(str(request.path_params.get("session_id", "")))
    if session_id is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    detail = detail_of(console, session_id, client_roster(console))
    if detail is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    return render(
        request,
        SESSION_TEMPLATE,
        {
            "title": "Session",
            "detail": detail,
            "unverified": UNVERIFIED,
        },
    )
