"""Property 6: one altered stored field is detected at the row that carries it.

**Validates: Requirements 8.2, 8.3, 8.6, 8.7, 22.7**

This property needs the cluster, and that is not incidental. An append derives its
own sequence number from its own tip read and hashes both digests with the
cluster's own hash function, so a chain built anywhere else would be evidence
about a reimplementation rather than about the stored chain a reviewer verifies.
The module is therefore marked so it gates on a reachable instance and is
deselected from the credential-free workflow, exactly like every other suite that
needs one.

Five decisions shape what is asserted.

**Every one of the seven covered fields is mutated, in every example.** The
property is quantified over the mutation targets rather than over most of them, so
the drawn chain is carried through payload, category, timestamp, sequence number,
content digest, predecessor digest, and chain digest in turn. Each mutation is
applied, the report is asserted, and the stored value is put back, and the restored
chain is verified again before the next target: that last step is what makes each
finding attributable to one field rather than to an accumulation of edits.

**The report is asserted in full, not merely as a failure.** A verifier that
answered "something is wrong" for every alteration would satisfy a weaker claim and
be useless to an auditor. So each mutation states which stored field must be named,
which sequence number must be reported as the first disagreement, how many rows
must have verified before it, and which digest the report must carry as the terminal
digest of the prefix that held.

**The sequence-number mutation is reported at the place the walk first
disagrees.** The per-Session uniqueness constraint means a stored sequence number
can only be moved to a number the Session does not already hold, so it moves past
the end of the chain. Verification walks the rows in ascending stored order against
the numbers it expects, so when the moved row was the last one the disagreement is
that row's new number, and otherwise it is the number of the row that now stands
where the moved row used to be. Both cases are computed exactly from the chain
length and the drawn row, and the assertion pins whichever applies.

**The mutation is applied by a direct statement on the fixture's own
connection.** No role holds `UPDATE` on the ledger, and the module under test owns
no update at all, so the retrospective edit the chain exists to expose is made from
outside it. Every value is bound and every statement is a whole module-level
literal, the same discipline the source is held to.

**Only the first migration generation is applied.** The schema under test is what
that generation declares, so the category set the check constraint admits is the
one enumerated below rather than the wider set a later migration allows.

The example budget is 100 with no per-example deadline. A chain is drawn anywhere
from one to two hundred Events and every Event is a real insert, so per-example
cost is dominated by chain length and varies by an order of magnitude across
examples; a deadline would fail a long chain for being long rather than for being
wrong. The length is drawn directly rather than as a list size, because a list size
concentrates near its lower end and the upper end of the range is the interesting
part: drawn this way, most examples carry chains above sixty-five rows and the
whole file still finishes in well under a minute against a local instance, each
example carrying its chain through seven mutations with a verification and a
restoration apiece. Where a budget had to give, it was the budget; no assertion
was.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.models.event import (
    EmbeddingState,
    Event,
    EventCategory,
    JsonObject,
    JsonValue,
)
from molt.store import Connection, MemoryStore
from molt.store.chain import (
    GENESIS_PREDECESSOR,
    MISMATCH_CHAIN,
    MISMATCH_CONTENT,
    MISMATCH_PREDECESSOR,
    MISMATCH_SEQUENCE,
    AppendedRow,
    ChainRow,
    LedgerAppend,
    append_batch,
    canonical_payload_text,
    chain_rows,
    verify_chain,
)
from molt.store.migrate import apply_migrations, discover_migrations

pytestmark = pytest.mark.integration

# How many examples the property runs, and the bounds of a drawn chain. The
# reasoning behind the budget is in the module docstring.
MAX_EXAMPLES: Final[int] = 100
MIN_CHAIN: Final[int] = 1
MAX_CHAIN: Final[int] = 200

# The migration generation this module applies: the tenant table, the Session
# table, and the ledger with its two uniqueness constraints.
CORE_MIGRATION_VERSION: Final[int] = 1

# The categories the core migration's check constraint admits. The remaining
# member of the enumeration is admitted by a later migration that widens the
# constraint, and this module applies the first generation only.
CORE_CATEGORIES: Final[tuple[EventCategory, ...]] = tuple(
    category for category in EventCategory if category is not EventCategory.ATTRIBUTION_SUPERSEDED
)

# The fixture's own statements. The module under test owns no tenant insert, no
# Session insert, and no update at all, so each is written here with every value
# bound and no identifier interpolated.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)

# The seven alterations, one per stored field the digest rule covers.
SET_STORED_PAYLOAD: Final[str] = "UPDATE ledger SET payload = %s::JSONB WHERE id = %s"
SET_STORED_CATEGORY: Final[str] = "UPDATE ledger SET category = %s WHERE id = %s"
SET_STORED_TIMESTAMP: Final[str] = "UPDATE ledger SET occurred_at = %s WHERE id = %s"
SET_STORED_SEQUENCE: Final[str] = "UPDATE ledger SET seq = %s WHERE id = %s"
SET_STORED_CONTENT_DIGEST: Final[str] = "UPDATE ledger SET content_digest = %s WHERE id = %s"
SET_STORED_PREDECESSOR: Final[str] = "UPDATE ledger SET prev_chain_digest = %s WHERE id = %s"
SET_STORED_CHAIN_DIGEST: Final[str] = "UPDATE ledger SET chain_digest = %s WHERE id = %s"

# The instant the first Event of every chain carries, and how far apart two
# Events sit. The reading is derived from the epoch rather than written as a
# literal, and each row draws a sub-second offset of its own, so no chain embeds a
# calendar value and no two rows of one chain share an instant.
BASE_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
STEP: Final[timedelta] = timedelta(seconds=1)
MICROSECONDS_IN_SECOND: Final[int] = 1_000_000

# The retention interval an appended row expires after.
RETENTION: Final[timedelta] = timedelta(days=90)

# The two fields every generated Event shares, because neither is drawn and
# neither is what this property is about.
AGENT_CLI: Final[str] = "agent"
MACHINE_ID: Final[str] = "machine"

# The key the replacement payload carries and no generated payload holds, so a
# drawn replacement is guaranteed to render as different canonical text from the
# payload it replaces rather than needing a filter to make it so.
MARKER_KEY: Final[str] = "tamper_marker"

# The keys a generated payload draws from, mixing ordinary names with non-ASCII
# ones, because the digest commits to the canonical text as bytes and content
# outside the ASCII range is what shows the cluster's hashing and the verifier's
# hashing agree on the encoding.
PAYLOAD_KEYS: Final[tuple[str, ...]] = (
    "tool",
    "path",
    "argument",
    "count",
    "\u00e9tape",
    "\u0448\u0430\u0433",
    "nested",
)

# The alphabet a stored digest is written in, and the widths of the drawn values.
HEX_DIGITS: Final[str] = "0123456789abcdef"
INTEGER_BOUND: Final[int] = 2**53

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventSpec:
    """One Event of a chain, as the generator describes it.

    The identifier is minted at append time rather than drawn, because two drawn
    identifiers that shrank to one value would collide on the primary key and turn
    a shrink into an error about the fixture instead of a counterexample about the
    chain.
    """

    category: EventCategory
    payload: JsonObject
    microseconds: int
    redacted: bool
    text_body: str | None
    link_parent: bool


@dataclass(frozen=True, slots=True)
class Tampering:
    """Which row to alter, and the replacement value each covered field takes.

    One selector carries a replacement for every field rather than choosing one
    field, because the property is quantified over all seven targets and the test
    walks them in turn against the same drawn chain.
    """

    row_index: int
    payload: JsonObject
    marker: int
    category_rotation: int
    microsecond_shift: int
    sequence_gap: int
    content_digit: int
    predecessor_digit: int
    chain_digit: int


@dataclass(frozen=True, slots=True)
class ChainPlan:
    """A chain to append and the single-field alteration to make to one of its rows."""

    specs: tuple[EventSpec, ...]
    tampering: Tampering


@dataclass(frozen=True, slots=True)
class Mutation:
    """One alteration to one stored row, with what the verifier must then report.

    Attributes:
        target: The stored field being altered, for the failure message and the
            coverage record.
        statement: The alteration, as a module-level literal binding both values.
        replacement: The value to store in place of the stored one.
        restore: The stored value, put back once the report has been asserted.
        mismatch: The stored field the report must name.
        first_mismatch_seq: The sequence number the report must name as the first
            disagreement.
    """

    target: str
    statement: str
    replacement: object
    restore: object
    mismatch: str
    first_mismatch_seq: int


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def texts() -> st.SearchStrategy[str]:
    """Draw text spanning the ASCII range and beyond it, the empty string included.

    Surrogates are excluded by the codec because a payload is stored and hashed as
    UTF-8, and no encoding of a lone surrogate exists to store or to hash. The null
    character is admitted here on purpose: inside a payload it reaches the column
    as an escape sequence in the canonical JSON text rather than as a byte, so it
    exercises the encoding the digest commits to.
    """
    return st.text(alphabet=st.characters(codec="utf-8"), max_size=24)


def text_bodies() -> st.SearchStrategy[str]:
    """Draw the searchable text of an Event, excluding the null character.

    A text column holds no null byte anywhere in this schema, and the driver
    refuses one before the cluster ever sees it, so text carrying one is outside
    the input space every text column in the system admits rather than something
    the chain decides. The generator is constrained to that space instead of the
    property being weakened to tolerate a refusal.
    """
    return st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=24)


def payload_values(depth: int) -> st.SearchStrategy[JsonValue]:
    """Draw a JSON value nesting no deeper than the given number of levels."""
    scalars: st.SearchStrategy[JsonValue] = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-INTEGER_BOUND, max_value=INTEGER_BOUND),
        st.floats(allow_nan=False, allow_infinity=False),
        texts(),
    )
    if depth <= 1:
        return scalars
    child = payload_values(depth - 1)
    return st.one_of(
        scalars,
        st.lists(child, max_size=3),
        st.dictionaries(st.sampled_from(PAYLOAD_KEYS), child, max_size=3),
    )


def payloads() -> st.SearchStrategy[JsonObject]:
    """Draw an Event payload, empty or holding nested JSON values."""
    return st.dictionaries(st.sampled_from(PAYLOAD_KEYS), payload_values(3), max_size=4)


def event_specs() -> st.SearchStrategy[EventSpec]:
    """Draw one Event of a chain, over every category the core schema admits."""
    return st.builds(
        EventSpec,
        category=st.sampled_from(CORE_CATEGORIES),
        payload=payloads(),
        microseconds=st.integers(min_value=0, max_value=MICROSECONDS_IN_SECOND - 1),
        redacted=st.booleans(),
        text_body=st.one_of(st.none(), text_bodies()),
        link_parent=st.booleans(),
    )


def mutation_selectors(length: int) -> st.SearchStrategy[Tampering]:
    """Draw a row of a chain of the given length and a replacement per field.

    Each replacement is drawn from its field's own type and is constructed to
    differ from the value it replaces: a category is rotated by a non-zero
    offset, a timestamp is shifted by a non-zero number of microseconds, a
    sequence number is moved past the end of the chain because the per-Session
    uniqueness constraint admits no number the Session already holds, a digest
    keeps its shape and its length while one drawn digit of it changes, and a
    payload carries a key no generated payload holds.
    """
    return st.builds(
        Tampering,
        row_index=st.integers(min_value=0, max_value=length - 1),
        payload=payloads(),
        marker=st.integers(min_value=-INTEGER_BOUND, max_value=INTEGER_BOUND),
        category_rotation=st.integers(min_value=1, max_value=len(CORE_CATEGORIES) - 1),
        microsecond_shift=st.integers(min_value=1, max_value=MICROSECONDS_IN_SECOND - 1),
        sequence_gap=st.integers(min_value=1, max_value=64),
        content_digit=st.integers(min_value=1, max_value=len(HEX_DIGITS) - 1),
        predecessor_digit=st.integers(min_value=1, max_value=len(HEX_DIGITS) - 1),
        chain_digit=st.integers(min_value=1, max_value=len(HEX_DIGITS) - 1),
    )


@st.composite
def event_sequences(draw: st.DrawFn) -> ChainPlan:
    """Draw a chain of 1 to 200 Events and the single-field alteration to make.

    The length is drawn first and the row to alter is drawn inside it, so the
    selector names a row that exists rather than an index that has to be folded
    into range afterwards.
    """
    length = draw(st.integers(min_value=MIN_CHAIN, max_value=MAX_CHAIN))
    specs = draw(st.lists(event_specs(), min_size=length, max_size=length))
    return ChainPlan(specs=tuple(specs), tampering=draw(mutation_selectors(length)))


# ---------------------------------------------------------------------------
# The cluster the chain is stored on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding the core migration, a store over it, and one tenant."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def stage_core_migration(destination: Path) -> None:
    """Copy the core migration file into a directory of its own."""
    for migration in discover_migrations():
        if migration.version == CORE_MIGRATION_VERSION:
            destination.joinpath(migration.path.name).write_bytes(migration.path.read_bytes())


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Cluster]:
    """Apply the core migration, then build a store bound to that schema.

    Module scope is what keeps the schema cost paid once: examples are isolated
    from each other by a Session of their own rather than by a schema of their own.
    """
    directory = tmp_path_factory.mktemp("molt_p06_core")
    stage_core_migration(directory)
    apply_migrations(fresh_schema, directory=directory)

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

    client_id = uuid4()
    send(fresh_schema, INSERT_CLIENT, (client_id, f"tenant-{client_id.hex[:8]}", "Tenant", "eu"))

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema, client_id=client_id)


def new_session(cluster: Cluster) -> UUID:
    """Place one Session row, so each example owns a chain of its own."""
    session_id = uuid4()
    send(cluster.connection, INSERT_SESSION, (session_id, cluster.client_id, AGENT_CLI, MACHINE_ID))
    return session_id


def append_chain(
    cluster: Cluster, session_id: UUID, specs: Sequence[EventSpec]
) -> tuple[AppendedRow, ...]:
    """Append a drawn chain as one batch of the append statement.

    A batch is a loop of the same statement inside one transaction, so every row
    is still derived and hashed by the cluster exactly as a single append is, and a
    two-hundred-row chain costs one conflict window rather than two hundred.
    """
    requests: list[LedgerAppend] = []
    previous_id: UUID | None = None
    for index, spec in enumerate(specs):
        moment = BASE_INSTANT + index * STEP + timedelta(microseconds=spec.microseconds)
        event_id = uuid4()
        requests.append(
            LedgerAppend(
                event=Event(
                    id=event_id,
                    session_id=session_id,
                    client_id=cluster.client_id,
                    category=spec.category,
                    occurred_at=moment,
                    agent_cli=AGENT_CLI,
                    machine_id=MACHINE_ID,
                    parent_event_id=previous_id if spec.link_parent else None,
                    payload=spec.payload,
                    redacted=spec.redacted,
                    text_body=spec.text_body,
                ),
                expires_at=moment + RETENTION,
                embedding_state=EmbeddingState.PENDING,
            )
        )
        previous_id = event_id
    return append_batch(cluster.store, requests)


# ---------------------------------------------------------------------------
# Replacement values
# ---------------------------------------------------------------------------


def altered_payload(payload: JsonObject, marker: int) -> JsonObject:
    """A drawn payload carrying the marker key, so it cannot equal what it replaces."""
    return {**payload, MARKER_KEY: marker}


def altered_category(category: str, rotation: int) -> str:
    """Another category the schema admits, reached by a non-zero rotation."""
    names = [str(member) for member in CORE_CATEGORIES]
    return names[(names.index(category) + rotation) % len(names)]


def altered_digest(digest: str, rotation: int) -> str:
    """A digest-shaped value differing from the stored one in its first digit.

    The length and the alphabet are preserved because the stored column is
    constrained to both, so the alteration is one a retrospective editor could
    actually make rather than one the schema would refuse.
    """
    rotated = HEX_DIGITS[(HEX_DIGITS.index(digest[0]) + rotation) % len(HEX_DIGITS)]
    return rotated + digest[1:]


def mutations(row: ChainRow, *, length: int, plan: Tampering) -> tuple[Mutation, ...]:
    """The seven alterations to one stored row, with the report each must produce.

    Six of them leave the row where it is, so the walk reaches it at its own
    sequence number and reports there. The seventh moves the row's sequence number
    past the end of the chain, which is the only move the per-Session uniqueness
    constraint permits, so the walk disagrees either at that new number, when the
    moved row was the last of the chain and still sorts last, or at the number of
    the row that now stands in the moved row's place.
    """
    moved_to = length + plan.sequence_gap
    moved_mismatch = row.seq + 1 if row.seq < length else moved_to
    return (
        Mutation(
            target="payload",
            statement=SET_STORED_PAYLOAD,
            replacement=canonical_payload_text(altered_payload(plan.payload, plan.marker)),
            restore=canonical_payload_text(row.payload),
            mismatch=MISMATCH_CONTENT,
            first_mismatch_seq=row.seq,
        ),
        Mutation(
            target="category",
            statement=SET_STORED_CATEGORY,
            replacement=altered_category(row.category, plan.category_rotation),
            restore=row.category,
            mismatch=MISMATCH_CONTENT,
            first_mismatch_seq=row.seq,
        ),
        Mutation(
            target="timestamp",
            statement=SET_STORED_TIMESTAMP,
            replacement=row.occurred_at + timedelta(microseconds=plan.microsecond_shift),
            restore=row.occurred_at,
            mismatch=MISMATCH_CONTENT,
            first_mismatch_seq=row.seq,
        ),
        Mutation(
            target="sequence number",
            statement=SET_STORED_SEQUENCE,
            replacement=moved_to,
            restore=row.seq,
            mismatch=MISMATCH_SEQUENCE,
            first_mismatch_seq=moved_mismatch,
        ),
        Mutation(
            target="content digest",
            statement=SET_STORED_CONTENT_DIGEST,
            replacement=altered_digest(row.content_digest, plan.content_digit),
            restore=row.content_digest,
            mismatch=MISMATCH_CONTENT,
            first_mismatch_seq=row.seq,
        ),
        Mutation(
            target="predecessor digest",
            statement=SET_STORED_PREDECESSOR,
            replacement=altered_digest(row.prev_chain_digest, plan.predecessor_digit),
            restore=row.prev_chain_digest,
            mismatch=MISMATCH_PREDECESSOR,
            first_mismatch_seq=row.seq,
        ),
        Mutation(
            target="chain digest",
            statement=SET_STORED_CHAIN_DIGEST,
            replacement=altered_digest(row.chain_digest, plan.chain_digit),
            restore=row.chain_digest,
            mismatch=MISMATCH_CHAIN,
            first_mismatch_seq=row.seq,
        ),
    )


def length_band(length: int) -> str:
    """Which part of the length range an example drew, for the coverage record."""
    if length == MIN_CHAIN:
        return "1"
    if length <= 16:
        return "2-16"
    if length <= 64:
        return "17-64"
    return f"65-{MAX_CHAIN}"


# Feature: molt, Property 6: For any Event sequence of length 1 to 200 in one
# Session, plus any single-field mutation of one stored row, chain verification
# reports no mismatch on the unmutated chain together with the correct verified row
# count and terminal digest, and reports the mutated row's sequence number on the
# mutated chain, for mutations of payload, category, timestamp, sequence number,
# content digest, predecessor digest, or chain digest.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(plan=event_sequences())
def test_a_single_altered_stored_field_is_reported_at_the_row_it_was_made_in(
    cluster: Cluster, plan: ChainPlan
) -> None:
    length = len(plan.specs)
    event(f"chain length={length_band(length)}")
    session_id = new_session(cluster)
    written = append_chain(cluster, session_id, plan.specs)

    # The unmutated chain verifies, with the row count and the terminal digest a
    # checkpoint would commit to.
    intact = verify_chain(cluster.store, session_id)
    assert intact.ok, f"the stored chain disagreed at sequence {intact.first_mismatch_seq}"
    assert intact.rows == length
    assert intact.first_mismatch_seq is None
    assert intact.mismatch is None
    assert intact.terminal_digest == written[-1].chain_digest

    stored = chain_rows(cluster.store, session_id)
    assert [row.seq for row in stored] == list(range(1, length + 1))
    target = stored[plan.tampering.row_index]

    # How far the chain must be reported to have held: every row before the
    # altered one, and the digest that prefix ends on.
    held = plan.tampering.row_index
    held_digest = GENESIS_PREDECESSOR if held == 0 else stored[held - 1].chain_digest

    for mutation in mutations(target, length=length, plan=plan.tampering):
        event(f"mutated field={mutation.target}")
        assert mutation.replacement != mutation.restore, (
            f"the {mutation.target} replacement must differ from the stored value"
        )
        send(cluster.connection, mutation.statement, (mutation.replacement, target.event_id))

        report = verify_chain(cluster.store, session_id)

        assert not report.ok, f"the altered {mutation.target} was not detected"
        assert report.mismatch == mutation.mismatch
        assert report.first_mismatch_seq == mutation.first_mismatch_seq
        assert report.rows == held, "the report says how far the chain held"
        assert report.terminal_digest == held_digest

        # Putting the stored value back must restore an intact chain, which is
        # what makes each finding above attributable to one field alone.
        send(cluster.connection, mutation.statement, (mutation.restore, target.event_id))
        restored = verify_chain(cluster.store, session_id)
        assert restored.ok, (
            f"restoring the {mutation.target} left a chain disagreeing at "
            f"sequence {restored.first_mismatch_seq}"
        )
        assert restored.rows == length
        assert restored.terminal_digest == intact.terminal_digest
