"""Property 33: a checkpoint agrees before a change and localises it afterwards.

The claim has two halves and both are asserted against an emulated Ledger rather
than against the cluster, so the property exercises the verifier's own arithmetic
over many shapes instead of one shape a live instance can be driven into.

The emulation holds a chain as a *derived* value: any change to a Session's rows
re-derives that Session's digests from the changed row forward. That is deliberate
and it is what makes the property about the right adversary. A change that left
the digest columns stale is caught by the per-Session chain verifier, which is a
different mechanism and a different property; the change a checkpoint exists to
catch is the one whose chain still verifies against itself, and the emulation
produces exactly that.

The three change selectors are the three the design names. A single-row field
mutation and a whole-Session rewrite are both performed by no run, so each must
leave at least one changed Session that nothing on the record explains. A deletion
performed by an Erasure_Run records what it removed, so the same disagreement must
come back carrying the identifier of that run and must not be raised as a finding.

**Validates: Requirements 45.2, 45.3, 45.6, 45.7, 45.8**
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from molt.attest.checkpoint import (
    ACCOUNTING_RUNS_QUERY,
    CHECKPOINT_BY_ID_QUERY,
    INSERT_CHECKPOINT_SESSION_STATEMENT,
    INSERT_CHECKPOINT_STATEMENT,
    LATEST_BEFORE_QUERY,
    RECORDED_SESSIONS_QUERY,
    WINDOW_TIPS_QUERY,
    CheckpointPolicy,
    CheckpointWindow,
    StoredCheckpoint,
    compute,
    latest_before,
    require_agreement,
    sign_and_store,
    verify,
)
from molt.errors import CheckpointDisagreementError
from molt.store import MemoryStore
from molt.store.chain import GENESIS_PREDECESSOR, chain_digest, sha256_hex

# The shape the design's generator table names for this property.
SESSION_FLOOR: Final[int] = 1
SESSION_CEILING: Final[int] = 50
EVENT_FLOOR: Final[int] = 1
EVENT_CEILING: Final[int] = 200

# The window every example computes over. The events of an example are spaced by
# one millisecond so that a state of the maximum size still falls inside one
# interval of the configured width, and the window opens before the first event
# and closes after the last, so coverage is a property of the construction.
EVENT_SPACING: Final[timedelta] = timedelta(milliseconds=1)
WINDOW_MARGIN: Final[timedelta] = timedelta(seconds=1)

# The signing surface every example drives. Neither value reaches a real service.
STUB_KEY_ID: Final[str] = "stub-checkpoint-key"
STUB_ALGORITHM: Final[str] = "ECDSA_SHA_256"
STUB_INTERVAL_SECONDS: Final[int] = 3600

# The disposition spelling the accounting statement admits for a removed row, as
# the schema's own check constraint spells it.
HARD_DELETE: Final[str] = "hard_delete"

# The result type the emulated store's two call shapes are generic in, matching the
# store they stand in for.
T = TypeVar("T")


class Change(StrEnum):
    """The three ways an example moves the Ledger after the checkpoint is taken."""

    FIELD_MUTATION = "field_mutation"
    SESSION_REWRITE = "session_rewrite"
    ERASURE_DELETION = "erasure_deletion"


# ---------------------------------------------------------------------------
# The stub signer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StubSigner:
    """A deterministic asymmetric signer standing in for the key service.

    The digest, the key identifier, and the algorithm all contribute, so a
    signature produced for one checkpoint verifies for no other, which is the
    property of the real key that the verifier's logic depends on. Nothing here
    holds a credential and nothing leaves the process.
    """

    secret: bytes = b"stub-private-half"

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        """Produce the stand-in signature over a digest."""
        return hashlib.sha256(
            self.secret + key_id.encode("utf-8") + algorithm.encode("utf-8") + digest
        ).digest()

    def public_key(self, *, key_id: str) -> bytes:
        """The stand-in public half, which the verifier is handed rather than trusted."""
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


# ---------------------------------------------------------------------------
# The emulated ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Row:
    """One emulated Ledger row, carrying the fields the checkpoint reads."""

    event_id: UUID
    session_id: UUID
    seq: int
    occurred_at: datetime
    content: str
    chain_digest: str


def _rederived(session_id: UUID, rows: Sequence[Row]) -> tuple[Row, ...]:
    """Re-derive a Session's chain from its content, in sequence order.

    The sequence numbers are re-derived too, so a deletion leaves a contiguous
    chain rather than a gap: the point of the emulation is a Session whose chain
    verifies against itself after the change.
    """
    derived: list[Row] = []
    previous = GENESIS_PREDECESSOR
    for position, row in enumerate(rows, start=1):
        content_digest = sha256_hex(f"{row.event_id}\x1f{position}\x1f{row.content}")
        previous = chain_digest(previous, content_digest)
        derived.append(
            Row(
                event_id=row.event_id,
                session_id=session_id,
                seq=position,
                occurred_at=row.occurred_at,
                content=row.content,
                chain_digest=previous,
            )
        )
    return tuple(derived)


@dataclass(slots=True)
class Ledger:
    """An emulated Ledger with the checkpoint, disposition, and run tables beside it."""

    sessions: dict[UUID, tuple[Row, ...]] = field(default_factory=dict)
    checkpoints: dict[UUID, tuple[object, ...]] = field(default_factory=dict)
    covered: dict[UUID, list[tuple[UUID, int, str]]] = field(default_factory=dict)
    dispositions: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    run_sessions: list[tuple[UUID, UUID]] = field(default_factory=list)

    def rows(self) -> tuple[Row, ...]:
        """Every row of every Session, which is what the window read scans."""
        return tuple(row for rows in self.sessions.values() for row in rows)

    def event_ids(self) -> frozenset[UUID]:
        """Every event identifier the Ledger still holds."""
        return frozenset(row.event_id for row in self.rows())


class FakeCursor:
    """A cursor answering the statements the checkpoint module sends, in memory.

    Dispatch is on statement identity rather than on parsed SQL, so this stands in
    for the cluster only for the statements this module actually issues and fails
    loudly for anything else, which is what keeps the emulation honest.
    """

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._result: list[tuple[object, ...]] = []

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Answer one statement, recording its rows for the fetch that follows."""
        bound = tuple(params or ())
        if query == WINDOW_TIPS_QUERY:
            self._result = self._window_tips(bound)
        elif query == INSERT_CHECKPOINT_STATEMENT:
            self._result = self._insert_checkpoint(bound)
        elif query == INSERT_CHECKPOINT_SESSION_STATEMENT:
            self._result = self._insert_covered(bound)
        elif query == CHECKPOINT_BY_ID_QUERY:
            self._result = self._checkpoint(bound)
        elif query == LATEST_BEFORE_QUERY:
            self._result = self._latest_before(bound)
        elif query == RECORDED_SESSIONS_QUERY:
            self._result = self._covered(bound)
        elif query == ACCOUNTING_RUNS_QUERY:
            self._result = self._accounting_runs(bound)
        else:
            raise AssertionError("the emulation was sent a statement it does not answer")
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """The first row of the last statement, or None when it produced none."""
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row of the last statement."""
        return list(self._result)

    # -- the statements --------------------------------------------------

    def _window_tips(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        start, end = bound[0], bound[1]
        assert isinstance(start, datetime) and isinstance(end, datetime)
        tips: list[tuple[object, ...]] = []
        for session_id, rows in self._ledger.sessions.items():
            inside = [row for row in rows if start <= row.occurred_at < end]
            if not inside:
                continue
            terminal = max(inside, key=lambda row: row.seq)
            tips.append((session_id, terminal.seq, terminal.chain_digest))
        return sorted(tips, key=lambda row: str(row[0]))

    def _insert_checkpoint(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        identifier = uuid4()
        self._ledger.checkpoints[identifier] = (identifier, *bound, datetime.now(tz=UTC))
        self._ledger.covered[identifier] = []
        return [(identifier, datetime.now(tz=UTC))]

    def _insert_covered(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        checkpoint_id, session_id, digest, seq = bound
        assert isinstance(checkpoint_id, UUID) and isinstance(session_id, UUID)
        assert isinstance(digest, str) and isinstance(seq, int)
        self._ledger.covered[checkpoint_id].append((session_id, seq, digest))
        return []

    def _checkpoint(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        identifier = bound[0]
        assert isinstance(identifier, UUID)
        stored = self._ledger.checkpoints.get(identifier)
        return [] if stored is None else [stored]

    def _latest_before(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        moment = bound[0]
        assert isinstance(moment, datetime)
        eligible = [
            stored
            for stored in self._ledger.checkpoints.values()
            if isinstance(stored[2], datetime) and stored[2] <= moment
        ]
        if not eligible:
            return []
        return [max(eligible, key=lambda stored: cast("datetime", stored[2]))]

    def _covered(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        identifier = bound[0]
        assert isinstance(identifier, UUID)
        recorded = self._ledger.covered.get(identifier, [])
        return [
            (session_id, seq, digest)
            for session_id, seq, digest in sorted(recorded, key=lambda entry: str(entry[0]))
        ]

    def _accounting_runs(self, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        session_id = bound[2]
        assert isinstance(session_id, UUID)
        present = self._ledger.event_ids()
        touching = {run for run, touched in self._ledger.run_sessions if touched == session_id}
        session_events = {row.event_id for row in self._ledger.sessions.get(session_id, ())}
        runs: set[UUID] = set()
        for run_id, artifact_id, disposition in self._ledger.dispositions:
            named_directly = artifact_id in session_events
            removed = run_id in touching and artifact_id not in present
            if named_directly or (removed and disposition == HARD_DELETE):
                runs.add(run_id)
        return [(run_id,) for run_id in sorted(runs, key=str)]


class FakeStore:
    """The two call shapes the checkpoint module reaches a store through."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def read(self, body: Callable[[FakeCursor], T]) -> T:
        """Run a read body against one cursor over the emulated tables."""
        return body(FakeCursor(self._ledger))

    def in_serializable(self, body: Callable[[FakeCursor], T], *, label: str = "") -> T:
        """Run a write body against one cursor, framing nothing, as no cluster is here.

        The transaction label is accepted and discarded: it names a conflict window
        in a log record, and no emulated write has one.
        """
        del label
        return body(FakeCursor(self._ledger))


@dataclass(frozen=True, slots=True)
class CheckpointState:
    """A Ledger, a checkpoint taken over it, and the change to apply next."""

    ledger: Ledger
    store: MemoryStore
    stored: StoredCheckpoint
    window: CheckpointWindow
    change: Change


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def _policy() -> CheckpointPolicy:
    """The policy every example signs under, holding no real key identifier."""
    return CheckpointPolicy(
        interval_seconds=STUB_INTERVAL_SECONDS,
        kms_key_id=STUB_KEY_ID,
        signing_algorithm=STUB_ALGORITHM,
    )


@st.composite
def checkpoint_states(draw: st.DrawFn) -> CheckpointState:
    """A Ledger of many Sessions with one checkpoint over all of them, and a change.

    The content of each row is derived from its position rather than drawn, so an
    example of the maximum size stays cheap to build while the shape the property
    names is still reached. What is drawn is the shape itself: how many Sessions,
    how many Events each holds, and which of the three changes to apply.
    """
    session_count = draw(st.integers(min_value=SESSION_FLOOR, max_value=SESSION_CEILING))
    event_counts = draw(
        st.lists(
            st.integers(min_value=EVENT_FLOOR, max_value=EVENT_CEILING),
            min_size=session_count,
            max_size=session_count,
        )
    )
    change = draw(st.sampled_from(tuple(Change)))
    marker = draw(st.text(alphabet="abcdef", min_size=1, max_size=8))

    opening = datetime.now(tz=UTC)
    ledger = Ledger()
    position = 0
    for index, count in enumerate(event_counts):
        session_id = uuid4()
        rows: list[Row] = []
        for offset in range(count):
            position += 1
            rows.append(
                Row(
                    event_id=uuid4(),
                    session_id=session_id,
                    seq=offset + 1,
                    occurred_at=opening + position * EVENT_SPACING,
                    content=f"{marker}-{index}-{offset}",
                    chain_digest=GENESIS_PREDECESSOR,
                )
            )
        ledger.sessions[session_id] = _rederived(session_id, rows)

    window = CheckpointWindow(
        start=opening - WINDOW_MARGIN,
        end=opening + position * EVENT_SPACING + WINDOW_MARGIN,
    )
    store = cast("MemoryStore", FakeStore(ledger))
    stored = sign_and_store(
        store,
        compute(store, window),
        signer=StubSigner(),
        policy=_policy(),
    )
    return CheckpointState(
        ledger=ledger,
        store=store,
        stored=stored,
        window=window,
        change=change,
    )


def _apply(state: CheckpointState, target: UUID) -> None:
    """Apply the example's change to one Session, re-deriving its chain."""
    rows = state.ledger.sessions[target]
    if state.change is Change.FIELD_MUTATION:
        mutated = [
            Row(
                event_id=row.event_id,
                session_id=row.session_id,
                seq=row.seq,
                occurred_at=row.occurred_at,
                content=row.content if index else f"{row.content}-edited",
                chain_digest=row.chain_digest,
            )
            for index, row in enumerate(rows)
        ]
        state.ledger.sessions[target] = _rederived(target, mutated)
        return
    if state.change is Change.SESSION_REWRITE:
        rewritten = [
            Row(
                event_id=row.event_id,
                session_id=row.session_id,
                seq=row.seq,
                occurred_at=row.occurred_at,
                content=f"rewritten-{row.seq}",
                chain_digest=row.chain_digest,
            )
            for row in rows
        ]
        state.ledger.sessions[target] = _rederived(target, rewritten)
        return
    # The governed branch: the run records the Session it touched and a disposition
    # per row it removed, and only then are the rows gone. Recording after the
    # deletion would be the same evidence, but recording first is what a run does.
    run_id = uuid4()
    state.ledger.run_sessions.append((run_id, target))
    removed = rows[-1:] if len(rows) == 1 else rows[len(rows) // 2 :]
    for row in removed:
        state.ledger.dispositions.append((run_id, row.event_id, HARD_DELETE))
    surviving = [row for row in rows if row not in removed]
    state.ledger.sessions[target] = _rederived(target, surviving)


# Feature: molt, Property 33: For any Ledger state of 1 to 50 Sessions with 1 to
# 200 Events each, one computed Ledger_Checkpoint over that window, and any
# single-row mutation or Erasure_Run deletion inside the window, checkpoint
# verification reports agreement before the change and disagreement after it,
# naming exactly the Sessions whose terminal Hash_Chain digest differs from the
# digest recorded at checkpoint time; a deletion performed by an Erasure_Run is
# reported with the identifier of every run whose Dispositions account for it, and
# a mutation performed by no run leaves at least one changed Session unaccounted
# for.
# Validates: Requirements 45.2, 45.3, 45.6, 45.7, 45.8
# No per-example deadline, as everywhere else in this suite: a wall-clock deadline
# fails an example for the load on the machine rather than for the property, which
# under parallel execution reports contention as a correctness failure. Latency
# bounds are stated deliberately in the performance suite.
@settings(max_examples=100, deadline=None)
@given(checkpoint_states())
def test_a_checkpoint_agrees_before_a_change_and_localises_it_afterwards(
    state: CheckpointState,
) -> None:
    checkpoint_id = state.stored.checkpoint_id
    signer = StubSigner()

    # Every Session of the window is covered, and an untouched Ledger agrees.
    before = verify(state.store, checkpoint_id, signer=signer)
    assert state.stored.covered_session_count == len(state.ledger.sessions)
    assert before.agrees
    assert before.recomputed_root_digest == before.recorded_root_digest
    assert before.changes == ()
    assert before.signature_verified
    require_agreement(before)

    # The certificate's own lookup finds this checkpoint at its window end.
    found = latest_before(state.store, state.window.end)
    assert found is not None
    assert found.checkpoint_id == checkpoint_id

    target = sorted(state.ledger.sessions, key=str)[0]
    _apply(state, target)

    after = verify(state.store, checkpoint_id, signer=signer)

    # The root digest moved, the signature still stands, and exactly the changed
    # Session is named: a checkpoint localises rather than merely detecting.
    assert not after.agrees
    assert after.signature_verified
    assert after.recomputed_root_digest != after.recorded_root_digest
    assert after.changed_sessions == (target,)

    if state.change is Change.ERASURE_DELETION:
        # The governed branch is explained: every changed Session carries the run
        # whose dispositions account for it, and nothing is raised as a finding.
        assert after.unaccounted_changes == ()
        assert len(after.accounted_changes) == 1
        assert after.accounting_runs
        require_agreement(after)
    else:
        # The ungoverned branch is the finding, and it is raised carrying both sets.
        assert after.accounted_changes == ()
        assert len(after.unaccounted_changes) == 1
        assert after.accounting_runs == ()
        with pytest.raises(CheckpointDisagreementError) as raised:
            require_agreement(after)
        assert raised.value.changed_sessions == (target,)
        assert raised.value.accounting_runs == ()
