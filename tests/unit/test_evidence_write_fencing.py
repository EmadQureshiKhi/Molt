"""Unit tests for the two evidence writes that reach the fence through their own modules.

Nothing here opens a socket. A scripted cursor answers each statement from a script
and keeps what it was sent, so every claim below is read off the statements the
module produced and the order it produced them in. The claims that need a cluster to
be meaningful, that a concurrent takeover really conflicts with the guard read, live
in the instance-backed modules.

Two writes are covered, and both are evidence about a run rather than memory
content: the Residue_Detector's per-finding recording and the Backup_Manager's
record row. Both frame their own transactions inside the modules that own their
statements, so neither could be fenced by the engine reaching into it; each takes
the generation its worker holds and presents it.

What is asserted of each is the same pair of things. The fenced path is the one
taken: the generation read is sent inside the write's own transaction, after the
isolation level and before the insert, and the insert is sent before the commit. A
read taken in an earlier transaction would satisfy a test that only asserted a
stale generation is refused, and would still admit the row the fence exists to
refuse. And a stale generation writes nothing: the insert is not among the
statements the refused attempt sent at all, no commit follows it, and the refusal
names both generations.

The residue entry point is covered for the decision that made this fixable. Its
fence is optional, because the read-only exposures of the same walk hold no lease
and would have to invent a generation to name one; but a pass that records and
names no fence is refused before the corpus is read, so a recording caller cannot
forget the fence and have the omission read as a pass that found nothing.

**Validates: Requirements 44.8, 44.15**
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.backup import (
    BACKUP_RECORD_LABEL,
    BACKUP_RECORD_STATEMENT,
    NO_COMMAND,
    BackupRecord,
    BackupStatus,
    record_backup,
)
from molt.erase.residue import (
    AUTO_INCLUDE_REASON,
    INSERT_FINDING_STATEMENT,
    FindingFence,
    ResidueBand,
    ResidueFinding,
    ResiduePolicy,
    detect_residue,
    store_recorder,
)
from molt.errors import LeaseNotHeld, StaleFencingGeneration
from molt.models.artifact import ArtifactKind
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.fencing import CURRENT_GENERATION_QUERY, FIRST_GENERATION
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
)

# The fragments the script matches an answer to a statement by.
LEASE_FRAGMENT: Final[str] = "FROM erasure_lease"
FINDING_FRAGMENT: Final[str] = "INSERT INTO residue_candidate"
BACKUP_FRAGMENT: Final[str] = "INSERT INTO backup_record"

# The tenant, the lease, the owner, and the run every example here reads.
CLIENT_ID: Final[UUID] = uuid4()
LEASE_ID: Final[UUID] = uuid4()
OWNER: Final[str] = "worker-a"
RUN_ID: Final[UUID] = uuid4()
ARTIFACT_ID: Final[UUID] = uuid4()
QUERY_ARTIFACT_ID: Final[UUID] = uuid4()

# The generation the scripted lease holds, and the one a superseded owner still
# believes it holds. Distinct values well above the floor, so no assertion is
# satisfied by a coincidence with the floor.
CURRENT: Final[int] = 7
SUPERSEDED: Final[int] = 6

# The distance the recorded finding carries, inside the auto-inclusion band of the
# policy below so the band and the reason agree with each other.
NEAR: Final[float] = 0.05


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()
    error: Exception | None = None


@dataclass(slots=True)
class Script:
    """The answers a connection hands out, consumed in the order they match."""

    answers: list[Answer] = field(default_factory=list)
    sent: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    armed: tuple[tuple[object, ...], ...] = ()

    @property
    def statements(self) -> list[str]:
        """Every statement the script was sent, in order."""
        return [query for query, _ in self.sent]

    @property
    def issued(self) -> list[str]:
        """What the modules sent, with the pool's own setup and reset removed."""
        return [
            query
            for query in self.statements
            if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT)
        ]

    def parameters_of(self, statement: str) -> tuple[object, ...] | None:
        """The bound parameters of the one occurrence of a statement."""
        matches = [params for query, params in self.sent if query == statement]
        assert len(matches) == 1, f"the statement should have been sent once, not {len(matches)}"
        return matches[0]

    def take(self, query: str) -> Answer | None:
        """The next answer matching a statement, removed from the script."""
        for index, answer in enumerate(self.answers):
            if answer.fragment in query:
                return self.answers.pop(index)
        return None


class ScriptedCursor:
    """A cursor answering from a script and recording what it was sent."""

    def __init__(self, script: Script) -> None:
        self._script = script
        self.released = False

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then raise or arm rows as the script says."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        if answer is None:
            self._script.armed = ()
            return None
        if answer.error is not None:
            raise answer.error
        self._script.armed = answer.rows
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or None when the statement armed none."""
        rows = self._script.armed
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._script.armed)

    def close(self) -> None:
        """Mark this cursor released."""
        self.released = True


class ScriptedConnection:
    """A connection handing out scripted cursors over one shared script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Open a recording cursor over this connection's script."""
        return ScriptedCursor(self.script)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def lease_row(generation: int = CURRENT) -> tuple[object, ...]:
    """The row the generation read returns for a Client holding a current lease."""
    return (LEASE_ID, OWNER, generation)


def finding() -> ResidueFinding:
    """One auto-inclusion finding, which is the row the recorder writes."""
    return ResidueFinding(
        artifact_id=ARTIFACT_ID,
        artifact_kind=ArtifactKind.EVENT,
        query_artifact_id=QUERY_ARTIFACT_ID,
        cosine_distance=NEAR,
        band=ResidueBand.AUTO_INCLUDE,
        within_auto_include=True,
        within_review=True,
        included=True,
        decision_reason=AUTO_INCLUDE_REASON,
    )


def skipped_record() -> BackupRecord:
    """One backup record, in the shape an operator-skipped run leaves behind."""
    return BackupRecord(
        run_id=RUN_ID,
        status=BackupStatus.SKIPPED,
        command=NO_COMMAND,
        detail="the operator passed the skip-backup flag",
    )


def policy() -> ResiduePolicy:
    """A policy whose bands are usable, which the entry-point cases need one of."""
    return ResiduePolicy(
        auto_include_threshold=0.1,
        review_threshold=0.3,
        query_limit=10,
        top_k=10,
        excerpt_characters=100,
    )


# ---------------------------------------------------------------------------
# The residue finding write
# ---------------------------------------------------------------------------


def test_a_recorded_finding_reads_the_generation_in_its_own_transaction() -> None:
    """The read joins the write's own transaction, ahead of the insert and before the commit."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),)), Answer(FINDING_FRAGMENT)])
    fence = FindingFence(client_id=CLIENT_ID, generation=CURRENT)

    store_recorder(build_store(script), RUN_ID, fence)(finding())

    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        INSERT_FINDING_STATEMENT,
        COMMIT_STATEMENT,
    ]
    assert script.issued.count(BEGIN_STATEMENT) == 1, "one transaction carries both statements"
    assert script.parameters_of(CURRENT_GENERATION_QUERY) == (CLIENT_ID,)


def test_a_recorded_finding_binds_the_run_and_the_artifact() -> None:
    """The fence is added to the write rather than substituted for what it recorded."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),)), Answer(FINDING_FRAGMENT)])
    fence = FindingFence(client_id=CLIENT_ID, generation=CURRENT)

    store_recorder(build_store(script), RUN_ID, fence)(finding())

    bound = script.parameters_of(INSERT_FINDING_STATEMENT)
    assert bound is not None
    assert bound[0] == RUN_ID
    assert bound[1] == ARTIFACT_ID


def test_a_recorder_holding_a_stale_generation_writes_nothing() -> None:
    """A worker whose lease was taken over records no finding a later run would account for."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),))])
    fence = FindingFence(client_id=CLIENT_ID, generation=SUPERSEDED)

    with pytest.raises(StaleFencingGeneration) as raised:
        store_recorder(build_store(script), RUN_ID, fence)(finding())

    assert raised.value.presented == SUPERSEDED
    assert raised.value.current == CURRENT
    issued = script.issued
    assert INSERT_FINDING_STATEMENT not in issued
    assert COMMIT_STATEMENT not in issued
    assert issued == [BEGIN_STATEMENT, SERIALIZABLE_STATEMENT, CURRENT_GENERATION_QUERY]
    statements = script.statements
    assert statements.index(CURRENT_GENERATION_QUERY) < statements.index(ROLLBACK_STATEMENT)


def test_a_recorder_for_a_client_holding_no_lease_writes_nothing() -> None:
    """No current lease is a finding belonging to no owner, which is a different answer."""
    script = Script(answers=[Answer(LEASE_FRAGMENT)])
    fence = FindingFence(client_id=CLIENT_ID, generation=CURRENT)

    with pytest.raises(LeaseNotHeld, match="no current erasure lease"):
        store_recorder(build_store(script), RUN_ID, fence)(finding())

    assert INSERT_FINDING_STATEMENT not in script.issued
    assert COMMIT_STATEMENT not in script.issued


def test_a_fence_below_the_generation_floor_cannot_be_built() -> None:
    """A value naming no granted lease is refused where the fence is made, not at the write."""
    with pytest.raises(ValueError, match="fencing generation"):
        FindingFence(client_id=CLIENT_ID, generation=FIRST_GENERATION - 1)


# ---------------------------------------------------------------------------
# The residue entry point's decision about the fence
# ---------------------------------------------------------------------------


def test_a_recording_pass_naming_no_fence_is_refused_before_the_corpus_is_read() -> None:
    """A recording caller cannot forget the fence and have that read as finding nothing."""
    script = Script()

    with pytest.raises(ValueError, match="fence"):
        detect_residue(
            build_store(script),
            RUN_ID,
            policy(),
            permitted_clients=(CLIENT_ID,),
        )

    assert script.issued == [], "nothing was asked of the cluster at all"


def test_a_read_only_pass_needs_no_fence_and_writes_nothing() -> None:
    """A caller holding no lease is a real caller, so it is not made to invent a generation."""
    script = Script()

    report = detect_residue(
        build_store(script),
        RUN_ID,
        policy(),
        permitted_clients=(CLIENT_ID,),
        read_only=True,
    )

    assert report.read_only
    assert report.findings == ()
    assert INSERT_FINDING_STATEMENT not in script.issued
    assert BEGIN_STATEMENT not in script.issued, "a read-only pass opens no write transaction"


# ---------------------------------------------------------------------------
# The backup record write
# ---------------------------------------------------------------------------


def test_the_backup_record_reads_the_generation_in_its_own_transaction() -> None:
    """The read joins the write's own transaction, ahead of the insert and before the commit."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),)), Answer(BACKUP_FRAGMENT)])
    record = skipped_record()

    returned = record_backup(build_store(script), record, client_id=CLIENT_ID, generation=CURRENT)

    assert returned is record, "the record is returned unchanged"
    assert script.issued == [
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        CURRENT_GENERATION_QUERY,
        BACKUP_RECORD_STATEMENT,
        COMMIT_STATEMENT,
    ]
    assert script.parameters_of(CURRENT_GENERATION_QUERY) == (CLIENT_ID,)
    assert script.parameters_of(BACKUP_RECORD_STATEMENT) == record.parameters()


def test_a_backup_record_from_a_stale_owner_writes_nothing() -> None:
    """A superseded worker may have written a bucket, and it cannot record that it did."""
    script = Script(answers=[Answer(LEASE_FRAGMENT, (lease_row(),))])

    with pytest.raises(StaleFencingGeneration) as raised:
        record_backup(
            build_store(script), skipped_record(), client_id=CLIENT_ID, generation=SUPERSEDED
        )

    assert raised.value.presented == SUPERSEDED
    assert raised.value.current == CURRENT
    issued = script.issued
    assert BACKUP_RECORD_STATEMENT not in issued
    assert COMMIT_STATEMENT not in issued
    assert issued == [BEGIN_STATEMENT, SERIALIZABLE_STATEMENT, CURRENT_GENERATION_QUERY]


def test_a_backup_record_for_a_client_holding_no_lease_writes_nothing() -> None:
    """A row belonging to no owner is refused, and a certificate finds none to cite."""
    script = Script(answers=[Answer(LEASE_FRAGMENT)])

    with pytest.raises(LeaseNotHeld, match="no current erasure lease"):
        record_backup(
            build_store(script), skipped_record(), client_id=CLIENT_ID, generation=CURRENT
        )

    assert BACKUP_RECORD_STATEMENT not in script.issued
    assert COMMIT_STATEMENT not in script.issued


def test_the_two_fenced_evidence_writes_carry_distinct_labels() -> None:
    """The label is what makes a refused evidence write identifiable in a log record."""
    assert BACKUP_RECORD_LABEL == "backup_record"
    assert BACKUP_RECORD_LABEL != "residue_finding"
