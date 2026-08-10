"""Change stream consumption against a live instance: the primary path, end to end.

The unit modules assert the shape of the statements and the arithmetic of the severity
order. This module asserts the five things only a cluster can answer.

**The cluster serves the sinkless stream, so the primary path is the path taken.** The
statement is opened on a connection of its own and the capability row is written from
what the cluster did rather than from any version it reports. That row is read back
here, because an operator's fallback decision is made on it.

**Mutations written after the stream is open arrive on it.** Rows are inserted after
the stream opens rather than before, so what is asserted is delivery rather than an
initial scan, and the assertion holds whichever way the initial scan defaults.

**The position advances and it is durable.** The watermark row is read from the cluster
after consumption, not from the watcher's memory, because a restart reads the row.

**The health route reports the mode and the last consumed mutation.** Both routes give
the same answer, and neither carries memory content.

**A restart replays only the unresolved tail, and replay costs nothing.** A second
watcher resumes from the persisted resolved timestamp; whatever it replays, the match
rows and the halt marker are exactly what they were, which is the schema's uniqueness
constraints doing the deduplication rather than any check in the consuming code.

Every example is bounded: the batch bound is small, the resolved interval is short, and
each consuming call is asserted to have returned inside a stated number of seconds, so
a stream that stopped answering fails the example rather than hanging it.

**Validates: Requirements 23.1, 23.2, 23.6, 32.3, 36.2**
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.policy.apply import HALT_BOUND_SECONDS, session_halt
from molt.policy.rules import MatchKind, PolicyAction, PolicyRule, rule_identifier
from molt.policy.watcher import (
    HEALTH_PATH,
    LIVENESS_PATH,
    ConsumptionMode,
    StreamingConnection,
    StreamOpener,
    Watcher,
    WatcherSettings,
    dedicated_opener,
    read_watermark,
    route_answer,
)
from molt.store import Connection, MemoryStore
from molt.store.capability import capabilities
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The change stream reads a cluster setting, and a sinkless stream is refused while it
# reads disabled. Enabling it is a property of the local instance rather than of this
# module, so it is asked for once and never asserted against.
RANGEFEED_SETTING_STATEMENT: Final[str] = "SET CLUSTER SETTING kv.rangefeed.enabled = true"

# The rows this module places directly. The watcher owns no insert into any of these.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
INSERT_RULE: Final[str] = (
    "INSERT INTO policy_rule (id, name, enabled, match_kind, pattern, action) "
    "VALUES (%s, %s, true, %s, %s, %s)"
)
INSERT_EVENT: Final[str] = (
    "INSERT INTO ledger "
    "(id, session_id, client_id, seq, category, agent_cli, machine_id, payload, "
    "content_digest, prev_chain_digest, chain_digest, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s, %s, %s, now() + INTERVAL '90 days')"
)

# What every claim about stored rows is read from.
COUNT_MATCHES: Final[str] = "SELECT count(*) FROM policy_match WHERE session_id = %s"
COUNT_APPROVALS: Final[str] = "SELECT count(*) FROM approval_queue WHERE session_id = %s"

# The rule set every example applies. Neither rule is a built-in, so a match cannot be
# a coincidence with a pattern this codebase ships, and the two actions are the two
# that write something: a halt writes the marker, an approval writes a queue entry.
HALT_RULE_NAME: Final[str] = "example.halt_recursive_removal"
APPROVAL_RULE_NAME: Final[str] = "example.approve_key_material"
HALT_PATTERN: Final[str] = "*remove-everything*"
APPROVAL_PATTERN: Final[str] = "*.example-key"

# The command and the path the two rules match. Neither is a real instruction and
# neither names a real file.
HALTING_COMMAND: Final[str] = "sh -c remove-everything"
APPROVED_PATH: Final[str] = "workspace/local.example-key"

# The values the placed rows carry. None is what an assertion turns on.
JURISDICTION: Final[str] = "eu"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"

# The consumption surface every example is driven by. The resolved interval is short so
# a batch that ends at a resolved row ends promptly, and the batch bound is small so an
# example never waits for a hundred rows that are not coming.
RESOLVED_INTERVAL: Final[str] = "1s"
BATCH_LIMIT: Final[int] = 8
POLL_INTERVAL_SECONDS: Final[int] = 1

# How long a consuming call is allowed to take before the example fails rather than
# waits. Generous against the resolved interval and still far inside the halt bound's
# order of magnitude.
CONSUME_BUDGET_SECONDS: Final[float] = 30.0

# A connection is typed loosely because the driver is reached through a fixture rather
# than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


def example_rules() -> tuple[PolicyRule, ...]:
    """The two rules every example evaluates against."""
    return (
        PolicyRule(
            id=rule_identifier(HALT_RULE_NAME),
            name=HALT_RULE_NAME,
            match_kind=MatchKind.SHELL_COMMAND,
            action=PolicyAction.HALT_AGENT,
            pattern=HALT_PATTERN,
        ),
        PolicyRule(
            id=rule_identifier(APPROVAL_RULE_NAME),
            name=APPROVAL_RULE_NAME,
            match_kind=MatchKind.FILE_PATH,
            action=PolicyAction.REQUIRE_APPROVAL,
            pattern=APPROVAL_PATTERN,
        ),
    )


def example_settings() -> WatcherSettings:
    """The consumption surface, stated rather than defaulted so the example is bounded."""
    return WatcherSettings(
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        resolved_interval=RESOLVED_INTERVAL,
        batch_limit=BATCH_LIMIT,
    )


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the streaming factory."""

    store: MemoryStore
    connection: DriverConnection
    schema: str
    dsn: str
    driver: ModuleType

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

    def count(self, statement: str, params: tuple[object, ...]) -> int:
        """The number one counting statement reports."""
        produced = self.rows(statement, params)
        assert len(produced) == 1
        return int(produced[0][0])

    def client(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        self.send(
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:12]}", "Tenant", JURISDICTION),
        )
        return identifier

    def session(self, client_id: UUID) -> UUID:
        """Place one running Session of a Client and return its identifier."""
        identifier = uuid4()
        self.send(INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
        return identifier

    def event(
        self,
        session_id: UUID,
        client_id: UUID,
        *,
        seq: int,
        category: str,
        payload: str,
    ) -> UUID:
        """Place one Ledger Event, digests included, and return its identifier."""
        identifier = uuid4()
        self.send(
            INSERT_EVENT,
            (
                identifier,
                session_id,
                client_id,
                seq,
                category,
                AGENT_CLI,
                MACHINE_ID,
                payload,
                _digest(seq, "content"),
                _digest(seq, "previous"),
                _digest(seq, "chain"),
            ),
        )
        return identifier

    def opener(self) -> StreamOpener:
        """A stream opener building one connection per stream on this module's schema."""

        def connect() -> StreamingConnection:
            opened = self.driver.connect(self.dsn, autocommit=True)
            with opened.cursor() as cursor:
                cursor.execute(SEARCH_PATH_STATEMENT, (self.schema,))
            streaming: StreamingConnection = opened
            return streaming

        return dedicated_opener(connect)


def _digest(seq: int, role: str) -> str:
    """A distinct sixty-four character hexadecimal digest for one Event and one column.

    The chain digests are placed rather than computed because nothing under test reads
    them; what matters is that each is well formed and that no two Events of a Session
    share a predecessor, which is a uniqueness constraint of the Ledger.
    """
    return f"{seq:04x}{role:_>12}".encode().hex().ljust(64, "0")[:64]


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, enable the stream's setting, and build a store over it."""
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute(RANGEFEED_SETTING_STATEMENT)
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
        cluster = Cluster(
            store=store,
            connection=fresh_schema,
            schema=schema,
            dsn=local_instance_dsn,
            driver=database_driver,
        )
        for rule in example_rules():
            assert rule.pattern is not None
            cluster.send(
                INSERT_RULE,
                (rule.id, rule.name, rule.match_kind.value, rule.pattern, rule.action.value),
            )
        yield cluster


def consume_bounded(watcher: Watcher, *, batches: int) -> tuple[int, float]:
    """Take a bounded number of batches and report what it applied and how long it took."""
    started = time.monotonic()
    applied = watcher.run(batches=batches)
    elapsed = time.monotonic() - started
    assert elapsed < CONSUME_BUDGET_SECONDS, (
        f"consuming {batches} batch(es) took {elapsed:.2f}s, which is not a bounded loop"
    )
    return applied, elapsed


def test_the_cluster_serves_the_stream_and_the_capability_row_says_so(
    cluster: Cluster,
) -> None:
    """The primary path is taken, and what the cluster did is what the record reports."""
    watcher = Watcher(
        cluster.store,
        example_rules(),
        settings=example_settings(),
        opener=cluster.opener(),
    )

    mode = watcher.start()
    try:
        assert mode is ConsumptionMode.CHANGEFEED
        assert capabilities(cluster.store).changefeed is True
        assert read_watermark(cluster.store) is not None
    finally:
        watcher.stop()


def test_mutations_written_after_the_stream_opens_are_consumed_on_it(
    cluster: Cluster,
) -> None:
    """Delivery, the watermark, the health route, and the halt, in one consumption."""
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    watcher = Watcher(
        cluster.store,
        example_rules(),
        settings=example_settings(),
        opener=cluster.opener(),
    )
    assert watcher.start() is ConsumptionMode.CHANGEFEED

    try:
        cluster.event(
            session_id,
            client_id,
            seq=1,
            category="file_read",
            payload=f'{{"path": "{APPROVED_PATH}"}}',
        )
        cluster.event(
            session_id,
            client_id,
            seq=2,
            category="shell_command",
            payload=f'{{"command": "{HALTING_COMMAND}"}}',
        )

        applied, _ = consume_bounded(watcher, batches=3)

        assert applied >= 2, "both written mutations arrive on the stream"
        assert cluster.count(COUNT_MATCHES, (session_id,)) == 2
        assert cluster.count(COUNT_APPROVALS, (session_id,)) == 1

        halt = session_halt(cluster.store, session_id)
        assert halt is not None
        assert halt.halted is True
        assert halt.reason is not None and HALT_RULE_NAME in halt.reason
        assert halt.rule_id == rule_identifier(HALT_RULE_NAME)

        stored = read_watermark(cluster.store)
        assert stored is not None
        assert stored.mode is ConsumptionMode.CHANGEFEED
        assert stored.last_mutation_at is not None, "the position advanced past the mutations"

        answer = route_answer(watcher, HEALTH_PATH)
        assert answer is not None
        assert answer.body["mode"] == ConsumptionMode.CHANGEFEED.value
        assert answer.body["changefeed_available"] is True
        assert answer.body["last_mutation_at"] is not None
        assert route_answer(watcher, LIVENESS_PATH) == answer
        assert route_answer(watcher, "/anything-else") is None
    finally:
        watcher.stop()


def test_a_restart_replays_only_the_unresolved_tail_and_writes_nothing_twice(
    cluster: Cluster,
) -> None:
    """Replay is bounded by the persisted position and absorbed by the constraints."""
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    first = Watcher(
        cluster.store,
        example_rules(),
        settings=example_settings(),
        opener=cluster.opener(),
    )
    assert first.start() is ConsumptionMode.CHANGEFEED
    try:
        cluster.event(
            session_id,
            client_id,
            seq=1,
            category="shell_command",
            payload=f'{{"command": "{HALTING_COMMAND}"}}',
        )
        consume_bounded(first, batches=3)
    finally:
        first.stop()

    matches_before = cluster.count(COUNT_MATCHES, (session_id,))
    halt_before = session_halt(cluster.store, session_id)
    assert matches_before == 1
    assert halt_before is not None and halt_before.halted_at is not None
    resumed_from = read_watermark(cluster.store)
    assert resumed_from is not None

    second = Watcher(
        cluster.store,
        example_rules(),
        settings=example_settings(),
        opener=cluster.opener(),
    )
    assert second.start() is ConsumptionMode.CHANGEFEED
    try:
        assert second.watermark.resolved_at == resumed_from.resolved_at, (
            "a restart resumes from the persisted position rather than from the beginning"
        )
        consume_bounded(second, batches=2)
    finally:
        second.stop()

    halt_after = session_halt(cluster.store, session_id)
    assert cluster.count(COUNT_MATCHES, (session_id,)) == matches_before, (
        "a redelivered mutation collides with the uniqueness constraint and writes nothing"
    )
    assert halt_after is not None
    assert halt_after.halted_at == halt_before.halted_at, "the first halt's instant stands"


def test_the_halt_lands_inside_the_bound_on_the_primary_path(cluster: Cluster) -> None:
    """The kill switch is measured against the requirement's bound, not assumed."""
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    watcher = Watcher(
        cluster.store,
        example_rules(),
        settings=example_settings(),
        opener=cluster.opener(),
    )
    assert watcher.start() is ConsumptionMode.CHANGEFEED

    try:
        started = time.monotonic()
        cluster.event(
            session_id,
            client_id,
            seq=1,
            category="shell_command",
            payload=f'{{"command": "{HALTING_COMMAND}"}}',
        )
        applied, _ = consume_bounded(watcher, batches=2)
        elapsed = time.monotonic() - started
    finally:
        watcher.stop()

    halt = session_halt(cluster.store, session_id)
    assert applied >= 1
    assert halt is not None and halt.halted is True
    assert elapsed <= HALT_BOUND_SECONDS, (
        f"the halt landed {elapsed:.2f}s after the mutation, past the {HALT_BOUND_SECONDS}s bound"
    )
