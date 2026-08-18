"""Property 12: signature verification detects any alteration of the payload.

The claim is asserted through the whole verifier rather than through a digest
comparison, so what the property establishes is the reported outcome a reviewer
actually acts on: the failed-check list names `signature_invalid` and the process
status is non-zero. The cluster is emulated, because the claim is about the
document and the key rather than about any particular cluster state, and the
emulated state is the state a completed erasure leaves, so an unaltered
certificate verifies in full and every alteration is the only difference.

The signer is a local key pair generated in this process. Nothing reaches a key
service, and the public half is handed to the verifier the same way a retrieved
one would be, which is the point of verifying locally in the first place.

**Two of the five alteration levels need saying precisely, because the canonical
rules make the naive reading of them vacuous.**

*Array order.* The canonical serialiser orders every declared collection by its
own content, so permuting whole elements of `dispositions` yields byte-identical
output by construction. That is Property 11's guarantee and it is a feature: a
certificate assembled in a different insertion order is the same certificate. The
alteration asserted here is therefore a reordering of the identifiers *within* the
collection, which changes what each element says and is a content change rather
than a presentation one.

*Whitespace.* The canonical form carries no insignificant whitespace, so
reformatting the stored document changes nothing signed. Whitespace inside a signed
text value is content, and that is the whitespace altered here.

Both readings are the ones under which the property is meaningful: an alteration
of content is detected, and a difference that is not an alteration of content is
not an alteration at all.

**Validates: Requirements 22.2, 22.3**
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, TypeVar, cast
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from hypothesis import given, settings
from hypothesis import strategies as st

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
    SIGNATURE_INVALID,
    SUPPORTED_ALGORITHM,
    Envelope,
    exit_code,
    verify_certificate,
)
from molt.errors import StoreError
from molt.store import MemoryStore
from molt.store.chain import CHAIN_ROWS_QUERY, GENESIS_PREDECESSOR

# The shape of a generated certificate. Small on purpose: the property is over the
# alteration rather than over the size, and every level of the document is reached
# by two elements of each collection.
DISPOSITION_FLOOR: Final[int] = 2
DISPOSITION_CEILING: Final[int] = 6
SESSION_FLOOR: Final[int] = 1
SESSION_CEILING: Final[int] = 4

# The role the emulated cluster reports the connection is authenticated as.
READER_ROLE: Final[str] = "molt_reader"

# The key identifier the generated envelope names. It reaches no service.
STUB_KEY_ID: Final[str] = "stub-certificate-key"

# The dispositions a generated certificate records, in the schema's spellings.
HARD_DELETE: Final[str] = "hard_delete"
SURGICAL_REDACTION: Final[str] = "surgical_redaction"
RETAINED: Final[str] = "retained"

# The two template names the payload contract fixes.
ATTRIBUTION_QUERY_NAME: Final[str] = "no_current_attribution_remains"
SESSIONS_QUERY_NAME: Final[str] = "no_sessions_remain"

T = TypeVar("T")

# One local key pair for the whole module. Generating a pair per example would
# measure key generation rather than the property.
_PRIVATE_KEY: Final[ec.EllipticCurvePrivateKey] = ec.generate_private_key(ec.SECP256R1())


class Alteration(StrEnum):
    """The five levels of the payload an alteration is made at."""

    NONE = "none"
    SCALAR = "scalar"
    ARRAY_ORDER = "array_order"
    ADDED_FIELD = "added_field"
    REMOVED_FIELD = "removed_field"
    WHITESPACE = "whitespace"


# ---------------------------------------------------------------------------
# The stub signer and the public-key source
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StubSigner:
    """A local asymmetric signer standing in for the key service.

    The digest is signed rather than the payload, which is the shape the signing
    flow uses so that the document never leaves the process, and it is the shape
    the verifier checks against.
    """

    key: ec.EllipticCurvePrivateKey = _PRIVATE_KEY

    def sign_digest(self, digest: bytes) -> bytes:
        """Sign an already-computed digest with the local private half."""
        return self.key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    def public_key(self, *, key_id: str) -> bytes:
        """The public half in the encoded form a retrieval would return."""
        del key_id
        return self.key.public_key().public_bytes(
            encoding=Encoding.DER,
            format=PublicFormat.SubjectPublicKeyInfo,
        )


# ---------------------------------------------------------------------------
# The emulated cluster
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cluster:
    """The state a completed erasure leaves, as the emulation answers it."""

    disposed_before: int
    live_digests: Mapping[str, str]


class FakeCursor:
    """A cursor answering the statements the verifier sends, and nothing else.

    Dispatch is on statement identity rather than on parsed SQL, so a statement
    this emulation does not model is a loud failure rather than a silent empty
    answer.
    """

    def __init__(self, cluster: Cluster) -> None:
        self._cluster = cluster
        self._rows: list[tuple[object, ...]] = []

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Answer one statement, holding its rows for the fetch that follows."""
        bound = tuple(params or ())
        if query == CURRENT_ROLE_QUERY:
            self._rows = [(READER_ROLE,)]
        elif query in (CURRENT_BINDINGS_QUERY, LIVE_SESSIONS_QUERY, EXISTING_ARTIFACTS_QUERY):
            self._rows = []
        elif query == LIVE_COUNT_QUERY:
            self._rows = [(0,)]
        elif query == DISPOSED_BEFORE_QUERY:
            self._rows = [(self._cluster.disposed_before,)]
        elif query == CURRENT_DIGESTS_QUERY:
            self._rows = self._digests(bound)
        elif query == CHAIN_ROWS_QUERY:
            self._rows = []
        else:
            raise AssertionError("the emulation was sent a statement it does not answer")
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """The first row of the last statement, or None when it produced none."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row of the last statement."""
        return list(self._rows)

    def _digests(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        """The present content digest of each identifier the bound array names."""
        asked = bound[0]
        assert isinstance(asked, list)
        rows: list[tuple[object, ...]] = []
        for identifier in asked:
            digest = self._cluster.live_digests.get(str(identifier))
            if digest is not None:
                rows.append((str(identifier), digest))
        return rows


class FakeStore:
    """The store surface the verifier reaches the emulated cluster through."""

    def __init__(self, cluster: Cluster) -> None:
        self._cluster = cluster

    @property
    def role(self) -> str:
        """The configured role label, which the verifier checks before reading."""
        return READER_ROLE

    def read(self, body: Callable[[FakeCursor], T]) -> T:
        """Run a read body against one cursor over the emulated tables."""
        return body(FakeCursor(self._cluster))

    def gc_horizon(self) -> object:
        """Refuse: this cluster's horizon is unprobed, so no corroboration is attempted.

        An unattempted corroboration is a note rather than a failed check, which is
        what keeps a certificate verifiable after its timestamps age out.
        """
        raise StoreError("the garbage-collection horizon is unprobed on this cluster")


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signed:
    """One signed certificate, the cluster it verifies against, and an alteration."""

    payload: Mapping[str, CanonicalValue]
    signature: bytes
    digest: str
    cluster: Cluster
    alteration: Alteration


def _moment(offset: int) -> str:
    """A fixed-form instant, rendered as the canonical rules require."""
    base = datetime(2001, 1, 1, tzinfo=UTC) + timedelta(seconds=offset)
    return base.isoformat(timespec="microseconds")


def _digest_text(seed: str) -> str:
    """A hexadecimal digest of the fixed width the schema holds."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@st.composite
def signed_certificates(draw: st.DrawFn) -> Signed:
    """A signed certificate of the documented shape, plus the alteration to apply.

    Every collection of the payload carries at least two elements where the shape
    admits it, so an alteration at any level has somewhere to land. The emulated
    cluster is derived from the payload, so the unaltered certificate verifies in
    full and the alteration is the only difference between the two runs.
    """
    slug = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12))
    justification = draw(st.text(alphabet="abcdefghij ", min_size=1, max_size=40))
    disposition_count = draw(
        st.integers(min_value=DISPOSITION_FLOOR, max_value=DISPOSITION_CEILING)
    )
    session_count = draw(st.integers(min_value=SESSION_FLOOR, max_value=SESSION_CEILING))
    decisions = draw(
        st.lists(
            st.sampled_from((HARD_DELETE, SURGICAL_REDACTION, RETAINED)),
            min_size=disposition_count,
            max_size=disposition_count,
        )
    )
    alteration = draw(st.sampled_from(tuple(Alteration)))

    client_id = uuid4()
    run_id = uuid4()
    dispositions: list[Mapping[str, CanonicalValue]] = []
    live_digests: dict[str, str] = {}
    for index, decision in enumerate(decisions):
        artifact_id = uuid4()
        post = _digest_text(f"post-{index}") if decision == SURGICAL_REDACTION else None
        if post is not None:
            live_digests[str(artifact_id)] = post
        dispositions.append(
            {
                "artifact_id": str(artifact_id),
                "artifact_kind": "derived_artifact",
                "disposition": decision,
                "reason": "client_binding_removed",
                "selection_reason": "client_binding",
                "pre_digest": _digest_text(f"pre-{index}"),
                "post_digest": post,
                "bindings_before": [slug],
                "bindings_after": [],
                "first_attributed_at": _moment(index),
                "first_attribution_method": "marker",
            }
        )

    sessions: list[Mapping[str, CanonicalValue]] = [
        {
            "session_id": str(uuid4()),
            "terminal_chain_digest": GENESIS_PREDECESSOR,
            "terminal_seq": "0",
            "row_count": "0",
        }
        for _ in range(session_count)
    ]

    payload: dict[str, CanonicalValue] = {
        "certificate_version": "1",
        "erasure_request": {
            "request_id": str(uuid4()),
            "requester": "governance-owner-principal",
            "justification": justification,
            "submitted_at": _moment(0),
        },
        "client": {"client_id": str(client_id), "slug": slug},
        "run": {
            "run_id": str(run_id),
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
            "artifacts_bound_before": str(len(dispositions)),
            "artifacts_bound_after": "0",
            "count_derivation": "ledger_and_dispositions",
            "historical_corroboration": {
                "attempted": False,
                "within_horizon": False,
                "agrees": None,
                "gc_horizon_seconds": "4500",
            },
            "hard_delete": str(decisions.count(HARD_DELETE)),
            "surgical_redaction": str(decisions.count(SURGICAL_REDACTION)),
            "retained": str(decisions.count(RETAINED)),
        },
        "dispositions": dispositions,
        "lineage_subgraph": [],
        "residue_candidates": [],
        "sessions": sessions,
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

    digest = hashlib.sha256(canonicalise(payload, array_rules=CERTIFICATE_ARRAY_RULES)).hexdigest()
    return Signed(
        payload=payload,
        signature=StubSigner().sign_digest(bytes.fromhex(digest)),
        digest=digest,
        cluster=Cluster(disposed_before=len(dispositions), live_digests=live_digests),
        alteration=alteration,
    )


def _altered(payload: Mapping[str, CanonicalValue], alteration: Alteration) -> CanonicalValue:
    """Return the payload with one alteration applied at the named level."""
    changed = copy.deepcopy(dict(payload))
    counts = cast("dict[str, CanonicalValue]", changed["counts"])
    run = cast("dict[str, CanonicalValue]", changed["run"])
    request = cast("dict[str, CanonicalValue]", changed["erasure_request"])
    dispositions = cast("list[dict[str, CanonicalValue]]", changed["dispositions"])
    if alteration is Alteration.SCALAR:
        recorded = cast("str", counts["artifacts_bound_before"])
        counts["artifacts_bound_before"] = str(int(recorded, 10) + 1)
    elif alteration is Alteration.ARRAY_ORDER:
        # A permutation of whole elements is byte-identical by construction, so what
        # is reordered is the identifiers the elements carry: each element now says
        # something about a different artifact, which is a change of content.
        identifiers = [entry["artifact_id"] for entry in dispositions]
        for entry, identifier in zip(dispositions, reversed(identifiers), strict=True):
            entry["artifact_id"] = identifier
    elif alteration is Alteration.ADDED_FIELD:
        run["added_field"] = "1"
    elif alteration is Alteration.REMOVED_FIELD:
        del run["dry_run"]
    elif alteration is Alteration.WHITESPACE:
        request["justification"] = f"{cast('str', request['justification'])} "
    return changed


# Feature: molt, Property 12: *For any* signed certificate and any single-byte
# mutation of its payload, verification succeeds on the unmutated certificate and
# reports `signature_invalid` on every mutated certificate.
# Validates: Requirements 22.2, 22.3
# No per-example deadline, as everywhere else in this suite: a wall-clock deadline
# fails an example for the load on the machine rather than for the property, which
# under parallel execution reports contention as a correctness failure. Latency
# bounds are stated deliberately in the performance suite.
@settings(max_examples=100, deadline=None)
@given(signed_certificates())
def test_signature_verification_detects_any_alteration(state: Signed) -> None:
    store = cast("MemoryStore", FakeStore(state.cluster))
    keys = StubSigner()

    def envelope_over(payload: CanonicalValue) -> Envelope:
        return Envelope(
            payload=cast("Mapping[str, CanonicalValue]", payload),
            algorithm=SUPPORTED_ALGORITHM,
            kms_key_id=STUB_KEY_ID,
            payload_digest=state.digest,
            signature=state.signature,
        )

    # The unaltered certificate verifies in full: the recomputed digest is the
    # signed one, the signature stands against the retrieved public half, and every
    # live check agrees with what the document claims.
    intact = verify_certificate(envelope_over(state.payload), store=store, keys=keys)
    assert intact.verified
    assert intact.failed_checks == ()
    assert exit_code(intact) == 0
    assert [outcome.row_count for outcome in intact.queries] == [0, 0]

    if state.alteration is Alteration.NONE:
        # The unaltered case is drawn as often as any other, so the generator
        # exercises the positive half of the claim rather than assuming it.
        return

    altered = _altered(state.payload, state.alteration)
    assert altered != state.payload

    report = verify_certificate(envelope_over(altered), store=store, keys=keys)
    assert not report.verified
    assert SIGNATURE_INVALID in report.failed_check_names
    assert exit_code(report) != 0

    # The alteration is detected at the digest rather than only at the signature,
    # which is the check that needs no key at all.
    recomputed = hashlib.sha256(
        canonicalise(
            cast("Mapping[str, CanonicalValue]", altered), array_rules=CERTIFICATE_ARRAY_RULES
        )
    ).hexdigest()
    assert recomputed != state.digest


# A guard on the generator: a draw produces a document whose identifiers are
# identifiers and whose digest is over its own canonical bytes. Without it, a
# generator that produced one fixed shape would make the property above pass while
# asserting almost nothing.
# No per-example deadline, as everywhere else in this suite: a wall-clock deadline
# fails an example for the load on the machine rather than for the property, which
# under parallel execution reports contention as a correctness failure. Latency
# bounds are stated deliberately in the performance suite.
@settings(max_examples=20, deadline=None)
@given(signed_certificates())
def test_the_generator_produces_a_document_of_the_signed_contract(state: Signed) -> None:
    client = cast("Mapping[str, object]", state.payload["client"])
    assert UUID(cast("str", client["client_id"]))
    assert (
        state.digest
        == hashlib.sha256(
            canonicalise(state.payload, array_rules=CERTIFICATE_ARRAY_RULES)
        ).hexdigest()
    )
