"""Property 11: the certificate's canonical bytes are a function of content alone.

Three claims are exercised over generated certificates, and each one is what makes
independent verification possible rather than nominal.

**Order does not reach the bytes.** A payload's keys are re-inserted in a shuffled
order and each of its sorted collections is shuffled, and the canonical bytes are
compared against the original. If insertion order could move a byte, a verifier
rebuilding the payload from stored rows would compute a different digest and every
signature would appear invalid.

**The parse is equivalent.** Reading the canonical bytes back yields the same
logical content, and re-serialising the parsed form yields the same bytes, so the
document a reviewer parses and the bytes a signature commits to are the same
statement.

**Every contract key is present, including the three the ownership, attribution,
and checkpoint blocks contribute, and the derived counts match a count computed
independently from the generating graph.** The generator builds a small memory
graph of artifacts with their prior and surviving bindings; the expected
before-count and after-count are computed from that graph directly, and the payload
must agree with `count_derivation` reading the derived mechanism.

**Validates: Requirements 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.8, 21.9, 21.10,
21.11, 21.12, 20.2, 20.3, 20.4**
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.attest.builder import (
    CAVEATS,
    CERTIFICATE_VERSION,
    COUNT_DERIVATION,
    VERIFICATION_TEMPLATES,
    AuditWindow,
    BackupFacts,
    CertificatePolicy,
    CheckpointFacts,
    CorroborationFacts,
    CountFacts,
    DispositionRecord,
    Evidence,
    LineageEdgeRecord,
    OwnershipFacts,
    RequestFacts,
    ResidueRecord,
    RunFacts,
    SessionTip,
    certificate_payload,
    derived_counts,
    envelope_of,
    sign,
)
from molt.attest.canonical import CERTIFICATE_ARRAY_RULES, canonicalise

MAX_EXAMPLES: Final[int] = 100

# The keys the contract requires at the top level of a payload. The ownership,
# attribution, and checkpoint additions are the last three of the list, and the
# attribution pair lives inside each disposition entry rather than at the root.
CONTRACT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "certificate_version",
        "erasure_request",
        "client",
        "run",
        "backup",
        "counts",
        "dispositions",
        "lineage_subgraph",
        "residue_candidates",
        "sessions",
        "verification_queries",
        "cluster_audit_log",
        "caveats",
        "ownership",
        "ledger_checkpoint",
    }
)

# The key set of each nested block, so a missing optional field is a failure rather
# than a payload whose shape varies with its content.
BLOCK_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "erasure_request": frozenset({"request_id", "requester", "justification", "submitted_at"}),
    "client": frozenset({"client_id", "slug"}),
    "run": frozenset(
        {
            "run_id",
            "dry_run",
            "t_before",
            "t_after",
            "auto_include_threshold",
            "review_threshold",
            "unembedded_artifact_count",
            "working_rows_deleted",
        }
    ),
    "ownership": frozenset({"owner", "fencing_generation", "idempotency_key"}),
    "backup": frozenset(
        {"present", "backup_path", "taken", "referenced", "backup_id", "target_uri", "statement"}
    ),
    "counts": frozenset(
        {
            "artifacts_bound_before",
            "artifacts_bound_after",
            "count_derivation",
            "historical_corroboration",
            "hard_delete",
            "surgical_redaction",
            "retained",
        }
    ),
    "cluster_audit_log": frozenset({"window_start", "window_end", "records"}),
    "caveats": frozenset(CAVEATS),
}

CHECKPOINT_KEYS: Final[frozenset[str]] = frozenset(
    {"checkpoint_id", "window_start", "window_end", "covered_session_count", "root_digest"}
)
DISPOSITION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "artifact_id",
        "artifact_kind",
        "disposition",
        "reason",
        "selection_reason",
        "pre_digest",
        "post_digest",
        "bindings_before",
        "bindings_after",
        "first_attributed_at",
        "first_attribution_method",
    }
)
RESIDUE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "artifact_id",
        "artifact_kind",
        "cosine_distance",
        "band",
        "included",
        "decision_reason",
        "adjudicated",
        "model_id",
        "reasoning",
    }
)
SESSION_KEYS: Final[frozenset[str]] = frozenset(
    {"session_id", "terminal_chain_digest", "terminal_seq", "row_count"}
)
CORROBORATION_KEYS: Final[frozenset[str]] = frozenset(
    {"attempted", "within_horizon", "agrees", "gc_horizon_seconds"}
)

# The collections whose order the canonical rules derive from content, so shuffling
# any of them must leave the bytes untouched.
SORTED_COLLECTIONS: Final[tuple[str, ...]] = (
    "dispositions",
    "residue_candidates",
    "lineage_subgraph",
    "sessions",
    "verification_queries",
)

DISPOSITIONS: Final[tuple[str, ...]] = ("hard_delete", "surgical_redaction", "retained")
KINDS: Final[tuple[str, ...]] = ("event", "session", "derived_artifact")
METHODS: Final[tuple[str, ...]] = ("scope", "inherited", "marker", "residue")
BANDS: Final[tuple[str, ...]] = ("auto_include", "review")
ERASED_SLUG: Final[str] = "erased-tenant"
OTHER_SLUGS: Final[tuple[str, ...]] = ("retained-one", "retained-two")
DIGEST_WIDTH: Final[int] = 64

# The signing surface the envelope claim is exercised through. Nothing here holds a
# credential and nothing leaves the process.
STUB_KEY_ID: Final[str] = "stub-certificate-key"
STUB_ALGORITHM: Final[str] = "ECDSA_SHA_256"


@dataclass(frozen=True, slots=True)
class StubSigner:
    """A deterministic asymmetric signer standing in for the key service."""

    secret: bytes = b"stub-private-half"

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        """Produce the stand-in signature over a digest."""
        return hashlib.sha256(
            self.secret + key_id.encode("utf-8") + algorithm.encode("utf-8") + digest
        ).digest()

    def public_key(self, *, key_id: str) -> bytes:
        """The stand-in public half, which a verifier is handed rather than trusts."""
        return hashlib.sha256(self.secret + key_id.encode("utf-8")).digest()

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
        if public_key != self.public_key(key_id=key_id):
            return False
        return signature == self.sign_digest(digest, key_id=key_id, algorithm=algorithm)


@dataclass(frozen=True, slots=True)
class GraphArtifact:
    """One artifact of the generating memory graph, before and after the run."""

    artifact_id: UUID
    kind: str
    disposition: str
    bindings_before: tuple[str, ...]
    bindings_after: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Generated:
    """One generated certificate: the graph it came from and the evidence built."""

    graph: tuple[GraphArtifact, ...]
    live_bound: int
    evidence: Evidence


def policy() -> CertificatePolicy:
    """The policy every example signs under, naming no real key and no real bucket."""
    return CertificatePolicy(
        kms_key_id=STUB_KEY_ID,
        signing_algorithm=STUB_ALGORITHM,
        bucket="stub-certificate-bucket",
        prefix="certificates/",
        object_lock_days=1,
    )


@st.composite
def digests(draw: st.DrawFn) -> str:
    """A hexadecimal digest of the width the schema holds."""
    return draw(st.text(alphabet="0123456789abcdef", min_size=DIGEST_WIDTH, max_size=DIGEST_WIDTH))


@st.composite
def graphs(draw: st.DrawFn) -> tuple[GraphArtifact, ...]:
    """A small memory graph whose artifacts carry prior and surviving bindings."""
    count = draw(st.integers(min_value=0, max_value=6))
    identifiers = draw(st.lists(st.uuids(version=4), min_size=count, max_size=count, unique=True))
    artifacts: list[GraphArtifact] = []
    for identifier in identifiers:
        disposition = draw(st.sampled_from(DISPOSITIONS))
        held = draw(st.booleans())
        others = draw(st.lists(st.sampled_from(OTHER_SLUGS), max_size=2, unique=True))
        before = (*others, ERASED_SLUG) if held else tuple(others)
        artifacts.append(
            GraphArtifact(
                artifact_id=identifier,
                kind=draw(st.sampled_from(KINDS)),
                disposition=disposition,
                bindings_before=before,
                bindings_after=tuple(others),
            )
        )
    return tuple(artifacts)


@st.composite
def certificate_payloads(draw: st.DrawFn) -> Generated:
    """Build one evidence set, and the graph whose counts it must agree with."""
    graph = draw(graphs())
    live_bound = draw(st.integers(min_value=0, max_value=4))
    opening = datetime.fromtimestamp(
        draw(st.integers(min_value=0, max_value=10**9)), tz=UTC
    ) + timedelta(microseconds=draw(st.integers(min_value=0, max_value=999999)))
    closing = opening + timedelta(seconds=draw(st.integers(min_value=1, max_value=600)))
    client_id = draw(st.uuids(version=4))
    auto = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
    review = draw(st.floats(min_value=auto, max_value=2.0, allow_nan=False, allow_infinity=False))

    dispositions = tuple(
        DispositionRecord(
            artifact_id=entry.artifact_id,
            artifact_kind=entry.kind,
            disposition=entry.disposition,
            reason=draw(st.sampled_from(("sole_binding", "blended_artifact_rewritten"))),
            selection_reason=draw(st.sampled_from(("client_binding", "session_scope"))),
            pre_digest=draw(digests()),
            post_digest=draw(st.one_of(st.none(), digests())),
            bindings_before=entry.bindings_before,
            bindings_after=entry.bindings_after,
            first_attributed_at=draw(st.one_of(st.none(), st.just(opening))),
            first_attribution_method=draw(st.one_of(st.none(), st.sampled_from(METHODS))),
        )
        for entry in graph
    )
    residue = tuple(
        ResidueRecord(
            artifact_id=entry.artifact_id,
            artifact_kind=entry.kind,
            cosine_distance=draw(
                st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False)
            ),
            band=draw(st.sampled_from(BANDS)),
            included=draw(st.booleans()),
            decision_reason=draw(st.sampled_from(("below_auto_include_threshold", "adjudicated"))),
            adjudicated=draw(st.booleans()),
            model_id=draw(st.one_of(st.none(), st.just("stub-adjudication-model"))),
            reasoning=draw(st.one_of(st.none(), st.just("the fragment names the tenant"))),
        )
        for entry in graph
        if draw(st.booleans())
    )
    lineage = tuple(
        LineageEdgeRecord(
            child_id=graph[index].artifact_id,
            parent_id=graph[index - 1].artifact_id,
            parent_kind=graph[index - 1].kind,
            derivation_method="distil_behavioral_baseline",
        )
        for index in range(1, len(graph))
    )
    sessions = tuple(
        SessionTip(
            session_id=draw(st.uuids(version=4)),
            terminal_chain_digest=draw(st.one_of(st.none(), digests())),
            terminal_seq=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=500))),
            row_count=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=500))),
        )
        for _ in range(draw(st.integers(min_value=0, max_value=3)))
    )
    attempted = draw(st.booleans())
    counts = derived_counts(dispositions, slug=ERASED_SLUG, live_bound=live_bound)
    evidence = Evidence(
        request=RequestFacts(
            request_id=draw(st.uuids(version=4)),
            requester="governance-owner-principal",
            justification=draw(st.text(min_size=1, max_size=40)),
            submitted_at=opening,
        ),
        client_id=client_id,
        client_slug=ERASED_SLUG,
        run=RunFacts(
            run_id=draw(st.uuids(version=4)),
            dry_run=draw(st.booleans()),
            t_before=opening,
            t_after=draw(st.one_of(st.none(), st.just(closing))),
            auto_include_threshold=auto,
            review_threshold=review,
            unembedded_artifact_count=draw(st.integers(min_value=0, max_value=50)),
            working_rows_deleted=draw(st.integers(min_value=0, max_value=50)),
        ),
        ownership=OwnershipFacts(
            owner=draw(st.one_of(st.none(), st.just("worker-owner-identifier"))),
            fencing_generation=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=9))),
            idempotency_key=draw(st.one_of(st.none(), st.just("an-idempotency-key"))),
        ),
        checkpoint=draw(
            st.one_of(
                st.none(),
                st.builds(
                    CheckpointFacts,
                    checkpoint_id=st.uuids(version=4),
                    window_start=st.just(opening - timedelta(seconds=3600)),
                    window_end=st.just(opening),
                    covered_session_count=st.integers(min_value=0, max_value=99),
                    root_digest=digests(),
                ),
            )
        ),
        backup=draw(
            st.one_of(
                st.none(),
                st.builds(
                    BackupFacts,
                    backup_id=st.one_of(st.none(), st.just("a-backup-identifier")),
                    backup_path=st.sampled_from(("self_managed", "managed_referenced")),
                    target_uri=st.one_of(st.none(), st.just("an-object-store-target")),
                    statement=st.just("the recorded backup command"),
                    taken=st.booleans(),
                    referenced=st.booleans(),
                ),
            )
        ),
        counts=CountFacts(
            counts=counts,
            hard_delete=sum(1 for entry in graph if entry.disposition == "hard_delete"),
            surgical_redaction=sum(
                1 for entry in graph if entry.disposition == "surgical_redaction"
            ),
            retained=sum(1 for entry in graph if entry.disposition == "retained"),
            corroboration=CorroborationFacts(
                attempted=attempted,
                within_horizon=attempted or draw(st.booleans()),
                agrees=draw(st.booleans()) if attempted else None,
                gc_horizon_seconds=draw(st.one_of(st.none(), st.just(4500))),
            ),
        ),
        dispositions=dispositions,
        lineage=lineage,
        residue=residue,
        sessions=sessions,
        audit=AuditWindow(window_start=opening, window_end=closing, records=()),
    )
    return Generated(graph=graph, live_bound=live_bound, evidence=evidence)


def shuffled_keys(payload: Mapping[str, Any], seed: int) -> dict[str, Any]:
    """Re-insert every key of a payload in a rotated order, recursively."""
    keys = sorted(payload)
    if keys:
        offset = seed % len(keys)
        keys = keys[offset:] + keys[:offset]
    rebuilt: dict[str, Any] = {}
    for key in keys:
        value = payload[key]
        if isinstance(value, Mapping):
            rebuilt[key] = shuffled_keys(value, seed + 1)
        elif isinstance(value, list):
            rebuilt[key] = [
                shuffled_keys(element, seed + 1) if isinstance(element, Mapping) else element
                for element in value
            ]
        else:
            rebuilt[key] = value
    return rebuilt


def shuffled_arrays(payload: Mapping[str, Any], seed: int) -> dict[str, Any]:
    """Rotate every content-ordered collection, so array order is not insertion order."""
    rebuilt = dict(payload)
    for name in SORTED_COLLECTIONS:
        elements = rebuilt.get(name)
        if isinstance(elements, list) and elements:
            offset = seed % len(elements)
            rebuilt[name] = elements[offset:] + elements[:offset]
    return rebuilt


def canonical(payload: Mapping[str, Any]) -> bytes:
    """The canonical bytes of a payload, through the one serialiser."""
    return canonicalise(payload, array_rules=CERTIFICATE_ARRAY_RULES)


def expected_counts(graph: Sequence[GraphArtifact], live_bound: int) -> tuple[int, int]:
    """The before-count and after-count computed straight from the generating graph."""
    withdrawn = sum(1 for entry in graph if ERASED_SLUG in entry.bindings_before)
    return withdrawn + live_bound, live_bound


# Feature: molt, Property 11: For any Erasure_Certificate payload, parsing the
# canonical serialisation yields an equivalent payload, the canonical bytes are
# identical across arbitrary key insertion orders and arbitrary array orderings of
# the sorted collections, the payload contains every key the certificate contract
# requires with each collection agreeing with the stored evidence it is derived
# from, and the before-state and after-state counts derived from the Ledger and the
# recorded Dispositions equal the counts computed independently from the generating
# memory graph with `count_derivation` reading `ledger_and_dispositions`.
@settings(max_examples=MAX_EXAMPLES)
@given(case=certificate_payloads())
def test_certificate_canonical_round_trip_and_schema_completeness(case: Generated) -> None:
    """One conjoined statement: order-independent bytes, equivalent parse, whole schema."""
    payload = certificate_payload(case.evidence)
    bytes_of = canonical(payload)

    # Order does not reach the bytes, in either dimension.
    assert canonical(shuffled_keys(payload, seed=1)) == bytes_of
    assert canonical(shuffled_arrays(payload, seed=1)) == bytes_of
    assert canonical(shuffled_arrays(shuffled_keys(payload, seed=2), seed=3)) == bytes_of

    # The parse is equivalent, and re-serialising it reproduces the same bytes.
    parsed = json.loads(bytes_of.decode("utf-8"))
    assert isinstance(parsed, dict)
    assert canonical(parsed) == bytes_of

    # Every contract key is present, at the root and in each block.
    assert set(parsed) == set(CONTRACT_KEYS)
    for name, keys in BLOCK_KEYS.items():
        assert set(parsed[name]) == set(keys), name
    assert set(parsed["counts"]["historical_corroboration"]) == set(CORROBORATION_KEYS)
    if case.evidence.checkpoint is None:
        assert parsed["ledger_checkpoint"] is None
    else:
        assert set(parsed["ledger_checkpoint"]) == set(CHECKPOINT_KEYS)
    for entry in parsed["dispositions"]:
        assert set(entry) == set(DISPOSITION_KEYS)
    for entry in parsed["residue_candidates"]:
        assert set(entry) == set(RESIDUE_KEYS)
    for entry in parsed["sessions"]:
        assert set(entry) == set(SESSION_KEYS)

    # Each collection agrees with the evidence it derives from.
    assert len(parsed["dispositions"]) == len(case.evidence.dispositions)
    assert {entry["artifact_id"] for entry in parsed["dispositions"]} == {
        str(entry.artifact_id) for entry in case.evidence.dispositions
    }
    assert len(parsed["residue_candidates"]) == len(case.evidence.residue)
    assert len(parsed["lineage_subgraph"]) == len(case.evidence.lineage)
    assert len(parsed["sessions"]) == len(case.evidence.sessions)
    assert [entry["name"] for entry in parsed["verification_queries"]] == sorted(
        template.name for template in VERIFICATION_TEMPLATES
    )
    assert all(
        entry["params"] == [str(case.evidence.client_id)]
        for entry in parsed["verification_queries"]
    )
    assert parsed["certificate_version"] == CERTIFICATE_VERSION
    assert set(parsed["caveats"]) == set(CAVEATS)

    # The derived counts equal the counts computed from the generating graph.
    before, after = expected_counts(case.graph, case.live_bound)
    assert parsed["counts"]["count_derivation"] == COUNT_DERIVATION
    assert parsed["counts"]["artifacts_bound_before"] == str(before)
    assert parsed["counts"]["artifacts_bound_after"] == str(after)

    event(f"checkpoint named: {case.evidence.checkpoint is not None}")
    event(f"corroboration attempted: {case.evidence.counts.corroboration.attempted}")


@settings(max_examples=MAX_EXAMPLES)
@given(case=certificate_payloads())
def test_the_envelope_carries_the_digest_of_the_payload_it_wraps(case: Generated) -> None:
    """Signing commits to the canonical digest, and the envelope states that digest."""
    signed = sign(case.evidence, signer=StubSigner(), policy=policy())
    envelope = envelope_of(signed)
    signature = envelope["signature"]
    assert isinstance(signature, Mapping)

    assert signed.payload_bytes == canonical(signed.payload)
    assert signature["payload_digest"] == hashlib.sha256(signed.payload_bytes).hexdigest()
    assert signature["kms_key_id"] == STUB_KEY_ID
    assert signature["algorithm"] == STUB_ALGORITHM
    value = signature["value"]
    assert isinstance(value, str)
    assert base64.b64decode(value) == signed.signature
    assert StubSigner().verify_digest(
        bytes.fromhex(signed.payload_digest),
        signed.signature,
        key_id=STUB_KEY_ID,
        algorithm=STUB_ALGORITHM,
        public_key=StubSigner().public_key(key_id=STUB_KEY_ID),
    )
