"""The Erasure_Engine: the run skeleton, its transaction boundaries, and its endings.

Every phase of an erasure already exists as a module of its own. What is missing
until here is the thing that owns the run: what happens before the first mutation,
which transaction each phase's evidence lands in, what is held open across a model
call, and what a run leaves behind when it cannot finish. This module is that, and
nothing else — it sends the statements no phase owns, and it composes rather than
reimplements every statement a phase does own.

Nine claims carry the module.

**No lease, no run.** Ownership is taken before the first mutation, and a refused
acquisition ends the attempt with nothing written. The failure raised is the
no-lease one rather than the contention one, because from the run's point of view
the fact is that it holds no lease, and the current owner travels on the failure so
an operator learns whom to ask rather than only that it lost.

**An already-finalised attempt is reported, not performed.** The idempotency key is
looked up before ownership is even asked for, so a repeat of an attempt whose
acknowledgement was lost mutates nothing at all and returns the recorded outcome
exactly as it stands.

**No transaction is held open across a model call or a subprocess call.** The
backup runs between two transactions, the residue phase's provider calls happen
between the read that gathered its candidates and the transaction that records
them, and every rewrite is produced before the transaction that stores it opens.
That is why the phases are composed here as separate calls rather than wrapped in
one outer transaction: an outer transaction would hold locks for the duration of a
provider round trip and would make the whole run a single retry unit.

**Each phase's evidence commits in its own SERIALIZABLE transaction, and the phase
marker moves with it.** A crash mid-run therefore leaves a run row naming the phase
it reached and the evidence produced up to that point, which is resumable or
abortable rather than ambiguous. Every transaction goes through the store's retry
wrapper, and every body is replayable: the candidate and disposition writes are
idempotent by their unique constraints, the deletes are set-based, and the phase
marker and the counters are assignments rather than increments.

**The working tier is one number, not a record per row, and the delete carries the
fence.** The tier is disposable by construction, so its erasure is one set-based
delete for the tenant, and that delete and the count it returned commit in one
fenced transaction: the deletion is a mutation of memory content, so it presents the
generation this worker believes it holds, and a superseded owner removes no row and
records no number. Dispositions describe content that mattered; a working row is by
definition content that did not.

**A backup failure ends the run before any memory content is touched.** The backup
is secured between the run's own bookkeeping and the first statement that reaches
memory content, which means before the working tier is purged as well as before
phase three deletes anything, so a run with no evidence of the cluster's prior state
performs no deletion at all.

**Every evidence write on this path goes through the fence, the completion
included.** A worker superseded mid-run cannot purge the working tier, cannot record
its backup evidence, cannot record phase one's candidate set, cannot record a
residue finding, cannot extend that set with the residue phase's inclusions, cannot
record a disposition, cannot move the phase marker, and cannot declare the run or
its request finished. Two of those writes frame their own transactions inside the
modules that own them — the backup record and the detector's per-finding recording —
so this module hands each of those the generation it holds rather than reaching into
them. What no fence can withhold is the backup statement itself, which runs outside
every transaction because retrying it would issue a second backup: a superseded
worker may still write a bucket, and what it cannot do is record that it did. When a
write is refused as stale the run ends with the aborted status and the evidence
written under the valid generation stays exactly where it is: it was recorded by the
owner that held the run at the time, so removing it would destroy true evidence to
tidy up a false claim.

**A failure before phase one ends the run the same way a failure inside one does.**
Every step between the run row and the first phase — the ownership record, the
identity read, the backup, and the working purge — runs under a failure path that
records the abort and re-raises, because a run row left at the running status
refuses every binding write for that Client until something closes it. The one step
this cannot cover is the insert of the run row itself: a failure there rolls back
both of its statements, so there is no row to record an abort against and none to
refuse a binding write either.

**A dry run is the same skeleton with every memory-content mutation removed.** The
working delete is skipped entirely, the sweep and the residue phase run unchanged
because their writes are run-scoped evidence, phase three computes every decision
and records every disposition and performs no delete, no body write, no vector
replacement, and no attribution closure. What a dry run cannot produce is a
certificate, because there is nothing it could truthfully certify. It does acquire a
lease, so that two rehearsals cannot write two candidate sets for one Client, and it
gives that lease back on completion, so the run it rehearsed can start at once.

Every statement here is a whole module-level literal with bound parameters; no
identifier and no domain value is ever interpolated into statement text. Every
number the run turns on is read from the configuration surface.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from time import perf_counter
from typing import Final
from uuid import UUID, uuid4

from molt.backup import (
    BackupRecord,
    BackupSettings,
    BackupStatus,
    Clock,
    CommandRunner,
    StatementIssuer,
    record_backup,
    run_command,
    store_issuer,
    system_clock,
    take_backup,
)
from molt.confidence import ConfidencePolicy
from molt.config.resolve import Configuration
from molt.erase import adjudicator as adjudication
from molt.erase import residue as semantic
from molt.erase.disposition import (
    INSERT_DISPOSITION_STATEMENT,
    Candidate,
    Decision,
    DispositionKind,
    RunOwnership,
    SurgicalWrite,
    classify,
    decisions_for,
    fail_closed_delete,
    hard_delete,
    retain,
    surgical_redaction,
)
from molt.erase.lease import LeaseGrant, acquire, finalisation_for, finalise, owner_identifier
from molt.erase.lease import register_run as register_run_ownership
from molt.erase.lease import release as release_lease
from molt.erase.lease import renew as renew_lease
from molt.erase.rewriter import (
    ClientIdentity,
    RatioBand,
    Replacement,
    RewriteRequest,
    StructuralDiff,
    rewrite,
)
from molt.erase.sweep import SweepResult, sweep
from molt.errors import (
    BackupFailedError,
    LeaseNotHeld,
    LeaseRefusedError,
    ModelUnavailableError,
    StaleFencingGenerationError,
    StoreError,
)
from molt.models.event import JsonObject
from molt.providers import EmbeddingProvider, TextProvider
from molt.retention import default_interval, expiry_for
from molt.store import Cursor, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.store.capability import CapabilityRecord
from molt.store.erasure_lease import FinalisationRecord, LeaseInterval
from molt.store.fencing import fenced, fenced_disposition, fenced_run_completion
from molt.store.working import delete_client_scratch, record_working_purge
from molt.telemetry import Severity, log, metric
from molt.telemetry.inventory import UNIT_MILLISECONDS

__all__ = [
    "ABORT_RUN_STATEMENT",
    "BACKUP_ON_RUN_STATEMENT",
    "COMPLETE_RUN_STATEMENT",
    "COMPONENT",
    "EXTEND_CANDIDATES_STATEMENT",
    "INSERT_REQUEST_STATEMENT",
    "INSERT_RUN_STATEMENT",
    "PHASE_MARKER_STATEMENT",
    "REQUEST_STATUS_STATEMENT",
    "RUNS_METRIC",
    "RUN_DURATION_METRIC",
    "SELECT_BODY_STATEMENT",
    "SELECT_CANDIDATE_TEXT_STATEMENT",
    "SELECT_FLEET_STATEMENT",
    "SELECT_IDENTITY_STATEMENT",
    "SELECT_OTHER_IDENTITIES_STATEMENT",
    "SELECT_WORKING_ROWS_STATEMENT",
    "SEMANTIC_RESIDUE_REASON",
    "WORKING_ROWS_STATEMENT",
    "EngineSeams",
    "ErasureEngine",
    "ErasureRequest",
    "Phase",
    "PhaseProgress",
    "ProgressCallback",
    "RunOutcome",
    "RunStatus",
    "run_erasure",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "erase"

# The measurements a finished run is counted by. Both are undimensioned: the tenant
# is unbounded, so attaching it would turn one billable metric into as many as there
# are tenants, and it belongs in the log record instead.
#
# One counter covers every terminal state rather than one per outcome. The metric
# table declares a single run count, and the outcome distinction is carried by the
# log record each terminal path already writes, so an alarm on the declared name
# sees every run and the per-outcome breakdown costs no billable combination.
RUNS_METRIC: Final[str] = "erasure.runs"
RUN_DURATION_METRIC: Final[str] = "erasure.run_duration_ms"

# The outcome a terminal record names, so the distinction the single counter does
# not carry as a dimension is still recoverable from the record beside it.
COMPLETED_OUTCOME: Final[str] = "completed"
ABORTED_OUTCOME: Final[str] = "aborted"

# The duration is declared in milliseconds, and the monotonic reading is seconds.
_MILLISECONDS_PER_SECOND: Final[float] = 1000.0

# The selection reason a candidate the semantic phase admitted enters the set under.
# It is the sixth value the schema's reason check holds, and the only one no
# statement of the explicit sweep writes.
SEMANTIC_RESIDUE_REASON: Final[str] = "semantic_residue"

# How often the window is extended, as a fraction of the interval it covers. A
# third rather than a half, so a renewal that loses a conflict and is retried still
# lands well inside the window rather than at its edge.
RENEWAL_FRACTION: Final[int] = 3

# The labels this module's transactions appear under in a log record and in the note
# an exhausted retry attaches. One per boundary the design fixes, so an operator
# reading a log record learns which boundary kept losing rather than only that the
# run did.
OPEN_RUN_LABEL: Final[str] = "erasure_open_run"
WORKING_LABEL: Final[str] = "erasure_working_purge"
BACKUP_LABEL: Final[str] = "erasure_backup_on_run"
SWEEP_LABEL: Final[str] = "erasure_sweep"
PHASE_LABEL: Final[str] = "erasure_phase_marker"
EXTEND_LABEL: Final[str] = "erasure_extend_candidates"
DRY_DISPOSITION_LABEL: Final[str] = "erasure_dry_disposition"
REQUEST_LABEL: Final[str] = "erasure_request_status"

# The statuses the request row carries, which are the run's own statuses plus the
# one it is opened under.
_REQUEST_RUNNING: Final[str] = "running"
_REQUEST_COMPLETED: Final[str] = "completed"
_REQUEST_ABORTED: Final[str] = "aborted"

# What a fenced abort could not write, recorded rather than raised over the failure
# that caused the abort. A superseded owner cannot mark its own run aborted, which is
# the fence working as intended rather than a second fault to report.
_ABORT_REFUSED: Final[str] = (
    "the aborted status could not be recorded because this owner was superseded"
)


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# T0, statement one: the request this run answers. Opened at the running status,
# because a request whose run is in flight is not merely submitted.
INSERT_REQUEST_STATEMENT: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification, status) "
    "VALUES (%s, %s, %s, %s, %s)"
)

# T0, statement two: the run row. `t_before` is the cluster's own reading rather
# than a worker's, because it is the instant the certificate's before-and-after
# claim is anchored on. The thresholds are written as the values this run used, so
# a later reader sees what was in force then rather than what is in force now.
INSERT_RUN_STATEMENT: Final[str] = (
    "INSERT INTO erasure_run "
    "(id, request_id, client_id, requester, dry_run, status, phase, t_before, "
    "auto_include_threshold, review_threshold) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s)"
)

# The working purge's accounting: one aggregate number for the whole working tier of
# the tenant, written in the same fenced transaction as the delete that produced it,
# which runs after the backup rather than before it.
WORKING_ROWS_STATEMENT: Final[str] = (
    "UPDATE erasure_run SET working_rows_deleted = %s WHERE id = %s AND client_id = %s"
)

# What the run row says about its backup. The identifier is the control plane's
# where a backup was referenced and absent where one was taken, which is why the
# flag and the identifier are written together rather than either alone.
BACKUP_ON_RUN_STATEMENT: Final[str] = (
    "UPDATE erasure_run SET backup_id = %s, backup_skipped = %s WHERE id = %s AND client_id = %s"
)

# The durable phase marker. An assignment rather than a step, so replaying it is
# the same as running it once.
PHASE_MARKER_STATEMENT: Final[str] = (
    "UPDATE erasure_run SET phase = %s WHERE id = %s AND client_id = %s"
)

# T4: the completion. `t_after` is the cluster's reading for the same reason
# `t_before` is, and the pair bounds the window a verifier re-derives the corpus in.
COMPLETE_RUN_STATEMENT: Final[str] = (
    "UPDATE erasure_run SET status = %s, phase = %s, t_after = now(), finished_at = now() "
    "WHERE id = %s AND client_id = %s"
)

# The abort. The phase reached and the error detail are both recorded, because the
# question an operator asks of an aborted run is where it stopped and why. No
# `t_after` is written: nothing was completed, so there is no after to name.
ABORT_RUN_STATEMENT: Final[str] = (
    "UPDATE erasure_run SET status = %s, phase = %s, error_detail = %s, finished_at = now() "
    "WHERE id = %s AND client_id = %s"
)

# The request's own ending, which follows the run's.
REQUEST_STATUS_STATEMENT: Final[str] = "UPDATE erasure_request SET status = %s WHERE id = %s"

# T2's second half: every residue candidate the phase included enters the candidate
# set. Set-based from the rows the phase already committed, so no identifier crosses
# the wire and a replay writes nothing new.
EXTEND_CANDIDATES_STATEMENT: Final[str] = (
    "INSERT INTO erasure_candidate "
    "(run_id, artifact_id, artifact_kind, content_digest, selection_reason) "
    "SELECT %s, r.artifact_id, r.artifact_kind, NULL, %s FROM residue_candidate AS r "
    "WHERE r.run_id = %s AND r.included = true "
    "ON CONFLICT DO NOTHING"
)

# The fleet, which is what the neighbour query is permitted to return content for:
# residue is by definition content the erased tenant's own labels do not name, so
# restricting the search to that tenant would find none of it.
SELECT_FLEET_STATEMENT: Final[str] = "SELECT id FROM client"

# The erased tenant's names, which the rewrite validation and the disposition
# evidence both read.
SELECT_IDENTITY_STATEMENT: Final[str] = (
    "SELECT id, slug, display_name, content_markers FROM client WHERE id = %s"
)

# Every other tenant holding a current claim on one Artifact. The current-version
# predicate is the same one phase three classifies by, so the identities a rewrite
# must preserve are exactly the bindings that will survive it.
SELECT_OTHER_IDENTITIES_STATEMENT: Final[str] = (
    "SELECT c.id, c.slug, c.display_name, c.content_markers "
    "FROM client_binding AS b JOIN client AS c ON c.id = b.client_id "
    "WHERE b.artifact_id = %s AND b.superseded_by IS NULL AND b.client_id <> %s "
    "ORDER BY c.slug"
)

# The body a rewrite is produced from, read outside every transaction.
SELECT_BODY_STATEMENT: Final[str] = "SELECT body FROM derived_artifact WHERE id = %s"

# The aggregate the working tier was accounted by, read back from the row that holds
# it rather than carried in memory, so a reported outcome states what was stored.
SELECT_WORKING_ROWS_STATEMENT: Final[str] = (
    "SELECT working_rows_deleted FROM erasure_run WHERE id = %s"
)

# The text of a page of review-band candidates, over the two kinds that carry any.
# One statement over both tables rather than one per kind, because the page is
# decided as a page and a second round trip would order the two halves against
# nothing.
SELECT_CANDIDATE_TEXT_STATEMENT: Final[str] = (
    "SELECT id, text FROM ("
    "SELECT id, coalesce(text_body, '') AS text FROM ledger WHERE id = ANY(%s) "
    "UNION ALL "
    "SELECT id, body AS text FROM derived_artifact WHERE id = ANY(%s)"
    ") AS carried"
)

# How many columns each row shape carries, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_IDENTITY_ROW_WIDTH: Final[int] = 4
_TEXT_ROW_WIDTH: Final[int] = 2
_SINGLE_COLUMN: Final[int] = 1


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class Phase(StrEnum):
    """The phases the run row's marker holds, in the order a run passes through them."""

    SWEEP = "sweep"
    RESIDUE = "residue"
    DISPOSITION = "disposition"
    CERTIFICATE = "certificate"
    DONE = "done"


class RunStatus(StrEnum):
    """What became of one run, in the values the schema admits."""

    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class PhaseProgress:
    """One phase's completion, as the callback and the console both read it.

    The count means whatever the phase counts — candidates selected, candidates
    banded, Artifacts disposed — because a caller renders progress rather than
    arithmetic on it, and a shape with one field per phase would grow a field per
    phase added.
    """

    run_id: UUID
    client_id: UUID
    phase: Phase
    count: int
    dry_run: bool


ProgressCallback = Callable[[PhaseProgress], None]


@dataclass(frozen=True, slots=True)
class ErasureRequest:
    """What an operator asked for, and the key that identifies the asking.

    The key is the caller's rather than generated here, which is the whole point of
    it: a retry of a request the operator already issued has to present the same key
    for the run to be recognised as that same attempt.
    """

    client_id: UUID
    requester: str
    justification: str
    idempotency_key: str
    dry_run: bool = False
    skip_backup: bool = False

    def __post_init__(self) -> None:
        """Refuse a request missing anything the evidence has to record."""
        if not self.requester:
            raise ValueError("an erasure names the requester it was asked for by")
        if not self.justification:
            raise ValueError("an erasure names the justification it is performed under")
        if not self.idempotency_key:
            raise ValueError("an erasure names the key identifying this attempt")


@dataclass(frozen=True, slots=True)
class EngineSeams:
    """Everything the run reaches the world outside the cluster through.

    Every model call, every subprocess call, and every reading of a clock is here
    rather than imported at a call site, which is what makes the whole skeleton
    drivable with no credentials of any kind: a deployment supplies the configured
    providers, and a test supplies stubs.

    Attributes:
        configuration: The surface every number the run turns on is read from.
        backup: The backup target, control-plane command, and timeout.
        capabilities: The recorded probe results the backup path is chosen from.
        supersession: The Session context a surgical redaction's closing
            attribution Event is recorded within. Required rather than optional,
            because a blended Artifact that cannot record its withdrawal would
            otherwise be silently hard-deleted.
        text_provider: The Text_Provider the adjudication and the rewrites call, or
            None to take the fail-closed path in both.
        embedding_provider: The Embedding_Provider a replacement body's vector comes
            from, or None to fail closed on every rewrite.
        issuer: The seam the backup statement reaches the cluster through, or None
            to issue it on the run's own store.
        runner: The seam the control-plane command is invoked through.
        clock: The time source the recorded backup instant and the retention expiry
            are read from.
        progress: Where phase progress is reported, in addition to the durable
            marker the run row carries.
        prompt_cache_available: The cache capability the Provider_Selector
            recorded, which is what decides whether a Cache_Boundary is marked.
        owner: The identity ownership is taken under, derived from the host and the
            process where a deployment names none.
    """

    configuration: Configuration
    backup: BackupSettings
    capabilities: CapabilityRecord
    supersession: SupersessionContext
    text_provider: TextProvider | None = None
    embedding_provider: EmbeddingProvider | None = None
    issuer: StatementIssuer | None = None
    runner: CommandRunner = run_command
    clock: Clock = system_clock
    progress: ProgressCallback | None = None
    prompt_cache_available: bool = False
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one run did, in the numbers a certificate and a console both read.

    A replayed attempt carries the recorded finalisation and nothing else, because
    it performed no phase: the counts of a run that already happened are read from
    its own evidence rather than restated by the call that declined to repeat it.
    """

    client_id: UUID
    status: RunStatus
    phase: Phase
    dry_run: bool
    run_id: UUID | None = None
    generation: int | None = None
    working_rows_deleted: int = 0
    sweep: SweepResult | None = None
    residue: semantic.ResidueReport | None = None
    deleted: int = 0
    redacted: int = 0
    retained: int = 0
    fail_closed_rewrites: int = 0
    backup: BackupRecord | None = None
    finalisation: FinalisationRecord | None = None
    replayed: bool = False
    error_detail: str | None = None

    @property
    def completed(self) -> bool:
        """Whether this run finished and recorded a completion."""
        return self.status is RunStatus.COMPLETED

    @property
    def certificate_admissible(self) -> bool:
        """Whether a certificate may be assembled from this run's evidence.

        A dry run and an aborted run both answer false, for the same reason from
        two directions: neither performed the erasure a certificate would attest.
        """
        return self.completed and not self.dry_run


# ---------------------------------------------------------------------------
# The lease renewal, which runs beside every phase
# ---------------------------------------------------------------------------


class _Renewer:
    """Extends the run's window at a fraction of the interval, until it is stopped.

    A thread rather than a renewal folded into each phase, because a phase's length
    is a property of the corpus and the model rather than of the interval: a sweep
    over a large graph or a residue pass over many candidates would otherwise let a
    perfectly healthy worker lose ownership to expiry. Failures are recorded and not
    raised here — a renewal refused for supersession means the run's next fenced
    write is refused too, and that is where the run ends, on the main path, with the
    abort recorded once.
    """

    __slots__ = ("_failure", "_grant", "_interval", "_stop", "_store", "_thread")

    def __init__(self, store: MemoryStore, grant: LeaseGrant, interval: LeaseInterval) -> None:
        self._store = store
        self._grant = grant
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    @property
    def failure(self) -> BaseException | None:
        """The last renewal failure, or None where every renewal held."""
        return self._failure

    def __enter__(self) -> _Renewer:
        """Start renewing, so the window is extended for as long as the block runs."""
        thread = threading.Thread(target=self._loop, name="molt-lease-renewal", daemon=True)
        self._thread = thread
        thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Stop renewing, whether the run finished or failed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=float(self._interval.seconds))
            self._thread = None

    def _loop(self) -> None:
        every = max(1.0, self._interval.seconds / RENEWAL_FRACTION)
        while not self._stop.wait(every):
            try:
                self._grant = renew_lease(self._store, self._grant, interval=self._interval)
            except Exception as error:
                # Every cause is caught rather than the supersession alone, because a
                # renewal is not the run: a thread that raised here would end the
                # process's stack trace somewhere the run cannot report, and the run's
                # own next fenced write is where a lost lease has to be decided.
                self._failure = error
                log(
                    Severity.WARNING,
                    COMPONENT,
                    "a lease renewal did not hold, so the run's next fenced write decides it",
                    client_id=str(self._grant.client_id),
                    generation=self._grant.generation,
                    error_type=type(error).__name__,
                )
                return


# ---------------------------------------------------------------------------
# The Adjudicator adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ResidueAdjudicator:
    """The Residue_Detector's Adjudicator seam, answered by the Adjudicator module.

    The two modules were written to different shapes on purpose — the detector
    decides which candidates need judging and in what groups, the Adjudicator
    decides what the model is asked — and this is where the shapes meet. It is also
    where the candidate text is read: the detector's page carries identifiers and
    distances rather than bodies, because a body has no place in the ranking, and
    the prompt needs one. The read frames no transaction, so nothing is held open
    across the provider calls that follow.
    """

    store: MemoryStore
    adjudicator: adjudication.Adjudicator

    def adjudicate(self, batch: semantic.AdjudicationBatch) -> Sequence[semantic.Verdict]:
        """Judge one query Artifact's review-band candidates and translate the verdicts."""
        texts = _candidate_texts(self.store, [row.artifact_id for row in batch.candidates])
        query = adjudication.QueryArtifact(
            artifact_id=batch.query_artifact.artifact_id,
            text=batch.query_artifact.excerpt,
        )
        candidates = tuple(
            adjudication.Candidate(
                artifact_id=row.artifact_id,
                text=texts.get(row.artifact_id, ""),
            )
            for row in batch.candidates
        )
        judged = self.adjudicator.adjudicate(query, candidates)
        return tuple(_translated(verdict) for verdict in judged.verdicts)


def _translated(verdict: adjudication.Verdict) -> semantic.Verdict:
    """One Adjudicator verdict in the shape the Residue_Detector records."""
    return semantic.Verdict(
        artifact_id=verdict.artifact_id,
        classification=semantic.Classification(verdict.classification.value),
        included=verdict.included,
        decision_reason=verdict.reason,
        adjudicated=verdict.adjudicated,
        model_id=verdict.model_id,
        prompt_digest=verdict.prompt_digest,
        reasoning=verdict.reasoning,
    )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Tally:
    """What phase three did, counted as it happens rather than recomputed after."""

    deleted: int = 0
    redacted: int = 0
    retained: int = 0
    fail_closed: int = 0
    decisions: int = 0


class ErasureEngine:
    """One erasure run, from the lease to the completion or the abort.

    Held as an object because everything it needs resolves once per run — the store,
    the request, the seams, the resolved policy, and the ownership every fenced write
    presents — and because the abort path has to know which phase it is aborting
    from, which is state rather than an argument.
    """

    __slots__ = (
        "_aborted",
        "_backup",
        "_generation",
        "_grant",
        "_identity",
        "_interval",
        "_phase",
        "_policy",
        "_request",
        "_request_id",
        "_run_id",
        "_seams",
        "_started",
        "_store",
        "_tally",
    )

    def __init__(
        self,
        store: MemoryStore,
        request: ErasureRequest,
        seams: EngineSeams,
    ) -> None:
        self._store = store
        self._request = request
        self._seams = seams
        self._policy = semantic.ResiduePolicy.from_configuration(seams.configuration)
        self._interval = LeaseInterval.from_configuration(seams.configuration)
        self._phase = Phase.SWEEP
        self._tally = _Tally()
        self._request_id = uuid4()
        self._run_id = uuid4()
        # A monotonic reading rather than a wall clock: the duration reported at
        # either terminal state is an elapsed interval, and a clock adjustment
        # mid-run must not turn it into a negative one.
        self._started = perf_counter()
        self._grant: LeaseGrant | None = None
        self._generation = 0
        self._identity: ClientIdentity | None = None
        self._backup: BackupRecord | None = None
        # Whether this run has already recorded its abort, so a failure that aborts
        # with a detail of its own and then travels out through an enclosing handler
        # is recorded once rather than twice.
        self._aborted = False

    # -- the run ---------------------------------------------------------

    def run(self) -> RunOutcome:
        """Perform the run, or report the outcome an earlier attempt already recorded.

        Returns:
            What this run did, or the recorded finalisation of an attempt that
            already finished under this idempotency key.

        Raises:
            LeaseNotHeldError: This Client's erasure is owned by another worker, so
                nothing was mutated. The failure names the current owner.
            BackupFailedError: No backup evidence could be secured, so no memory
                content was touched: the backup precedes the working purge as well as
                every phase. The run is recorded aborted.
            StaleFencingGenerationError: This owner was superseded mid-run. The run
                is recorded aborted where the fence still admits that write, and the
                evidence written under the valid generation is retained.
            StoreError: The run could not be opened, or a step before phase one
                failed. Everything from the ownership record onwards records the
                aborted status before re-raising, so the run row does not stay at the
                running status refusing this Client's binding writes; a failure of the
                opening transaction itself leaves no run row at all.
        """
        recorded = finalisation_for(self._store, self._request.idempotency_key)
        if recorded is not None:
            return self._replayed(recorded)
        self._grant = self._acquire()
        self._generation = self._grant.generation
        self._open_run()
        self._prelude()
        with _Renewer(self._store, self._grant, self._interval):
            return self._phases()

    def _prelude(self) -> None:
        """Everything between the run row and phase one, with every failure aborted.

        These four steps used to sit outside every failure path, which left a run row
        at the running status with nothing to close it whenever one of them failed —
        and a running run row refuses every binding write for that Client for as long
        as it stands. So they abort exactly as a phase does: record the aborted status
        against the phase reached, then re-raise the failure that caused it. The lease
        is not released and the evidence already written is kept, which is the rule
        every abort on this path follows.

        The backup records its own abort with the detail the backup path reported,
        which is more than the type name this handler could name, so a second abort is
        not recorded over it.
        """
        try:
            register_run_ownership(self._store, self._held(), self._run_id)
            self._identity = self._read_identity()
            self._secure_backup()
            self._erase_working_tier()
        except StaleFencingGenerationError as refused:
            self._abort_once(
                f"a write before phase one was refused as stale: {type(refused).__name__}"
            )
            raise
        except (StoreError, BackupFailedError, ModelUnavailableError, ValueError) as failure:
            self._abort_once(f"the run could not reach phase one: {type(failure).__name__}")
            raise

    def _phases(self) -> RunOutcome:
        """The three phases and the completion, with every failure ending the run."""
        try:
            swept = self._sweep()
            report = self._residue()
            self._dispose(report)
            return self._complete(swept, report)
        except StaleFencingGenerationError as refused:
            self._abort_once(f"a mid-run write was refused as stale: {type(refused).__name__}")
            raise
        except (StoreError, ModelUnavailableError, ValueError) as failure:
            self._abort_once(f"the run could not be completed: {type(failure).__name__}")
            raise

    # -- T-1: ownership --------------------------------------------------

    def _acquire(self) -> LeaseGrant:
        """Take ownership before any mutation, or end the attempt naming the holder.

        A contended acquisition is reported as the no-lease failure rather than as
        the contention one, because what matters to a run is that it holds no lease:
        it performs nothing at all, and the current owner travels on the failure so
        the operator learns whom to ask rather than only that it lost.
        """
        owner = self._seams.owner or owner_identifier(self._seams.configuration)
        try:
            return acquire(
                self._store,
                self._request.client_id,
                owner,
                self._request.idempotency_key,
                interval=self._interval,
            )
        except LeaseRefusedError as refused:
            self._count_run(ABORTED_OUTCOME)
            log(
                Severity.ERROR,
                COMPONENT,
                "an erasure run began holding no lease, so it aborted before any mutation",
                client_id=str(self._request.client_id),
                owner=owner,
                current_owner=refused.owner,
                current_generation=refused.generation,
            )
            failure = LeaseNotHeld(
                f"erasure for this Client is held by {refused.owner} at generation "
                f"{refused.generation}, so this run holds no lease and mutated nothing"
            )
            raise failure from refused

    # -- T0: the run's own bookkeeping -----------------------------------

    def _open_run(self) -> None:
        """Insert the request and the run in one transaction, and record no abort for it.

        The run row is the conflict footprint concurrent binding writers read, which
        is why it is committed before any phase rather than written alongside the
        first one.

        This is the one step of the run that no abort can cover, and it is covered by
        the transaction instead: the request insert and the run insert are one
        transaction, so a failure of either leaves neither row. There is therefore no
        run row to record the aborted status against, and equally none at the running
        status to refuse this Client's binding writes, so the failure is counted and
        recorded as a terminal attempt and re-raised with nothing to close.

        The ownership record is deliberately not sent here: it is the first step of the
        prelude, which can abort, because by then the run row exists.
        """

        def body(cursor: Cursor) -> None:
            cursor.execute(
                INSERT_REQUEST_STATEMENT,
                (
                    self._request_id,
                    self._request.client_id,
                    self._request.requester,
                    self._request.justification,
                    _REQUEST_RUNNING,
                ),
            )
            cursor.execute(
                INSERT_RUN_STATEMENT,
                (
                    self._run_id,
                    self._request_id,
                    self._request.client_id,
                    self._request.requester,
                    self._request.dry_run,
                    RunStatus.RUNNING.value,
                    Phase.SWEEP.value,
                    self._policy.auto_include_threshold,
                    self._policy.review_threshold,
                ),
            )

        try:
            self._store.in_serializable(body, label=OPEN_RUN_LABEL)
        except (StoreError, ValueError):
            self._count_run(ABORTED_OUTCOME)
            log(
                Severity.ERROR,
                COMPONENT,
                "an erasure run could not open, so neither of its rows exists and no "
                "abort could be recorded against a run that was never inserted",
                client_id=str(self._request.client_id),
                run_id=str(self._run_id),
                generation=self._generation,
            )
            raise
        log(
            Severity.INFO,
            COMPONENT,
            "an erasure run was opened under a held lease",
            client_id=str(self._request.client_id),
            run_id=str(self._run_id),
            generation=self._generation,
            dry_run=self._request.dry_run,
        )

    # -- the working tier, after the backup ------------------------------

    def _erase_working_tier(self) -> None:
        """Delete the tenant's working rows as one set, and record the count as one number.

        Skipped entirely on a dry run, so no Working_Memory row is touched by a pass
        that promises to mutate no memory content. The delete and the count it
        returned are one fenced transaction rather than a delete framing its own and a
        fenced write after it: the deletion is a mutation of memory content, so it
        presents the generation this worker believes it holds, and a superseded owner
        therefore removes no row and records no number. The purge statement is the one
        the working tier owns, composed on this transaction's cursor rather than
        restated here.

        The measurement is emitted after the transaction committed, because the retry
        wrapper may have run the body more than once and one commit produces one
        count.
        """
        if self._request.dry_run:
            return

        def body(cursor: Cursor) -> int:
            removed = delete_client_scratch(cursor, self._request.client_id)
            cursor.execute(
                WORKING_ROWS_STATEMENT,
                (removed, self._run_id, self._request.client_id),
            )
            return removed

        purged = record_working_purge(
            fenced(
                self._store,
                self._request.client_id,
                self._generation,
                body,
                label=WORKING_LABEL,
            ),
            self._request.client_id,
        )
        log(
            Severity.INFO,
            COMPONENT,
            "the working tier of the Client was erased as one set and accounted as one number",
            client_id=str(self._request.client_id),
            run_id=str(self._run_id),
            working_rows_deleted=purged,
        )

    # -- the backup, which holds no transaction --------------------------

    def _secure_backup(self) -> None:
        """Secure backup evidence before the first memory-content mutation, or abort.

        This runs before the working purge as well as before every phase, so a run
        that could secure no evidence of the cluster's prior state has deleted no
        Working_Memory row either. Before, the purge ran first and a fatal backup
        aborted a run whose working tier was already gone.

        The statement and the control-plane command both run outside every
        transaction, and the recording write is its own short fenced transaction,
        so nothing is held open across either. The recording write carries the
        fence because the row is evidence about the run and a certificate reads its
        backup evidence out of it; the generation is available here because
        ownership was taken before this step, ahead of every mutation.
        """
        issuer = self._seams.issuer or store_issuer(self._store)
        secured = take_backup(
            self._run_id,
            capabilities=self._seams.capabilities,
            settings=self._seams.backup,
            issuer=issuer,
            runner=self._seams.runner,
            clock=self._seams.clock,
            skip=self._request.skip_backup,
        )
        self._backup = record_backup(
            self._store,
            secured,
            client_id=self._request.client_id,
            generation=self._generation,
        )
        self._record_backup_on_run(secured)
        if secured.fatal:
            detail = secured.detail or "no backup path succeeded"
            self._abort_once(detail)
            raise BackupFailedError(
                "no pre-erasure backup evidence could be secured, so the run aborted "
                "with every memory-content table unchanged"
            )

    def _record_backup_on_run(self, secured: BackupRecord) -> None:
        """Name the backup on the run row, so the run states its own evidence."""

        def body(cursor: Cursor) -> None:
            cursor.execute(
                BACKUP_ON_RUN_STATEMENT,
                (
                    secured.backup_id,
                    secured.status is BackupStatus.SKIPPED,
                    self._run_id,
                    self._request.client_id,
                ),
            )

        fenced(
            self._store,
            self._request.client_id,
            self._generation,
            body,
            label=BACKUP_LABEL,
        )

    # -- T1: the explicit sweep ------------------------------------------

    def _sweep(self) -> SweepResult:
        """Phase one, in its own fenced transaction, then the marker moves to phase two.

        The phase's statements are the sweep module's, composed on this transaction's
        cursor rather than restated, and the transaction is the fenced one rather than
        a plain serializable one: the candidate set, the run's Session record, and the
        pending-embedding count are all evidence about the run, so a superseded owner
        writes none of them. The recall floor is read from the run's own resolved
        configuration rather than resolved again inside the phase.
        """
        floor = ConfidencePolicy.from_configuration(self._seams.configuration).recall_floor

        def body(cursor: Cursor) -> SweepResult:
            return sweep(cursor, self._run_id, self._request.client_id, recall_floor=floor)

        swept = fenced(
            self._store,
            self._request.client_id,
            self._generation,
            body,
            label=SWEEP_LABEL,
        )
        self._advance(Phase.RESIDUE, swept.counts.total)
        return swept

    # -- the read phase, the model calls, and T2 -------------------------

    def _residue(self) -> semantic.ResidueReport:
        """Phase two: read, judge outside every transaction, then record and extend.

        The detector performs its own reads and its own recording transaction per
        finding, and the provider calls sit between them, so no transaction is open
        across a model call. What this adds is the second half of T2: the included
        candidates enter the candidate set, set-based from the rows just recorded, and
        that write is fenced, so a superseded owner extends no candidate set.

        The detector's own per-finding recording is fenced too, on the generation
        handed to it here. A `residue_candidate` row is evidence about the run, so a
        superseded owner records none, and the refusal propagates out of the walk to
        the phase handler above, which ends the run with the aborted status.
        """
        report = semantic.detect_residue(
            self._store,
            self._run_id,
            self._policy,
            permitted_clients=_fleet(self._store),
            fence=semantic.FindingFence(
                client_id=self._request.client_id,
                generation=self._generation,
            ),
            adjudicator=self._adjudicator(),
        )

        def body(cursor: Cursor) -> None:
            cursor.execute(
                EXTEND_CANDIDATES_STATEMENT,
                (self._run_id, SEMANTIC_RESIDUE_REASON, self._run_id),
            )

        fenced(
            self._store,
            self._request.client_id,
            self._generation,
            body,
            label=EXTEND_LABEL,
        )
        self._advance(Phase.DISPOSITION, len(report.findings))
        return report

    def _adjudicator(self) -> semantic.Adjudicator | None:
        """The Adjudicator seam, or None where no Text_Provider was supplied.

        None is not a silent exclusion: the detector takes the fail-closed path for
        every review-band candidate, which includes them and records that nothing
        judged them.
        """
        if self._seams.text_provider is None:
            return None
        return _ResidueAdjudicator(
            store=self._store,
            adjudicator=adjudication.Adjudicator.from_configuration(
                self._seams.configuration,
                self._seams.text_provider,
                prompt_cache_available=self._seams.prompt_cache_available,
            ),
        )

    # -- T3a and T3b: the dispositions -----------------------------------

    def _dispose(self, report: semantic.ResidueReport) -> None:
        """Phase three: classify once, then act on each decision by its kind."""
        exclusions = {
            finding.artifact_id: finding.decision_reason
            for finding in report.findings
            if not finding.included
        }
        decisions = decisions_for(classify(self._store, self._ownership()), exclusions=exclusions)
        self._tally.decisions = len(decisions)
        deletes = [d for d in decisions if d.disposition is DispositionKind.HARD_DELETE]
        surgical = [d for d in decisions if d.disposition is DispositionKind.SURGICAL_REDACTION]
        retentions = [d for d in decisions if d.disposition is DispositionKind.RETAINED]

        for decision in retentions:
            retain(self._store, self._ownership(), decision)
            self._tally.retained += 1
        self._delete(deletes)
        for decision in surgical:
            self._redact(decision)
        self._advance(Phase.DISPOSITION, self._tally.decisions)

    def _delete(self, decisions: Sequence[Decision]) -> None:
        """The batched hard delete, at the configured batch size, or its dry-run form."""
        if not decisions:
            return
        if self._request.dry_run:
            for decision in decisions:
                self._record_computed(decision, post_digest=None, bindings_after=())
                self._tally.deleted += 1
            return
        self._tally.deleted += hard_delete(
            self._store,
            self._ownership(),
            decisions,
            configuration=self._seams.configuration,
        )

    def _redact(self, decision: Decision) -> None:
        """One blended Artifact: rewrite outside every transaction, then write it once.

        A rewrite that produced nothing usable, for any cause, falls to the hard
        delete with the fail-closed reason. That is the bias the whole path is built
        around: blended memory is lost rather than an erased Client's content left in
        place.
        """
        produced = self._replacement(decision.candidate)
        if produced is None:
            self._tally.fail_closed += 1
            self._delete([fail_closed_delete(decision.candidate)])
            return
        replacement, vector, provider, model_id = produced
        if self._request.dry_run:
            self._record_computed(
                decision,
                post_digest=replacement.digest,
                bindings_after=self._survivors(decision.candidate),
                diff=replacement.diff,
            )
            self._tally.redacted += 1
            return
        written = surgical_redaction(
            self._store,
            self._ownership(),
            SurgicalWrite(
                decision=decision,
                replacement=replacement,
                vector=vector,
                embedding_provider=provider,
                embedding_model_id=model_id,
                expires_at=self._expiry(),
                context=self._seams.supersession,
            ),
        )
        if written is None:
            log(
                Severity.WARNING,
                COMPONENT,
                "a body moved between its rewrite and its transaction, so nothing was written",
                run_id=str(self._run_id),
                artifact_id=str(decision.artifact_id),
            )
            return
        self._tally.redacted += 1

    def _replacement(
        self,
        candidate: Candidate,
    ) -> tuple[Replacement, tuple[float, ...], str, str] | None:
        """A validated replacement and its vector, or None where neither exists.

        Both calls happen here, outside every transaction, which is the constraint
        that shapes the whole phase: the surgical transaction takes the text and the
        vector as values it was handed rather than obtaining either.
        """
        provider = self._seams.text_provider
        embedder = self._seams.embedding_provider
        if provider is None or embedder is None:
            return None
        body = _body_of(self._store, candidate.artifact_id)
        retained = _identities_besides(self._store, candidate.artifact_id, self._request.client_id)
        if body is None or not retained:
            return None
        try:
            replacement = rewrite(
                provider,
                RewriteRequest(
                    artifact_id=candidate.artifact_id,
                    body=body,
                    erased=self._erased(),
                    retained=retained,
                ),
                band=RatioBand.from_configuration(self._seams.configuration),
            )
            vectors = embedder.embed([replacement.text])
        except (ModelUnavailableError, ValueError):
            return None
        if not vectors:
            return None
        vector = tuple(float(value) for value in vectors[0])
        return replacement, vector, embedder.name, embedder.model_id

    def _record_computed(
        self,
        decision: Decision,
        *,
        post_digest: str | None,
        bindings_after: tuple[str, ...],
        diff: StructuralDiff | None = None,
    ) -> None:
        """Record one computed disposition and mutate no memory content.

        This is the dry run's whole phase three. The statement is the one phase three
        owns, imported rather than restated, so a column added there cannot be missed
        here; what this supplies is the parameter tuple, because that module exposes
        no writer that records a decision without performing it.
        """
        candidate = decision.candidate
        ownership = self._ownership()

        def body(cursor: Cursor) -> None:
            cursor.execute(
                INSERT_DISPOSITION_STATEMENT,
                (
                    ownership.run_id,
                    candidate.artifact_id,
                    candidate.artifact_kind.value,
                    decision.disposition.value,
                    decision.reason,
                    candidate.selection_reason,
                    candidate.content_digest,
                    post_digest,
                    list(candidate.binding_slugs),
                    list(bindings_after),
                    None if diff is None else diff.removed_segments,
                    None if diff is None else diff.retained_segments,
                    ownership.generation,
                ),
            )

        fenced_disposition(self._store, ownership.client_id, ownership.generation, body)

    # -- T4: the completion ----------------------------------------------

    def _complete(
        self,
        swept: SweepResult,
        report: semantic.ResidueReport,
    ) -> RunOutcome:
        """Record the completion behind the fence, then finalise the attempt once.

        The completion and the finalisation are two writes rather than one because
        they answer two questions: the run says what it did, and the attempt says it
        is over and what it returned. Both go through the fence, so a superseded
        owner declares neither.
        """
        recorded = self._record_completion()
        self._request_status(_REQUEST_COMPLETED)
        self._phase = Phase.CERTIFICATE
        self._report(Phase.CERTIFICATE, self._tally.decisions)
        self._count_run(COMPLETED_OUTCOME)
        log(
            Severity.INFO,
            COMPONENT,
            "an erasure run completed and recorded its finalising generation",
            client_id=str(self._request.client_id),
            run_id=str(self._run_id),
            generation=recorded.generation,
            dry_run=self._request.dry_run,
        )
        # A completed run gives back the remainder of its window, and a dry run is a
        # completed run. The lease exists to keep two workers from erasing one tenant
        # at once; a dry run mutated no memory content, so there is nothing left for
        # its window to protect. Holding it would block the very run the rehearsal was
        # performed for, until the interval ran out on the cluster's clock — a lease
        # cannot be re-taken by the same owner under a new attempt key. Only an
        # aborting run keeps its window, and it keeps it by calling nothing at all, so
        # a crashed worker and an aborted one release ownership by the same rule.
        release_lease(self._store, self._held())
        return RunOutcome(
            client_id=self._request.client_id,
            status=RunStatus.COMPLETED,
            phase=Phase.CERTIFICATE,
            dry_run=self._request.dry_run,
            run_id=self._run_id,
            generation=recorded.generation,
            working_rows_deleted=_working_rows(self._store, self._run_id),
            sweep=swept,
            residue=report,
            deleted=self._tally.deleted,
            redacted=self._tally.redacted,
            retained=self._tally.retained,
            fail_closed_rewrites=self._tally.fail_closed,
            backup=self._backup,
            finalisation=recorded,
        )

    def _record_completion(self) -> FinalisationRecord:
        """The completion statement and the finalisation, both behind the fence."""

        def body(cursor: Cursor) -> None:
            cursor.execute(
                COMPLETE_RUN_STATEMENT,
                (
                    RunStatus.COMPLETED.value,
                    Phase.CERTIFICATE.value,
                    self._run_id,
                    self._request.client_id,
                ),
            )

        fenced_run_completion(self._store, self._request.client_id, self._generation, body)
        return finalise(self._store, self._held(), self._run_id, self._result())

    def _result(self) -> JsonObject:
        """The outcome a repeated attempt is answered with, recorded on the run row."""
        return {
            "run_id": str(self._run_id),
            "status": RunStatus.COMPLETED.value,
            "dry_run": self._request.dry_run,
            "deleted": self._tally.deleted,
            "redacted": self._tally.redacted,
            "retained": self._tally.retained,
        }

    # -- the measurements ------------------------------------------------

    def _count_run(self, outcome: str) -> None:
        """Count one terminal run and report how long it took, in milliseconds.

        Both measurements are emitted at every terminal state, including the one
        reached before any mutation, so the run count is the count of attempts an
        operator can compare against the certificates that exist. The outcome is a
        log field rather than a dimension: the metric table declares one
        undimensioned run count, and an outcome dimension would spend part of the
        billable bound restating what the record beside it already says.
        """
        elapsed = max(perf_counter() - self._started, 0.0) * _MILLISECONDS_PER_SECOND
        metric(RUNS_METRIC)
        metric(RUN_DURATION_METRIC, elapsed, unit=UNIT_MILLISECONDS)
        log(
            Severity.INFO,
            COMPONENT,
            "an erasure run reached a terminal state",
            run_id=str(self._run_id),
            outcome=outcome,
            duration_ms=round(elapsed, 3),
            phase=self._phase.value,
        )

    # -- the abort -------------------------------------------------------

    def _abort_once(self, detail: str) -> None:
        """Record the abort unless this run already recorded one.

        The backup path aborts with the detail the backup itself reported, and the
        failure it then raises travels out through the prelude's own handler, so
        without this the same run would record two aborts and be counted twice. The
        first detail is the specific one, so the first abort is the one that stands.
        """
        if self._aborted:
            return
        self._abort(detail)

    def _abort(self, detail: str) -> None:
        """Record the aborted status with the phase reached, and leave the lease alone.

        The lease is deliberately not released: a crashed worker and a cleanly
        aborted worker then give ownership back by the same rule, on the cluster's
        clock, so the case a graceful path would hide is the case the fence exists
        for. Where this write is itself refused for supersession the refusal is
        recorded rather than raised over the failure that caused the abort, because a
        superseded owner not being able to mark its own run aborted is the fence
        working.
        """

        def body(cursor: Cursor) -> None:
            cursor.execute(
                ABORT_RUN_STATEMENT,
                (
                    RunStatus.ABORTED.value,
                    self._phase.value,
                    detail,
                    self._run_id,
                    self._request.client_id,
                ),
            )

        self._aborted = True
        self._count_run(ABORTED_OUTCOME)
        try:
            fenced_run_completion(self._store, self._request.client_id, self._generation, body)
            self._request_status(_REQUEST_ABORTED)
        except StaleFencingGenerationError:
            log(
                Severity.ERROR,
                COMPONENT,
                _ABORT_REFUSED,
                client_id=str(self._request.client_id),
                run_id=str(self._run_id),
                generation=self._generation,
            )
            return
        log(
            Severity.ERROR,
            COMPONENT,
            "an erasure run aborted, and the evidence written under this generation stands",
            client_id=str(self._request.client_id),
            run_id=str(self._run_id),
            phase=self._phase.value,
            detail=detail,
        )

    def _request_status(self, status: str) -> None:
        """Follow the run's ending on the request the run answers, behind the fence.

        The status of the request is the run's own claim about what became of the
        asking, so it is guarded by the same generation every other claim of this run
        presents: a superseded owner declares neither the run nor its request finished.
        """

        def body(cursor: Cursor) -> None:
            cursor.execute(REQUEST_STATUS_STATEMENT, (status, self._request_id))

        fenced(
            self._store,
            self._request.client_id,
            self._generation,
            body,
            label=REQUEST_LABEL,
        )

    # -- shared state ----------------------------------------------------

    def _advance(self, phase: Phase, count: int) -> None:
        """Move the durable phase marker and report the same step to the callback."""
        self._phase = phase

        def body(cursor: Cursor) -> None:
            cursor.execute(
                PHASE_MARKER_STATEMENT,
                (phase.value, self._run_id, self._request.client_id),
            )

        fenced(
            self._store,
            self._request.client_id,
            self._generation,
            body,
            label=PHASE_LABEL,
        )
        self._report(phase, count)

    def _report(self, phase: Phase, count: int) -> None:
        """Report one phase's completion to the callback, having moved no marker.

        Kept apart from the marker because the completion statement writes the
        certificate phase itself, and a second assignment of the same value would be
        a transaction that recorded nothing.
        """
        if self._seams.progress is not None:
            self._seams.progress(
                PhaseProgress(
                    run_id=self._run_id,
                    client_id=self._request.client_id,
                    phase=phase,
                    count=count,
                    dry_run=self._request.dry_run,
                )
            )

    def _ownership(self) -> RunOwnership:
        """The run, the tenant, and the generation every phase-three write presents."""
        return RunOwnership(
            run_id=self._run_id,
            client_id=self._request.client_id,
            slug=self._erased().slug,
            generation=self._generation,
        )

    def _held(self) -> LeaseGrant:
        """The grant this run performs under, refusing a call made before it was taken."""
        if self._grant is None:
            raise StoreError("this run holds no lease, so nothing it would write belongs to it")
        return self._grant

    def _erased(self) -> ClientIdentity:
        """The erased tenant's names, read once per run."""
        if self._identity is None:
            self._identity = self._read_identity()
        return self._identity

    def _read_identity(self) -> ClientIdentity:
        """Read the erased tenant's names, refusing a Client that is not recorded."""
        found = _identity_of(self._store, self._request.client_id)
        if found is None:
            raise StoreError(
                "the Client this erasure names is not recorded, so there is nothing to erase"
            )
        return found

    def _expiry(self) -> datetime:
        """The retention instant a replacement vector is written under."""
        return expiry_for(self._seams.clock(), default_interval(self._seams.configuration))

    def _survivors(self, candidate: Candidate) -> tuple[str, ...]:
        """The binding slugs a surgical redaction leaves, which is the set less one."""
        return tuple(slug for slug in candidate.binding_slugs if slug != self._erased().slug)

    def _replayed(self, recorded: FinalisationRecord) -> RunOutcome:
        """The outcome of an attempt that already finished, returned unchanged."""
        log(
            Severity.WARNING,
            COMPONENT,
            "a repeated erasure attempt returned the recorded finalisation and mutated nothing",
            client_id=str(self._request.client_id),
            run_id=str(recorded.run_id),
            generation=recorded.generation,
        )
        return RunOutcome(
            client_id=self._request.client_id,
            status=RunStatus.COMPLETED,
            phase=Phase.CERTIFICATE,
            dry_run=self._request.dry_run,
            run_id=recorded.run_id,
            generation=recorded.generation,
            finalisation=recorded,
            replayed=True,
        )


def run_erasure(
    store: MemoryStore,
    request: ErasureRequest,
    seams: EngineSeams,
) -> RunOutcome:
    """Perform one erasure run for one Client, or report what an earlier attempt did.

    This is the entry point the CLI verb and the Web_Console both call. The
    certificate is deliberately not assembled here: it is built from the evidence
    this run committed, by the Certificate_Builder, after the completion this
    function records.
    """
    return ErasureEngine(store, request, seams).run()


# ---------------------------------------------------------------------------
# The reads this module owns
# ---------------------------------------------------------------------------


def _fleet(store: MemoryStore) -> tuple[UUID, ...]:
    """Every Client the neighbour query may return content for."""

    def body(cursor: Cursor) -> tuple[UUID, ...]:
        cursor.execute(SELECT_FLEET_STATEMENT, ())
        return tuple(_as_uuid(_column(row, 0, _SINGLE_COLUMN)) for row in cursor.fetchall())

    return store.read(body)


def _identity_of(store: MemoryStore, client_id: UUID) -> ClientIdentity | None:
    """One Client's names, or None where the identifier names no Client."""

    def body(cursor: Cursor) -> ClientIdentity | None:
        cursor.execute(SELECT_IDENTITY_STATEMENT, (client_id,))
        row = cursor.fetchone()
        return None if row is None else _identity(row)

    return store.read(body)


def _identities_besides(
    store: MemoryStore,
    artifact_id: UUID,
    client_id: UUID,
) -> tuple[ClientIdentity, ...]:
    """Every other Client holding a current claim on one Artifact."""

    def body(cursor: Cursor) -> tuple[ClientIdentity, ...]:
        cursor.execute(SELECT_OTHER_IDENTITIES_STATEMENT, (artifact_id, client_id))
        return tuple(_identity(row) for row in cursor.fetchall())

    return store.read(body)


def _body_of(store: MemoryStore, artifact_id: UUID) -> str | None:
    """One Artifact's body, or None where the row carries none."""

    def body(cursor: Cursor) -> str | None:
        cursor.execute(SELECT_BODY_STATEMENT, (artifact_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        held = _column(row, 0, _SINGLE_COLUMN)
        return held if isinstance(held, str) else None

    return store.read(body)


def _candidate_texts(store: MemoryStore, ids: Sequence[UUID]) -> Mapping[UUID, str]:
    """The text of a page of candidates, over both kinds that carry any."""
    if not ids:
        return {}
    wanted = list(dict.fromkeys(ids))

    def body(cursor: Cursor) -> dict[UUID, str]:
        cursor.execute(SELECT_CANDIDATE_TEXT_STATEMENT, (wanted, wanted))
        carried: dict[UUID, str] = {}
        for row in cursor.fetchall():
            _column(row, 0, _TEXT_ROW_WIDTH)
            carried[_as_uuid(row[0])] = _as_text(row[1])
        return carried

    return store.read(body)


def _working_rows(store: MemoryStore, run_id: UUID) -> int:
    """The aggregate working-row count the run row records."""

    def body(cursor: Cursor) -> int:
        cursor.execute(SELECT_WORKING_ROWS_STATEMENT, (run_id,))
        row = cursor.fetchone()
        if row is None:
            raise StoreError("the run this count belongs to is not recorded")
        return _as_count(_column(row, 0, _SINGLE_COLUMN))

    return store.read(body)


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------


def _identity(row: Sequence[object]) -> ClientIdentity:
    """Build one Client identity from a selected row."""
    _column(row, 0, _IDENTITY_ROW_WIDTH)
    return ClientIdentity(
        client_id=_as_uuid(row[0]),
        slug=_as_text(row[1]),
        display_name=_as_text(row[2]),
        content_markers=_as_markers(row[3]),
    )


def _as_markers(value: object) -> tuple[str, ...]:
    """The content markers column, which a Client may hold none of."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(_as_text(marker) for marker in value)
    raise _unexpected(value, "a list of markers")


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a selected row carried {len(row)} column(s) where {width} are read")
    return row[index]


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a value whose type is not the one the schema declares.

    The type is named and the value is not, because a message belongs in a log
    record and stored content does not.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_count(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a count")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a count")
