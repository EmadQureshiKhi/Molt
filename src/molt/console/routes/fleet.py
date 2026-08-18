"""The fleet overview: every permitted Client's Sessions, one table.

The view answers one question an operator asks first: what has been running, for whom,
on which machine, how deep, and at what cost. It answers it from the Session rows
themselves, which carry the counters and the accrued cost, so the overview costs one
read per permitted Client rather than one per Session.

**Tenancy is the roster, and the roster is the cluster's.** The Client set is read from
the Client table, a request narrows it by naming a slug that is matched against those
rows, and each Session read is `sessions_of_client` scoped by an identifier that came
from a roster row. There is no read here that takes a Client identifier from the
caller, so a query string cannot reach a tenant the roster does not hold.

**A row's Event count is the Session's own counters rather than a per-Session count.**
Counting a Session's Ledger rows would be one read per row of the table, and the
Session row already carries what those counters are for. The per-Session view is where
the Event stream itself is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from starlette.requests import Request
from starlette.responses import Response

from molt.console.app import console_of
from molt.console.routes.tenancy import ClientChoice, client_roster, render, selected_client
from molt.console.routing import register
from molt.models.session import Session
from molt.store.sessions import sessions_of_client

__all__ = ["FLEET_TEMPLATE", "SESSIONS_PER_CLIENT", "FleetRow", "fleet", "fleet_rows"]

# The template this view renders, and how many Sessions are read per permitted Client.
# The bound is per Client rather than over the whole fleet, so one busy tenant cannot
# crowd every other tenant out of the overview.
FLEET_TEMPLATE: Final[str] = "fleet.html"
SESSIONS_PER_CLIENT: Final[int] = 50


@dataclass(frozen=True, slots=True)
class FleetRow:
    """One Session of the overview, joined to the Client whose roster row read it."""

    client: ClientChoice
    session: Session

    @property
    def running(self) -> bool:
        """Whether this Session has not yet been closed."""
        return self.session.ended_at is None

    @property
    def standing(self) -> str:
        """The text label of the row's standing, so colour is never the only channel."""
        if self.session.halted:
            return "halted"
        return "running" if self.running else str(self.session.outcome)

    @property
    def activity(self) -> int:
        """The Events this Session accounts for: its tool calls and its model requests."""
        return self.session.tool_call_count + self.session.model_request_count

    @property
    def cost(self) -> Decimal:
        """The cost this Session accrued, as the row holds it."""
        return self.session.cost_usd


def fleet_rows(
    request: Request, roster: tuple[ClientChoice, ...], chosen: ClientChoice | None
) -> tuple[FleetRow, ...]:
    """Read the Sessions of every permitted Client, most recently started first.

    One read per Client, each scoped by that Client's own identifier. The rows are
    ordered across Clients afterwards so the overview reads as one fleet rather than as
    a sequence of per-tenant blocks.
    """
    store = console_of(request).read_only_store()
    covered = (chosen,) if chosen is not None else roster
    rows: list[FleetRow] = []
    for choice in covered:
        for record in sessions_of_client(store, choice.id, limit=SESSIONS_PER_CLIENT):
            rows.append(FleetRow(client=choice, session=record))
    rows.sort(key=lambda row: (row.session.started_at, str(row.session.id)), reverse=True)
    return tuple(rows)


@register("fleet")
async def fleet(request: Request) -> Response:
    """Serve the fleet overview, narrowed to one permitted Client when one is named."""
    console = console_of(request)
    roster = client_roster(console)
    chosen, recognised = selected_client(request, roster)
    rows = () if not recognised else fleet_rows(request, roster, chosen)
    return render(
        request,
        FLEET_TEMPLATE,
        {
            "title": "Fleet",
            "roster": roster,
            "chosen": chosen,
            "filter_recognised": recognised,
            "rows": rows,
        },
    )
