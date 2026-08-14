"""The sensitivity grid, the procedure standing view, and the Memory_Tier view.

Credential-free and cluster-free. The store is a stand-in whose read path hands out a
cursor that answers the statements these three views send, and whose write path raises:
all three views are read-only, so a view that opened a write transaction fails the
suite rather than merely being noticed in review.

The application is driven through the Lambda adapter, which is the deployed path, so
what is asserted here is what a browser would receive.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

import pytest

from molt.confidence import ConfidencePolicy
from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import build_app
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.console.routes import procedures as procedures_view
from molt.console.routes import sensitivity as sensitivity_view
from molt.console.routes import tiers as tiers_view
from molt.erase.sensitivity import (
    INAPPLICABLE_REASON,
    PairOutcome,
    SensitivityReport,
    ThresholdGrid,
)
from molt.models.tiers import TIER_NAMES, WORKING_TIER
from molt.store.capability import CapabilityRecord

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

PROCEDURE_ONE: Final[UUID] = UUID(int=11)
PROCEDURE_TWO: Final[UUID] = UUID(int=12)
OWNER: Final[UUID] = UUID(int=21)
OUTCOME_ROW: Final[UUID] = UUID(int=31)

# The creation statement the working table reports, carrying the cron the migration
# applied. The view reads the schedule back from this rather than from a constant.
CREATE_WORKING: Final[str] = (
    "CREATE TABLE public.working_memory (session_id UUID NOT NULL) WITH "
    "(ttl_expiration_expression = 'expires_at', ttl_job_cron = '@hourly', "
    "ttl_delete_batch_size = 500)"
)


class WriteAttemptedError(AssertionError):
    """A read-only view opened a write transaction, which none of the three may."""


class StubCursor:
    """A cursor answering the statements the three views send, and nothing else."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self._rows: list[tuple[object, ...]] = []
        self._one: tuple[object, ...] | None = None

    def execute(self, statement: str, parameters: Sequence[object] | None = None) -> None:
        """Record the statement and prepare the answer it is given."""
        self._log.append(statement)
        self._one = None
        self._rows = []
        if statement == tiers_view.CLUSTER_NOW_QUERY:
            self._one = (NOW,)
        elif statement == tiers_view.WORKING_TTL_CONFIGURATION_QUERY:
            self._one = (CREATE_WORKING,)
        elif statement == tiers_view.WORKING_EXPIRED_QUERY:
            self._one = (3,)
        elif statement in set(tiers_view.TIER_COUNT_QUERIES.values()):
            self._one = (7,)
        elif statement == procedures_view.SELECT_PROCEDURES_QUERY:
            self._rows = [
                (PROCEDURE_ONE, OWNER, 0.05, 1, NOW),
                (PROCEDURE_TWO, OWNER, 0.80, 2, NOW),
            ]
        elif statement.startswith("SELECT procedure_confidence FROM derived_artifact"):
            self._one = (0.05 if _named(parameters, PROCEDURE_ONE) else 0.80,)
        elif statement.startswith("SELECT count(*) FROM procedure_retrieval"):
            self._one = (4,)
        elif statement.startswith("SELECT outcome, count(*) FROM procedure_outcome"):
            self._rows = [("failed", 2), ("succeeded", 1)]
        elif statement.startswith("SELECT id, procedure_id, prior_value"):
            self._rows = [(UUID(int=41), PROCEDURE_ONE, 0.5, 0.35, OUTCOME_ROW, NOW)]

    def fetchone(self) -> tuple[object, ...] | None:
        """The single row a scalar statement answers with."""
        if self._one is not None:
            return self._one
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row a listing statement answers with."""
        return list(self._rows)

    def close(self) -> None:
        """Close the cursor, which this stand-in holds nothing for."""


def _named(parameters: Sequence[object] | None, procedure_id: UUID) -> bool:
    """Whether a statement's parameters name one procedure."""
    return parameters is not None and procedure_id in tuple(parameters)


class StubStore:
    """The read path the three views use, and a write path that refuses."""

    role = "reader"

    def __init__(self) -> None:
        self.statements: list[str] = []

    def read(self, body: Callable[[StubCursor], object]) -> object:
        """Run a read body on a cursor of this stand-in's own."""
        return body(StubCursor(self.statements))

    def in_serializable(self, _body: object, **_: object) -> object:
        """Refuse: none of these three views may open a write transaction."""
        raise WriteAttemptedError("a read-only console view opened a write transaction")

    def known_capabilities(self) -> CapabilityRecord:
        """The capability record, which the health route reads and these views do not."""
        return CapabilityRecord()


def _console(store: StubStore, *, demo_mode: bool = False) -> Console:
    root = Path(__file__).resolve().parents[2]
    settings = ConsoleSettings(
        host="127.0.0.1",
        port=8080,
        demo_mode=demo_mode,
        interface_spec_path=root / "docs" / "interface.json",
        template_directory=root / "web" / "templates",
        static_directory=root / "web" / "static",
    )
    return Console(
        settings=settings,
        store=cast(Any, store),
        credential=Credential(
            auth.credential_record(CREDENTIAL, iterations=2),
            source_name="test",
            source=CredentialSource.ENVIRONMENT,
        ),
        session_key=Credential(
            SESSION_KEY, source_name="test", source=CredentialSource.ENVIRONMENT
        ),
        clock=lambda: NOW,
    )


def _serve(store: StubStore, path: str, *, demo_mode: bool = False) -> LambdaResponse:
    _, cookie = auth.issue(SESSION_KEY, now=NOW)
    event: Mapping[str, object] = {
        "version": "2.0",
        "rawPath": path.partition("?")[0],
        "rawQueryString": path.partition("?")[2],
        "headers": {"accept": "text/html"},
        "cookies": [f"{auth.COOKIE_NAME}={cookie}"],
        "requestContext": {"http": {"method": "GET", "path": path.partition("?")[0]}},
        "body": "",
        "isBase64Encoded": False,
    }
    app = build_app(_console(store, demo_mode=demo_mode))
    return invoke(cast(Any, app), dict(event))


def _html(answer: LambdaResponse) -> str:
    return cast(str, answer["body"])


# -- the sensitivity grid --------------------------------------------------


def _report() -> SensitivityReport:
    """A two-by-two report whose lower row is inapplicable, built without a cluster."""
    grid = ThresholdGrid.from_axes([0.2, 0.5], [0.3, 0.4])
    outcomes: list[PairOutcome] = []
    for pair in grid.pairs:
        if pair.applicable:
            outcomes.append(
                PairOutcome(
                    auto_include_threshold=pair.auto_include_threshold,
                    review_threshold=pair.review_threshold,
                    candidate_count=5,
                    auto_included_count=2,
                    referred_count=3,
                    recovered_count=1,
                )
            )
        else:
            outcomes.append(
                PairOutcome(
                    auto_include_threshold=pair.auto_include_threshold,
                    review_threshold=pair.review_threshold,
                    candidate_count=None,
                    auto_included_count=None,
                    referred_count=None,
                    inapplicable_reason=INAPPLICABLE_REASON,
                )
            )
    return SensitivityReport(
        grid=grid,
        outcomes=tuple(outcomes),
        retained=(),
        searched_at=0.4,
        query_artifact_ids=(UUID(int=51),),
        ground_truth_available=True,
    )


@pytest.fixture
def analysed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the grid from a hand-built report, so no corpus and no search is needed."""
    monkeypatch.setattr(sensitivity_view, "permitted_client_ids", lambda *_: (UUID(int=61),))
    monkeypatch.setattr(sensitivity_view, "analyse_client", lambda *_, **__: _report())
    monkeypatch.setattr(sensitivity_view, "load_configuration", lambda: cast(Any, object()))
    monkeypatch.setattr(sensitivity_view, "default_grid", lambda *_: None)


@pytest.mark.usefixtures("analysed")
def test_the_grid_stays_rectangular_and_names_both_header_directions() -> None:
    answer = _serve(StubStore(), "/sensitivity?client=one")
    assert answer["statusCode"] == 200
    body = _html(answer)
    assert body.count('scope="col"') == 3
    assert body.count('scope="row"') == 2
    assert "<caption>" in body
    rows = sensitivity_view.grid_rows(_report())
    assert {len(row.cells) for row in rows} == {2}


@pytest.mark.usefixtures("analysed")
def test_an_inapplicable_cell_renders_the_word_and_the_reason_and_no_count() -> None:
    body = _html(_serve(StubStore(), "/sensitivity?client=one"))
    assert sensitivity_view.INAPPLICABLE_LABEL in body
    assert INAPPLICABLE_REASON in body
    inapplicable = [cell for row in sensitivity_view.grid_rows(_report()) for cell in row.cells]
    absent = [cell for cell in inapplicable if not cell.applicable]
    assert absent
    for cell in absent:
        assert cell.candidate_count is None
        assert cell.auto_included_count is None
        assert cell.referred_count is None
        assert cell.recovered_count is None


@pytest.mark.usefixtures("analysed")
def test_the_grid_view_names_a_client_picker_with_a_label() -> None:
    body = _html(_serve(StubStore(), "/sensitivity"))
    assert 'for="sensitivity-client"' in body
    assert 'id="sensitivity-client"' in body


@pytest.mark.usefixtures("analysed")
def test_the_grid_view_opens_no_write_transaction() -> None:
    store = StubStore()
    assert _serve(store, "/sensitivity?client=one")["statusCode"] == 200


# -- procedure standing ----------------------------------------------------


def test_the_procedures_view_marks_a_below_floor_procedure_as_retained() -> None:
    answer = _serve(StubStore(), "/procedures")
    assert answer["statusCode"] == 200
    body = _html(answer)
    assert procedures_view.BELOW_FLOOR_MARKER in body
    assert str(PROCEDURE_ONE) in body
    assert str(PROCEDURE_TWO) in body


def test_the_procedures_view_shows_the_change_history_per_procedure() -> None:
    body = _html(_serve(StubStore(), "/procedures"))
    assert "<summary>" in body
    assert "Prior value" in body
    assert "Triggering outcome" in body
    assert str(OUTCOME_ROW) in body


def test_the_procedures_view_opens_no_write_transaction() -> None:
    store = StubStore()
    assert _serve(store, "/procedures")["statusCode"] == 200
    assert store.statements
    assert procedures_view.SELECT_PROCEDURES_QUERY in store.statements


def test_the_below_floor_verdict_is_the_configured_policy_and_not_a_local_number() -> None:
    policy = ConfidencePolicy(initial=0.5, success_delta=0.1, failure_delta=0.2, recall_floor=0.4)
    rows = procedures_view.procedure_rows(cast(Any, StubStore()), policy=policy)
    assert [row.below_floor for row in rows] == [True, False]


# -- the memory tier view --------------------------------------------------


def test_the_tier_view_renders_one_row_per_tier_naming_the_mapping_s_tier_set() -> None:
    answer = _serve(StubStore(), "/tiers")
    assert answer["statusCode"] == 200
    body = _html(answer)
    for name in TIER_NAMES:
        assert f'<th scope="row">{name}</th>' in body
    assert body.count('<th scope="row">') == len(TIER_NAMES)


def test_the_working_row_carries_its_expired_count_and_its_next_sweep() -> None:
    store = StubStore()
    view = tiers_view.read_tiers(cast(Any, store))
    working = [reading for reading in view.readings if reading.name == WORKING_TIER]
    assert len(working) == 1
    assert working[0].expired_count == 3
    assert working[0].cron == "@hourly"
    assert working[0].next_sweep is not None
    assert 0 < working[0].next_sweep.total_seconds() <= 3600
    assert all(
        reading.expired_count is None for reading in view.readings if reading.name != WORKING_TIER
    )


def test_the_counts_are_taken_inside_one_read_only_transaction() -> None:
    store = StubStore()
    assert _serve(store, "/tiers")["statusCode"] == 200
    assert store.statements.count(tiers_view.BEGIN_READ_ONLY_STATEMENT) == 1
    assert store.statements.count(tiers_view.COMMIT_STATEMENT) == 1
    for name in TIER_NAMES:
        assert tiers_view.TIER_COUNT_QUERIES[name] in store.statements


def test_the_counting_statements_and_the_tier_mapping_name_the_same_tiers() -> None:
    assert tuple(tiers_view.TIER_COUNT_QUERIES) == TIER_NAMES


def test_an_uninterpretable_cron_is_reported_rather_than_guessed() -> None:
    assert tiers_view.next_sweep_after("*/17 * * * *", NOW) is None
    assert tiers_view.cron_of("CREATE TABLE t (a INT)") is None
    assert tiers_view.next_sweep_after("@daily", NOW) is not None


def test_the_tier_view_is_available_unchanged_in_demonstration_mode() -> None:
    plain = _html(_serve(StubStore(), "/tiers"))
    demonstration = _html(_serve(StubStore(), "/tiers", demo_mode=True))
    assert "demonstration mode" in demonstration
    assert demonstration.count('<th scope="row">') == plain.count('<th scope="row">')
