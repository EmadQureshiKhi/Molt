"""Signed checkpoints against a live instance: coverage, agreement, and the two disagreements.

The property suite drives the verifier's arithmetic over many shapes against an
emulated Ledger. This module asserts the four things only a cluster can answer.

**A checkpoint covers every Session holding an Event inside its window, and only
those.** The covered set is read back from the stored per-Session rows rather than
from what the computation returned, because the claim is about what a later
verifier will find. A Session whose Events fall outside the window is present in
the Ledger throughout, so exclusion is a property of the window rather than of the
data available.

**An untouched Ledger verifies.** The root digest is recomputed from live rows by
the same statement that computed it, and the signature is checked against the
retrieved public half, so agreement here is the baseline the two disagreements
below are departures from.

**A retrospective edit is unaccounted for.** The edit performed is the one the
per-Session chain cannot catch: the terminal row's content and both of its digests
are rewritten together, so the chain still verifies against itself afterwards. The
checkpoint is what notices, and nothing on the record explains it, so the refusal
is raised carrying the changed Session.

**An authorised erasure is accounted for.** The run records the Session it touched
and a Disposition per row it removed before the rows are gone, which is what lets
the same digest movement come back explained rather than flagged.

**Validates: Requirements 45.2, 45.3, 45.5, 45.6, 45.7, 45.8, 45.11**
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import count
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.attest.checkpoint import (
    CheckpointPolicy,
    CheckpointWindow,
    StoredCheckpoint,
    compute,
    latest_before,
    recorded_sessions,
    require_agreement,
    sign_and_store,
    verify,
)
from molt.errors import CheckpointDisagreementError
from molt.models.event import Event, EventCategory
from molt.models.session import SessionOutcome
from molt.store import Connection, MemoryStore
from molt.store.chain import LedgerAppend, append_batch, chain_tip
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places directly. The checkpoint owns no Client insert and no
# Session insert, and the governed erasure below is recorded rather than executed,
# so the four erasure rows are placed here too.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, outcome, ended_at) "
    "VALUES (%s, %s, %s, %s, %s, now())"
)
INSERT_REQUEST: Final[str] = (
    "INSERT INTO erasure_request (id, client_id, requester, justification) VALUES (%s, %s, %s, %s)"
)
INSERT_RUN: Final[str] = (
    "INSERT INTO erasure_run (id, request_id, client_id, requester, t_before) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_RUN_SESSION: Final[str] = (
    "INSERT INTO run_session (run_id, session_id, terminal_chain_digest, terminal_seq, row_count) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_DISPOSITION: Final[str] = (
    "INSERT INTO disposition "
    "(run_id, artifact_id, artifact_kind, disposition, reason, selection_reason) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
DELETE_EVENT: Final[str] = "DELETE FROM ledger WHERE id = %s"

# The retrospective edit. The row's content, its content digest, and its chain
# digest move together under the chain's own rule, so the Session's chain still
# verifies against itself afterwards: this is the consistent rewrite a per-Session
# chain cannot detect and a checkpoint can.
REWRITE_TERMINAL_ROW: Final[str] = """
UPDATE ledger
SET text_body = %s,
    content_digest = sha256(%s::STRING),
    chain_digest = sha256(prev_chain_digest || chr(31) || sha256(%s::STRING))
WHERE id = %s
"""

TERMINAL_EVENT_QUERY: Final[str] = (
    "SELECT id FROM ledger WHERE session_id = %s ORDER BY seq DESC LIMIT 1"
)
EVENT_IDS_QUERY: Final[str] = "SELECT id FROM ledger WHERE session_id = %s ORDER BY seq ASC"

# The signing surface every example drives. Neither value reaches a real service,
# and no credential is present in this environment by design.
STUB_KEY_ID: Final[str] = "stub-checkpoint-key"
STUB_ALGORITHM: Final[str] = "ECDSA_SHA_256"
STUB_INTERVAL_SECONDS: Final[int] = 3600

# How the placed Events are laid out. Each Session's Events are spaced by one
# millisecond and each test takes its own slot, so the windows of two tests never
# claim the same instant even though they share one schema.
EVENT_SPACING: Final[timedelta] = timedelta(milliseconds=1)
SLOT_WIDTH: Final[timedelta] = timedelta(hours=1)
WINDOW_MARGIN: Final[timedelta] = timedelta(seconds=1)
ROW_LIFETIME: Final[timedelta] = timedelta(days=90)

# The values the placed rows carry. None is what an assertion turns on.
JURISDICTION: Final[str] = "eu"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"
REQUESTER: Final[str] = "stub-requester"
JUSTIFICATION: Final[str] = "an exercised erasure that the checkpoint must explain"
EVENT_KIND: Final[str] = "event"
HARD_DELETE: Final[str] = "hard_delete"
DISPOSITION_REASON: Final[str] = "sole binding was the erased tenant"
SELECTION_REASON: Final[str] = "event_of_scoped_session"
EDITED_BODY: Final[str] = "the body a later editor substituted"

# Events per placed Session, and how many Sessions the coverage example places.
EVENTS_PER_SESSION: Final[int] = 4
SESSIONS_IN_WINDOW: Final[int] = 3

# The slots the windows of this module are cut from. A module-level counter is what
# keeps two tests in one schema from sharing an instant.
_SLOTS: Final[Iterator[int]] = count(1)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


@dataclass(frozen=True, slots=True)
class StubSigner:
    """A deterministic asymmetric signer standing in for the key service.

    The digest, the key identifier, and the algorithm all contribute, so a
    signature produced for one checkpoint verifies for no other, which is the
    property of the real key the verifier's logic depends on. Nothing here holds a
    credential and nothing leaves the process.
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

    def client(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        self.send(
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:12]}", "Tenant", JURISDICTION),
        )
        return identifier

    def session(self, client_id: UUID) -> UUID:
        """Place one terminal Session of a Client and return its identifier."""
        identifier = uuid4()
        self.send(
            INSERT_SESSION,
            (identifier, client_id, AGENT_CLI, MACHINE_ID, SessionOutcome.SUCCEEDED.value),
        )
        return identifier

    def events(self, client_id: UUID, session_id: UUID, opening: datetime, count_of: int) -> None:
        """Append a contiguous run of Events to one Session, through the chain's own path."""
        expires_at = datetime.now(tz=UTC) + ROW_LIFETIME
        append_batch(
            self.store,
            [
                LedgerAppend(
                    event=Event(
                        id=uuid4(),
                        session_id=session_id,
                        client_id=client_id,
                        category=EventCategory.TOOL_CALL,
                        occurred_at=opening + offset * EVENT_SPACING,
                        agent_cli=AGENT_CLI,
                        machine_id=MACHINE_ID,
                        parent_event_id=None,
                        payload={"step": offset},
                        redacted=False,
                        text_body=f"step {offset}",
                    ),
                    expires_at=expires_at,
                )
                for offset in range(count_of)
            ],
        )

    def event_ids(self, session_id: UUID) -> tuple[UUID, ...]:
        """Every Event identifier one Session still holds, in sequence order."""
        return tuple(UUID(str(row[0])) for row in self.rows(EVENT_IDS_QUERY, (session_id,)))

    def terminal_event(self, session_id: UUID) -> UUID:
        """The identifier of the last Event of one Session's chain."""
        produced = self.rows(TERMINAL_EVENT_QUERY, (session_id,))
        assert len(produced) == 1, "a placed Session holds at least one Event"
        return UUID(str(produced[0][0]))

    def rewrite_terminal_row(self, session_id: UUID) -> None:
        """Rewrite the terminal row's content and both of its digests together."""
        event_id = self.terminal_event(session_id)
        self.send(
            REWRITE_TERMINAL_ROW,
            (EDITED_BODY, EDITED_BODY, EDITED_BODY, event_id),
        )

    def record_erasure(self, client_id: UUID, session_id: UUID, removed: Sequence[UUID]) -> UUID:
        """Record a governed run over one Session, then remove the rows it names.

        The evidence is written before the deletion, which is the order a run
        performs it in: the Dispositions and the per-Session record are what a later
        verifier has left to read once the rows themselves are gone.
        """
        tip = chain_tip(self.store, session_id)
        request_id = uuid4()
        run_id = uuid4()
        self.send(INSERT_REQUEST, (request_id, client_id, REQUESTER, JUSTIFICATION))
        self.send(
            INSERT_RUN,
            (run_id, request_id, client_id, REQUESTER, datetime.now(tz=UTC)),
        )
        self.send(
            INSERT_RUN_SESSION,
            (run_id, session_id, tip.chain_digest, tip.seq, len(removed)),
        )
        for artifact_id in removed:
            self.send(
                INSERT_DISPOSITION,
                (
                    run_id,
                    artifact_id,
                    EVENT_KIND,
                    HARD_DELETE,
                    DISPOSITION_REASON,
                    SELECTION_REASON,
                ),
            )
        for artifact_id in removed:
            self.send(DELETE_EVENT, (artifact_id,))
        return run_id


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema.

    Every migration is applied because the checkpoint tables arrive in a later
    generation than the Ledger they commit to, and the restricting references and
    the privilege revocations arrive later still.
    """
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


def policy() -> CheckpointPolicy:
    """The policy every example signs under, naming no real key."""
    return CheckpointPolicy(
        interval_seconds=STUB_INTERVAL_SECONDS,
        kms_key_id=STUB_KEY_ID,
        signing_algorithm=STUB_ALGORITHM,
    )


def slot_opening() -> datetime:
    """The instant this test's Events begin at, in a slot no other test claims."""
    return datetime.now(tz=UTC) + next(_SLOTS) * SLOT_WIDTH


def taken_over(cluster: Cluster, opening: datetime, closing: datetime) -> StoredCheckpoint:
    """Compute, sign, and store one checkpoint over an explicit window."""
    window = CheckpointWindow(start=opening - WINDOW_MARGIN, end=closing + WINDOW_MARGIN)
    return sign_and_store(
        cluster.store,
        compute(cluster.store, window),
        signer=StubSigner(),
        policy=policy(),
    )


# ---------------------------------------------------------------------------
# Coverage, and agreement on an untouched ledger
# ---------------------------------------------------------------------------


def test_a_checkpoint_covers_every_session_holding_an_event_in_its_window(
    cluster: Cluster,
) -> None:
    """The covered set is the window's Sessions, read back from the stored rows."""
    client_id = cluster.client()
    opening = slot_opening()
    inside: list[UUID] = []
    for index in range(SESSIONS_IN_WINDOW):
        session_id = cluster.session(client_id)
        cluster.events(
            client_id,
            session_id,
            opening + index * EVENTS_PER_SESSION * EVENT_SPACING,
            EVENTS_PER_SESSION,
        )
        inside.append(session_id)
    closing = opening + SESSIONS_IN_WINDOW * EVENTS_PER_SESSION * EVENT_SPACING

    # One Session outside the window, present in the Ledger the whole time, so the
    # exclusion below is the window's doing rather than the data's.
    outside_id = cluster.session(client_id)
    cluster.events(client_id, outside_id, closing + SLOT_WIDTH, EVENTS_PER_SESSION)

    stored = taken_over(cluster, opening, closing)

    covered = recorded_sessions(cluster.store, stored.checkpoint_id)
    assert stored.covered_session_count == SESSIONS_IN_WINDOW
    assert {entry.session_id for entry in covered} == set(inside)
    assert outside_id not in {entry.session_id for entry in covered}
    for entry in covered:
        assert entry.terminal_seq == EVENTS_PER_SESSION
        assert (
            entry.terminal_chain_digest == chain_tip(cluster.store, entry.session_id).chain_digest
        )
    assert stored.kms_key_id == STUB_KEY_ID
    assert stored.signing_algorithm == STUB_ALGORITHM
    assert stored.signature


def test_verification_agrees_on_an_untouched_ledger(cluster: Cluster) -> None:
    """The baseline: nothing moved, so the recomputation and the signature both stand."""
    client_id = cluster.client()
    opening = slot_opening()
    session_id = cluster.session(client_id)
    cluster.events(client_id, session_id, opening, EVENTS_PER_SESSION)
    closing = opening + EVENTS_PER_SESSION * EVENT_SPACING

    stored = taken_over(cluster, opening, closing)
    report = verify(cluster.store, stored.checkpoint_id, signer=StubSigner())

    assert report.agrees
    assert report.signature_verified
    assert report.recomputed_root_digest == stored.root_digest
    assert report.changes == ()
    require_agreement(report)

    # The certificate's lookup reaches this checkpoint by its window end.
    found = latest_before(cluster.store, stored.window.end)
    assert found is not None
    assert found.checkpoint_id == stored.checkpoint_id
    assert found.root_digest == stored.root_digest


# ---------------------------------------------------------------------------
# The two disagreements
# ---------------------------------------------------------------------------


def test_a_retrospective_edit_is_reported_as_unaccounted(cluster: Cluster) -> None:
    """A rewrite whose chain still verifies is caught, and nothing explains it."""
    client_id = cluster.client()
    opening = slot_opening()
    edited_id = cluster.session(client_id)
    untouched_id = cluster.session(client_id)
    cluster.events(client_id, edited_id, opening, EVENTS_PER_SESSION)
    cluster.events(
        client_id,
        untouched_id,
        opening + EVENTS_PER_SESSION * EVENT_SPACING,
        EVENTS_PER_SESSION,
    )
    closing = opening + 2 * EVENTS_PER_SESSION * EVENT_SPACING

    stored = taken_over(cluster, opening, closing)
    cluster.rewrite_terminal_row(edited_id)

    report = verify(cluster.store, stored.checkpoint_id, signer=StubSigner())

    assert not report.agrees
    assert report.signature_verified, "the signature covers the stored digest, which nobody touched"
    assert report.recomputed_root_digest != stored.root_digest
    assert report.changed_sessions == (edited_id,)
    assert report.accounted_changes == ()
    assert len(report.unaccounted_changes) == 1
    assert report.unaccounted_changes[0].live_digest is not None
    with pytest.raises(CheckpointDisagreementError) as raised:
        require_agreement(report)
    assert raised.value.changed_sessions == (edited_id,)
    assert raised.value.accounting_runs == ()


def test_an_authorised_erasure_is_reported_as_accounted(cluster: Cluster) -> None:
    """A governed deletion is explained by the run that recorded it rather than flagged."""
    client_id = cluster.client()
    opening = slot_opening()
    erased_id = cluster.session(client_id)
    cluster.events(client_id, erased_id, opening, EVENTS_PER_SESSION)
    closing = opening + EVENTS_PER_SESSION * EVENT_SPACING

    stored = taken_over(cluster, opening, closing)
    removed = cluster.event_ids(erased_id)[-1:]
    run_id = cluster.record_erasure(client_id, erased_id, removed)

    report = verify(cluster.store, stored.checkpoint_id, signer=StubSigner())

    assert not report.agrees
    assert report.recomputed_root_digest != stored.root_digest
    assert report.changed_sessions == (erased_id,)
    assert report.unaccounted_changes == ()
    assert len(report.accounted_changes) == 1
    assert run_id in report.accounted_changes[0].accounting_runs
    # An explained erasure is reported rather than raised: raising here would flag
    # the governance record that exists to account for the change.
    require_agreement(report)
