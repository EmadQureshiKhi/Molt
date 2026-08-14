"""The run detail view and the redaction comparison view, both over stored evidence.

Two routes the table already declares, claimed by name: `GET /erase/{run_id}` and
`GET /erase/{run_id}/redactions/{artifact_id}`. Neither declares a path, a method,
or an authentication requirement of its own.

**The run detail view reads the run row and its Dispositions and nothing else.** The
same `disposition` rows the certificate is assembled from are what the page renders,
so the page and the certificate cannot disagree about what happened to an Artifact.

**The redaction comparison view is a query over stored evidence, not a text diff.**
A surgical redaction destroys the pre-redaction body by design (Requirement 18.6),
so there is no *before* pane to render and this module sends no statement that could
reconstruct one. What the *before* column carries is the evidence the Disposition row
retained: the pre-redaction digest, the tenant slugs bound before the decision, and
the count of segments the rewrite removed. What the *after* column carries is the
post-redaction digest, the slugs bound after, the retained-segment count, and the
current body of the row, which *is* the post-redaction body because the rewrite
committed. The template states in prose that the original text was not retained,
which is the honest presentation of the requirement rather than a pretence of a full
diff.

The two segment counts arrive from migration 015 and are nullable, because a hard
delete and a retention summarise no rewrite: absence is rendered as *not applicable*
rather than as a zero, since a zero would claim a rewrite that dropped nothing.
"""

from __future__ import annotations

from typing import Final, cast
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.templating import Jinja2Templates

from molt.console import routing
from molt.console.app import console_of, session_of
from molt.console.deps import COMPONENT, Console
from molt.erase.disposition import DispositionKind
from molt.store import Cursor
from molt.telemetry import Severity, log

__all__ = [
    "DISPOSITIONS_QUERY",
    "POST_REDACTION_BODY_QUERY",
    "REDACTION_QUERY",
    "RUN_QUERY",
    "DispositionRow",
    "RedactionEvidence",
    "RunHeader",
    "erase_detail",
    "erase_redaction",
    "read_dispositions",
    "read_redaction",
    "read_run",
]

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The run header. The tenant appears as its slug rather than as its identifier,
# because a page is read by a person and a slug is what the Disposition evidence
# records too, so the two agree on one name for the tenant.
RUN_QUERY: Final[str] = (
    "SELECT r.id, c.slug, r.requester, r.dry_run, r.status, r.phase, "
    "r.t_before, r.t_after, r.auto_include_threshold, r.review_threshold, "
    "r.unembedded_count, r.working_rows_deleted, r.fencing_generation, "
    "r.started_at, r.finished_at "
    "FROM erasure_run AS r JOIN client AS c ON c.id = r.client_id "
    "WHERE r.id = %s"
)

# The per-Artifact Dispositions of one run. Digests, binding slugs, and the two
# count-only segment summaries; no body column appears in the projection, because
# the table holds none and the view has none to render.
DISPOSITIONS_QUERY: Final[str] = (
    "SELECT artifact_id, artifact_kind, disposition, reason, selection_reason, "
    "pre_digest, post_digest, bindings_before, bindings_after, "
    "removed_segments, retained_segments, decided_at "
    "FROM disposition WHERE run_id = %s "
    "ORDER BY disposition, artifact_kind, artifact_id"
)

# One Disposition, for the comparison view. The projection is the same evidence
# columns: two digests, two slug arrays, two counts.
REDACTION_QUERY: Final[str] = (
    "SELECT artifact_id, artifact_kind, disposition, reason, selection_reason, "
    "pre_digest, post_digest, bindings_before, bindings_after, "
    "removed_segments, retained_segments, decided_at "
    "FROM disposition WHERE run_id = %s AND artifact_id = %s"
)

# The body the row holds now. This read happens after the rewrite committed, so what
# it returns is the post-redaction body; the pre-redaction body is not a row anywhere
# and no statement of this module asks for one. An Artifact deleted by a later run
# returns no row at all, which the view reports as an absence rather than inventing
# text.
POST_REDACTION_BODY_QUERY: Final[str] = (
    "SELECT body, content_digest, revision, redacted_at FROM derived_artifact WHERE id = %s"
)

_NOT_FOUND: Final[dict[str, str]] = {"error": "no such run"}
_NOT_FOUND_STATUS: Final[int] = 404
_UNAVAILABLE_STATUS: Final[int] = 503

# How wide each projection is, checked before a row is decoded so a statement and
# its decoder cannot drift apart in silence.
_RUN_WIDTH: Final[int] = 15
_DISPOSITION_WIDTH: Final[int] = 12
_BODY_WIDTH: Final[int] = 4


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class RunHeader:
    """One Erasure_Run as the detail view reports it."""

    __slots__ = ("fields",)

    def __init__(self, fields: dict[str, object]) -> None:
        self.fields = fields

    @property
    def run_id(self) -> str:
        """The run this header is about, as text a template renders."""
        return str(self.fields["run_id"])


class DispositionRow:
    """One stored Disposition, narrowed to what a page renders.

    The two segment counts stay optional. A hard delete removed the body and a
    retention left it untouched, so neither summarises a rewrite, and rendering a
    zero for either would state that a rewrite dropped nothing.
    """

    __slots__ = ("fields",)

    def __init__(self, fields: dict[str, object]) -> None:
        self.fields = fields

    @property
    def artifact_id(self) -> str:
        """The Artifact this Disposition is about."""
        return str(self.fields["artifact_id"])

    @property
    def redacted(self) -> bool:
        """Whether this Disposition is the surgical case, which alone has a rewrite."""
        return self.fields["disposition"] == DispositionKind.SURGICAL_REDACTION.value


class RedactionEvidence:
    """The comparison view's whole content: stored evidence plus the surviving body."""

    __slots__ = ("body", "disposition")

    def __init__(self, disposition: DispositionRow, body: dict[str, object] | None) -> None:
        self.disposition = disposition
        self.body = body


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_run(cursor: Cursor, run_id: UUID) -> RunHeader | None:
    """The run header, or None when the table holds no run of that identifier."""
    cursor.execute(RUN_QUERY, (run_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    if len(row) != _RUN_WIDTH:
        raise ValueError("the run projection returned an unexpected column count")
    return RunHeader(
        {
            "run_id": str(row[0]),
            "client_slug": row[1],
            "requester": row[2],
            "dry_run": row[3],
            "status": row[4],
            "phase": row[5],
            "t_before": row[6],
            "t_after": row[7],
            "auto_include_threshold": row[8],
            "review_threshold": row[9],
            "unembedded_count": row[10],
            "working_rows_deleted": row[11],
            "fencing_generation": row[12],
            "started_at": row[13],
            "finished_at": row[14],
        }
    )


def _disposition_of(row: tuple[object, ...]) -> DispositionRow:
    """Narrow one Disposition row into the values a template renders."""
    if len(row) != _DISPOSITION_WIDTH:
        raise ValueError("the disposition projection returned an unexpected column count")
    return DispositionRow(
        {
            "artifact_id": str(row[0]),
            "artifact_kind": row[1],
            "disposition": row[2],
            "reason": row[3],
            "selection_reason": row[4],
            "pre_digest": row[5],
            "post_digest": row[6],
            "bindings_before": tuple(cast("list[str]", row[7] or [])),
            "bindings_after": tuple(cast("list[str]", row[8] or [])),
            "removed_segments": row[9],
            "retained_segments": row[10],
            "decided_at": row[11],
        }
    )


def read_dispositions(cursor: Cursor, run_id: UUID) -> tuple[DispositionRow, ...]:
    """Every Disposition of one run, in a stated order rather than the scan's."""
    cursor.execute(DISPOSITIONS_QUERY, (run_id,))
    return tuple(_disposition_of(row) for row in cursor.fetchall())


def read_redaction(cursor: Cursor, run_id: UUID, artifact_id: UUID) -> RedactionEvidence | None:
    """One Disposition and the body that survived it, or None when there is no row.

    The body read is deliberately second and deliberately optional: the evidence is
    the Disposition, and the body is what the cluster still holds. A missing body is
    reported as missing.
    """
    cursor.execute(REDACTION_QUERY, (run_id, artifact_id))
    row = cursor.fetchone()
    if row is None:
        return None
    disposition = _disposition_of(row)
    cursor.execute(POST_REDACTION_BODY_QUERY, (artifact_id,))
    body_row = cursor.fetchone()
    if body_row is None:
        return RedactionEvidence(disposition, None)
    if len(body_row) != _BODY_WIDTH:
        raise ValueError("the body projection returned an unexpected column count")
    return RedactionEvidence(
        disposition,
        {
            "text": body_row[0],
            "content_digest": body_row[1],
            "revision": body_row[2],
            "redacted_at": body_row[3],
        },
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@routing.register("erase_detail")
async def erase_detail(request: Request) -> Response:
    """Render one run with its per-Artifact Dispositions (Requirement 18.8)."""
    identifier = _identifier(request, "run_id")
    if identifier is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    console = console_of(request)
    templates = _templates(request)
    if templates is None:
        return JSONResponse({"error": "unavailable"}, status_code=_UNAVAILABLE_STATUS)

    def body(cursor: Cursor) -> tuple[RunHeader | None, tuple[DispositionRow, ...]]:
        header = read_run(cursor, identifier)
        if header is None:
            return (None, ())
        return (header, read_dispositions(cursor, identifier))

    header, dispositions = console.store.read(body)
    if header is None:
        log(Severity.INFO, COMPONENT, "the run detail view was asked for an absent run")
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        _page(console, request)
        | {
            "title": "Erasure run detail",
            "run": header.fields,
            "dispositions": [entry.fields for entry in dispositions],
            "redaction_count": len([entry for entry in dispositions if entry.redacted]),
        },
    )


@routing.register("erase_redaction")
async def erase_redaction(request: Request) -> Response:
    """Render the stored comparison for one redacted Artifact (Requirement 25.6).

    The page renders the post-redaction body, both digests, both binding sets, and
    the stored segment counts. It renders no pre-redaction body, because none exists.
    """
    run_identifier = _identifier(request, "run_id")
    artifact_identifier = _identifier(request, "artifact_id")
    if run_identifier is None or artifact_identifier is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    console = console_of(request)
    templates = _templates(request)
    if templates is None:
        return JSONResponse({"error": "unavailable"}, status_code=_UNAVAILABLE_STATUS)

    evidence = console.store.read(
        lambda cursor: read_redaction(cursor, run_identifier, artifact_identifier)
    )
    if evidence is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    return templates.TemplateResponse(
        request,
        "redaction_comparison.html",
        _page(console, request)
        | {
            "title": "Redaction comparison",
            "run_id": str(run_identifier),
            "disposition": evidence.disposition.fields,
            "redacted": evidence.disposition.redacted,
            "body": evidence.body,
        },
    )


# ---------------------------------------------------------------------------
# Shared request plumbing
# ---------------------------------------------------------------------------


def _identifier(request: Request, name: str) -> UUID | None:
    """One path parameter as an identifier, or None when it is not one.

    Text that is not an identifier is a 404 rather than a message repeating what was
    submitted, because a page has nothing to say about a name no row can carry.
    """
    raw = request.path_params.get(name)
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError):
        return None


def _page(console: Console, request: Request) -> dict[str, object]:
    """The layout's own context: the mode banner, the session, and the CSRF token."""
    session = session_of(request)
    return {
        "demo_mode": console.demo_mode,
        "authenticated": session is not None,
        "csrf_token": "" if session is None else session.csrf_token,
    }


def _templates(request: Request) -> Jinja2Templates | None:
    """The template environment the application resolved, or None when absent."""
    return cast("Jinja2Templates | None", getattr(request.app.state, "molt_templates", None))
