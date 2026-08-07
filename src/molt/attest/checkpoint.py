"""Signed Ledger checkpoints: the window, the root digest, and what disagreement means.

The per-Session hash chain detects the tampering an editor of one row leaves
behind. It does not detect a consistent rewrite, because a chain recomputed after
a change verifies against itself by construction. A checkpoint closes that gap by
committing to the terminal chain digest of every Session holding at least one
Event inside a bounded window, and by having that commitment signed with a key
the cluster holds no access to. The evidence therefore survives a principal
holding administrator privilege on the cluster, which is coverage no in-cluster
mechanism can give itself.

Five shapes here are load-bearing rather than incidental.

**The window bounds what a checkpoint commits to, and the terminal digest is the
terminal digest inside that window.** A Session's chain grows after the window
closes, so committing to the Session's present tip would make every checkpoint
disagree with itself as soon as the next Event arrived. The tip of the Session's
rows *within* the window is a value that only a change to the past can move,
which is exactly the change a checkpoint exists to detect.

**The root digest is taken over the canonical bytes of the covered set, not over
a concatenation assembled here.** The canonical serialiser is the only place the
ordering, separator, and scalar rules live, and it orders the covered records by
Session identifier, so an independent verifier reproduces the bytes without
knowing anything about this module. A second concatenation written here would be
a second rule that has to agree with the first, and the two would drift.

**Signing is a digest-signing call on an injected signer.** The signer is a
parameter rather than a constructed client, so the signing key and the signing
path are the Certificate_Builder's own and a test drives both halves with no
credential. Verification retrieves the public half and checks the signature
against it rather than asking the key service to check, so verification survives
the loss of permission to call that service.

**Disagreement is partitioned before it is reported.** A Session whose terminal
digest moved because a recorded Erasure_Run deleted rows is explained; a Session
whose digest moved with no Disposition accounting for it is the finding. Both are
carried, so a caller reports the distinction rather than collapsing an authorised
erasure into an alarm. The refusal is raised only where a change is unaccounted
for, because raising on a governed erasure would flag the very thing the
governance record exists to explain.

**Every statement is a whole module-level literal with bound parameters.** No
identifier and no domain value is interpolated, here or anywhere the checkpoint
reads and writes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol
from uuid import UUID

from molt.attest.canonical import CERTIFICATE_ARRAY_RULES, CanonicalValue, canonicalise
from molt.config.resolve import Configuration
from molt.errors import (
    CheckpointDisagreement,
    StoreError,
    VerificationFailedError,
)
from molt.models.event import require_aware
from molt.store import Cursor, MemoryStore
from molt.telemetry import Severity, log, metric

__all__ = [
    "ACCOUNTED",
    "ACCOUNTING_RUNS_QUERY",
    "ALGORITHM_KEY",
    "CHECKPOINT_BY_ID_QUERY",
    "COMPONENT",
    "COMPUTED_METRIC",
    "DIGEST_FIELD",
    "DISAGREEMENT_METRIC",
    "EXPLAINED_DIMENSION",
    "INSERT_CHECKPOINT_SESSION_STATEMENT",
    "INSERT_CHECKPOINT_STATEMENT",
    "INTERVAL_KEY",
    "KEY_ID_KEY",
    "LATEST_BEFORE_QUERY",
    "RECORDED_SESSIONS_QUERY",
    "SESSIONS_KEY",
    "SESSION_ID_FIELD",
    "UNACCOUNTED",
    "WINDOW_TIPS_QUERY",
    "CheckpointPolicy",
    "CheckpointVerification",
    "CheckpointWindow",
    "ComputedCheckpoint",
    "DigestSigner",
    "SessionChange",
    "SessionDigest",
    "StoredCheckpoint",
    "accounting_runs",
    "compute",
    "latest_before",
    "load",
    "recorded_sessions",
    "require_agreement",
    "root_digest",
    "sign_and_store",
    "take_checkpoint",
    "verify",
    "window_ending_at",
]

# What this module is called in a log record.
COMPONENT: Final[str] = "checkpoint"

# The configuration keys this module reads. The interval is the checkpoint
# surface's own; the key identifier and the algorithm are the certificate
# surface's, deliberately, because one signing key and one signing path exist.
INTERVAL_KEY: Final[str] = "MOLT_CHECKPOINT_INTERVAL_SECONDS"
KEY_ID_KEY: Final[str] = "MOLT_KMS_KEY_ID"
ALGORITHM_KEY: Final[str] = "MOLT_KMS_SIGNING_ALGORITHM"

# The two measurements this module emits, and the dimension that carries the
# partition a disagreement was reported under.
COMPUTED_METRIC: Final[str] = "checkpoint.computed"
DISAGREEMENT_METRIC: Final[str] = "checkpoint.verification_disagreements"
EXPLAINED_DIMENSION: Final[str] = "explained"
ACCOUNTED: Final[str] = "accounted"
UNACCOUNTED: Final[str] = "unaccounted"

# The payload keys the root digest's canonical bytes carry. The collection key is
# the one the canonical serialiser already orders by Session identifier, so the
# ordering rule is declared in one place rather than restated here.
SESSIONS_KEY: Final[str] = "sessions"
SESSION_ID_FIELD: Final[str] = "session_id"
DIGEST_FIELD: Final[str] = "terminal_chain_digest"

# The width a hexadecimal SHA-256 digest is held at, which the schema also checks.
DIGEST_LENGTH: Final[int] = 64

# The terminal row of every Session inside the window: one row per Session,
# highest sequence first within the Session, so the leading row of each group is
# that Session's tip as the window closed. The window is half-open, closed at the
# start and open at the end, so two consecutive windows partition an instant
# between them rather than both claiming it.
WINDOW_TIPS_QUERY: Final[str] = """
SELECT DISTINCT ON (session_id) session_id, seq, chain_digest
FROM ledger
WHERE occurred_at >= %s AND occurred_at < %s
ORDER BY session_id, seq DESC
"""

# The checkpoint row. The identifier is returned rather than generated here, so
# the value the per-Session rows reference is the one the cluster committed.
INSERT_CHECKPOINT_STATEMENT: Final[str] = """
INSERT INTO ledger_checkpoint (
    window_start, window_end, covered_session_count,
    root_digest, signature, kms_key_id, signing_algorithm)
VALUES (%s, %s, %s, %s, %s, %s, %s)
RETURNING id, created_at
"""

# One row per covered Session, carrying the digest as it stood at checkpoint time.
INSERT_CHECKPOINT_SESSION_STATEMENT: Final[str] = """
INSERT INTO checkpoint_session (checkpoint_id, session_id, terminal_chain_digest, terminal_seq)
VALUES (%s, %s, %s, %s)
"""

# The certificate's lookup: the most recent checkpoint whose window ended before
# the instant the run began reading. Served by the descending window-end index as
# a bounded seek rather than a scan that lengthens with checkpoint history.
#
# The column list is written out in both statements rather than assembled from a
# shared fragment, because a statement composed by interpolation is no longer a
# whole literal, and the two readers agree by selecting the same nine columns in
# the same order rather than by sharing a variable.
LATEST_BEFORE_QUERY: Final[str] = """
SELECT id, window_start, window_end, covered_session_count,
       root_digest, signature, kms_key_id, signing_algorithm, created_at
FROM ledger_checkpoint
WHERE window_end <= %s
ORDER BY window_end DESC
LIMIT 1
"""

CHECKPOINT_BY_ID_QUERY: Final[str] = """
SELECT id, window_start, window_end, covered_session_count,
       root_digest, signature, kms_key_id, signing_algorithm, created_at
FROM ledger_checkpoint
WHERE id = %s
"""

# The digests recorded at checkpoint time, ordered so a reader sees one order
# whatever the storage layout happens to return. The columns are selected in the
# order the live window read selects them, which is what lets one narrowing serve
# both and keeps the recorded shape and the live shape from drifting apart.
RECORDED_SESSIONS_QUERY: Final[str] = """
SELECT session_id, terminal_seq, terminal_chain_digest
FROM checkpoint_session
WHERE checkpoint_id = %s
ORDER BY session_id ASC
"""

# Which Erasure_Runs account for a change to one Session, in the two ways a
# Disposition can name a row of that Session.
#
# The first arm is the redaction case: the row survives with rewritten content,
# so it is still joinable and the Disposition names it directly. The second arm is
# the deletion case: the row is gone, so nothing joins to it, and the run's own
# per-Session record is what ties the run to the Session while the row's absence
# is what says the recorded deletion happened. Together they are the difference
# between a governed erasure and an edit nobody recorded.
ACCOUNTING_RUNS_QUERY: Final[str] = """
SELECT DISTINCT d.run_id
FROM disposition AS d
WHERE d.disposition IN (%s, %s)
  AND d.artifact_id IN (SELECT l.id FROM ledger AS l WHERE l.session_id = %s)
UNION
SELECT DISTINCT d.run_id
FROM disposition AS d
JOIN run_session AS rs ON rs.run_id = d.run_id
WHERE rs.session_id = %s
  AND d.disposition = %s
  AND NOT EXISTS (SELECT 1 FROM ledger AS l WHERE l.id = d.artifact_id)
"""

# The two dispositions that move a Session's terminal digest, in the spellings the
# schema's own check constraint admits. They are bound as parameters rather than
# written into the statement text, so no domain value appears in a statement.
_HARD_DELETE: Final[str] = "hard_delete"
_SURGICAL_REDACTION: Final[str] = "surgical_redaction"

# What the transaction that writes a checkpoint appears under in a log record.
_STORE_LABEL: Final[str] = "checkpoint_store"


# ---------------------------------------------------------------------------
# The signing surface
# ---------------------------------------------------------------------------


class DigestSigner(Protocol):
    """The asymmetric signing calls a checkpoint and a certificate both make.

    Declared structurally and injected rather than constructed, for two reasons.
    A deployment holds one signing key and one signing path, so both documents
    reach the key service through the same shape. And a test drives signing and
    verification with no credential and no network call, which is what lets the
    checkpoint's own logic be exercised rather than mocked around.
    """

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        """Sign an already-computed digest with the named asymmetric key."""

    def public_key(self, *, key_id: str) -> bytes:
        """Retrieve the public half of the named key, for an offline check."""

    def verify_digest(
        self,
        digest: bytes,
        signature: bytes,
        *,
        key_id: str,
        algorithm: str,
        public_key: bytes,
    ) -> bool:
        """Check a signature against a retrieved public key rather than against the service.

        The public half is passed in rather than fetched here, so the retrieval is
        visible at the call site: verification is a local computation over a key
        the verifier holds, which is what makes it survive the loss of permission
        to call the signing service.
        """


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckpointPolicy:
    """The interval a checkpoint covers, and the key it is signed with.

    The interval is both the window width and the scheduled period, so a
    consecutive series of checkpoints partitions the Ledger's history with no gap
    and no overlap.
    """

    interval_seconds: int
    kms_key_id: str
    signing_algorithm: str

    def __post_init__(self) -> None:
        """Refuse a non-positive interval, which would describe no window at all."""
        if self.interval_seconds <= 0:
            raise ValueError("a checkpoint window covers a positive number of seconds")

    @property
    def interval(self) -> timedelta:
        """The window width as an interval."""
        return timedelta(seconds=self.interval_seconds)

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> CheckpointPolicy:
        """Read the interval and the signing key from the configuration surface."""
        return cls(
            interval_seconds=configuration.integer(INTERVAL_KEY),
            kms_key_id=configuration.text(KEY_ID_KEY),
            signing_algorithm=configuration.text(ALGORITHM_KEY),
        )


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckpointWindow:
    """The half-open interval one checkpoint commits to."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """Refuse an unordered window and an offsetless bound, as the schema does."""
        require_aware(self.start, "a checkpoint window start")
        require_aware(self.end, "a checkpoint window end")
        if self.end <= self.start:
            raise ValueError("a checkpoint window ending at or before its start covers no interval")


@dataclass(frozen=True, slots=True)
class SessionDigest:
    """One covered Session's terminal chain state, as of one instant."""

    session_id: UUID
    terminal_chain_digest: str
    terminal_seq: int


@dataclass(frozen=True, slots=True)
class ComputedCheckpoint:
    """What a computation derived, before anything was signed or stored."""

    window: CheckpointWindow
    sessions: tuple[SessionDigest, ...]
    root_digest: str

    @property
    def covered_session_count(self) -> int:
        """How many Sessions held at least one Event inside the window."""
        return len(self.sessions)


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    """A checkpoint as it stands in the Ledger, signature and key included."""

    checkpoint_id: UUID
    window: CheckpointWindow
    covered_session_count: int
    root_digest: str
    signature: bytes
    kms_key_id: str
    signing_algorithm: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionChange:
    """One covered Session whose terminal digest no longer matches the record.

    Attributes:
        session_id: The Session the checkpoint committed to.
        recorded_digest: The terminal digest recorded at checkpoint time.
        live_digest: The terminal digest the live rows produce now, or None when
            the Session holds no row inside the window any more.
        accounting_runs: Every Erasure_Run whose Dispositions account for the
            difference. Empty is the finding rather than an absence of detail.
    """

    session_id: UUID
    recorded_digest: str
    live_digest: str | None
    accounting_runs: tuple[UUID, ...]

    @property
    def accounted(self) -> bool:
        """Whether a recorded authorised erasure explains this change."""
        return bool(self.accounting_runs)


@dataclass(frozen=True, slots=True)
class CheckpointVerification:
    """What a verification found, with the disagreement already partitioned."""

    checkpoint_id: UUID
    window: CheckpointWindow
    recorded_root_digest: str
    recomputed_root_digest: str
    signature_verified: bool
    changes: tuple[SessionChange, ...]

    @property
    def agrees(self) -> bool:
        """Whether the recomputation and the signature both stand."""
        return (
            self.signature_verified
            and self.recomputed_root_digest == self.recorded_root_digest
            and not self.changes
        )

    @property
    def accounted_changes(self) -> tuple[SessionChange, ...]:
        """The changed Sessions a recorded erasure explains."""
        return tuple(change for change in self.changes if change.accounted)

    @property
    def unaccounted_changes(self) -> tuple[SessionChange, ...]:
        """The changed Sessions nothing on the record explains."""
        return tuple(change for change in self.changes if not change.accounted)

    @property
    def changed_sessions(self) -> tuple[UUID, ...]:
        """Every covered Session whose terminal digest moved."""
        return tuple(change.session_id for change in self.changes)

    @property
    def accounting_runs(self) -> tuple[UUID, ...]:
        """Every Erasure_Run accounting for some part of the disagreement."""
        runs: set[UUID] = set()
        for change in self.changes:
            runs.update(change.accounting_runs)
        return tuple(sorted(runs, key=str))


# ---------------------------------------------------------------------------
# The root digest
# ---------------------------------------------------------------------------


def root_digest(sessions: Sequence[SessionDigest]) -> str:
    """Return the SHA-256 digest committing to a covered set of Sessions.

    The digest is taken over the canonical bytes of the covered records, which the
    one canonical serialiser orders by Session identifier and renders with fixed
    separators between fields and between records. Ordering and separation
    therefore come from the same place a certificate's signed bytes come from, so
    an independent verifier reproduces this value from the stored rows alone and
    the order the rows were read in cannot reach the digest.
    """
    payload: Mapping[str, CanonicalValue] = {
        SESSIONS_KEY: [
            {
                SESSION_ID_FIELD: entry.session_id,
                DIGEST_FIELD: entry.terminal_chain_digest,
            }
            for entry in sessions
        ]
    }
    return hashlib.sha256(canonicalise(payload, array_rules=CERTIFICATE_ARRAY_RULES)).hexdigest()


def window_ending_at(end: datetime, policy: CheckpointPolicy) -> CheckpointWindow:
    """The window one scheduled invocation covers, ending at the given instant."""
    closing = require_aware(end, "a checkpoint window end")
    return CheckpointWindow(start=closing - policy.interval, end=closing)


# ---------------------------------------------------------------------------
# Computation and signed storage
# ---------------------------------------------------------------------------


def compute(store: MemoryStore, window: CheckpointWindow) -> ComputedCheckpoint:
    """Gather the terminal chain state of every Session holding an Event in the window.

    The terminal state is the Session's tip *inside* the window rather than its
    present tip, so a checkpoint stays a statement about a closed interval and
    later appends move nothing it committed to.
    """

    def body(cursor: Cursor) -> list[tuple[object, ...]]:
        cursor.execute(WINDOW_TIPS_QUERY, (window.start, window.end))
        return cursor.fetchall()

    sessions = tuple(_session_digest_of(row) for row in store.read(body))
    return ComputedCheckpoint(
        window=window,
        sessions=sessions,
        root_digest=root_digest(sessions),
    )


def sign_and_store(
    store: MemoryStore,
    computed: ComputedCheckpoint,
    *,
    signer: DigestSigner,
    policy: CheckpointPolicy,
) -> StoredCheckpoint:
    """Sign a computed root digest and store the checkpoint with its covered set.

    The signature is produced before the transaction opens, because a signing call
    leaves the process and a transaction held across it would hold a conflict
    window open for the length of a network round trip. The checkpoint row and its
    per-Session rows are then one transaction, so a checkpoint never exists
    without the digests that localise a later disagreement.
    """
    signature = signer.sign_digest(
        bytes.fromhex(computed.root_digest),
        key_id=policy.kms_key_id,
        algorithm=policy.signing_algorithm,
    )

    def body(cursor: Cursor) -> tuple[UUID, datetime]:
        cursor.execute(
            INSERT_CHECKPOINT_STATEMENT,
            (
                computed.window.start,
                computed.window.end,
                computed.covered_session_count,
                computed.root_digest,
                signature,
                policy.kms_key_id,
                policy.signing_algorithm,
            ),
        )
        row = cursor.fetchone()
        if row is None or len(row) != 2:
            raise StoreError(
                "the checkpoint insert returned no identifier, so nothing references it"
            )
        identifier = _as_uuid(row[0], "id")
        for entry in computed.sessions:
            cursor.execute(
                INSERT_CHECKPOINT_SESSION_STATEMENT,
                (
                    identifier,
                    entry.session_id,
                    entry.terminal_chain_digest,
                    entry.terminal_seq,
                ),
            )
        return identifier, _as_moment(row[1], "created_at")

    checkpoint_id, created_at = store.in_serializable(body, label=_STORE_LABEL)
    stored = StoredCheckpoint(
        checkpoint_id=checkpoint_id,
        window=computed.window,
        covered_session_count=computed.covered_session_count,
        root_digest=computed.root_digest,
        signature=signature,
        kms_key_id=policy.kms_key_id,
        signing_algorithm=policy.signing_algorithm,
        created_at=created_at,
    )
    metric(COMPUTED_METRIC, 1.0)
    log(
        Severity.INFO,
        COMPONENT,
        "a signed ledger checkpoint was computed and stored",
        checkpoint_id=str(stored.checkpoint_id),
        covered_session_count=stored.covered_session_count,
    )
    return stored


def take_checkpoint(
    store: MemoryStore,
    *,
    signer: DigestSigner,
    policy: CheckpointPolicy,
    now: datetime,
) -> StoredCheckpoint:
    """Compute, sign, and store the checkpoint one scheduled invocation covers.

    This is the entry point the scheduled console invocation calls, which is the
    only principal holding the signing permission. The closing instant is passed
    in rather than read here, so the window a run covered is a value the caller
    can record and a test can state.
    """
    window = window_ending_at(now, policy)
    return sign_and_store(store, compute(store, window), signer=signer, policy=policy)


# ---------------------------------------------------------------------------
# Reading a stored checkpoint
# ---------------------------------------------------------------------------


def latest_before(store: MemoryStore, t_before: datetime) -> StoredCheckpoint | None:
    """The most recent checkpoint whose window ended at or before an instant.

    This is the read a certificate cannot be assembled without: the certificate
    names the checkpoint covering the history it reports over, and absence is a
    return value because a deployment's first erasure may precede its first
    checkpoint.
    """
    moment = require_aware(t_before, "a certificate reading instant")

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(LATEST_BEFORE_QUERY, (moment,))
        return cursor.fetchone()

    row = store.read(body)
    return None if row is None else _stored_checkpoint_of(row)


def load(store: MemoryStore, checkpoint_id: UUID) -> StoredCheckpoint:
    """Read one named checkpoint, refusing an identifier that names none."""

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(CHECKPOINT_BY_ID_QUERY, (checkpoint_id,))
        return cursor.fetchone()

    row = store.read(body)
    if row is None:
        raise VerificationFailedError("the named ledger checkpoint holds no stored row")
    return _stored_checkpoint_of(row)


def recorded_sessions(store: MemoryStore, checkpoint_id: UUID) -> tuple[SessionDigest, ...]:
    """The per-Session digests a checkpoint recorded at the time it was taken."""

    def body(cursor: Cursor) -> list[tuple[object, ...]]:
        cursor.execute(RECORDED_SESSIONS_QUERY, (checkpoint_id,))
        return cursor.fetchall()

    return tuple(_session_digest_of(row) for row in store.read(body))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def accounting_runs(store: MemoryStore, session_id: UUID) -> tuple[UUID, ...]:
    """Every Erasure_Run whose Dispositions account for a change to one Session.

    Both arms of the statement are needed, because a Disposition reaches a
    Session's Events in two ways: a redacted row survives and is named directly,
    while a deleted row is gone and is tied to the Session by the run's own
    per-Session record together with the row's absence.
    """

    def body(cursor: Cursor) -> list[tuple[object, ...]]:
        cursor.execute(
            ACCOUNTING_RUNS_QUERY,
            (
                _HARD_DELETE,
                _SURGICAL_REDACTION,
                session_id,
                session_id,
                _HARD_DELETE,
            ),
        )
        return cursor.fetchall()

    runs = {_as_uuid(row[0], "run_id") for row in store.read(body)}
    return tuple(sorted(runs, key=str))


def verify(
    store: MemoryStore,
    checkpoint_id: UUID,
    *,
    signer: DigestSigner,
) -> CheckpointVerification:
    """Recompute a checkpoint from live rows, check its signature, and report.

    Three findings are distinguished rather than collapsed. The signature is
    checked against the public half retrieved from the key service, so a stored
    digest that was rewritten alongside its rows is still caught. The root digest
    is recomputed from the live Ledger, so any movement inside the window
    surfaces. And each Session whose recorded digest moved is partitioned by
    whether a recorded Erasure_Run's Dispositions account for it, so a governed
    erasure is explained rather than flagged.
    """
    stored = load(store, checkpoint_id)
    recorded = recorded_sessions(store, checkpoint_id)
    live = compute(store, stored.window)
    live_by_session = {entry.session_id: entry for entry in live.sessions}

    signature_verified = signer.verify_digest(
        bytes.fromhex(stored.root_digest),
        stored.signature,
        key_id=stored.kms_key_id,
        algorithm=stored.signing_algorithm,
        public_key=signer.public_key(key_id=stored.kms_key_id),
    )

    changes: list[SessionChange] = []
    for entry in recorded:
        present = live_by_session.get(entry.session_id)
        if present is not None and present.terminal_chain_digest == entry.terminal_chain_digest:
            continue
        changes.append(
            SessionChange(
                session_id=entry.session_id,
                recorded_digest=entry.terminal_chain_digest,
                live_digest=None if present is None else present.terminal_chain_digest,
                accounting_runs=accounting_runs(store, entry.session_id),
            )
        )

    report = CheckpointVerification(
        checkpoint_id=stored.checkpoint_id,
        window=stored.window,
        recorded_root_digest=stored.root_digest,
        recomputed_root_digest=live.root_digest,
        signature_verified=signature_verified,
        changes=tuple(changes),
    )
    _report(report)
    return report


def require_agreement(report: CheckpointVerification) -> None:
    """Raise where a verification found something nothing on the record explains.

    A disagreement every changed Session's Erasure_Run accounts for is reported
    and returns, because raising there would flag the authorised erasure the
    governance record exists to explain. A signature that does not verify is a
    different fault from a moved digest and is named as itself.
    """
    if not report.signature_verified:
        raise VerificationFailedError(
            "the stored checkpoint signature does not verify against the retrieved public key"
        )
    if report.unaccounted_changes:
        raise CheckpointDisagreement(report.changed_sessions, report.accounting_runs)


def _report(report: CheckpointVerification) -> None:
    """Emit the disagreement counts under the dimension that explains them."""
    if report.agrees:
        return
    accounted = len(report.accounted_changes)
    unaccounted = len(report.unaccounted_changes)
    if accounted:
        metric(DISAGREEMENT_METRIC, float(accounted), **{EXPLAINED_DIMENSION: ACCOUNTED})
    if unaccounted:
        metric(DISAGREEMENT_METRIC, float(unaccounted), **{EXPLAINED_DIMENSION: UNACCOUNTED})
    log(
        Severity.WARNING,
        COMPONENT,
        "a ledger checkpoint verification disagreed with the live ledger",
        checkpoint_id=str(report.checkpoint_id),
        signature_verified=report.signature_verified,
        accounted_sessions=accounted,
        unaccounted_sessions=unaccounted,
    )


# ---------------------------------------------------------------------------
# Row narrowing
# ---------------------------------------------------------------------------


def _session_digest_of(row: Sequence[object]) -> SessionDigest:
    """Narrow one covered-Session row, whichever of the two statements produced it.

    The live window read and the recorded read select the identifier, the sequence,
    and the digest in that one order, so a single narrowing serves both and the two
    shapes cannot drift apart.
    """
    if len(row) != 3:
        raise StoreError(f"a covered session row returned {len(row)} column(s) where 3 are read")
    return SessionDigest(
        session_id=_as_uuid(row[0], "session_id"),
        terminal_chain_digest=_as_digest(row[2], "chain_digest"),
        terminal_seq=_as_int(row[1], "seq"),
    )


def _stored_checkpoint_of(row: Sequence[object]) -> StoredCheckpoint:
    """Narrow one stored checkpoint row into the shape a verifier reads."""
    if len(row) != 9:
        raise StoreError(f"a checkpoint row returned {len(row)} column(s) where 9 are read")
    return StoredCheckpoint(
        checkpoint_id=_as_uuid(row[0], "id"),
        window=CheckpointWindow(
            start=_as_moment(row[1], "window_start"),
            end=_as_moment(row[2], "window_end"),
        ),
        covered_session_count=_as_int(row[3], "covered_session_count"),
        root_digest=_as_digest(row[4], "root_digest"),
        signature=_as_bytes(row[5], "signature"),
        kms_key_id=_as_text(row[6], "kms_key_id"),
        signing_algorithm=_as_text(row[7], "signing_algorithm"),
        created_at=_as_moment(row[8], "created_at"),
    )


def _as_int(value: object, column: str) -> int:
    """Read a whole number out of a column, refusing anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError(f"the checkpoint column {column} did not return a whole number")
    return value


def _as_text(value: object, column: str) -> str:
    """Read text out of a column, refusing anything else."""
    if not isinstance(value, str):
        raise StoreError(f"the checkpoint column {column} did not return text")
    return value


def _as_digest(value: object, column: str) -> str:
    """Read a digest out of a column, refusing text of any other length."""
    text = _as_text(value, column)
    if len(text) != DIGEST_LENGTH:
        raise StoreError(
            f"the checkpoint column {column} returned {len(text)} character(s) where a "
            f"{DIGEST_LENGTH} character digest is stored"
        )
    return text


def _as_bytes(value: object, column: str) -> bytes:
    """Read a byte string out of a column, accepting either driver representation."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise StoreError(f"the checkpoint column {column} did not return bytes")


def _as_uuid(value: object, column: str) -> UUID:
    """Read an identifier out of a column, accepting either representation."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise StoreError(
                f"the checkpoint column {column} did not return a hyphenated identifier"
            ) from exc
    raise StoreError(f"the checkpoint column {column} did not return an identifier")


def _as_moment(value: object, column: str) -> datetime:
    """Read a timezone-aware instant out of a column, refusing a naive one."""
    if not isinstance(value, datetime):
        raise StoreError(f"the checkpoint column {column} did not return an instant")
    return require_aware(value, f"the checkpoint column {column}")
