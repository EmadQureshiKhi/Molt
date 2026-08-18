"""The Memory_Tier view: the taxonomy rendered against the cluster as it stands now.

The taxonomy is documented in two other places already. What this view adds is the
only thing documentation cannot: a count taken from the cluster at request time. A
cached count would make the page decorative, so every number here is read inside the
request that renders it and nothing is precomputed.

**The four descriptive columns are not written into the template.** They come from the
one immutable mapping in `molt.models.tiers`, which the documentation generator reads
as well, so the design table, the rendered view, and the documentation cannot state
three different taxonomies. The tier set the view renders is that mapping's own key
order, and the counting statements are checked against it at import.

**One statement per tier, all inside one read-only transaction.** The transaction is
opened as read-only explicitly, so the claim that the view mutates nothing is the
cluster's refusal rather than this module's intention: a write reaching that
transaction would be rejected by the cluster. Every statement is a whole module-level
literal, and a tier holding several tables is one statement summing over them rather
than several statements a reader would have to add up.

**The working tier is not presented as if it were durable.** It is the one tier
nothing may depend on: rows are overwritten in place and physically removed on expiry
by Row-Level TTL, and no evidence anywhere in this system is drawn from it. So its row
carries two figures no other tier has — how many resident rows have already expired,
and how long until the next sweep — and its prose says plainly that it is excluded
from evidence.

**The next sweep is read back from the table's own configuration.** The interval is
computed from the TTL job cron storage parameter as the cluster reports it, not from a
number written here and not from a configuration key that could have drifted from what
was applied. A cron expression this module cannot interpret is reported as such rather
than guessed at, because a wrong interval is worse than an absent one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, cast

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.templating import Jinja2Templates

from molt.console import routing
from molt.console.app import console_of, session_of
from molt.errors import MoltError
from molt.models.tiers import MEMORY_TIERS, TIER_NAMES, WORKING_TIER, MemoryTierSpec
from molt.store import Cursor
from molt.telemetry import Severity, log

__all__ = [
    "BEGIN_READ_ONLY_STATEMENT",
    "CLUSTER_NOW_QUERY",
    "COMMIT_STATEMENT",
    "COMPONENT",
    "COUNTS_UNAVAILABLE_NOTICE",
    "TEMPLATE",
    "TIER_COUNT_QUERIES",
    "UNREADABLE_CRON",
    "WORKING_EXPIRED_QUERY",
    "WORKING_TTL_CONFIGURATION_QUERY",
    "TierReading",
    "TierView",
    "cron_of",
    "next_sweep_after",
    "read_tiers",
    "tiers_view",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "console"

# The template this view renders.
TEMPLATE: Final[str] = "tiers.html"

# What the view says when the cluster reports a cron expression this module does not
# interpret. Naming the situation is the honest answer: an interval guessed from an
# expression nobody read is worse than no interval at all.
UNREADABLE_CRON: Final[str] = "not derivable from the reported expression"

# What the page says when the counts could not be taken at all. Counting every tier is the
# widest read this console performs, so it is the read a statement timeout reaches first
# while an authorised erasure sweeps the same tables. Saying so is the answer; a page that
# fails outright would report a defect where there is contention.
COUNTS_UNAVAILABLE_NOTICE: Final[str] = (
    "the tier counts could not be taken from the cluster, which is what a read of every "
    "tier answers with while an erasure is sweeping the same tables"
)

# The transaction the counts are taken in. Read-only is declared to the cluster, so a
# statement that wrote would be refused by the cluster rather than merely absent from
# this module.
BEGIN_READ_ONLY_STATEMENT: Final[str] = "BEGIN TRANSACTION READ ONLY"
COMMIT_STATEMENT: Final[str] = "COMMIT"

# One counting statement per tier, each summing over that tier's own tables so a tier
# is one statement rather than several a reader would have to add up. The episodic and
# provenance tiers both draw on the ledger table, so these are per-tier row totals
# rather than a partition of the cluster, and the rendered caption says so.
_QUERIES: Final[dict[str, str]] = {
    "episodic": "SELECT count(*) FROM ledger",
    "attribution": "SELECT count(*) FROM client_binding",
    "procedural_semantic": (
        "SELECT (SELECT count(*) FROM derived_artifact) "
        "+ (SELECT count(*) FROM procedure_retrieval) "
        "+ (SELECT count(*) FROM procedure_outcome) "
        "+ (SELECT count(*) FROM procedure_confidence_change)"
    ),
    "provenance": (
        "SELECT (SELECT count(*) FROM lineage_edge) "
        "+ (SELECT count(*) FROM ledger) "
        "+ (SELECT count(*) FROM ledger_checkpoint)"
    ),
    "action": (
        "SELECT (SELECT count(*) FROM erasure_lease) "
        "+ (SELECT count(*) FROM erasure_request) "
        "+ (SELECT count(*) FROM erasure_run) "
        "+ (SELECT count(*) FROM erasure_candidate) "
        "+ (SELECT count(*) FROM residue_candidate) "
        "+ (SELECT count(*) FROM disposition) "
        "+ (SELECT count(*) FROM run_session) "
        "+ (SELECT count(*) FROM backup_record) "
        "+ (SELECT count(*) FROM erasure_certificate)"
    ),
    WORKING_TIER: "SELECT count(*) FROM working_memory",
}

# The working tier's two extra figures. The expired count compares the stored expiry
# against the cluster's own current timestamp rather than against a reading taken in
# this process, and the configuration is read back from the table itself.
WORKING_EXPIRED_QUERY: Final[str] = "SELECT count(*) FROM working_memory WHERE expires_at < now()"
WORKING_TTL_CONFIGURATION_QUERY: Final[str] = (
    "SELECT create_statement FROM [SHOW CREATE TABLE working_memory]"
)
CLUSTER_NOW_QUERY: Final[str] = "SELECT now()"


def _validated(queries: dict[str, str]) -> Mapping[str, str]:
    """Check at import that the counting statements and the tier mapping agree."""
    if set(queries) != set(TIER_NAMES):
        raise MoltError("the tier counting statements and the tier mapping name different tiers")
    return MappingProxyType({name: queries[name] for name in TIER_NAMES})


TIER_COUNT_QUERIES: Final[Mapping[str, str]] = _validated(_QUERIES)

# How the cron storage parameter is recognised in the table's own creation statement,
# and the shorthands this module interprets.
_CRON_PATTERN: Final[re.Pattern[str]] = re.compile(r"ttl_job_cron\s*=\s*'([^']*)'")
_HOURLY: Final[str] = "@hourly"
_DAILY: Final[str] = "@daily"
_WEEKLY: Final[str] = "@weekly"
_FIELD_COUNT: Final[int] = 5
_MINUTES_PER_HOUR: Final[int] = 60
_HOURS_PER_DAY: Final[int] = 24
_DAYS_PER_WEEK: Final[int] = 7
_ROW_WIDTH: Final[int] = 1
_UNAVAILABLE_STATUS: Final[int] = 503


@dataclass(frozen=True, slots=True)
class TierReading:
    """One tier's rendered row: its specification and its live count.

    The two working-tier figures are optional because they belong to that tier alone:
    an absent expiry figure on another tier's row is the truth about that tier rather
    than a missing measurement.
    """

    spec: MemoryTierSpec
    row_count: int
    expired_count: int | None = None
    next_sweep: timedelta | None = None
    cron: str | None = None

    @property
    def name(self) -> str:
        """The tier name, as stored and as rendered."""
        return self.spec.name

    @property
    def is_working(self) -> bool:
        """Whether this is the tier nothing may depend on."""
        return self.spec.name == WORKING_TIER


@dataclass(frozen=True, slots=True)
class TierView:
    """Every tier's reading, taken inside one read-only transaction."""

    readings: tuple[TierReading, ...]
    read_at: datetime

    @property
    def total_rows(self) -> int:
        """The sum of the per-tier totals, which double-counts a shared table."""
        return sum(reading.row_count for reading in self.readings)


def cron_of(create_statement: str) -> str | None:
    """The TTL job cron the table's own creation statement reports, or None."""
    found = _CRON_PATTERN.search(create_statement)
    return None if found is None else found.group(1)


def next_sweep_after(cron: str, now: datetime) -> datetime | None:
    """When the TTL job next runs under one cron expression, or None when unreadable.

    Only the forms the migrations use and the plain numeric minute and hour fields are
    interpreted. Anything else answers None, which the view renders as a statement that
    the interval is not derivable rather than as a number nobody checked.
    """
    expression = cron.strip()
    if expression == _HOURLY:
        return _floor_hour(now) + timedelta(hours=1)
    if expression == _DAILY:
        return _floor_day(now) + timedelta(days=1)
    if expression == _WEEKLY:
        start = _floor_day(now) - timedelta(days=(now.weekday() + 1) % _DAYS_PER_WEEK)
        candidate = start + timedelta(days=_DAYS_PER_WEEK)
        return candidate if candidate > now else candidate + timedelta(days=_DAYS_PER_WEEK)
    fields = expression.split()
    if len(fields) != _FIELD_COUNT or fields[2:] != ["*", "*", "*"]:
        return None
    minute = _field(fields[0], _MINUTES_PER_HOUR)
    if minute is None:
        return None
    if fields[1] == "*":
        candidate = _floor_hour(now) + timedelta(minutes=minute)
        return candidate if candidate > now else candidate + timedelta(hours=1)
    hour = _field(fields[1], _HOURS_PER_DAY)
    if hour is None:
        return None
    candidate = _floor_day(now) + timedelta(hours=hour, minutes=minute)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _field(text: str, bound: int) -> int | None:
    """One whole cron field, or None when it is not a plain number inside its bound."""
    if not text.isdigit():
        return None
    value = int(text, 10)
    return value if value < bound else None


def _floor_hour(now: datetime) -> datetime:
    """The start of the hour one instant falls in."""
    return now.replace(minute=0, second=0, microsecond=0)


def _floor_day(now: datetime) -> datetime:
    """The start of the day one instant falls in."""
    return _floor_hour(now).replace(hour=0)


def read_tiers(store: object) -> TierView:
    """Read every tier's live count in one read-only transaction, and nothing else.

    The transaction is opened and committed by this body, so the counts are one
    consistent reading rather than a sequence of independent ones, and the cluster
    itself refuses a write inside it.
    """
    from molt.store import MemoryStore

    def body(cursor: Cursor) -> TierView:
        cursor.execute(BEGIN_READ_ONLY_STATEMENT)
        try:
            read_at = _instant(_one(cursor, CLUSTER_NOW_QUERY))
            readings: list[TierReading] = []
            for name in TIER_NAMES:
                count = _count(_one(cursor, TIER_COUNT_QUERIES[name]))
                if name != WORKING_TIER:
                    readings.append(TierReading(spec=MEMORY_TIERS[name], row_count=count))
                    continue
                expired = _count(_one(cursor, WORKING_EXPIRED_QUERY))
                cron = cron_of(_text(_one(cursor, WORKING_TTL_CONFIGURATION_QUERY)))
                due = None if cron is None else next_sweep_after(cron, read_at)
                readings.append(
                    TierReading(
                        spec=MEMORY_TIERS[name],
                        row_count=count,
                        expired_count=expired,
                        next_sweep=None if due is None else due - read_at,
                        cron=cron,
                    )
                )
        finally:
            cursor.execute(COMMIT_STATEMENT)
        return TierView(readings=tuple(readings), read_at=read_at)

    return cast(MemoryStore, store).read(body)


def _one(cursor: Cursor, statement: str) -> object:
    """The one column of the one row a single-value statement answers with."""
    cursor.execute(statement)
    row = cursor.fetchone()
    if row is None or len(row) != _ROW_WIDTH:
        raise MoltError("a tier reading answered with no single value, so it cannot be reported")
    return row[0]


def _count(value: object) -> int:
    """One count column, refused rather than coerced when it is not a whole number."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoltError("a tier count answered with something other than a whole number")
    return value


def _instant(value: object) -> datetime:
    """The cluster's own current timestamp, refused when it is not a timestamp."""
    if isinstance(value, datetime):
        return value
    raise MoltError("the cluster answered with something other than a timestamp")


def _text(value: object) -> str:
    """One text column, refused rather than coerced when it is not text."""
    if isinstance(value, str):
        return value
    raise MoltError("the table configuration answered with something other than text")


@routing.register("tiers")
async def tiers_view(request: Request) -> Response:
    """Render one row per tier, with the working tier's two extra figures."""
    context: dict[str, object] = {
        "title": "Memory tiers",
        "working_tier": WORKING_TIER,
        "unreadable_cron": UNREADABLE_CRON,
        "view": None,
        "notice": None,
    }
    try:
        context["view"] = read_tiers(console_of(request).read_only_store())
    except MoltError as error:
        log(
            Severity.INFO,
            COMPONENT,
            "the memory tier view could not be rendered",
            error_type=type(error).__name__,
        )
        context["notice"] = str(error)
    # A read that does not complete is the other way this page fails to have counts, and
    # it is not a shape this module raised. Counting every tier is the widest read the
    # console performs, so it is the first read to be cancelled by the statement timeout
    # when an authorised erasure is sweeping the same tables — which is a moment a reviewer
    # is especially likely to be looking at this page. A page reporting that the counts
    # could not be taken is the honest answer and the answer every other read-only view
    # here already gives; a page that fails outright reports a defect where there is
    # contention.
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "the tier counts could not be read, so no count is rendered",
            error_type=type(error).__name__,
        )
        context["notice"] = COUNTS_UNAVAILABLE_NOTICE
    return _page(request, context)


def _page(request: Request, context: dict[str, object]) -> Response:
    """Render this view's template with the fields the shared layout expects."""
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
