"""Property 37: the working tier is disposable, so its presence changes one number.

**Validates: Requirements 42.10, 42.11, 42.12, 42.13**

The working tier is the one tier the design promises nothing about. No field of a
certificate is derived from it, no verification query reads it, no Disposition
describes one of its rows, and an erasure removes the whole of a tenant's scratch
as one set-based delete accounted for by a single number. Each of those is a
claim about absence, and absence is what a return value cannot show. So this
property runs the shipped erasure twice over the same corpus — once with the
working rows present and once with them absent — and compares what the two runs
produced.

Four decisions shape it.

**The corpus is the graph Property 1 draws, and the working state is drawn across
it.** `memory_graphs()` is imported rather than restated, so the two properties
cannot drift about what a memory graph is, and the working rows are drawn over the
Sessions and the Clients that graph placed. The graph's own working-row count is
replaced with none, because the working state of this property is the thing under
test and has to be entirely the drawn one.

**The presence selector chooses the order, not the arms.** Both arms always run:
an example that only ever ran the present arm would compare nothing. What the
selector draws is which arm goes first, so a difference that came from the order
two runs happened in — a table left non-empty, an evidence row read by the second
run — fails an example rather than hiding behind a fixed sequence.

**The comparison is pairwise over a normalised payload.** Two runs over the same
corpus are still two runs: they carry their own run identifier, their own request,
their own key, and their own instants. Those keys are dropped, the identifiers the
corpus was placed under are replaced with the ordinals they were placed at, and
every collection is re-sorted, so what remains is what the certificate says about
the corpus. That document has to be identical, with the one exception the tier is
allowed: the aggregate count of working rows removed.

**The tier's exemption is read from the catalog, not from the migration text.** A
constraint that exists in a file and not in the catalog enforces nothing, so the
claim that no Lineage_Edge, Client_Binding, Disposition, or Ledger_Checkpoint
references a working row is read from the referential catalog of the schema the
run acted on, and it is read beside the assertion that the run's Dispositions name
only Artifacts.

The example budget is deliberately small: an example places a graph, places up to
fifty working rows, and drives two complete runs against a live instance, so the
cost per example is round trips rather than arithmetic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from types import ModuleType
from typing import Any, Final, cast
from uuid import UUID

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from tests.property.test_p01_erasure_completeness import (
    ERASED_ORDINAL,
    Cluster,
    MemoryGraph,
    Placed,
    PlannedArtifact,
    body_digest,
    memory_graphs,
    request_for,
    seams,
)
from tests.property.test_p01_erasure_completeness import _body_for as body_for
from tests.property.test_p01_erasure_completeness import _rewritten as rewritten

from molt.attest.builder import (
    CAVEATS,
    VERIFICATION_TEMPLATES,
    assemble,
    certificate_payload,
)
from molt.attest.canonical import CERTIFICATE_ARRAY_RULES, canonicalise
from molt.erase.engine import RunStatus, run_erasure
from molt.store import Connection, MemoryStore
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.instance

# How many corpora the property is asserted over. Half of Property 1's budget,
# because an example here is two complete runs against a live instance rather than
# one, and what varies between examples is how much scratch the tier holds rather
# than which branch of the run is taken.
MAX_EXAMPLES: Final[int] = 10

# The working state one example draws, at the bounds the design states: a tenant
# holding no scratch at all is as ordinary as one holding a walk's worth of it.
MIN_WORKING_ROWS: Final[int] = 0
MAX_WORKING_ROWS: Final[int] = 50

# How far a drawn expiry falls either side of the cluster's own reading, in
# seconds. Both signs are drawn because a working row whose expiry has already
# passed is still a row the erasure must account for: the tier's own expiry and the
# tier's erasure are different mechanisms, and neither may depend on the other. No
# instant is written here — the reading is the cluster's and the drawn number is an
# offset from it.
EXPIRY_SPAN_SECONDS: Final[int] = 3600

# The shape of one drawn scratch document. Small, because what is asserted is that
# an arbitrary document changes nothing about the certificate, not that a large one
# round-trips.
MAX_DOCUMENT_KEYS: Final[int] = 3
MAX_KEY_LENGTH: Final[int] = 8
MAX_TEXT_LENGTH: Final[int] = 8

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The cluster's own reading, which every drawn expiry is measured from.
CLUSTER_READING: Final[str] = "SELECT now()"

# One drawn working row, placed directly: the tier owns no insert this property
# could reach, and the expiry is a bound value rather than a computed default
# because the drawn offset is the point.
INSERT_WORKING_ROW: Final[str] = (
    "INSERT INTO working_memory (session_id, client_id, scratch_key, value, expires_at) "
    "VALUES (%s, %s, %s, %s::JSONB, %s)"
)

# What the working tier is counted by, before and after a run.
COUNT_CLIENT_WORKING: Final[str] = "SELECT count(*) FROM working_memory WHERE client_id = %s"
COUNT_ALL_WORKING: Final[str] = "SELECT count(*) FROM working_memory"

# Every surviving row of the tier, paired with whether the Session hosting it also
# survived. The outer join is what makes the pairing readable: the tier's foreign
# key onto the Session cascades, so a surviving row whose Session is gone would be a
# row the schema says cannot exist, and asserting that is worth more than predicting
# which Sessions a run removes. Which Sessions it removes depends on the sweep's
# decision table, and a test that predicted it would be asserting the decision table
# twice rather than asserting the tier.
SELECT_SURVIVING_WORKING: Final[str] = (
    "SELECT w.client_id, w.scratch_key, s.id IS NOT NULL "
    "FROM working_memory AS w LEFT JOIN session AS s ON s.id = w.session_id "
    "ORDER BY w.client_id, w.scratch_key"
)

# The aggregate the run recorded, read from the row that holds it, and the evidence
# the aggregate is asserted against: a Disposition per swept candidate and no more.
SELECT_RUN_WORKING_ROWS: Final[str] = (
    "SELECT working_rows_deleted FROM erasure_run WHERE id = %s AND client_id = %s"
)
COUNT_RUN_DISPOSITIONS: Final[str] = "SELECT count(*) FROM disposition WHERE run_id = %s"
COUNT_RUN_CANDIDATES: Final[str] = "SELECT count(*) FROM erasure_candidate WHERE run_id = %s"
SELECT_DISPOSITION_ARTIFACTS: Final[str] = "SELECT artifact_id FROM disposition WHERE run_id = %s"

# The referential catalog, read for what points at the tier's own table.
REFERENCES_TO_WORKING: Final[str] = (
    "SELECT table_name, constraint_name FROM information_schema.referential_constraints "
    "WHERE constraint_schema = %s AND referenced_table_name = %s"
)

# The table the tier holds, and the four tables Requirement 42 criterion 12 names as
# holding no reference to a row of it.
WORKING_TABLE: Final[str] = "working_memory"
UNREFERENCING_TABLES: Final[frozenset[str]] = frozenset(
    {"lineage_edge", "client_binding", "disposition", "ledger_checkpoint"}
)

# The two verification queries a certificate carries, in the driver's placeholder
# form so this module can run them. Each is a whole literal here and is asserted
# below to be the certificate's own text with the document's placeholder replaced,
# so nothing is executed that a reviewer of the certificate would not run.
VERIFY_NO_CURRENT_ATTRIBUTION: Final[str] = (
    "SELECT b.artifact_id FROM client_binding AS b "
    "WHERE b.client_id = %s AND b.superseded_by IS NULL"
)
VERIFY_NO_SESSIONS: Final[str] = "SELECT s.id FROM session AS s WHERE s.client_id = %s"
RUNNABLE_QUERIES: Final[Mapping[str, str]] = {
    "no_current_attribution_remains": VERIFY_NO_CURRENT_ATTRIBUTION,
    "no_sessions_remain": VERIFY_NO_SESSIONS,
}

# The placeholder a certificate's query text carries, and the one a driver binds by.
DOCUMENT_PLACEHOLDER: Final[str] = "$1"
DRIVER_PLACEHOLDER: Final[str] = "%s"

# The payload keys that name this attempt rather than this corpus. Two runs over one
# corpus differ in exactly these and must differ in nothing else, so they are
# dropped before the documents are compared rather than compared and excused.
#
#   run_id, request_id, idempotency_key: generated per attempt.
#   submitted_at, t_before, t_after, first_attributed_at: instants a second run
#       cannot reproduce, since it happened later.
#   window_start, window_end, records: the audit window is the run's own.
#   backup_id, backup_path, statement: the backup names the run it covers.
VOLATILE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "run_id",
        "request_id",
        "idempotency_key",
        "submitted_at",
        "t_before",
        "t_after",
        "first_attributed_at",
        "window_start",
        "window_end",
        "records",
        "backup_id",
        "backup_path",
        "statement",
    }
)

# The one field of the certificate the working tier is permitted to change, and
# where it sits.
WORKING_FIELD: Final[str] = "working_rows_deleted"
RUN_BLOCK: Final[str] = "run"

# What an identifier and a digest look like, so a value the corpus did not name can
# be recognised as one this attempt generated rather than compared across attempts.
_UUID_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_DIGEST_SHAPE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver installed.
DriverConnection = Any


# ---------------------------------------------------------------------------
# What one drawn working state is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkingRow:
    """One drawn row of scratch: where it hangs, who holds it, and when it expires.

    Attributes:
        session: The ordinal of the Session the row hangs off, where the ordinal
            past the last drawn Session names the governance Session the graph
            places for a retained Client.
        client: The ordinal of the Client the row is held for, drawn freely rather
            than taken from the Session's owner, because the tenant column is what
            the purge predicates on and a row whose tenant and Session disagree is
            a row the schema admits.
        key: The scratch key, unique across the drawn rows so no two of them
            collide on the pair the primary key spans.
        document: The drawn value, already rendered as the document it is stored as.
        offset: How far the stored expiry falls from the cluster's own reading.
    """

    session: int
    client: int
    key: str
    document: str
    offset: int


@dataclass(frozen=True, slots=True)
class WorkingState:
    """One memory graph, the scratch drawn across it, and which arm runs first."""

    graph: MemoryGraph
    rows: tuple[WorkingRow, ...]
    present_first: bool

    @property
    def order(self) -> tuple[bool, bool]:
        """The two arms, in the order the selector drew them."""
        return (True, False) if self.present_first else (False, True)

    @property
    def erased_rows(self) -> int:
        """How many drawn rows the erased Client holds, which is what the run removes."""
        return sum(1 for row in self.rows if row.client == ERASED_ORDINAL)

    def _session_owners(self) -> tuple[int, ...]:
        """The Client ordinal owning each placeable Session, the governance one last."""
        return (*(session.client for session in self.graph.sessions), 1)


@st.composite
def scratch_documents(draw: st.DrawFn) -> str:
    """Draw one arbitrary scratch document, rendered as the value a row stores."""
    document = draw(
        st.dictionaries(
            st.text(min_size=1, max_size=MAX_KEY_LENGTH),
            st.integers() | st.booleans() | st.text(max_size=MAX_TEXT_LENGTH),
            max_size=MAX_DOCUMENT_KEYS,
        )
    )
    return json.dumps(document)


@st.composite
def graphs_with_working_state(draw: st.DrawFn) -> WorkingState:
    """Draw a memory graph crossed with the working state a run finds beside it.

    The graph's own working-row count is replaced with none, because every working
    row of this property is a drawn one and an arm that is meant to hold no scratch
    has to hold none at all.
    """
    graph = draw(memory_graphs())
    placements = len(graph.sessions) + 1
    rows = tuple(
        WorkingRow(
            session=draw(st.integers(min_value=0, max_value=placements - 1)),
            client=draw(st.integers(min_value=0, max_value=graph.clients - 1)),
            key=f"scratch-{index}",
            document=draw(scratch_documents()),
            offset=draw(st.integers(min_value=-EXPIRY_SPAN_SECONDS, max_value=EXPIRY_SPAN_SECONDS)),
        )
        for index in range(
            draw(st.integers(min_value=MIN_WORKING_ROWS, max_value=MAX_WORKING_ROWS))
        )
    )
    return WorkingState(
        graph=replace(graph, working_rows=0),
        rows=rows,
        present_first=draw(st.booleans()),
    )


# ---------------------------------------------------------------------------
# What one arm produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """What one arm of the pair produced, in the form the other is compared against.

    Attributes:
        payload: The certificate document, with this attempt's own identifiers and
            instants removed and the corpus's identifiers replaced by their ordinals.
        verification: Each verification query's result, by the query's name.
        working_deleted: The aggregate the run recorded for the tier.
        placed_before: How many rows the erased Client held when the run began.
        surviving: How many working rows the whole schema holds afterwards.
        survivors: Each surviving row as the Client that owns it, its key, and
            whether the Session hosting it survived too.
        dispositions: How many Dispositions the run recorded.
        candidates: How many candidates the run swept.
        disposed: The Artifacts the Dispositions name.
    """

    payload: Mapping[str, Any]
    verification: Mapping[str, tuple[str, ...]]
    working_deleted: int
    placed_before: int
    surviving: int
    survivors: tuple[tuple[str, str, bool], ...]
    dispositions: int
    candidates: int
    disposed: frozenset[str]


def name_digests(planned: PlannedArtifact, ordinal: int, table: dict[str, str]) -> None:
    """Name the digests of one placed Artifact, before and after a rewrite of it.

    A content digest is a function of the body, and the bodies are a function of the
    graph, so both digests are corpus-named rather than per-attempt and both are
    compared as themselves. What is left unnamed after this is a digest of an Event,
    whose identifier the graph does not fix.
    """
    table[body_digest(body_for(planned))] = f"digest-artifact-{ordinal}"
    table[body_digest(rewritten(planned))] = f"post-digest-artifact-{ordinal}"


def scrubbed(value: str) -> str:
    """One value with a per-attempt identifier or digest replaced by a placeholder.

    Every identifier and digest the corpus fixes is named by the symbol table before
    this is reached, so what still looks like one here was generated by the attempt:
    an Event identifier, or a chain digest computed from one. Replacing it keeps the
    field's presence and its shape in the comparison while dropping the part two
    runs cannot share.
    """
    if _UUID_SHAPE.fullmatch(value):
        return "per-run-identifier"
    if _DIGEST_SHAPE.fullmatch(value):
        return "per-run-digest"
    return value


def symbols(placed: Placed, state: WorkingState) -> dict[str, str]:
    """Every identifier, slug, and digest the corpus fixes, by the ordinal it took.

    A certificate names the rows it describes, and two runs place those rows under
    identifiers of their own, so a pairwise comparison is only meaningful once the
    identifiers are read as the positions they were placed at.
    """
    table: dict[str, str] = {}
    for ordinal, planned in enumerate(state.graph.artifacts):
        name_digests(planned, ordinal, table)
    for ordinal, identifier in enumerate(placed.clients):
        table[str(identifier)] = f"client-{ordinal}"
        table[f"tenant-{identifier.hex[:12]}"] = f"slug-{ordinal}"
    for ordinal, identifier in enumerate(placed.sessions):
        table[str(identifier)] = f"session-{ordinal}"
    table[str(placed.governance_session)] = "session-governance"
    for ordinal, identifier in enumerate(placed.artifacts):
        table[str(identifier)] = f"artifact-{ordinal}"
    return table


def normalised(value: object, table: Mapping[str, str]) -> object:
    """One payload value with this attempt's own parts removed and the corpus named.

    Collections are re-sorted after substitution because the document orders them by
    the identifiers a run generated, and two runs generate different ones: an order
    that followed the identifiers would report a difference where the content agrees.
    """
    if isinstance(value, dict):
        entries = cast(Mapping[str, object], value)
        return {
            key: normalised(held, table)
            for key, held in entries.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        members = [normalised(held, table) for held in cast(list[object], value)]
        return sorted(members, key=lambda held: json.dumps(held, sort_keys=True))
    if isinstance(value, str):
        named = table.get(value)
        return scrubbed(value) if named is None else named
    return value


def payload_for(cluster: Cluster, run_id: UUID) -> Mapping[str, Any]:
    """Assemble the certificate for one run and read it back as a reviewer would."""
    evidence = assemble(cluster.store, run_id)
    document = canonicalise(certificate_payload(evidence), array_rules=CERTIFICATE_ARRAY_RULES)
    parsed = json.loads(document.decode("utf-8"))
    assert isinstance(parsed, dict)
    return cast(Mapping[str, Any], parsed)


def verification_results(
    cluster: Cluster,
    client_id: UUID,
    table: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    """Run every verification query the certificate carries, and report its rows.

    The rows are read through the same symbol table the certificate is, because a
    row a query returns is a row of the corpus and two attempts placed the corpus
    under identifiers of their own.
    """
    produced: dict[str, tuple[str, ...]] = {}
    for name, statement in RUNNABLE_QUERIES.items():
        rows = cluster.rows(statement, (client_id,))
        named = (table.get(str(row[0]), scrubbed(str(row[0]))) for row in rows)
        produced[name] = tuple(sorted(named))
    return produced


def place_working(cluster: Cluster, placed: Placed, state: WorkingState) -> None:
    """Place every drawn working row, with each expiry offset from the cluster's reading."""
    reading = cluster.rows(CLUSTER_READING)[0][0]
    sessions = (*placed.sessions, placed.governance_session)
    for row in state.rows:
        cluster.send(
            INSERT_WORKING_ROW,
            (
                sessions[row.session],
                placed.clients[row.client],
                row.key,
                row.document,
                reading + timedelta(seconds=row.offset),
            ),
        )


def one_arm(cluster: Cluster, state: WorkingState, *, present: bool) -> Observation:
    """Place the corpus, hold the drawn scratch or none of it, and drive one run."""
    cluster.reset()
    placed = cluster.place(state.graph)
    if present:
        place_working(cluster, placed, state)

    placed_before = cluster.count(COUNT_CLIENT_WORKING, (placed.erased,))
    outcome = run_erasure(cluster.store, request_for(placed), seams(placed))

    assert outcome.status is RunStatus.COMPLETED, (
        f"the run did not complete: {outcome.error_detail}"
    )
    assert outcome.run_id is not None
    run_id = outcome.run_id

    table = symbols(placed, state)
    return Observation(
        payload=cast(Mapping[str, Any], normalised(payload_for(cluster, run_id), table)),
        verification=verification_results(cluster, placed.erased, table),
        working_deleted=cluster.count(SELECT_RUN_WORKING_ROWS, (run_id, placed.erased)),
        placed_before=placed_before,
        surviving=cluster.count(COUNT_ALL_WORKING),
        survivors=tuple(
            (table.get(str(row[0]), scrubbed(str(row[0]))), str(row[1]), bool(row[2]))
            for row in cluster.rows(SELECT_SURVIVING_WORKING, ())
        ),
        dispositions=cluster.count(COUNT_RUN_DISPOSITIONS, (run_id,)),
        candidates=cluster.count(COUNT_RUN_CANDIDATES, (run_id,)),
        disposed=frozenset(
            table.get(str(row[0]), scrubbed(str(row[0])))
            for row in cluster.rows(SELECT_DISPOSITION_ARTIFACTS, (run_id,))
        ),
    )


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
        return cast(Connection, opened)

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


@pytest.fixture(scope="module")
def schema_name(fresh_schema: DriverConnection) -> str:
    """The namespace this module's copies of the tables live in."""
    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        return str(row[0])


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 37: For any memory graph crossed with any working state,
# the same erasure run with the working rows present and with them absent produces
# the same certificate and the same verification results but for one aggregate
# count, no evidence record references a working row, and every working row the
# erased Client held is removed with no Disposition of its own.
@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(state=graphs_with_working_state())
def test_the_working_tier_changes_one_count_and_nothing_else(
    cluster: Cluster,
    schema_name: str,
    state: WorkingState,
) -> None:
    event(f"working rows={len(state.rows)}")
    event(f"working rows of the erased client={state.erased_rows}")
    event(f"present arm first={state.present_first}")

    observed = {present: one_arm(cluster, state, present=present) for present in state.order}
    present, absent = observed[True], observed[False]

    # Requirements 42.10 and 42.11: the certificate is derived from evidence outside
    # the tier, so holding scratch or holding none leaves the same document behind
    # apart from the one number the tier is accounted by.
    assert present.payload[RUN_BLOCK][WORKING_FIELD] == str(state.erased_rows)
    assert absent.payload[RUN_BLOCK][WORKING_FIELD] == "0"
    stripped = {
        arm: {
            block: (
                {key: held for key, held in fields.items() if key != WORKING_FIELD}
                if block == RUN_BLOCK
                else fields
            )
            for block, fields in observation.payload.items()
        }
        for arm, observation in ((True, present), (False, absent))
    }
    assert stripped[True] == stripped[False], (
        "the working tier changed a certificate field other than its aggregate count"
    )
    assert "working_tier_excluded" in present.payload["caveats"]
    assert set(present.payload["caveats"]) == set(CAVEATS)

    # Requirement 42.11: the queries a verifier is asked to run read the same rows
    # either way, and none of them names the tier.
    assert present.verification == absent.verification, (
        "a verification query answered differently with the working tier present"
    )
    for template in VERIFICATION_TEMPLATES:
        runnable = RUNNABLE_QUERIES[template.name]
        assert template.sql.replace(DOCUMENT_PLACEHOLDER, DRIVER_PLACEHOLDER) == runnable, (
            f"the query run for {template.name} is not the one the certificate carries"
        )
        assert WORKING_TABLE not in runnable
        # What each query answers is asserted here to be independent of the tier,
        # not to be empty. Whether the run left the rows these queries look for is
        # the sweep's claim and belongs to the properties that own the sweep; a
        # clause about it here would report a sweep result as a working-tier fault.
        assert template.name in present.verification

    # Requirement 42.12: nothing outside the tier holds a reference to a row of it,
    # read from the catalog of the schema the runs acted on, and no Disposition names
    # anything but an Artifact.
    referencing = {
        str(row[0]) for row in cluster.rows(REFERENCES_TO_WORKING, (schema_name, WORKING_TABLE))
    }
    assert referencing & UNREFERENCING_TABLES == frozenset(), (
        f"a table outside the tier references a working row: {sorted(referencing)}"
    )
    for observation in (present, absent):
        assert observation.dispositions == observation.candidates, (
            "the run recorded a disposition for something other than a swept candidate"
        )
    assert present.disposed == absent.disposed, "the disposed set moved with the working tier"

    # Requirement 42.13: the run removed every working row the erased Client held,
    # accounted for by one number rather than a record per row, and left the scratch
    # of the Clients it was not erasing alone.
    assert present.placed_before == state.erased_rows, (
        "the drawn scratch of the erased client was not all placed"
    )
    assert absent.placed_before == 0
    assert present.working_deleted == state.erased_rows, (
        "the aggregate count is not the number of rows the erased client held"
    )
    assert absent.working_deleted == 0
    # Nothing of the erased Client's remains, which is the removal clause stated
    # against the rows rather than against the count that reports them.
    erased_name = f"client-{ERASED_ORDINAL}"
    assert [entry for entry in present.survivors if entry[0] == erased_name] == [], (
        f"scratch of the erased client survived the purge: {present.survivors}"
    )

    # Every surviving row is hosted by a surviving Session. The tier's foreign key
    # onto the Session cascades, so a surviving row whose Session is gone is a row
    # the schema forbids; this is what makes the count below attributable.
    assert [entry for entry in present.survivors if not entry[2]] == [], (
        f"a working row outlived the session hosting it: {present.survivors}"
    )

    # What survives is drawn from the retained Clients' scratch and from nothing else.
    # The bound is an inequality rather than an equality because the cascade above may
    # legitimately have removed some of it: a retained Client's row hosted by a Session
    # the sweep deleted goes with that Session, which is the schema's doing. Which
    # Sessions the sweep deletes is the decision table's claim, asserted by Property 1,
    # and predicting it here would restate that table rather than test this tier.
    retained_drawn = len(state.rows) - state.erased_rows
    assert present.surviving <= retained_drawn, (
        "the tier holds more rows than were ever drawn for the retained clients"
    )
    assert absent.surviving == 0, "no scratch was placed, so none can survive"
    assert absent.survivors == ()
    assert present.dispositions == absent.dispositions, (
        "the working rows earned dispositions of their own"
    )
