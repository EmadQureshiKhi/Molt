"""The procedure standing view: what each Learned_Procedure has earned, and how.

A standing on its own is a number an operator has to trust. A standing beside the
retrieval count, the outcome counts per classification, and the ordered history of
every movement is a number an operator can check, which is why each row carries all
four and expands to the history rather than linking away to it.

**The arithmetic is not restated here.** The standing, the retrieval count, and the
three outcome counts come from the Confidence_Tracker's own summary read, and the
history comes from its own history read, so the console cannot come to disagree with
the tracker about what a movement was. The recall floor comes from the configured
policy for the same reason: a floor this module spelled out would be a second floor
the recall predicate could contradict.

**A procedure below the recall floor is shown, marked, and explained.** Hiding it
would misrepresent what the floor does: the floor excludes a procedure from recall
and leaves it in storage, where erasure still reaches it. So a below-floor row is
rendered with a text marker saying exactly that, and the ordering puts the lowest
standings first, because those are the rows an operator came to look at.

**The view opens no write transaction.** Every read here goes through the read path
of the store, which frames no transaction and issues no statement that writes, so the
view is available unchanged in read-only demonstration mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast
from uuid import UUID

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.templating import Jinja2Templates

from molt.confidence import ConfidencePolicy, history, summary
from molt.config.resolve import load_configuration
from molt.console import routing
from molt.console.app import console_of, session_of
from molt.errors import MoltError
from molt.models.artifact import DerivedArtifactKind
from molt.store import Cursor, MemoryStore
from molt.store.confidence import ConfidenceChange, ProcedureStanding
from molt.telemetry import Severity, log

__all__ = [
    "BELOW_FLOOR_MARKER",
    "COMPONENT",
    "DEFAULT_PROCEDURE_LIMIT",
    "HISTORY_LIMIT",
    "SELECT_PROCEDURES_QUERY",
    "TEMPLATE",
    "ProcedureRow",
    "procedure_rows",
    "procedures_view",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "console"

# The template this view renders, and the marker a below-floor row carries. The
# marker is a constant because the accessibility test asserts a below-floor row says
# both halves of the truth: excluded from recall, retained in storage.
TEMPLATE: Final[str] = "procedures.html"
BELOW_FLOOR_MARKER: Final[str] = "excluded from recall, retained in storage"

# How many procedures one page lists, and how many movements one row expands to.
# Both are bounds rather than defaults a caller may widen, because an unbounded page
# of a long-lived fleet's procedures is a scan nobody asked for.
DEFAULT_PROCEDURE_LIMIT: Final[int] = 50
HISTORY_LIMIT: Final[int] = 20

# The listing. Ascending by standing, so the rows nearest the floor lead the page,
# with the identifier as the final key so the order is total. The kind and the bound
# both travel as parameters: this statement names no domain value of its own.
SELECT_PROCEDURES_QUERY: Final[str] = (
    "SELECT id, owner_client_id, procedure_confidence, revision, created_at "
    "FROM derived_artifact WHERE kind = %s "
    "ORDER BY procedure_confidence ASC, id ASC LIMIT %s"
)

_PROCEDURE_KIND: Final[str] = DerivedArtifactKind.LEARNED_PROCEDURE.value
_ROW_WIDTH: Final[int] = 5
_UNAVAILABLE_STATUS: Final[int] = 503


@dataclass(frozen=True, slots=True)
class ProcedureRow:
    """One rendered row: the standing, the counts, the history, and the floor verdict."""

    procedure_id: UUID
    owner_client_id: UUID
    revision: int
    standing: ProcedureStanding
    changes: tuple[ConfidenceChange, ...]
    below_floor: bool

    @property
    def confidence(self) -> float:
        """The current standing, which is what the row leads with."""
        return self.standing.confidence


def _listed(store: MemoryStore, limit: int) -> tuple[tuple[UUID, UUID, int], ...]:
    """The procedures one page lists, as identifier, owning Client, and revision.

    The standing is read again per row through the tracker's own summary, so the three
    numbers a row shows are taken together at one instant rather than paired with a
    standing this listing happened to see.
    """

    def body(cursor: Cursor) -> tuple[tuple[UUID, UUID, int], ...]:
        cursor.execute(SELECT_PROCEDURES_QUERY, (_PROCEDURE_KIND, limit))
        listed: list[tuple[UUID, UUID, int]] = []
        for row in cursor.fetchall():
            if len(row) != _ROW_WIDTH:
                raise MoltError("the procedure listing returned a row of an unexpected width")
            listed.append((_as_uuid(row[0]), _as_uuid(row[1]), _as_int(row[3])))
        return tuple(listed)

    return store.read(body)


def procedure_rows(
    store: MemoryStore,
    *,
    policy: ConfidencePolicy,
    limit: int = DEFAULT_PROCEDURE_LIMIT,
) -> tuple[ProcedureRow, ...]:
    """Every listed procedure with its standing, its counts, and its change history."""
    rows: list[ProcedureRow] = []
    for procedure_id, owner_client_id, revision in _listed(store, limit):
        standing = summary(store, procedure_id)
        rows.append(
            ProcedureRow(
                procedure_id=procedure_id,
                owner_client_id=owner_client_id,
                revision=revision,
                standing=standing,
                changes=history(store, procedure_id, limit=HISTORY_LIMIT),
                below_floor=policy.below_floor(standing.confidence),
            )
        )
    return tuple(rows)


@routing.register("procedures")
async def procedures_view(request: Request) -> Response:
    """Render every listed procedure's standing, counts, and history."""
    policy = ConfidencePolicy.from_configuration(load_configuration())
    context: dict[str, object] = {
        "title": "Procedure standing",
        "recall_floor": policy.recall_floor,
        "below_floor_marker": BELOW_FLOOR_MARKER,
        "rows": (),
        "notice": None,
    }
    try:
        context["rows"] = procedure_rows(console_of(request).store, policy=policy)
    except MoltError as error:
        log(
            Severity.INFO,
            COMPONENT,
            "the procedure standing view could not be rendered",
            error_type=type(error).__name__,
        )
        context["notice"] = str(error)
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


def _as_uuid(value: object) -> UUID:
    """One identifier column, refused rather than coerced when it is not one."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise MoltError("the procedure listing returned a column that is not an identifier")


def _as_int(value: object) -> int:
    """One whole-number column, refused rather than coerced when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoltError("the procedure listing returned a column that is not a whole number")
    return value
