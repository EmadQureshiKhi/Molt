"""The Erasure_Certificate builder: stored evidence in, one signed document out.

A certificate is the deliverable a departing tenant's reviewer reads, so every
field of it has to be re-derivable by somebody holding no access to the process
that produced it. That single obligation shapes everything here.

**Only stored rows reach the payload.** Nothing is carried across from the
engine's memory, and no field is computed from a value that lived only inside the
run. The evidence tables are read by the statements below and the payload is a
function of what they returned, which is why an independent verifier can re-read
the same rows and reach the same document.

**No field is derived from the working tier.** The disposable tier is exempt from
attribution by construction, so a certificate that read it would be attesting
over rows nothing was ever promised about. The tier's removal for the tenant is
carried as one aggregate count on the run row instead, and no verification query
this module emits names that tier either.

**The Ledger-plus-Dispositions derivation is the primary count mechanism, on
every certificate.** The measured collection horizon is far shorter than the
evidence lifetime of a certificate, so a document leaning on a point-in-time read
would stop being re-derivable within roughly an hour and a quarter of being
issued. The derived figures come from append-only rows and stay derivable
indefinitely. The historical read is attempted only when both of the run's
instants still fall inside the measured horizon, and its outcome is recorded in a
corroboration block as attempted, within-horizon, and agreement rather than
replacing anything.

**Assembly is split from serialisation, and serialisation is not implemented
here.** The one canonical serialiser in the codebase produces the bytes a
signature commits to. This module builds the payload and calls that serialiser;
it defines no ordering, separator, or number rule of its own, because a second
rule would be a rule that has to agree with the first.

**Signing and object storage are injected seams.** The signer is the same
structural protocol the checkpoint signs through, and the object store is a
protocol of two values in and a version identifier out. Neither is constructed
here, so the whole path is exercised with no credential and no network call, and
a deployment holds one signing key and one signing path rather than two.

**The signed document is persisted before storage is attempted.** A failed object
write leaves a signed certificate in the cluster with the failure recorded on the
row, because a document nobody could upload is still evidence. A signing failure
is different in kind: no signed document exists, so certificate creation is
abandoned and the run record is left alone.

**Every statement is a whole module-level literal with bound parameters.** No
identifier and no domain value is interpolated, here or anywhere below.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol, TypeVar, cast
from uuid import UUID

from molt.attest.canonical import CERTIFICATE_ARRAY_RULES, CanonicalValue, canonicalise
from molt.attest.checkpoint import DigestSigner, StoredCheckpoint, latest_before
from molt.config.resolve import Configuration
from molt.errors import (
    HistoricalHorizonError,
    MoltError,
    SigningUnavailableError,
    StoreError,
)
from molt.models.event import require_aware
from molt.store import Cursor, MemoryStore
from molt.store.attribution import FirstAttribution, first_attributions
from molt.store.fencing import fenced_certificate
from molt.store.historical import GcHorizon, gc_horizon, historical, within_gc_horizon
from molt.telemetry import Severity, log, metric

__all__ = [
    "ALGORITHM_KEY",
    "AUDIT_SNAPSHOT_QUERY",
    "BACKUP_QUERY",
    "BUCKET_KEY",
    "CAVEATS",
    "CERTIFICATE_VERSION",
    "COMPONENT",
    "COUNT_DERIVATION",
    "DISPOSITIONS_QUERY",
    "HARD_DELETE",
    "INSERT_CERTIFICATE_STATEMENT",
    "ISSUED_METRIC",
    "KEY_ID_KEY",
    "LINEAGE_SUBGRAPH_QUERY",
    "LIVE_BOUND_COUNT_QUERY",
    "OBJECT_LOCK_DAYS_KEY",
    "OBJECT_LOCK_MODE",
    "PREFIX_KEY",
    "RESIDUE_QUERY",
    "RETAINED",
    "RUN_QUERY",
    "RUN_SESSIONS_QUERY",
    "STORAGE_FAILED",
    "STORAGE_FAILURE_METRIC",
    "STORAGE_PENDING",
    "STORAGE_STORED",
    "SURGICAL_REDACTION",
    "UPDATE_STORAGE_STATEMENT",
    "VERIFICATION_TEMPLATES",
    "AuditWindow",
    "BackupFacts",
    "BoundCounts",
    "CertificateObjectStore",
    "CertificatePolicy",
    "CheckpointFacts",
    "CorroborationFacts",
    "CountFacts",
    "DispositionRecord",
    "Evidence",
    "IssuedCertificate",
    "LineageEdgeRecord",
    "OwnershipFacts",
    "RequestFacts",
    "ResidueRecord",
    "RunFacts",
    "SessionTip",
    "SignedCertificate",
    "VerificationTemplate",
    "assemble",
    "certificate_payload",
    "corroborate",
    "derived_counts",
    "envelope_of",
    "first_attribution_snapshot",
    "issue",
    "object_key_for",
    "persist",
    "publish",
    "sign",
    "verification_queries",
]

# What this module is called in a log record.
COMPONENT: Final[str] = "certificate"

# The offset every reading this module takes is expressed in, so a retention
# instant and a horizon comparison never depend on where the process runs.
_UTC: Final = UTC

# What a framed write returns, which is the body's own result and nothing added.
_Result = TypeVar("_Result")

# The contract version the payload declares. A string, because every scalar of the
# payload is a string under the canonical rules and a version is no exception.
CERTIFICATE_VERSION: Final[str] = "1"

# The one value `count_derivation` ever holds. The historical read corroborates and
# never replaces, so there is no second mechanism a certificate could name.
COUNT_DERIVATION: Final[str] = "ledger_and_dispositions"

# The configuration keys this module reads. The signing key and the algorithm are
# the same two the checkpoint signs under, deliberately: one deployment, one key.
KEY_ID_KEY: Final[str] = "MOLT_KMS_KEY_ID"
ALGORITHM_KEY: Final[str] = "MOLT_KMS_SIGNING_ALGORITHM"
BUCKET_KEY: Final[str] = "MOLT_CERT_BUCKET"
PREFIX_KEY: Final[str] = "MOLT_CERT_PREFIX"
OBJECT_LOCK_DAYS_KEY: Final[str] = "MOLT_CERT_OBJECT_LOCK_DAYS"

# The retention posture the delivered configuration applies. Governance retention
# can be released by a principal holding the release permission, so teardown is
# possible without manual intervention; the compliance posture is recorded in the
# documentation as the production one precisely because nothing can release it.
OBJECT_LOCK_MODE: Final[str] = "GOVERNANCE"

# The three storage states the certificate row admits, in the schema's spellings.
STORAGE_PENDING: Final[str] = "pending"
STORAGE_STORED: Final[str] = "stored"
STORAGE_FAILED: Final[str] = "failed"

# The disposition spellings the schema's own check constraint admits. Bound as
# parameters or compared in the process, never written into statement text.
HARD_DELETE: Final[str] = "hard_delete"
SURGICAL_REDACTION: Final[str] = "surgical_redaction"
RETAINED: Final[str] = "retained"

# The two measurements this module emits.
ISSUED_METRIC: Final[str] = "certificate.issued"
STORAGE_FAILURE_METRIC: Final[str] = "certificate.storage_failures"

# What the guarded writes appear under in a log record.
_PERSIST_LABEL: Final[str] = "certificate_persist"
_STORAGE_LABEL: Final[str] = "certificate_storage"

# The object name's fixed parts. The tenant slug and the run identifier are the
# only variables, and both are path components rather than statement text.
_KEY_SUFFIX: Final[str] = ".json"
_KEY_SEPARATOR: Final[str] = "/"

# The expectation every template of the fixed set declares. An empty result set is
# the whole claim: a row returned is an artifact the erasure did not reach.
_EMPTY: Final[str] = "empty"

# The four caveat statements every certificate carries. They are constants rather
# than composed sentences, so two certificates state the same limits in the same
# words and a reviewer comparing documents sees content differ and framing not.
CAVEATS: Final[Mapping[str, str]] = {
    "historical_read_bound": (
        "Historical reads are bounded by the cluster garbage-collection interval, "
        "which is measured on the cluster rather than assumed by this software."
    ),
    "durable_evidence": (
        "The append-only Ledger and the recorded Dispositions are the primary and "
        "durable evidence for long-horizon provenance; historical reads are a "
        "corroborating convenience layer performed only when both instants fall "
        "inside the garbage-collection horizon."
    ),
    "checkpoint_scope": (
        "A Ledger_Checkpoint provides tamper evidence rather than tamper proofing. "
        "The per-Session hash chain reaches this certificate only for the Sessions "
        "this run touched; the named checkpoint covers every Session in its window."
    ),
    "working_tier_excluded": (
        "No field of this certificate is derived from the working memory tier, and "
        "no verification query reads it. Working rows removed for this Client are "
        "reported as one aggregate count."
    ),
}


# ---------------------------------------------------------------------------
# The fixed verification-query template set
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationTemplate:
    """One member of the fixed set of queries a certificate may carry.

    The set is fixed at build time and validated against at verify time, so a
    hostile certificate cannot ask a verifier to run a statement of its own
    choosing. The tenant identifier travels as a bound parameter, so a slug or an
    identifier can never reach statement text through the certificate either.

    The text carries the document's positional placeholders rather than the
    driver's. This literal is evidence a reader checks and never a statement this
    process sends, so it is written in the form an outside auditor would run it in;
    the statement actually executed for the query is the verifier's own literal.
    The two forms must agree token for token, because a verifier admits a query by
    comparing this text against its documented counterpart.
    """

    name: str
    sql: str
    expectation: str = _EMPTY


# Both templates read evidence tables outside the working tier, and the tier's own
# table is named by neither. The predicate on the attribution query is the current
# form, because a superseded version naming the tenant is a historical statement
# and counting it would make an after-count non-zero for every tenant whose
# attribution had ever changed.
VERIFICATION_TEMPLATES: Final[tuple[VerificationTemplate, ...]] = (
    VerificationTemplate(
        name="no_current_attribution_remains",
        sql=(
            "SELECT b.artifact_id FROM client_binding AS b "
            "WHERE b.client_id = $1 AND b.superseded_by IS NULL"
        ),
    ),
    VerificationTemplate(
        name="no_sessions_remain",
        sql="SELECT s.id FROM session AS s WHERE s.client_id = $1",
    ),
)


# ---------------------------------------------------------------------------
# The statements
# ---------------------------------------------------------------------------

# The run, the request it answers, the tenant it erased, and the lease that owned
# it. One statement rather than four reads, because the four rows are one fact
# about one run and reading them apart would admit a torn view of it.
RUN_QUERY: Final[str] = """
SELECT r.id, r.client_id, r.dry_run, r.t_before, r.t_after,
       r.auto_include_threshold, r.review_threshold, r.unembedded_count,
       r.working_rows_deleted, r.fencing_generation, r.idempotency_key,
       q.id, q.requester, q.justification, q.submitted_at,
       c.slug, l.owner
FROM erasure_run AS r
JOIN erasure_request AS q ON q.id = r.request_id
JOIN client AS c ON c.id = r.client_id
LEFT JOIN erasure_lease AS l ON l.id = r.lease_id
WHERE r.id = %s
"""

# The backup evidence. The path value and the two flags are read together because
# the certificate's claim is which of the two paths ran, and a row where neither
# flag holds names the path that was attempted and did not complete.
BACKUP_QUERY: Final[str] = """
SELECT backup_id, backup_path, target_uri, command, taken, referenced
FROM backup_record
WHERE run_id = %s
ORDER BY id
LIMIT 1
"""

# One row per touched Artifact, both digests and both binding arrays included. The
# arrays are what let a verifier check a redaction's whole claim without
# reconstructing bindings the run deleted.
DISPOSITIONS_QUERY: Final[str] = """
SELECT artifact_id, artifact_kind, disposition, reason, selection_reason,
       pre_digest, post_digest, bindings_before, bindings_after
FROM disposition
WHERE run_id = %s
ORDER BY artifact_id
"""

# Every residue candidate with its distance and, where the adjudicator ran, the
# evidence it produced. A candidate the auto-inclusion band admitted carries no
# model identifier and no reasoning at all rather than defaulted ones.
RESIDUE_QUERY: Final[str] = """
SELECT artifact_id, artifact_kind, cosine_distance, band, included,
       decision_reason, adjudicated, model_id, reasoning
FROM residue_candidate
WHERE run_id = %s
ORDER BY artifact_id
"""

# The chain tip of every Session the run touched, as the run recorded it. Read from
# the run's own per-Session rows rather than from the live chain, because the rows
# the chain was built from may be gone and the recorded tip is the evidence.
RUN_SESSIONS_QUERY: Final[str] = """
SELECT session_id, terminal_chain_digest, terminal_seq, row_count
FROM run_session
WHERE run_id = %s
ORDER BY session_id
"""

# The lineage subgraph covering the touched Artifacts, in both directions: an edge
# whose child was touched and an edge whose parent was touched both belong to the
# subgraph, because the certificate's claim is about what the touched set was
# connected to.
LINEAGE_SUBGRAPH_QUERY: Final[str] = """
SELECT child_id, parent_id, parent_kind, derivation_method
FROM lineage_edge
WHERE child_id = ANY (%s::UUID[]) OR parent_id = ANY (%s::UUID[])
ORDER BY child_id, parent_id
"""

# The cluster audit records covering the run window, as retrieved and stored.
AUDIT_SNAPSHOT_QUERY: Final[str] = """
SELECT window_start, window_end, records
FROM audit_log_snapshot
WHERE run_id = %s
ORDER BY retrieved_at DESC
LIMIT 1
"""

# The live count of current attribution for one tenant. The same statement serves
# the derived after-count and, at a validated historical instant, the corroborating
# read, so the two figures being compared are counts of the same thing.
LIVE_BOUND_COUNT_QUERY: Final[str] = (
    "SELECT count(*) FROM client_binding AS b WHERE b.client_id = %s AND b.superseded_by IS NULL"
)

# The certificate row, written before any object write is attempted so a storage
# failure never loses the signed document.
INSERT_CERTIFICATE_STATEMENT: Final[str] = """
INSERT INTO erasure_certificate (
    run_id, payload, canonical_digest, signature, kms_key_id,
    signing_algorithm, s3_bucket, storage_status, fencing_generation)
VALUES (%s, %s::JSONB, %s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""

# The storage outcome, whichever it was. One statement serves both, so a stored
# certificate and a failed one differ in the values bound rather than in the path
# taken.
UPDATE_STORAGE_STATEMENT: Final[str] = """
UPDATE erasure_certificate
SET storage_status = %s, s3_key = %s, s3_version_id = %s, storage_detail = %s
WHERE id = %s
"""

# The widths each read returns, checked before a row is narrowed so a statement and
# its decoder cannot drift apart in silence.
_RUN_ROW_WIDTH: Final[int] = 17
_BACKUP_ROW_WIDTH: Final[int] = 6
_DISPOSITION_ROW_WIDTH: Final[int] = 9
_RESIDUE_ROW_WIDTH: Final[int] = 9
_SESSION_ROW_WIDTH: Final[int] = 4
_LINEAGE_ROW_WIDTH: Final[int] = 4
_AUDIT_ROW_WIDTH: Final[int] = 3


# ---------------------------------------------------------------------------
# The injected seams
# ---------------------------------------------------------------------------


class CertificateObjectStore(Protocol):
    """The single object write a certificate performs, as a structural protocol.

    Declared and injected rather than constructed, for the same reason the signer
    is: the whole issuing path then runs with no credential and no network call, so
    a test exercises the builder's own logic instead of mocking around it. The
    retention posture and the instant retention runs until are parameters rather
    than the implementation's business, because the certificate surface owns them.
    """

    def put_certificate(
        self,
        body: bytes,
        *,
        bucket: str,
        key: str,
        object_lock_mode: str,
        retain_until: datetime,
    ) -> str:
        """Write the envelope under Object Lock and return the object version."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CertificatePolicy:
    """The signing key, the object location, and the retention a certificate takes.

    Every value is read from the configuration surface rather than written here,
    because a key identifier, a bucket name, and a retention interval are all
    deployment facts and a default compiled into this module would be a deployment
    decision nobody made.
    """

    kms_key_id: str
    signing_algorithm: str
    bucket: str
    prefix: str
    object_lock_days: int

    def __post_init__(self) -> None:
        """Refuse a policy that could not place an object or lock one."""
        if not self.bucket:
            raise ValueError("a certificate is written to a named bucket")
        if self.object_lock_days <= 0:
            raise ValueError("an object lock retention interval covers whole days")

    @property
    def retention(self) -> timedelta:
        """The interval Object Lock retention is applied for."""
        return timedelta(days=self.object_lock_days)

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> CertificatePolicy:
        """Read the whole certificate surface from the configuration."""
        return cls(
            kms_key_id=configuration.text(KEY_ID_KEY),
            signing_algorithm=configuration.text(ALGORITHM_KEY),
            bucket=configuration.text(BUCKET_KEY),
            prefix=configuration.text(PREFIX_KEY),
            object_lock_days=configuration.integer(OBJECT_LOCK_DAYS_KEY),
        )


# ---------------------------------------------------------------------------
# The evidence, as it was read
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestFacts:
    """The request a run answers: who asked, why, and when."""

    request_id: UUID
    requester: str
    justification: str
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class RunFacts:
    """The run row's own statement of what it did and under which thresholds."""

    run_id: UUID
    dry_run: bool
    t_before: datetime
    t_after: datetime | None
    auto_include_threshold: float
    review_threshold: float
    unembedded_artifact_count: int
    working_rows_deleted: int


@dataclass(frozen=True, slots=True)
class OwnershipFacts:
    """The owner that finalised the run, and the fence it held while doing so.

    Carried into the document so the fencing claim is checkable from the
    certificate: a reader compares the stated generation against the current one
    for that tenant and sees that the finalising owner was the legitimate one.
    """

    owner: str | None
    fencing_generation: int | None
    idempotency_key: str | None


@dataclass(frozen=True, slots=True)
class CheckpointFacts:
    """The Ledger_Checkpoint a certificate names, reduced to what it states."""

    checkpoint_id: UUID
    window_start: datetime
    window_end: datetime
    covered_session_count: int
    root_digest: str

    @classmethod
    def of(cls, stored: StoredCheckpoint) -> CheckpointFacts:
        """Narrow a stored checkpoint to the five values the payload carries."""
        return cls(
            checkpoint_id=stored.checkpoint_id,
            window_start=stored.window.start,
            window_end=stored.window.end,
            covered_session_count=stored.covered_session_count,
            root_digest=stored.root_digest,
        )


@dataclass(frozen=True, slots=True)
class BackupFacts:
    """The pre-erasure backup record, path value and both flags included."""

    backup_id: str | None
    backup_path: str | None
    target_uri: str | None
    statement: str
    taken: bool
    referenced: bool


@dataclass(frozen=True, slots=True)
class DispositionRecord:
    """One touched Artifact's disposition, with the attribution it was held under.

    The two attribution fields answer the question a reviewer asks first: not only
    that the Artifact was removed but that it had been held since a stated moment,
    concluded a stated way. They are read before any disposition runs, because a
    hard delete removes the rows they are read from.
    """

    artifact_id: UUID
    artifact_kind: str
    disposition: str
    reason: str
    selection_reason: str
    pre_digest: str | None
    post_digest: str | None
    bindings_before: tuple[str, ...]
    bindings_after: tuple[str, ...]
    first_attributed_at: datetime | None = None
    first_attribution_method: str | None = None

    def with_attribution(self, found: FirstAttribution | None) -> DispositionRecord:
        """Return this record carrying the first attribution of its Artifact."""
        if found is None:
            return self
        return DispositionRecord(
            artifact_id=self.artifact_id,
            artifact_kind=self.artifact_kind,
            disposition=self.disposition,
            reason=self.reason,
            selection_reason=self.selection_reason,
            pre_digest=self.pre_digest,
            post_digest=self.post_digest,
            bindings_before=self.bindings_before,
            bindings_after=self.bindings_after,
            first_attributed_at=found.first_attributed_at,
            first_attribution_method=found.first_method.value,
        )


@dataclass(frozen=True, slots=True)
class LineageEdgeRecord:
    """One edge of the lineage subgraph covering the touched Artifacts."""

    child_id: UUID
    parent_id: UUID
    parent_kind: str
    derivation_method: str


@dataclass(frozen=True, slots=True)
class ResidueRecord:
    """One residue candidate, its distance, and the adjudication evidence."""

    artifact_id: UUID
    artifact_kind: str
    cosine_distance: float
    band: str
    included: bool
    decision_reason: str
    adjudicated: bool
    model_id: str | None
    reasoning: str | None


@dataclass(frozen=True, slots=True)
class SessionTip:
    """The terminal chain state of one Session the run touched."""

    session_id: UUID
    terminal_chain_digest: str | None
    terminal_seq: int | None
    row_count: int | None


@dataclass(frozen=True, slots=True)
class AuditWindow:
    """The cluster audit records covering the run window, as retrieved."""

    window_start: datetime
    window_end: datetime
    records: tuple[CanonicalValue, ...]


@dataclass(frozen=True, slots=True)
class BoundCounts:
    """The before-count and after-count of current attribution for one tenant."""

    before: int
    after: int


@dataclass(frozen=True, slots=True)
class CorroborationFacts:
    """What the opportunistic historical read was allowed to do, and found.

    Agreement is None where nothing was attempted, which is a note rather than a
    finding: the derived figures stand on their own and the corroboration is a
    convenience the horizon may simply have closed on.
    """

    attempted: bool
    within_horizon: bool
    agrees: bool | None
    gc_horizon_seconds: int | None


@dataclass(frozen=True, slots=True)
class CountFacts:
    """The counts block: the derived figures, the tally, and the corroboration."""

    counts: BoundCounts
    hard_delete: int
    surgical_redaction: int
    retained: int
    corroboration: CorroborationFacts


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything a certificate is assembled from, as it was read from the cluster.

    Held as one value so the payload builder is a pure function of stored evidence
    and can be driven directly by a test without a cluster, which is what makes the
    schema-completeness property a statement about this code rather than about a
    fixture.
    """

    request: RequestFacts
    client_id: UUID
    client_slug: str
    run: RunFacts
    ownership: OwnershipFacts
    checkpoint: CheckpointFacts | None
    backup: BackupFacts | None
    counts: CountFacts
    dispositions: tuple[DispositionRecord, ...]
    lineage: tuple[LineageEdgeRecord, ...]
    residue: tuple[ResidueRecord, ...]
    sessions: tuple[SessionTip, ...]
    audit: AuditWindow


# ---------------------------------------------------------------------------
# The signed and stored results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignedCertificate:
    """A canonicalised payload, its digest, and the signature over that digest."""

    evidence: Evidence
    payload: Mapping[str, CanonicalValue]
    payload_bytes: bytes
    payload_digest: str
    signature: bytes
    kms_key_id: str
    signing_algorithm: str

    @property
    def envelope(self) -> Mapping[str, CanonicalValue]:
        """The envelope wrapping the payload without being part of its bytes."""
        return envelope_of(self)


@dataclass(frozen=True, slots=True)
class IssuedCertificate:
    """What issuing produced: the row, the object location, and the outcome.

    A failed object write is a state on this value rather than an exception,
    because the signed document is in the cluster either way and a caller that
    treated storage as fatal would discard evidence it already holds.
    """

    certificate_id: UUID
    signed: SignedCertificate
    bucket: str
    object_key: str
    version_id: str | None
    storage_status: str
    storage_detail: str | None

    @property
    def stored(self) -> bool:
        """Whether the object write completed."""
        return self.storage_status == STORAGE_STORED


# ---------------------------------------------------------------------------
# Reading the evidence
# ---------------------------------------------------------------------------


def first_attribution_snapshot(
    store: MemoryStore,
    client_id: UUID,
    artifact_ids: Sequence[UUID],
) -> Mapping[UUID, FirstAttribution]:
    """Read the earliest attribution of each Artifact, keyed by Artifact.

    A run calls this before its disposition phase, because a hard delete removes
    the attribution versions it reads from and the certificate states when the
    tenant's content began being held. The snapshot is then handed to assembly,
    which is the only way that field can be truthful for a deleted Artifact.
    """
    return {
        found.artifact_id: found
        for found in first_attributions(store, client_id, list(artifact_ids))
    }


def assemble(
    store: MemoryStore,
    run_id: UUID,
    *,
    attributions: Mapping[UUID, FirstAttribution] | None = None,
    now: datetime | None = None,
) -> Evidence:
    """Read every stored row a certificate is assembled from, and nothing else.

    The evidence reads share one connection and one statement sequence, so the
    document describes one view of the evidence rather than a sequence of views
    taken as rows continued to arrive. Nothing here reads the working tier, and
    nothing here reads the memory content itself: a digest, a disposition, and a
    binding slug are what the payload carries.

    Args:
        store: The connection surface the reads are performed on.
        run_id: The completed run the certificate attests to.
        attributions: The first-attribution snapshot taken before the disposition
            phase. Where it is absent the attribution rows are read now, which is
            correct for a redaction and empty for a hard delete, so a caller that
            wants the field populated takes the snapshot at the right moment.
        now: The reading the horizon arithmetic is measured against, injectable so
            the corroboration branch can be driven rather than waited out.

    Returns:
        The whole evidence set, ready to be turned into a payload.

    Raises:
        StoreError: The run names no row, or a column holds a type the schema does
            not declare.
    """

    def body(
        cursor: Cursor,
    ) -> tuple[
        tuple[object, ...],
        list[tuple[object, ...]],
        list[tuple[object, ...]],
        list[tuple[object, ...]],
        tuple[object, ...] | None,
        tuple[object, ...] | None,
        int,
    ]:
        cursor.execute(RUN_QUERY, (run_id,))
        run_row = cursor.fetchone()
        if run_row is None:
            raise StoreError("the named erasure run holds no stored row to attest to")
        cursor.execute(DISPOSITIONS_QUERY, (run_id,))
        disposition_rows = cursor.fetchall()
        cursor.execute(RESIDUE_QUERY, (run_id,))
        residue_rows = cursor.fetchall()
        cursor.execute(RUN_SESSIONS_QUERY, (run_id,))
        session_rows = cursor.fetchall()
        cursor.execute(BACKUP_QUERY, (run_id,))
        backup_row = cursor.fetchone()
        cursor.execute(AUDIT_SNAPSHOT_QUERY, (run_id,))
        audit_row = cursor.fetchone()
        cursor.execute(LIVE_BOUND_COUNT_QUERY, (_as_uuid(run_row[1], "client_id"),))
        return (
            run_row,
            disposition_rows,
            residue_rows,
            session_rows,
            backup_row,
            audit_row,
            _as_count(_one(cursor.fetchone()), "count"),
        )

    (
        run_row,
        disposition_rows,
        residue_rows,
        session_rows,
        backup_row,
        audit_row,
        live_bound,
    ) = store.read(body)

    request, client_id, client_slug, run, ownership = _run_facts_of(run_row)
    dispositions = tuple(_disposition_of(row) for row in disposition_rows)
    snapshot = (
        first_attribution_snapshot(store, client_id, [entry.artifact_id for entry in dispositions])
        if attributions is None
        else attributions
    )
    dispositions = tuple(
        entry.with_attribution(snapshot.get(entry.artifact_id)) for entry in dispositions
    )
    touched = [entry.artifact_id for entry in dispositions]

    def lineage_body(cursor: Cursor) -> list[tuple[object, ...]]:
        cursor.execute(LINEAGE_SUBGRAPH_QUERY, (touched, touched))
        return cursor.fetchall()

    lineage = () if not touched else tuple(_edge_of(row) for row in store.read(lineage_body))
    counts = derived_counts(dispositions, slug=client_slug, live_bound=live_bound)
    checkpoint = latest_before(store, run.t_before)

    return Evidence(
        request=request,
        client_id=client_id,
        client_slug=client_slug,
        run=run,
        ownership=ownership,
        checkpoint=None if checkpoint is None else CheckpointFacts.of(checkpoint),
        backup=None if backup_row is None else _backup_of(backup_row),
        counts=CountFacts(
            counts=counts,
            hard_delete=_tally(dispositions, HARD_DELETE),
            surgical_redaction=_tally(dispositions, SURGICAL_REDACTION),
            retained=_tally(dispositions, RETAINED),
            corroboration=corroborate(store, run, counts, client_id, now=now),
        ),
        dispositions=dispositions,
        lineage=lineage,
        residue=tuple(_residue_of(row) for row in residue_rows),
        sessions=tuple(_session_of(row) for row in session_rows),
        audit=_audit_of(audit_row, run),
    )


def derived_counts(
    dispositions: Sequence[DispositionRecord],
    *,
    slug: str,
    live_bound: int,
) -> BoundCounts:
    """Derive the before-count and after-count from append-only evidence alone.

    The before-count is the number of Dispositions whose recorded prior bindings
    named the erased tenant, plus whatever is still bound; the after-count is what
    is still bound. Both figures come from rows that outlive the collection
    horizon, which is why this mechanism is the primary one on every certificate
    and the point-in-time read is only ever a corroboration of it.
    """
    withdrawn = sum(1 for entry in dispositions if slug in entry.bindings_before)
    return BoundCounts(before=withdrawn + live_bound, after=live_bound)


def corroborate(
    store: MemoryStore,
    run: RunFacts,
    derived: BoundCounts,
    client_id: UUID,
    *,
    now: datetime | None = None,
) -> CorroborationFacts:
    """Attempt the historical corroboration, but only inside the measured horizon.

    The horizon is read from the capability record rather than assumed, and both of
    the run's instants are compared against it before any statement is sent, so an
    instant the cluster can no longer answer for is never asked about. Where the
    horizon has closed, or was never measured, or the run has no closing instant
    yet, nothing is attempted and that is recorded as a note. Where the read runs,
    agreement or disagreement with the derived figures is recorded and the derived
    figures stand either way.
    """
    horizon = _measured_horizon(store)
    seconds = None if horizon is None else horizon.seconds
    if horizon is None or run.t_after is None:
        return CorroborationFacts(
            attempted=False,
            within_horizon=False,
            agrees=None,
            gc_horizon_seconds=seconds,
        )
    reachable = within_gc_horizon(
        store, run.t_before, now=now, horizon=horizon
    ) and within_gc_horizon(store, run.t_after, now=now, horizon=horizon)
    if not reachable:
        return CorroborationFacts(
            attempted=False,
            within_horizon=False,
            agrees=None,
            gc_horizon_seconds=seconds,
        )
    try:
        before = _historical_count(store, client_id, run.t_before, now=now, horizon=horizon)
        after = _historical_count(store, client_id, run.t_after, now=now, horizon=horizon)
    except (HistoricalHorizonError, StoreError) as error:
        log(
            Severity.INFO,
            COMPONENT,
            "the historical corroboration could not be performed, so none is recorded",
            run_id=str(run.run_id),
            error_type=type(error).__name__,
        )
        return CorroborationFacts(
            attempted=False,
            within_horizon=True,
            agrees=None,
            gc_horizon_seconds=seconds,
        )
    return CorroborationFacts(
        attempted=True,
        within_horizon=True,
        agrees=(before, after) == (derived.before, derived.after),
        gc_horizon_seconds=seconds,
    )


def _measured_horizon(store: MemoryStore) -> GcHorizon | None:
    """The recorded collection horizon, or None where none was ever measured."""
    try:
        return gc_horizon(store)
    except StoreError:
        return None


def _historical_count(
    store: MemoryStore,
    client_id: UUID,
    at: datetime,
    *,
    now: datetime | None,
    horizon: GcHorizon,
) -> int:
    """Count current attribution as the cluster held it at one earlier instant."""
    rows = historical(
        store,
        LIVE_BOUND_COUNT_QUERY,
        (client_id,),
        at=at,
        now=now,
        horizon=horizon,
    )
    if len(rows) != 1:
        raise StoreError("the historical count returned no single row to compare")
    return _as_count(_one(rows[0]), "count")


def _tally(dispositions: Sequence[DispositionRecord], disposition: str) -> int:
    """How many touched Artifacts received one disposition."""
    return sum(1 for entry in dispositions if entry.disposition == disposition)


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------


def verification_queries(client_id: UUID) -> list[Mapping[str, CanonicalValue]]:
    """Build the verification-query set from the fixed template list.

    Every member reads evidence tables outside the working tier, the tenant travels
    as a bound parameter rather than as statement text, and the set is drawn from
    the fixed list rather than composed, so a verifier can validate each query
    against that list and refuse anything else. A certificate therefore cannot ask
    a verifier to run a statement of its own devising.
    """
    return [
        {
            "name": template.name,
            "sql": template.sql,
            "params": [str(client_id)],
            "expectation": template.expectation,
        }
        for template in VERIFICATION_TEMPLATES
    ]


def certificate_payload(evidence: Evidence) -> Mapping[str, CanonicalValue]:
    """Build the certificate payload from stored evidence, as a pure function.

    Nothing is read here and nothing is computed from the process's own state, so
    the same evidence yields the same payload in any process, which is the
    precondition for a signature over its canonical bytes meaning anything. Every
    optional value is present as an explicit null rather than omitted, so the key
    set of a certificate does not vary with its content.
    """
    run = evidence.run
    counts = evidence.counts
    payload: dict[str, CanonicalValue] = {
        "certificate_version": CERTIFICATE_VERSION,
        "erasure_request": {
            "request_id": evidence.request.request_id,
            "requester": evidence.request.requester,
            "justification": evidence.request.justification,
            "submitted_at": evidence.request.submitted_at,
        },
        "client": {"client_id": evidence.client_id, "slug": evidence.client_slug},
        "run": {
            "run_id": run.run_id,
            "dry_run": run.dry_run,
            "t_before": run.t_before,
            "t_after": run.t_after,
            "auto_include_threshold": run.auto_include_threshold,
            "review_threshold": run.review_threshold,
            "unembedded_artifact_count": run.unembedded_artifact_count,
            "working_rows_deleted": run.working_rows_deleted,
        },
        "ownership": {
            "owner": evidence.ownership.owner,
            "fencing_generation": evidence.ownership.fencing_generation,
            "idempotency_key": evidence.ownership.idempotency_key,
        },
        "ledger_checkpoint": _checkpoint_block(evidence.checkpoint),
        "backup": _backup_block(evidence.backup),
        "counts": {
            "artifacts_bound_before": counts.counts.before,
            "artifacts_bound_after": counts.counts.after,
            "count_derivation": COUNT_DERIVATION,
            "historical_corroboration": {
                "attempted": counts.corroboration.attempted,
                "within_horizon": counts.corroboration.within_horizon,
                "agrees": counts.corroboration.agrees,
                "gc_horizon_seconds": counts.corroboration.gc_horizon_seconds,
            },
            "hard_delete": counts.hard_delete,
            "surgical_redaction": counts.surgical_redaction,
            "retained": counts.retained,
        },
        "dispositions": [
            {
                "artifact_id": entry.artifact_id,
                "artifact_kind": entry.artifact_kind,
                "disposition": entry.disposition,
                "reason": entry.reason,
                "selection_reason": entry.selection_reason,
                "pre_digest": entry.pre_digest,
                "post_digest": entry.post_digest,
                "bindings_before": list(entry.bindings_before),
                "bindings_after": list(entry.bindings_after),
                "first_attributed_at": entry.first_attributed_at,
                "first_attribution_method": entry.first_attribution_method,
            }
            for entry in evidence.dispositions
        ],
        "lineage_subgraph": [
            {
                "child_id": edge.child_id,
                "parent_id": edge.parent_id,
                "parent_kind": edge.parent_kind,
                "derivation_method": edge.derivation_method,
            }
            for edge in evidence.lineage
        ],
        "residue_candidates": [
            {
                "artifact_id": entry.artifact_id,
                "artifact_kind": entry.artifact_kind,
                "cosine_distance": entry.cosine_distance,
                "band": entry.band,
                "included": entry.included,
                "decision_reason": entry.decision_reason,
                "adjudicated": entry.adjudicated,
                "model_id": entry.model_id,
                "reasoning": entry.reasoning,
            }
            for entry in evidence.residue
        ],
        "sessions": [
            {
                "session_id": entry.session_id,
                "terminal_chain_digest": entry.terminal_chain_digest,
                "terminal_seq": entry.terminal_seq,
                "row_count": entry.row_count,
            }
            for entry in evidence.sessions
        ],
        "verification_queries": verification_queries(evidence.client_id),
        "cluster_audit_log": {
            "window_start": evidence.audit.window_start,
            "window_end": evidence.audit.window_end,
            "records": list(evidence.audit.records),
        },
        "caveats": dict(CAVEATS),
    }
    return payload


def _checkpoint_block(facts: CheckpointFacts | None) -> CanonicalValue:
    """The named checkpoint, or an explicit null where the run precedes the first."""
    if facts is None:
        return None
    return {
        "checkpoint_id": facts.checkpoint_id,
        "window_start": facts.window_start,
        "window_end": facts.window_end,
        "covered_session_count": facts.covered_session_count,
        "root_digest": facts.root_digest,
    }


def _backup_block(facts: BackupFacts | None) -> Mapping[str, CanonicalValue]:
    """The backup block, whose key set is the same whether a backup exists or not."""
    if facts is None:
        return {
            "present": False,
            "backup_path": None,
            "taken": False,
            "referenced": False,
            "backup_id": None,
            "target_uri": None,
            "statement": None,
        }
    return {
        "present": True,
        "backup_path": facts.backup_path,
        "taken": facts.taken,
        "referenced": facts.referenced,
        "backup_id": facts.backup_id,
        "target_uri": facts.target_uri,
        "statement": facts.statement,
    }


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign(
    evidence: Evidence,
    *,
    signer: DigestSigner,
    policy: CertificatePolicy,
) -> SignedCertificate:
    """Canonicalise the payload, digest it, and sign the digest.

    The payload is not sent to the signing service: the digest is, which matters
    because the payload names artifacts and a signing request is a record held
    elsewhere. A signing service that cannot answer aborts certificate creation
    rather than producing an unsigned document, because an unsigned certificate
    would look like evidence while committing to nothing.
    """
    payload = certificate_payload(evidence)
    payload_bytes = canonicalise(payload, array_rules=CERTIFICATE_ARRAY_RULES)
    digest = hashlib.sha256(payload_bytes).hexdigest()
    try:
        signature = signer.sign_digest(
            bytes.fromhex(digest),
            key_id=policy.kms_key_id,
            algorithm=policy.signing_algorithm,
        )
    except MoltError:
        raise
    except Exception as error:
        raise SigningUnavailableError(
            "the signing key could not be used, so no certificate was produced"
        ) from error
    if not signature:
        raise SigningUnavailableError(
            "the signing key returned no signature, so no certificate was produced"
        )
    return SignedCertificate(
        evidence=evidence,
        payload=payload,
        payload_bytes=payload_bytes,
        payload_digest=digest,
        signature=signature,
        kms_key_id=policy.kms_key_id,
        signing_algorithm=policy.signing_algorithm,
    )


def envelope_of(signed: SignedCertificate) -> Mapping[str, CanonicalValue]:
    """Wrap a signed payload in the envelope, which the signature does not cover.

    The signature block sits beside the payload rather than inside it, because
    bytes cannot commit to a signature over themselves. A verifier re-canonicalises
    the payload it finds here and compares the digest, so the wrapping is a
    container and never a source of signed content.
    """
    return {
        "payload": signed.payload,
        "signature": {
            "algorithm": signed.signing_algorithm,
            "kms_key_id": signed.kms_key_id,
            "payload_digest": signed.payload_digest,
            "value": base64.b64encode(signed.signature).decode("ascii"),
        },
    }


def object_key_for(policy: CertificatePolicy, client_slug: str, run_id: UUID) -> str:
    """The object name a certificate is written under, inside the tenant's prefix.

    One object per run under a per-tenant prefix, so a tenant's certificates are
    listable as a set and one run's certificate is addressable without a search.
    """
    prefix = (
        policy.prefix
        if policy.prefix.endswith(_KEY_SEPARATOR)
        else (policy.prefix + _KEY_SEPARATOR)
    )
    return f"{prefix}{client_slug}{_KEY_SEPARATOR}{run_id}{_KEY_SUFFIX}"


# ---------------------------------------------------------------------------
# Persistence and storage
# ---------------------------------------------------------------------------


def persist(store: MemoryStore, signed: SignedCertificate, *, bucket: str) -> UUID:
    """Write the signed certificate to the cluster before any object write runs.

    The write goes through the fence, so an owner whose lease was superseded
    mid-run cannot sign for that run, and the generation the document states is one
    that was current when the row was written. The storage columns are left in the
    pending state: they record an outcome that has not happened yet.
    """
    evidence = signed.evidence
    generation = evidence.ownership.fencing_generation

    def body(cursor: Cursor) -> UUID:
        cursor.execute(
            INSERT_CERTIFICATE_STATEMENT,
            (
                evidence.run.run_id,
                signed.payload_bytes.decode("utf-8"),
                signed.payload_digest,
                signed.signature,
                signed.kms_key_id,
                signed.signing_algorithm,
                bucket,
                STORAGE_PENDING,
                generation,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise StoreError("the certificate insert returned no identifier")
        return _as_uuid(_one(row), "id")

    return _fenced(store, evidence.client_id, generation, body, label=_PERSIST_LABEL)


def publish(
    store: MemoryStore,
    signed: SignedCertificate,
    certificate_id: UUID,
    *,
    object_store: CertificateObjectStore,
    policy: CertificatePolicy,
    now: datetime,
) -> IssuedCertificate:
    """Write the envelope under Object Lock and record the outcome on the row.

    A failed write is recorded and reported rather than raised: the signed document
    is already in the cluster, so treating the storage fault as fatal would discard
    evidence that exists. A completed write records the object name and the version
    identifier beside the payload digest, which is what lets a verifier fetch the
    same bytes the cluster attests to.
    """
    evidence = signed.evidence
    key = object_key_for(policy, evidence.client_slug, evidence.run.run_id)
    body_bytes = canonicalise(envelope_of(signed), array_rules=CERTIFICATE_ARRAY_RULES)
    version: str | None = None
    status = STORAGE_STORED
    detail: str | None = None
    try:
        version = object_store.put_certificate(
            body_bytes,
            bucket=policy.bucket,
            key=key,
            object_lock_mode=OBJECT_LOCK_MODE,
            retain_until=require_aware(now, "a retention reading") + policy.retention,
        )
    except Exception as error:
        status = STORAGE_FAILED
        detail = type(error).__name__
        version = None
    _record_storage(
        store, evidence, certificate_id, key=key, version=version, status=status, detail=detail
    )
    issued = IssuedCertificate(
        certificate_id=certificate_id,
        signed=signed,
        bucket=policy.bucket,
        object_key=key,
        version_id=version,
        storage_status=status,
        storage_detail=detail,
    )
    _report(issued)
    return issued


def _record_storage(
    store: MemoryStore,
    evidence: Evidence,
    certificate_id: UUID,
    *,
    key: str,
    version: str | None,
    status: str,
    detail: str | None,
) -> None:
    """Record the storage outcome, whichever it was, through the same statement."""

    def body(cursor: Cursor) -> None:
        cursor.execute(UPDATE_STORAGE_STATEMENT, (status, key, version, detail, certificate_id))

    _fenced(
        store,
        evidence.client_id,
        evidence.ownership.fencing_generation,
        body,
        label=_STORAGE_LABEL,
    )


def _report(issued: IssuedCertificate) -> None:
    """Emit the issue and, where the object write failed, the failure beside it."""
    metric(ISSUED_METRIC, 1.0)
    if issued.stored:
        log(
            Severity.INFO,
            COMPONENT,
            "a signed erasure certificate was issued and stored",
            certificate_id=str(issued.certificate_id),
            run_id=str(issued.signed.evidence.run.run_id),
        )
        return
    metric(STORAGE_FAILURE_METRIC, 1.0)
    log(
        Severity.WARNING,
        COMPONENT,
        "a signed erasure certificate was retained in the cluster after a storage failure",
        certificate_id=str(issued.certificate_id),
        run_id=str(issued.signed.evidence.run.run_id),
        storage_status=issued.storage_status,
        error_type=issued.storage_detail,
    )


def issue(
    store: MemoryStore,
    run_id: UUID,
    *,
    signer: DigestSigner,
    object_store: CertificateObjectStore,
    policy: CertificatePolicy,
    attributions: Mapping[UUID, FirstAttribution] | None = None,
    now: datetime | None = None,
) -> IssuedCertificate:
    """Assemble, sign, persist, and store one certificate, in that order.

    The order is the guarantee: nothing is signed that was not read from stored
    rows, nothing is stored that was not first persisted, and a fault at the last
    step leaves a signed document behind rather than nothing at all.
    """
    reading = require_aware(now, "a certificate reading") if now is not None else _reading()
    evidence = assemble(store, run_id, attributions=attributions, now=reading)
    signed = sign(evidence, signer=signer, policy=policy)
    certificate_id = persist(store, signed, bucket=policy.bucket)
    return publish(
        store,
        signed,
        certificate_id,
        object_store=object_store,
        policy=policy,
        now=reading,
    )


# ---------------------------------------------------------------------------
# Transaction framing
# ---------------------------------------------------------------------------


def _fenced(
    store: MemoryStore,
    client_id: UUID,
    generation: int | None,
    body: Callable[[Cursor], _Result],
    *,
    label: str,
) -> _Result:
    """Frame a certificate write, behind the fence wherever a generation is stated.

    A run that recorded the generation it held is written behind the fencing
    predicate, so a superseded owner records nothing. A run predating the lease
    columns states no generation and therefore has no fence to present; its write is
    still one serializable transaction, and the certificate carries a null
    generation rather than a number nobody can check.
    """
    if generation is None:
        return store.in_serializable(body, label=label)
    return fenced_certificate(store, client_id, generation, body)


def _reading() -> datetime:
    """The current reading the horizon arithmetic and retention are measured from."""
    return datetime.now(_UTC)


# ---------------------------------------------------------------------------
# Row narrowing
# ---------------------------------------------------------------------------


def _run_facts_of(
    row: Sequence[object],
) -> tuple[RequestFacts, UUID, str, RunFacts, OwnershipFacts]:
    """Narrow the one row the run read returns into the four facts it carries."""
    if len(row) != _RUN_ROW_WIDTH:
        raise StoreError(
            f"the run read returned {len(row)} column(s) where {_RUN_ROW_WIDTH} are read"
        )
    run = RunFacts(
        run_id=_as_uuid(row[0], "run id"),
        dry_run=_as_flag(row[2], "dry run"),
        t_before=_as_moment(row[3], "t_before"),
        t_after=None if row[4] is None else _as_moment(row[4], "t_after"),
        auto_include_threshold=_as_real(row[5], "auto inclusion threshold"),
        review_threshold=_as_real(row[6], "review threshold"),
        unembedded_artifact_count=_as_count(row[7], "unembedded count"),
        working_rows_deleted=_as_count(row[8], "working rows deleted"),
    )
    request = RequestFacts(
        request_id=_as_uuid(row[11], "request id"),
        requester=_as_text(row[12], "requester"),
        justification=_as_text(row[13], "justification"),
        submitted_at=_as_moment(row[14], "submitted_at"),
    )
    ownership = OwnershipFacts(
        owner=None if row[16] is None else _as_text(row[16], "owner"),
        fencing_generation=None if row[9] is None else _as_count(row[9], "fencing generation"),
        idempotency_key=None if row[10] is None else _as_text(row[10], "idempotency key"),
    )
    return request, _as_uuid(row[1], "client id"), _as_text(row[15], "slug"), run, ownership


def _backup_of(row: Sequence[object]) -> BackupFacts:
    """Narrow the backup row, keeping the path value and both flags distinct."""
    if len(row) != _BACKUP_ROW_WIDTH:
        raise StoreError(
            f"the backup read returned {len(row)} column(s) where {_BACKUP_ROW_WIDTH} are read"
        )
    return BackupFacts(
        backup_id=None if row[0] is None else _as_text(row[0], "backup id"),
        backup_path=None if row[1] is None else _as_text(row[1], "backup path"),
        target_uri=None if row[2] is None else _as_text(row[2], "target uri"),
        statement=_as_text(row[3], "command"),
        taken=_as_flag(row[4], "taken"),
        referenced=_as_flag(row[5], "referenced"),
    )


def _disposition_of(row: Sequence[object]) -> DispositionRecord:
    """Narrow one disposition row, both digests and both binding arrays included."""
    if len(row) != _DISPOSITION_ROW_WIDTH:
        raise StoreError(
            f"a disposition row returned {len(row)} column(s) "
            f"where {_DISPOSITION_ROW_WIDTH} are read"
        )
    return DispositionRecord(
        artifact_id=_as_uuid(row[0], "artifact id"),
        artifact_kind=_as_text(row[1], "artifact kind"),
        disposition=_as_text(row[2], "disposition"),
        reason=_as_text(row[3], "reason"),
        selection_reason=_as_text(row[4], "selection reason"),
        pre_digest=None if row[5] is None else _as_text(row[5], "pre digest"),
        post_digest=None if row[6] is None else _as_text(row[6], "post digest"),
        bindings_before=_as_slugs(row[7], "bindings before"),
        bindings_after=_as_slugs(row[8], "bindings after"),
    )


def _residue_of(row: Sequence[object]) -> ResidueRecord:
    """Narrow one residue candidate row, adjudication evidence included."""
    if len(row) != _RESIDUE_ROW_WIDTH:
        raise StoreError(
            f"a residue row returned {len(row)} column(s) where {_RESIDUE_ROW_WIDTH} are read"
        )
    return ResidueRecord(
        artifact_id=_as_uuid(row[0], "artifact id"),
        artifact_kind=_as_text(row[1], "artifact kind"),
        cosine_distance=_as_real(row[2], "cosine distance"),
        band=_as_text(row[3], "band"),
        included=_as_flag(row[4], "inclusion decision"),
        decision_reason=_as_text(row[5], "decision reason"),
        adjudicated=_as_flag(row[6], "adjudicated"),
        model_id=None if row[7] is None else _as_text(row[7], "model id"),
        reasoning=None if row[8] is None else _as_text(row[8], "reasoning"),
    )


def _session_of(row: Sequence[object]) -> SessionTip:
    """Narrow one touched Session's recorded chain tip."""
    if len(row) != _SESSION_ROW_WIDTH:
        raise StoreError(
            f"a session row returned {len(row)} column(s) where {_SESSION_ROW_WIDTH} are read"
        )
    return SessionTip(
        session_id=_as_uuid(row[0], "session id"),
        terminal_chain_digest=None if row[1] is None else _as_text(row[1], "chain digest"),
        terminal_seq=None if row[2] is None else _as_count(row[2], "terminal sequence"),
        row_count=None if row[3] is None else _as_count(row[3], "row count"),
    )


def _edge_of(row: Sequence[object]) -> LineageEdgeRecord:
    """Narrow one lineage edge of the subgraph."""
    if len(row) != _LINEAGE_ROW_WIDTH:
        raise StoreError(
            f"a lineage row returned {len(row)} column(s) where {_LINEAGE_ROW_WIDTH} are read"
        )
    return LineageEdgeRecord(
        child_id=_as_uuid(row[0], "child id"),
        parent_id=_as_uuid(row[1], "parent id"),
        parent_kind=_as_text(row[2], "parent kind"),
        derivation_method=_as_text(row[3], "derivation method"),
    )


def _audit_of(row: Sequence[object] | None, run: RunFacts) -> AuditWindow:
    """Narrow the audit snapshot, falling back to the run's own window when none.

    An absent snapshot is an empty record set over the run's window rather than a
    missing key, because the certificate's key set does not vary with what the
    control plane happened to return.
    """
    if row is None:
        return AuditWindow(
            window_start=run.t_before,
            window_end=run.t_after if run.t_after is not None else run.t_before,
            records=(),
        )
    if len(row) != _AUDIT_ROW_WIDTH:
        raise StoreError(
            f"the audit read returned {len(row)} column(s) where {_AUDIT_ROW_WIDTH} are read"
        )
    return AuditWindow(
        window_start=_as_moment(row[0], "audit window start"),
        window_end=_as_moment(row[1], "audit window end"),
        records=_as_records(row[2]),
    )


def _one(row: Sequence[object] | None) -> object:
    """The single column of a single-column row, refusing every other shape."""
    if row is None or len(row) != 1:
        raise StoreError("a single-column read returned no single column")
    return row[0]


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a column whose type is not the one the schema declares.

    The type is named and the value is not, for the reason every decoder in this
    codebase names one: a message reaches a log record and stored content is memory
    content.
    """
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object, expected: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, expected)


def _as_text(value: object, expected: str) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, expected)


def _as_flag(value: object, expected: str) -> bool:
    if isinstance(value, bool):
        return value
    raise _unexpected(value, expected)


def _as_count(value: object, expected: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unexpected(value, expected)
    return value


def _as_real(value: object, expected: str) -> float:
    if isinstance(value, bool):
        raise _unexpected(value, expected)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    raise _unexpected(value, expected)


def _as_moment(value: object, expected: str) -> datetime:
    if isinstance(value, datetime):
        return require_aware(value, expected)
    raise _unexpected(value, expected)


def _as_slugs(value: object, expected: str) -> tuple[str, ...]:
    """Narrow one of the two binding arrays, refusing an element that is not a slug."""
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _unexpected(value, expected)
    return tuple(_as_text(element, expected) for element in value)


def _as_records(value: object) -> tuple[CanonicalValue, ...]:
    """Read the audit records column, whether the driver decoded it or not."""
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if decoded is None:
        return ()
    if not isinstance(decoded, list):
        raise _unexpected(decoded, "an audit record list")
    return tuple(cast("CanonicalValue", element) for element in decoded)
