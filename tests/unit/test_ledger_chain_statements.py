"""Unit tests for the ledger append statement, its parameters, and the verifier.

Nothing here opens a socket. A recording cursor answers each statement from a
script and keeps what it was sent, so every claim below is read off the statement
the module produced or off rows the test itself constructed. The claims only a
cluster can settle, that a live append really does derive contiguous sequence
numbers and that an altered stored column really is caught where it was altered,
are asserted in the instance-backed module.

Four claims about the append are made here.

The digest commits to a sequence number the same statement derived. The number
reaches the hashed input as an expression over the tip read rather than as a bound
parameter, so nothing outside the statement could have computed the row's digest
in advance, and no digest is among the bound parameters at all.

Every value the append carries is bound. The placeholder count and the parameter
count agree exactly, so a value cannot have been formatted into the statement
text and no identifier can have been either.

The payload is bound as the canonical text that is both stored and hashed, once
per placeholder that mentions it, so a verifier reading the stored payload
reproduces the digest without depending on how the cluster orders keys inside its
own representation.

The verifier is an independent recomputation, and so is this module's check of it.
The digests compared against below are recomputed here from each row's own fields
by a restatement of the rule rather than by calling the module's own helpers, so
agreement is evidence rather than tautology. Every field the digest covers is then
mutated in turn, and each mutation is expected to be reported at the row that
carries it and named by the column that disagreed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt.errors import StoreError
from molt.models.event import EmbeddingState, Event, EventCategory, JsonObject
from molt.store import STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.chain import (
    APPEND_STATEMENT,
    CHAIN_ROWS_QUERY,
    DIGEST_LENGTH,
    GENESIS_PREDECESSOR,
    MISMATCH_CHAIN,
    MISMATCH_CONTENT,
    MISMATCH_PREDECESSOR,
    MISMATCH_SEQUENCE,
    TIP_QUERY,
    UNIT_SEPARATOR,
    ChainRow,
    LedgerAppend,
    append,
    append_batch,
    append_parameters,
    canonical_payload_text,
    chain_tip,
    format_digest_timestamp,
    verify_chain,
    verify_rows,
)
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
)

# The statements the connection surface and the transaction wrapper send on their
# own account: the per-connection setting the pool establishes, the transaction
# control statements, and the reset a returned connection is cleared with. None of
# them belongs to a script, so none of them draws a scripted result.
FRAMING_STATEMENTS: Final[frozenset[str]] = frozenset(
    {
        STATEMENT_TIMEOUT_STATEMENT,
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        COMMIT_STATEMENT,
        ROLLBACK_STATEMENT,
    }
)

# How much the injected clock moves between two Events of a chain, so no two rows
# share an instant and a timestamp mutation is therefore observable.
STEP_SECONDS: Final[float] = 1.5

# The offset of the zone one test builds an instant in, chosen away from the whole
# hour so that a rendering ignoring the offset could not accidentally agree.
AWAY_FROM_UTC: Final[timezone] = timezone(timedelta(hours=5, minutes=30))


class Clock(Protocol):
    """The two calls a test makes on the injected clock.

    The shape is declared structurally rather than imported, because the shared
    fixtures reach a test as a plugin rather than as an importable module path.
    Anything offering an advancing timezone-aware reading satisfies this.
    """

    def now(self) -> datetime:
        """The current wall reading, timezone aware."""

    def advance(self, seconds: float) -> None:
        """Move the reading forward by a non-negative number of seconds."""


# ---------------------------------------------------------------------------
# The recording cursor
# ---------------------------------------------------------------------------


class RecordingCursor:
    """A cursor answering from a script and keeping every statement it was sent."""

    def __init__(self, owner: RecordingConnection) -> None:
        self._owner = owner
        self.released = False

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then arm the next scripted result unless it frames.

        A framing statement is recorded and nothing more. Arming a result for one
        would hand the script's first answer to the pool's session setting or to
        the wrapper's `BEGIN`, and the statement the script was written for would
        then find nothing.
        """
        self._owner.sent.append((query, None if params is None else tuple(params)))
        if query not in FRAMING_STATEMENTS:
            self._owner.armed = self._owner.results.pop(0) if self._owner.results else []
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or nothing when the result is empty."""
        armed = self._owner.armed
        return armed[0] if armed else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._owner.armed)

    def close(self) -> None:
        """Mark this cursor released."""
        self.released = True


class RecordingConnection:
    """A connection handing out recording cursors over one shared script."""

    def __init__(self, results: list[list[tuple[object, ...]]] | None = None) -> None:
        self.sent: list[tuple[str, tuple[object, ...] | None]] = []
        self.results: list[list[tuple[object, ...]]] = [] if results is None else list(results)
        self.armed: list[tuple[object, ...]] = []
        self.closed = False

    def cursor(self) -> RecordingCursor:
        """Open a recording cursor over this connection's script."""
        return RecordingCursor(self)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True

    @property
    def statements(self) -> list[str]:
        """Every statement this connection was sent, in order."""
        return [query for query, _ in self.sent]

    @property
    def scripted_statements(self) -> list[str]:
        """Only the statements a module under test wrote, in order."""
        return [query for query in self.statements if query not in FRAMING_STATEMENTS]

    def parameters_of(self, statement: str) -> tuple[object, ...]:
        """The bound parameters of the one occurrence of a statement."""
        matches = [params for query, params in self.sent if query == statement]
        assert len(matches) == 1, f"the statement should have been sent once, not {len(matches)}"
        bound = matches[0]
        assert bound is not None, "the statement should have carried bound parameters"
        return bound


def build_store(connection: RecordingConnection) -> MemoryStore:
    """A store whose only connection is the recording one, with no waiting."""

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


# ---------------------------------------------------------------------------
# The digest rule, restated independently of the module
# ---------------------------------------------------------------------------


def digest_of(fields: Sequence[str]) -> str:
    """Hash a unit-separated field sequence, as the rule says to."""
    return hashlib.sha256(UNIT_SEPARATOR.join(fields).encode("utf-8")).hexdigest()


def payload_text_of(payload: JsonObject) -> str:
    """Render a payload the way the stored and hashed text is rendered."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def timestamp_text_of(moment: datetime) -> str:
    """Render an instant the way the digest input renders one, in UTC."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00"


def content_digest_of(row: ChainRow) -> str:
    """Recompute one row's content digest from the row's own stored fields."""
    return digest_of(
        [
            str(row.event_id),
            str(row.session_id),
            str(row.client_id),
            str(row.seq),
            row.category,
            timestamp_text_of(row.occurred_at),
            row.agent_cli,
            row.machine_id,
            "" if row.parent_event_id is None else str(row.parent_event_id),
            payload_text_of(row.payload),
            "true" if row.redacted else "false",
        ]
    )


def chain_digest_of(previous: str, content: str) -> str:
    """Recompute the digest linking a row to its predecessor."""
    return digest_of([previous, content])


# ---------------------------------------------------------------------------
# Rows and Events
# ---------------------------------------------------------------------------


def build_event(
    *,
    session_id: UUID,
    client_id: UUID,
    occurred_at: datetime,
    parent_event_id: UUID | None = None,
    payload: JsonObject | None = None,
) -> Event:
    """One observation, with a payload holding unordered and non-ASCII keys."""
    return Event(
        id=uuid4(),
        session_id=session_id,
        client_id=client_id,
        category=EventCategory.TOOL_CALL,
        occurred_at=occurred_at,
        agent_cli="agent",
        machine_id="machine",
        parent_event_id=parent_event_id,
        payload=payload if payload is not None else {"tool": "read", "\u00e9tape": 2, "arg": None},
        redacted=False,
        text_body="a tool call",
    )


def build_request(
    *,
    session_id: UUID,
    client_id: UUID,
    occurred_at: datetime,
    payload: JsonObject | None = None,
) -> LedgerAppend:
    """One append request, with an expiry an interval after the observation."""
    return LedgerAppend(
        event=build_event(
            session_id=session_id,
            client_id=client_id,
            occurred_at=occurred_at,
            payload=payload,
        ),
        expires_at=occurred_at + timedelta(days=90),
        embedding_state=EmbeddingState.PENDING,
    )


def build_chain(*, session_id: UUID, client_id: UUID, length: int, clock: Clock) -> list[ChainRow]:
    """An intact chain of stored rows, with every digest computed by the rule."""
    rows: list[ChainRow] = []
    previous = GENESIS_PREDECESSOR
    for index in range(length):
        moment = clock.now()
        clock.advance(STEP_SECONDS)
        draft = ChainRow(
            event_id=uuid4(),
            session_id=session_id,
            client_id=client_id,
            seq=index + 1,
            category=str(EventCategory.TOOL_CALL),
            occurred_at=moment,
            agent_cli="agent",
            machine_id="machine",
            parent_event_id=None if index == 0 else rows[index - 1].event_id,
            payload={"tool": "read", "path": f"/workspace/file-{index}", "\u00e9tape": index},
            redacted=index % 2 == 1,
            content_digest="",
            prev_chain_digest="",
            chain_digest="",
        )
        content = content_digest_of(draft)
        chain = chain_digest_of(previous, content)
        rows.append(
            replace(
                draft,
                content_digest=content,
                prev_chain_digest=previous,
                chain_digest=chain,
            )
        )
        previous = chain
    return rows


def returned_row(row: ChainRow) -> tuple[object, ...]:
    """One row as the driver hands it back to the read that verification uses.

    The identifier arrives as text and the payload arrives as a JSON document in
    text, because a driver may hand back either representation depending on how it
    was configured, and the verifier reads the chain either way.
    """
    return (
        str(row.event_id),
        row.session_id,
        row.client_id,
        row.seq,
        row.category,
        row.occurred_at,
        row.agent_cli,
        row.machine_id,
        row.parent_event_id,
        payload_text_of(row.payload),
        row.redacted,
        row.content_digest,
        row.prev_chain_digest,
        row.chain_digest,
    )


# ---------------------------------------------------------------------------
# The shape of the append statement
# ---------------------------------------------------------------------------


def test_the_digest_commits_to_the_sequence_number_the_statement_derived() -> None:
    """The hashed sequence number is an expression over the tip read."""
    assert "c.next_seq::STRING" in APPEND_STATEMENT, "the derived number is hashed"
    assert "COALESCE(p.seq, 0) + 1 AS next_seq" in APPEND_STATEMENT
    assert "ORDER BY seq DESC" in APPEND_STATEMENT, "the tip is the highest row"
    assert "COALESCE(p.chain_digest, repeat('0', 64))" in APPEND_STATEMENT
    assert APPEND_STATEMENT.count("sha256(") == 2, "the content and the link are both hashed"
    assert APPEND_STATEMENT.rstrip().endswith(
        "RETURNING seq, content_digest, prev_chain_digest, chain_digest"
    )


def test_every_value_the_append_carries_is_a_bound_parameter(time_source: Clock) -> None:
    """The placeholders and the parameters agree in number, so nothing is inlined."""
    request = build_request(session_id=uuid4(), client_id=uuid4(), occurred_at=time_source.now())

    assert APPEND_STATEMENT.count("%s") == len(append_parameters(request))
    assert str(request.event.id) not in APPEND_STATEMENT, "no identifier reaches the text"
    assert str(request.event.session_id) not in APPEND_STATEMENT


def test_no_digest_is_presented_by_the_caller(time_source: Clock) -> None:
    """A caller binds content and never binds evidence about content."""
    request = build_request(session_id=uuid4(), client_id=uuid4(), occurred_at=time_source.now())

    for value in append_parameters(request):
        assert not (
            isinstance(value, str) and len(value) == DIGEST_LENGTH and _is_hexadecimal(value)
        ), "a digest among the parameters would be a digest the caller chose"


def _is_hexadecimal(value: str) -> bool:
    """Whether text is entirely hexadecimal, the shape a stored digest has."""
    return all(character in "0123456789abcdef" for character in value)


def test_the_payload_is_bound_as_the_canonical_text_that_is_hashed(time_source: Clock) -> None:
    """One canonical rendering is stored and hashed, and its keys are ordered."""
    payload: JsonObject = {"tool": "read", "\u00e9tape": 2, "arg": None}
    request = build_request(
        session_id=uuid4(),
        client_id=uuid4(),
        occurred_at=time_source.now(),
        payload=payload,
    )

    rendered = canonical_payload_text(payload)
    bound = append_parameters(request)

    assert rendered == payload_text_of(payload)
    assert rendered.index('"arg"') < rendered.index('"tool"'), "keys are ordered"
    assert ", " not in rendered and ": " not in rendered, "no insignificant whitespace"
    assert "\u00e9" in rendered, "non-ASCII content is itself rather than escaped"
    assert bound.count(rendered) == 2, "the stored text and the hashed text are one value"


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------


def test_an_append_is_one_statement_in_one_serializable_transaction(time_source: Clock) -> None:
    """The tip read, both digests, and the insert are one round trip."""
    content = digest_of(["content"])
    previous = GENESIS_PREDECESSOR
    chain = chain_digest_of(previous, content)
    connection = RecordingConnection(results=[[(1, content, previous, chain)]])
    request = build_request(session_id=uuid4(), client_id=uuid4(), occurred_at=time_source.now())

    written = append(build_store(connection), request)

    assert written.seq == 1
    assert written.event_id == request.event.id
    assert written.content_digest == content
    assert written.prev_chain_digest == GENESIS_PREDECESSOR
    assert written.chain_digest == chain
    assert connection.scripted_statements == [APPEND_STATEMENT]
    statements = connection.statements
    assert statements.index(BEGIN_STATEMENT) < statements.index(APPEND_STATEMENT)
    assert statements.index(SERIALIZABLE_STATEMENT) < statements.index(APPEND_STATEMENT)
    assert statements.index(APPEND_STATEMENT) < statements.index(COMMIT_STATEMENT)


def test_an_append_returning_no_row_is_reported(time_source: Clock) -> None:
    """A refused insert is reported rather than passed over as a written row."""
    connection = RecordingConnection()
    request = build_request(session_id=uuid4(), client_id=uuid4(), occurred_at=time_source.now())

    with pytest.raises(StoreError, match="no sequence number"):
        append(build_store(connection), request)

    assert COMMIT_STATEMENT not in connection.statements, "nothing was committed"


def test_a_batch_for_one_session_is_a_loop_inside_one_transaction(time_source: Clock) -> None:
    """Each statement sees the row the previous one inserted, in one conflict window."""
    session_id = uuid4()
    client_id = uuid4()
    requests = []
    for _ in range(3):
        requests.append(
            build_request(session_id=session_id, client_id=client_id, occurred_at=time_source.now())
        )
        time_source.advance(STEP_SECONDS)
    results: list[list[tuple[object, ...]]] = [
        [
            (
                index + 1,
                digest_of([f"content-{index}"]),
                digest_of([f"prev-{index}"]),
                digest_of([f"chain-{index}"]),
            )
        ]
        for index in range(3)
    ]
    connection = RecordingConnection(results=results)

    written = append_batch(build_store(connection), requests)

    assert [row.seq for row in written] == [1, 2, 3]
    assert connection.scripted_statements == [APPEND_STATEMENT] * 3
    assert connection.statements.count(BEGIN_STATEMENT) == 1, "one conflict window, not three"
    assert connection.statements.count(COMMIT_STATEMENT) == 1


def test_a_batch_spanning_two_sessions_is_refused(time_source: Clock) -> None:
    """Appends to distinct Sessions share no conflict window, so they share no batch."""
    connection = RecordingConnection()
    moment = time_source.now()
    requests = [
        build_request(session_id=uuid4(), client_id=uuid4(), occurred_at=moment),
        build_request(session_id=uuid4(), client_id=uuid4(), occurred_at=moment),
    ]

    with pytest.raises(ValueError, match="one Session"):
        append_batch(build_store(connection), requests)

    assert connection.statements == []


def test_an_empty_batch_sends_nothing() -> None:
    """No work means no transaction."""
    connection = RecordingConnection()

    assert append_batch(build_store(connection), []) == ()
    assert connection.statements == []


def test_an_expiry_with_no_offset_is_refused(time_source: Clock) -> None:
    """An instant with no offset has no defined position on the timeline."""
    moment = time_source.now()

    with pytest.raises(ValueError, match="timezone aware"):
        LedgerAppend(
            event=build_event(session_id=uuid4(), client_id=uuid4(), occurred_at=moment),
            expires_at=moment.replace(tzinfo=None),
        )


# ---------------------------------------------------------------------------
# The terminal tip
# ---------------------------------------------------------------------------


def test_the_tip_of_a_session_holding_no_row_reports_the_genesis_predecessor() -> None:
    """A caller reads one shape whether the chain has begun or not."""
    connection = RecordingConnection()
    session_id = uuid4()

    tip = chain_tip(build_store(connection), session_id)

    assert tip.session_id == session_id
    assert tip.empty
    assert tip.seq == 0
    assert tip.next_seq == 1
    assert tip.chain_digest == GENESIS_PREDECESSOR
    assert connection.parameters_of(TIP_QUERY) == (session_id,)
    assert BEGIN_STATEMENT not in connection.statements, "a read frames no transaction"


def test_the_tip_reports_the_terminal_sequence_number_and_digest() -> None:
    """The checkpoint reads the last row of the chain, by one index seek."""
    terminal = digest_of(["terminal"])
    connection = RecordingConnection(results=[[(12, terminal)]])

    tip = chain_tip(build_store(connection), uuid4())

    assert tip.seq == 12
    assert tip.next_seq == 13
    assert not tip.empty
    assert tip.chain_digest == terminal
    assert "ORDER BY seq DESC LIMIT 1" in TIP_QUERY


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_the_timestamp_form_is_fixed_and_rendered_in_utc(time_source: Clock) -> None:
    """One instant renders to one text, whatever zone the value carries."""
    moment = time_source.now()
    elsewhere = moment.astimezone(AWAY_FROM_UTC)

    assert format_digest_timestamp(moment) == timestamp_text_of(moment)
    assert format_digest_timestamp(elsewhere) == format_digest_timestamp(moment)
    assert format_digest_timestamp(moment).endswith("+00")


def test_an_intact_chain_reports_the_row_count_and_the_terminal_digest(
    time_source: Clock,
) -> None:
    """Verification recomputes every digest and agrees with the stored evidence."""
    session_id = uuid4()
    rows = build_chain(session_id=session_id, client_id=uuid4(), length=4, clock=time_source)
    connection = RecordingConnection(results=[[returned_row(row) for row in rows]])

    report = verify_chain(build_store(connection), session_id)

    assert report.ok
    assert report.rows == 4
    assert report.terminal_digest == rows[-1].chain_digest
    assert report.first_mismatch_seq is None
    assert report.mismatch is None
    assert connection.parameters_of(CHAIN_ROWS_QUERY) == (session_id,)
    assert "ORDER BY seq ASC" in CHAIN_ROWS_QUERY


def test_a_chain_holding_no_row_verifies_as_empty() -> None:
    """A Session with no Event has an intact chain of no rows."""
    report = verify_rows(uuid4(), [])

    assert report.ok
    assert report.rows == 0
    assert report.terminal_digest == GENESIS_PREDECESSOR


def _forge(text: str) -> str:
    """A digest of the right shape that the rule would not have produced."""
    return digest_of([f"forged-{text}"])


MUTATIONS: Final[tuple[tuple[str, Callable[[ChainRow], ChainRow], str, int], ...]] = (
    (
        "payload",
        lambda row: replace(row, payload={"tool": "write", "path": "/workspace/other"}),
        MISMATCH_CONTENT,
        2,
    ),
    (
        "category",
        lambda row: replace(row, category=str(EventCategory.USER_PROMPT)),
        MISMATCH_CONTENT,
        2,
    ),
    (
        "timestamp",
        lambda row: replace(row, occurred_at=row.occurred_at + timedelta(microseconds=1)),
        MISMATCH_CONTENT,
        2,
    ),
    ("sequence number", lambda row: replace(row, seq=9), MISMATCH_SEQUENCE, 9),
    (
        "content digest",
        lambda row: replace(row, content_digest=_forge("content")),
        MISMATCH_CONTENT,
        2,
    ),
    (
        "predecessor digest",
        lambda row: replace(row, prev_chain_digest=GENESIS_PREDECESSOR),
        MISMATCH_PREDECESSOR,
        2,
    ),
    (
        "chain digest",
        lambda row: replace(row, chain_digest=_forge("chain")),
        MISMATCH_CHAIN,
        2,
    ),
)


@pytest.mark.parametrize(
    ("mutate", "expected_column", "expected_seq"),
    [(mutate, column, seq) for _, mutate, column, seq in MUTATIONS],
    ids=[name for name, _, _, _ in MUTATIONS],
)
def test_an_altered_field_is_reported_at_the_row_that_carries_it(
    mutate: Callable[[ChainRow], ChainRow],
    expected_column: str,
    expected_seq: int,
    time_source: Clock,
) -> None:
    """Every field the digest covers is covered, the digest columns included."""
    session_id = uuid4()
    rows = build_chain(session_id=session_id, client_id=uuid4(), length=3, clock=time_source)
    rows[1] = mutate(rows[1])

    report = verify_rows(session_id, rows)

    assert not report.ok
    assert report.mismatch == expected_column
    assert report.first_mismatch_seq == expected_seq


def test_a_mismatch_reports_how_far_the_chain_held(time_source: Clock) -> None:
    """The report says which prefix verified, so an auditor learns where to look."""
    session_id = uuid4()
    rows = build_chain(session_id=session_id, client_id=uuid4(), length=3, clock=time_source)
    intact = rows[0]
    rows[1] = replace(rows[1], payload={"tool": "write"})

    report = verify_rows(session_id, rows)

    assert not report.ok
    assert report.rows == 1
    assert report.terminal_digest == intact.chain_digest
