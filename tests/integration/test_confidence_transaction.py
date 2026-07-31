"""Confidence movements, retries, and history against a live cluster.

These tests cover the transaction boundary that pure arithmetic cannot: rollback
leaves neither a moved value nor an audit row, contending writers retry from the
value that actually committed, below-floor procedures remain stored, and history
is returned in transition order.

**Validates: Requirements 49.10, 49.13, 49.15, 36.2**
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from itertools import pairwise
from typing import Final
from uuid import UUID

import pytest
from tests.integration.test_procedure_standing_writes import (
    COUNT_CHANGES,
    COUNT_PROCEDURE_ROWS,
    EXAMPLE_INITIAL,
    Cluster,
    consume,
    example_policy,
)
from tests.integration.test_procedure_standing_writes import cluster as _cluster_fixture

import molt.store.confidence as confidence_store
from molt.confidence import history, record_outcome
from molt.models.session import SessionOutcome
from molt.store import Cursor
from molt.store.confidence import adjust_standing, select_standing

pytestmark = pytest.mark.integration

THREADS: Final[int] = 2
GATE_TIMEOUT: Final[float] = 10.0
JOIN_TIMEOUT: Final[float] = 15.0


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """One concurrent outcome writer's result or failure."""

    change_prior: float | None
    change_new: float | None
    failure: BaseException | None


class FirstReadGate:
    """Hold each writer after its first standing read; retries pass through."""

    def __init__(self) -> None:
        self.barrier = threading.Barrier(THREADS)
        self.lock = threading.Lock()
        self.admitted: set[int] = set()
        self.reads: dict[int, list[float]] = {}

    def arrive(self, value: float) -> None:
        """Record a read and synchronize only the first read of each worker."""
        worker = threading.get_ident()
        with self.lock:
            self.reads.setdefault(worker, []).append(value)
            first = worker not in self.admitted
            self.admitted.add(worker)
        if first:
            self.barrier.wait(timeout=GATE_TIMEOUT)


@pytest.fixture(name="cluster", scope="module")
def _shared_cluster(request: pytest.FixtureRequest) -> Cluster:
    """Expose the established standing fixture under its declared fixture name."""
    assert _cluster_fixture is not None
    provided = request.getfixturevalue("_cluster_fixture")
    assert isinstance(provided, Cluster)
    return provided


def test_a_movement_and_its_change_record_commit_together(cluster: Cluster) -> None:
    """A committed standing movement has exactly one durable justification."""
    procedure_id = cluster.procedure(cluster.client())
    session_id = consume(cluster, procedure_id, SessionOutcome.SUCCEEDED)

    change = record_outcome(
        cluster.store,
        procedure_id,
        session_id,
        SessionOutcome.SUCCEEDED,
        policy=example_policy(),
    )

    assert change is not None
    assert cluster.standing(procedure_id) == pytest.approx(change.new_value)
    assert cluster.count(COUNT_CHANGES, (procedure_id,)) == 1


def test_a_rolled_back_movement_leaves_neither_value_nor_record(cluster: Cluster) -> None:
    """Failure between adjustment and evidence rolls the adjustment back."""
    procedure_id = cluster.procedure(cluster.client())

    def fail_after_adjustment(cursor: Cursor) -> None:
        prior = select_standing(cursor, procedure_id)
        assert prior == EXAMPLE_INITIAL
        adjust_standing(cursor, procedure_id, example_policy().success_delta)
        raise RuntimeError("stop before the evidence write")

    with pytest.raises(RuntimeError, match="evidence write"):
        cluster.store.in_serializable(fail_after_adjustment, label="confidence_atomicity")

    assert cluster.standing(procedure_id) == EXAMPLE_INITIAL
    assert cluster.count(COUNT_CHANGES, (procedure_id,)) == 0


def test_concurrent_adjustment_retries_from_the_committed_value(
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conflicting writer re-reads and records the transition it caused."""
    procedure_id = cluster.procedure(cluster.client())
    sessions = tuple(
        consume(cluster, procedure_id, SessionOutcome.SUCCEEDED) for _ in range(THREADS)
    )
    gate = FirstReadGate()
    original = confidence_store.select_standing

    def gated_read(cursor: Cursor, selected: UUID) -> float | None:
        value = original(cursor, selected)
        assert value is not None
        gate.arrive(value)
        return value

    monkeypatch.setattr(confidence_store, "select_standing", gated_read)
    results: list[WorkerResult] = []
    guard = threading.Lock()

    def work(session_id: UUID) -> None:
        result: WorkerResult
        try:
            change = record_outcome(
                cluster.store,
                procedure_id,
                session_id,
                SessionOutcome.SUCCEEDED,
                policy=example_policy(),
            )
            assert change is not None
            result = WorkerResult(change.prior_value, change.new_value, None)
        except BaseException as error:  # carried to the asserting thread
            result = WorkerResult(None, None, error)
        with guard:
            results.append(result)

    workers = [
        threading.Thread(target=work, args=(session_id,), name=f"confidence-writer-{index}")
        for index, session_id in enumerate(sessions)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=JOIN_TIMEOUT)

    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == THREADS
    assert all(result.failure is None for result in results), results
    assert sum(len(reads) for reads in gate.reads.values()) >= THREADS + 1, (
        "one serializable transaction must have retried and re-read the standing"
    )

    records = history(cluster.store, procedure_id)
    assert len(records) == THREADS
    assert records[0].prior_value == EXAMPLE_INITIAL
    assert records[1].prior_value == pytest.approx(records[0].new_value)
    assert records[-1].new_value == pytest.approx(cluster.standing(procedure_id))


def test_a_below_floor_procedure_remains_stored(cluster: Cluster) -> None:
    """Recall standing can exclude a procedure without deleting its row."""
    procedure_id = cluster.procedure(cluster.client())
    chosen = example_policy()
    while not chosen.below_floor(cluster.standing(procedure_id)):
        record_outcome(
            cluster.store,
            procedure_id,
            consume(cluster, procedure_id, SessionOutcome.FAILED),
            SessionOutcome.FAILED,
            policy=chosen,
        )

    assert cluster.count(COUNT_PROCEDURE_ROWS, (procedure_id,)) == 1


def test_history_returns_transitions_in_change_order(cluster: Cluster) -> None:
    """Each later record starts at the value the preceding record committed."""
    procedure_id = cluster.procedure(cluster.client())
    chosen = example_policy()
    for outcome in (
        SessionOutcome.SUCCEEDED,
        SessionOutcome.FAILED,
        SessionOutcome.ABANDONED,
        SessionOutcome.SUCCEEDED,
    ):
        record_outcome(
            cluster.store,
            procedure_id,
            consume(cluster, procedure_id, outcome),
            outcome,
            policy=chosen,
        )

    records = history(cluster.store, procedure_id)
    assert len(records) == 3
    for earlier, later in pairwise(records):
        assert earlier.changed_at <= later.changed_at
        assert later.prior_value == pytest.approx(earlier.new_value)
