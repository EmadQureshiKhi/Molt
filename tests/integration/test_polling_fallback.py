"""The retained poll against a live instance: entering it, recording it, and its bound.

The delivered cluster serves the change stream, so a refusal cannot be produced by
asking this instance nicely. It is injected instead: the stream opener raises the
refusal the statement would raise on a tier that rejects it, and everything downstream
of the refusal is the real path against real rows.

Four claims, one per thing an operator or a later process relies on.

**The refusal enters the poll rather than ending consumption.** The mode the start
reports is the poll, and it is reached through the refusal rather than through
configuration, which is what the injection buys.

**The degradation is counted.** `watcher.degraded_to_polling` is asserted on a telemetry
instance this module owns, so the assertion is about the counter rather than about a
line of output.

**The mode is persisted, and so is the capability.** A later process reads the
watermark row and the capability row, so both are read back from the cluster here
rather than from the watcher's memory.

**The halt bound still holds in the degraded mode.** The poll reads the Ledger on
`(recorded_at, id)` from the watermark, and the Session is marked inside the bound the
kill switch is required to meet. The sleeper is injected, so what is measured is the
work rather than the interval a deployment waits between batches.

**Validates: Requirements 23.3, 23.6, 23.12, 32.3, 36.2**
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.policy.apply import HALT_BOUND_SECONDS, pending_approvals, session_halt
from molt.policy.rules import MatchKind, PolicyAction, PolicyRule, rule_identifier
from molt.policy.watcher import (
    DEGRADED_METRIC,
    HEALTH_PATH,
    ChangefeedRejectedError,
    ConsumptionMode,
    MutationStream,
    Watcher,
    WatcherSettings,
    read_watermark,
    route_answer,
)
from molt.store import Connection, MemoryStore
from molt.store.capability import CHANGEFEED, capabilities
from molt.store.migrate import apply_migrations
from molt.telemetry import Telemetry, configure, reset

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places directly.
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

COUNT_MATCHES: Final[str] = "SELECT count(*) FROM policy_match WHERE session_id = %s"

# The rule set every example applies, neither rule being a built-in.
HALT_RULE_NAME: Final[str] = "example.halt_recursive_removal"
APPROVAL_RULE_NAME: Final[str] = "example.approve_key_material"
HALT_PATTERN: Final[str] = "*remove-everything*"
APPROVAL_PATTERN: Final[str] = "*.example-key"
HALTING_COMMAND: Final[str] = "sh -c remove-everything"
APPROVED_PATH: Final[str] = "workspace/local.example-key"

JURISDICTION: Final[str] = "eu"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"

# The consumption surface. The poll interval is stated because the fallback's own
# interval is what the degraded mode's bound turns on, and the sleeper is injected so
# no example waits for it.
POLL_INTERVAL_SECONDS: Final[int] = 2
RESOLVED_INTERVAL: Final[str] = "1s"
BATCH_LIMIT: Final[int] = 8

# How long a bounded poll is allowed to take before the example fails rather than waits.
CONSUME_BUDGET_SECONDS: Final[float] = 30.0

DriverConnection = Any


def refusing_opener(_statement: str, _params: tuple[object, ...]) -> MutationStream:
    """The opener a tier that rejects the statement behaves like.

    It raises the refusal the start path catches, which is the one thing that separates
    a tier without the stream from the delivered one.
    """
    raise ChangefeedRejectedError("the cluster refused the sinkless change stream")


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
    """The consumption surface, stated rather than defaulted."""
    return WatcherSettings(
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        resolved_interval=RESOLVED_INTERVAL,
        batch_limit=BATCH_LIMIT,
    )


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration and a store over it."""

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


def _digest(seq: int, role: str) -> str:
    """A distinct sixty-four character hexadecimal digest for one Event and one column."""
    return f"{seq:04x}{role:_>12}".encode().hex().ljust(64, "0")[:64]


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
        cluster = Cluster(store=store, connection=fresh_schema)
        for rule in example_rules():
            assert rule.pattern is not None
            cluster.send(
                INSERT_RULE,
                (rule.id, rule.name, rule.match_kind.value, rule.pattern, rule.action.value),
            )
        yield cluster


@pytest.fixture
def emitter() -> Iterator[Telemetry]:
    """A process-wide telemetry instance this module owns, writing nowhere visible."""
    instance = configure(Configuration(environ={}, file_values={}), stream=io.StringIO())
    try:
        yield instance
    finally:
        reset()


def degraded_count(emitter: Telemetry) -> float:
    """How many degradations the counter holds, the counter being undimensioned."""
    return emitter.counters().get((DEGRADED_METRIC, ()), 0.0)


def polling_watcher(cluster: Cluster) -> Watcher:
    """A watcher whose stream opener refuses, with a sleeper that waits for nothing."""
    return Watcher(
        cluster.store,
        example_rules(),
        settings=example_settings(),
        opener=refusing_opener,
        sleep=lambda _: None,
    )


def test_a_refused_stream_enters_the_poll_and_records_it_three_ways(
    cluster: Cluster,
    emitter: Telemetry,
) -> None:
    """The mode, the counter, the persisted row, and the capability row all agree."""
    watcher = polling_watcher(cluster)

    mode = watcher.start()

    assert mode is ConsumptionMode.POLLING
    assert degraded_count(emitter) == 1.0, "entering the poll is counted once"

    stored = read_watermark(cluster.store)
    assert stored is not None
    assert stored.mode is ConsumptionMode.POLLING, "the next process starts in the poll"
    assert capabilities(cluster.store).unavailable(CHANGEFEED) is True, (
        "a cluster that was asked and said no is recorded as such, not left unprobed"
    )

    answer = route_answer(watcher, HEALTH_PATH)
    assert answer is not None
    assert answer.body["mode"] == ConsumptionMode.POLLING.value
    assert answer.body["changefeed_available"] is False


def test_the_poll_consumes_from_the_watermark_and_halts_inside_the_bound(
    cluster: Cluster,
    emitter: Telemetry,
) -> None:
    """The degraded mode enforces policy, and the bound is measured rather than assumed."""
    client_id = cluster.client()
    session_id = cluster.session(client_id)
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
    watcher = polling_watcher(cluster)
    assert watcher.start() is ConsumptionMode.POLLING

    started = time.monotonic()
    applied = watcher.run(batches=2)
    elapsed = time.monotonic() - started

    assert elapsed < CONSUME_BUDGET_SECONDS, "the poll loop is bounded"
    assert degraded_count(emitter) == 1.0, "the degradation is counted on entry, once"
    assert applied >= 2
    assert cluster.count(COUNT_MATCHES, (session_id,)) == 2

    halt = session_halt(cluster.store, session_id)
    assert halt is not None
    assert halt.halted is True
    assert halt.rule_id == rule_identifier(HALT_RULE_NAME)
    assert elapsed <= HALT_BOUND_SECONDS, (
        f"the degraded mode halted {elapsed:.2f}s after consuming, "
        f"past the {HALT_BOUND_SECONDS}s bound"
    )

    queued = pending_approvals(cluster.store, session_id)
    assert len(queued) == 1
    assert queued[0].rule_id == rule_identifier(APPROVAL_RULE_NAME)

    answer = route_answer(watcher, HEALTH_PATH)
    assert answer is not None
    assert answer.body["last_mutation_at"] is not None, (
        "the liveness answer reports the last consumed mutation in the degraded mode too"
    )


def test_a_second_poll_from_the_persisted_position_consumes_nothing_twice(
    cluster: Cluster,
    emitter: Telemetry,
) -> None:
    """The position is where the poll resumes, so a restart re-reads no consumed row."""
    client_id = cluster.client()
    session_id = cluster.session(client_id)
    cluster.event(
        session_id,
        client_id,
        seq=1,
        category="shell_command",
        payload=f'{{"command": "{HALTING_COMMAND}"}}',
    )
    first = polling_watcher(cluster)
    first.start()
    assert first.run(batches=1) >= 1
    matches_after_first = cluster.count(COUNT_MATCHES, (session_id,))

    second = polling_watcher(cluster)
    second.start()
    consumed = second.run(batches=1)

    assert consumed == 0, "the persisted position is past every row already consumed"
    assert cluster.count(COUNT_MATCHES, (session_id,)) == matches_after_first
    assert degraded_count(emitter) == 2.0, "each start that degrades is counted"
