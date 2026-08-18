"""The scheduled checkpoint entry point: both of its outcomes, and its evidence.

A new module rather than an addition to `test_console_skeleton.py`, which asserts the
route table, authentication, the adapter, and the *dispatch* of this entry point by
name. What is asserted here is the entry point's own behaviour, which needs a stand-in
store, a stand-in signer, and a telemetry sink that suite has no use for.

Credential-free, cluster-free, and key-service-free. The store is a stand-in whose
cursor answers exactly the three statements the checkpoint component issues, matched
against the statements themselves rather than against copies, so a statement that
changed there would stop being answered here rather than being answered wrongly. The
signer records what it was asked to sign and answers with bytes. Nothing here reaches
a network.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Final, cast
from uuid import UUID

import pytest

from molt.attest.checkpoint import (
    COMPUTED_METRIC,
    INSERT_CHECKPOINT_SESSION_STATEMENT,
    INSERT_CHECKPOINT_STATEMENT,
    WINDOW_TIPS_QUERY,
    CheckpointPolicy,
    SessionDigest,
    root_digest,
)
from molt.config.resolve import Configuration
from molt.console.handler import (
    CHECKPOINT_ENTRY_POINT,
    ENTRY_POINT_KEY,
    NO_SIGNING_KEY_REASON,
    SCHEDULED_TIME_KEY,
    SIGNED_STATUS,
    UNCONFIGURED_STATUS,
    checkpoint_signer,
)
from molt.errors import SigningUnavailableError
from molt.telemetry import configure, current, reset

# Two fixed instants, derived from a count of seconds rather than written as calendar
# readings, so the module carries no date literal.
FIRED: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)
CREATED: Final[datetime] = datetime.fromtimestamp(1_000_000_001, tz=UTC)

INTERVAL_SECONDS: Final[int] = 3600
KEY_ID: Final[str] = "a-provisioned-signing-key"
ALGORITHM: Final[str] = "ECDSA_SHA_256"
SIGNATURE: Final[bytes] = b"a-signature-over-the-root-digest"
CHECKPOINT_ID: Final[UUID] = UUID(int=0x0C)

# Two covered Sessions, so the reported count is a number a wrong wiring could not
# produce by accident.
COVERED: Final[tuple[tuple[UUID, int, str], ...]] = (
    (UUID(int=0x01), 7, hashlib.sha256(b"the first session tip").hexdigest()),
    (UUID(int=0x02), 3, hashlib.sha256(b"the second session tip").hexdigest()),
)


def _covered_digests() -> list[SessionDigest]:
    """The covered set in the shape the root digest is taken over."""
    return [
        SessionDigest(session_id=session_id, terminal_chain_digest=digest, terminal_seq=seq)
        for session_id, seq, digest in COVERED
    ]


class StubCursor:
    """The three statements the checkpoint component issues, and nothing else."""

    def __init__(self) -> None:
        self.window: tuple[object, ...] = ()
        self.checkpoint_row: tuple[object, ...] = ()
        self.session_rows: list[tuple[object, ...]] = []
        self._pending: tuple[object, ...] | None = None
        self._tips = False

    def execute(self, statement: str, parameters: Sequence[object] = ()) -> None:
        if statement == WINDOW_TIPS_QUERY:
            self.window = tuple(parameters)
            self._tips = True
            return
        if statement == INSERT_CHECKPOINT_STATEMENT:
            self.checkpoint_row = tuple(parameters)
            self._pending = (CHECKPOINT_ID, CREATED)
            return
        if statement == INSERT_CHECKPOINT_SESSION_STATEMENT:
            self.session_rows.append(tuple(parameters))
            return
        raise AssertionError(f"the stand-in cursor was given an unread statement: {statement}")

    def fetchall(self) -> list[tuple[object, ...]]:
        assert self._tips, "the covered set was read before the window statement ran"
        return [(session_id, seq, digest) for session_id, seq, digest in COVERED]

    def fetchone(self) -> tuple[object, ...] | None:
        answer = self._pending
        self._pending = None
        return answer


class StubStore:
    """The two ways the checkpoint component reaches a store."""

    role = "eraser"

    def __init__(self) -> None:
        self.cursor = StubCursor()
        self.labels: list[str] = []

    def read(self, body: Callable[[StubCursor], object]) -> object:
        return body(self.cursor)

    def in_serializable(self, body: Callable[[StubCursor], object], *, label: str) -> object:
        self.labels.append(label)
        return body(self.cursor)


class StubSigner:
    """A signer that records the digest it was handed and answers with bytes."""

    def __init__(self) -> None:
        self.signed: list[tuple[bytes, str, str]] = []

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        self.signed.append((digest, key_id, algorithm))
        return SIGNATURE

    def public_key(self, *, key_id: str) -> bytes:
        raise AssertionError(f"the entry point retrieved a public half for {key_id}")

    def verify_digest(
        self,
        digest: bytes,
        signature: bytes,
        *,
        key_id: str,
        algorithm: str,
        public_key: bytes,
    ) -> bool:
        raise AssertionError(
            "the entry point verified rather than signed: "
            f"{len(digest)} digest and {len(signature)} signature bytes under "
            f"{key_id} and {algorithm} against {len(public_key)} public bytes"
        )


class RefusingSigner(StubSigner):
    """A signer whose call fails, standing in for a key service that refused."""

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        self.signed.append((digest, key_id, algorithm))
        raise SigningUnavailableError("the key service refused the signing call")


@pytest.fixture
def sink() -> Iterator[io.StringIO]:
    """Install a process-wide telemetry instance writing to a sink for one test."""
    stream = io.StringIO()
    configure(Configuration(environ={"MOLT_LOG_LEVEL": "debug"}, file_values={}), stream=stream)
    try:
        yield stream
    finally:
        reset()


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    """Every record written to the sink, as read structures."""
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def _emitted() -> set[str]:
    """Every metric name the process-wide instance has published."""
    return {name for name, _ in current().combinations()}


def _policy() -> CheckpointPolicy:
    return CheckpointPolicy(
        interval_seconds=INTERVAL_SECONDS,
        kms_key_id=KEY_ID,
        signing_algorithm=ALGORITHM,
    )


def _event() -> dict[str, object]:
    """The event the scheduled rule delivers, carrying the instant it fired."""
    return {ENTRY_POINT_KEY: CHECKPOINT_ENTRY_POINT, SCHEDULED_TIME_KEY: FIRED.isoformat()}


# -- the unconfigured deployment -------------------------------------------


def test_a_deployment_with_no_signing_key_reports_unconfigured_and_why(sink: io.StringIO) -> None:
    """An unprovisioned surface is reported by name rather than raised or signed.

    The surface is supplied empty rather than left to resolve, so the case states the
    condition it is about instead of depending on what the environment happens to hold.
    """
    answer = checkpoint_signer(_event(), configuration=Configuration(environ={}, file_values={}))

    assert answer["entry_point"] == CHECKPOINT_ENTRY_POINT
    assert answer["status"] == UNCONFIGURED_STATUS
    assert answer["signed"] is False
    assert answer["reason"] == NO_SIGNING_KEY_REASON
    assert "checkpoint_id" not in answer

    reported = [record for record in _records(sink) if record.get("reason")]
    assert [record["reason"] for record in reported] == [NO_SIGNING_KEY_REASON]
    assert COMPUTED_METRIC not in _emitted()


# -- the configured deployment ---------------------------------------------


def test_a_configured_invocation_takes_a_checkpoint_and_names_what_it_took(
    sink: io.StringIO,
) -> None:
    """The response and the record both carry the identifier and the covered count."""
    store = StubStore()
    signer = StubSigner()

    answer = checkpoint_signer(
        _event(),
        signer=cast(Any, signer),
        policy=_policy(),
        store=cast(Any, store),
    )

    assert answer["status"] == SIGNED_STATUS
    assert answer["signed"] is True
    assert answer["checkpoint_id"] == str(CHECKPOINT_ID)
    assert answer["covered_session_count"] == len(COVERED)

    expected = root_digest(_covered_digests())
    assert answer["root_digest"] == expected
    assert signer.signed == [(bytes.fromhex(expected), KEY_ID, ALGORITHM)]

    # One per-Session row per covered Session, so a later disagreement is localisable.
    assert [row[1] for row in store.cursor.session_rows] == [entry[0] for entry in COVERED]

    named = [record for record in _records(sink) if record.get("checkpoint_id")]
    assert str(CHECKPOINT_ID) in {str(record["checkpoint_id"]) for record in named}
    assert len(COVERED) in {record.get("covered_session_count") for record in named}


@pytest.mark.usefixtures("sink")
def test_the_window_closes_at_the_instant_the_rule_fired() -> None:
    """Consecutive scheduled windows meet at the schedule boundary rather than drift."""
    store = StubStore()

    answer = checkpoint_signer(
        _event(),
        signer=cast(Any, StubSigner()),
        policy=_policy(),
        store=cast(Any, store),
    )

    interval = _policy().interval
    assert answer["window_end"] == FIRED.isoformat()
    assert answer["window_start"] == (FIRED - interval).isoformat()
    assert store.cursor.window == (FIRED - interval, FIRED)


@pytest.mark.usefixtures("sink")
def test_one_stored_checkpoint_is_counted_once() -> None:
    """The declared measurement is published exactly once for one taken checkpoint."""
    checkpoint_signer(
        _event(),
        signer=cast(Any, StubSigner()),
        policy=_policy(),
        store=cast(Any, StubStore()),
    )

    assert current().counters()[(COMPUTED_METRIC, ())] == 1.0


# -- a failure is not swallowed --------------------------------------------


def test_a_refused_signing_call_fails_the_invocation_rather_than_reporting_nothing(
    sink: io.StringIO,
) -> None:
    """A failure raises, so the scheduler counts it and its retry takes the next attempt.

    The distinction from the unconfigured case is the whole subject: a key that is
    absent is reported, and a key service that refused the call is raised, so a
    deployment cannot lose checkpoints behind a word that means *not provisioned*.
    """
    with pytest.raises(SigningUnavailableError):
        checkpoint_signer(
            _event(),
            signer=cast(Any, RefusingSigner()),
            policy=_policy(),
            store=cast(Any, StubStore()),
        )

    failed = [record for record in _records(sink) if record.get("error_type")]
    assert [record["error_type"] for record in failed] == [SigningUnavailableError.__name__]
    assert COMPUTED_METRIC not in _emitted()
