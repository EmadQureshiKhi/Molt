"""Certificate assembly against a live instance: stored rows in, one signed document out.

The property suite drives the payload's shape and its canonical bytes over many
generated shapes. This module asserts the four things only a cluster can answer.

**The payload is assembled from stored rows and from nothing else.** Every value
the document states is placed in a table first, and the assertions compare the
payload against the rows rather than against what the test passed to the builder.
A field the builder invented, or carried across from its own arguments, would fail
here even though it would serialise perfectly.

**Every required field of the contract is present, the ownership, attribution, and
checkpoint blocks included.** The attribution pair is the interesting one: the rows
it is read from are removed by the disposition phase, so the snapshot is taken
before those rows go and the certificate still states when the tenant's content
began being held.

**The counts use the derived mechanism, and the historical read is recorded as
corroboration.** The derived figures come from the Dispositions and the live
attribution count; the point-in-time read is attempted only because the measured
horizon is recorded as covering both instants, and whatever it found is recorded in
the corroboration block rather than replacing anything.

**A storage failure keeps the signed certificate in the cluster.** The object store
raises, and the certificate row is still there with its signature, its digest, and
the failure recorded on it, because a document nobody could upload is still
evidence.

**Validates: Requirements 20.2, 20.3, 20.4, 20.6, 21.1, 21.2, 21.3, 21.4, 21.5,
21.6, 21.7, 21.8, 21.9, 21.10, 21.11, 21.12, 21.13, 21.14, 21.15, 43.7, 44.11,
45.11**
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.attest.builder import (
    CAVEATS,
    COUNT_DERIVATION,
    OBJECT_LOCK_MODE,
    STORAGE_FAILED,
    STORAGE_STORED,
    VERIFICATION_TEMPLATES,
    CertificatePolicy,
    assemble,
    certificate_payload,
    first_attribution_snapshot,
    issue,
    object_key_for,
)
from molt.attest.canonical import CERTIFICATE_ARRAY_RULES, canonicalise
from molt.attest.checkpoint import CheckpointPolicy, CheckpointWindow, compute, sign_and_store
from molt.models.session import SessionOutcome
from molt.store import Connection, MemoryStore
from molt.store.historical import GC_HORIZON_CAPABILITY

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Every row this module places. The certificate builder owns no insert of its own
# beyond the certificate row, so the whole evidence set is placed here.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, outcome, ended_at) "
    "VALUES (%s, %s, %s, %s, %s, now())"
)
INSERT_LEASE: Final[str] = (
    "INSERT INTO erasure_lease (id, client_id, owner, generation, idempotency_key, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
INSERT_REQUEST: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification, status) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_RUN: Final[str] = """
INSERT INTO erasure_run (
    id, request_id, client_id, requester, dry_run, status, phase, t_before, t_after,
    auto_include_threshold, review_threshold, unembedded_count, working_rows_deleted,
    fencing_generation, lease_id, idempotency_key, finished_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
"""
INSERT_RUN_SESSION: Final[str] = (
    "INSERT INTO run_session (run_id, session_id, terminal_chain_digest, terminal_seq, row_count) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_DISPOSITION: Final[str] = """
INSERT INTO disposition (
    run_id, artifact_id, artifact_kind, disposition, reason, selection_reason,
    pre_digest, post_digest, bindings_before, bindings_after, fencing_generation)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
INSERT_RESIDUE: Final[str] = """
INSERT INTO residue_candidate (
    run_id, artifact_id, artifact_kind, query_artifact_id, cosine_distance, band,
    adjudicated, model_id, classification, reasoning, included, decision_reason)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
INSERT_BACKUP: Final[str] = """
INSERT INTO backup_record (
    run_id, backup_id, backup_path, target_uri, command, taken, referenced, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""
INSERT_AUDIT: Final[str] = (
    "INSERT INTO audit_log_snapshot (run_id, window_start, window_end, records) "
    "VALUES (%s, %s, %s, %s::JSONB)"
)
INSERT_DERIVED: Final[str] = """
INSERT INTO derived_artifact (
    id, kind, owner_client_id, body, content_digest, derivation_method, expires_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""
INSERT_EDGE: Final[str] = (
    "INSERT INTO lineage_edge (child_id, parent_id, parent_kind, derivation_method) "
    "VALUES (%s, %s, %s, %s)"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, confidence) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
DELETE_BINDING: Final[str] = "DELETE FROM client_binding WHERE id = %s"
UPSERT_CAPABILITY: Final[str] = (
    "UPSERT INTO capability (name, available, detail) VALUES (%s, %s, %s)"
)
CLUSTER_NOW: Final[str] = "SELECT now()"
CERTIFICATE_ROW_QUERY: Final[str] = """
SELECT payload, canonical_digest, signature, kms_key_id, signing_algorithm,
       s3_bucket, s3_key, s3_version_id, storage_status, storage_detail, fencing_generation
FROM erasure_certificate
WHERE run_id = %s
"""

# The signing surface and the object surface every example drives. Neither reaches a
# real service, and no credential is present in this environment by design.
STUB_KEY_ID: Final[str] = "stub-certificate-key"
STUB_ALGORITHM: Final[str] = "ECDSA_SHA_256"
STUB_BUCKET: Final[str] = "stub-certificate-bucket"
STUB_PREFIX: Final[str] = "certificates/"
LOCK_DAYS: Final[int] = 1

# The values the placed rows carry. Each one an assertion turns on is named here so
# the assertion compares against the same value the insert bound.
JURISDICTION: Final[str] = "eu"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"
REQUESTER: Final[str] = "governance-owner-principal"
JUSTIFICATION: Final[str] = "engagement concluded under a contractual purge obligation"
OWNER: Final[str] = "worker-owner-identifier"
GENERATION: Final[int] = 1
AUTO_INCLUDE: Final[float] = 0.20
REVIEW: Final[float] = 0.45
UNEMBEDDED: Final[int] = 3
WORKING_ROWS: Final[int] = 12
EVENT_KIND: Final[str] = "event"
DERIVED_KIND: Final[str] = "derived_artifact"
HARD_DELETE: Final[str] = "hard_delete"
SURGICAL_REDACTION: Final[str] = "surgical_redaction"
DELETE_REASON: Final[str] = "sole binding was the erased tenant"
REDACT_REASON: Final[str] = "blended_artifact_rewritten"
SELECTION_REASON: Final[str] = "client_binding"
RETAINED_SLUG: Final[str] = "borealis"
BAND: Final[str] = "auto_include"
DECISION_REASON: Final[str] = "below_auto_include_threshold"
DISTANCE: Final[float] = 0.183041
BACKUP_PATH: Final[str] = "self_managed"
BACKUP_TARGET: Final[str] = "an-object-store-backup-target"
BACKUP_COMMAND: Final[str] = "the recorded backup command"
BACKUP_STATUS: Final[str] = "succeeded"
DERIVATION_METHOD: Final[str] = "distil_behavioral_baseline"
DERIVED_BODY_KIND: Final[str] = "summary"
BINDING_METHOD: Final[str] = "marker"
BINDING_CONFIDENCE: Final[float] = 0.9
AUDIT_RECORD: Final[dict[str, str]] = {"action": "backup", "principal": "an-operator-principal"}
HORIZON_SECONDS: Final[str] = "4500"
CHECKPOINT_INTERVAL: Final[int] = 3600
ROW_LIFETIME: Final[timedelta] = timedelta(days=90)
LEASE_LIFETIME: Final[timedelta] = timedelta(minutes=30)
TERMINAL_SEQ: Final[int] = 4
ROW_COUNT: Final[int] = 4

# One slug per placed tenant, so two tests in one schema never share a lease.
_SLOTS: Final[Iterator[int]] = count(1)

# A connection is typed loosely because the driver arrives through a fixture rather
# than an import, which keeps this module collectable with no driver installed.
DriverConnection = Any


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
class PlacedObject:
    """One recorded write, so the retention posture is asserted rather than assumed."""

    bucket: str
    key: str
    body: bytes
    object_lock_mode: str
    retain_until: datetime


@dataclass(slots=True)
class StubObjectStore:
    """An object store that records what it was asked to write, or refuses to.

    Refusal is a constructor flag rather than a patch, because the failure path is
    part of the contract: a signed certificate has to survive a storage fault, and
    the only way to assert that is to make the fault happen.
    """

    refuse: bool = False
    version: str = "a-stub-object-version"
    written: list[PlacedObject] = field(default_factory=list)

    def put_certificate(
        self,
        body: bytes,
        *,
        bucket: str,
        key: str,
        object_lock_mode: str,
        retain_until: datetime,
    ) -> str:
        """Record the write, or refuse it the way an unreachable store would."""
        if self.refuse:
            raise OSError("the evidence object store refused the write")
        self.written.append(
            PlacedObject(
                bucket=bucket,
                key=key,
                body=body,
                object_lock_mode=object_lock_mode,
                retain_until=retain_until,
            )
        )
        return self.version


@dataclass(frozen=True, slots=True)
class Placed:
    """Every identifier the placed evidence set is addressed by."""

    client_id: UUID
    client_slug: str
    session_id: UUID
    request_id: UUID
    run_id: UUID
    deleted_id: UUID
    redacted_id: UUID
    parent_id: UUID
    chain_digest: str
    t_before: datetime
    t_after: datetime


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, and a store over it."""

    store: MemoryStore
    connection: DriverConnection

    def rows(
        self,
        statement: str,
        params: tuple[object, ...] | None = None,
    ) -> list[tuple[Any, ...]]:
        """Send one statement on this module's own connection."""
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            if cursor.description is None:
                return []
            return list(cursor.fetchall())

    def send(self, statement: str, params: tuple[object, ...] | None = None) -> None:
        """Send one statement whose rows nothing reads."""
        self.rows(statement, params)

    def now(self) -> datetime:
        """The cluster's own reading, which the run's instants are cut from."""
        produced = self.rows(CLUSTER_NOW)
        assert len(produced) == 1
        reading = produced[0][0]
        assert isinstance(reading, datetime)
        return reading

    def place(self) -> Placed:
        """Place one complete evidence set for one tenant and return its identifiers.

        The order is the order a run performs it in: the tenant and its lease, the
        request and the run, the artifacts and their lineage, then the evidence rows
        the certificate is assembled from. The run's instants are the cluster's own
        readings rather than the test process's, so the historical corroboration is
        asked about instants this cluster can answer for.
        """
        slot = next(_SLOTS)
        client_id = uuid4()
        slug = f"tenant-{slot}-{client_id.hex[:8]}"
        self.send(INSERT_CLIENT, (client_id, slug, "Tenant", JURISDICTION))
        lease_id = uuid4()
        self.send(
            INSERT_LEASE,
            (
                lease_id,
                client_id,
                OWNER,
                GENERATION,
                f"an-idempotency-key-{slug}",
                datetime.now(tz=UTC) + LEASE_LIFETIME,
            ),
        )
        session_id = uuid4()
        self.send(
            INSERT_SESSION,
            (session_id, client_id, AGENT_CLI, MACHINE_ID, SessionOutcome.SUCCEEDED.value),
        )
        t_before = self.now()

        deleted_id = uuid4()
        redacted_id = uuid4()
        parent_id = uuid4()
        expires_at = datetime.now(tz=UTC) + ROW_LIFETIME
        for identifier, body in ((redacted_id, "the rewritten body"), (parent_id, "the parent")):
            self.send(
                INSERT_DERIVED,
                (
                    identifier,
                    DERIVED_BODY_KIND,
                    client_id,
                    body,
                    hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    DERIVATION_METHOD,
                    expires_at,
                ),
            )
        self.send(INSERT_EDGE, (redacted_id, parent_id, DERIVED_KIND, DERIVATION_METHOD))

        request_id = uuid4()
        run_id = uuid4()
        self.send(INSERT_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION, "completed"))
        t_after = self.now()
        self.send(
            INSERT_RUN,
            (
                run_id,
                request_id,
                client_id,
                REQUESTER,
                False,
                "completed",
                "done",
                t_before,
                t_after,
                AUTO_INCLUDE,
                REVIEW,
                UNEMBEDDED,
                WORKING_ROWS,
                GENERATION,
                lease_id,
                f"a-run-idempotency-key-{slug}",
            ),
        )
        chain_digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()
        self.send(
            INSERT_RUN_SESSION,
            (run_id, session_id, chain_digest, TERMINAL_SEQ, ROW_COUNT),
        )
        self.send(
            INSERT_DISPOSITION,
            (
                run_id,
                deleted_id,
                EVENT_KIND,
                HARD_DELETE,
                DELETE_REASON,
                SELECTION_REASON,
                hashlib.sha256(b"the deleted body").hexdigest(),
                None,
                [slug],
                [],
                GENERATION,
            ),
        )
        self.send(
            INSERT_DISPOSITION,
            (
                run_id,
                redacted_id,
                DERIVED_KIND,
                SURGICAL_REDACTION,
                REDACT_REASON,
                SELECTION_REASON,
                hashlib.sha256(b"the prior body").hexdigest(),
                hashlib.sha256(b"the rewritten body").hexdigest(),
                [slug, RETAINED_SLUG],
                [RETAINED_SLUG],
                GENERATION,
            ),
        )
        self.send(
            INSERT_RESIDUE,
            (
                run_id,
                deleted_id,
                EVENT_KIND,
                uuid4(),
                DISTANCE,
                BAND,
                False,
                None,
                None,
                None,
                True,
                DECISION_REASON,
            ),
        )
        self.send(
            INSERT_BACKUP,
            (
                run_id,
                f"a-backup-identifier-{slot}",
                BACKUP_PATH,
                BACKUP_TARGET,
                BACKUP_COMMAND,
                True,
                False,
                BACKUP_STATUS,
            ),
        )
        self.send(
            INSERT_AUDIT,
            (run_id, t_before, t_after, json.dumps([AUDIT_RECORD])),
        )
        return Placed(
            client_id=client_id,
            client_slug=slug,
            session_id=session_id,
            request_id=request_id,
            run_id=run_id,
            deleted_id=deleted_id,
            redacted_id=redacted_id,
            parent_id=parent_id,
            chain_digest=chain_digest,
            t_before=t_before,
            t_after=t_after,
        )

    def attribution_snapshot(self, placed: Placed) -> dict[UUID, Any]:
        """Take the first-attribution snapshot the way a run does: before disposition.

        The binding is placed, the snapshot is read, and the binding is then removed,
        which is exactly the sequence a hard delete performs. The certificate must
        still state when the tenant's content began being held.
        """
        binding_id = uuid4()
        self.send(
            INSERT_BINDING,
            (
                binding_id,
                placed.deleted_id,
                EVENT_KIND,
                placed.client_id,
                BINDING_METHOD,
                BINDING_CONFIDENCE,
            ),
        )
        snapshot = dict(
            first_attribution_snapshot(self.store, placed.client_id, [placed.deleted_id])
        )
        self.send(DELETE_BINDING, (binding_id,))
        return snapshot


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema.

    Every migration is applied because the certificate's ownership columns, the
    checkpoint tables, and the restricting references all arrive in later
    generations than the evidence tables they sit beside.
    """
    from molt.store.migrate import apply_migrations

    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])
        cursor.execute(UPSERT_CAPABILITY, (GC_HORIZON_CAPABILITY, True, HORIZON_SECONDS))

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


def policy() -> CertificatePolicy:
    """The policy every example issues under, naming no real key and no real bucket."""
    return CertificatePolicy(
        kms_key_id=STUB_KEY_ID,
        signing_algorithm=STUB_ALGORITHM,
        bucket=STUB_BUCKET,
        prefix=STUB_PREFIX,
        object_lock_days=LOCK_DAYS,
    )


def parsed_payload(cluster: Cluster, placed: Placed) -> dict[str, Any]:
    """Assemble and canonicalise, then read the document back the way a reviewer would."""
    evidence = assemble(
        cluster.store,
        placed.run_id,
        attributions=cluster.attribution_snapshot(placed),
    )
    payload = certificate_payload(evidence)
    parsed = json.loads(canonicalise(payload, array_rules=CERTIFICATE_ARRAY_RULES).decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


# ---------------------------------------------------------------------------
# Assembly from stored rows
# ---------------------------------------------------------------------------


def test_the_payload_is_assembled_from_the_stored_evidence_rows(cluster: Cluster) -> None:
    """Every block states what a table holds, and the collections match the rows."""
    placed = cluster.place()
    parsed = parsed_payload(cluster, placed)

    assert parsed["erasure_request"] == {
        "request_id": str(placed.request_id),
        "requester": REQUESTER,
        "justification": JUSTIFICATION,
        "submitted_at": parsed["erasure_request"]["submitted_at"],
    }
    assert parsed["client"] == {"client_id": str(placed.client_id), "slug": placed.client_slug}
    assert parsed["run"]["run_id"] == str(placed.run_id)
    assert parsed["run"]["dry_run"] is False
    assert parsed["run"]["auto_include_threshold"] == "0.200000"
    assert parsed["run"]["review_threshold"] == "0.450000"
    assert parsed["run"]["unembedded_artifact_count"] == str(UNEMBEDDED)
    assert parsed["run"]["working_rows_deleted"] == str(WORKING_ROWS)

    assert parsed["ownership"] == {
        "owner": OWNER,
        "fencing_generation": str(GENERATION),
        "idempotency_key": parsed["ownership"]["idempotency_key"],
    }
    assert parsed["backup"]["present"] is True
    assert parsed["backup"]["backup_path"] == BACKUP_PATH
    assert parsed["backup"]["taken"] is True
    assert parsed["backup"]["referenced"] is False
    assert parsed["backup"]["target_uri"] == BACKUP_TARGET
    assert parsed["backup"]["statement"] == BACKUP_COMMAND

    dispositions = {entry["artifact_id"]: entry for entry in parsed["dispositions"]}
    assert set(dispositions) == {str(placed.deleted_id), str(placed.redacted_id)}
    redacted = dispositions[str(placed.redacted_id)]
    assert redacted["disposition"] == SURGICAL_REDACTION
    assert redacted["bindings_before"] == sorted([placed.client_slug, RETAINED_SLUG])
    assert redacted["bindings_after"] == [RETAINED_SLUG]
    assert redacted["pre_digest"] and redacted["post_digest"]
    deleted = dispositions[str(placed.deleted_id)]
    assert deleted["post_digest"] is None
    # Read before the disposition ran, which is the only moment it could be read.
    assert deleted["first_attributed_at"] is not None
    assert deleted["first_attribution_method"] == BINDING_METHOD

    assert parsed["lineage_subgraph"] == [
        {
            "child_id": str(placed.redacted_id),
            "parent_id": str(placed.parent_id),
            "parent_kind": DERIVED_KIND,
            "derivation_method": DERIVATION_METHOD,
        }
    ]
    assert len(parsed["residue_candidates"]) == 1
    assert parsed["residue_candidates"][0]["cosine_distance"] == "0.183041"
    assert parsed["residue_candidates"][0]["band"] == BAND
    assert parsed["residue_candidates"][0]["adjudicated"] is False
    assert parsed["residue_candidates"][0]["model_id"] is None

    assert parsed["sessions"] == [
        {
            "session_id": str(placed.session_id),
            "terminal_chain_digest": placed.chain_digest,
            "terminal_seq": str(TERMINAL_SEQ),
            "row_count": str(ROW_COUNT),
        }
    ]
    assert parsed["cluster_audit_log"]["records"] == [AUDIT_RECORD]
    assert set(parsed["caveats"]) == set(CAVEATS)
    assert [entry["name"] for entry in parsed["verification_queries"]] == sorted(
        template.name for template in VERIFICATION_TEMPLATES
    )
    # No verification query, and no assembled field, names the working tier.
    assert all("working_memory" not in entry["sql"] for entry in parsed["verification_queries"])


def test_the_named_checkpoint_is_the_most_recent_one_closing_before_the_run(
    cluster: Cluster,
) -> None:
    """The checkpoint block names the checkpoint a verifier can extend integrity from."""
    placed = cluster.place()
    window = CheckpointWindow(
        start=placed.t_before - timedelta(seconds=CHECKPOINT_INTERVAL),
        end=placed.t_before,
    )
    stored = sign_and_store(
        cluster.store,
        compute(cluster.store, window),
        signer=StubSigner(),
        policy=CheckpointPolicy(
            interval_seconds=CHECKPOINT_INTERVAL,
            kms_key_id=STUB_KEY_ID,
            signing_algorithm=STUB_ALGORITHM,
        ),
    )

    parsed = parsed_payload(cluster, placed)
    block = parsed["ledger_checkpoint"]

    assert block is not None
    assert block["checkpoint_id"] == str(stored.checkpoint_id)
    assert block["root_digest"] == stored.root_digest
    assert block["covered_session_count"] == str(stored.covered_session_count)
    assert block["window_end"] is not None


# ---------------------------------------------------------------------------
# The counts and the corroboration
# ---------------------------------------------------------------------------


def test_the_counts_are_derived_and_the_historical_read_only_corroborates(
    cluster: Cluster,
) -> None:
    """The derived mechanism is named on the certificate and the read is a note beside it."""
    placed = cluster.place()
    parsed = parsed_payload(cluster, placed)
    counts = parsed["counts"]

    # Two Dispositions recorded the tenant among the prior bindings, and nothing is
    # bound now, so the derived pair is two and zero and comes from append-only rows.
    assert counts["count_derivation"] == COUNT_DERIVATION
    assert counts["artifacts_bound_before"] == "2"
    assert counts["artifacts_bound_after"] == "0"
    assert counts["hard_delete"] == "1"
    assert counts["surgical_redaction"] == "1"
    assert counts["retained"] == "0"

    corroboration = counts["historical_corroboration"]
    assert set(corroboration) == {"attempted", "within_horizon", "agrees", "gc_horizon_seconds"}
    assert corroboration["gc_horizon_seconds"] == HORIZON_SECONDS
    assert corroboration["within_horizon"] is True
    assert isinstance(corroboration["attempted"], bool)
    if corroboration["attempted"]:
        assert isinstance(corroboration["agrees"], bool)
    else:
        assert corroboration["agrees"] is None


# ---------------------------------------------------------------------------
# Signing, storage, and the storage failure
# ---------------------------------------------------------------------------


def test_a_stored_certificate_records_its_object_and_its_lock(cluster: Cluster) -> None:
    """The row carries the signature, the digest, the object name, and the version."""
    placed = cluster.place()
    object_store = StubObjectStore()
    issued = issue(
        cluster.store,
        placed.run_id,
        signer=StubSigner(),
        object_store=object_store,
        policy=policy(),
        attributions=cluster.attribution_snapshot(placed),
    )

    assert issued.stored
    assert len(object_store.written) == 1
    written = object_store.written[0]
    assert written.bucket == STUB_BUCKET
    assert written.key == object_key_for(policy(), placed.client_slug, placed.run_id)
    assert written.object_lock_mode == OBJECT_LOCK_MODE
    assert written.retain_until > datetime.now(tz=UTC)
    envelope = json.loads(written.body.decode("utf-8"))
    assert envelope["signature"]["payload_digest"] == issued.signed.payload_digest
    assert envelope["signature"]["kms_key_id"] == STUB_KEY_ID

    row = cluster.rows(CERTIFICATE_ROW_QUERY, (placed.run_id,))
    assert len(row) == 1
    (
        payload,
        digest,
        signature,
        key_id,
        algorithm,
        bucket,
        key,
        version,
        status,
        detail,
        generation,
    ) = row[0]
    assert payload is not None
    assert digest == issued.signed.payload_digest
    assert bytes(signature) == issued.signed.signature
    assert key_id == STUB_KEY_ID
    assert algorithm == STUB_ALGORITHM
    assert (bucket, key, version) == (STUB_BUCKET, written.key, object_store.version)
    assert status == STORAGE_STORED
    assert detail is None
    assert generation == GENERATION


def test_a_storage_failure_keeps_the_signed_certificate_in_the_cluster(
    cluster: Cluster,
) -> None:
    """The object write fails, and the signed document is still evidence in the cluster."""
    placed = cluster.place()
    object_store = StubObjectStore(refuse=True)
    issued = issue(
        cluster.store,
        placed.run_id,
        signer=StubSigner(),
        object_store=object_store,
        policy=policy(),
        attributions=cluster.attribution_snapshot(placed),
    )

    assert not issued.stored
    assert issued.storage_status == STORAGE_FAILED
    assert issued.storage_detail
    assert issued.version_id is None
    assert object_store.written == []

    row = cluster.rows(CERTIFICATE_ROW_QUERY, (placed.run_id,))
    assert len(row) == 1
    payload, digest, signature, _key_id, _algorithm, _bucket, key, version, status, detail, _gen = (
        row[0]
    )
    assert payload is not None
    assert digest == issued.signed.payload_digest
    assert bytes(signature) == issued.signed.signature
    assert key == issued.object_key
    assert version is None
    assert status == STORAGE_FAILED
    assert detail
