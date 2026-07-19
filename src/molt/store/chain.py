"""The Ledger hash chain: one-statement append, terminal tip, and verification.

The Ledger is the append-only system of record, and its tamper evidence is a
per-Session chain of digests. Three shapes carry that evidence, and each is
load-bearing rather than stylistic.

**An append is one statement.** The statement reads the current tip of the
Session's chain, derives the next sequence number and the predecessor digest from
that read, computes the row's content digest and its chain digest with the
cluster's own hash function, and inserts the row, all inside the transaction that
will commit it. Nothing reads a previously written digest in one round trip and
writes it back in another, so there is no window in which a digest exists outside
a transaction and no opportunity for a caller to present a digest it computed
itself. The sequence number the digest commits to is derived by the same
statement, which is what makes a client-side pre-computation impossible.

**The payload contributes as canonical text rather than as the column type.** The
text bound for the payload is the canonical JSON form the Event serialiser
produces: keys sorted at every level, no insignificant whitespace, no escaping of
non-ASCII content. That one value is both stored in the JSON column and hashed as
text by the same statement, so a verifier written in Python reproduces the digest
from the stored payload without depending on how the cluster orders keys inside
its own JSON representation. The rendering is also chosen so that reading the
stored payload back and rendering it again produces the same characters, because a
number the column returns in a different but equal form would otherwise make an
untouched row read as altered. The timestamp contributes in a fixed textual form at
microsecond precision with a numeric offset, rendered by the cluster in UTC, so
the digest is a function of the instant rather than of a session setting or a
client locale. Fields are joined with the unit separator, which no field can
contain, so no two distinct field sequences share one digest input.

**The genesis predecessor is sixty-four zero characters.** Every row therefore
carries a non-null predecessor digest, which keeps the one-successor-per-
predecessor uniqueness constraint total: it forbids two rows in one Session from
claiming the same predecessor, and so forbids the chain from forking into a tree.
Together with the per-Session sequence uniqueness constraint, the invariant is
structural rather than probabilistic.

**Concurrency is the isolation level plus those two constraints.** Two concurrent
appends to one Session read the same tip; under SERIALIZABLE the second commit
conflicts on that read and aborts, and the retry re-reads the new tip and produces
the following sequence number. Appends to different Sessions never conflict,
because the read set is one Session's index span, which is what lets many
machines write at once without a shared lock. A batch for one Session is a loop of
the same statement inside one transaction: each statement sees the row the
previous one inserted, so the batch is contiguous and the whole batch shares a
single conflict window.

**Verification is an independent recomputation in Python.** It re-derives the
rule from the stored columns rather than re-reading the cluster's answer, so an
alteration to any stored field the digest covers — the payload, the category, the
timestamp, the sequence number, or a digest column itself — surfaces as a
mismatch at that row. An intact chain is reported with the verified row count and
the terminal digest, which is what a checkpoint commits to.

Every value a caller supplies is a bound parameter and no identifier is ever
interpolated: each statement here is a whole module-level literal.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from molt.errors import StoreError
from molt.models.event import (
    EmbeddingState,
    Event,
    JsonObject,
    JsonValue,
    decode_capture_payload,
    require_aware,
)
from molt.store import Cursor, MemoryStore

__all__ = [
    "APPEND_STATEMENT",
    "CHAIN_ROWS_QUERY",
    "DIGEST_LENGTH",
    "EMPTY_TIP_SEQUENCE",
    "FIRST_SEQUENCE",
    "GENESIS_PREDECESSOR",
    "MISMATCH_CHAIN",
    "MISMATCH_CONTENT",
    "MISMATCH_PREDECESSOR",
    "MISMATCH_SEQUENCE",
    "TIP_QUERY",
    "UNIT_SEPARATOR",
    "AppendedRow",
    "ChainReport",
    "ChainRow",
    "ChainTip",
    "LedgerAppend",
    "append",
    "append_batch",
    "append_in_transaction",
    "append_parameters",
    "canonical_payload_text",
    "chain_digest",
    "chain_rows",
    "chain_tip",
    "content_digest_input",
    "format_digest_timestamp",
    "sha256_hex",
    "verify_chain",
    "verify_rows",
]

# The separator between fields of a digest input. A control character is used
# because no captured field can contain one: an identifier, a category, a
# timestamp rendering, and a canonical JSON document are all free of it, so the
# concatenation is unambiguous and no two distinct field sequences collide.
UNIT_SEPARATOR: Final[str] = "\x1f"

# The predecessor digest the first row of every chain carries. A fixed value
# rather than an absent one is what keeps the predecessor column non-null and the
# uniqueness constraint over it total.
DIGEST_LENGTH: Final[int] = 64
GENESIS_PREDECESSOR: Final[str] = "0" * DIGEST_LENGTH

# Sequence numbers begin at one, so a tip reporting zero means the Session holds
# no row yet and the next append is the genesis row of its chain.
FIRST_SEQUENCE: Final[int] = 1
EMPTY_TIP_SEQUENCE: Final[int] = 0

# The textual timestamp form of the digest input, as the cluster renders it. The
# cluster renders this pattern in UTC with the numeric offset spelled as two
# digits, and the Python renderer below produces the same characters, which is
# what lets an independent recomputation reproduce the digest.
_TIMESTAMP_OFFSET: Final[str] = "+00"

# How a boolean contributes to a digest input, matching the cluster's own text
# rendering of the type.
_TRUE_TEXT: Final[str] = "true"
_FALSE_TEXT: Final[str] = "false"

# The value a null parent reference contributes, so an Event with no parent and an
# Event whose parent is absent cannot be told apart by the digest only if they are
# the same thing, which they are.
_ABSENT_PARENT: Final[str] = ""

# The one statement an append is. The tip read, the sequence derivation, both
# digests, and the insert are one round trip inside one transaction, so no digest
# is ever computed from state read in an earlier operation.
#
# The placeholders appear in the order `append_parameters` builds them, and a
# value bound more than once is bound once per appearance: the statement text
# carries no interpolated value and no interpolated identifier.
APPEND_STATEMENT: Final[str] = """
WITH anchor AS (SELECT 1 AS n),
prev AS (
    SELECT seq, chain_digest
    FROM ledger
    WHERE session_id = %s
    ORDER BY seq DESC
    LIMIT 1
),
computed AS (
    SELECT COALESCE(p.seq, 0) + 1 AS next_seq,
           COALESCE(p.chain_digest, repeat('0', 64)) AS prev_chain_digest
    FROM anchor AS a LEFT JOIN prev AS p ON true
),
content AS (
    SELECT c.next_seq,
           c.prev_chain_digest,
           sha256(concat_ws(chr(31),
               %s::STRING,
               %s::STRING,
               %s::STRING,
               c.next_seq::STRING,
               %s::STRING,
               to_char(%s::TIMESTAMPTZ, 'YYYY-MM-DD"T"HH24:MI:SS.USOF'),
               %s::STRING,
               %s::STRING,
               COALESCE(%s::STRING, ''),
               %s::STRING,
               %s::STRING
           )) AS content_digest
    FROM computed AS c
)
INSERT INTO ledger (
    id, session_id, client_id, seq, category, occurred_at, agent_cli, machine_id,
    parent_event_id, payload, redacted, text_body,
    content_digest, prev_chain_digest, chain_digest, embedding_state, expires_at)
SELECT %s, %s, %s, k.next_seq, %s, %s, %s, %s, %s, %s::JSONB, %s::BOOL, %s,
       k.content_digest,
       k.prev_chain_digest,
       sha256(k.prev_chain_digest || chr(31) || k.content_digest),
       %s, %s
FROM content AS k
RETURNING seq, content_digest, prev_chain_digest, chain_digest
"""

# The terminal tip of one Session's chain, served by the ascending sequence index
# read backwards, so the tip costs one index seek rather than a scan.
TIP_QUERY: Final[str] = (
    "SELECT seq, chain_digest FROM ledger WHERE session_id = %s ORDER BY seq DESC LIMIT 1"
)

# Every column the independent recomputation needs, in ascending sequence order.
# The digest columns are read alongside the fields they cover, because the
# recomputation compares against them rather than trusting them.
CHAIN_ROWS_QUERY: Final[str] = """
SELECT id, session_id, client_id, seq, category, occurred_at, agent_cli, machine_id,
       parent_event_id, payload, redacted, content_digest, prev_chain_digest, chain_digest
FROM ledger
WHERE session_id = %s
ORDER BY seq ASC
"""

# What a transaction is called in a log record when it conflicts and retries.
_APPEND_LABEL: Final[str] = "ledger_append"
_BATCH_LABEL: Final[str] = "ledger_append_batch"

# The stored fields a mismatch is reported against, named rather than described
# so a report localises the disagreement.
MISMATCH_SEQUENCE: Final[str] = "seq"
MISMATCH_CONTENT: Final[str] = "content_digest"
MISMATCH_PREDECESSOR: Final[str] = "prev_chain_digest"
MISMATCH_CHAIN: Final[str] = "chain_digest"


# ---------------------------------------------------------------------------
# The digest rule, as a pure function of content
# ---------------------------------------------------------------------------


def sha256_hex(text: str) -> str:
    """Return the hexadecimal digest of text, encoded as UTF-8 first.

    The encoding is fixed here rather than left to a caller, because the digest
    commits to bytes and two callers encoding differently would produce two
    digests for one logical input.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def format_digest_timestamp(value: datetime) -> str:
    """Render an instant in the digest's fixed textual form.

    The instant is converted to UTC and rendered at microsecond precision with a
    numeric offset, which is the form the cluster's own rendering produces for the
    same value. Each component is written with an explicit width rather than
    through a locale-aware formatter, so the characters do not depend on the
    platform the verifier runs on.
    """
    moment = require_aware(value, "a digest timestamp").astimezone(UTC)
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        f".{moment.microsecond:06d}{_TIMESTAMP_OFFSET}"
    )


def canonical_payload_text(payload: Mapping[str, object]) -> str:
    """Render a payload in the canonical text form that is stored and hashed.

    The rules are the Event wire form's own: keys ordered at every level, no
    insignificant whitespace, non-ASCII content left as itself rather than
    escaped, and a non-finite number refused rather than rendered. The same text
    is inserted into the JSON column and hashed, so a verifier reproduces the
    digest from the stored payload without depending on the cluster's internal key
    ordering.

    One further rule makes the text a fixed point of the stored representation,
    and it is what keeps verification free of false findings. The JSON column
    holds a number as an exact decimal and renders it back in that decimal's own
    form, so a number whose value is whole is read back as a whole number however
    it was written: a real written with an exponent, say a mantissa of seventeen
    digits scaled up, comes back as the digits alone. Text hashed in one form and
    recomputed from the other would disagree at a row nobody had touched, which is
    the one failure an audit chain must not have. Rendering a whole-valued real as
    its digits up front removes the difference, because the digits are what the
    column returns.
    """
    document: JsonObject = _stable_document(decode_capture_payload(payload))
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _stable_document(document: JsonObject) -> JsonObject:
    """Return a payload whose numbers survive the stored representation unchanged."""
    return {key: _stable_value(value) for key, value in document.items()}


def _stable_value(value: JsonValue) -> JsonValue:
    """Render a whole-valued real as a whole number, at every level of a document.

    A boolean is tested for before a number because a boolean is a number in
    Python and rendering one as its integer would change what the payload says.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        return {str(key): _stable_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    return value


def content_digest_input(
    *,
    event_id: UUID,
    session_id: UUID,
    client_id: UUID,
    seq: int,
    category: str,
    occurred_at: datetime,
    agent_cli: str,
    machine_id: str,
    parent_event_id: UUID | None,
    payload_text: str,
    redacted: bool,
) -> str:
    """Assemble the unit-separated digest input for one Ledger row.

    The field order and the rendering of each field mirror what the append
    statement hashes, so this function and that statement are two expressions of
    one rule. The sequence number is part of the input, which is why a row's
    digest cannot be computed before the statement that derives that number.
    """
    fields = (
        str(event_id),
        str(session_id),
        str(client_id),
        str(seq),
        category,
        format_digest_timestamp(occurred_at),
        agent_cli,
        machine_id,
        _ABSENT_PARENT if parent_event_id is None else str(parent_event_id),
        payload_text,
        _TRUE_TEXT if redacted else _FALSE_TEXT,
    )
    return UNIT_SEPARATOR.join(fields)


def chain_digest(prev_chain_digest: str, content_digest: str) -> str:
    """Return the chain digest linking a row to its predecessor."""
    return sha256_hex(prev_chain_digest + UNIT_SEPARATOR + content_digest)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerAppend:
    """One Event to append, with the two row fields the Event does not carry.

    The expiry is a row field rather than an Event field because it follows from
    the Client's retention interval rather than from the observation, and the
    embedding state is a row field because it describes work owed on the row
    rather than anything observed.
    """

    event: Event
    expires_at: datetime
    embedding_state: EmbeddingState = EmbeddingState.NOT_REQUIRED

    def __post_init__(self) -> None:
        """Refuse an expiry with no offset, which would have no defined instant."""
        require_aware(self.expires_at, "a row expiry")
        EmbeddingState(self.embedding_state)


@dataclass(frozen=True, slots=True)
class AppendedRow:
    """What the append statement derived and committed for one row."""

    event_id: UUID
    session_id: UUID
    seq: int
    content_digest: str
    prev_chain_digest: str
    chain_digest: str


@dataclass(frozen=True, slots=True)
class ChainTip:
    """The terminal state of one Session's chain, as a checkpoint reads it."""

    session_id: UUID
    seq: int
    chain_digest: str

    @property
    def empty(self) -> bool:
        """Whether the Session holds no Ledger row yet."""
        return self.seq == EMPTY_TIP_SEQUENCE

    @property
    def next_seq(self) -> int:
        """The sequence number the next append to this Session will carry."""
        return self.seq + 1


@dataclass(frozen=True, slots=True)
class ChainRow:
    """One stored Ledger row, holding the fields the digest rule covers."""

    event_id: UUID
    session_id: UUID
    client_id: UUID
    seq: int
    category: str
    occurred_at: datetime
    agent_cli: str
    machine_id: str
    parent_event_id: UUID | None
    payload: JsonObject
    redacted: bool
    content_digest: str
    prev_chain_digest: str
    chain_digest: str


@dataclass(frozen=True, slots=True)
class ChainReport:
    """The outcome of verifying one Session's chain.

    Attributes:
        session_id: The Session whose chain was recomputed.
        ok: Whether every row agreed with the recomputation.
        rows: How many rows verified. On a mismatch this is the count of rows
            that verified before it, so the report says how far the chain held.
        terminal_digest: The chain digest of the last verified row, or the genesis
            predecessor when no row verified.
        first_mismatch_seq: The sequence number of the first row that disagreed,
            or None when the chain is intact.
        mismatch: Which stored field disagreed, or None when the chain is intact.
    """

    session_id: UUID
    ok: bool
    rows: int
    terminal_digest: str
    first_mismatch_seq: int | None = None
    mismatch: str | None = None


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------


def append_parameters(request: LedgerAppend) -> tuple[object, ...]:
    """Build the bound parameters of the append statement, in placeholder order.

    A value the statement mentions twice is bound twice, once per placeholder, so
    the statement text stays a literal. The payload is bound as the canonical text
    that is both stored and hashed, and the category and the embedding state are
    bound as plain text rather than as enumeration members, so the driver adapts a
    value of a type it knows.
    """
    event = request.event
    payload_text = canonical_payload_text(event.payload)
    category = str(event.category)
    occurred_at = require_aware(event.occurred_at, "an Event timestamp")
    return (
        # The tip read.
        event.session_id,
        # The digest input.
        event.id,
        event.session_id,
        event.client_id,
        category,
        occurred_at,
        event.agent_cli,
        event.machine_id,
        event.parent_event_id,
        payload_text,
        event.redacted,
        # The inserted row.
        event.id,
        event.session_id,
        event.client_id,
        category,
        occurred_at,
        event.agent_cli,
        event.machine_id,
        event.parent_event_id,
        payload_text,
        event.redacted,
        event.text_body,
        str(request.embedding_state),
        request.expires_at,
    )


def append_in_transaction(cursor: Cursor, request: LedgerAppend) -> AppendedRow:
    """Append one Event with the single statement, inside the caller's transaction.

    This is the entry point for a caller that must append inside a transaction of
    its own: writing a spawning Event and the child Session it spawns is one such
    case, because the reference between them is checked per statement rather than
    at commit.

    The statement returns exactly one row. No returned row means the insert
    selected nothing, which the shape of the statement makes impossible unless the
    row was refused, so it is reported rather than passed over.
    """
    cursor.execute(APPEND_STATEMENT, append_parameters(request))
    row = cursor.fetchone()
    if row is None or len(row) != 4:
        raise StoreError(
            "the ledger append returned no row, so no sequence number and no digest were derived"
        )
    return AppendedRow(
        event_id=request.event.id,
        session_id=request.event.session_id,
        seq=_as_int(row[0], "seq"),
        content_digest=_as_digest(row[1], "content_digest"),
        prev_chain_digest=_as_digest(row[2], "prev_chain_digest"),
        chain_digest=_as_digest(row[3], "chain_digest"),
    )


def append(store: MemoryStore, request: LedgerAppend) -> AppendedRow:
    """Append one Event in one SERIALIZABLE transaction, retrying a conflict."""

    def body(cursor: Cursor) -> AppendedRow:
        return append_in_transaction(cursor, request)

    return store.in_serializable(body, label=_APPEND_LABEL)


def append_batch(store: MemoryStore, requests: Sequence[LedgerAppend]) -> tuple[AppendedRow, ...]:
    """Append several Events for one Session inside one transaction.

    The loop is the same statement each time, so each append sees the row the
    previous one inserted and the batch is contiguous. One transaction means one
    conflict window for the whole batch rather than one per Event, and a conflict
    re-runs the whole loop, which is safe because nothing it wrote was committed.

    A batch spanning more than one Session is refused: appends to distinct
    Sessions are independent by design, and joining them into one transaction
    would make them conflict with each other for no reason.
    """
    if not requests:
        return ()
    sessions = {request.event.session_id for request in requests}
    if len(sessions) != 1:
        raise ValueError(
            "a batch append covers one Session, because appends to distinct Sessions "
            f"share no conflict window; {len(sessions)} Sessions were given"
        )

    def body(cursor: Cursor) -> tuple[AppendedRow, ...]:
        return tuple(append_in_transaction(cursor, request) for request in requests)

    return store.in_serializable(body, label=_BATCH_LABEL)


# ---------------------------------------------------------------------------
# Reading the tip and the rows
# ---------------------------------------------------------------------------


def chain_tip(store: MemoryStore, session_id: UUID) -> ChainTip:
    """Return the terminal sequence number and chain digest for one Session.

    A Session holding no row reports sequence zero and the genesis predecessor,
    so a caller reads one shape whether the chain has begun or not.
    """

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(TIP_QUERY, (session_id,))
        return cursor.fetchone()

    row = store.read(body)
    if row is None:
        return ChainTip(
            session_id=session_id,
            seq=EMPTY_TIP_SEQUENCE,
            chain_digest=GENESIS_PREDECESSOR,
        )
    return ChainTip(
        session_id=session_id,
        seq=_as_int(row[0], "seq"),
        chain_digest=_as_digest(row[1], "chain_digest"),
    )


def chain_rows(store: MemoryStore, session_id: UUID) -> tuple[ChainRow, ...]:
    """Read every stored row of one Session's chain in ascending sequence order."""

    def body(cursor: Cursor) -> list[tuple[object, ...]]:
        cursor.execute(CHAIN_ROWS_QUERY, (session_id,))
        return cursor.fetchall()

    return tuple(_row_of(row) for row in store.read(body))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_rows(session_id: UUID, rows: Sequence[ChainRow]) -> ChainReport:
    """Recompute a chain from stored rows and report the first disagreement.

    The recomputation derives the rule again rather than reading the cluster's
    answer: each row's content digest is computed from the row's own fields, and
    each chain digest is computed from the recomputed predecessor. An alteration to
    any covered field, or to a digest column itself, therefore surfaces at the row
    it was made in.
    """
    previous = GENESIS_PREDECESSOR
    expected_seq = FIRST_SEQUENCE
    verified = 0
    for row in rows:
        content = sha256_hex(
            content_digest_input(
                event_id=row.event_id,
                session_id=row.session_id,
                client_id=row.client_id,
                seq=row.seq,
                category=row.category,
                occurred_at=row.occurred_at,
                agent_cli=row.agent_cli,
                machine_id=row.machine_id,
                parent_event_id=row.parent_event_id,
                payload_text=canonical_payload_text(row.payload),
                redacted=row.redacted,
            )
        )
        chain = chain_digest(previous, content)
        mismatch = _mismatch_of(
            row,
            expected_seq=expected_seq,
            predecessor=previous,
            content=content,
            chain=chain,
        )
        if mismatch is not None:
            return ChainReport(
                session_id=session_id,
                ok=False,
                rows=verified,
                terminal_digest=previous,
                first_mismatch_seq=row.seq,
                mismatch=mismatch,
            )
        previous = chain
        expected_seq += 1
        verified += 1
    return ChainReport(
        session_id=session_id,
        ok=True,
        rows=verified,
        terminal_digest=previous,
    )


def verify_chain(store: MemoryStore, session_id: UUID) -> ChainReport:
    """Read one Session's chain and verify it by independent recomputation."""
    return verify_rows(session_id, chain_rows(store, session_id))


def _mismatch_of(
    row: ChainRow,
    *,
    expected_seq: int,
    predecessor: str,
    content: str,
    chain: str,
) -> str | None:
    """Name the first stored field of a row that disagrees, or None when all agree.

    The sequence number is checked first because a gap or a repetition makes every
    later comparison a consequence rather than a finding, and naming the cause is
    more use to a reader than naming its effect.
    """
    if row.seq != expected_seq:
        return MISMATCH_SEQUENCE
    if row.content_digest != content:
        return MISMATCH_CONTENT
    if row.prev_chain_digest != predecessor:
        return MISMATCH_PREDECESSOR
    if row.chain_digest != chain:
        return MISMATCH_CHAIN
    return None


# ---------------------------------------------------------------------------
# Row narrowing
# ---------------------------------------------------------------------------


def _row_of(row: Sequence[object]) -> ChainRow:
    """Narrow one returned row into the shape the recomputation reads.

    Every column is checked rather than assumed, so a driver returning an
    unexpected representation is reported by column name instead of failing later
    inside the digest rule.
    """
    if len(row) != 14:
        raise StoreError(f"a ledger row returned {len(row)} column(s) where 14 are read")
    return ChainRow(
        event_id=_as_uuid(row[0], "id"),
        session_id=_as_uuid(row[1], "session_id"),
        client_id=_as_uuid(row[2], "client_id"),
        seq=_as_int(row[3], "seq"),
        category=_as_text(row[4], "category"),
        occurred_at=_as_moment(row[5], "occurred_at"),
        agent_cli=_as_text(row[6], "agent_cli"),
        machine_id=_as_text(row[7], "machine_id"),
        parent_event_id=_as_optional_uuid(row[8], "parent_event_id"),
        payload=_as_payload(row[9], "payload"),
        redacted=_as_bool(row[10], "redacted"),
        content_digest=_as_digest(row[11], "content_digest"),
        prev_chain_digest=_as_digest(row[12], "prev_chain_digest"),
        chain_digest=_as_digest(row[13], "chain_digest"),
    )


def _as_int(value: object, column: str) -> int:
    """Read a whole number out of a column, refusing anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError(f"the ledger column {column} did not return a whole number")
    return value


def _as_text(value: object, column: str) -> str:
    """Read text out of a column, refusing anything else."""
    if not isinstance(value, str):
        raise StoreError(f"the ledger column {column} did not return text")
    return value


def _as_digest(value: object, column: str) -> str:
    """Read a digest out of a column, refusing text of any other length."""
    text = _as_text(value, column)
    if len(text) != DIGEST_LENGTH:
        raise StoreError(
            f"the ledger column {column} returned {len(text)} character(s) where a "
            f"{DIGEST_LENGTH} character digest is stored"
        )
    return text


def _as_bool(value: object, column: str) -> bool:
    """Read a boolean out of a column, refusing anything else."""
    if not isinstance(value, bool):
        raise StoreError(f"the ledger column {column} did not return a boolean")
    return value


def _as_uuid(value: object, column: str) -> UUID:
    """Read an identifier out of a column, accepting either representation."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise StoreError(
                f"the ledger column {column} did not return a hyphenated identifier"
            ) from exc
    raise StoreError(f"the ledger column {column} did not return an identifier")


def _as_optional_uuid(value: object, column: str) -> UUID | None:
    """Read an optional identifier out of a column."""
    if value is None:
        return None
    return _as_uuid(value, column)


def _as_moment(value: object, column: str) -> datetime:
    """Read a timezone-aware instant out of a column, refusing a naive one."""
    if not isinstance(value, datetime):
        raise StoreError(f"the ledger column {column} did not return an instant")
    return require_aware(value, f"the ledger column {column}")


def _as_payload(value: object, column: str) -> JsonObject:
    """Read a JSON document out of a column, refusing anything else.

    A document arriving as text is parsed, because a driver may hand back either
    the parsed document or its text depending on how it was configured, and the
    recomputation needs the document either way.
    """
    if isinstance(value, str):
        try:
            decoded: object = json.loads(value)
        except json.JSONDecodeError as exc:
            raise StoreError(f"the ledger column {column} did not return a JSON object") from exc
    else:
        decoded = value
    if not isinstance(decoded, Mapping):
        raise StoreError(f"the ledger column {column} did not return a JSON object")
    return decode_capture_payload({str(key): item for key, item in decoded.items()})
