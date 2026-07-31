"""Procedural standing against a live instance: the clamp, the record, and the refusals.

The unit modules assert the shape of the statements and the arithmetic behind the
deltas. This module asserts the four things only a cluster can answer.

**The clamp the statement performs agrees with the arithmetic stated in the
tracker.** Both exist on purpose -- the cluster's is authoritative because it holds
the interval even for a delta a caller made up, and the tracker's is what a test and
a caller can reason with -- and two arithmetics that disagreed would be worse than
one. So every example compares the value the cluster committed against the value the
pure function predicted, over ordinary movements and at both bounds.

**A movement and its change record arrive together, and an absorbed movement writes
no record.** The counts are read back from the tables rather than from what the call
returned, because the claim is about what is stored. An outcome standing on the
record with no change record beside it is what an adjustment absorbed by a bound
looks like, and that is asserted as a pair of counts rather than inferred.

**The cluster refuses what the predicate says it refuses.** An outcome asserted for
a procedure the Session never retrieved, and a classification disagreeing with the
Session's own recorded outcome, each write nothing, which is read back as an absent
row rather than as a raised error alone. A repeated report leaves one outcome row and
one change record, so the adjustment is idempotent per Session without the recording
path holding any state.

**A procedure below the recall floor is still stored.** Exclusion from recall is not
a soft delete, so the row is counted after the value has been driven under the floor.

**Validates: Requirements 49.1, 49.2, 49.4, 49.5, 49.6, 49.7, 49.10, 49.12, 49.13, 49.15**
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.confidence import (
    FAILURE_DELTA_KEY,
    INITIAL_KEY,
    RECALL_FLOOR_KEY,
    SUCCESS_DELTA_KEY,
    ConfidencePolicy,
    adjusted,
    history,
    initial_standing,
    record_outcome,
    record_retrieval,
    summary,
)
from molt.config.resolve import Configuration
from molt.errors import StoreError
from molt.models.artifact import CONFIDENCE_CEILING, CONFIDENCE_FLOOR, DerivedArtifactKind
from molt.models.session import SessionOutcome
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The rows this module places directly. The tracker owns no Client insert, no Session
# insert, and no Artifact insert, so the rows a standing hangs off are placed here.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction) VALUES (%s, %s, %s, %s)"
)
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id, outcome, ended_at) "
    "VALUES (%s, %s, %s, %s, %s, now())"
)
INSERT_PROCEDURE: Final[str] = (
    "INSERT INTO derived_artifact "
    "(id, kind, owner_client_id, body, content_digest, derivation_method, expires_at, "
    "procedure_confidence) "
    "VALUES (%s, %s, %s, %s, %s, %s, now() + INTERVAL '90 days', %s)"
)

# The counts and values every claim about stored rows is read from.
COUNT_OUTCOMES: Final[str] = (
    "SELECT count(*) FROM procedure_outcome WHERE procedure_id = %s AND session_id = %s"
)
COUNT_CHANGES: Final[str] = (
    "SELECT count(*) FROM procedure_confidence_change WHERE procedure_id = %s"
)
COUNT_PROCEDURE_ROWS: Final[str] = "SELECT count(*) FROM derived_artifact WHERE id = %s"
READ_STANDING: Final[str] = "SELECT procedure_confidence FROM derived_artifact WHERE id = %s"
READ_STORED_OUTCOME: Final[str] = (
    "SELECT outcome FROM procedure_outcome WHERE procedure_id = %s AND session_id = %s"
)

# The policy every example applies. None of the four is a surface default, so a value
# the cluster committed cannot agree with a prediction by coincidence with a number
# this codebase holds. The failure delta stays larger than the success delta, which
# is the asymmetry the design argues for.
EXAMPLE_INITIAL: Final[float] = 0.4
EXAMPLE_SUCCESS_DELTA: Final[float] = 0.06
EXAMPLE_FAILURE_DELTA: Final[float] = 0.11
EXAMPLE_FLOOR: Final[float] = 0.2

# How many successes drive a standing to the ceiling from the example initial value,
# and how many failures drive it to the floor. Both are comfortably more than enough,
# because the point is to reach the bound and then attempt to pass it.
RUNS_TO_A_BOUND: Final[int] = 12

# The values the placed rows carry. None is what an assertion turns on.
JURISDICTION: Final[str] = "eu"
AGENT_CLI: Final[str] = "stub"
MACHINE_ID: Final[str] = "stub-machine"
PROCEDURE_BODY: Final[str] = "check the migration ran before asserting the schema shape"
DERIVATION_METHOD: Final[str] = "distilled"

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

    def count(self, statement: str, params: tuple[object, ...]) -> int:
        """The number one counting statement reports."""
        return int(self.one(statement, params)[0])

    def standing(self, procedure_id: UUID) -> float:
        """The standing the cluster holds for one procedure."""
        return float(self.one(READ_STANDING, (procedure_id,))[0])

    def client(self) -> UUID:
        """Place one Client directly and return its identifier."""
        identifier = uuid4()
        self.send(
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:12]}", "Tenant", JURISDICTION),
        )
        return identifier

    def session(self, client_id: UUID, outcome: SessionOutcome) -> UUID:
        """Place one terminal Session of a Client and return its identifier."""
        identifier = uuid4()
        self.send(
            INSERT_SESSION,
            (identifier, client_id, AGENT_CLI, MACHINE_ID, outcome.value),
        )
        return identifier

    def procedure(self, client_id: UUID, *, standing: float | None = None) -> UUID:
        """Place one learned procedure at the configured initial standing."""
        identifier = uuid4()
        opening = initial_standing(
            DerivedArtifactKind.LEARNED_PROCEDURE,
            policy=example_policy(),
        )
        self.send(
            INSERT_PROCEDURE,
            (
                identifier,
                DerivedArtifactKind.LEARNED_PROCEDURE.value,
                client_id,
                PROCEDURE_BODY,
                hashlib.sha256(identifier.bytes).hexdigest(),
                DERIVATION_METHOD,
                opening if standing is None else standing,
            ),
        )
        return identifier

    def summary_artifact(self, client_id: UUID) -> UUID:
        """Place one summary, which carries no standing at all."""
        identifier = uuid4()
        self.send(
            INSERT_PROCEDURE,
            (
                identifier,
                DerivedArtifactKind.SUMMARY.value,
                client_id,
                PROCEDURE_BODY,
                hashlib.sha256(identifier.bytes).hexdigest(),
                DERIVATION_METHOD,
                None,
            ),
        )
        return identifier


def example_policy() -> ConfidencePolicy:
    """The policy every example applies, read from a configuration naming four values."""
    return ConfidencePolicy.from_configuration(
        Configuration(
            environ={
                INITIAL_KEY: str(EXAMPLE_INITIAL),
                SUCCESS_DELTA_KEY: str(EXAMPLE_SUCCESS_DELTA),
                FAILURE_DELTA_KEY: str(EXAMPLE_FAILURE_DELTA),
                RECALL_FLOOR_KEY: str(EXAMPLE_FLOOR),
            },
            file_values={},
        )
    )


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store over this module's own schema.

    Every migration is applied because the standing column and its three tables
    arrive in a later generation than the tables they hang off.
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

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


def consume(cluster: Cluster, procedure_id: UUID, outcome: SessionOutcome) -> UUID:
    """One Session that retrieved a procedure and then reached a terminal outcome."""
    session_id = cluster.session(
        cluster.one(
            "SELECT owner_client_id FROM derived_artifact WHERE id = %s",
            (procedure_id,),
        )[0],
        outcome,
    )
    record_retrieval(cluster.store, procedure_id, session_id)
    return session_id


# ---------------------------------------------------------------------------
# The initial standing
# ---------------------------------------------------------------------------


def test_a_procedure_is_stored_with_the_configured_initial_standing(cluster: Cluster) -> None:
    """A procedure never exists without a standing, and the standing is configured."""
    procedure_id = cluster.procedure(cluster.client())

    assert cluster.standing(procedure_id) == EXAMPLE_INITIAL


def test_a_summary_can_hold_no_standing_at_all(cluster: Cluster) -> None:
    """The kind equivalence is the cluster's, so the other direction is unwritable too."""
    client_id = cluster.client()
    summary_id = cluster.summary_artifact(client_id)

    assert cluster.one(READ_STANDING, (summary_id,))[0] is None
    with pytest.raises(Exception, match="procedure_confidence IS NOT NULL"):
        cluster.send(
            "UPDATE derived_artifact SET procedure_confidence = %s WHERE id = %s",
            (EXAMPLE_INITIAL, summary_id),
        )


# ---------------------------------------------------------------------------
# The movement, and the record that accompanies it
# ---------------------------------------------------------------------------


def test_a_succeeded_outcome_raises_the_standing_and_records_the_movement(
    cluster: Cluster,
) -> None:
    """The cluster's clamp and the tracker's arithmetic agree on the committed value."""
    procedure_id = cluster.procedure(cluster.client())
    session_id = consume(cluster, procedure_id, SessionOutcome.SUCCEEDED)

    change = record_outcome(
        cluster.store,
        procedure_id,
        session_id,
        SessionOutcome.SUCCEEDED,
        policy=example_policy(),
    )

    predicted = adjusted(EXAMPLE_INITIAL, EXAMPLE_SUCCESS_DELTA)
    assert change is not None
    assert cluster.standing(procedure_id) == pytest.approx(predicted)
    assert change.new_value == pytest.approx(predicted)
    assert change.prior_value == EXAMPLE_INITIAL
    assert change.outcome_id is not None
    assert cluster.count(COUNT_OUTCOMES, (procedure_id, session_id)) == 1
    assert cluster.count(COUNT_CHANGES, (procedure_id,)) == 1
    assert cluster.one(READ_STORED_OUTCOME, (procedure_id, session_id))[0] == (
        SessionOutcome.SUCCEEDED.value
    )


def test_a_failed_outcome_lowers_the_standing_by_the_configured_decrement(
    cluster: Cluster,
) -> None:
    """Standing is lost faster than it is earned, by the two configured magnitudes."""
    procedure_id = cluster.procedure(cluster.client())
    session_id = consume(cluster, procedure_id, SessionOutcome.FAILED)

    change = record_outcome(
        cluster.store,
        procedure_id,
        session_id,
        SessionOutcome.FAILED,
        policy=example_policy(),
    )

    predicted = adjusted(EXAMPLE_INITIAL, -EXAMPLE_FAILURE_DELTA)
    assert change is not None
    assert cluster.standing(procedure_id) == pytest.approx(predicted)
    assert predicted < EXAMPLE_INITIAL - EXAMPLE_SUCCESS_DELTA


def test_an_abandoned_outcome_is_recorded_and_moves_nothing(cluster: Cluster) -> None:
    """An interruption says nothing about the procedure, so nothing moves and none is recorded."""
    procedure_id = cluster.procedure(cluster.client())
    session_id = consume(cluster, procedure_id, SessionOutcome.ABANDONED)

    change = record_outcome(
        cluster.store,
        procedure_id,
        session_id,
        SessionOutcome.ABANDONED,
        policy=example_policy(),
    )

    assert change is None
    assert cluster.standing(procedure_id) == EXAMPLE_INITIAL
    assert cluster.count(COUNT_OUTCOMES, (procedure_id, session_id)) == 1
    assert cluster.count(COUNT_CHANGES, (procedure_id,)) == 0


# ---------------------------------------------------------------------------
# The bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "bound"),
    [
        (SessionOutcome.SUCCEEDED, CONFIDENCE_CEILING),
        (SessionOutcome.FAILED, CONFIDENCE_FLOOR),
    ],
    ids=["ceiling", "floor"],
)
def test_an_adjustment_at_a_bound_is_absorbed_and_records_nothing(
    cluster: Cluster,
    outcome: SessionOutcome,
    bound: float,
) -> None:
    """The interval holds, and the absorbed attempt is an outcome with no record beside it."""
    policy = example_policy()
    procedure_id = cluster.procedure(cluster.client())

    for _ in range(RUNS_TO_A_BOUND):
        record_outcome(
            cluster.store,
            procedure_id,
            consume(cluster, procedure_id, outcome),
            outcome,
            policy=policy,
        )

    assert cluster.standing(procedure_id) == bound
    changes_before = cluster.count(COUNT_CHANGES, (procedure_id,))

    session_id = consume(cluster, procedure_id, outcome)
    absorbed = record_outcome(cluster.store, procedure_id, session_id, outcome, policy=policy)

    assert absorbed is None, "a value that did not move has no change record to report"
    assert cluster.standing(procedure_id) == bound
    assert cluster.count(COUNT_OUTCOMES, (procedure_id, session_id)) == 1, (
        "the attempt is on the record as an outcome"
    )
    assert cluster.count(COUNT_CHANGES, (procedure_id,)) == changes_before, (
        "and it claimed no movement"
    )
    assert len(history(cluster.store, procedure_id)) == changes_before


def test_a_procedure_driven_below_the_floor_is_still_stored(cluster: Cluster) -> None:
    """Exclusion from recall is not a soft delete."""
    policy = example_policy()
    procedure_id = cluster.procedure(cluster.client())

    record_outcome(
        cluster.store,
        procedure_id,
        consume(cluster, procedure_id, SessionOutcome.FAILED),
        SessionOutcome.FAILED,
        policy=policy,
    )
    record_outcome(
        cluster.store,
        procedure_id,
        consume(cluster, procedure_id, SessionOutcome.FAILED),
        SessionOutcome.FAILED,
        policy=policy,
    )

    standing = cluster.standing(procedure_id)
    assert policy.below_floor(standing)
    assert cluster.count(COUNT_PROCEDURE_ROWS, (procedure_id,)) == 1


# ---------------------------------------------------------------------------
# What the cluster refuses
# ---------------------------------------------------------------------------


def test_an_outcome_for_a_procedure_a_session_never_retrieved_writes_nothing(
    cluster: Cluster,
) -> None:
    """The retrieval requirement is the cluster's, so no row appears and none is claimed."""
    client_id = cluster.client()
    procedure_id = cluster.procedure(client_id)
    session_id = cluster.session(client_id, SessionOutcome.SUCCEEDED)

    with pytest.raises(StoreError, match="never retrieved"):
        record_outcome(
            cluster.store,
            procedure_id,
            session_id,
            SessionOutcome.SUCCEEDED,
            policy=example_policy(),
        )

    assert cluster.count(COUNT_OUTCOMES, (procedure_id, session_id)) == 0
    assert cluster.standing(procedure_id) == EXAMPLE_INITIAL


def test_a_classification_disagreeing_with_the_session_is_refused(cluster: Cluster) -> None:
    """The stored classification is the Session's own, so an assertion about it is checked."""
    procedure_id = cluster.procedure(cluster.client())
    session_id = consume(cluster, procedure_id, SessionOutcome.FAILED)

    with pytest.raises(StoreError, match="own recorded outcome"):
        record_outcome(
            cluster.store,
            procedure_id,
            session_id,
            SessionOutcome.SUCCEEDED,
            policy=example_policy(),
        )

    assert cluster.count(COUNT_OUTCOMES, (procedure_id, session_id)) == 0
    assert cluster.standing(procedure_id) == EXAMPLE_INITIAL


def test_a_repeated_report_leaves_one_outcome_and_one_change(cluster: Cluster) -> None:
    """The per-Session arbiter makes the adjustment idempotent without any held state."""
    policy = example_policy()
    procedure_id = cluster.procedure(cluster.client())
    session_id = consume(cluster, procedure_id, SessionOutcome.SUCCEEDED)

    first = record_outcome(
        cluster.store, procedure_id, session_id, SessionOutcome.SUCCEEDED, policy=policy
    )
    second = record_outcome(
        cluster.store, procedure_id, session_id, SessionOutcome.SUCCEEDED, policy=policy
    )

    assert first is not None
    assert second is None, "the second report moved nothing, so it reports no change"
    assert cluster.count(COUNT_OUTCOMES, (procedure_id, session_id)) == 1
    assert cluster.count(COUNT_CHANGES, (procedure_id,)) == 1
    assert cluster.standing(procedure_id) == pytest.approx(
        adjusted(EXAMPLE_INITIAL, EXAMPLE_SUCCESS_DELTA)
    )


# ---------------------------------------------------------------------------
# The audited history and the standing summary
# ---------------------------------------------------------------------------


def test_the_history_reads_back_in_the_order_the_movements_happened(cluster: Cluster) -> None:
    """Each record's prior value is the previous record's new value, in change order."""
    policy = example_policy()
    procedure_id = cluster.procedure(cluster.client())
    applied = [
        SessionOutcome.SUCCEEDED,
        SessionOutcome.FAILED,
        SessionOutcome.ABANDONED,
        SessionOutcome.SUCCEEDED,
    ]

    for outcome in applied:
        record_outcome(
            cluster.store,
            procedure_id,
            consume(cluster, procedure_id, outcome),
            outcome,
            policy=policy,
        )

    records = history(cluster.store, procedure_id)

    moving = [outcome for outcome in applied if policy.delta_for(outcome) is not None]
    assert len(records) == len(moving), "one record per movement, and none for the abandoned one"
    assert records[0].prior_value == EXAMPLE_INITIAL
    for earlier, later in pairwise(records):
        assert later.prior_value == pytest.approx(earlier.new_value)
        assert earlier.changed_at <= later.changed_at
    assert records[-1].new_value == pytest.approx(cluster.standing(procedure_id))


def test_the_summary_reports_the_standing_the_retrievals_and_the_outcomes(
    cluster: Cluster,
) -> None:
    """The three numbers the console shows, read from the cluster in one call."""
    policy = example_policy()
    procedure_id = cluster.procedure(cluster.client())
    for outcome in (SessionOutcome.SUCCEEDED, SessionOutcome.SUCCEEDED, SessionOutcome.FAILED):
        record_outcome(
            cluster.store,
            procedure_id,
            consume(cluster, procedure_id, outcome),
            outcome,
            policy=policy,
        )

    standing = summary(cluster.store, procedure_id)

    assert standing.confidence == pytest.approx(cluster.standing(procedure_id))
    assert standing.retrievals == 3
    assert standing.count_of(SessionOutcome.SUCCEEDED) == 2
    assert standing.count_of(SessionOutcome.FAILED) == 1
    assert standing.count_of(SessionOutcome.ABANDONED) == 0
