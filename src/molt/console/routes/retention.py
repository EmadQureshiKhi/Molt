"""The retention view: each permitted Client's regime, and what is about to leave it.

One route the table already declares, claimed by name: `GET /retention`. The view
declares no path, no method, and no authentication requirement of its own.

**Nothing about retention is computed here.** The Jurisdiction, the interval, the
count expiring inside the reporting window, and the count already expired all come
from the Retention component's own report, which takes both counts against one bound
reading so they cannot describe two different presents. The interval applied where a
Client names none comes from the same surface key the ingest path reads, and the
expiry an Artifact written now would carry comes from the same arithmetic every write
path uses. A second spelling of any of the four would be a second answer an operator
could be given about one regime.

**The roster is the permitted Client set, and the report is narrowed to it.** The
report describes the Client table, which is the roster this console may see; a request
narrows that roster by naming a slug matched against its rows, and a line whose
identifier is not in the narrowed set is not rendered. So a query string cannot reach
a tenant the roster does not hold, and a Client the roster holds and the report does
not is rendered as a Client with no line rather than omitted in silence.

**The view opens no write transaction.** The report is one statement over the Client
table and the three content tables, and no handler here writes, so the page is
available unchanged in read-only demonstration mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from starlette.requests import Request
from starlette.responses import Response

from molt.console.app import console_of
from molt.console.deps import COMPONENT
from molt.console.routes.erasure_common import configuration_of
from molt.console.routes.tenancy import (
    ClientChoice,
    client_roster,
    render,
    selected_client,
)
from molt.console.routing import register
from molt.retention import (
    EXPIRING_SOON_WINDOW,
    ClientRetentionReport,
    default_interval,
    expiry_for,
    report,
)
from molt.telemetry import Severity, log

__all__ = [
    "RETENTION_TEMPLATE",
    "RetentionRow",
    "retention_rows",
    "retention_view",
]

# The template this view renders.
RETENTION_TEMPLATE: Final[str] = "retention.html"

# What a Client the report answered no line for renders as. It is a stated absence
# rather than a row of zeros, because a zero would claim the cluster holds nothing for
# that Client when the truth is that the report said nothing about it.
NO_LINE_NOTICE: Final[str] = "the retention report answered no line for this Client"


@dataclass(frozen=True, slots=True)
class RetentionRow:
    """One rendered row: a roster Client beside the report line that describes it.

    The line stays optional, because the roster and the report are two reads and a
    Client added between them has no line yet. The expiry is the instant an Artifact
    written at the reading would carry under this Client's own interval, computed with
    the arithmetic every write path uses rather than a second one of this module's.
    """

    client: ClientChoice
    line: ClientRetentionReport | None
    expiry_of_a_write_now: datetime | None

    @property
    def jurisdiction(self) -> str:
        """The Jurisdiction this Client's regime is configured under."""
        return "" if self.line is None else self.line.jurisdiction

    @property
    def interval(self) -> timedelta | None:
        """The retention interval the Client row holds, absent where no line was read."""
        return None if self.line is None else self.line.interval

    @property
    def expiring_soon(self) -> int | None:
        """How many rows expire inside the reporting window."""
        return None if self.line is None else self.line.expiring_soon

    @property
    def already_expired(self) -> int | None:
        """How many rows are expired and awaiting the cluster's own sweep."""
        return None if self.line is None else self.line.already_expired


def retention_rows(
    lines: tuple[ClientRetentionReport, ...],
    roster: tuple[ClientChoice, ...],
    chosen: ClientChoice | None,
    now: datetime,
) -> tuple[RetentionRow, ...]:
    """Pair each permitted Client with its report line, in roster order.

    The iteration is over the roster rather than over the report, which is what scopes
    the page: a line describing a Client the roster does not hold is never reached, and
    a permitted Client the report answered nothing for is rendered as such.
    """
    described = {line.client_id: line for line in lines}
    covered = (chosen,) if chosen is not None else roster
    rows: list[RetentionRow] = []
    for choice in covered:
        line = described.get(choice.id)
        rows.append(
            RetentionRow(
                client=choice,
                line=line,
                expiry_of_a_write_now=None if line is None else expiry_for(now, line.interval),
            )
        )
    return tuple(rows)


@register("retention")
async def retention_view(request: Request) -> Response:
    """Render each permitted Client's regime and the two counts taken against one reading.

    A cluster that does not answer renders the reason in a notice rather than a stack
    trace, so an operator learns that the report could not be taken instead of being
    told nothing is expiring.
    """
    console = console_of(request)
    roster = client_roster(console)
    chosen, recognised = selected_client(request, roster)
    now = console.now()
    context: dict[str, object] = {
        "title": "Retention",
        "roster": roster,
        "chosen": chosen,
        "filter_recognised": recognised,
        "window_days": EXPIRING_SOON_WINDOW.days,
        "fallback_interval": _fallback_interval(request),
        "no_line_notice": NO_LINE_NOTICE,
        "read_at": now,
        "rows": (),
        "notice": None,
    }
    if recognised and roster:
        try:
            lines = report(console.read_only_store(), now=now)
        except Exception as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "the retention report could not be taken, so no regime is rendered",
                error_type=type(error).__name__,
            )
            context["notice"] = "the retention report could not be taken from the cluster"
        else:
            context["rows"] = retention_rows(lines, roster, chosen, now)
    return render(request, RETENTION_TEMPLATE, context)


def _fallback_interval(request: Request) -> timedelta | None:
    """The interval applied where a Client's Jurisdiction names none, or None.

    Read through the Retention component's own accessor, so the console reports the
    value the write paths use. A surface that cannot be resolved at all renders as an
    absence rather than as a number nobody configured.
    """
    try:
        return default_interval(configuration_of(request))
    except Exception as error:
        log(
            Severity.INFO,
            COMPONENT,
            "the configured fallback retention interval could not be read",
            error_type=type(error).__name__,
        )
        return None
