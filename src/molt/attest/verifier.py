"""The independent Certificate_Verifier: cryptography plus live queries, nothing else.

A departing client's reviewer has no reason to accept the consultancy's word, so
this module is written to be run by somebody who holds none of the consultancy's
privileges. Six shapes carry that intent.

**The signature is checked locally against a retrieved public half.** The key
service is asked for the public key and for nothing else, so a verification
performed after the permission to call that service's verify operation is gone
still stands, and a verification performed against a saved public key needs no
service at all. The digest is recomputed with the one canonical serialiser rather
than with a second implementation, so signer and verifier cannot drift apart.

**The certificate's own SQL is validated, never executed.** Each embedded query is
matched by name against a fixed template set declared here, and what is sent to
the cluster is this module's own statement literal for that name with the
certificate's parameters bound. A hostile certificate therefore cannot ask the
verifier to run a statement of its choosing, and no value from the certificate
ever reaches statement text. The expectation each query is judged against and the
set of queries a certificate owes are both properties of that template set rather
than fields of the document, so an issuer can neither weaken the completeness
check nor leave it out.

**The reader role is a structural guarantee rather than a discipline.** The role
is checked before any query runs, both as the label the configuration resolved and
as the role the cluster reports the connection is authenticated as. A verifier
that cannot show it is read-only refuses to verify rather than proceeding
carefully.

**The derived mechanism is primary and the historical read is corroboration.** The
before-count and the after-count are re-derived from append-only rows, so a
certificate stays verifiable long after the cluster's garbage-collection horizon
has passed over its timestamps. The historical read is attempted only when both
timestamps lie inside the horizon read from the capability record, and an
unattempted corroboration is recorded as a note rather than as a failed check.

**Every finding is a value, not a message.** The report carries the machine-
readable outcome, the failed checks with their subjects and identifiers, the row
count of every query, the count comparison and the mechanism it used, the first
mismatching sequence number of any chain, and the checkpoint accounting. The exit
status is derived from the outcome, so the command surface maps one to the other
rather than deciding it again.

**Both retrieval paths are injected seams.** The certificate arrives from a local
path or from an object key read through a passed-in reader, and the public key
arrives through a passed-in source. Neither is constructed here, so a test drives
the whole algorithm with no credential and no network call.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import load_der_public_key

from molt.attest.canonical import CERTIFICATE_ARRAY_RULES, CanonicalValue, canonicalise
from molt.attest.checkpoint import CheckpointVerification, latest_before
from molt.attest.checkpoint import verify as verify_checkpoint
from molt.config.resolve import Configuration
from molt.errors import StoreError, VerificationFailedError
from molt.store import READER_ROLE_NAMES as STORE_READER_ROLE_NAMES
from molt.store import Cursor, MemoryStore
from molt.store.chain import verify_chain
from molt.telemetry import Severity, log, metric

__all__ = [
    "ALGORITHM_KEY",
    "ARTIFACT_STILL_PRESENT",
    "BUCKET_KEY",
    "CHAIN_MISMATCH",
    "CHAIN_TIP_MISMATCH",
    "CHECKPOINT_ABSENT",
    "CHECKPOINT_SIGNATURE_INVALID",
    "CHECKPOINT_UNEXPLAINED_CHANGE",
    "COMPONENT",
    "COUNT_DISAGREEMENT",
    "CURRENT_DIGESTS_QUERY",
    "CURRENT_ROLE_QUERY",
    "DERIVED_MECHANISM",
    "DISPOSED_BEFORE_QUERY",
    "ERASURE_INCOMPLETE",
    "EXISTING_ARTIFACTS_QUERY",
    "EXIT_FAILED",
    "EXIT_VERIFIED",
    "KEY_ID_KEY",
    "LIVE_COUNT_QUERY",
    "OUTCOME_FAILED",
    "OUTCOME_VERIFIED",
    "PREFIX_KEY",
    "QUERY_TEMPLATES",
    "QUERY_TEMPLATE_UNKNOWN",
    "READER_MEMBERSHIP_QUERY",
    "READER_ROLE_NAMES",
    "REDACTION_DIGEST_MISMATCH",
    "SIGNATURE_INVALID",
    "SUPPORTED_ALGORITHM",
    "VERIFICATION_QUERY_MISSING",
    "CertificateLocation",
    "CertificateSettings",
    "ChainOutcome",
    "CheckpointOutcome",
    "Corroboration",
    "CountComparison",
    "Envelope",
    "FailedCheck",
    "LocalSignatureChecker",
    "Note",
    "ObjectSource",
    "PublicKeySource",
    "QueryOutcome",
    "QueryTemplate",
    "VerificationReport",
    "exit_code",
    "load_envelope",
    "parse_envelope",
    "require_reader_role",
    "verify_certificate",
    "verify_signature",
    "verify_source",
]

# What this module is called in a log record.
COMPONENT: Final[str] = "verifier"

# The configuration surface keys this module reads. Nothing about the key or the
# evidence bucket is written into this file.
KEY_ID_KEY: Final[str] = "MOLT_KMS_KEY_ID"
ALGORITHM_KEY: Final[str] = "MOLT_KMS_SIGNING_ALGORITHM"
BUCKET_KEY: Final[str] = "MOLT_CERT_BUCKET"
PREFIX_KEY: Final[str] = "MOLT_CERT_PREFIX"

# The measurement this module emits, and the dimension carrying the outcome.
VERIFICATION_METRIC: Final[str] = "certificate.verifications"
OUTCOME_DIMENSION: Final[str] = "outcome"

# The two machine-readable outcomes and the exit statuses they map to.
OUTCOME_VERIFIED: Final[str] = "verified"
OUTCOME_FAILED: Final[str] = "failed"
EXIT_VERIFIED: Final[int] = 0
EXIT_FAILED: Final[int] = 1

# The failed-check names. Each is a value a caller branches on rather than a
# sentence a caller parses.
SIGNATURE_INVALID: Final[str] = "signature_invalid"
ERASURE_INCOMPLETE: Final[str] = "erasure_incomplete"
QUERY_TEMPLATE_UNKNOWN: Final[str] = "query_template_unknown"
VERIFICATION_QUERY_MISSING: Final[str] = "verification_query_missing"
COUNT_DISAGREEMENT: Final[str] = "count_disagreement"
CHAIN_MISMATCH: Final[str] = "chain_mismatch"
CHAIN_TIP_MISMATCH: Final[str] = "chain_tip_mismatch"
CHECKPOINT_SIGNATURE_INVALID: Final[str] = "checkpoint_signature_invalid"
CHECKPOINT_ABSENT: Final[str] = "checkpoint_absent"
CHECKPOINT_UNEXPLAINED_CHANGE: Final[str] = "checkpoint_unexplained_change"
REDACTION_DIGEST_MISMATCH: Final[str] = "redaction_digest_mismatch"
ARTIFACT_STILL_PRESENT: Final[str] = "artifact_still_present"

# The note names. A note records something that was observed or deliberately not
# attempted, and no note makes a verification fail.
HISTORICAL_CORROBORATION_NOTE: Final[str] = "historical_corroboration"
CHECKPOINT_EXPLAINED_NOTE: Final[str] = "checkpoint_disagreement_explained"
CHECKPOINT_SUCCESSION_NOTE: Final[str] = "checkpoint_is_not_the_latest_before_run"
DERIVATION_NOTE: Final[str] = "recorded_count_derivation"
SESSION_DELETED_NOTE: Final[str] = "session_deleted_by_this_run"
SESSION_TIP_ABSENT_NOTE: Final[str] = "session_tip_not_recorded"

# The subjects a failed check names, where the subject is a position in the
# document rather than an identifier of a row.
PAYLOAD_DIGEST_SUBJECT: Final[str] = "payload_digest"
SIGNATURE_VALUE_SUBJECT: Final[str] = "signature_value"
BEFORE_SUBJECT: Final[str] = "artifacts_bound_before"
AFTER_SUBJECT: Final[str] = "artifacts_bound_after"

# The count mechanism this module uses as its primary path, and the reasons a
# corroborating historical read was not attempted.
DERIVED_MECHANISM: Final[str] = "ledger_and_dispositions"
OUTSIDE_HORIZON_REASON: Final[str] = "outside_gc_horizon"
UNPROBED_HORIZON_REASON: Final[str] = "gc_horizon_unprobed"
HORIZON_REFUSED_REASON: Final[str] = "historical_read_refused"
INCOMPLETE_RUN_REASON: Final[str] = "run_records_no_after_instant"

# The one signing algorithm the delivered key uses. An envelope naming anything
# else is reported as an invalid signature rather than verified by guesswork.
SUPPORTED_ALGORITHM: Final[str] = "ECDSA_SHA_256"

# The role names that name the read-only role. The label the configuration resolves
# and the role the cluster reports are both matched against this set, so a deployment
# naming the role in its qualified form and a test naming it in its short form are both
# admitted and nothing else is.
#
# Re-exported from the store layer rather than restated, because a database role is
# the store's fact and two components that disagreed about its name would disagree
# about whether the same connection is read-only.
READER_ROLE_NAMES: Final[frozenset[str]] = STORE_READER_ROLE_NAMES

# The dispositions whose consistency is checked, in the spellings the schema's own
# check constraint admits. They are compared in this process rather than bound
# into a statement, because the batching is done over identifier arrays.
_HARD_DELETE: Final[str] = "hard_delete"
_SURGICAL_REDACTION: Final[str] = "surgical_redaction"

# ---------------------------------------------------------------------------
# Statements. Every one is a whole module-level literal binding its values.
# ---------------------------------------------------------------------------

# The role the connection is authenticated as, asked of the cluster rather than
# assumed from the configuration.
CURRENT_ROLE_QUERY: Final[str] = "SELECT current_user"

# Whether the connected login carries the read-only role, asked of the cluster as a
# privilege question rather than settled by comparing names.
#
# A deployment does not log in as a role. It provisions a login per component and grants
# that login the role its privileges were written for, so the connected user is a member of
# the read-only role and is not named the same as it. Comparing `current_user` against the
# role's own spellings therefore refused every connection a deployment can actually make:
# certificate verification could not succeed against the deployed cluster at all, which is
# the one claim in this system that is meant to be checkable by somebody who does not trust
# the party that signed. It succeeded locally, where the suites connect as an administrator
# and the label was set by hand.
#
# Membership is the right question in any case. What the guarantee rests on is the
# privileges the connection holds, and those come from the role however the login is spelled;
# a name is a claim and a membership is a privilege. This is the same predicate the schema's
# own column guards are written with, so the two cannot come to disagree about what holding a
# role means.
READER_MEMBERSHIP_QUERY: Final[str] = "SELECT pg_has_role(current_user, %s, %s)"

# What membership is asked for. `MEMBER` is satisfied by holding the role whether or not the
# session has assumed it, which is what a login granted the role at provisioning time has.
_MEMBERSHIP: Final[str] = "MEMBER"

# The role a verifying connection must carry, in the spelling the migrations create it under.
_READER_ROLE: Final[str] = "molt_reader"

# The executable form of the two Verification_Query templates. The certificate's
# own text is validated against the documented form below; what is sent is this.
CURRENT_BINDINGS_QUERY: Final[str] = """
SELECT b.artifact_id FROM client_binding AS b
WHERE b.client_id = %s AND b.superseded_by IS NULL
"""

LIVE_SESSIONS_QUERY: Final[str] = """
SELECT s.id FROM session AS s WHERE s.client_id = %s
"""

# Current attribution for one Client, which is both the after-count and the live
# term of the derived before-count.
LIVE_COUNT_QUERY: Final[str] = """
SELECT count(*) FROM client_binding AS b
WHERE b.client_id = %s AND b.superseded_by IS NULL
"""

# The append-only term of the derived before-count: the Dispositions of this run
# whose pre-decision bindings named the erased Client. This outlives the
# garbage-collection horizon, which is why it is the primary mechanism.
DISPOSED_BEFORE_QUERY: Final[str] = """
SELECT count(*) FROM disposition AS d
WHERE d.run_id = %s AND %s::STRING = ANY (d.bindings_before)
"""

# The first of the two set-based disposition checks: which of the identifiers a
# certificate reports as hard-deleted are still present, in one statement over a
# bound array rather than one statement per artifact.
EXISTING_ARTIFACTS_QUERY: Final[str] = """
SELECT a.id FROM derived_artifact AS a WHERE a.id = ANY (%s::UUID[])
UNION
SELECT l.id FROM ledger AS l WHERE l.id = ANY (%s::UUID[])
"""

# The second: the present content digest of every identifier a certificate reports
# as surgically redacted, again in one statement over a bound array.
CURRENT_DIGESTS_QUERY: Final[str] = """
SELECT a.id, a.content_digest FROM derived_artifact AS a WHERE a.id = ANY (%s::UUID[])
"""


# ---------------------------------------------------------------------------
# The fixed template set
# ---------------------------------------------------------------------------


# The expectation every template of the fixed set declares, held here rather than
# read from the document, because a certificate that chose its own expectation would
# choose whether the completeness check was made at all.
EXPECTATION_EMPTY: Final[str] = "empty"


@dataclass(frozen=True, slots=True)
class QueryTemplate:
    """One admitted Verification_Query: its documented text and what is run for it.

    The documented text is the form the certificate carries, written with the
    positional placeholders of the document rather than of the driver. The
    statement is this module's own literal. Keeping them apart is the whole point:
    the certificate's text is evidence to be checked, not code to be run.

    Attributes:
        expectation: What the result set must be for this template to be satisfied.
            It is the template's fixed property rather than the document's field, so
            an issuer cannot weaken the one check a hostile reviewer relies on by
            declaring a laxer expectation beside the query.
        obligatory: Whether a certificate must carry this template. An obligatory
            template that no entry presents is a check that was never made, which is
            reported by name rather than passed over in silence.
    """

    name: str
    documented_sql: str
    statement: str
    parameter_count: int
    expectation: str = EXPECTATION_EMPTY
    obligatory: bool = True


QUERY_TEMPLATES: Final[Mapping[str, QueryTemplate]] = {
    "no_current_attribution_remains": QueryTemplate(
        name="no_current_attribution_remains",
        documented_sql=(
            "SELECT b.artifact_id FROM client_binding AS b "
            "WHERE b.client_id = $1 AND b.superseded_by IS NULL"
        ),
        statement=CURRENT_BINDINGS_QUERY,
        parameter_count=1,
    ),
    "no_sessions_remain": QueryTemplate(
        name="no_sessions_remain",
        documented_sql="SELECT s.id FROM session AS s WHERE s.client_id = $1",
        statement=LIVE_SESSIONS_QUERY,
        parameter_count=1,
    ),
}

# The templates a certificate owes. Derived from the registry rather than restated,
# so the set of obligatory checks and the set of admitted templates cannot drift.
OBLIGATORY_TEMPLATE_NAMES: Final[tuple[str, ...]] = tuple(
    name for name, template in QUERY_TEMPLATES.items() if template.obligatory
)


# ---------------------------------------------------------------------------
# The payload contract
# ---------------------------------------------------------------------------

PAYLOAD_KEY: Final[str] = "payload"
SIGNATURE_KEY: Final[str] = "signature"
ALGORITHM_FIELD: Final[str] = "algorithm"
KEY_ID_FIELD: Final[str] = "kms_key_id"
PAYLOAD_DIGEST_FIELD: Final[str] = "payload_digest"
SIGNATURE_VALUE_FIELD: Final[str] = "value"

CLIENT_KEY: Final[str] = "client"
CLIENT_ID_FIELD: Final[str] = "client_id"
SLUG_FIELD: Final[str] = "slug"

RUN_KEY: Final[str] = "run"
RUN_ID_FIELD: Final[str] = "run_id"
T_BEFORE_FIELD: Final[str] = "t_before"
T_AFTER_FIELD: Final[str] = "t_after"

COUNTS_KEY: Final[str] = "counts"
BEFORE_FIELD: Final[str] = "artifacts_bound_before"
AFTER_FIELD: Final[str] = "artifacts_bound_after"
DERIVATION_FIELD: Final[str] = "count_derivation"

DISPOSITIONS_KEY: Final[str] = "dispositions"
ARTIFACT_ID_FIELD: Final[str] = "artifact_id"
DISPOSITION_FIELD: Final[str] = "disposition"
POST_DIGEST_FIELD: Final[str] = "post_digest"

SESSIONS_KEY: Final[str] = "sessions"
SESSION_ID_FIELD: Final[str] = "session_id"
TERMINAL_DIGEST_FIELD: Final[str] = "terminal_chain_digest"

QUERIES_KEY: Final[str] = "verification_queries"
NAME_FIELD: Final[str] = "name"
SQL_FIELD: Final[str] = "sql"
PARAMS_FIELD: Final[str] = "params"
EXPECTATION_FIELD: Final[str] = "expectation"

CHECKPOINT_KEY: Final[str] = "ledger_checkpoint"
CHECKPOINT_ID_FIELD: Final[str] = "checkpoint_id"


# ---------------------------------------------------------------------------
# The injected seams
# ---------------------------------------------------------------------------


class PublicKeySource(Protocol):
    """Retrieval of the public half of a named asymmetric key.

    This is the only thing the verifier asks the key service for. The check itself
    is a local computation, so a reviewer holding a saved public key verifies with
    no service call at all.
    """

    def public_key(self, *, key_id: str) -> bytes:
        """The public half of the named key, in the standard encoded form."""


class ObjectSource(Protocol):
    """Retrieval of one stored certificate object by bucket and key."""

    def read_object(self, *, bucket: str, key: str) -> bytes:
        """The bytes of the named object."""


@dataclass(frozen=True, slots=True)
class CertificateSettings:
    """Where certificates are stored and which key signs them.

    Read from the configuration surface rather than written here, so a deployment
    verifying against its own bucket and its own key changes configuration rather
    than code.
    """

    bucket: str
    prefix: str
    kms_key_id: str
    signing_algorithm: str

    @classmethod
    def from_configuration(cls, configuration: Configuration) -> CertificateSettings:
        """Read the evidence bucket, its prefix, and the signing key from configuration."""
        return cls(
            bucket=configuration.text(BUCKET_KEY),
            prefix=configuration.text(PREFIX_KEY),
            kms_key_id=configuration.text(KEY_ID_KEY),
            signing_algorithm=configuration.text(ALGORITHM_KEY),
        )


@dataclass(frozen=True, slots=True)
class CertificateLocation:
    """Where one certificate is being read from: a local path or an object key.

    Exactly one is held, so a caller states which retrieval it means rather than
    leaving the verifier to infer it from whether a file happens to exist.
    """

    path: Path | None = None
    object_key: str | None = None

    def __post_init__(self) -> None:
        """Refuse a location naming neither retrieval or both of them."""
        if (self.path is None) == (self.object_key is None):
            raise ValueError("a certificate location names either a local path or an object key")

    @classmethod
    def at_path(cls, path: Path | str) -> CertificateLocation:
        """A certificate held in the local filesystem."""
        return cls(path=Path(path))

    @classmethod
    def at_object_key(cls, key: str) -> CertificateLocation:
        """A certificate held in the evidence object store."""
        return cls(object_key=key)


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Envelope:
    """A signed certificate: the payload and the signature block over its bytes.

    The payload is held as the parsed mapping rather than as the bytes it arrived
    in, because the digest under verification is the digest of the *canonical*
    bytes of that content. Re-serialising is the whole check: a document whose
    bytes were pretty-printed, whose keys were reordered, or whose arrays were
    shuffled canonicalises to the same bytes, and a document whose content was
    changed anywhere does not.
    """

    payload: Mapping[str, CanonicalValue]
    algorithm: str
    kms_key_id: str
    payload_digest: str
    signature: bytes

    def canonical_bytes(self) -> bytes:
        """The bytes the signature commits to, produced by the one canonicaliser."""
        return canonicalise(self.payload, array_rules=CERTIFICATE_ARRAY_RULES)

    def recomputed_digest(self) -> str:
        """The digest of the recomputed canonical bytes."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def parse_envelope(raw: bytes) -> Envelope:
    """Read one signed envelope from its stored bytes.

    A document that is not the documented envelope shape is refused here rather
    than producing a report whose checks silently had nothing to check.
    """
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationFailedError(
            "the certificate is not a readable document, so nothing can be verified"
        ) from error
    if not isinstance(document, dict):
        raise VerificationFailedError("the certificate is not an object at its top level")
    payload = document.get(PAYLOAD_KEY)
    block = document.get(SIGNATURE_KEY)
    if not isinstance(payload, dict) or not isinstance(block, dict):
        raise VerificationFailedError(
            "the certificate carries no payload and signature pair, so nothing is signed"
        )
    try:
        signature = base64.b64decode(_text(block, SIGNATURE_VALUE_FIELD), validate=True)
    except (ValueError, TypeError) as error:
        raise VerificationFailedError("the signature value is not an encoded value") from error
    return Envelope(
        payload=payload,
        algorithm=_text(block, ALGORITHM_FIELD),
        kms_key_id=_text(block, KEY_ID_FIELD),
        payload_digest=_text(block, PAYLOAD_DIGEST_FIELD),
        signature=signature,
    )


def load_envelope(
    location: CertificateLocation,
    *,
    objects: ObjectSource | None = None,
    settings: CertificateSettings | None = None,
) -> Envelope:
    """Load one envelope from a local path or from an object key.

    The object path needs both a reader and the bucket the settings name, and says
    so rather than reaching for a client of its own, which is what lets the whole
    algorithm run with no credential present.
    """
    if location.path is not None:
        try:
            raw = location.path.read_bytes()
        except OSError as error:
            raise VerificationFailedError(
                "the certificate could not be read from the local path given"
            ) from error
        return parse_envelope(raw)
    if objects is None or settings is None:
        raise VerificationFailedError(
            "reading a certificate by object key needs an object reader and the "
            "configured evidence bucket"
        )
    key = location.object_key
    if key is None:  # pragma: no cover - the location invariant already holds this
        raise VerificationFailedError("the certificate location names no object key")
    return parse_envelope(objects.read_object(bucket=settings.bucket, key=key))


# ---------------------------------------------------------------------------
# The local signature check
# ---------------------------------------------------------------------------


def verify_signature(
    digest: bytes,
    signature: bytes,
    *,
    public_key: bytes,
    algorithm: str,
) -> bool:
    """Check an asymmetric signature over a digest against a retrieved public half.

    The check is local. The public half is passed in, the curve arithmetic happens
    in this process, and the key service is not asked whether the signature is
    good, so a reviewer who has lost the permission to call that service, or who
    never held it, verifies exactly as well as the issuer does.

    A malformed key, a truncated signature, an algorithm the key does not use, and
    a signature over other bytes are all one answer: the signature does not
    verify. They are collapsed on purpose, because a caller's response to each is
    the same and distinguishing them would invite a branch no requirement asks
    for.
    """
    if algorithm != SUPPORTED_ALGORITHM:
        return False
    try:
        loaded = load_der_public_key(public_key)
    except (ValueError, UnsupportedAlgorithm):
        return False
    if not isinstance(loaded, ec.EllipticCurvePublicKey):
        return False
    try:
        loaded.verify(signature, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


@dataclass(frozen=True, slots=True)
class LocalSignatureChecker:
    """The signing surface as a verifier holds it: retrieval and a local check.

    This satisfies the shape the checkpoint verification asks for while signing
    nothing. The signing call is present and refuses, because a verifier that
    could sign would be a verifier holding the very privilege its independence
    rests on not holding.
    """

    keys: PublicKeySource

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        """Refuse: a verifier holds no signing privilege and produces no signature."""
        del digest, key_id, algorithm
        raise VerificationFailedError("the certificate verifier signs nothing")

    def public_key(self, *, key_id: str) -> bytes:
        """Retrieve the public half of the named key through the injected source."""
        return self.keys.public_key(key_id=key_id)

    def verify_digest(
        self,
        digest: bytes,
        signature: bytes,
        *,
        key_id: str,
        algorithm: str,
        public_key: bytes,
    ) -> bool:
        """Check a signature locally against the retrieved public half."""
        del key_id
        return verify_signature(digest, signature, public_key=public_key, algorithm=algorithm)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailedCheck:
    """One check that did not pass, named by value with what it was about.

    The subject is a position in the document or a row identifier, and the
    identifiers are the rows the finding is about, so a caller reports the finding
    without parsing a sentence.
    """

    check: str
    subject: str
    identifiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Note:
    """Something observed or deliberately not attempted. No note fails anything."""

    name: str
    detail: str


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """One Verification_Query, its row count, and the identifiers it returned."""

    name: str
    expectation: str
    row_count: int
    identifiers: tuple[str, ...]
    satisfied: bool


@dataclass(frozen=True, slots=True)
class CountComparison:
    """The re-derived counts beside the recorded ones, and the mechanism used."""

    mechanism: str
    recorded_before: int
    recorded_after: int
    derived_before: int
    derived_after: int

    @property
    def before_agrees(self) -> bool:
        """Whether the re-derived before-count matches the recorded one."""
        return self.recorded_before == self.derived_before

    @property
    def after_agrees(self) -> bool:
        """Whether the re-derived after-count matches the recorded one."""
        return self.recorded_after == self.derived_after

    @property
    def agrees(self) -> bool:
        """Whether both counts agree with the certificate."""
        return self.before_agrees and self.after_agrees


@dataclass(frozen=True, slots=True)
class Corroboration:
    """The opportunistic historical read: whether it ran, and what it found."""

    attempted: bool
    within_horizon: bool
    agrees: bool | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ChainOutcome:
    """One named Session's chain verification and the tip comparison beside it.

    Attributes:
        accounted_deletion: Whether this Session holds no rows because the attested
            run itself deleted them, which the certificate's own dispositions record.
            The recorded tip is then the tip as it stood before the run and the
            verified tip is the genesis predecessor, so the two do not agree and are
            not meant to: the certificate's claim about this Session is that its rows
            are gone, and their absence is that claim upheld rather than broken.
    """

    session_id: str
    ok: bool
    rows: int
    first_mismatch_seq: int | None
    recorded_tip: str
    verified_tip: str
    accounted_deletion: bool = False

    @property
    def tip_agrees(self) -> bool:
        """Whether the verified terminal digest is the one the certificate names."""
        return self.recorded_tip == self.verified_tip


@dataclass(frozen=True, slots=True)
class CheckpointOutcome:
    """The named Ledger_Checkpoint's accounting, partitioned as it was found."""

    checkpoint_id: str
    signature_verified: bool
    agrees: bool
    changed_sessions: tuple[str, ...]
    accounting_runs: tuple[str, ...]
    unaccounted_sessions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Everything one verification found, as values a caller reports or branches on."""

    outcome: str
    failed_checks: tuple[FailedCheck, ...] = ()
    queries: tuple[QueryOutcome, ...] = ()
    counts: CountComparison | None = None
    corroboration: Corroboration | None = None
    chains: tuple[ChainOutcome, ...] = ()
    checkpoint: CheckpointOutcome | None = None
    notes: tuple[Note, ...] = field(default_factory=tuple)

    @property
    def verified(self) -> bool:
        """Whether every check passed."""
        return self.outcome == OUTCOME_VERIFIED

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        """The distinct names of the failed checks, in the order they were found."""
        return tuple(dict.fromkeys(entry.check for entry in self.failed_checks))

    def as_mapping(self) -> Mapping[str, CanonicalValue]:
        """The report as one machine-readable structure, for an output stream."""
        return {
            "outcome": self.outcome,
            "failed_checks": [
                {
                    "check": entry.check,
                    "subject": entry.subject,
                    "identifiers": list(entry.identifiers),
                }
                for entry in self.failed_checks
            ],
            "verification_queries": [
                {
                    "name": entry.name,
                    "expectation": entry.expectation,
                    "row_count": str(entry.row_count),
                    "identifiers": list(entry.identifiers),
                    "satisfied": entry.satisfied,
                }
                for entry in self.queries
            ],
            "counts": None
            if self.counts is None
            else {
                "count_derivation": self.counts.mechanism,
                "recorded_before": str(self.counts.recorded_before),
                "recorded_after": str(self.counts.recorded_after),
                "derived_before": str(self.counts.derived_before),
                "derived_after": str(self.counts.derived_after),
                "agrees": self.counts.agrees,
            },
            "historical_corroboration": None
            if self.corroboration is None
            else {
                "attempted": self.corroboration.attempted,
                "within_horizon": self.corroboration.within_horizon,
                "agrees": self.corroboration.agrees,
                "reason": self.corroboration.reason,
            },
            "sessions": [
                {
                    "session_id": entry.session_id,
                    "ok": entry.ok,
                    "row_count": str(entry.rows),
                    "first_mismatch_seq": None
                    if entry.first_mismatch_seq is None
                    else str(entry.first_mismatch_seq),
                    "tip_agrees": entry.tip_agrees,
                    "accounted_deletion": entry.accounted_deletion,
                }
                for entry in self.chains
            ],
            "ledger_checkpoint": None
            if self.checkpoint is None
            else {
                "checkpoint_id": self.checkpoint.checkpoint_id,
                "signature_verified": self.checkpoint.signature_verified,
                "agrees": self.checkpoint.agrees,
                "changed_sessions": list(self.checkpoint.changed_sessions),
                "accounting_runs": list(self.checkpoint.accounting_runs),
                "unaccounted_sessions": list(self.checkpoint.unaccounted_sessions),
            },
            "notes": [{"name": entry.name, "detail": entry.detail} for entry in self.notes],
        }


def exit_code(report: VerificationReport) -> int:
    """The process status one report maps to: zero for verified, non-zero otherwise."""
    return EXIT_VERIFIED if report.verified else EXIT_FAILED


# ---------------------------------------------------------------------------
# The read-only guarantee
# ---------------------------------------------------------------------------


def require_reader_role(store: MemoryStore) -> str:
    """Refuse to verify over a connection that is not the read-only role.

    Both the configured label and the role the cluster reports are checked, and
    the cluster's answer is the one that decides, because a label is a claim and a
    role is a privilege. This makes the no-mutation guarantee structural: a
    verification cannot write, whatever the code that follows attempts, because the
    role it runs as holds no privilege to.

    Neither half is satisfied by silence. An unnamed label and a connection that
    reports no role are stores that prove nothing about their privileges, and
    admitting them would make the guarantee a default rather than a check: the
    refusal is what a reviewer is relying on when the label is missing precisely
    because nobody set it. Every path that verifies opens its store under the
    read-only role by name, so there is no caller for whom nothing named is the
    legitimate state.

    The cluster's half is asked as a membership question rather than by comparing the
    connected user's name against the role's. A deployment logs in as a component's own
    login and grants it the role, so the connected user carries the read-only role without
    being named it, and a name comparison refused every connection a deployment can make.
    A login that *is* the role satisfies membership too, so nothing that used to pass now
    fails.
    """
    label = store.role.strip().lower()
    if label not in READER_ROLE_NAMES:
        raise VerificationFailedError(
            "certificate verification runs as the read-only role, and the configured "
            "role does not name that role, so nothing was read"
        )
    rows = _read_rows(store, CURRENT_ROLE_QUERY, ())
    reported = "" if not rows else str(rows[0][0]).strip().lower()
    if not reported:
        raise VerificationFailedError(
            "the connection reports no role at all, so verification would not carry the "
            "no-mutation guarantee it claims"
        )
    if reported not in READER_ROLE_NAMES and not _carries_reader_role(store):
        raise VerificationFailedError(
            f"the connection is authenticated as {reported}, which neither is nor holds "
            "the read-only role, so verification would not carry the no-mutation "
            "guarantee it claims"
        )
    return reported


def _carries_reader_role(store: MemoryStore) -> bool:
    """Whether the connected login holds the read-only role, as the cluster reports it.

    A cluster that cannot answer the question is treated as answering no. The refusal is
    the guarantee, so an unanswerable membership check must not become an admission.
    """
    try:
        rows = _read_rows(store, READER_MEMBERSHIP_QUERY, (_READER_ROLE, _MEMBERSHIP))
    except StoreError:
        return False
    return bool(rows) and rows[0][0] is True


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _check_signature(envelope: Envelope, *, keys: PublicKeySource) -> list[FailedCheck]:
    """Recompute the canonical digest and check the signature over it locally."""
    failures: list[FailedCheck] = []
    recomputed = envelope.recomputed_digest()
    if recomputed != envelope.payload_digest:
        failures.append(FailedCheck(SIGNATURE_INVALID, PAYLOAD_DIGEST_SUBJECT))
    public_key = keys.public_key(key_id=envelope.kms_key_id)
    if not verify_signature(
        bytes.fromhex(recomputed),
        envelope.signature,
        public_key=public_key,
        algorithm=envelope.algorithm,
    ):
        failures.append(FailedCheck(SIGNATURE_INVALID, SIGNATURE_VALUE_SUBJECT))
    return failures


def _check_queries(
    store: MemoryStore,
    payload: Mapping[str, CanonicalValue],
) -> tuple[list[QueryOutcome], list[FailedCheck]]:
    """Run every obligatory query through its template and report each row count.

    Two things about this check belong to the verifier rather than to the document.

    The expectation applied is the template's own, taken from the registry above. A
    certificate declaring a laxer expectation beside a query would otherwise decide
    whether the erasure-completeness check was made, which is the one check a
    hostile reviewer relies on. A declared expectation that disagrees with the
    template is a query that is not the template it names, so it is reported under
    the same name as any other departure from the fixed set, and it is a failed
    check rather than a note because a note would leave the outcome `verified` and
    the divergence is exactly the lever an issuer would reach for.

    Presence is required rather than assumed. Every template the registry declares
    obligatory must be presented by some entry; one that is not is reported by the
    name of the missing query, because a certificate carrying no queries at all
    would otherwise report `verified` with this check never performed.
    """
    outcomes: list[QueryOutcome] = []
    failures: list[FailedCheck] = []
    presented: set[str] = set()
    for entry in _sequence(payload, QUERIES_KEY):
        name = _text(entry, NAME_FIELD)
        declared = _text(entry, EXPECTATION_FIELD)
        template = QUERY_TEMPLATES.get(name)
        parameters = _parameters(entry)
        if template is None or _normalised(_text(entry, SQL_FIELD)) != _normalised(
            template.documented_sql
        ):
            failures.append(FailedCheck(QUERY_TEMPLATE_UNKNOWN, name))
            continue
        if len(parameters) != template.parameter_count:
            failures.append(FailedCheck(QUERY_TEMPLATE_UNKNOWN, name))
            continue
        presented.add(name)
        if declared != template.expectation:
            failures.append(FailedCheck(QUERY_TEMPLATE_UNKNOWN, name, (declared,)))
        rows = _read_rows(store, template.statement, parameters)
        identifiers = tuple(str(row[0]) for row in rows if row)
        satisfied = not (template.expectation == EXPECTATION_EMPTY and rows)
        outcomes.append(
            QueryOutcome(
                name=name,
                expectation=template.expectation,
                row_count=len(rows),
                identifiers=identifiers,
                satisfied=satisfied,
            )
        )
        if not satisfied:
            failures.append(FailedCheck(ERASURE_INCOMPLETE, name, identifiers))
    failures.extend(
        FailedCheck(VERIFICATION_QUERY_MISSING, missing)
        for missing in OBLIGATORY_TEMPLATE_NAMES
        if missing not in presented
    )
    return outcomes, failures


def _check_counts(
    store: MemoryStore,
    payload: Mapping[str, CanonicalValue],
    *,
    now: datetime,
) -> tuple[CountComparison, Corroboration, list[FailedCheck], list[Note]]:
    """Re-derive the before-count and after-count, then corroborate opportunistically.

    The derived mechanism is the primary path and is always run: both of its terms
    come from rows that outlive the cluster's garbage-collection horizon, which is
    why a certificate verified long after issue still reports an outcome rather
    than an expiry. The historical read is attempted only inside the horizon, and
    not attempting it is a note.
    """
    client = _mapping(payload, CLIENT_KEY)
    run = _mapping(payload, RUN_KEY)
    counts = _mapping(payload, COUNTS_KEY)
    client_id = _uuid(_text(client, CLIENT_ID_FIELD))
    slug = _text(client, SLUG_FIELD)
    run_id = _uuid(_text(run, RUN_ID_FIELD))

    live = _scalar(_read_rows(store, LIVE_COUNT_QUERY, (client_id,)))
    disposed = _scalar(_read_rows(store, DISPOSED_BEFORE_QUERY, (run_id, slug)))
    comparison = CountComparison(
        mechanism=DERIVED_MECHANISM,
        recorded_before=_count(counts, BEFORE_FIELD),
        recorded_after=_count(counts, AFTER_FIELD),
        derived_before=disposed + live,
        derived_after=live,
    )

    failures: list[FailedCheck] = []
    if not comparison.before_agrees:
        failures.append(FailedCheck(COUNT_DISAGREEMENT, BEFORE_SUBJECT))
    if not comparison.after_agrees:
        failures.append(FailedCheck(COUNT_DISAGREEMENT, AFTER_SUBJECT))

    notes: list[Note] = [Note(DERIVATION_NOTE, _text(counts, DERIVATION_FIELD))]
    corroboration = _corroborate(
        store,
        client_id=client_id,
        t_before=_moment(run, T_BEFORE_FIELD),
        t_after=_optional_moment(run, T_AFTER_FIELD),
        derived=comparison,
        now=now,
    )
    notes.append(
        Note(
            HISTORICAL_CORROBORATION_NOTE,
            corroboration.reason
            if corroboration.reason
            else f"attempted, agrees={corroboration.agrees}",
        )
    )
    return comparison, corroboration, failures, notes


def _corroborate(
    store: MemoryStore,
    *,
    client_id: UUID,
    t_before: datetime,
    t_after: datetime | None,
    derived: CountComparison,
    now: datetime,
) -> Corroboration:
    """Read the two counts at the two instants, but only inside the horizon.

    The horizon comes from the capability record rather than from a constant here,
    so a cluster configured differently is respected rather than assumed about. A
    horizon nobody probed, and an instant the horizon no longer covers, both end
    as an unattempted corroboration carrying its reason, which is a note rather
    than a failed check.

    A run that recorded no after-instant is the same kind of outcome and is a note
    for the same reason: the after-instant is nullable on the run because a run that
    did not complete has none, so there is no second instant to read the count at
    and nothing was withheld. The derived mechanism is the primary path and needs
    neither timestamp, so both counts are still compared and a certificate for an
    unfinished run reports rather than raising.
    """
    if t_after is None:
        return Corroboration(attempted=False, within_horizon=False, reason=INCOMPLETE_RUN_REASON)
    try:
        horizon = store.gc_horizon()
    except StoreError:
        return Corroboration(attempted=False, within_horizon=False, reason=UNPROBED_HORIZON_REASON)
    within = store.within_gc_horizon(
        t_before, now=now, horizon=horizon
    ) and store.within_gc_horizon(t_after, now=now, horizon=horizon)
    if not within:
        return Corroboration(attempted=False, within_horizon=False, reason=OUTSIDE_HORIZON_REASON)
    try:
        before = _scalar(
            store.historical(LIVE_COUNT_QUERY, (client_id,), at=t_before, now=now, horizon=horizon)
        )
        after = _scalar(
            store.historical(LIVE_COUNT_QUERY, (client_id,), at=t_after, now=now, horizon=horizon)
        )
    except StoreError:
        return Corroboration(attempted=False, within_horizon=True, reason=HORIZON_REFUSED_REASON)
    return Corroboration(
        attempted=True,
        within_horizon=True,
        agrees=(before, after) == (derived.derived_before, derived.derived_after),
    )


def _hard_deleted_artifacts(payload: Mapping[str, CanonicalValue]) -> frozenset[str]:
    """The identifiers the certificate's own dispositions record as hard-deleted.

    Read from the signed payload and from nowhere else. The set decides how a missing
    Session is read, so taking it from a live query would let present state settle
    what the document is claiming, and taking it from outside the document would let
    something nobody signed do so.
    """
    return frozenset(
        _text(entry, ARTIFACT_ID_FIELD)
        for entry in _sequence(payload, DISPOSITIONS_KEY)
        if _text(entry, DISPOSITION_FIELD) == _HARD_DELETE
    )


def _check_chains(
    store: MemoryStore,
    payload: Mapping[str, CanonicalValue],
) -> tuple[list[ChainOutcome], list[FailedCheck], list[Note]]:
    """Verify every named Session's chain, allowing for a Session this run deleted.

    A named Session the certificate's own dispositions record as hard-deleted has
    two arms rather than one, for the reason the checkpoint's accounting query has
    two: a redaction leaves a joinable row, and a deletion leaves nothing to join
    to. The per-Session record the run wrote holds the tip as it stood before the
    erasure and outlives the rows it describes, so a certificate for a completed
    tenant erasure names Sessions that are meant to be gone. Re-deriving a tip from
    zero rows yields the genesis predecessor, which agrees with no recorded tip, so
    treating absence as a tip mismatch would make every such certificate
    unverifiable — and those are precisely the certificates an auditor reads.

    So absence is an accounted outcome here: the chain must hold zero rows, that
    zero is recorded on the outcome and as a note, and no check fails. It is not
    ignored. Rows surviving for a Session the certificate says was deleted is the
    opposite finding — content outlived a recorded deletion — and fails as an
    artifact still present.

    A Session the certificate does not record as deleted is unchanged: its chain
    must re-derive from its stored rows and its tip must be the tip the certificate
    committed to.

    The recorded tip is nullable on the per-Session row, so a certificate can carry
    no tip for a Session. For a Session this run deleted that changes nothing: that
    arm never compares tips. For a surviving Session it is a failed check rather
    than a note, because the tip is the whole of what the certificate claims about
    that Session's Events and a document naming none has committed to nothing that
    could be checked. A note beside it records that the tip was absent rather than
    different, so a reviewer reads which of the two it was, and the failure is
    reported under the tip check rather than aborting the verification.
    """
    deleted = _hard_deleted_artifacts(payload)
    outcomes: list[ChainOutcome] = []
    failures: list[FailedCheck] = []
    notes: list[Note] = []
    for entry in _sequence(payload, SESSIONS_KEY):
        session_text = _text(entry, SESSION_ID_FIELD)
        recorded_tip = _optional_text(entry, TERMINAL_DIGEST_FIELD)
        report = verify_chain(store, _uuid(session_text))
        was_deleted = session_text in deleted
        outcome = ChainOutcome(
            session_id=session_text,
            ok=report.ok,
            rows=report.rows,
            first_mismatch_seq=report.first_mismatch_seq,
            recorded_tip="" if recorded_tip is None else recorded_tip,
            verified_tip=report.terminal_digest,
            accounted_deletion=was_deleted and report.rows == 0,
        )
        outcomes.append(outcome)
        if was_deleted:
            if report.rows:
                failures.append(
                    FailedCheck(ARTIFACT_STILL_PRESENT, session_text, (str(report.rows),))
                )
            else:
                notes.append(Note(SESSION_DELETED_NOTE, session_text))
            continue
        if not report.ok:
            failures.append(
                FailedCheck(CHAIN_MISMATCH, session_text, (str(report.first_mismatch_seq),))
            )
        elif recorded_tip is None:
            notes.append(Note(SESSION_TIP_ABSENT_NOTE, session_text))
            failures.append(FailedCheck(CHAIN_TIP_MISMATCH, session_text))
        elif not outcome.tip_agrees:
            failures.append(FailedCheck(CHAIN_TIP_MISMATCH, session_text))
    return outcomes, failures, notes


def _check_checkpoint(
    store: MemoryStore,
    payload: Mapping[str, CanonicalValue],
    *,
    checker: LocalSignatureChecker,
) -> tuple[CheckpointOutcome | None, list[FailedCheck], list[Note]]:
    """Verify the Ledger_Checkpoint the certificate names, partitioning disagreement.

    A checkpoint reaches every Session in its window rather than only the Sessions
    this run touched, which is why verifying the certificate verifies the
    checkpoint too. A disagreement every changed Session's recorded Erasure_Run
    accounts for is an explanation and is recorded as a note; a change nothing on
    the record explains is the finding.
    """
    block = payload.get(CHECKPOINT_KEY)
    if not isinstance(block, Mapping):
        return None, [], []
    identifier = _text(block, CHECKPOINT_ID_FIELD)
    try:
        found = verify_checkpoint(store, _uuid(identifier), signer=checker)
    except VerificationFailedError:
        return None, [FailedCheck(CHECKPOINT_ABSENT, identifier)], []
    outcome = CheckpointOutcome(
        checkpoint_id=identifier,
        signature_verified=found.signature_verified,
        agrees=found.agrees,
        changed_sessions=tuple(str(entry) for entry in found.changed_sessions),
        accounting_runs=tuple(str(entry) for entry in found.accounting_runs),
        unaccounted_sessions=tuple(str(change.session_id) for change in found.unaccounted_changes),
    )
    failures: list[FailedCheck] = []
    if not found.signature_verified:
        failures.append(FailedCheck(CHECKPOINT_SIGNATURE_INVALID, identifier))
    if outcome.unaccounted_sessions:
        failures.append(
            FailedCheck(CHECKPOINT_UNEXPLAINED_CHANGE, identifier, outcome.unaccounted_sessions)
        )
    notes = _checkpoint_notes(store, payload, identifier, found)
    return outcome, failures, notes


def _checkpoint_notes(
    store: MemoryStore,
    payload: Mapping[str, CanonicalValue],
    identifier: str,
    found: CheckpointVerification,
) -> list[Note]:
    """Record what a checkpoint disagreement explained, and whether it is the right one."""
    notes: list[Note] = []
    if found.accounted_changes:
        notes.append(
            Note(
                CHECKPOINT_EXPLAINED_NOTE,
                f"{len(found.accounted_changes)} changed session(s) are accounted for by "
                f"{len(found.accounting_runs)} recorded erasure run(s)",
            )
        )
    latest = latest_before(store, _moment(_mapping(payload, RUN_KEY), T_BEFORE_FIELD))
    if latest is not None and str(latest.checkpoint_id) != identifier:
        notes.append(Note(CHECKPOINT_SUCCESSION_NOTE, str(latest.checkpoint_id)))
    return notes


def _check_dispositions(
    store: MemoryStore,
    payload: Mapping[str, CanonicalValue],
) -> list[FailedCheck]:
    """Check the disposition claims with two set-based queries over bound arrays.

    One statement per artifact would make the cost of verification linear in the
    document's length in round trips rather than in rows, which is what the 1000
    artifact bound rules out. So the identifiers travel as two arrays.
    """
    deleted: list[UUID] = []
    redacted: dict[str, str] = {}
    for entry in _sequence(payload, DISPOSITIONS_KEY):
        artifact = _text(entry, ARTIFACT_ID_FIELD)
        decision = _text(entry, DISPOSITION_FIELD)
        if decision == _HARD_DELETE:
            deleted.append(_uuid(artifact))
        elif decision == _SURGICAL_REDACTION:
            digest = entry.get(POST_DIGEST_FIELD)
            if isinstance(digest, str):
                redacted[artifact] = digest

    failures: list[FailedCheck] = []
    if deleted:
        present = tuple(
            str(row[0]) for row in _read_rows(store, EXISTING_ARTIFACTS_QUERY, (deleted, deleted))
        )
        if present:
            failures.append(FailedCheck(ARTIFACT_STILL_PRESENT, DISPOSITIONS_KEY, present))
    if redacted:
        keys = [_uuid(artifact) for artifact in sorted(redacted)]
        live = {
            str(row[0]): str(row[1]) for row in _read_rows(store, CURRENT_DIGESTS_QUERY, (keys,))
        }
        mismatched = tuple(
            artifact
            for artifact, digest in sorted(redacted.items())
            if live.get(artifact) != digest
        )
        if mismatched:
            failures.append(FailedCheck(REDACTION_DIGEST_MISMATCH, DISPOSITIONS_KEY, mismatched))
    return failures


# ---------------------------------------------------------------------------
# The whole verification
# ---------------------------------------------------------------------------


def verify_certificate(
    envelope: Envelope,
    *,
    store: MemoryStore,
    keys: PublicKeySource,
    now: datetime | None = None,
) -> VerificationReport:
    """Verify one loaded envelope end to end and report what was found.

    The order is the order of increasing cost: the cryptographic check needs no
    cluster at all, the embedded queries are two statements, the counts are two
    more, and the chain and checkpoint work scales with what the certificate names.
    Every check runs regardless of an earlier failure, because a reviewer wants the
    whole picture rather than the first thing that went wrong.
    """
    reading = datetime.now(tz=UTC) if now is None else now
    require_reader_role(store)
    checker = LocalSignatureChecker(keys=keys)

    failures: list[FailedCheck] = list(_check_signature(envelope, keys=keys))
    queries, query_failures = _check_queries(store, envelope.payload)
    failures.extend(query_failures)
    counts, corroboration, count_failures, notes = _check_counts(
        store, envelope.payload, now=reading
    )
    failures.extend(count_failures)
    chains, chain_failures, chain_notes = _check_chains(store, envelope.payload)
    failures.extend(chain_failures)
    notes.extend(chain_notes)
    checkpoint, checkpoint_failures, checkpoint_notes = _check_checkpoint(
        store, envelope.payload, checker=checker
    )
    failures.extend(checkpoint_failures)
    notes.extend(checkpoint_notes)
    failures.extend(_check_dispositions(store, envelope.payload))

    report = VerificationReport(
        outcome=OUTCOME_FAILED if failures else OUTCOME_VERIFIED,
        failed_checks=tuple(failures),
        queries=tuple(queries),
        counts=counts,
        corroboration=corroboration,
        chains=tuple(chains),
        checkpoint=checkpoint,
        notes=tuple(notes),
    )
    _report(report)
    return report


def verify_source(
    location: CertificateLocation,
    *,
    store: MemoryStore,
    keys: PublicKeySource,
    objects: ObjectSource | None = None,
    settings: CertificateSettings | None = None,
    now: datetime | None = None,
) -> VerificationReport:
    """Load a certificate from a local path or an object key, then verify it."""
    envelope = load_envelope(location, objects=objects, settings=settings)
    return verify_certificate(envelope, store=store, keys=keys, now=now)


def _report(report: VerificationReport) -> None:
    """Emit the outcome as one measurement and one record naming the failed checks."""
    metric(VERIFICATION_METRIC, 1.0, **{OUTCOME_DIMENSION: report.outcome})
    log(
        Severity.INFO if report.verified else Severity.WARNING,
        COMPONENT,
        "an erasure certificate was verified",
        outcome=report.outcome,
        failed_checks=",".join(report.failed_check_names),
        query_count=len(report.queries),
        session_count=len(report.chains),
    )


# ---------------------------------------------------------------------------
# Reading rows and narrowing values
# ---------------------------------------------------------------------------


def _read_rows(
    store: MemoryStore,
    statement: str,
    parameters: Sequence[object],
) -> tuple[tuple[object, ...], ...]:
    """Run one read statement with its values bound and return every row."""

    def body(cursor: Cursor) -> tuple[tuple[object, ...], ...]:
        cursor.execute(statement, tuple(parameters))
        return tuple(tuple(row) for row in cursor.fetchall())

    return store.read(body)


def _scalar(rows: Sequence[Sequence[object]]) -> int:
    """The single whole number a counting statement produced."""
    if not rows or not rows[0]:
        raise VerificationFailedError("a counting statement returned no row")
    value = rows[0][0]
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationFailedError("a counting statement returned something not a count")
    return value


def _normalised(sql: str) -> str:
    """A statement's text with its whitespace collapsed, for comparison alone.

    Comparison is on the statement's tokens rather than on its layout, because a
    template rendered across two lines and the same template on one line are the
    same claim. Nothing normalised here is ever executed.
    """
    return " ".join(sql.split())


def _mapping(payload: Mapping[str, CanonicalValue], key: str) -> Mapping[str, CanonicalValue]:
    """One required object of the payload."""
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise VerificationFailedError(f"the certificate carries no {key} object")
    return value


def _sequence(
    payload: Mapping[str, CanonicalValue], key: str
) -> tuple[Mapping[str, CanonicalValue], ...]:
    """One required collection of the payload, whose elements are objects."""
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise VerificationFailedError(f"the certificate carries no {key} collection")
    entries: list[Mapping[str, CanonicalValue]] = []
    for element in value:
        if not isinstance(element, Mapping):
            raise VerificationFailedError(f"an element of {key} is not an object")
        entries.append(element)
    return tuple(entries)


def _text(entry: Mapping[str, object], name: str) -> str:
    """One required text field."""
    value = entry.get(name)
    if not isinstance(value, str):
        raise VerificationFailedError(f"the certificate field {name} is not text")
    return value


def _optional_text(entry: Mapping[str, object], name: str) -> str | None:
    """One text field that the record it came from allows to be null.

    An explicit null and an absent key are one answer here, because both say the
    document carries no value at that position, and a caller decides what that means
    for the check it is making rather than being handed an exception.
    """
    return None if entry.get(name) is None else _text(entry, name)


def _count(entry: Mapping[str, CanonicalValue], name: str) -> int:
    """One required count, held as a decimal string by the canonical rules.

    Every number in a certificate is a string, because that is what makes the
    signed bytes reproducible across producers. Reading one back therefore parses
    rather than casts, and a value that is not a whole decimal is refused instead
    of being coerced into a comparison that would silently pass.
    """
    value = entry.get(name)
    if isinstance(value, bool):
        raise VerificationFailedError(f"the certificate count {name} is not a whole number")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise VerificationFailedError(f"the certificate count {name} is not a whole number")
    try:
        return int(value, 10)
    except ValueError as error:
        raise VerificationFailedError(
            f"the certificate count {name} is not a whole decimal number"
        ) from error


def _moment(entry: Mapping[str, CanonicalValue], name: str) -> datetime:
    """One required timestamp, in the offset-carrying form the canonical rules fix."""
    value = entry.get(name)
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as error:
            raise VerificationFailedError(
                f"the certificate timestamp {name} is not in the recorded form"
            ) from error
    else:
        raise VerificationFailedError(f"the certificate timestamp {name} is absent")
    if moment.utcoffset() is None:
        raise VerificationFailedError(f"the certificate timestamp {name} carries no offset")
    return moment


def _optional_moment(entry: Mapping[str, CanonicalValue], name: str) -> datetime | None:
    """One timestamp the run record allows to be null, such as an after-instant.

    A malformed instant is still refused by the required reader: nullability is
    permission to carry nothing, not permission to carry anything.
    """
    return None if entry.get(name) is None else _moment(entry, name)


def _uuid(value: str) -> UUID:
    """One identifier, refusing anything that is not one."""
    try:
        return UUID(value)
    except ValueError as error:
        raise VerificationFailedError("a certificate identifier is not an identifier") from error


def _parameters(entry: Mapping[str, CanonicalValue]) -> tuple[object, ...]:
    """The bound parameters of one embedded query, narrowed where they are identifiers.

    An identifier arrives as text, because every scalar of a certificate is text,
    and it is converted to an identifier here so the comparison the cluster makes
    is between values of one type rather than between a value and a rendering.
    Nothing here reaches statement text: these are the bound values and only that.
    """
    value = entry.get(PARAMS_FIELD)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise VerificationFailedError("an embedded query carries no bound parameter list")
    bound: list[object] = []
    for element in value:
        if isinstance(element, str):
            try:
                bound.append(UUID(element))
            except ValueError:
                bound.append(element)
        else:
            bound.append(element)
    return tuple(bound)
