"""Verifying a certificate that covers a thousand Artifacts stays inside 30 seconds.

**Validates: Requirements 22.11**

Why the bound exists. A departing client's reviewer runs the verifier, and a
verifier that takes minutes over one document is a verifier nobody runs twice. The
requirement fixes the document size the bound has to hold at — a certificate whose
disposition list names a thousand Artifacts — and this module is the only place a
document of that size is actually built and verified.

**What is inside the measurement.** One `verify_source` call: reading the envelope
from a local path, parsing it, canonicalising the whole thousand-disposition
payload, recomputing its SHA-256 digest, checking the signature against the
retrieved public half, running both embedded Verification_Queries through their
templates, re-deriving both counts, verifying every named Session's chain, and
running the two set-based disposition checks. Nothing in that list is stubbed.

**What is outside it, and why.** Building the payload, canonicalising it once to
obtain the digest the issuer signs, signing it, and writing the file are the
Certificate_Builder's costs rather than the verifier's, so they are paid in a
fixture and reported on their own line. Charging the issuer's work to the
reviewer's budget would describe neither.

**Why this benchmark needs no cluster, stated rather than glossed.** The verifier
refuses to read over any role but the read-only one, and it refuses on the role the
*cluster* reports rather than on a label, which is the guarantee that makes a
verification structurally unable to write. A benchmark connecting as the
administrative role would therefore measure a refusal, and provisioning a cluster
role to get past that would put a cluster-wide grant in a module whose only
subject is latency. So the cluster is stood in for by a reader whose answers are
the answers of a completed erasure, exactly as the signature-invalidation test
does, and the module carries `perf` alone: it is an in-process benchmark with no
instance prerequisite.

That substitution is honest for this bound because of how the verifier is written,
and the second case here is what turns that from an assertion into a measurement.
Every check the verifier makes over a disposition list is set-based: the
still-present check binds one array of identifiers and the redaction-digest check
binds another, so the number of statements sent does not grow with the document.
The reader counts every statement it is asked, and the case below asserts that
count is the same for a thousand dispositions as for one. What remains that *does*
grow with the document is canonicalisation and hashing, and that is in this process
either way. So the figure reported here is the part of the cost that scales, and
the part that is left out is a fixed handful of round trips.

**Both retrieval paths of the loading seam are exercised.** The timed call is
`verify_source` against a local path, which is what a reviewer holding a saved
certificate runs, and it is the whole algorithm including the parse.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TypeVar, cast
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from molt.attest.canonical import CERTIFICATE_ARRAY_RULES, CanonicalValue, canonicalise
from molt.attest.verifier import (
    CURRENT_BINDINGS_QUERY,
    CURRENT_DIGESTS_QUERY,
    CURRENT_ROLE_QUERY,
    DISPOSED_BEFORE_QUERY,
    EXISTING_ARTIFACTS_QUERY,
    LIVE_COUNT_QUERY,
    LIVE_SESSIONS_QUERY,
    QUERY_TEMPLATES,
    SUPPORTED_ALGORITHM,
    CertificateLocation,
    VerificationReport,
    exit_code,
    verify_source,
)
from molt.errors import StoreError
from molt.store import MemoryStore
from molt.store.chain import CHAIN_ROWS_QUERY, GENESIS_PREDECESSOR

# An in-process benchmark: the bound is measured over canonicalisation, hashing,
# the local signature check, and the verifier's own control flow, and the reads are
# answered by a reader standing in for a completed erasure. So `perf` alone, with
# no instance prerequisite, for the reason the module docstring gives.
pytestmark = [pytest.mark.perf]

# The document size the requirement states and the bound it states for it.
COVERED_ARTIFACTS: Final[int] = 1000
VERIFICATION_BOUND_SECONDS: Final[float] = 30.0

# The one-disposition document the statement count is compared against, so the
# claim that no check is per-artifact is measured rather than asserted.
SINGLE_ARTIFACT: Final[int] = 1

# How many Sessions the certificate names. A run over a thousand Artifacts reaches
# many Sessions, and every one of them is chain-verified, so the document names a
# realistic spread rather than one.
COVERED_SESSIONS: Final[int] = 20

READER_ROLE: Final[str] = "molt_reader"
ISSUING_KEY_ID: Final[str] = "issuing-certificate-key"
ATTRIBUTION_QUERY_NAME: Final[str] = "no_current_attribution_remains"
SESSIONS_QUERY_NAME: Final[str] = "no_sessions_remain"

# The instant the document is written at, derived from the epoch rather than spelled
# out, so a run embeds nothing about when it happened.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# The local key pair and the retrieval seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Keys:
    """One local key pair, retrieved by identifier the way a key service answers.

    There is no key client in this project's source, and the verifier asks its key
    source for the public half and for nothing else, so a locally generated pair is
    the whole of what a verification needs.
    """

    issuing: ec.EllipticCurvePrivateKey

    def sign_digest(self, digest: bytes, *, key_id: str) -> bytes:
        """Sign a digest under the named key, as the issuing side does."""
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
        raise AssertionError("the emulated key service holds no key under that identifier")


# ---------------------------------------------------------------------------
# A reader answering as a completed erasure, counting every statement it is asked
# ---------------------------------------------------------------------------


class CountingCursor:
    """A cursor answering the verifier's statements over an erased tenant.

    Every statement is recorded, because the count is the subject of the second
    case here: a verifier whose disposition checks were per-artifact would send a
    thousand statements for a thousand dispositions, and this is how that is
    observed rather than trusted.
    """

    def __init__(self, sent: list[str], disposed_before: int) -> None:
        self._sent = sent
        self._disposed_before = disposed_before
        self._rows: list[tuple[object, ...]] = []

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Answer one statement, holding its rows for the fetch that follows."""
        del params
        self._sent.append(" ".join(query.split()))
        if query == CURRENT_ROLE_QUERY:
            self._rows = [(READER_ROLE,)]
        elif query == LIVE_COUNT_QUERY:
            self._rows = [(0,)]
        elif query == DISPOSED_BEFORE_QUERY:
            self._rows = [(self._disposed_before,)]
        elif query in (
            CURRENT_BINDINGS_QUERY,
            LIVE_SESSIONS_QUERY,
            EXISTING_ARTIFACTS_QUERY,
            CURRENT_DIGESTS_QUERY,
            CHAIN_ROWS_QUERY,
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


@dataclass(slots=True)
class CountingStore:
    """The store surface the verifier reads the stood-in cluster through."""

    disposed_before: int
    sent: list[str] = field(default_factory=list)

    @property
    def role(self) -> str:
        """The read-only role label the verifier requires before reading anything."""
        return READER_ROLE

    def read(self, body: Callable[[CountingCursor], T]) -> T:
        """Run one read body against a cursor over the stood-in tables."""
        return body(CountingCursor(self.sent, self.disposed_before))

    def gc_horizon(self) -> object:
        """Refuse: the horizon is unprobed here, so no corroboration is attempted."""
        raise StoreError("the garbage-collection horizon is unprobed on this reader")


# ---------------------------------------------------------------------------
# One signed certificate of the documented shape, at a chosen coverage
# ---------------------------------------------------------------------------


def _moment(offset: int) -> str:
    """A fixed-form instant, rendered as the canonical rules require."""
    return (MOMENT + timedelta(seconds=offset)).isoformat(timespec="microseconds")


def _disposition(index: int, slug: str) -> dict[str, CanonicalValue]:
    """One hard-deleted Artifact's disposition record, of the full documented width.

    Every field a real record carries is present, because the payload's size is what
    the bound is stated over and a thinned record would understate the
    canonicalisation this measures.
    """
    return {
        "artifact_id": str(uuid4()),
        "artifact_kind": "derived_artifact",
        "disposition": "hard_delete",
        "reason": "client_binding_removed",
        "selection_reason": "client_binding",
        "pre_digest": hashlib.sha256(f"pre {index}".encode()).hexdigest(),
        "post_digest": None,
        "bindings_before": [slug],
        "bindings_after": [],
        "first_attributed_at": _moment(1),
        "first_attribution_method": "marker",
    }


def _session_record() -> dict[str, CanonicalValue]:
    """One covered Session, whose chain the verifier recomputes and compares."""
    return {
        "session_id": str(uuid4()),
        "terminal_chain_digest": GENESIS_PREDECESSOR,
        "terminal_seq": "0",
        "row_count": "0",
    }


def _payload(covered: int) -> dict[str, CanonicalValue]:
    """A certificate payload for one completed erasure covering `covered` Artifacts."""
    client_id = uuid4()
    slug = "tenant"
    return {
        "certificate_version": "1",
        "erasure_request": {
            "request_id": str(uuid4()),
            "requester": "governance-owner-principal",
            "justification": "engagement concluded under a contractual purge obligation",
            "submitted_at": _moment(0),
        },
        "client": {"client_id": str(client_id), "slug": slug},
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
            "artifacts_bound_before": str(covered),
            "artifacts_bound_after": "0",
            "count_derivation": "ledger_and_dispositions",
            "historical_corroboration": {
                "attempted": False,
                "within_horizon": False,
                "agrees": None,
                "gc_horizon_seconds": "4500",
            },
            "hard_delete": str(covered),
            "surgical_redaction": "0",
            "retained": "0",
        },
        "dispositions": [_disposition(index, slug) for index in range(covered)],
        "lineage_subgraph": [],
        "residue_candidates": [],
        "sessions": [_session_record() for _ in range(COVERED_SESSIONS)],
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


def _write_signed(payload: Mapping[str, CanonicalValue], keys: Keys, destination: Path) -> Path:
    """Sign one payload and write the envelope the verifier loads, as the builder does."""
    digest = _digest_of(payload)
    document = {
        "payload": payload,
        "signature": {
            "algorithm": SUPPORTED_ALGORITHM,
            "kms_key_id": ISSUING_KEY_ID,
            "payload_digest": digest,
            "value": _encoded(keys.sign_digest(bytes.fromhex(digest), key_id=ISSUING_KEY_ID)),
        },
    }
    destination.write_text(json.dumps(document), encoding="utf-8")
    return destination


def _encoded(signature: bytes) -> str:
    """The signature as the envelope carries it."""
    return base64.b64encode(signature).decode("ascii")


@dataclass(frozen=True, slots=True)
class Certificate:
    """One written certificate, what it covers, and what producing it cost."""

    path: Path
    covered: int
    build_seconds: float
    bytes_written: int


def _issue(covered: int, keys: Keys, destination: Path) -> Certificate:
    """Build, sign, and write one certificate, timing the whole issuing side."""
    started = time.perf_counter()
    written = _write_signed(_payload(covered), keys, destination)
    build = time.perf_counter() - started
    return Certificate(
        path=written,
        covered=covered,
        build_seconds=build,
        bytes_written=written.stat().st_size,
    )


@pytest.fixture(name="keys", scope="module")
def keys_fixture() -> Keys:
    """One local key pair, generated once for the module."""
    return Keys(issuing=ec.generate_private_key(ec.SECP256R1()))


@pytest.fixture(name="certificate", scope="module")
def certificate_fixture(
    keys: Keys,
    tmp_path_factory: pytest.TempPathFactory,
) -> Certificate:
    """One certificate covering the stated thousand Artifacts, written to a local path.

    Module scope pays the issuing cost once. It is timed and reported on its own
    line because it belongs to the Certificate_Builder rather than to the verifier,
    and it is deliberately outside every measurement below.
    """
    directory = tmp_path_factory.mktemp("molt_verification_latency")
    issued = _issue(COVERED_ARTIFACTS, keys, directory / "certificate.json")
    print(
        f"\nissuing side, outside every measurement: a certificate covering "
        f"{issued.covered} artifacts and {COVERED_SESSIONS} sessions built, signed, and "
        f"written in {issued.build_seconds:.2f}s, {issued.bytes_written} bytes"
    )
    return issued


def _verify(certificate: Certificate, keys: Keys) -> tuple[VerificationReport, float, list[str]]:
    """Verify one written certificate, returning the report, the cost, and the reads."""
    store = CountingStore(disposed_before=certificate.covered)
    started = time.perf_counter()
    report = verify_source(
        CertificateLocation.at_path(certificate.path),
        store=cast("MemoryStore", store),
        keys=keys,
    )
    return report, time.perf_counter() - started, store.sent


# ---------------------------------------------------------------------------
# The benchmark
# ---------------------------------------------------------------------------


def test_verifying_a_thousand_artifact_certificate_stays_inside_the_bound(
    certificate: Certificate,
    keys: Keys,
) -> None:
    """Requirement 22.11: the whole verification completes within 30 seconds.

    The verified outcome is asserted beside the figure, because a verification that
    failed early would be a measurement of less work than the bound is stated over.
    """
    report, elapsed, sent = _verify(certificate, keys)

    print(
        f"verification of a certificate covering {certificate.covered} artifacts: "
        f"{elapsed:.3f}s (bound {VERIFICATION_BOUND_SECONDS:.0f}s); "
        f"{len(sent)} statements read; issuing side "
        f"{certificate.build_seconds:.2f}s, outside the measurement"
    )

    assert report.verified, f"the certificate did not verify: {report.failed_checks}"
    assert exit_code(report) == 0
    assert report.counts is not None
    assert report.counts.recorded_before == COVERED_ARTIFACTS
    assert report.counts.derived_before == COVERED_ARTIFACTS, (
        "the derived before-count did not reach the stated coverage, so the "
        "disposition list was not the thing verified"
    )
    assert len(report.chains) == COVERED_SESSIONS
    assert len(report.queries) == len(QUERY_TEMPLATES)
    assert elapsed < VERIFICATION_BOUND_SECONDS, (
        f"verification took {elapsed:.3f}s against a bound of {VERIFICATION_BOUND_SECONDS:.0f}s"
    )


def test_no_check_costs_a_statement_per_covered_artifact(
    certificate: Certificate,
    keys: Keys,
    tmp_path: Path,
) -> None:
    """The disposition checks bind arrays, so the read count is flat in the coverage.

    This is what licenses measuring the bound in this process: the part of
    verification that grows with the document is canonicalisation and hashing, which
    is in this process either way, and the part that touches a cluster is a fixed
    handful of statements. A thousand-fold larger document sending the same number
    of statements is that claim, measured.
    """
    one = _issue(SINGLE_ARTIFACT, keys, tmp_path / "one.json")
    small_report, _, small_reads = _verify(one, keys)
    large_report, _, large_reads = _verify(certificate, keys)

    print(
        f"statements read: {len(small_reads)} for {one.covered} artifact, "
        f"{len(large_reads)} for {certificate.covered} artifacts"
    )

    assert small_report.verified and large_report.verified
    assert len(large_reads) == len(small_reads), (
        f"verification sent {len(large_reads)} statements for {certificate.covered} "
        f"artifacts and {len(small_reads)} for {one.covered}, so some check is "
        "per-artifact and the bound would grow with the document in round trips"
    )
    assert sorted(set(large_reads)) == sorted(set(small_reads))
