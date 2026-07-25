"""Historical reads as corroboration: two instants, two routes, one number each.

`test_historical_horizon.py` asserts the mechanics of this read against a live
instance: that a read inside the horizon returns the state of its instant, that a
read beyond the horizon names the horizon and sends no statement, that no second
read is attempted at another instant, and that the horizon is read from the
capability record rather than assumed. None of that is repeated here.

What this module adds is the shape Requirement 20.6 asks for, which is a pair
rather than a single read. A certificate names two instants, and where both fall
inside the horizon the builder additionally runs the historical read and records
whether it agrees with the counts it derived from append-only evidence. Agreement
between two independent routes to the same number is the whole value of that
step, so what is asserted here is the agreement rather than either number on its
own:

- the historical route counts the Attribution_Versions carrying no superseding
  reference, at each instant, through `AS OF SYSTEM TIME`;
- the derived route counts the same thing forward from the Ledger, as the
  difference between the attribution records and the closure records appended at
  or before that instant, with no historical clause anywhere in it.

The two routes read different tables by different means and are seeded by one
sequence of writes, so they agree only if the clause reached the cluster and the
cluster honoured it.

The horizon is the one this cluster reports, measured by the probe rather than
seeded, because the condition Requirement 20.6 is stated under is a fact about the
cluster at the moment a certificate is assembled.

Two things this module deliberately does not do. It appends no Ledger row through
the chain append, because the derived route needs recorded instants and an
attribution history rather than a chain, and the chain is asserted live in
`test_ledger_chain_live.py`. And it asserts nothing about what the store sends,
because the statement-level claims about a refused read are made in
`test_historical_horizon.py` with a recording connection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.store import Connection, MemoryStore
from molt.store.capability import probe_gc_horizon
from molt.store.historical import GcHorizon, gc_horizon, historical, within_gc_horizon
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# Direct writes the fixtures make, parameterised in full. This module owns no
# Client insert, no Artifact write, no attribution write, and no Ledger append, so
# every row its two routes read is placed here.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACT_ROW: Final[str] = (
    "INSERT INTO derived_artifact (id, kind, owner_client_id, body, content_digest, "
    "derivation_method, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
INSERT_SESSION_ROW: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, started_at) "
    "VALUES (%s, %s, %s, %s, now())"
)
INSERT_BINDING: Final[str] = (
    "INSERT INTO client_binding (id, artifact_id, artifact_kind, client_id, method, "
    "confidence, valid_from) VALUES (%s, %s, %s, %s, %s, %s, now())"
)
DELETE_BINDING: Final[str] = "DELETE FROM client_binding WHERE id = %s"
# The episodic record of one attribution decision, or of one closure. The reading
# instant comes from the cluster with the write, because the derived route reads it
# and a value supplied here would be this module's claim about when rather than the
# cluster's record of it.
APPEND_LEDGER_ROW: Final[str] = (
    "INSERT INTO ledger (id, session_id, client_id, seq, category, occurred_at, "
    "recorded_at, agent_cli, machine_id, payload, content_digest, prev_chain_digest, "
    "chain_digest, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, now(), now(), %s, %s, %s::JSONB, %s, %s, %s, %s)"
)
CLUSTER_READING: Final[str] = "SELECT now()"

# The two statements the two routes run. Both are module-level literals of this
# module, exactly as the statement a production caller hands the historical read
# is a literal of theirs, and every value either one carries stays bound.
#
# The historical route counts what the erasure evidence counts: the Artifacts whose
# current attribution names the Client, which is the versions carrying no
# superseding reference.
COUNT_CURRENT_ATTRIBUTIONS: Final[str] = (
    "SELECT count(*) FROM client_binding WHERE client_id = %s AND superseded_by IS NULL"
)
# The derived route counts the same thing forward from the append-only record: the
# attribution decisions recorded at or before an instant, less the closures
# recorded by then. Nothing about it is bounded by the collection horizon.
DERIVE_ATTRIBUTION_COUNT: Final[str] = (
    "SELECT count(*) FILTER (WHERE category = %s) - count(*) FILTER (WHERE category = %s) "
    "FROM ledger WHERE client_id = %s AND recorded_at <= %s"
)

# The two categories the derived route reads. An attribution decision is recorded
# as a decision, and its closure carries the category migration 008 adds for
# exactly that event.
ATTRIBUTION_CATEGORY: Final[str] = "decision"
CLOSURE_CATEGORY: Final[str] = "attribution_superseded"

# The retention every placed row carries, and how many attributed Artifacts the
# tenant starts with. Three rather than two, so a count that moved by one is
# distinguishable from a count that emptied.
RETENTION: Final[timedelta] = timedelta(days=90)
ATTRIBUTED: Final[int] = 3

# How far past the horizon the unreachable instant sits. Any positive distance
# would do; a minute is enough to be unambiguous and short enough to stay a
# statement about the horizon rather than about the calendar.
BEYOND: Final[timedelta] = timedelta(seconds=60)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


def digest_of(label: str) -> str:
    """A hexadecimal digest of a label, for a column the schema fixes at 64 characters."""
    return hashlib.sha256(label.encode()).hexdigest()


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def scalar(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> object:
    """Read one column of one row on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    assert row is not None
    return row[0]


@dataclass(frozen=True, slots=True)
class Evidence:
    """One tenant's attribution history, the instants around it, and both routes."""

    store: MemoryStore
    connection: DriverConnection
    client_id: UUID
    empty: datetime
    before: datetime
    after: datetime

    def reading(self) -> datetime:
        """The cluster's own current reading, so no example reads a local clock."""
        moment = scalar(self.connection, CLUSTER_READING, ())
        assert isinstance(moment, datetime)
        return moment

    def historical_count(self, at: datetime) -> int:
        """The current-attribution count as the cluster held it at an instant."""
        rows = historical(
            self.store,
            COUNT_CURRENT_ATTRIBUTIONS,
            (self.client_id,),
            at=at,
            now=self.reading(),
        )
        assert len(rows) == 1
        return int(str(rows[0][0]))

    def derived_count(self, at: datetime) -> int:
        """The same count derived forward from the append-only Ledger."""
        counted = scalar(
            self.connection,
            DERIVE_ATTRIBUTION_COUNT,
            (ATTRIBUTION_CATEGORY, CLOSURE_CATEGORY, self.client_id, at),
        )
        return int(str(counted))


@pytest.fixture(scope="module")
def evidence(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Evidence]:
    """Apply every migration, probe the horizon, then write a history with two instants.

    The sequence is the one a certificate describes. Three Artifacts are attributed
    to one tenant and each attribution is recorded in the Ledger. The instant before
    that is captured, so a read at it sees an empty history. The instant after it is
    `t_before`. One attribution is then removed and its closure recorded, and the
    instant after that is `t_after`.
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

    client_id = uuid4()
    send(fresh_schema, INSERT_CLIENT, (client_id, f"tenant-{client_id.hex[:10]}", "Tenant", "eu"))
    session_id = uuid4()
    send(fresh_schema, INSERT_SESSION_ROW, (session_id, client_id, "agent", "machine"))

    empty = _reading(fresh_schema)

    bindings: list[UUID] = []
    for step in range(ATTRIBUTED):
        artifact_id = uuid4()
        send(
            fresh_schema,
            INSERT_ARTIFACT_ROW,
            (
                artifact_id,
                "summary",
                client_id,
                "a body",
                digest_of(f"artifact-{artifact_id}"),
                "distil",
                empty + RETENTION,
            ),
        )
        binding_id = uuid4()
        send(
            fresh_schema,
            INSERT_BINDING,
            (binding_id, artifact_id, "derived_artifact", client_id, "scope", 0.9),
        )
        _record(
            fresh_schema,
            session_id,
            client_id,
            step + 1,
            ATTRIBUTION_CATEGORY,
            artifact_id,
            empty,
        )
        bindings.append(binding_id)

    before = _reading(fresh_schema)

    send(fresh_schema, DELETE_BINDING, (bindings[0],))
    _record(
        fresh_schema,
        session_id,
        client_id,
        ATTRIBUTED + 1,
        CLOSURE_CATEGORY,
        bindings[0],
        empty,
    )

    after = _reading(fresh_schema)

    with MemoryStore(connect_with=connect_with) as store:
        probe_gc_horizon(store)
        yield Evidence(
            store=store,
            connection=fresh_schema,
            client_id=client_id,
            empty=empty,
            before=before,
            after=after,
        )


def _reading(connection: DriverConnection) -> datetime:
    """The cluster's own current reading, taken while the fixture builds the history."""
    moment = scalar(connection, CLUSTER_READING, ())
    assert isinstance(moment, datetime)
    return moment


def _record(
    connection: DriverConnection,
    session_id: UUID,
    client_id: UUID,
    seq: int,
    category: str,
    subject: UUID,
    expiry_base: datetime,
) -> None:
    """Append one episodic record naming what it is about, with the cluster's instant."""
    identifier = uuid4()
    send(
        connection,
        APPEND_LEDGER_ROW,
        (
            identifier,
            session_id,
            client_id,
            seq,
            category,
            "agent",
            "machine",
            json.dumps({"subject": str(subject)}),
            digest_of(f"content-{identifier}"),
            digest_of(f"previous-{seq}"),
            digest_of(f"chain-{identifier}"),
            expiry_base + RETENTION,
        ),
    )


# ---------------------------------------------------------------------------
# The pair of instants, both inside the horizon
# ---------------------------------------------------------------------------


def test_both_instants_of_the_pair_fall_inside_the_measured_horizon(evidence: Evidence) -> None:
    """The corroboration is conditional, and the condition is asked about both instants.

    The horizon is the one this cluster reports rather than a value assumed here,
    which is what makes the condition a fact about the cluster the certificate was
    assembled against.
    """
    horizon = gc_horizon(evidence.store)
    assert isinstance(horizon, GcHorizon)
    now = evidence.reading()

    assert within_gc_horizon(evidence.store, evidence.before, now=now, horizon=horizon) is True
    assert within_gc_horizon(evidence.store, evidence.after, now=now, horizon=horizon) is True


def test_the_historical_pair_agrees_with_the_counts_derived_from_the_ledger(
    evidence: Evidence,
) -> None:
    """Two routes, two instants, and the same number at each.

    The historical route counts current Attribution_Versions at the instant. The
    derived route counts the attribution records less the closure records the
    Ledger holds by that instant, with no historical clause anywhere in it. They
    agree, and they agree on a pair that straddles a change, so neither is agreeing
    with the other by reading the same state twice.
    """
    historical_before = evidence.historical_count(evidence.before)
    historical_after = evidence.historical_count(evidence.after)

    assert historical_before == ATTRIBUTED
    assert historical_after == ATTRIBUTED - 1
    assert historical_before == evidence.derived_count(evidence.before)
    assert historical_after == evidence.derived_count(evidence.after)


def test_a_read_before_the_history_began_returns_the_empty_state(evidence: Evidence) -> None:
    """The read reaches back across the whole change rather than only across the last one.

    An instant taken before the first attribution was written sees no attribution
    at all, by both routes, while the live state holds the rest. That is what makes
    the two later readings statements about their instants rather than about now.
    """
    live = scalar(evidence.connection, COUNT_CURRENT_ATTRIBUTIONS, (evidence.client_id,))

    assert evidence.historical_count(evidence.empty) == 0
    assert evidence.derived_count(evidence.empty) == 0
    assert int(str(live)) == ATTRIBUTED - 1


# ---------------------------------------------------------------------------
# Beyond the horizon, where only one route answers
# ---------------------------------------------------------------------------


def test_the_derived_route_still_answers_where_the_historical_route_may_not(
    evidence: Evidence,
) -> None:
    """This is why the derived mechanism is the primary one and this read corroborates it.

    An instant older than the measured horizon is not reachable, so the predicate
    says so and a caller does not attempt the read. The derived route answers at
    that same instant regardless, because the Ledger is append-only and holds its
    own recorded instants, and it answers the truth: nothing had been attributed
    that long ago.
    """
    horizon = gc_horizon(evidence.store)
    now = evidence.reading()
    unreachable = now - horizon.interval - BEYOND

    assert within_gc_horizon(evidence.store, unreachable, now=now, horizon=horizon) is False
    assert evidence.derived_count(unreachable) == 0
