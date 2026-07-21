"""Row-Level TTL against a live instance: what the descriptor stores, and what it must not.

The unit surface asserts the shape of the statements and the arithmetic of an expiry.
This module asserts the four things only a cluster can answer, and it answers them by
reading the stored configuration back rather than by trusting the statement that
applied it.

**Every content table carries the expiry parameters the migration asked for.** The
parameters are read out of the catalogue and compared name by name, because the
platform accepts a Row-Level TTL configuration, reports success, commits, and stores
no such parameter when the table was created earlier in the same transaction. A test
that checked for a raised error would pass against a tier that sweeps nothing.

**The working tier carries its own interval, and the interval is the tier's.** The
sweep is hourly rather than daily, and a row written with no expiry of its own lives
for the fixed working interval, which is asserted from the stored row rather than from
the column default alone.

**The checkpoint tables carry no expiry at all.** The absence is the design: a
checkpoint proves a chain segment was already this way, and a checkpoint that left
alongside the rows it commits to would prove nothing later.

**The read-back refuses an absent configuration by name.** A probe table with no
expiry, and a probe table configured on terms other than the ones asked for, each
raise an error naming the table, so the guard is known to fire rather than assumed to.

**Validates: Requirements 14.1, 14.5, 14.6, 14.7, 14.8, 42.9, 45.10**
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.retention import (
    CONTENT_TTL_CRON,
    EXPIRING_SOON_WINDOW,
    NO_TTL_TABLES,
    TTL_DELETE_BATCH_SIZE,
    TTL_ENABLED_PARAMETER,
    TTL_ENABLED_VALUE,
    TTL_EXPIRATION_COLUMN,
    TTL_SPECS,
    WORKING_TTL_CRON,
    ClientRetention,
    RetentionNotConfiguredError,
    TtlSpec,
    confirm_no_ttl,
    confirm_ttl,
    report,
    spec_for,
    stored_parameters,
    working_interval,
)
from molt.store import Connection, Cursor, MemoryStore
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places directly, so a report has something to count.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction, retention_interval) "
    "VALUES (%s, %s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
INSERT_DERIVED: Final[str] = (
    "INSERT INTO derived_artifact "
    "(id, kind, owner_client_id, body, content_digest, derivation_method, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
# The working write deliberately names no expiry, so the stored value is the tier's
# own default rather than one this module chose.
INSERT_WORKING: Final[str] = (
    "INSERT INTO working_memory (session_id, scratch_key, client_id, value) "
    "VALUES (%s, %s, %s, %s::JSONB) RETURNING updated_at, expires_at"
)

# A table shaped like a content table and configured with nothing, which is what the
# silent failure leaves behind. Two statements, each a whole literal naming its own
# table, because no statement here interpolates an identifier.
CREATE_PROBE: Final[str] = (
    "CREATE TABLE IF NOT EXISTS retention_probe ("
    "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
    "expires_at TIMESTAMPTZ NOT NULL)"
)
CONFIGURE_PROBE_DAILY: Final[str] = (
    "ALTER TABLE retention_probe SET ("
    "ttl_expiration_expression = 'expires_at', "
    "ttl_job_cron = '@daily', "
    "ttl_delete_batch_size = 500)"
)

PROBE_TABLE: Final[str] = "retention_probe"

# The values the placed rows carry. None is what an assertion turns on.
JURISDICTION: Final[str] = "eu"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"
ARTIFACT_BODY: Final[str] = "read the descriptor back before believing the tier expires anything"
DERIVATION_METHOD: Final[str] = "distilled"
ARTIFACT_KIND: Final[str] = "summary"
SCRATCH_KEY: Final[str] = "plan-under-revision"
SCRATCH_VALUE: Final[str] = '{"step": 1}'

# The regime the placed Client is given, and the offsets the report is driven over.
CLIENT_INTERVAL: Final[timedelta] = timedelta(days=90)
INSIDE_WINDOW: Final[timedelta] = timedelta(days=3)
OUTSIDE_WINDOW: Final[timedelta] = timedelta(days=30)
ALREADY_GONE: Final[timedelta] = timedelta(days=1)

# How closely a stored expiry has to match the interval it was derived from. Both
# columns default to the statement's own instant, so the two readings agree, and this
# tolerance covers nothing but the platform's own microsecond rounding.
TOLERANCE: Final[timedelta] = timedelta(seconds=1)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


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

    def one(self, statement: str, params: tuple[object, ...]) -> tuple[Any, ...]:
        """The single row a statement is expected to produce."""
        produced = self.rows(statement, params)
        assert len(produced) == 1, f"the statement produced {len(produced)} rows where one was read"
        return produced[0]

    def parameters(self, table: str) -> dict[str, str]:
        """The storage parameters the cluster holds for one table."""
        with self.connection.cursor() as cursor:
            return dict(stored_parameters(cursor, table))

    def client(self, *, interval: timedelta = CLIENT_INTERVAL) -> UUID:
        """Place one Client carrying a Jurisdiction and its retention interval."""
        identifier = uuid4()
        self.send(
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:12]}", "Tenant", JURISDICTION, interval),
        )
        return identifier

    def session(self, client_id: UUID) -> UUID:
        """Place one Session of a Client and return its identifier."""
        identifier = uuid4()
        self.send(INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
        return identifier

    def artifact(self, client_id: UUID, expires_at: datetime) -> UUID:
        """Place one Derived_Artifact expiring at a chosen instant."""
        identifier = uuid4()
        self.send(
            INSERT_DERIVED,
            (
                identifier,
                ARTIFACT_KIND,
                client_id,
                ARTIFACT_BODY,
                hashlib.sha256(identifier.bytes).hexdigest(),
                DERIVATION_METHOD,
                expires_at,
            ),
        )
        return identifier


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema."""
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


# ---------------------------------------------------------------------------
# What the descriptor stores
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", TTL_SPECS, ids=[spec.table for spec in TTL_SPECS])
def test_every_swept_table_stores_the_parameters_that_were_asked_for(
    cluster: Cluster,
    spec: TtlSpec,
) -> None:
    """The migration's configuration is present in the committed descriptor, not merely sent."""
    stored = cluster.parameters(spec.table)

    assert stored, f"the table {spec.table} stores no storage parameter at all"
    assert stored[TTL_ENABLED_PARAMETER] == TTL_ENABLED_VALUE
    assert stored["ttl_expiration_expression"] == TTL_EXPIRATION_COLUMN
    assert stored["ttl_job_cron"] == spec.cron
    assert stored["ttl_delete_batch_size"] == str(TTL_DELETE_BATCH_SIZE)

    with cluster.connection.cursor() as cursor:
        confirmed = confirm_ttl(cursor, spec)
    assert dict(confirmed) == stored


def test_the_content_tables_are_swept_on_the_content_schedule(cluster: Cluster) -> None:
    """The three content tables share one schedule, which is not the working tier's."""
    for table in ("ledger", "derived_artifact", "embedding"):
        assert cluster.parameters(table)["ttl_job_cron"] == CONTENT_TTL_CRON
    assert CONTENT_TTL_CRON != WORKING_TTL_CRON


# ---------------------------------------------------------------------------
# The working tier's own interval
# ---------------------------------------------------------------------------


def test_the_working_tier_carries_the_fixed_interval_rather_than_a_regime(
    cluster: Cluster,
) -> None:
    """A working row lives for the tier's interval whatever the owning Client's regime is."""
    spec = spec_for("working_memory")
    fixed = working_interval()
    assert fixed.seconds == 3600, "the working tier's interval is fixed by the tier"

    assert cluster.parameters(spec.table)["ttl_job_cron"] == WORKING_TTL_CRON

    client_id = cluster.client(interval=CLIENT_INTERVAL)
    session_id = cluster.session(client_id)
    updated_at, expires_at = cluster.one(
        INSERT_WORKING,
        (session_id, SCRATCH_KEY, client_id, SCRATCH_VALUE),
    )

    stored_life = expires_at - updated_at
    assert abs(stored_life - fixed.interval) <= TOLERANCE
    assert stored_life < CLIENT_INTERVAL, (
        "working state's lifetime is a property of the tier, not of the Client's regime"
    )


# ---------------------------------------------------------------------------
# Where no expiry may be configured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", NO_TTL_TABLES)
def test_a_checkpoint_table_carries_no_expiry_at_all(cluster: Cluster, table: str) -> None:
    """A checkpoint has to outlive the rows it commits to, so no sweep may reach it."""
    stored = cluster.parameters(table)

    assert not [name for name in stored if name.startswith(TTL_ENABLED_PARAMETER)]
    with cluster.connection.cursor() as cursor:
        confirm_no_ttl(cursor, table)


def test_the_absence_check_refuses_a_checkpoint_table_that_gained_an_expiry(
    cluster: Cluster,
) -> None:
    """The refusal fires on a table that does carry one, so the check is not vacuous."""
    cluster.send(CREATE_PROBE)
    cluster.send(CONFIGURE_PROBE_DAILY)

    with (
        cluster.connection.cursor() as cursor,
        pytest.raises(RetentionNotConfiguredError, match=PROBE_TABLE),
    ):
        confirm_no_ttl(cursor, PROBE_TABLE)


# ---------------------------------------------------------------------------
# The read-back guard
# ---------------------------------------------------------------------------


def test_the_read_back_raises_naming_the_table_when_no_parameter_is_stored(
    cluster: Cluster,
) -> None:
    """This is the silent failure made loud: nothing raised, nothing stored, so read back."""
    absent = TtlSpec(
        table="checkpoint_session",
        statement="ALTER TABLE checkpoint_session SET (ttl_job_cron = '@daily')",
        cron=CONTENT_TTL_CRON,
    )

    with (
        cluster.connection.cursor() as cursor,
        pytest.raises(RetentionNotConfiguredError, match="checkpoint_session") as raised,
    ):
        confirm_ttl(cursor, absent)

    assert "expires no row" in str(raised.value)


def test_the_read_back_raises_when_a_stored_parameter_is_not_the_one_asked_for(
    cluster: Cluster,
) -> None:
    """A tier swept on terms nobody chose is a finding, not a pass."""
    cluster.send(CREATE_PROBE)
    cluster.send(CONFIGURE_PROBE_DAILY)
    disagreeing = TtlSpec(
        table=PROBE_TABLE,
        statement=CONFIGURE_PROBE_DAILY,
        cron=WORKING_TTL_CRON,
    )

    with (
        cluster.connection.cursor() as cursor,
        pytest.raises(RetentionNotConfiguredError, match=PROBE_TABLE),
    ):
        confirm_ttl(cursor, disagreeing)


def test_the_read_back_names_a_table_the_catalogue_does_not_hold(cluster: Cluster) -> None:
    """A table nobody created cannot be reported as configured either."""
    with cluster.connection.cursor() as cursor, pytest.raises(Exception, match="no table named"):
        stored_parameters(cursor, "retention_absent_table")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_the_report_counts_agree_with_the_stored_rows(cluster: Cluster) -> None:
    """Per Client, the Jurisdiction, the interval, and both counts come from the rows."""
    present = datetime.now(UTC)
    client_id = cluster.client()
    regime = ClientRetention(
        client_id=client_id,
        jurisdiction=JURISDICTION,
        interval=CLIENT_INTERVAL,
    )

    cluster.artifact(client_id, present + INSIDE_WINDOW)
    cluster.artifact(client_id, present + INSIDE_WINDOW + timedelta(hours=1))
    cluster.artifact(client_id, present + OUTSIDE_WINDOW)
    cluster.artifact(client_id, present - ALREADY_GONE)

    lines = {line.client_id: line for line in report(cluster.store, now=present)}

    assert client_id in lines
    line = lines[client_id]
    assert line.jurisdiction == regime.jurisdiction
    assert line.interval == regime.interval
    assert line.expiring_soon == 2, "two Artifacts fall inside the reporting window"
    assert line.already_expired == 1
    assert OUTSIDE_WINDOW > EXPIRING_SOON_WINDOW > INSIDE_WINDOW

    other = cluster.client()
    reported = {entry.client_id: entry for entry in report(cluster.store, now=present)}
    assert reported[other].expiring_soon == 0
    assert reported[other].already_expired == 0
    assert reported[client_id].expiring_soon == line.expiring_soon


def test_a_report_over_a_later_present_moves_rows_from_soon_to_expired(
    cluster: Cluster,
) -> None:
    """The two counts are taken against one injected reading, so the horizon can be driven."""
    present = datetime.now(UTC)
    client_id = cluster.client()
    cluster.artifact(client_id, present + INSIDE_WINDOW)

    def line_at(instant: datetime) -> tuple[int, int]:
        for entry in report(cluster.store, now=instant):
            if entry.client_id == client_id:
                return entry.expiring_soon, entry.already_expired
        raise AssertionError("the report omitted a Client the cluster holds")

    assert line_at(present) == (1, 0)
    assert line_at(present + INSIDE_WINDOW + timedelta(seconds=1)) == (0, 1)


def test_the_report_reads_through_the_store_rather_than_a_module_connection(
    cluster: Cluster,
) -> None:
    """The reporting path is the one a caller has, and it frames its own transaction."""

    def probe(cursor: Cursor) -> int:
        cursor.execute("SELECT count(*) FROM client")
        row = cursor.fetchone()
        assert row is not None
        counted = row[0]
        assert isinstance(counted, int)
        return counted

    assert cluster.store.read(probe) >= len(report(cluster.store))
