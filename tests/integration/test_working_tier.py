"""The working tier against a live instance: overwrite, point read, purge, expiry, isolation.

The unit module asserts the shape of the statements this tier sends. This module
asserts the five things only a cluster can answer, and one of them is the reason
the module exists at all.

**A repeated write overwrites in place.** The primary key is the Session and the
scratch key together, so the count of stored rows is asserted alongside the stored
value. A tier that accumulated versions would satisfy a value assertion and fail
the count, and a tier that accumulates versions is a history, which is the one
thing this tier must not become.

**A point read returns the stored value, to the tenant that owns it.** The
document is round-tripped through the cluster rather than compared against what
the write returned, and the same key read under a second Client reads as absent
while the owning Client still finds it, so the absence is scoping rather than
deletion.

**The purge is one statement and one number.** Every statement the store sends is
recorded here, so the transaction that empties a Client's scratch is read back and
asserted to be one deleting statement between the isolation level and the commit,
not a loop over batches. Another tenant's rows are counted afterwards, and a
second purge answers with no rows removed rather than failing.

**Row-Level TTL physically removes an expired row, and nothing outside the cluster
removes it.** This is the claim the tier's disposability rests on, and it is
asserted three ways. Every storage parameter migration 011 declares is read back
off the committed descriptor, because configuring row-level expiry inside the
migration's own transaction does not fail — it reports success, commits, and
leaves the parameters absent, so a read-back is the only evidence the
configuration landed. The cluster's own delete schedule for this table is read
back beside it, which is what makes the deleting process the cluster's rather than
something scheduled outside it. And then a row whose expiry has already passed is
written and watched until it is gone, while every statement this module sends
during the wait is recorded and asserted to remove nothing: the deletion is
therefore the cluster's work and not this module's.

**No table other than the tier's own holds a reference to a working row.** That is
read from the catalog rather than from the migration text, because a constraint
that exists in a file and not in the catalog enforces nothing, and the direction
is asserted both ways: nothing references a working row, and a working row
references the Session and the Client it belongs to.

Two notes on what this module does to the cluster to make expiry observable, both
undone before it finishes. The delete schedule's recurrence on this module's own
table is shortened to the finest resolution a cron admits, and the pace at which
the cluster's job scheduler notices a due schedule is shortened with it, because
those two granularities compose and each is a minute by default. The recurrence is
a property of a table in a schema this module created and drops; the pace belongs
to the cluster, so it is read before it is changed and written back from the value
that was read.

**Validates: Requirements 42.9, 42.12, 42.13, 36.2**
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.models.event import JsonObject
from molt.models.tiers import MEMORY_TIERS, WORKING_TIER
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.migrate import apply_migrations, discover_migrations
from molt.store.retry import BEGIN_STATEMENT, COMMIT_STATEMENT, SERIALIZABLE_STATEMENT
from molt.store.working import (
    PURGE_CLIENT_SCRATCH_STATEMENT,
    WORKING_TTL_KEY,
    ScratchRow,
    ScratchWrite,
    WorkingInterval,
    purge_working_rows,
    read_scratch,
    session_scratch,
    write_scratch,
)

pytestmark = pytest.mark.integration

# The one table the taxonomy assigns to the disposable tier, read from the
# taxonomy rather than restated, so this module and the tier model cannot disagree
# about which table the tier holds.
WORKING_TABLE: Final[str] = MEMORY_TIERS[WORKING_TIER].tables[0]

# The migration that creates that table and configures its expiry. The storage
# parameters asserted below are read out of that file rather than listed here, so
# what is asserted is that what the migration declared is what the cluster
# committed.
WORKING_MIGRATION_VERSION: Final[int] = 11

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes this module makes, parameterised in full. The tier owns no Client
# insert and no Session insert, so the rows its scratch state hangs off are placed
# here.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)

# A write naming no expiry, so the column default the migration declares computes
# the stored expiry. The difference the cluster reports is what the configured
# interval is compared against.
INSERT_WITHOUT_EXPIRY: Final[str] = (
    "INSERT INTO working_memory (session_id, scratch_key, client_id, value) "
    "VALUES (%s, %s, %s, %s::JSONB) RETURNING expires_at - updated_at"
)

# The counts every claim about stored rows is read from. Counting is deliberate: a
# claim that a repeated write overwrote rather than accumulated is a claim about
# how many rows exist, not only about what one of them holds.
COUNT_KEYED_ROWS: Final[str] = (
    "SELECT count(*) FROM working_memory WHERE session_id = %s AND scratch_key = %s"
)
COUNT_SESSION_ROWS: Final[str] = "SELECT count(*) FROM working_memory WHERE session_id = %s"
COUNT_CLIENT_ROWS: Final[str] = "SELECT count(*) FROM working_memory WHERE client_id = %s"

# The cluster's own reading, so no example places an instant from a local clock.
CLUSTER_READING: Final[str] = "SELECT now()"

# The identity of this module's own copy of the table, bound as a value, and the
# committed descriptor of it. The descriptor is how a silent expiry-configuration
# failure is caught: it reports what the cluster holds rather than what was asked
# for.
TABLE_IDENTITY: Final[str] = "SELECT %s::regclass::oid"
TABLE_DESCRIPTOR: Final[str] = "SELECT create_statement FROM [SHOW CREATE TABLE working_memory]"

# The cluster's own delete schedule for a table, found by the table it names
# rather than by the text of its label, so the match is on identity.
TABLE_SCHEDULE: Final[str] = (
    "SELECT recurrence, owner, schedule_status FROM [SHOW SCHEDULES] WHERE command->>'tableId' = %s"
)

# The referential catalog, read in both directions: what references a table, and
# what a table references.
REFERENCES_TO_TABLE: Final[str] = (
    "SELECT table_name, constraint_name FROM information_schema.referential_constraints "
    "WHERE constraint_schema = %s AND referenced_table_name = %s"
)
REFERENCES_FROM_TABLE: Final[str] = (
    "SELECT referenced_table_name, constraint_name "
    "FROM information_schema.referential_constraints "
    "WHERE constraint_schema = %s AND table_name = %s"
)
TABLE_PRESENT: Final[str] = (
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s"
)

# The tables Requirement 42 criterion 12 names as holding no reference to a
# working row, and the two tables a working row references outward.
UNREFERENCING_TABLES: Final[tuple[str, ...]] = (
    "lineage_edge",
    "client_binding",
    "disposition",
    "ledger_checkpoint",
)
OUTWARD_REFERENCES: Final[frozenset[str]] = frozenset({"session", "client"})

# The two levers that make expiry observable inside a bounded wait, and the
# statements that read and restore them.
SHORTEN_RECURRENCE: Final[str] = "ALTER TABLE working_memory SET (ttl_job_cron = '* * * * *')"
RESTORE_RECURRENCE: Final[str] = "ALTER TABLE working_memory SET (ttl_job_cron = '@hourly')"
DECLARED_RECURRENCE: Final[str] = "ttl_job_cron = '@hourly'"
READ_SCHEDULER_PACE: Final[str] = "SHOW CLUSTER SETTING jobs.scheduler.pace"
SET_SCHEDULER_PACE: Final[str] = "SET CLUSTER SETTING jobs.scheduler.pace = %s"
QUICK_SCHEDULER_PACE: Final[str] = "5s"

# How long the expired row is watched for, and how often it is looked for.
#
# Two coarse granularities compose here. A cron admits no resolution finer than
# one minute, so even a shortened recurrence falls due only at a minute boundary,
# and the cluster notices a due schedule only when its job scheduler next polls,
# which the fixture shortens to a few seconds. The wait is therefore a minute plus
# a poll plus however long the delete job takes over one row, and the bound below
# is comfortably more than twice that sum. Removals observed on this platform land
# far inside it; the bound exists so a cluster that never removes the row fails an
# assertion rather than hanging.
EXPIRY_BOUND_SECONDS: Final[float] = 150.0
EXPIRY_POLL_SECONDS: Final[float] = 2.0

# Words that would remove or rewrite a working row. No statement this module sends
# while it waits for the cluster to remove the expired row may hold one of them,
# which is what makes the deletion the cluster's work rather than this module's.
MUTATING_WORDS: Final[tuple[str, ...]] = ("DELETE", "TRUNCATE", "DROP", "UPSERT", "UPDATE")

# The intervals this module configures for its own writes. Neither is the surface
# default, so nothing asserted here could be satisfied by a constant the tier held
# instead of reading the configuration surface.
WRITE_TTL_SECONDS: Final[int] = 120
SHORT_TTL_SECONDS: Final[int] = 60

# How far in the past the expired row's stored expiry is placed: far enough that no
# difference between this process's clock and the cluster's could leave the row
# live, and derived from the cluster's own reading rather than written out.
ALREADY_EXPIRED_SECONDS: Final[int] = 3600

# How far apart the two writes of the overwrite example are placed, so the second
# stored instant is later than the first without waiting for a clock.
REWRITE_GAP_SECONDS: Final[int] = 30

# The values the placed rows carry. None is what an assertion turns on, so each is
# fixed here rather than varied per example.
JURISDICTION: Final[str] = "eu"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"

# The scratch keys and documents the examples write. The nested shape is
# deliberate: the round trip is asserted over a document rather than a scalar, so a
# value the cluster stored as text and returned decoded is caught.
PLAN_KEY: Final[str] = "plan-under-revision"
CURSOR_KEY: Final[str] = "file-walk-cursor"
FIRST_PLAN: Final[JsonObject] = {"step": 1, "open": ["a", "b"], "note": "held while working"}
SECOND_PLAN: Final[JsonObject] = {"step": 2, "open": [], "done": True, "depth": {"inner": 3}}
CURSOR_VALUE: Final[JsonObject] = {"offset": 4096}

# The scratch keys each seeded Session of the purge example carries, and how many
# Sessions the purged tenant holds. The purge spans Sessions, so the count it
# reports has to cover the tenant's whole scratch rather than one Session's.
SEEDED_KEYS: Final[tuple[str, ...]] = (PLAN_KEY, CURSOR_KEY)
SESSIONS_PER_TENANT: Final[int] = 2

# A connection and a cursor are typed loosely because the driver is reached through
# a fixture rather than imported, which keeps this module collectable with no
# driver installed.
DriverConnection = Any
DriverCursor = Any

# The storage parameter list of an expiry configuration, as a migration writes it.
_PARAMETER_SET: Final[re.Pattern[str]] = re.compile(
    r"ALTER\s+TABLE\s+working_memory\s+SET\s*\((?P<body>[^)]*)\)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Recording what reached the cluster
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Recorder:
    """Every statement that reached the cluster, in order, from either source here.

    The store's connections and this module's own connection record into one
    recorder, which is what lets the expiry claim be stated as an absence: over the
    window in which the cluster removes the expired row, nothing sent from this
    process removed anything.
    """

    statements: list[str] = field(default_factory=list)

    def mark(self) -> int:
        """How many statements have been seen, so a later claim spans only the rest."""
        return len(self.statements)

    def since(self, mark: int) -> tuple[str, ...]:
        """Every statement sent after a mark was taken."""
        return tuple(self.statements[mark:])

    def issued_since(self, mark: int) -> tuple[str, ...]:
        """What a caller sent after a mark, with the pool's own statements removed.

        Establishing the statement timeout, setting the search path, and resetting a
        returned connection belong to the pool rather than to the tier, so a claim
        about the statements one transaction sent is read off this list.
        """
        return tuple(
            query
            for query in self.since(mark)
            if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT, SEARCH_PATH_STATEMENT)
        )


class RecordingCursor:
    """A cursor that records each statement and otherwise delegates in full."""

    def __init__(self, inner: DriverCursor, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then send it exactly as it was given."""
        self._recorder.statements.append(query)
        sent: object = self._inner.execute(query, params)
        return sent

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the next row the last statement produced."""
        row: tuple[object, ...] | None = self._inner.fetchone()
        return row

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every remaining row the last statement produced."""
        rows: list[tuple[object, ...]] = self._inner.fetchall()
        return rows

    def close(self) -> None:
        """Release this cursor."""
        self._inner.close()


class RecordingConnection:
    """A connection handing out recording cursors over one shared recorder."""

    def __init__(self, inner: DriverConnection, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder

    @property
    def closed(self) -> bool:
        """Whether the underlying connection can no longer be used."""
        state: bool = bool(self._inner.closed)
        return state

    def cursor(self) -> RecordingCursor:
        """Open a recording cursor over the underlying connection."""
        return RecordingCursor(self._inner.cursor(), self._recorder)

    def close(self) -> None:
        """Close the underlying connection."""
        self._inner.close()


# ---------------------------------------------------------------------------
# The declared expiry configuration, read out of the migration
# ---------------------------------------------------------------------------


def declared_parameters() -> tuple[str, ...]:
    """The storage parameters the working-tier migration declares, one per pair.

    Read from the migration rather than restated here, so the descriptor assertion
    below says what it means: whatever that file asked the cluster to configure is
    what the cluster committed. A pair written into this module instead would let
    the migration and the assertion drift, and the drift would be silent in exactly
    the direction that matters.
    """
    sources = [
        migration.path
        for migration in discover_migrations()
        if migration.version == WORKING_MIGRATION_VERSION
    ]
    assert len(sources) == 1, f"one migration should configure the tier, not {len(sources)}"
    matched = _PARAMETER_SET.search(sources[0].read_text(encoding="utf-8"))
    assert matched is not None, "the migration should configure row-level expiry on the table"
    pairs = tuple(
        " ".join(fragment.split())
        for fragment in matched.group("body").split(",")
        if fragment.strip()
    )
    assert pairs, "the expiry configuration should declare at least one storage parameter"
    return pairs


# ---------------------------------------------------------------------------
# The schema, the store, and the rows the examples hang off
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the shared recorder."""

    store: MemoryStore
    connection: DriverConnection
    schema: str
    recorder: Recorder
    parameters: tuple[str, ...]

    def rows(
        self,
        statement: str,
        params: tuple[object, ...] | None = None,
    ) -> list[tuple[Any, ...]]:
        """Send one statement on this module's own connection, recording it."""
        self.recorder.statements.append(statement)
        with self.connection.cursor() as cursor:
            cursor.execute(statement, params)
            if cursor.description is None:
                return []
            return list(cursor.fetchall())

    def send(self, statement: str, params: tuple[object, ...] | None = None) -> None:
        """Send one statement whose rows nothing reads."""
        self.rows(statement, params)

    def one(self, statement: str, params: tuple[object, ...] | None = None) -> tuple[Any, ...]:
        """The single row a statement is expected to produce."""
        produced = self.rows(statement, params)
        assert len(produced) == 1, f"the statement produced {len(produced)} rows where one was read"
        return produced[0]

    def count(self, statement: str, params: tuple[object, ...]) -> int:
        """The number one counting statement reports."""
        return int(self.one(statement, params)[0])

    def reading(self) -> datetime:
        """The cluster's own current reading, so no example places a local instant."""
        moment = self.one(CLUSTER_READING)[0]
        assert isinstance(moment, datetime)
        return moment

    def identity(self) -> str:
        """This schema's copy of the tier's table, as the cluster identifies it."""
        return str(self.one(TABLE_IDENTITY, (WORKING_TABLE,))[0])

    def descriptor(self) -> str:
        """The committed definition of the tier's table, as the cluster reports it."""
        return str(self.one(TABLE_DESCRIPTOR)[0])

    def client(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        self.send(
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:12]}", "Tenant", JURISDICTION),
        )
        return identifier

    def session(self, client_id: UUID) -> UUID:
        """Place one Session of a Client directly and return its identifier."""
        identifier = uuid4()
        self.send(INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
        return identifier


def interval_of(seconds: int) -> WorkingInterval:
    """The interval the tier reads from a configuration naming that many seconds."""
    return WorkingInterval.from_configuration(
        Configuration(environ={WORKING_TTL_KEY: str(seconds)}, file_values={})
    )


def scratch(session_id: UUID, client_id: UUID, key: str, value: JsonObject) -> ScratchWrite:
    """One scratch write, named by the pair the primary key spans."""
    return ScratchWrite(session_id=session_id, scratch_key=key, client_id=client_id, value=value)


def mutating(statements: Sequence[str]) -> tuple[str, ...]:
    """Every statement among these that could remove or rewrite a stored row."""
    return tuple(
        statement
        for statement in statements
        if any(word in statement.upper() for word in MUTATING_WORDS)
    )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a recording store over this module's schema.

    Every migration is applied because the tier's table arrives in the second
    generation and its privileges arrive after it, and because the referential
    claim is a claim about the whole schema rather than about part of one.
    """
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    recorder = Recorder()

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = RecordingConnection(opened, recorder)
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(
            store=store,
            connection=fresh_schema,
            schema=schema,
            recorder=recorder,
            parameters=declared_parameters(),
        )


@pytest.fixture
def quickened_expiry(cluster: Cluster) -> Iterator[None]:
    """Shorten both granularities that delay a delete job, then put both back.

    The recurrence belongs to this module's own table, in a schema this module
    created and drops, so shortening it reaches no other database on the instance.
    The pace at which the cluster notices a due schedule belongs to the cluster, so
    it is read first and written back from the value that was read rather than reset
    to a default this module has no business asserting.

    The recurrence is restored to the value the migration declares, which is
    asserted here rather than assumed, so the restoring statement cannot quietly
    leave the table configured differently from the way the migration configured it.
    """
    assert DECLARED_RECURRENCE in cluster.parameters, (
        "the restoring statement should put back the recurrence the migration declares"
    )
    previous = cluster.one(READ_SCHEDULER_PACE)[0]
    cluster.send(SET_SCHEDULER_PACE, (QUICK_SCHEDULER_PACE,))
    cluster.send(SHORTEN_RECURRENCE)
    try:
        yield
    finally:
        cluster.send(RESTORE_RECURRENCE)
        cluster.send(SET_SCHEDULER_PACE, (previous,))


def wait_for_removal(cluster: Cluster, session_id: UUID, key: str) -> float | None:
    """Watch one working row until the cluster has removed it, or the bound passes.

    Returns:
        How many seconds the removal took, or None when the row was still present
        when the bound ran out. Polling rather than sleeping the whole bound is what
        keeps the ordinary case as short as the cluster makes it.
    """
    started = time.monotonic()
    while True:
        if cluster.count(COUNT_KEYED_ROWS, (session_id, key)) == 0:
            return time.monotonic() - started
        if time.monotonic() - started >= EXPIRY_BOUND_SECONDS:
            return None
        time.sleep(EXPIRY_POLL_SECONDS)


# ---------------------------------------------------------------------------
# A write overwrites in place
# ---------------------------------------------------------------------------


def test_a_repeated_write_overwrites_the_row_it_replaces(cluster: Cluster) -> None:
    """Two writes under one key leave one row holding the second value.

    The count is asserted alongside the value, because a tier that kept both writes
    would still report the second from a read ordered by anything and would still be
    a history.
    """
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    interval = interval_of(WRITE_TTL_SECONDS)
    first_reading = cluster.reading()

    first = write_scratch(
        cluster.store,
        scratch(session_id, client_id, PLAN_KEY, FIRST_PLAN),
        interval=interval,
        now=first_reading,
    )
    second = write_scratch(
        cluster.store,
        scratch(session_id, client_id, PLAN_KEY, SECOND_PLAN),
        interval=interval,
        now=first_reading + timedelta(seconds=REWRITE_GAP_SECONDS),
    )

    assert cluster.count(COUNT_KEYED_ROWS, (session_id, PLAN_KEY)) == 1, (
        "the pair the primary key spans should hold one row, not a version per write"
    )
    stored: ScratchRow | None = read_scratch(cluster.store, session_id, PLAN_KEY, client_id)
    assert stored == second
    assert stored is not None
    assert stored.value == SECOND_PLAN
    assert second.updated_at == first.updated_at + timedelta(seconds=REWRITE_GAP_SECONDS)
    assert second.expires_at == first.expires_at + timedelta(seconds=REWRITE_GAP_SECONDS)


def test_a_second_scratch_key_is_a_second_row_of_the_same_session(cluster: Cluster) -> None:
    """The overwrite is keyed by the pair, so a Session's scratch is a set of names."""
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    interval = interval_of(WRITE_TTL_SECONDS)

    for key, value in ((PLAN_KEY, FIRST_PLAN), (CURSOR_KEY, CURSOR_VALUE)):
        write_scratch(
            cluster.store,
            scratch(session_id, client_id, key, value),
            interval=interval,
        )

    assert cluster.count(COUNT_SESSION_ROWS, (session_id,)) == len(SEEDED_KEYS)
    listed = session_scratch(cluster.store, session_id, client_id)
    assert tuple(row.scratch_key for row in listed) == tuple(sorted(SEEDED_KEYS))


# ---------------------------------------------------------------------------
# A point read returns the stored value, to the tenant that owns it
# ---------------------------------------------------------------------------


def test_a_point_read_returns_the_document_the_write_stored(cluster: Cluster) -> None:
    """The value round-trips through the cluster as the document it was written as."""
    client_id = cluster.client()
    session_id = cluster.session(client_id)

    written = write_scratch(
        cluster.store,
        scratch(session_id, client_id, PLAN_KEY, SECOND_PLAN),
        interval=interval_of(WRITE_TTL_SECONDS),
    )

    stored = read_scratch(cluster.store, session_id, PLAN_KEY, client_id)

    assert stored == written
    assert stored is not None
    assert stored.value == SECOND_PLAN, "the nested document comes back as it went in"
    assert stored.session_id == session_id
    assert stored.client_id == client_id


def test_a_point_read_of_another_tenant_finds_nothing(cluster: Cluster) -> None:
    """Holding a Session identifier is not authority over the rows that name it."""
    client_id = cluster.client()
    other_client_id = cluster.client()
    session_id = cluster.session(client_id)
    write_scratch(
        cluster.store,
        scratch(session_id, client_id, PLAN_KEY, FIRST_PLAN),
        interval=interval_of(WRITE_TTL_SECONDS),
    )

    assert read_scratch(cluster.store, session_id, PLAN_KEY, other_client_id) is None
    assert read_scratch(cluster.store, session_id, CURSOR_KEY, client_id) is None
    assert read_scratch(cluster.store, session_id, PLAN_KEY, client_id) is not None, (
        "the row is still stored, so the other tenant's absence is scoping"
    )
    assert cluster.count(COUNT_KEYED_ROWS, (session_id, PLAN_KEY)) == 1


# ---------------------------------------------------------------------------
# The purge: one statement, one number, one tenant
# ---------------------------------------------------------------------------


def test_the_purge_removes_one_tenants_scratch_in_one_statement(cluster: Cluster) -> None:
    """One deleting statement between the isolation level and the commit, and one count."""
    purged_client = cluster.client()
    kept_client = cluster.client()
    interval = interval_of(WRITE_TTL_SECONDS)

    for _ in range(SESSIONS_PER_TENANT):
        session_id = cluster.session(purged_client)
        for key in SEEDED_KEYS:
            write_scratch(
                cluster.store,
                scratch(session_id, purged_client, key, FIRST_PLAN),
                interval=interval,
            )
    kept_session = cluster.session(kept_client)
    for key in SEEDED_KEYS:
        write_scratch(
            cluster.store,
            scratch(kept_session, kept_client, key, CURSOR_VALUE),
            interval=interval,
        )

    seeded = SESSIONS_PER_TENANT * len(SEEDED_KEYS)
    assert cluster.count(COUNT_CLIENT_ROWS, (purged_client,)) == seeded

    mark = cluster.recorder.mark()
    removed = purge_working_rows(cluster.store, purged_client)

    assert removed == seeded, "the aggregate count is the number of rows the statement removed"
    assert cluster.recorder.issued_since(mark) == (
        BEGIN_STATEMENT,
        SERIALIZABLE_STATEMENT,
        PURGE_CLIENT_SCRATCH_STATEMENT,
        COMMIT_STATEMENT,
    ), "the purge is one statement, not a loop over batches"
    assert cluster.count(COUNT_CLIENT_ROWS, (purged_client,)) == 0
    assert cluster.count(COUNT_CLIENT_ROWS, (kept_client,)) == len(SEEDED_KEYS), (
        "the predicate is the tenant column, so another tenant's scratch survives"
    )


def test_a_purge_of_a_tenant_holding_nothing_reports_no_rows(cluster: Cluster) -> None:
    """A repeated purge answers with a count of nothing rather than failing."""
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    write_scratch(
        cluster.store,
        scratch(session_id, client_id, PLAN_KEY, FIRST_PLAN),
        interval=interval_of(WRITE_TTL_SECONDS),
    )

    assert purge_working_rows(cluster.store, client_id) == 1
    assert purge_working_rows(cluster.store, client_id) == 0
    assert read_scratch(cluster.store, session_id, PLAN_KEY, client_id) is None


# ---------------------------------------------------------------------------
# Row-Level TTL: configured, scheduled by the cluster, and physically enforced
# ---------------------------------------------------------------------------


def test_every_declared_expiry_parameter_reached_the_committed_descriptor(
    cluster: Cluster,
) -> None:
    """What the migration asked the cluster to configure is what the cluster holds.

    This is the assertion the migration's own transaction marker exists for.
    Configuring row-level expiry on a table created earlier in the same transaction
    is not refused: it reports success, the transaction commits, and the parameters
    are simply absent from the committed descriptor afterwards. Watching for an
    error would therefore report a table that expires nothing as configured, so the
    evidence has to be the descriptor.
    """
    descriptor = cluster.descriptor()

    assert "ttl = 'on'" in descriptor, "the cluster should report row-level expiry as active"
    missing = [pair for pair in cluster.parameters if pair not in descriptor]
    assert missing == [], f"the committed descriptor does not carry {missing}"


def test_the_cluster_holds_its_own_delete_schedule_for_the_table(cluster: Cluster) -> None:
    """The delete job is the cluster's, at the recurrence the migration declared.

    Read by the table the schedule names rather than by the text of its label, and
    asserted to be active, because the tier's disposability rests on expiry being
    enforced by the cluster rather than by a process scheduled outside it. A schedule
    the cluster owns and runs is that enforcement; nothing in this repository runs a
    sweeper of its own.
    """
    schedules = cluster.rows(TABLE_SCHEDULE, (cluster.identity(),))

    assert len(schedules) == 1, f"the table should carry one delete schedule, not {len(schedules)}"
    recurrence = str(schedules[0][0])
    owner = str(schedules[0][1])
    status = str(schedules[0][2])
    assert f"ttl_job_cron = '{recurrence}'" in cluster.parameters, (
        f"the schedule runs at {recurrence}, which the migration does not declare"
    )
    assert status == "ACTIVE"
    assert owner, "the schedule is owned inside the cluster"


def test_the_column_default_expires_a_row_after_the_configured_interval(
    cluster: Cluster,
) -> None:
    """A row written naming no expiry lives exactly the interval configuration states.

    The number is not written here. It is read from the configuration surface with
    nothing set, so the surface's own default is what the schema's default is
    compared against, and an operator shortening the tier's lifetime on one side
    without the other is a failure rather than a silent disagreement.
    """
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    configured = WorkingInterval.from_configuration(Configuration(environ={}, file_values={}))

    lifetime = cluster.one(
        INSERT_WITHOUT_EXPIRY,
        (session_id, PLAN_KEY, client_id, "{}"),
    )[0]

    assert lifetime == configured.interval


# ---------------------------------------------------------------------------
# Nothing depends on a working row
# ---------------------------------------------------------------------------


def test_no_table_holds_a_reference_to_a_working_row(cluster: Cluster) -> None:
    """The catalog names no foreign key targeting the tier's table.

    Read from the cluster rather than from the migration text, because a constraint
    that exists in a file and not in the catalog enforces nothing, and the schema
    this reads has every migration applied, so a reference added by any later
    generation would appear here.
    """
    inbound = cluster.rows(REFERENCES_TO_TABLE, (cluster.schema, WORKING_TABLE))

    assert inbound == [], f"a working row is referenced by {inbound}"

    for table in UNREFERENCING_TABLES:
        assert cluster.count(TABLE_PRESENT, (cluster.schema, table)) == 1, (
            f"{table} should exist in the schema this claim is read from"
        )
        referenced = {
            str(row[0]) for row in cluster.rows(REFERENCES_FROM_TABLE, (cluster.schema, table))
        }
        assert WORKING_TABLE not in referenced, f"{table} references a working row"


def test_a_working_row_references_the_session_and_the_tenant_it_belongs_to(
    cluster: Cluster,
) -> None:
    """The reference runs one way, which is what makes the tier disposable.

    A working row names the Session and the Client it belongs to, so removing either
    removes the scratch with it, and nothing names the working row back.
    """
    outbound = {
        str(row[0]) for row in cluster.rows(REFERENCES_FROM_TABLE, (cluster.schema, WORKING_TABLE))
    }

    assert outbound == OUTWARD_REFERENCES


# ---------------------------------------------------------------------------
# The expiry the cluster performs
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("quickened_expiry")
def test_the_cluster_physically_removes_a_row_whose_expiry_has_passed(cluster: Cluster) -> None:
    """An expired row leaves, and nothing this process sent removed it.

    The row is written through the tier's own write path with a short configured
    interval and a write instant taken from the cluster and moved back, so its stored
    expiry has already passed when it lands. It is then watched, and every statement
    this module sends while watching is recorded and asserted to remove nothing: no
    deletion, no truncation, no rewrite. What removes the row is therefore the
    cluster's own delete job and not a sweeper of this repository's.

    The wait is bounded and explained where the bound is declared. It runs last in
    this module because the shortened recurrence sweeps the whole table, so an earlier
    example's rows may expire while this one waits.
    """
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    interval = interval_of(SHORT_TTL_SECONDS)

    written = write_scratch(
        cluster.store,
        scratch(session_id, client_id, PLAN_KEY, FIRST_PLAN),
        interval=interval,
        now=cluster.reading() - timedelta(seconds=ALREADY_EXPIRED_SECONDS + interval.seconds),
    )

    assert written.expires_at < cluster.reading(), "the row lands already past its expiry"
    assert cluster.count(COUNT_KEYED_ROWS, (session_id, PLAN_KEY)) == 1

    mark = cluster.recorder.mark()
    elapsed = wait_for_removal(cluster, session_id, PLAN_KEY)

    assert elapsed is not None, (
        f"the cluster did not remove the expired row within {EXPIRY_BOUND_SECONDS} seconds"
    )
    assert mutating(cluster.recorder.since(mark)) == (), (
        "nothing outside the cluster removed the row"
    )
    assert read_scratch(cluster.store, session_id, PLAN_KEY, client_id) is None, (
        "a forgotten row reads as absent, exactly as a row that never existed does"
    )
