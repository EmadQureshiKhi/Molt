"""Historical reads against a live instance: the horizon, the clause, the refusal.

The unit module asserts the shape of what this module sends. This one asserts the
three things only a cluster can answer.

A read at an instant inside the horizon really returns the state of that instant.
The proof is a row written after the instant was taken: the current count sees it
and the historical count does not, so the clause reached the cluster and the
cluster honoured it. A composed clause that were merely well-formed would satisfy
a statement-shape assertion and fail this one.

A read at an instant beyond the horizon is refused with the horizon named, and no
read goes out. Every statement the store sends is recorded here, so the absence of
a read is asserted rather than assumed, and the absence of a second read at some
other instant with it. That is the whole of the no-fallback obligation, expressed
in statements.

The predicate agrees with the value the capability record holds. The row is seeded
by this module rather than by the probe that writes it in a deployment, because
this module owns the read side of that contract: the `capability` table, the
`gc_horizon_seconds` name, and a detail column carrying the horizon as a base-ten
count of seconds. Seeding it here is what lets the read side be asserted before
the probe exists, and what pins the contract the probe has to satisfy.

The reading the predicate measures against is injected rather than taken from a
clock, so the boundary cases cost no waiting.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import pytest

from molt.errors import HistoricalHorizonError, StoreError
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.historical import (
    AS_OF_STATEMENT_PREFIX,
    CAPABILITY_QUERY,
    GC_HORIZON_CAPABILITY,
    GcHorizon,
    gc_horizon,
    historical,
    within_gc_horizon,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes and reads the fixtures make, parameterised in full.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_CAPABILITY: Final[str] = (
    "UPSERT INTO capability (name, available, detail) VALUES (%s, %s, %s)"
)
DELETE_CAPABILITY: Final[str] = "DELETE FROM capability WHERE name = %s"
CLUSTER_READING: Final[str] = "SELECT now()"

# The caller statement every read in this module sends. It is a literal of this
# module, exactly as the statement a production caller passes is a literal of
# theirs, and its one value stays bound.
COUNT_CLIENTS: Final[str] = "SELECT count(*) FROM client WHERE jurisdiction = %s"

# The horizon this module seeds into the capability record, matching the value
# measured on the delivered cluster.
HORIZON_SECONDS: Final[int] = 4500

# The tenancy value the counted rows share, so the count is this module's own.
JURISDICTION: Final[str] = "eu"

# A connection and a cursor are typed loosely because the driver is reached
# through a fixture rather than imported, which keeps this module collectable
# with no driver installed.
DriverConnection = Any
DriverCursor = Any


class Clock(Protocol):
    """The two calls this module makes on the injected clock.

    Declared structurally rather than imported, because the fixtures load as a
    plugin under their own module name and a test reaches them as fixtures.
    """

    def now(self) -> datetime:
        """The current wall reading, timezone aware."""

    def advance(self, seconds: float) -> None:
        """Move the reading forward by a number of seconds."""


@dataclass(slots=True)
class Recorder:
    """Every statement the store sent, in order, across every connection it used."""

    statements: list[str] = field(default_factory=list)

    @property
    def issued(self) -> list[str]:
        """What the module sent, with the connection surface's own statements removed.

        Establishing the statement timeout, setting the search path, and resetting
        a returned connection belong to the pool rather than to the module under
        test, so a claim about what a read sent is read off this list.
        """
        return [
            query
            for query in self.statements
            if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT, SEARCH_PATH_STATEMENT)
        ]

    @property
    def historical_clauses(self) -> list[str]:
        """Every statement that placed a transaction at a historical instant."""
        return [query for query in self.statements if AS_OF_STATEMENT_PREFIX in query]


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


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the recorder."""

    store: MemoryStore
    connection: DriverConnection
    recorder: Recorder

    def client(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:12]}", "Tenant", JURISDICTION),
        )
        return identifier

    def reading(self) -> datetime:
        """The cluster's own current reading, so no example reads a local clock."""
        with self.connection.cursor() as cursor:
            cursor.execute(CLUSTER_READING)
            row = cursor.fetchone()
        assert row is not None
        moment = row[0]
        assert isinstance(moment, datetime)
        return moment

    def live_count(self) -> int:
        """How many Clients of this module's tenancy value the schema now holds."""
        with self.connection.cursor() as cursor:
            cursor.execute(COUNT_CLIENTS, (JURISDICTION,))
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, seed the horizon row, then build a store over it."""
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    send(
        fresh_schema,
        INSERT_CAPABILITY,
        (GC_HORIZON_CAPABILITY, True, str(HORIZON_SECONDS)),
    )

    recorder = Recorder()

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = RecordingConnection(opened, recorder)
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema, recorder=recorder)


@pytest.fixture
def without_horizon_row(cluster: Cluster) -> Iterator[None]:
    """Remove the seeded horizon row for one example, then put it back."""
    send(cluster.connection, DELETE_CAPABILITY, (GC_HORIZON_CAPABILITY,))
    try:
        yield
    finally:
        send(
            cluster.connection,
            INSERT_CAPABILITY,
            (GC_HORIZON_CAPABILITY, True, str(HORIZON_SECONDS)),
        )


# ---------------------------------------------------------------------------
# A read inside the horizon
# ---------------------------------------------------------------------------


def test_a_read_inside_the_horizon_returns_the_state_of_that_instant(cluster: Cluster) -> None:
    """A row written after the instant is absent from the read taken at it."""
    cluster.client()
    before = cluster.live_count()
    instant = cluster.reading()
    cluster.client()
    after = cluster.live_count()
    assert after == before + 1, "the second Client landed, so the two states differ"

    rows = historical(
        cluster.store,
        COUNT_CLIENTS,
        (JURISDICTION,),
        at=instant,
        now=cluster.reading(),
    )

    assert rows == ((before,),)
    assert len(cluster.recorder.historical_clauses) == 1


def test_a_read_inside_the_horizon_leaves_the_connection_usable(cluster: Cluster) -> None:
    """The historical transaction is committed, so the next read is an ordinary one."""
    instant = cluster.reading()

    historical(cluster.store, COUNT_CLIENTS, (JURISDICTION,), at=instant, now=cluster.reading())

    assert gc_horizon(cluster.store) == GcHorizon(seconds=HORIZON_SECONDS)


# ---------------------------------------------------------------------------
# A read beyond the horizon
# ---------------------------------------------------------------------------


def test_a_read_beyond_the_horizon_names_the_horizon_and_reads_nothing(cluster: Cluster) -> None:
    """The refusal names the measured horizon, and no read is attempted at all."""
    now = cluster.reading()
    beyond = now - timedelta(seconds=HORIZON_SECONDS + 60)
    cluster.recorder.statements.clear()

    with pytest.raises(HistoricalHorizonError, match=str(HORIZON_SECONDS)) as raised:
        historical(cluster.store, COUNT_CLIENTS, (JURISDICTION,), at=beyond, now=now)

    assert "no read was attempted" in str(raised.value)
    assert cluster.recorder.issued == [CAPABILITY_QUERY]
    assert cluster.recorder.historical_clauses == []
    assert COUNT_CLIENTS not in cluster.recorder.statements


@pytest.mark.usefixtures("without_horizon_row")
def test_the_horizon_is_read_from_the_capability_record_rather_than_assumed(
    cluster: Cluster,
) -> None:
    """With nothing probed there is no horizon, so no read is measured against one."""
    now = cluster.reading()
    cluster.recorder.statements.clear()

    with pytest.raises(StoreError, match="has not been probed"):
        historical(
            cluster.store,
            COUNT_CLIENTS,
            (JURISDICTION,),
            at=now - timedelta(seconds=1),
            now=now,
        )

    assert cluster.recorder.issued == [CAPABILITY_QUERY]
    assert cluster.recorder.historical_clauses == []


# ---------------------------------------------------------------------------
# The predicate, against the seeded value
# ---------------------------------------------------------------------------


def test_the_predicate_agrees_with_the_seeded_capability_value(cluster: Cluster) -> None:
    """The read horizon is the seeded one, and the predicate turns over at its floor."""
    horizon = gc_horizon(cluster.store)
    assert horizon == GcHorizon(seconds=HORIZON_SECONDS)

    now = cluster.reading()

    assert within_gc_horizon(cluster.store, now - horizon.interval, now=now) is True
    assert (
        within_gc_horizon(
            cluster.store,
            now - horizon.interval - timedelta(seconds=1),
            now=now,
        )
        is False
    )
    assert within_gc_horizon(cluster.store, now + timedelta(seconds=1), now=now) is False


def test_a_reachable_instant_falls_out_of_the_horizon_as_the_reading_advances(
    cluster: Cluster,
    time_source: Clock,
) -> None:
    """The predicate is a function of the reading, driven here rather than waited out."""
    horizon = gc_horizon(cluster.store)
    instant = time_source.now()

    assert within_gc_horizon(cluster.store, instant, now=time_source.now(), horizon=horizon) is True

    time_source.advance(HORIZON_SECONDS + 1)

    assert (
        within_gc_horizon(cluster.store, instant, now=time_source.now(), horizon=horizon) is False
    )
