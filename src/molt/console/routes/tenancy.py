"""What the fleet, Session, and lineage views share: the Client roster and rendering.

Three views need the same two things, and neither belongs in any one of them.

**The permitted Client set is read from the cluster, never from the request.** The
console's principal is the single operator credential set, so the roster the operator
may see is the Client table itself; what a request may do is *narrow* that roster, and
it narrows it by naming a slug that is matched against the rows the cluster returned.
A parameter naming no roster row selects nothing, so a caller cannot widen its own
tenancy by editing a query string, and every read a view then issues is scoped by a
`client_id` that came from a roster row rather than from the caller.

**Read-only demonstration mode narrows the roster here, at the one place tenancy is
produced.** A demonstration serves an anonymous visitor, so the Clients it may read
are the seeded tenants and nothing a real engagement created. That narrowing was
declared on the demonstration principal and applied by nobody, which left the roster
read answering with every Client the cluster holds. It is applied inside
`client_roster` now, and the flag it is applied on is the console's own `demo_mode`
rather than something a caller passes: `client_roster` already takes the `Console`, so
no view has anything to thread and no view has an unnarrowed roster available to ask
for. A view added later inherits the containment from the roster it is handed. Outside
demonstration mode the roster is the Client table exactly as before.

**A view renders through one helper.** The base layout wants the title, the mode flag,
the authenticated flag, and the session's CSRF token on every page, and a view that
assembled those itself would be a view that could forget one. The helper here assembles
them from the request and refuses with a stated status when the template directory is
absent, so a deployment missing its assets says so rather than failing inside Jinja.

The one statement is a whole module-level literal and interpolates nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast
from uuid import UUID

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.templating import Jinja2Templates

from molt.console.app import console_of, session_of
from molt.console.demo import may_read_client
from molt.console.deps import COMPONENT, Console
from molt.store import Cursor
from molt.telemetry import Severity, log

__all__ = [
    "CLIENT_QUERY_FIELD",
    "CLIENT_ROSTER_STATEMENT",
    "ClientChoice",
    "client_roster",
    "identifier_of",
    "permitted_ids",
    "render",
    "selected_client",
    "templates_absent",
]

# The query field a view narrows its roster with. It carries a slug rather than an
# identifier, because a slug is matched against the roster rows and an identifier
# would invite a caller to present one the roster does not hold.
CLIENT_QUERY_FIELD: Final[str] = "client"

# The roster read. It names the Client table alone: no memory content is reached, and
# what comes back is the set every following read is scoped by.
CLIENT_ROSTER_STATEMENT: Final[str] = "SELECT id, slug, display_name FROM client ORDER BY slug, id"

_ROSTER_ROW_WIDTH: Final[int] = 3
_UNAVAILABLE_STATUS: Final[int] = 503
_TEMPLATES_ABSENT: Final[str] = "the console templates are not available"


@dataclass(frozen=True, slots=True)
class ClientChoice:
    """One Client of the permitted roster, as the picker and the reads both use it."""

    id: UUID
    slug: str
    display_name: str

    @property
    def label(self) -> str:
        """What the picker shows: the display name, falling back to the slug."""
        return self.display_name or self.slug


def client_roster(console: Console) -> tuple[ClientChoice, ...]:
    """The permitted Client set of this console, read from the cluster and then narrowed.

    A cluster that does not answer yields an empty roster rather than a failure, so a
    view renders an empty state and says the roster could not be read instead of
    returning a 500 that tells an operator less.

    In read-only demonstration mode the rows the cluster answered are narrowed to the
    seeded tenants before they are returned, so every view that reads tenancy inherits
    the containment rather than each one remembering to ask for it.
    """

    def body(opened: Cursor) -> tuple[ClientChoice, ...]:
        opened.execute(CLIENT_ROSTER_STATEMENT)
        return tuple(_choice_of(row) for row in opened.fetchall())

    try:
        read = console.read_only_store().read(body)
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "the Client roster could not be read, so no tenant content is rendered",
            error_type=type(error).__name__,
        )
        return ()
    return _permitted(read, demo_mode=console.demo_mode)


def _permitted(roster: tuple[ClientChoice, ...], *, demo_mode: bool) -> tuple[ClientChoice, ...]:
    """Keep the roster rows this deployment's posture permits reading.

    Outside demonstration mode that is all of them: the console's principal is the
    operator credential set and the roster is the Client table. Inside it, a Client the
    seed corpus does not name is dropped here, which is what keeps a demonstration off
    a tenant a real engagement created.
    """
    if not demo_mode:
        return roster
    narrowed = tuple(choice for choice in roster if may_read_client(choice.slug, demo_mode=True))
    if len(narrowed) != len(roster):
        log(
            Severity.INFO,
            COMPONENT,
            "the Client roster was narrowed to the seeded tenants for a demonstration",
            withheld=len(roster) - len(narrowed),
        )
    return narrowed


def _choice_of(row: tuple[object, ...]) -> ClientChoice:
    """Narrow one roster row, refusing a width the statement did not select."""
    if len(row) != _ROSTER_ROW_WIDTH:
        raise ValueError(f"a roster row carries {len(row)} column(s) where 3 were selected")
    identifier = row[0]
    return ClientChoice(
        id=identifier if isinstance(identifier, UUID) else UUID(str(identifier)),
        slug=str(row[1]),
        display_name="" if row[2] is None else str(row[2]),
    )


def selected_client(
    request: Request, roster: tuple[ClientChoice, ...]
) -> tuple[ClientChoice | None, bool]:
    """The roster row the request narrows to, and whether its parameter was recognised.

    A request naming no slug narrows to nothing, which the fleet view reads as *every
    permitted Client*. A request naming a slug the roster does not hold also narrows to
    nothing, and the second element of the pair is False so the view can say the filter
    matched no permitted Client rather than silently widening back to the whole roster.
    """
    asked = request.query_params.get(CLIENT_QUERY_FIELD, "").strip()
    if not asked:
        return (None, True)
    for choice in roster:
        if choice.slug == asked:
            return (choice, True)
    return (None, False)


def permitted_ids(
    roster: tuple[ClientChoice, ...], chosen: ClientChoice | None
) -> tuple[UUID, ...]:
    """The identifiers a read may be scoped by: the narrowed one, or the whole roster."""
    return (chosen.id,) if chosen is not None else tuple(choice.id for choice in roster)


def identifier_of(text: str) -> UUID | None:
    """One identifier a path parameter names, or None when it names none well-formed."""
    try:
        return UUID(text)
    except (ValueError, AttributeError, TypeError):
        return None


def templates_absent() -> Response:
    """The answer a view gives when the template directory is not there at all."""
    return PlainTextResponse(_TEMPLATES_ABSENT, status_code=_UNAVAILABLE_STATUS)


def render(request: Request, template: str, context: Mapping[str, object]) -> Response:
    """Render one view through the base layout, with the layout's own context filled in.

    The title, the mode flag, the authenticated flag, and the CSRF token are added here
    rather than by each view, so a view rendered through this helper cannot be missing one.

    The route being served is deliberately *not* among them. It used to be, and that was the
    defect: the layout marks the current navigation section from it, this helper is one of
    two ways a view reaches a template, and the eight views that render directly therefore
    highlighted nothing. The layout now reads it out of the request scope, where the
    application binds it for every route, so no view supplies it and none can omit it.
    """
    templates = cast("Jinja2Templates | None", getattr(request.app.state, "molt_templates", None))
    if templates is None:
        return templates_absent()
    session = session_of(request)
    carried: dict[str, object] = {
        "demo_mode": console_of(request).demo_mode,
        "authenticated": session is not None,
        "csrf_token": "" if session is None else session.csrf_token,
        "client_field": CLIENT_QUERY_FIELD,
    }
    carried.update(context)
    return templates.TemplateResponse(request, template, carried)
