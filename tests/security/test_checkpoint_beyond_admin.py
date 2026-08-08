"""What a signature held outside the cluster adds over the per-Session chain.

A principal holding administrator privilege can rewrite a Session's rows *and* the
digest columns that cover them. The chain rule is then satisfied by construction:
recomputing it from the rewritten content reproduces exactly the digests stored, so
per-Session chain verification passes and reports nothing. That is not a defect of
the chain, it is the boundary of what any in-cluster mechanism can establish about
itself.

The checkpoint is the mechanism outside that boundary, and this module pins the
three claims that make it worth having.

**The consistent rewrite passes chain verification.** Asserted directly rather than
assumed, because the rest of the module only means something if the rewrite really
is the one the chain cannot see.

**The same rewrite fails checkpoint verification.** The terminal digest of the
window no longer matches the digest recorded when the checkpoint was taken, no
Disposition accounts for the movement, and the refusal names the rewritten Session.

**Rewriting the stored root digest too does not recover agreement.** An
administrator who notices the recomputation and edits the stored digest to match
holds no signing key: the digest they wrote is not the digest that was signed, so
the signature check fails and the finding is named as a signature fault rather than
as a moved Session. This is precisely the coverage a signature produced outside the
cluster adds, and it is why the private half is generated here in this process and
never handed to the emulated cluster.

Nothing here opens a socket or reads a credential. Two real key pairs are generated
locally, the ledger and the checkpoint tables are emulated in memory, and the
statements the checkpoint module sends are answered by identity, so a statement this
emulation does not know fails loudly instead of being guessed at.

**Validates: Requirements 45.6, 45.7, 45.14, 36.2**
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
)

from molt.attest.checkpoint import (
    ACCOUNTING_RUNS_QUERY,
    CHECKPOINT_BY_ID_QUERY,
    INSERT_CHECKPOINT_SESSION_STATEMENT,
    INSERT_CHECKPOINT_STATEMENT,
    RECORDED_SESSIONS_QUERY,
    WINDOW_TIPS_QUERY,
    CheckpointPolicy,
    CheckpointWindow,
    StoredCheckpoint,
    compute,
    require_agreement,
    sign_and_store,
    verify,
)
from molt.errors import CheckpointDisagreementError, VerificationFailedError
from molt.models.event import JsonObject
from molt.store import MemoryStore
from molt.store.chain import (
    CHAIN_ROWS_QUERY,
    GENESIS_PREDECESSOR,
    ChainRow,
    canonical_payload_text,
    chain_digest,
    content_digest_input,
    sha256_hex,
    verify_chain,
)

# The signing surface. The identifier names a key generated in this process, and no
# credential of any kind is read.
SIGNING_KEY_ID: Final[str] = "checkpoint-signing-key"
SIGNING_ALGORITHM: Final[str] = "ECDSA_SHA_256"
INTERVAL_SECONDS: Final[int] = 3600

# The column count of the stored checkpoint row, and the position the root digest is
# held at, which is the column the administrator below rewrites.
CHECKPOINT_COLUMNS: Final[int] = 9
ROOT_DIGEST_COLUMN: Final[int] = 4

# How the emulated rows are laid out. The window encloses every placed row, so a
# disagreement is a change to the past rather than an append after the fact.
EVENT_SPACING: Final[timedelta] = timedelta(milliseconds=1)
WINDOW_MARGIN: Final[timedelta] = timedelta(seconds=1)
EVENTS_PER_SESSION: Final[int] = 4
SESSION_COUNT: Final[int] = 3

# The values the emulated rows carry. None is what an assertion turns on.
CATEGORY: Final[str] = "tool_call"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"
REWRITTEN_BODY: Final[str] = "the step a later administrator substituted"

T = TypeVar("T")


# ---------------------------------------------------------------------------
# The key the cluster does not hold
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutsideKey:
    """One asymmetric key pair held outside the emulated cluster.

    The private half never reaches the ledger, the checkpoint tables, or anything an
    administrator of them could read, which is the whole point: a rewrite inside the
    cluster cannot be followed by a signature over the rewritten value.
    """

    private: ec.EllipticCurvePrivateKey

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        """Sign an already-computed digest, as the certificate path signs."""
        assert key_id == SIGNING_KEY_ID, "the emulated key service holds one key"
        assert algorithm == SIGNING_ALGORITHM
        return self.private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    def public_key(self, *, key_id: str) -> bytes:
        """The public half, in the encoded form a retrieval returns."""
        assert key_id == SIGNING_KEY_ID, "the emulated key service holds one key"
        return self.private.public_key().public_bytes(
            encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo
        )

    def verify_digest(
        self,
        digest: bytes,
        signature: bytes,
        *,
        key_id: str,
        algorithm: str,
        public_key: bytes,
    ) -> bool:
        """Check a signature against the retrieved public half, locally.

        The retrieved half is used rather than the private one, so verification is a
        computation over a key the verifier holds rather than a question put to the
        service that signed.
        """
        assert algorithm == SIGNING_ALGORITHM
        del key_id
        loaded = _loaded_public_key(public_key)
        try:
            loaded.verify(signature, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        except InvalidSignature:
            return False
        return True


def _loaded_public_key(encoded: bytes) -> ec.EllipticCurvePublicKey:
    """The public half parsed out of the encoded form a retrieval returns.

    Parsed as the structure it is rather than scanned for the point inside it. A scan
    for the uncompressed-point marker is not merely inelegant, it is wrong: the marker
    byte is an ordinary value that the coordinates themselves may contain, so which
    occurrence a scan finds depends on the key, and a key whose coordinates hold a
    later occurrence would be rejected as malformed. This is the same parse the
    Certificate_Verifier performs on the same bytes, so the check here is made against
    what a retrieval actually hands back.
    """
    loaded = load_der_public_key(encoded)
    assert isinstance(loaded, ec.EllipticCurvePublicKey), (
        "the emulated key service holds one elliptic-curve key"
    )
    return loaded


# ---------------------------------------------------------------------------
# The emulated ledger and checkpoint tables
# ---------------------------------------------------------------------------


def _rows_of(
    session_id: UUID,
    client_id: UUID,
    opening: datetime,
    bodies: Sequence[str],
) -> tuple[ChainRow, ...]:
    """Derive one Session's chain from its content, under the chain's own rule.

    Both digest columns follow from the content, so a Session rebuilt through this
    function is self-consistent whatever its content is. That is what makes the
    rewrite below the rewrite a per-Session chain cannot detect.
    """
    derived: list[ChainRow] = []
    previous = GENESIS_PREDECESSOR
    for position, body in enumerate(bodies, start=1):
        payload: JsonObject = {"step": position, "body": body}
        occurred_at = opening + (position - 1) * EVENT_SPACING
        content = sha256_hex(
            content_digest_input(
                event_id=_event_id(session_id, position),
                session_id=session_id,
                client_id=client_id,
                seq=position,
                category=CATEGORY,
                occurred_at=occurred_at,
                agent_cli=AGENT_CLI,
                machine_id=MACHINE_ID,
                parent_event_id=None,
                payload_text=canonical_payload_text(payload),
                redacted=False,
            )
        )
        chained = chain_digest(previous, content)
        derived.append(
            ChainRow(
                event_id=_event_id(session_id, position),
                session_id=session_id,
                client_id=client_id,
                seq=position,
                category=CATEGORY,
                occurred_at=occurred_at,
                agent_cli=AGENT_CLI,
                machine_id=MACHINE_ID,
                parent_event_id=None,
                payload=payload,
                redacted=False,
                content_digest=content,
                prev_chain_digest=previous,
                chain_digest=chained,
            )
        )
        previous = chained
    return tuple(derived)


def _event_id(session_id: UUID, position: int) -> UUID:
    """A stable Event identifier, so a rewrite keeps the row it rewrote."""
    return UUID(int=(session_id.int + position) % (1 << 128))


@dataclass(slots=True)
class Tables:
    """The ledger and the two checkpoint tables, in memory and mutable."""

    sessions: dict[UUID, tuple[ChainRow, ...]] = field(default_factory=dict)
    checkpoints: dict[UUID, tuple[object, ...]] = field(default_factory=dict)
    covered: dict[UUID, list[tuple[UUID, int, str]]] = field(default_factory=dict)

    def rewrite_consistently(self, session_id: UUID) -> None:
        """Rewrite the terminal row's content and re-derive the whole chain over it.

        This is the administrator's edit: the content moves, and both digest columns
        move with it under the same rule the verifier recomputes, so the Session is
        left self-consistent.
        """
        rows = self.sessions[session_id]
        bodies = [str(row.payload["body"]) for row in rows]
        bodies[-1] = REWRITTEN_BODY
        self.sessions[session_id] = _rows_of(
            session_id, rows[0].client_id, rows[0].occurred_at, bodies
        )

    def rewrite_stored_root_digest(self, checkpoint_id: UUID, digest: str) -> None:
        """Rewrite the root digest column of a stored checkpoint row.

        Reachable by anyone holding write privilege on the cluster, which is exactly
        the principal the signature exists to be outside of.
        """
        stored = list(self.checkpoints[checkpoint_id])
        stored[ROOT_DIGEST_COLUMN] = digest
        self.checkpoints[checkpoint_id] = tuple(stored)

    def rewrite_recorded_digests(self, checkpoint_id: UUID) -> None:
        """Rewrite every recorded per-Session digest to match the live rows.

        The administrator covering their tracks would edit these rows too, and doing
        it here is what leaves the signature as the only value still disagreeing.
        """
        self.covered[checkpoint_id] = [
            (session_id, rows[-1].seq, rows[-1].chain_digest)
            for session_id, rows in self.sessions.items()
            if rows
        ]


class FakeCursor:
    """A cursor answering the statements this module's paths send, in memory.

    Dispatch is on statement identity rather than on parsed SQL, so the emulation
    stands in for the cluster only where a real statement was sent and fails loudly
    for anything else.
    """

    def __init__(self, tables: Tables) -> None:
        self._tables = tables
        self._result: list[tuple[object, ...]] = []

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Answer one statement, holding its rows for the fetch that follows."""
        bound = tuple(params or ())
        if query == WINDOW_TIPS_QUERY:
            self._result = self._window_tips(bound)
        elif query == CHAIN_ROWS_QUERY:
            self._result = self._chain_rows(bound)
        elif query == INSERT_CHECKPOINT_STATEMENT:
            self._result = self._insert_checkpoint(bound)
        elif query == INSERT_CHECKPOINT_SESSION_STATEMENT:
            self._result = self._insert_covered(bound)
        elif query == CHECKPOINT_BY_ID_QUERY:
            self._result = self._checkpoint(bound)
        elif query == RECORDED_SESSIONS_QUERY:
            self._result = self._recorded(bound)
        elif query == ACCOUNTING_RUNS_QUERY:
            # No Erasure_Run is recorded anywhere in this module: every change here
            # is an administrator's edit, so nothing accounts for any of it.
            self._result = []
        else:
            raise AssertionError("the emulation was sent a statement it does not answer")
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """The first row of the last statement, or None when it produced none."""
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row of the last statement."""
        return list(self._result)

    def _window_tips(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        start, end = bound[0], bound[1]
        assert isinstance(start, datetime) and isinstance(end, datetime)
        tips: list[tuple[object, ...]] = []
        for session_id, rows in self._tables.sessions.items():
            inside = [row for row in rows if start <= row.occurred_at < end]
            if not inside:
                continue
            terminal = max(inside, key=lambda row: row.seq)
            tips.append((session_id, terminal.seq, terminal.chain_digest))
        return sorted(tips, key=lambda row: str(row[0]))

    def _chain_rows(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        session_id = bound[0]
        assert isinstance(session_id, UUID)
        return [
            (
                row.event_id,
                row.session_id,
                row.client_id,
                row.seq,
                row.category,
                row.occurred_at,
                row.agent_cli,
                row.machine_id,
                row.parent_event_id,
                dict(row.payload),
                row.redacted,
                row.content_digest,
                row.prev_chain_digest,
                row.chain_digest,
            )
            for row in self._tables.sessions.get(session_id, ())
        ]

    def _insert_checkpoint(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        identifier = uuid4()
        created_at = datetime.now(tz=UTC)
        stored = (identifier, *bound, created_at)
        assert len(stored) == CHECKPOINT_COLUMNS
        self._tables.checkpoints[identifier] = stored
        self._tables.covered[identifier] = []
        return [(identifier, created_at)]

    def _insert_covered(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        checkpoint_id, session_id, digest, seq = bound
        assert isinstance(checkpoint_id, UUID) and isinstance(session_id, UUID)
        assert isinstance(digest, str) and isinstance(seq, int)
        self._tables.covered[checkpoint_id].append((session_id, seq, digest))
        return []

    def _checkpoint(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        identifier = bound[0]
        assert isinstance(identifier, UUID)
        stored = self._tables.checkpoints.get(identifier)
        return [] if stored is None else [stored]

    def _recorded(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        identifier = bound[0]
        assert isinstance(identifier, UUID)
        recorded = self._tables.covered.get(identifier, [])
        return [
            (session_id, seq, digest)
            for session_id, seq, digest in sorted(recorded, key=lambda entry: str(entry[0]))
        ]


class FakeStore:
    """The two call shapes the checkpoint and chain paths reach a store through."""

    def __init__(self, tables: Tables) -> None:
        self._tables = tables

    def read(self, body: Callable[[FakeCursor], T]) -> T:
        """Run a read body against one cursor over the emulated tables."""
        return body(FakeCursor(self._tables))

    def in_serializable(self, body: Callable[[FakeCursor], T], *, label: str = "") -> T:
        """Run a write body against one cursor, framing nothing, as no cluster is here."""
        del label
        return body(FakeCursor(self._tables))


# ---------------------------------------------------------------------------
# The scenario every example starts from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scenario:
    """A populated ledger, a signed checkpoint over it, and the key held outside."""

    tables: Tables
    store: MemoryStore
    key: OutsideKey
    stored: StoredCheckpoint
    sessions: tuple[UUID, ...]

    @property
    def target(self) -> UUID:
        """The Session the administrator rewrites."""
        return self.sessions[0]


@pytest.fixture(name="scenario")
def scenario_fixture() -> Scenario:
    """Place several Sessions, then sign one checkpoint covering all of them."""
    tables = Tables()
    store = cast("MemoryStore", FakeStore(tables))
    client_id = uuid4()
    opening = datetime.now(tz=UTC)
    sessions: list[UUID] = []
    for index in range(SESSION_COUNT):
        session_id = uuid4()
        tables.sessions[session_id] = _rows_of(
            session_id,
            client_id,
            opening + index * EVENTS_PER_SESSION * EVENT_SPACING,
            [f"step {position}" for position in range(EVENTS_PER_SESSION)],
        )
        sessions.append(session_id)

    window = CheckpointWindow(
        start=opening - WINDOW_MARGIN,
        end=opening + SESSION_COUNT * EVENTS_PER_SESSION * EVENT_SPACING + WINDOW_MARGIN,
    )
    key = OutsideKey(private=ec.generate_private_key(ec.SECP256R1()))
    stored = sign_and_store(
        store,
        compute(store, window),
        signer=key,
        policy=CheckpointPolicy(
            interval_seconds=INTERVAL_SECONDS,
            kms_key_id=SIGNING_KEY_ID,
            signing_algorithm=SIGNING_ALGORITHM,
        ),
    )
    return Scenario(
        tables=tables,
        store=store,
        key=key,
        stored=stored,
        sessions=tuple(sessions),
    )


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def test_an_untouched_ledger_passes_both_verifications(scenario: Scenario) -> None:
    """Both mechanisms agree before anything is rewritten, so the departures below mean more."""
    for session_id in scenario.sessions:
        report = verify_chain(scenario.store, session_id)
        assert report.ok
        assert report.rows == EVENTS_PER_SESSION

    checkpoint = verify(scenario.store, scenario.stored.checkpoint_id, signer=scenario.key)
    assert checkpoint.agrees
    assert checkpoint.signature_verified
    assert checkpoint.changes == ()
    require_agreement(checkpoint)


# ---------------------------------------------------------------------------
# The rewrite the chain cannot see
# ---------------------------------------------------------------------------


def test_a_consistently_rewritten_session_passes_per_session_chain_verification(
    scenario: Scenario,
) -> None:
    """The rewrite satisfies the chain rule, so the chain reports nothing about it."""
    before = scenario.tables.sessions[scenario.target]
    scenario.tables.rewrite_consistently(scenario.target)
    after = scenario.tables.sessions[scenario.target]

    assert after[-1].payload["body"] == REWRITTEN_BODY
    assert after[-1].chain_digest != before[-1].chain_digest, (
        "the rewrite moved the terminal digest"
    )

    report = verify_chain(scenario.store, scenario.target)
    assert report.ok, "a consistent rewrite is invisible to the per-session chain"
    assert report.first_mismatch_seq is None
    assert report.mismatch is None
    assert report.rows == EVENTS_PER_SESSION
    assert report.terminal_digest == after[-1].chain_digest


def test_the_same_rewrite_fails_checkpoint_verification_and_names_the_session(
    scenario: Scenario,
) -> None:
    """The checkpoint is what notices, and nothing on the record explains it."""
    recorded_digest = scenario.stored.root_digest
    scenario.tables.rewrite_consistently(scenario.target)

    assert verify_chain(scenario.store, scenario.target).ok

    report = verify(scenario.store, scenario.stored.checkpoint_id, signer=scenario.key)
    assert not report.agrees
    assert report.signature_verified, "the signature covers the stored digest, which nobody touched"
    assert report.recomputed_root_digest != recorded_digest
    assert report.changed_sessions == (scenario.target,)
    assert report.accounted_changes == ()
    assert len(report.unaccounted_changes) == 1
    assert report.unaccounted_changes[0].live_digest is not None

    with pytest.raises(CheckpointDisagreementError) as raised:
        require_agreement(report)
    assert raised.value.changed_sessions == (scenario.target,)
    assert raised.value.accounting_runs == ()


# ---------------------------------------------------------------------------
# What the outside signature adds
# ---------------------------------------------------------------------------


def test_rewriting_the_stored_root_digest_too_fails_the_signature_check(
    scenario: Scenario,
) -> None:
    """An administrator who covers their tracks in-cluster holds no signing key.

    Every in-cluster value now agrees with every other: the rewritten Session
    verifies against the chain rule, and the stored root digest matches what the
    live rows recompute to. The one value that does not agree is the signature, and
    it does not agree because it was produced with a key the cluster never held.
    """
    scenario.tables.rewrite_consistently(scenario.target)
    recomputed = compute(scenario.store, scenario.stored.window).root_digest
    scenario.tables.rewrite_stored_root_digest(scenario.stored.checkpoint_id, recomputed)
    scenario.tables.rewrite_recorded_digests(scenario.stored.checkpoint_id)

    report = verify(scenario.store, scenario.stored.checkpoint_id, signer=scenario.key)

    assert verify_chain(scenario.store, scenario.target).ok
    assert report.recomputed_root_digest == report.recorded_root_digest
    assert report.changes == (), "every recorded digest was rewritten to match the live rows"
    assert not report.signature_verified
    assert not report.agrees

    # The fault is named as itself rather than as a moved Session, because a
    # signature that does not stand says nothing about which row moved.
    with pytest.raises(VerificationFailedError):
        require_agreement(report)
