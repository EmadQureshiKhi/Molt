"""The erasure console: start a run without holding a request, watch it durably.

Three routes and one idea. A function invocation is request-scoped and can be frozen
between requests, so nothing here may depend on this process still being alive when
the next request arrives.

**`POST /erase` records the attempt and returns its identifier.** It resolves the
Client, mints the attempt's idempotency key, hands the run to a launcher seam, and
answers immediately. The engine's own `run()` is what performs the phases, under the
Erasure_Lease and writing the durable phase marker on the run row as it goes; the
launcher decides where that happens, so the deployed path detaches it and a test
supplies a stand-in that records the plan and runs nothing.

**`GET /erase/{run_id}/stream` reads the durable rows and never process memory.** The
run row's `phase` and `status`, the `erasure_candidate` count, the `residue_candidate`
counts, and the `disposition` counts are the same evidence the certificate is
assembled from, so streamed progress and the certificate cannot disagree. A client
connecting late or reconnecting therefore receives the current state rather than
nothing, and a terminal run ends the stream with an explicit outcome event rather
than by closing the connection and leaving the client to guess.

**The identifier in the path is the attempt's, and the run is found by either name.**
The engine mints the run row's own identifier when it claims the lease and stamps the
attempt's idempotency key onto that row, so one statement finds the run by the run
identifier or by the attempt identifier. Before the lease is claimed there is no run
row at all, and the stream says `queued` rather than inventing a phase.

**Two refusals guard the mutation, not one.** The route table marks `erase_start` a
mutation, so the application's middleware refuses it without the session's own CSRF
token before this module is reached; and this handler refuses again when read-only
demonstration mode is configured, rather than relying on the demonstration
middleware being installed. A mutation reachable because one guard was absent is the
failure both guards exist to prevent.

Every statement is a whole module-level literal with bound parameters.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Thread
from typing import Final, TypeVar, cast
from uuid import UUID, uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from molt.backup import BackupSettings
from molt.cli.context import AGENT_CLI, machine_identifier
from molt.config.resolve import Configuration
from molt.console import routing
from molt.console.app import console_of, form_values, session_of
from molt.console.routes.erasure_common import (
    UNAVAILABLE_STATUS,
    configuration_of,
    fleet,
    templates_of,
)
from molt.erase.engine import EngineSeams, ErasureRequest, Phase, RunStatus, run_erasure
from molt.erase.residue import ResiduePolicy
from molt.errors import StoreError
from molt.providers.selector import select_embedding_provider, select_text_provider
from molt.retention import default_interval, expiry_for
from molt.store import Cursor, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.telemetry import Severity, log

__all__ = [
    "CONSOLE_TEMPLATE",
    "LAUNCHER_STATE_KEY",
    "SELECT_CANDIDATE_COUNT_STATEMENT",
    "SELECT_DISPOSITION_COUNTS_STATEMENT",
    "SELECT_RESIDUE_COUNTS_STATEMENT",
    "SELECT_RUN_STATEMENT",
    "RunPlan",
    "RunProgress",
    "detached_launcher",
    "erase_console",
    "erase_start",
    "erase_stream",
    "progress_of",
    "stream_body",
]

_COMPONENT: Final[str] = "console"

# What a provider selection returns, so an unselectable provider stays absent rather
# than becoming an untyped value the engine would have to guess about.
_Provider = TypeVar("_Provider")

# The template this console renders.
CONSOLE_TEMPLATE: Final[str] = "erase.html"

# Where a deployment or a test puts the launcher the start route hands a plan to.
LAUNCHER_STATE_KEY: Final[str] = "molt_erasure_launcher"

# The submitted field names, fixed so the template and the handler agree.
CLIENT_FIELD: Final[str] = "client"
REQUESTER_FIELD: Final[str] = "requester"
JUSTIFICATION_FIELD: Final[str] = "justification"
DRY_RUN_FIELD: Final[str] = "dry_run"
SKIP_BACKUP_FIELD: Final[str] = "skip_backup"
AUTO_INCLUDE_FIELD: Final[str] = "auto_include_threshold"
REVIEW_FIELD: Final[str] = "review_threshold"
ATTEMPT_PARAMETER: Final[str] = "attempt"

# The two form values that mean consent when a checkbox is submitted at all.
_CHECKED: Final[frozenset[str]] = frozenset({"on", "true", "1", "yes"})

_ACCEPTED: Final[int] = 202
_SEE_OTHER: Final[int] = 303
_BAD_REQUEST: Final[int] = 400
_FORBIDDEN_STATUS: Final[int] = 403

_FORBIDDEN: Final[Mapping[str, str]] = {"error": "forbidden"}
_DEMONSTRATION_REFUSAL: Final[str] = (
    "read-only demonstration mode records no erasure and starts none"
)

# The media type and the reconnection hint the stream carries. The client reconnects
# rather than the server holding the connection, because the invocation hosting this
# response may be frozen the moment it returns.
EVENT_STREAM_MEDIA_TYPE: Final[str] = "text/event-stream"
RECONNECT_MILLISECONDS: Final[int] = 2000

# The event names the stream emits. `outcome` is the terminal one, so a client learns
# the result from an event rather than from the connection closing.
PHASE_EVENT: Final[str] = "phase"
QUEUED_EVENT: Final[str] = "queued"
OUTCOME_EVENT: Final[str] = "outcome"

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# The run row, found by its own identifier or by the attempt's idempotency key. One
# statement rather than two, because the caller holds one identifier and does not
# know which of the two names it is until the engine has claimed the run.
SELECT_RUN_STATEMENT: Final[str] = (
    "SELECT id, client_id, requester, dry_run, status, phase, error_detail, "
    "auto_include_threshold, review_threshold "
    "FROM erasure_run WHERE id = %s OR idempotency_key = %s"
)

# The explicit candidate set the sweep committed.
SELECT_CANDIDATE_COUNT_STATEMENT: Final[str] = (
    "SELECT count(*) FROM erasure_candidate WHERE run_id = %s"
)

# What the residue phase banded and included, from the rows it committed.
SELECT_RESIDUE_COUNTS_STATEMENT: Final[str] = (
    "SELECT count(*), count(*) FILTER (WHERE included), "
    "count(*) FILTER (WHERE band = 'review') "
    "FROM residue_candidate WHERE run_id = %s"
)

# The per-Artifact decisions, counted by the value the schema admits.
SELECT_DISPOSITION_COUNTS_STATEMENT: Final[str] = (
    "SELECT disposition, count(*) FROM disposition WHERE run_id = %s GROUP BY disposition"
)

_RUN_ROW_WIDTH: Final[int] = 9
_RESIDUE_ROW_WIDTH: Final[int] = 3


# ---------------------------------------------------------------------------
# Starting a run without holding the request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunPlan:
    """One recorded attempt: what to erase, under which thresholds, for whom.

    The thresholds travel on the plan rather than being read again by the launcher,
    so the values the operator submitted are the values the run is performed under
    and a test reads them from the plan it was handed.
    """

    request: ErasureRequest
    client_slug: str
    auto_include_threshold: float
    review_threshold: float

    @property
    def attempt(self) -> UUID:
        """The identifier the client watches this attempt by."""
        return UUID(self.request.idempotency_key)


# Where a plan goes. A seam rather than a call, because the deployed path detaches the
# run from this invocation and a test records the plan and performs nothing.
RunLauncher = Callable[[RunPlan], None]


def detached_launcher(configuration: Configuration) -> RunLauncher:
    """The launcher a deployment uses: the run proceeds outside this request.

    The worker opens its own store rather than borrowing the request's, because the
    request's connection is returned to the pool the moment the response is written,
    and the run's phases must not depend on this invocation staying alive. Progress
    is durable either way: every phase advance is an assignment to the run row's
    marker, which is what the stream reads.
    """

    def launch(plan: RunPlan) -> None:
        thread = Thread(
            target=_perform,
            args=(plan, configuration),
            name=f"molt-erasure-{plan.attempt}",
            daemon=True,
        )
        thread.start()

    return launch


def _perform(plan: RunPlan, configuration: Configuration) -> None:
    """Run the engine for one plan, recording the failure rather than raising it.

    Nothing is raised to a caller here: the caller is a worker with no request to
    answer, and the evidence of a failed run is the aborted run row the engine wrote
    plus the record this logs.
    """
    try:
        with MemoryStore.from_configuration(configuration) as store:
            run_erasure(store, plan.request, _seams(configuration, store))
    except Exception as failure:
        log(
            Severity.ERROR,
            _COMPONENT,
            "a console-started erasure run ended in a failure",
            attempt=str(plan.attempt),
            error_type=type(failure).__name__,
        )


def _seams(configuration: Configuration, store: MemoryStore) -> EngineSeams:
    """The engine's seams, assembled the way the CLI verb assembles them.

    A provider that cannot be selected is left unset rather than defaulted, so the
    run takes the fail-closed path instead of proceeding as though a model answered.
    """
    now = datetime.now(tz=UTC)
    return EngineSeams(
        configuration=configuration,
        backup=BackupSettings.from_configuration(configuration),
        capabilities=store.capabilities(),
        supersession=SupersessionContext(
            session_id=uuid4(),
            agent_cli=AGENT_CLI,
            machine_id=machine_identifier(configuration),
            expires_at=expiry_for(now, default_interval(configuration)),
        ),
        text_provider=_selected(select_text_provider, configuration),
        embedding_provider=_selected(select_embedding_provider, configuration),
    )


def _selected(
    select: Callable[[Configuration], _Provider], configuration: Configuration
) -> _Provider | None:
    """One configured provider, or None so the run fails closed rather than proceeds."""
    chosen: _Provider | None = None
    with suppress(Exception):
        chosen = select(configuration)
    return chosen


def launcher_of(request: Request) -> RunLauncher:
    """The launcher this request's application was built with, or the detached one."""
    held = getattr(request.app.state, LAUNCHER_STATE_KEY, None)
    if held is None:
        return detached_launcher(configuration_of(request))
    return cast(RunLauncher, held)


# ---------------------------------------------------------------------------
# Reading progress from the durable rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunProgress:
    """What the durable rows say about one attempt, at the instant they were read.

    `recorded` is false where no run row exists yet, which is the queued state: the
    attempt was accepted and the engine has not yet claimed its lease. Inventing a
    phase there would report progress nothing made.
    """

    attempt: UUID
    recorded: bool
    run_id: UUID | None = None
    status: str | None = None
    phase: str | None = None
    dry_run: bool = False
    error_detail: str | None = None
    candidates: int = 0
    residue_candidates: int = 0
    residue_included: int = 0
    residue_review: int = 0
    dispositions: Mapping[str, int] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        """Whether the run reached a recorded end, which is what ends the stream."""
        return self.status in (str(RunStatus.COMPLETED), str(RunStatus.ABORTED))

    def document(self) -> Mapping[str, object]:
        """The event body, carrying no memory content: identifiers and counts only."""
        return {
            "attempt": str(self.attempt),
            "run_id": None if self.run_id is None else str(self.run_id),
            "recorded": self.recorded,
            "status": self.status,
            "phase": self.phase,
            "dry_run": self.dry_run,
            "candidates": self.candidates,
            "residue_candidates": self.residue_candidates,
            "residue_included": self.residue_included,
            "residue_review": self.residue_review,
            "dispositions": dict(self.dispositions),
            "error_detail": self.error_detail,
            "terminal": self.terminal,
        }


def progress_of(store: MemoryStore, attempt: UUID) -> RunProgress:
    """Read one attempt's progress from the durable rows and from nothing else."""

    def body(cursor: Cursor) -> RunProgress:
        cursor.execute(SELECT_RUN_STATEMENT, (attempt, attempt.hex))
        row = cursor.fetchone()
        if row is None:
            return RunProgress(attempt=attempt, recorded=False)
        run_id = _as_uuid(_column(row, 0, _RUN_ROW_WIDTH))
        cursor.execute(SELECT_CANDIDATE_COUNT_STATEMENT, (run_id,))
        candidates = _as_count(cursor.fetchone())
        cursor.execute(SELECT_RESIDUE_COUNTS_STATEMENT, (run_id,))
        residue = _residue_counts(cursor.fetchone())
        cursor.execute(SELECT_DISPOSITION_COUNTS_STATEMENT, (run_id,))
        dispositions = {str(each[0]): int(cast(int, each[1])) for each in cursor.fetchall()}
        return RunProgress(
            attempt=attempt,
            recorded=True,
            run_id=run_id,
            status=str(_column(row, 4, _RUN_ROW_WIDTH)),
            phase=str(_column(row, 5, _RUN_ROW_WIDTH)),
            dry_run=bool(_column(row, 3, _RUN_ROW_WIDTH)),
            error_detail=_as_optional_text(_column(row, 6, _RUN_ROW_WIDTH)),
            candidates=candidates,
            residue_candidates=residue[0],
            residue_included=residue[1],
            residue_review=residue[2],
            dispositions=dispositions,
        )

    return store.read(body)


def stream_body(progress: RunProgress) -> Iterator[str]:
    """The event stream for one reading of the durable rows.

    One reading per invocation, then the response ends: the client reconnects on the
    hint this emits, and each reconnection reads the rows again. A recorded terminal
    state emits the outcome event, which is how a client learns the result rather
    than by observing a closed connection.
    """
    yield f"retry: {RECONNECT_MILLISECONDS}\n\n"
    body = json.dumps(dict(progress.document()), sort_keys=True)
    name = PHASE_EVENT if progress.recorded else QUEUED_EVENT
    yield f"event: {name}\ndata: {body}\n\n"
    if progress.terminal:
        yield f"event: {OUTCOME_EVENT}\ndata: {body}\n\n"


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@routing.register("erase_console")
async def erase_console(request: Request) -> Response:
    """Render the console: the Client select, the thresholds, and the live region."""
    console = console_of(request)
    templates = templates_of(request)
    if templates is None:
        return PlainTextResponse(
            "the console templates are not available", status_code=UNAVAILABLE_STATUS
        )
    policy = ResiduePolicy.from_configuration(configuration_of(request))
    session = session_of(request)
    attempt = request.query_params.get(ATTEMPT_PARAMETER, "").strip()
    return templates.TemplateResponse(
        request,
        CONSOLE_TEMPLATE,
        {
            "title": "Erasure",
            "demo_mode": console.demo_mode,
            "authenticated": session is not None,
            "csrf_token": "" if session is None else session.csrf_token,
            "clients": fleet(console.store),
            "policy": policy,
            "attempt": attempt,
            "blocked_explanation": _DEMONSTRATION_REFUSAL,
            "client_field": CLIENT_FIELD,
            "requester_field": REQUESTER_FIELD,
            "justification_field": JUSTIFICATION_FIELD,
            "dry_run_field": DRY_RUN_FIELD,
            "skip_backup_field": SKIP_BACKUP_FIELD,
            "auto_include_field": AUTO_INCLUDE_FIELD,
            "review_field": REVIEW_FIELD,
        },
    )


@routing.register("erase_start")
async def erase_start(request: Request) -> Response:
    """Record one attempt, hand it to the launcher, and answer with its identifier.

    The response is written before any phase runs. Nothing about the run's progress
    is held here: the identifier answered is what the stream route reads the durable
    rows by.
    """
    console = console_of(request)
    if console.demo_mode:
        log(
            Severity.WARNING,
            _COMPONENT,
            "an erasure start was refused because demonstration mode is configured",
        )
        return JSONResponse(
            dict(_FORBIDDEN) | {"detail": _DEMONSTRATION_REFUSAL},
            status_code=_FORBIDDEN_STATUS,
        )

    submitted = await form_values(request)
    plan = _plan_of(console.store, submitted, configuration_of(request))
    if isinstance(plan, str):
        return JSONResponse({"error": plan}, status_code=_BAD_REQUEST)

    launcher_of(request)(plan)
    log(
        Severity.INFO,
        _COMPONENT,
        "recorded an erasure attempt and returned its identifier",
        attempt=str(plan.attempt),
        client_slug=plan.client_slug,
        dry_run=plan.request.dry_run,
    )
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(
            f"/erase?{ATTEMPT_PARAMETER}={plan.attempt}", status_code=_SEE_OTHER
        )
    return JSONResponse(
        {
            "attempt": str(plan.attempt),
            "dry_run": plan.request.dry_run,
            "stream": f"/erase/{plan.attempt}/stream",
        },
        status_code=_ACCEPTED,
    )


@routing.register("erase_stream")
async def erase_stream(request: Request) -> Response:
    """Stream one attempt's phase progress from the durable rows.

    Read-only in every mode, including demonstration mode, because it opens no write
    transaction: a completed seeded run is watched through exactly this route.
    """
    console = console_of(request)
    raw = str(request.path_params.get("run_id", ""))
    try:
        attempt = UUID(raw)
    except ValueError:
        return JSONResponse({"error": "that identifier is not one"}, status_code=_BAD_REQUEST)
    progress = progress_of(console.store, attempt)
    # One whole reading rather than a held connection. A streamed response is
    # truncated by the function host the moment the invocation's request is reported
    # disconnected, so the events for this reading are assembled and written in full
    # and the client reconnects on the retry hint they carry.
    return Response(
        "".join(stream_body(progress)),
        media_type=EVENT_STREAM_MEDIA_TYPE,
        headers={"cache-control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Reading the form
# ---------------------------------------------------------------------------


def _plan_of(
    store: MemoryStore,
    submitted: Mapping[str, str],
    configuration: Configuration,
) -> RunPlan | str:
    """One plan from one submission, or the reason the submission was refused."""
    slug = submitted.get(CLIENT_FIELD, "").strip()
    requester = submitted.get(REQUESTER_FIELD, "").strip()
    justification = submitted.get(JUSTIFICATION_FIELD, "").strip()
    if not slug:
        return "an erasure names the Client it is performed for"
    if not requester:
        return "an erasure names the requester it was asked for by"
    if not justification:
        return "an erasure names the justification it is performed under"

    chosen = next((row for row in fleet(store) if row.slug == slug), None)
    if chosen is None:
        return "no Client carries that slug"

    policy = ResiduePolicy.from_configuration(configuration)
    auto = _number(submitted.get(AUTO_INCLUDE_FIELD), policy.auto_include_threshold)
    review = _number(submitted.get(REVIEW_FIELD), policy.review_threshold)
    if auto is None or review is None:
        return "a threshold was not a number"
    if review < auto:
        return "those thresholds describe no band"

    # The attempt's identifier and its idempotency key are one value in two forms, so
    # a client watching one is watching the run the other names.
    attempt = uuid4()
    return RunPlan(
        request=ErasureRequest(
            client_id=chosen.client_id,
            requester=requester,
            justification=justification,
            idempotency_key=attempt.hex,
            dry_run=_checked(submitted.get(DRY_RUN_FIELD)),
            skip_backup=_checked(submitted.get(SKIP_BACKUP_FIELD)),
        ),
        client_slug=slug,
        auto_include_threshold=auto,
        review_threshold=review,
    )


def _checked(value: str | None) -> bool:
    """Whether a checkbox was submitted as consent, absent meaning no."""
    return value is not None and value.strip().lower() in _CHECKED


def _number(text: str | None, held: float) -> float | None:
    """One submitted number, the held value where nothing was submitted, or None."""
    if text is None or not text.strip():
        return held
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"an erasure progress row carried {len(row)} columns rather than {width}")
    return row[index]


def _as_uuid(value: object) -> UUID:
    """One identifier column, however the driver rendered it."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _as_count(row: Sequence[object] | None) -> int:
    """One counted aggregate, absent meaning nothing was counted."""
    if row is None or not row:
        return 0
    return int(cast(int, row[0]))


def _residue_counts(row: Sequence[object] | None) -> tuple[int, int, int]:
    """The three residue aggregates, absent meaning the phase committed nothing."""
    if row is None:
        return (0, 0, 0)
    if len(row) != _RESIDUE_ROW_WIDTH:
        raise StoreError(f"a residue count row carried {len(row)} columns rather than 3")
    return (
        int(cast(int, row[0])),
        int(cast(int, row[1])),
        int(cast(int, row[2])),
    )


def _as_optional_text(value: object) -> str | None:
    """One nullable text column, absent staying absent."""
    return None if value is None else str(value)


# The phase vocabulary the template labels, taken from the engine's own enumeration so
# the console cannot name a phase the run row may not hold.
PHASE_NAMES: Final[tuple[str, ...]] = tuple(str(phase) for phase in Phase)
