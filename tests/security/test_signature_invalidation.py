"""Four ways a signature stops standing, each reported as `signature_invalid`.

A tampered payload, a substituted signature, a wrong key identifier, and a
truncated signature are four different faults with one honest answer: the document
in hand is not the document that was signed, so nothing it claims has been
established. The verifier reports the same check name for all four and the process
status is non-zero for all four, which is what a reviewer's automation branches on.

The single-byte claim is asserted directly as well: one character of one scalar of
the payload changes the canonical bytes, so it changes the digest, so verification
fails. Nothing about that depends on which byte, which is why the property test
beside this one generates alterations at every level while this test pins the four
named faults.

Two local key pairs are generated in this process. Neither reaches a key service,
and the wrong-identifier case is modelled the way a key service would answer it:
the identifier names a real key, the retrieval returns that key's public half, and
the signature made under the other key does not verify against it.

Three further documents are signed, intact, and still refused. A certificate that
declares a laxer expectation beside a Verification_Query, one that lists no queries
at all, and one that names a Session without committing to its terminal digest are
each an attempt to have the completeness of an erasure taken on the document's own
word. The expectation applied and the queries a certificate owes are properties of
the fixed template set rather than fields of the payload, so none of the three
reaches a `verified` outcome, and the third produces a report rather than an
exception because an unverifiable certificate is a worse answer for an auditor than
a failed one.

**Validates: Requirements 22.3, 22.4, 22.7, 22.9, 36.13**
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, TypeVar, cast
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from molt.attest.canonical import CERTIFICATE_ARRAY_RULES, CanonicalValue, canonicalise
from molt.attest.verifier import (
    CHAIN_TIP_MISMATCH,
    CURRENT_BINDINGS_QUERY,
    CURRENT_DIGESTS_QUERY,
    CURRENT_ROLE_QUERY,
    DISPOSED_BEFORE_QUERY,
    EXISTING_ARTIFACTS_QUERY,
    LIVE_COUNT_QUERY,
    LIVE_SESSIONS_QUERY,
    QUERY_TEMPLATE_UNKNOWN,
    QUERY_TEMPLATES,
    SIGNATURE_INVALID,
    SUPPORTED_ALGORITHM,
    VERIFICATION_QUERY_MISSING,
    Envelope,
    VerificationReport,
    exit_code,
    verify_certificate,
)
from molt.errors import StoreError
from molt.store import MemoryStore
from molt.store.chain import GENESIS_PREDECESSOR

READER_ROLE: Final[str] = "molt_reader"
ISSUING_KEY_ID: Final[str] = "issuing-certificate-key"
OTHER_KEY_ID: Final[str] = "other-certificate-key"
ATTRIBUTION_QUERY_NAME: Final[str] = "no_current_attribution_remains"
SESSIONS_QUERY_NAME: Final[str] = "no_sessions_remain"
TRUNCATION_BYTES: Final[int] = 5

T = TypeVar("T")


# ---------------------------------------------------------------------------
# The two local key pairs and the retrieval seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Keys:
    """Two local key pairs, retrieved by identifier the way a key service answers.

    Both halves are held here because a test that wanted to model a wrong
    identifier by handing back nothing would be modelling an unavailable service
    rather than the wrong key, and those are different faults.
    """

    issuing: ec.EllipticCurvePrivateKey
    other: ec.EllipticCurvePrivateKey

    def sign_digest(self, digest: bytes, *, key_id: str) -> bytes:
        """Sign a digest under the named key."""
        return self._private(key_id).sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    def public_key(self, *, key_id: str) -> bytes:
        """The public half of the named key, in the encoded form a retrieval returns."""
        return (
            self._private(key_id)
            .public_key()
            .public_bytes(encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo)
        )

    def _private(self, key_id: str) -> ec.EllipticCurvePrivateKey:
        """The private half the identifier names, refusing an identifier naming none."""
        if key_id == ISSUING_KEY_ID:
            return self.issuing
        if key_id == OTHER_KEY_ID:
            return self.other
        raise AssertionError("the emulated key service holds no key under that identifier")


# ---------------------------------------------------------------------------
# The emulated cluster
# ---------------------------------------------------------------------------


class FakeCursor:
    """A cursor answering the statements the verifier sends over an erased cluster."""

    def __init__(self) -> None:
        self._rows: list[tuple[object, ...]] = []

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Answer one statement, holding its rows for the fetch that follows."""
        del params
        if query == CURRENT_ROLE_QUERY:
            self._rows = [(READER_ROLE,)]
        elif query == LIVE_COUNT_QUERY:
            self._rows = [(0,)]
        elif query == DISPOSED_BEFORE_QUERY:
            self._rows = [(1,)]
        elif query in (
            CURRENT_BINDINGS_QUERY,
            LIVE_SESSIONS_QUERY,
            EXISTING_ARTIFACTS_QUERY,
            CURRENT_DIGESTS_QUERY,
        ):
            self._rows = []
        else:
            self._rows = []
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """The first row of the last statement, or None when it produced none."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row of the last statement."""
        return list(self._rows)


class FakeStore:
    """The store surface the verifier reaches the emulated cluster through."""

    @property
    def role(self) -> str:
        """The read-only role label the verifier requires before reading anything."""
        return READER_ROLE

    def read(self, body: Callable[[FakeCursor], T]) -> T:
        """Run a read body against one cursor over the emulated tables."""
        return body(FakeCursor())

    def gc_horizon(self) -> object:
        """Refuse: the horizon is unprobed here, so no corroboration is attempted."""
        raise StoreError("the garbage-collection horizon is unprobed on this cluster")


# ---------------------------------------------------------------------------
# One signed certificate of the documented shape
# ---------------------------------------------------------------------------


def _moment(offset: int) -> str:
    """A fixed-form instant, rendered as the canonical rules require."""
    return (datetime(2001, 1, 1, tzinfo=UTC) + timedelta(seconds=offset)).isoformat(
        timespec="microseconds"
    )


def _payload() -> dict[str, CanonicalValue]:
    """A certificate payload for one completed erasure of one Client."""
    client_id = uuid4()
    return {
        "certificate_version": "1",
        "erasure_request": {
            "request_id": str(uuid4()),
            "requester": "governance-owner-principal",
            "justification": "engagement concluded under a contractual purge obligation",
            "submitted_at": _moment(0),
        },
        "client": {"client_id": str(client_id), "slug": "tenant"},
        "run": {
            "run_id": str(uuid4()),
            "dry_run": False,
            "t_before": _moment(10),
            "t_after": _moment(20),
            "auto_include_threshold": "0.200000",
            "review_threshold": "0.450000",
            "unembedded_artifact_count": "0",
            "working_rows_deleted": "0",
        },
        "ownership": {
            "owner": "worker-owner-identifier",
            "fencing_generation": "3",
            "idempotency_key": str(uuid4()),
        },
        "ledger_checkpoint": None,
        "backup": {
            "present": True,
            "backup_path": "self_managed",
            "taken": True,
            "referenced": False,
            "backup_id": str(uuid4()),
            "target_uri": "evidence-target",
            "statement": "BACKUP INTO the configured target",
        },
        "counts": {
            "artifacts_bound_before": "1",
            "artifacts_bound_after": "0",
            "count_derivation": "ledger_and_dispositions",
            "historical_corroboration": {
                "attempted": False,
                "within_horizon": False,
                "agrees": None,
                "gc_horizon_seconds": "4500",
            },
            "hard_delete": "1",
            "surgical_redaction": "0",
            "retained": "0",
        },
        "dispositions": [
            {
                "artifact_id": str(uuid4()),
                "artifact_kind": "derived_artifact",
                "disposition": "hard_delete",
                "reason": "client_binding_removed",
                "selection_reason": "client_binding",
                "pre_digest": hashlib.sha256(b"pre").hexdigest(),
                "post_digest": None,
                "bindings_before": ["tenant"],
                "bindings_after": [],
                "first_attributed_at": _moment(1),
                "first_attribution_method": "marker",
            }
        ],
        "lineage_subgraph": [],
        "residue_candidates": [],
        "sessions": [
            {
                "session_id": str(uuid4()),
                "terminal_chain_digest": GENESIS_PREDECESSOR,
                "terminal_seq": "0",
                "row_count": "0",
            }
        ],
        "verification_queries": [
            {
                "name": ATTRIBUTION_QUERY_NAME,
                "sql": QUERY_TEMPLATES[ATTRIBUTION_QUERY_NAME].documented_sql,
                "params": [str(client_id)],
                "expectation": "empty",
            },
            {
                "name": SESSIONS_QUERY_NAME,
                "sql": QUERY_TEMPLATES[SESSIONS_QUERY_NAME].documented_sql,
                "params": [str(client_id)],
                "expectation": "empty",
            },
        ],
        "cluster_audit_log": {
            "window_start": _moment(10),
            "window_end": _moment(20),
            "records": [],
        },
        "caveats": {
            "historical_read_bound": "historical reads are bounded by the cluster horizon",
            "durable_evidence": "the append-only ledger and the dispositions are primary",
            "checkpoint_scope": "a checkpoint provides tamper evidence rather than proofing",
            "working_tier_excluded": "no field is derived from the working tier",
        },
    }


def _digest_of(payload: Mapping[str, CanonicalValue]) -> str:
    """The digest of a payload's canonical bytes, as the signing flow computes it."""
    return hashlib.sha256(canonicalise(payload, array_rules=CERTIFICATE_ARRAY_RULES)).hexdigest()


@pytest.fixture(name="keys")
def keys_fixture() -> Keys:
    """Two local key pairs, generated once per test."""
    return Keys(
        issuing=ec.generate_private_key(ec.SECP256R1()),
        other=ec.generate_private_key(ec.SECP256R1()),
    )


def _report(envelope: Envelope, keys: Keys) -> VerificationReport:
    """Verify one envelope against the emulated erased cluster."""
    return verify_certificate(envelope, store=cast("MemoryStore", FakeStore()), keys=keys)


def _signed(payload: Mapping[str, CanonicalValue], keys: Keys) -> Envelope:
    """One envelope signed under the issuing key, as the builder produces it."""
    digest = _digest_of(payload)
    return Envelope(
        payload=payload,
        algorithm=SUPPORTED_ALGORITHM,
        kms_key_id=ISSUING_KEY_ID,
        payload_digest=digest,
        signature=keys.sign_digest(bytes.fromhex(digest), key_id=ISSUING_KEY_ID),
    )


def _assert_invalid(report: VerificationReport) -> None:
    """The one answer all four faults produce: the named check, and a non-zero exit."""
    assert not report.verified
    assert report.outcome == "failed"
    assert SIGNATURE_INVALID in report.failed_check_names
    assert exit_code(report) != 0


# ---------------------------------------------------------------------------
# The baseline: an untouched certificate verifies
# ---------------------------------------------------------------------------


def test_an_untouched_certificate_verifies_and_exits_zero(keys: Keys) -> None:
    """Requirement 22.9: the outcome is machine-readable and the exit status follows it."""
    report = _report(_signed(_payload(), keys), keys)
    assert report.verified
    assert report.outcome == "verified"
    assert report.failed_checks == ()
    assert exit_code(report) == 0


# ---------------------------------------------------------------------------
# The four faults
# ---------------------------------------------------------------------------


def test_a_tampered_payload_reports_signature_invalid(keys: Keys) -> None:
    """Requirement 22.3: altering the payload after signing invalidates the signature."""
    payload = _payload()
    envelope = _signed(payload, keys)
    tampered = dict(payload)
    counts = dict(cast("Mapping[str, CanonicalValue]", tampered["counts"]))
    counts["artifacts_bound_before"] = "2"
    tampered["counts"] = counts

    report = _report(
        Envelope(
            payload=tampered,
            algorithm=envelope.algorithm,
            kms_key_id=envelope.kms_key_id,
            payload_digest=envelope.payload_digest,
            signature=envelope.signature,
        ),
        keys,
    )
    _assert_invalid(report)
    assert {entry.subject for entry in report.failed_checks} >= {"payload_digest"}


def test_a_single_altered_byte_of_the_payload_reports_signature_invalid(keys: Keys) -> None:
    """Requirement 36.13: one character of one scalar is enough to invalidate."""
    payload = _payload()
    envelope = _signed(payload, keys)
    altered = dict(payload)
    client = dict(cast("Mapping[str, CanonicalValue]", altered["client"]))
    slug = cast("str", client["slug"])
    client["slug"] = slug[:-1] + ("a" if slug[-1] != "a" else "b")
    altered["client"] = client

    _assert_invalid(
        _report(
            Envelope(
                payload=altered,
                algorithm=envelope.algorithm,
                kms_key_id=envelope.kms_key_id,
                payload_digest=envelope.payload_digest,
                signature=envelope.signature,
            ),
            keys,
        )
    )


def test_a_substituted_signature_reports_signature_invalid(keys: Keys) -> None:
    """A signature over other bytes, made with the right key, still does not stand."""
    payload = _payload()
    envelope = _signed(payload, keys)
    substituted = keys.sign_digest(hashlib.sha256(b"other bytes").digest(), key_id=ISSUING_KEY_ID)
    assert substituted != envelope.signature

    report = _report(
        Envelope(
            payload=payload,
            algorithm=envelope.algorithm,
            kms_key_id=envelope.kms_key_id,
            payload_digest=envelope.payload_digest,
            signature=substituted,
        ),
        keys,
    )
    _assert_invalid(report)
    assert {entry.subject for entry in report.failed_checks} == {"signature_value"}


def test_a_wrong_key_identifier_reports_signature_invalid(keys: Keys) -> None:
    """Requirement 22.2: the check is against the key the envelope names, retrieved.

    The digest still matches, so the only thing that fails is the check against the
    retrieved public half, which is exactly the guarantee: naming a different key
    does not let a document verify.
    """
    payload = _payload()
    envelope = _signed(payload, keys)

    report = _report(
        Envelope(
            payload=payload,
            algorithm=envelope.algorithm,
            kms_key_id=OTHER_KEY_ID,
            payload_digest=envelope.payload_digest,
            signature=envelope.signature,
        ),
        keys,
    )
    _assert_invalid(report)
    assert {entry.subject for entry in report.failed_checks} == {"signature_value"}


def test_a_truncated_signature_reports_signature_invalid(keys: Keys) -> None:
    """A signature missing its tail is a signature that does not verify, not an error."""
    payload = _payload()
    envelope = _signed(payload, keys)

    report = _report(
        Envelope(
            payload=payload,
            algorithm=envelope.algorithm,
            kms_key_id=envelope.kms_key_id,
            payload_digest=envelope.payload_digest,
            signature=envelope.signature[:-TRUNCATION_BYTES],
        ),
        keys,
    )
    _assert_invalid(report)
    assert {entry.subject for entry in report.failed_checks} == {"signature_value"}


# ---------------------------------------------------------------------------
# Three documents that are signed, intact, and still do not verify
# ---------------------------------------------------------------------------


def _queries_of(payload: Mapping[str, CanonicalValue]) -> list[dict[str, CanonicalValue]]:
    """The payload's verification-query entries, as mutable copies."""
    return [
        dict(entry)
        for entry in cast("Sequence[Mapping[str, CanonicalValue]]", payload["verification_queries"])
    ]


def test_a_weakened_expectation_does_not_switch_off_the_completeness_check(keys: Keys) -> None:
    """Requirement 22.4: the expectation applied is the template's, not the document's.

    The issuer signs the weakened document, so nothing here is a tampering fault:
    the signature stands and the finding is that the query is not the fixed template
    it names. That is the whole point of the fixed set — a certificate cannot choose
    the terms it is judged on.
    """
    payload = _payload()
    queries = _queries_of(payload)
    queries[0]["expectation"] = "any"
    payload["verification_queries"] = queries

    report = _report(_signed(payload, keys), keys)
    assert not report.verified
    assert SIGNATURE_INVALID not in report.failed_check_names
    assert QUERY_TEMPLATE_UNKNOWN in report.failed_check_names
    assert exit_code(report) != 0
    applied = next(entry for entry in report.queries if entry.name == ATTRIBUTION_QUERY_NAME)
    assert applied.expectation == "empty", (
        "the reported expectation is the one the verifier applied, which is the "
        "template's, so a reader of the report sees the terms of the check made"
    )


def test_a_certificate_listing_no_queries_reports_each_obligatory_query_missing(
    keys: Keys,
) -> None:
    """Requirement 22.4: an obligatory check nobody made is a finding, not a pass.

    A document carrying an empty query list previously reported `verified` with the
    erasure-completeness check never performed, which is the one check a hostile
    reviewer relies on.
    """
    payload = _payload()
    payload["verification_queries"] = []

    report = _report(_signed(payload, keys), keys)
    assert not report.verified
    assert report.queries == ()
    assert VERIFICATION_QUERY_MISSING in report.failed_check_names
    missing = {
        entry.subject for entry in report.failed_checks if entry.check == VERIFICATION_QUERY_MISSING
    }
    assert missing == {ATTRIBUTION_QUERY_NAME, SESSIONS_QUERY_NAME}
    assert exit_code(report) != 0


def test_a_null_session_tip_is_reported_rather_than_left_unverifiable(keys: Keys) -> None:
    """Requirement 22.7: a nullable field the document leaves empty still yields a report.

    The recorded tip is nullable on the per-Session row, so this document is one a
    cluster can produce. An auditor holding it needs a verdict rather than an
    exception, so the absent commitment is reported under the tip check and the rest
    of the verification still runs.
    """
    payload = _payload()
    sessions = [
        dict(entry) for entry in cast("Sequence[Mapping[str, CanonicalValue]]", payload["sessions"])
    ]
    sessions[0]["terminal_chain_digest"] = None
    payload["sessions"] = sessions

    report = _report(_signed(payload, keys), keys)
    assert not report.verified
    assert SIGNATURE_INVALID not in report.failed_check_names
    assert CHAIN_TIP_MISMATCH in report.failed_check_names
    assert exit_code(report) != 0
    assert report.chains[0].rows == 0, (
        "the chain was still re-derived, so the absent tip narrowed the finding "
        "rather than stopping the verification"
    )
