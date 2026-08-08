"""Checkpoint computation, verification, and the certificate's lookup, against live rows.

The signing module beside this one asserts coverage and the two disagreements. This
module asserts the three claims a verifier depends on rather than a signer, and each
of them is a claim about the cluster rather than about arithmetic.

**Computation and verification stand against live rows.** The window read, the root
digest, the stored per-Session digests, and the signature check all run against a
schema holding every migration, so what a later verifier will find is what is
asserted rather than what a computation returned in passing.

**The root digest is a function of content alone.** The same covered set is read a
second time by a statement whose ordering is reversed, and the digest recomputed
from those rows is the digest that was signed. Nothing about the order rows arrive
in reaches the value, which is what lets a verifier holding no knowledge of this
module reproduce it from the stored rows.

**The certificate's lookup returns the checkpoint it should.** Three checkpoints
over disjoint consecutive windows are stored, and the most-recent-before lookup is
asked at each boundary: it returns the checkpoint whose window closed at or before
the instant and never a later one, and it returns nothing for an instant preceding
every window, because a deployment's first erasure may precede its first checkpoint.

**Validates: Requirements 45.2, 45.3, 45.6, 45.7, 45.14, 36.2**
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
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
    SessionDigest,
    StoredCheckpoint,
    compute,
    latest_before,
    recorded_sessions,
    require_agreement,
    root_digest,
    sign_and_store,
    verify,
)
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
# Session insert.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, outcome, ended_at) "
    "VALUES (%s, %s, %s, %s, %s, now())"
)

# The same covered set the checkpoint reads, read in the opposite Session order.
# The grouping is identical and only the direction differs, so a digest recomputed
# from these rows differs from the signed one exactly when the order reaches it.
REVERSED_WINDOW_TIPS_QUERY: Final[str] = """
SELECT DISTINCT ON (session_id) session_id, seq, chain_digest
FROM ledger
WHERE occurred_at >= %s AND occurred_at < %s
ORDER BY session_id DESC, seq DESC
"""

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

# Events per placed Session, how many Sessions the covered set holds, and how many
# consecutive windows the lookup example stores a checkpoint over.
EVENTS_PER_SESSION: Final[int] = 3
SESSIONS_IN_WINDOW: Final[int] = 4
WINDOWS_IN_SERIES: Final[int] = 3

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

    def reversed_tips(self, window: CheckpointWindow) -> tuple[SessionDigest, ...]:
        """The window's covered set, read by the statement whose ordering is reversed."""
        return tuple(
            SessionDigest(
                session_id=UUID(str(row[0])),
                terminal_chain_digest=str(row[2]),
                terminal_seq=int(row[1]),
            )
            for row in self.rows(REVERSED_WINDOW_TIPS_QUERY, (window.start, window.end))
        )


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


def taken_over(cluster: Cluster, window: CheckpointWindow) -> StoredCheckpoint:
    """Compute, sign, and store one checkpoint over an explicit window."""
    return sign_and_store(
        cluster.store,
        compute(cluster.store, window),
        signer=StubSigner(),
        policy=policy(),
    )


def populated_window(cluster: Cluster, client_id: UUID, opening: datetime) -> CheckpointWindow:
    """Place a covered set of Sessions and return the window enclosing it."""
    for index in range(SESSIONS_IN_WINDOW):
        session_id = cluster.session(client_id)
        cluster.events(
            client_id,
            session_id,
            opening + index * EVENTS_PER_SESSION * EVENT_SPACING,
            EVENTS_PER_SESSION,
        )
    closing = opening + SESSIONS_IN_WINDOW * EVENTS_PER_SESSION * EVENT_SPACING
    return CheckpointWindow(start=opening - WINDOW_MARGIN, end=closing + WINDOW_MARGIN)


# ---------------------------------------------------------------------------
# Computation and verification against live rows
# ---------------------------------------------------------------------------


def test_computation_and_verification_stand_against_live_rows(cluster: Cluster) -> None:
    """What a later verifier finds is read back from the cluster rather than returned."""
    client_id = cluster.client()
    window = populated_window(cluster, client_id, slot_opening())

    stored = taken_over(cluster, window)

    assert stored.covered_session_count == SESSIONS_IN_WINDOW
    assert stored.window.start == window.start
    assert stored.window.end == window.end
    assert stored.kms_key_id == STUB_KEY_ID
    assert stored.signing_algorithm == STUB_ALGORITHM

    covered = recorded_sessions(cluster.store, stored.checkpoint_id)
    assert len(covered) == SESSIONS_IN_WINDOW
    for entry in covered:
        live_tip = chain_tip(cluster.store, entry.session_id)
        assert entry.terminal_seq == EVENTS_PER_SESSION
        assert entry.terminal_chain_digest == live_tip.chain_digest

    report = verify(cluster.store, stored.checkpoint_id, signer=StubSigner())
    assert report.agrees
    assert report.signature_verified
    assert report.recomputed_root_digest == stored.root_digest
    assert report.changes == ()
    require_agreement(report)


# ---------------------------------------------------------------------------
# The root digest is a function of content alone
# ---------------------------------------------------------------------------


def test_the_root_digest_is_a_function_of_content_rather_than_of_read_order(
    cluster: Cluster,
) -> None:
    """The same covered set read in the opposite order produces the digest that was signed."""
    client_id = cluster.client()
    window = populated_window(cluster, client_id, slot_opening())

    stored = taken_over(cluster, window)

    forward = compute(cluster.store, window).sessions
    reversed_read = cluster.reversed_tips(window)

    # The two reads see one covered set and disagree only on the order it arrived in,
    # so the recomputation below is a statement about the digest rather than the rows.
    assert len(reversed_read) == SESSIONS_IN_WINDOW
    assert {entry.session_id for entry in reversed_read} == {entry.session_id for entry in forward}
    assert [entry.session_id for entry in reversed_read] != [
        entry.session_id for entry in forward
    ], "the second read arrived in the same order, so this example asserts nothing"

    assert root_digest(reversed_read) == stored.root_digest
    assert root_digest(tuple(reversed(reversed_read))) == stored.root_digest
    assert root_digest(sorted(reversed_read, key=lambda entry: str(entry.terminal_seq))) == (
        stored.root_digest
    )


# ---------------------------------------------------------------------------
# The certificate's most-recent-before lookup
# ---------------------------------------------------------------------------


def test_the_most_recent_before_lookup_returns_the_expected_checkpoint(cluster: Cluster) -> None:
    """The lookup returns the checkpoint whose window closed at or before the instant."""
    client_id = cluster.client()
    opening = slot_opening()
    series: list[StoredCheckpoint] = []
    for index in range(WINDOWS_IN_SERIES):
        window = populated_window(
            cluster,
            client_id,
            opening + index * SLOT_WIDTH,
        )
        series.append(taken_over(cluster, window))

    ends = [stored.window.end for stored in series]
    assert ends == sorted(ends), "the series is stored over consecutive windows"

    # At each window end the lookup names that window's checkpoint, and one instant
    # before it names the previous one, which is the boundary a certificate sits on.
    for index, stored in enumerate(series):
        found = latest_before(cluster.store, stored.window.end)
        assert found is not None
        assert found.checkpoint_id == stored.checkpoint_id
        assert found.root_digest == stored.root_digest
        assert found.covered_session_count == stored.covered_session_count

        earlier = latest_before(cluster.store, stored.window.end - EVENT_SPACING)
        if index == 0:
            assert earlier is None or earlier.window.end < stored.window.end
        else:
            assert earlier is not None
            assert earlier.checkpoint_id == series[index - 1].checkpoint_id

    # An instant preceding every stored window reaches no checkpoint of this series,
    # because a deployment's first erasure may precede its first checkpoint.
    preceding = latest_before(cluster.store, series[0].window.start - SLOT_WIDTH)
    assert preceding is None or preceding.window.end <= series[0].window.start
