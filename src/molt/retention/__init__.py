"""Database-enforced retention: the expiry a write stores, the TTL configuration, the report.

Retention here is a property of the cluster rather than of any process this project
ships. Every content row carries the instant it stops being live, the cluster holds
the schedule that removes expired rows, and nothing outside the cluster is asked to
remember anything. If every process here were stopped forever, expired content would
still leave.

Four claims carry this module.

**An expiry is the write instant plus the owning Client's interval, and the instant
is injected rather than read here.** The arithmetic is one function over an aware
instant and a positive interval, so the same computation serves the ingest path, the
derivation path, and a test that drives a ten-year interval without waiting for one.
The interval belongs to the Client row, because a Jurisdiction's retention period is
configured per Client and the surface holds only the period applied where a Client
names none.

**The working tier gets a fixed interval, and it is fixed for a reason that is not
about jurisdictions at all.** Working state's lifetime is a property of the tier: a
scratch value is disposable because nothing may depend on it, not because a
regulator said so. So the working table's expiry comes from the one surface key that
states the tier's own interval, read through the shape the working-tier writer
already owns, rather than from any Client's regime. Configuring a Jurisdiction
interval there would make a tier whose disposability is nominal for a Client whose
regime is generous.

**The checkpoint tables are configured with no expiry at all, and the absence is the
design.** A checkpoint's whole value is outliving the rows it commits to: a chain
segment whose checkpoint expired alongside it proves nothing later. So this module
holds those table names in a list of tables it refuses to configure and offers a
check that they carry no expiry parameter, which is how an accidental later
configuration is caught rather than assumed absent.

**The configuration is verified by reading the descriptor back, and that read-back is
the point.** The platform accepts a Row-Level TTL configuration applied to a table
created earlier in the same transaction, reports success, commits, and stores no such
parameter. A caller that checked only for an error would conclude the tier expires
its rows while the cluster sweeps nothing, which is the one failure a disposable tier
cannot tolerate quietly. So every configuring call here reads the stored parameters
back through the catalogue and raises an error naming the table when a parameter it
asked for is absent, and the applying statement's own outcome is never treated as
evidence. The stored parameters are read with the table name as a bound value, so the
read-back needs no interpolated identifier.

Every statement is a whole module-level literal. The configuring statements name one
table each, which is what keeps a table identifier out of any format string, and the
read-back and the report bind every value they carry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final
from uuid import UUID

from molt.collector.handler import retention_interval
from molt.config.resolve import Configuration, load_configuration
from molt.errors import StoreError
from molt.models.event import require_aware
from molt.store import Cursor, MemoryStore
from molt.store.working import WORKING_TTL_KEY, WorkingInterval

__all__ = [
    "ALTER_DERIVED_ARTIFACT_TTL_STATEMENT",
    "ALTER_EMBEDDING_TTL_STATEMENT",
    "ALTER_LEDGER_TTL_STATEMENT",
    "ALTER_WORKING_MEMORY_TTL_STATEMENT",
    "COMPONENT",
    "CONTENT_TTL_CRON",
    "DEFAULT_INTERVAL_KEY",
    "EXPIRING_SOON_WINDOW",
    "NO_TTL_TABLES",
    "READ_STORAGE_PARAMETERS_STATEMENT",
    "REPORT_STATEMENT",
    "TTL_DELETE_BATCH_SIZE",
    "TTL_ENABLED_PARAMETER",
    "TTL_ENABLED_VALUE",
    "TTL_EXPIRATION_COLUMN",
    "TTL_SPECS",
    "WORKING_TTL_CRON",
    "WORKING_TTL_KEY",
    "ClientRetention",
    "ClientRetentionReport",
    "RetentionNotConfiguredError",
    "TtlReadback",
    "TtlSpec",
    "apply_ttl",
    "configure_retention",
    "confirm_no_ttl",
    "confirm_ttl",
    "default_interval",
    "expiry_for",
    "report",
    "spec_for",
    "stored_parameters",
    "working_interval",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "retention"

# The surface key holding the interval applied where a Client names none. The
# parser is the one the ingest path already reads this key with, so one key has one
# reading and a deployment cannot get two answers about its own default.
DEFAULT_INTERVAL_KEY: Final[str] = "MOLT_RETENTION_DEFAULT_INTERVAL"

# How far ahead the report looks when it counts what is about to expire. The window
# is stated by the reporting obligation itself rather than configured, because a
# figure an operator could move would make two reports incomparable.
EXPIRING_SOON_WINDOW: Final[timedelta] = timedelta(days=7)

# The column every expiry expression names, and the schedules and batch size the
# migrations declare. These are restated here rather than parsed out of the
# migration files, because this module's job is to assert what the descriptor must
# hold: a value read from the same place it is compared against would compare
# nothing.
TTL_EXPIRATION_COLUMN: Final[str] = "expires_at"
CONTENT_TTL_CRON: Final[str] = "@daily"
WORKING_TTL_CRON: Final[str] = "@hourly"
TTL_DELETE_BATCH_SIZE: Final[int] = 500

# The parameter the platform derives when an expiry expression is set, and the value
# it reads when expiry is live. Checked alongside the parameters that were asked for,
# so the read-back confirms the cluster considers the sweep enabled rather than
# merely that it stored three strings.
TTL_ENABLED_PARAMETER: Final[str] = "ttl"
TTL_ENABLED_VALUE: Final[str] = "on"

# The parameter names a configuration asks for, in the order the statements state
# them.
_EXPIRATION_PARAMETER: Final[str] = "ttl_expiration_expression"
_CRON_PARAMETER: Final[str] = "ttl_job_cron"
_BATCH_PARAMETER: Final[str] = "ttl_delete_batch_size"

# How many columns each read shape carries, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_PARAMETERS_ROW_WIDTH: Final[int] = 1
_REPORT_ROW_WIDTH: Final[int] = 6

# What the transactions of this module are called in a log record and in the note an
# exhausted retry attaches.
_REPORT_LABEL: Final[str] = "retention_report"


class RetentionNotConfiguredError(StoreError):
    """A table's stored Row-Level TTL parameters are absent, or one is not as asked.

    Raised by the read-back rather than by the configuring statement, because the
    configuring statement reports success in exactly the case this names. The
    message carries the table, so an operator reading it knows which tier expires
    nothing.
    """


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

# One configuring statement per table, each a whole literal naming its own table.
# The expression form is used rather than a fixed interval after insertion because
# the retention period varies per Jurisdiction and therefore per row: the write path
# sets the column and the sweep reads whatever the column holds, so a Jurisdiction
# with a different period needs no schema change. Re-declaring the same parameters
# on a table that already carries them leaves the configuration as it was, so each
# is re-runnable.
ALTER_LEDGER_TTL_STATEMENT: Final[str] = (
    "ALTER TABLE ledger SET ("
    "ttl_expiration_expression = 'expires_at', "
    "ttl_job_cron = '@daily', "
    "ttl_delete_batch_size = 500)"
)
ALTER_DERIVED_ARTIFACT_TTL_STATEMENT: Final[str] = (
    "ALTER TABLE derived_artifact SET ("
    "ttl_expiration_expression = 'expires_at', "
    "ttl_job_cron = '@daily', "
    "ttl_delete_batch_size = 500)"
)
ALTER_EMBEDDING_TTL_STATEMENT: Final[str] = (
    "ALTER TABLE embedding SET ("
    "ttl_expiration_expression = 'expires_at', "
    "ttl_job_cron = '@daily', "
    "ttl_delete_batch_size = 500)"
)

# The working tier sweeps hourly against an hourly interval. A daily sweep here
# would leave a row whose stated lifetime is one hour resident for up to a day, and
# the tier's disposability would then be a claim in a document rather than a
# property of the cluster.
ALTER_WORKING_MEMORY_TTL_STATEMENT: Final[str] = (
    "ALTER TABLE working_memory SET ("
    "ttl_expiration_expression = 'expires_at', "
    "ttl_job_cron = '@hourly', "
    "ttl_delete_batch_size = 500)"
)

# The read-back. The catalogue is read rather than the platform's own descriptive
# statement, because a catalogue row takes the table name as a bound value while
# that statement would take it as an identifier, and an interpolated identifier is
# the one thing no statement here does. The schema is the session's own, so a
# module-scoped test schema is read rather than a table of the same name elsewhere.
READ_STORAGE_PARAMETERS_STATEMENT: Final[str] = (
    "SELECT c.reloptions FROM pg_catalog.pg_class AS c "
    "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
    "WHERE c.relname = %s AND n.nspname = current_schema()"
)

# The report: one statement, one pass, one row per Client. The three content tables
# are unioned into one artifact relation so a Client with rows in all three is
# counted once against each bound rather than three times, and the join is an outer
# one so a Client holding nothing still reports its regime with two zeros. Both
# instants are bound from one reading, so the two counts cannot be taken against
# different presents.
REPORT_STATEMENT: Final[str] = (
    "WITH horizon AS (SELECT %s::TIMESTAMPTZ AS present, %s::TIMESTAMPTZ AS soon), "
    "artifact AS ("
    "SELECT client_id, expires_at FROM ledger "
    "UNION ALL SELECT owner_client_id AS client_id, expires_at FROM derived_artifact "
    "UNION ALL SELECT client_id, expires_at FROM embedding"
    ") "
    "SELECT c.id, c.slug, c.jurisdiction, c.retention_interval, "
    "count(a.expires_at) FILTER ("
    "WHERE a.expires_at > h.present AND a.expires_at <= h.soon) AS expiring_soon, "
    "count(a.expires_at) FILTER (WHERE a.expires_at <= h.present) AS already_expired "
    "FROM client AS c CROSS JOIN horizon AS h "
    "LEFT JOIN artifact AS a ON a.client_id = c.id "
    "GROUP BY c.id, c.slug, c.jurisdiction, c.retention_interval "
    "ORDER BY c.slug ASC"
)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TtlSpec:
    """One table's Row-Level TTL configuration: the statement and what it must store.

    The statement and the expected parameters are held together on purpose. A
    configuration whose read-back checked something other than what the statement
    asked for would pass while the tier expired nothing, which is precisely the
    failure this shape exists to catch.
    """

    table: str
    statement: str
    cron: str
    delete_batch_size: int = TTL_DELETE_BATCH_SIZE
    expiration_column: str = TTL_EXPIRATION_COLUMN

    def __post_init__(self) -> None:
        """Refuse a specification that names no table or asks for no rows per batch."""
        if not self.table:
            raise ValueError("a Row-Level TTL specification names the table it configures")
        if self.delete_batch_size <= 0:
            raise ValueError("a Row-Level TTL delete batch covers a positive number of rows")

    @property
    def expected(self) -> Mapping[str, str]:
        """The parameters the descriptor must hold once this configuration is applied."""
        return MappingProxyType(
            {
                TTL_ENABLED_PARAMETER: TTL_ENABLED_VALUE,
                _EXPIRATION_PARAMETER: self.expiration_column,
                _CRON_PARAMETER: self.cron,
                _BATCH_PARAMETER: str(self.delete_batch_size),
            }
        )


# Every table the cluster sweeps, in migration order. The working table is last and
# differs in exactly one parameter, which is the difference the tier is about.
TTL_SPECS: Final[tuple[TtlSpec, ...]] = (
    TtlSpec(table="ledger", statement=ALTER_LEDGER_TTL_STATEMENT, cron=CONTENT_TTL_CRON),
    TtlSpec(
        table="derived_artifact",
        statement=ALTER_DERIVED_ARTIFACT_TTL_STATEMENT,
        cron=CONTENT_TTL_CRON,
    ),
    TtlSpec(table="embedding", statement=ALTER_EMBEDDING_TTL_STATEMENT, cron=CONTENT_TTL_CRON),
    TtlSpec(
        table="working_memory",
        statement=ALTER_WORKING_MEMORY_TTL_STATEMENT,
        cron=WORKING_TTL_CRON,
    ),
)

# The tables this module configures no expiry on, named rather than merely omitted.
# A checkpoint proves a chain segment was already this way; a checkpoint that
# expired alongside the rows it commits to would prove nothing later, so the absence
# is stated here so that a later reader adding one has to remove a name first.
NO_TTL_TABLES: Final[tuple[str, ...]] = ("ledger_checkpoint", "checkpoint_session")


@dataclass(frozen=True, slots=True)
class TtlReadback:
    """What the descriptor held for one table after its configuration was applied."""

    table: str
    parameters: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ClientRetention:
    """One Client's retention regime: the Jurisdiction and the interval it resolves to.

    The interval is carried rather than looked up per write, so a batch of writes
    for one Client computes its expiries from one reading of one row.
    """

    client_id: UUID
    jurisdiction: str
    interval: timedelta

    def __post_init__(self) -> None:
        """Refuse a regime whose interval gives a row no life at all."""
        if self.interval <= timedelta(0):
            raise ValueError("a Client retention interval covers a positive duration")

    def expiry_from(self, written_at: datetime) -> datetime:
        """The instant an Artifact written at a reading stops being live."""
        return expiry_for(written_at, self.interval)


@dataclass(frozen=True, slots=True)
class ClientRetentionReport:
    """One Client's line of the retention report.

    Both counts are taken against one bound reading, and both are counts of stored
    rows rather than of rows a caller predicted, because the report exists to say
    what the cluster holds.
    """

    client_id: UUID
    slug: str
    jurisdiction: str
    interval: timedelta
    expiring_soon: int
    already_expired: int


# ---------------------------------------------------------------------------
# The expiry a write stores
# ---------------------------------------------------------------------------


def expiry_for(written_at: datetime, interval: timedelta) -> datetime:
    """The expiry to store on an Artifact written at an instant under an interval.

    This is the whole of the arithmetic, deliberately: one function, no clock of its
    own, and no source of an interval of its own. A caller passes the instant it is
    storing on the row so that a row's write instant and its expiry cannot disagree
    by however long a statement took to reach the cluster.

    Args:
        written_at: The write instant, timezone aware.
        interval: The retention interval of the Artifact's Jurisdiction.

    Returns:
        The instant after which the cluster may remove the row.

    Raises:
        ValueError: The instant carries no offset, or the interval is not positive.
    """
    if interval <= timedelta(0):
        raise ValueError("a retention interval covers a positive duration")
    return require_aware(written_at, "an Artifact write instant") + interval


def default_interval(configuration: Configuration | None = None) -> timedelta:
    """The interval applied where a Client's Jurisdiction names no other.

    Read from the configuration surface through the same parser the ingest path
    reads that key with, so one key has one reading.
    """
    return retention_interval(load_configuration() if configuration is None else configuration)


def working_interval(configuration: Configuration | None = None) -> WorkingInterval:
    """The working tier's own interval, which is a property of the tier.

    Fixed at the configured count of seconds rather than taken from a Client's
    Jurisdiction: a scratch value is disposable because nothing may depend on it,
    and a Client with a generous regime must not be given a working tier that keeps
    scratch state for as long as its content. Read through the shape the
    working-tier writer already owns, so the interval the writer stores on a row and
    the interval this module reasons about are the same value.
    """
    return WorkingInterval.from_configuration(configuration)


# ---------------------------------------------------------------------------
# The TTL configuration, and the read-back that is the point of it
# ---------------------------------------------------------------------------


def spec_for(table: str) -> TtlSpec:
    """The configuration one table is swept under.

    Raises:
        KeyError: The table is not one this module configures an expiry on.
    """
    for spec in TTL_SPECS:
        if spec.table == table:
            return spec
    raise KeyError(f"no Row-Level TTL configuration is declared for the table {table}")


def stored_parameters(cursor: Cursor, table: str) -> Mapping[str, str]:
    """The storage parameters the cluster holds for one table, as the catalogue reports.

    Returns:
        The stored parameters by name, with each value unquoted. An empty mapping
        means the table holds none, which is what a silently dropped configuration
        looks like and is also what a table configured with no expiry looks like.

    Raises:
        StoreError: The catalogue names no such table in this schema.
    """
    cursor.execute(READ_STORAGE_PARAMETERS_STATEMENT, (table,))
    row = cursor.fetchone()
    if row is None:
        raise StoreError(
            f"the catalogue holds no table named {table} in this schema, so its "
            "Row-Level TTL configuration cannot be read back"
        )
    return _parameters_of(_column(row, 0, _PARAMETERS_ROW_WIDTH))


def confirm_ttl(cursor: Cursor, spec: TtlSpec) -> Mapping[str, str]:
    """Read a table's configuration back and confirm every asked-for parameter is there.

    This is the verification the design leans on rather than the configuring
    statement's outcome, because that statement reports success in the case that
    stores nothing.

    Returns:
        The stored parameters, so a caller can record what it confirmed.

    Raises:
        RetentionNotConfiguredError: A parameter is absent, or one holds a value
            other than the one asked for. The message names the table.
    """
    stored = stored_parameters(cursor, spec.table)
    absent = tuple(name for name in spec.expected if name not in stored)
    if absent:
        raise RetentionNotConfiguredError(
            f"the table {spec.table} stores no {', '.join(absent)} parameter after its "
            "Row-Level TTL configuration was applied, so this memory tier expires no row"
        )
    disagreeing = tuple(name for name, value in spec.expected.items() if stored[name] != value)
    if disagreeing:
        raise RetentionNotConfiguredError(
            f"the table {spec.table} stores a different {', '.join(disagreeing)} than the "
            "Row-Level TTL configuration asked for, so this memory tier expires rows on "
            "terms nobody chose"
        )
    return stored


def apply_ttl(cursor: Cursor, spec: TtlSpec) -> TtlReadback:
    """Configure Row-Level TTL on one table and confirm the descriptor holds it.

    The confirmation is not optional and not a separate call a caller may forget:
    applying without reading back would leave the one failure mode this whole path
    exists for undetected.
    """
    cursor.execute(spec.statement)
    return TtlReadback(table=spec.table, parameters=confirm_ttl(cursor, spec))


def confirm_no_ttl(cursor: Cursor, table: str) -> Mapping[str, str]:
    """Confirm one table carries no expiry configuration at all.

    Used for the checkpoint tables, where the absence is the design rather than an
    omission: a checkpoint outlives the rows it commits to.

    Returns:
        The stored parameters, which carry no expiry parameter.

    Raises:
        RetentionNotConfiguredError: The table carries an expiry parameter, so a
            checkpoint would leave with the rows it attests to.
    """
    stored = stored_parameters(cursor, table)
    present = tuple(name for name in stored if name.startswith(TTL_ENABLED_PARAMETER))
    if present:
        raise RetentionNotConfiguredError(
            f"the table {table} carries the {', '.join(present)} parameter where no "
            "expiry may be configured, because a checkpoint has to outlive the rows it "
            "commits to"
        )
    return stored


def configure_retention(store: MemoryStore) -> tuple[TtlReadback, ...]:
    """Configure every swept table, confirm each descriptor, and refuse the rest.

    Each table is configured on a connection framing no transaction of its own, so
    the configuring statement and its read-back each travel in an implicit
    transaction. That is what makes the read-back meaningful: a configuration read
    back inside the transaction that applied it would report the descriptor the
    transaction intends rather than the one the cluster committed, which is the
    difference the silent failure hides in.

    Returns:
        What the descriptor held for each configured table, in declared order.

    Raises:
        RetentionNotConfiguredError: A configured table stores no such parameter, or
            a checkpoint table carries one.
    """
    confirmed = tuple(store.read(_applier(spec)) for spec in TTL_SPECS)
    for table in NO_TTL_TABLES:
        store.read(_refuser(table))
    return confirmed


def _applier(spec: TtlSpec) -> Callable[[Cursor], TtlReadback]:
    """One table's configuration and read-back, as a body a connection can be lent."""

    def body(cursor: Cursor) -> TtlReadback:
        return apply_ttl(cursor, spec)

    return body


def _refuser(table: str) -> Callable[[Cursor], Mapping[str, str]]:
    """One table's absence-of-expiry check, as a body a connection can be lent."""

    def body(cursor: Cursor) -> Mapping[str, str]:
        return confirm_no_ttl(cursor, table)

    return body


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def report(
    store: MemoryStore,
    *,
    now: datetime | None = None,
    window: timedelta = EXPIRING_SOON_WINDOW,
) -> tuple[ClientRetentionReport, ...]:
    """Report every Client's regime and what is about to leave under it.

    Args:
        store: The store the one statement runs on.
        now: The present the two counts are taken against, timezone aware, or None
            to take a reading. Injected so a report over a fabricated horizon needs
            no waiting.
        window: How far ahead the near-expiry count looks. Defaults to the window
            the reporting obligation states.

    Returns:
        One line per Client, in slug order, each carrying the Jurisdiction, the
        interval, the count expiring inside the window, and the count already
        expired.
    """
    if window <= timedelta(0):
        raise ValueError("the near-expiry window covers a positive duration")
    present = _reading(now)

    def body(cursor: Cursor) -> tuple[ClientRetentionReport, ...]:
        cursor.execute(REPORT_STATEMENT, (present, present + window))
        return tuple(_report_of(row) for row in cursor.fetchall())

    return store.in_serializable(body, label=_REPORT_LABEL)


# ---------------------------------------------------------------------------
# Parameters and row decoding
# ---------------------------------------------------------------------------


def _reading(now: datetime | None) -> datetime:
    """The instant every bound of one report is taken against."""
    if now is None:
        return datetime.now(UTC)
    return require_aware(now, "a retention report instant")


def _column(row: Sequence[object], index: int, width: int) -> object:
    """One column of a row whose width has been checked."""
    if len(row) != width:
        raise StoreError(f"a result row carries {len(row)} column(s) where {width} were selected")
    return row[index]


def _parameters_of(value: object) -> Mapping[str, str]:
    """Read the catalogue's storage-parameter list into named values.

    A parameter arrives as one assignment per entry, with a text value quoted and a
    numeric one bare, so the quotes are stripped and the comparison is on text. The
    absent case is a table carrying none, which reads as no entries rather than as a
    fault: a table with no configuration is exactly what the silent failure leaves
    behind, and this function's caller is the one that decides whether that is
    permitted.
    """
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, (list, tuple)):
        raise StoreError(
            f"the catalogue reported {type(value).__name__} where a storage-parameter list was read"
        )
    parameters: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, str):
            raise StoreError("a storage-parameter entry is not text")
        name, separator, raw = entry.partition("=")
        if not separator:
            continue
        parameters[name.strip()] = raw.strip().strip("'")
    return MappingProxyType(parameters)


def _report_of(row: Sequence[object]) -> ClientRetentionReport:
    """Build one report line from a selected row."""
    return ClientRetentionReport(
        client_id=_as_uuid(_column(row, 0, _REPORT_ROW_WIDTH)),
        slug=_as_str(row[1]),
        jurisdiction=_as_str(row[2]),
        interval=_as_interval(row[3]),
        expiring_soon=_as_count(row[4]),
        already_expired=_as_count(row[5]),
    )


def _unexpected(value: object, expected: str) -> StoreError:
    """The failure for a column whose type is not the one the schema declares."""
    return StoreError(f"a selected column holds {type(value).__name__} where {expected} was read")


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise _unexpected(value, "an identifier")


def _as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _unexpected(value, "text")


def _as_count(value: object) -> int:
    if isinstance(value, bool):
        raise _unexpected(value, "a whole number")
    if isinstance(value, int):
        return value
    raise _unexpected(value, "a whole number")


def _as_interval(value: object) -> timedelta:
    if isinstance(value, timedelta):
        return value
    raise _unexpected(value, "an interval")
