"""Unit tests for the capability record, the three probes, and the two query forms.

Nothing here opens a socket. A scripted cursor answers each statement from a
script and keeps what it was sent, so every claim below is read off what the
modules produced. The claims that need a cluster to be meaningful, that the
measured horizon really is the cluster's and that the two query forms really rank
alike over stored vectors, live in the instance-backed module.

Five properties of the shape are checked.

An unprobed fact and a fact probed absent are different answers. That distinction
is what decides a fallback, so it is asserted from both directions on each
accessor rather than inferred from one of them.

The horizon the zone-configuration probe records is in the exact form the
historical read module parses. The proof is a round trip: the detail the probe
produced is fed back through that module's own read, and what comes out is the
interval the cluster reported. A probe writing a plausible-looking detail would
satisfy a string assertion and fail this one.

The backup probe never lets its target reach statement text, a recorded detail, or
a log record, because a backup target may carry credentials in its query
parameters. The target is looked for in everything the probe emitted, on the
refusal path as well as the accepted one.

The two query forms are composed from shared terms, so the projection, the
ceiling, and the ordering are asserted to be the same characters in both rather
than asserted twice. What differs is asserted too: the tenancy term's spelling
and the candidate cap.

Which form is sent follows the record the store holds, and the store holds it
without module state: it is read once, primed explicitly, or read again on
request, and each of those three is driven here directly.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.errors import StoreError
from molt.models.artifact import EMBEDDING_DIMENSION
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.capability import (
    BACKUP_PLAN_QUERY,
    CHANGEFEED,
    GC_HORIZON_SECONDS,
    INDEX_DEFINITION_QUERY,
    ON_DEMAND_BACKUP,
    PROBED_CAPABILITIES,
    RANGEFEED_SETTING,
    RECORD_CAPABILITY_STATEMENT,
    RECORD_QUERY,
    SELF_MANAGED_BACKUP,
    TEXT_PROVIDER_PROMPT_CACHE,
    VECTOR_INDEX,
    VECTOR_INDEX_NAME,
    ZONE_CONFIGURATION_QUERY,
    Capability,
    CapabilityRecord,
    capabilities,
    probe_gc_horizon,
    probe_platform,
    probe_self_managed_backup,
    probe_vector_index,
    record_capability,
)
from molt.store.embeddings import (
    DEFAULT_CANDIDATE_CAP,
    MAX_CANDIDATE_CAP,
    NEAREST_SCAN_STATEMENT,
    NEAREST_STATEMENT,
    VECTOR_INDEX_UNAVAILABLE_METRIC,
    index_served,
    nearest,
)
from molt.store.historical import CAPABILITY_QUERY, GC_HORIZON_CAPABILITY, GcHorizon, gc_horizon
from molt.telemetry import Telemetry, configure, current, reset

# The interval this module's scripted cluster reports, and the zone configuration
# it reports it inside. The configuration is written the way the platform writes
# one, so the reading is a reading rather than a match against a bare number.
HORIZON_SECONDS: Final[int] = 4500
ZONE_CONFIGURATION: Final[str] = (
    "ALTER RANGE default CONFIGURE ZONE USING\n"
    "\trange_min_bytes = 134217728,\n"
    "\trange_max_bytes = 536870912,\n"
    f"\tgc.ttlseconds = {HORIZON_SECONDS},\n"
    "\tnum_replicas = 3,\n"
    "\tconstraints = '[]',\n"
    "\tlease_preferences = '[]'"
)

# The operator class the scripted cluster reports for the vector index, and the
# table definition it reports it inside.
OPERATOR_CLASS: Final[str] = "vector_l2_ops"
TABLE_DEFINITION: Final[str] = (
    "CREATE TABLE public.embedding (\n"
    "\tid UUID NOT NULL DEFAULT gen_random_uuid(),\n"
    "\tvec VECTOR(1024) NOT NULL,\n"
    "\tCONSTRAINT embedding_pkey PRIMARY KEY (id ASC),\n"
    "\tINDEX embedding_by_client (client_id ASC),\n"
    f"\tVECTOR INDEX {VECTOR_INDEX_NAME} (vec {OPERATOR_CLASS})\n"
    ")"
)

# A backup target of the shape an operator configures, carrying credentials in its
# query parameters exactly as one may. Nothing this module asserts may contain it.
BACKUP_SCHEME: Final[str] = "s3"
TARGET_MARKER: Final[str] = "a-key-that-must-not-be-recorded"
BACKUP_TARGET: Final[str] = (
    f"{BACKUP_SCHEME}://operator-owned/molt?AWS_ACCESS_KEY_ID={TARGET_MARKER}"
)

# Statement fragments the script matches an answer to a statement by.
RECORD_FRAGMENT: Final[str] = "FROM capability"
ZONE_FRAGMENT: Final[str] = "ZONE CONFIGURATION"
DEFINITION_FRAGMENT: Final[str] = "SHOW CREATE TABLE"
BACKUP_FRAGMENT: Final[str] = "BACKUP INTO"
NEIGHBOUR_FRAGMENT: Final[str] = "FROM embedding AS e"

# The terms the two neighbour forms share, read from the index-served statement so
# a change there reaches the comparison rather than being restated here.
PROJECTION_TERM: Final[str] = (
    "SELECT e.artifact_id, e.artifact_kind, e.client_id, "
    "(e.vec <=> %s::VECTOR) AS cosine_distance FROM embedding AS e "
)
CEILING_TERM: Final[str] = "AND (%s::FLOAT8 IS NULL OR (e.vec <=> %s::VECTOR) <= %s::FLOAT8) "
ORDERING_TERM: Final[str] = "ORDER BY e.vec <-> %s::VECTOR "
ROW_CAP_TERM: Final[str] = "LIMIT %s"

# How many values each neighbour form binds.
INDEX_PARAMETER_COUNT: Final[int] = 7
SCAN_PARAMETER_COUNT: Final[int] = 8

# Where the candidate cap sits among the exact-scan form's bound values.
CANDIDATE_CAP_POSITION: Final[int] = 2


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()
    error: Exception | None = None


@dataclass(slots=True)
class Script:
    """The answers a connection hands out, consumed in the order they match."""

    answers: list[Answer] = field(default_factory=list)
    sent: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    armed: tuple[tuple[object, ...], ...] = ()

    @property
    def statements(self) -> list[str]:
        """Every statement the script was sent, in order."""
        return [query for query, _ in self.sent]

    @property
    def issued(self) -> list[str]:
        """What the modules sent, with the pool's own setup and reset removed."""
        return [
            query
            for query in self.statements
            if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT)
        ]

    def parameters_of(self, statement: str) -> tuple[object, ...] | None:
        """The bound parameters of the one occurrence of a statement."""
        matches = [params for query, params in self.sent if query == statement]
        assert len(matches) == 1, f"the statement should have been sent once, not {len(matches)}"
        return matches[0]

    def take(self, query: str) -> Answer | None:
        """The next answer matching a statement, removed from the script."""
        for index, answer in enumerate(self.answers):
            if answer.fragment in query:
                return self.answers.pop(index)
        return None


class ScriptedCursor:
    """A cursor answering from a script and recording what it was sent."""

    def __init__(self, script: Script) -> None:
        self._script = script
        self.released = False

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then raise or arm rows as the script says."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        if answer is None:
            self._script.armed = ()
            return None
        if answer.error is not None:
            raise answer.error
        self._script.armed = answer.rows
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or None when the statement armed none."""
        rows = self._script.armed
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._script.armed)

    def close(self) -> None:
        """Mark this cursor released."""
        self.released = True


class ScriptedConnection:
    """A connection handing out scripted cursors over one shared script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Open a recording cursor over this connection's script."""
        return ScriptedCursor(self.script)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def unit_vector(index: int = 0) -> tuple[float, ...]:
    """A unit-length vector of the fixed width, with one component carrying it."""
    components = [0.0] * EMBEDDING_DIMENSION
    components[index] = 1.0
    return tuple(components)


@pytest.fixture
def telemetry_sink() -> Iterator[io.StringIO]:
    """Install a process-wide telemetry instance writing to a sink for one test."""
    sink = io.StringIO()
    configure(Configuration(environ={"MOLT_LOG_LEVEL": "debug"}, file_values={}), stream=sink)
    try:
        yield sink
    finally:
        reset()


def instance() -> Telemetry:
    """The process-wide telemetry instance the modules emitted through."""
    return current()


# ---------------------------------------------------------------------------
# Reading the record
# ---------------------------------------------------------------------------


def test_the_record_read_is_one_statement_binding_nothing() -> None:
    """The whole record arrives in one read, and no value reaches that statement."""
    script = Script(
        answers=[
            Answer(
                RECORD_FRAGMENT,
                (
                    (CHANGEFEED, True, None),
                    (GC_HORIZON_SECONDS, True, str(HORIZON_SECONDS)),
                    (VECTOR_INDEX, True, OPERATOR_CLASS),
                ),
            )
        ]
    )

    record = capabilities(build_store(script))

    assert script.issued == [RECORD_QUERY]
    assert script.parameters_of(RECORD_QUERY) is None
    assert record.vector_index is True
    assert record.vector_index_operator_class == OPERATOR_CLASS
    assert record.changefeed is True
    assert record.detail(GC_HORIZON_SECONDS) == str(HORIZON_SECONDS)


def test_an_unprobed_fact_is_neither_present_nor_absent() -> None:
    """Nobody looked, so nothing is claimed in either direction."""
    record = CapabilityRecord()

    assert record.probed(VECTOR_INDEX) is False
    assert record.available(VECTOR_INDEX) is False
    assert record.unavailable(VECTOR_INDEX) is False
    assert record.of(VECTOR_INDEX) is None
    assert record.detail(VECTOR_INDEX) is None


def test_a_fact_probed_absent_is_reported_absent_rather_than_unprobed() -> None:
    """The cluster was asked and said no, which is what a fallback turns on."""
    record = CapabilityRecord((Capability(VECTOR_INDEX, available=False),))

    assert record.probed(VECTOR_INDEX) is True
    assert record.available(VECTOR_INDEX) is False
    assert record.unavailable(VECTOR_INDEX) is True
    assert record.vector_index_operator_class is None


def test_the_record_lists_the_facts_no_row_answers_for() -> None:
    """Every name the design expects an answer for is reported until one arrives."""
    assert CapabilityRecord().unprobed == PROBED_CAPABILITIES

    answered = CapabilityRecord(
        tuple(Capability(name, available=True) for name in PROBED_CAPABILITIES)
    )

    assert answered.unprobed == ()
    assert answered.self_managed_backup is True
    assert answered.on_demand_backup is True
    assert answered.rangefeed_setting is True
    assert answered.text_provider_prompt_cache is True


def test_every_named_fact_the_design_expects_is_spelled_once() -> None:
    """The names are this module family's own, and the horizon's is imported."""
    assert GC_HORIZON_SECONDS == GC_HORIZON_CAPABILITY
    assert set(PROBED_CAPABILITIES) == {
        CHANGEFEED,
        GC_HORIZON_SECONDS,
        ON_DEMAND_BACKUP,
        RANGEFEED_SETTING,
        SELF_MANAGED_BACKUP,
        TEXT_PROVIDER_PROMPT_CACHE,
        VECTOR_INDEX,
    }
    assert len(set(PROBED_CAPABILITIES)) == len(PROBED_CAPABILITIES)


@pytest.mark.parametrize(
    "row",
    [
        pytest.param((VECTOR_INDEX, True), id="too_narrow"),
        pytest.param((VECTOR_INDEX, True, None, None), id="too_wide"),
    ],
)
def test_a_capability_row_of_the_wrong_width_is_refused(row: tuple[object, ...]) -> None:
    """A statement and its decoder cannot drift apart silently."""
    script = Script(answers=[Answer(RECORD_FRAGMENT, (row,))])

    with pytest.raises(StoreError, match="column"):
        capabilities(build_store(script))


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param((None, True, None), "name did not return text", id="name_absent"),
        pytest.param((VECTOR_INDEX, "yes", None), "did not return a boolean", id="answer_as_text"),
        pytest.param((VECTOR_INDEX, True, 4500), "detail did not return text", id="detail_number"),
    ],
)
def test_a_capability_column_of_another_type_is_refused(
    row: tuple[object, ...],
    expected: str,
) -> None:
    """Each column is what the schema declares, or the record is not read."""
    script = Script(answers=[Answer(RECORD_FRAGMENT, (row,))])

    with pytest.raises(StoreError, match=expected):
        capabilities(build_store(script))


def test_a_capability_row_answering_for_no_fact_is_refused() -> None:
    """A row with no name answers nothing, so it is not a capability."""
    with pytest.raises(ValueError, match="name of the fact"):
        Capability("", available=True)


# ---------------------------------------------------------------------------
# Recording a probe result
# ---------------------------------------------------------------------------


def test_recording_binds_the_name_the_answer_and_the_detail() -> None:
    """One write, every value bound, and the reading instant the cluster's own."""
    script = Script()
    probed = Capability(VECTOR_INDEX, available=True, detail=OPERATOR_CLASS)

    assert record_capability(build_store(script), probed) == probed

    assert script.parameters_of(RECORD_CAPABILITY_STATEMENT) == (
        VECTOR_INDEX,
        True,
        OPERATOR_CLASS,
    )
    assert "now()" in RECORD_CAPABILITY_STATEMENT
    assert RECORD_CAPABILITY_STATEMENT.startswith("UPSERT"), "re-probing replaces one answer"


# ---------------------------------------------------------------------------
# The zone-configuration probe
# ---------------------------------------------------------------------------


def test_the_horizon_probe_records_the_measured_interval_in_the_form_it_is_read_in() -> None:
    """The detail the probe wrote is read back by the module that owns the form."""
    script = Script(answers=[Answer(ZONE_FRAGMENT, ((ZONE_CONFIGURATION,),))])

    probed = probe_gc_horizon(build_store(script))

    assert probed == Capability(GC_HORIZON_SECONDS, available=True, detail=str(HORIZON_SECONDS))
    assert ZONE_CONFIGURATION_QUERY in script.statements
    assert script.parameters_of(ZONE_CONFIGURATION_QUERY) is None

    reader = Script(answers=[Answer("FROM capability", ((probed.available, probed.detail),))])

    assert gc_horizon(build_store(reader)) == GcHorizon(seconds=HORIZON_SECONDS)
    assert reader.parameters_of(CAPABILITY_QUERY) == (GC_HORIZON_CAPABILITY,)


@pytest.mark.parametrize(
    "configuration",
    [
        pytest.param("ALTER RANGE default CONFIGURE ZONE USING num_replicas = 3", id="no_interval"),
        pytest.param("gc.ttlseconds = 0", id="reaches_no_distance"),
        pytest.param("gc.ttlseconds = 4500.5", id="not_a_whole_count"),
        pytest.param("gc.ttlseconds = \u0664\u0665\u0660\u0660", id="digits_of_another_script"),
        pytest.param("", id="empty"),
    ],
)
def test_a_configuration_naming_no_usable_interval_records_no_horizon(configuration: str) -> None:
    """A horizon nobody measured is recorded unmeasured rather than assumed."""
    script = Script(answers=[Answer(ZONE_FRAGMENT, ((configuration,),))])

    probed = probe_gc_horizon(build_store(script))

    assert probed.available is False
    assert probed.detail is None
    assert script.parameters_of(RECORD_CAPABILITY_STATEMENT) == (
        GC_HORIZON_SECONDS,
        False,
        None,
    )


def test_a_zone_configuration_read_that_fails_records_an_unavailable_horizon() -> None:
    """A probe that cannot measure records that, so a later refusal can name it."""
    script = Script(answers=[Answer(ZONE_FRAGMENT, error=RuntimeError("no permission"))])

    probed = probe_gc_horizon(build_store(script))

    assert probed == Capability(GC_HORIZON_SECONDS, available=False, detail=None)
    assert RECORD_CAPABILITY_STATEMENT in script.statements


def test_a_zone_configuration_read_returning_no_row_records_no_horizon() -> None:
    """Nothing measured is nothing recorded, and no statement decides otherwise."""
    script = Script(answers=[Answer(ZONE_FRAGMENT, ())])

    assert probe_gc_horizon(build_store(script)).available is False


# ---------------------------------------------------------------------------
# The index-definition probe
# ---------------------------------------------------------------------------


def test_the_index_probe_records_the_operator_class_the_cluster_reports() -> None:
    """What is recorded is the cluster's own reading of the index it holds."""
    script = Script(answers=[Answer(DEFINITION_FRAGMENT, ((TABLE_DEFINITION,),))])

    probed = probe_vector_index(build_store(script))

    assert probed == Capability(VECTOR_INDEX, available=True, detail=OPERATOR_CLASS)
    assert INDEX_DEFINITION_QUERY in script.statements


@pytest.mark.parametrize(
    "definition",
    [
        pytest.param("CREATE TABLE public.embedding (vec VECTOR(1024) NOT NULL)", id="no_index"),
        pytest.param(
            f"CREATE TABLE public.embedding (\n\tVECTOR INDEX {VECTOR_INDEX_NAME} "
            "(payload vector_l2_ops)\n)",
            id="another_column",
        ),
        pytest.param(
            "CREATE TABLE public.embedding (\n\tVECTOR INDEX other_idx (vec vector_l2_ops)\n)",
            id="another_index",
        ),
    ],
)
def test_a_definition_reporting_no_usable_vector_index_records_it_absent(definition: str) -> None:
    """An index the cluster does not report is an index the fallback exists for."""
    script = Script(answers=[Answer(DEFINITION_FRAGMENT, ((definition,),))])

    probed = probe_vector_index(build_store(script))

    assert probed == Capability(VECTOR_INDEX, available=False, detail=None)


def test_an_index_definition_read_that_fails_records_the_index_absent() -> None:
    """A reading that could not be taken leaves the primary path unclaimed."""
    script = Script(answers=[Answer(DEFINITION_FRAGMENT, error=RuntimeError("no such table"))])

    assert probe_vector_index(build_store(script)).available is False


def test_an_introspection_result_of_another_shape_is_a_reading_that_failed() -> None:
    """A result the decoder does not read is recorded as unread, not guessed at."""
    script = Script(answers=[Answer(DEFINITION_FRAGMENT, (("embedding", TABLE_DEFINITION),))])

    probed = probe_vector_index(build_store(script))

    assert probed == Capability(VECTOR_INDEX, available=False, detail=None)


# ---------------------------------------------------------------------------
# The backup probe
# ---------------------------------------------------------------------------


def test_the_backup_probe_plans_the_statement_and_records_the_scheme() -> None:
    """The target is planned rather than run, and only its scheme is recorded."""
    script = Script(answers=[Answer(BACKUP_FRAGMENT, (("distribution: local",),))])

    probed = probe_self_managed_backup(build_store(script), target=BACKUP_TARGET)

    assert probed == Capability(SELF_MANAGED_BACKUP, available=True, detail=BACKUP_SCHEME)
    assert script.parameters_of(BACKUP_PLAN_QUERY) == (BACKUP_TARGET,)
    assert BACKUP_PLAN_QUERY.startswith("EXPLAIN "), "planning moves no data and creates no job"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            Answer(BACKUP_FRAGMENT, (("distribution: local",),)),
            id="planned",
        ),
        pytest.param(
            Answer(BACKUP_FRAGMENT, error=RuntimeError(f"cannot write to {BACKUP_TARGET}")),
            id="refused",
        ),
    ],
)
@pytest.mark.usefixtures("telemetry_sink")
def test_the_backup_target_reaches_no_statement_no_detail_and_no_record(
    answer: Answer,
    telemetry_sink: io.StringIO,
) -> None:
    """A target may carry a credential, so nothing but its scheme is ever kept."""
    script = Script(answers=[answer])

    probed = probe_self_managed_backup(build_store(script), target=BACKUP_TARGET)

    assert probed.detail == BACKUP_SCHEME
    assert probed.available is (answer.error is None)
    for statement in script.statements:
        assert TARGET_MARKER not in statement
        assert BACKUP_TARGET not in statement
    assert TARGET_MARKER not in telemetry_sink.getvalue()
    assert BACKUP_TARGET not in telemetry_sink.getvalue()


def test_a_target_naming_no_storage_scheme_is_refused_before_anything_is_sent() -> None:
    """There is nothing to plan against, and the refusal names the fault only."""
    script = Script()

    with pytest.raises(ValueError, match="storage scheme") as raised:
        probe_self_managed_backup(build_store(script), target="operator-owned/molt")

    assert script.statements == []
    assert "operator-owned" not in str(raised.value)


# ---------------------------------------------------------------------------
# The probes run together
# ---------------------------------------------------------------------------


def test_running_the_probes_returns_the_record_they_produced() -> None:
    """The record a caller gets back is read after the rows landed, not before."""
    script = Script(
        answers=[
            Answer(DEFINITION_FRAGMENT, ((TABLE_DEFINITION,),)),
            Answer(ZONE_FRAGMENT, ((ZONE_CONFIGURATION,),)),
            Answer(BACKUP_FRAGMENT, (("distribution: local",),)),
            Answer(
                RECORD_FRAGMENT,
                (
                    (GC_HORIZON_SECONDS, True, str(HORIZON_SECONDS)),
                    (SELF_MANAGED_BACKUP, True, BACKUP_SCHEME),
                    (VECTOR_INDEX, True, OPERATOR_CLASS),
                ),
            ),
        ]
    )
    store = build_store(script)

    record = probe_platform(store, backup_target=BACKUP_TARGET)

    assert record.vector_index_operator_class == OPERATOR_CLASS
    assert record.self_managed_backup is True
    assert record.detail(GC_HORIZON_SECONDS) == str(HORIZON_SECONDS)
    assert script.statements.index(RECORD_CAPABILITY_STATEMENT) < script.statements.index(
        RECORD_QUERY
    ), "the record is read after the rows it reports landed"
    assert store.known_capabilities() == record


def test_the_probes_leave_the_backup_row_unprobed_when_no_target_is_configured() -> None:
    """A question with no target has no target-free form, so it is not answered."""
    script = Script(
        answers=[
            Answer(DEFINITION_FRAGMENT, ((TABLE_DEFINITION,),)),
            Answer(ZONE_FRAGMENT, ((ZONE_CONFIGURATION,),)),
            Answer(RECORD_FRAGMENT, ((VECTOR_INDEX, True, OPERATOR_CLASS),)),
        ]
    )

    record = probe_platform(build_store(script))

    assert SELF_MANAGED_BACKUP in record.unprobed
    assert BACKUP_PLAN_QUERY not in script.statements


# ---------------------------------------------------------------------------
# The record the store holds
# ---------------------------------------------------------------------------


def test_the_record_is_read_once_and_held() -> None:
    """A second caller spends no round trip on rows that have already arrived."""
    script = Script(answers=[Answer(RECORD_FRAGMENT, ((VECTOR_INDEX, True, OPERATOR_CLASS),))])
    store = build_store(script)

    first = store.capabilities()
    second = store.capabilities()

    assert first == second
    assert script.issued == [RECORD_QUERY]


def test_the_held_record_is_re_read_only_when_a_caller_asks() -> None:
    """A probe that has just recorded a row takes effect on request, not by chance."""
    script = Script(
        answers=[
            Answer(RECORD_FRAGMENT, ((VECTOR_INDEX, False, None),)),
            Answer(RECORD_FRAGMENT, ((VECTOR_INDEX, True, OPERATOR_CLASS),)),
        ]
    )
    store = build_store(script)

    assert store.capabilities().unavailable(VECTOR_INDEX) is True
    assert store.capabilities(refresh=True).vector_index is True
    assert script.issued == [RECORD_QUERY, RECORD_QUERY]


def test_a_primed_record_is_held_without_any_read() -> None:
    """A startup sequence and a test both establish the record rather than time it."""
    script = Script()
    store = build_store(script)
    primed = CapabilityRecord((Capability(VECTOR_INDEX, available=True, detail=OPERATOR_CLASS),))

    store.prime_capabilities(primed)

    assert store.capabilities() == primed
    assert store.known_capabilities() == primed
    assert script.statements == []


def test_the_held_record_is_empty_before_anything_is_read() -> None:
    """The honest reading of a cluster nobody probed, and it costs no statement."""
    script = Script()

    assert build_store(script).known_capabilities() == CapabilityRecord()
    assert script.statements == []


# ---------------------------------------------------------------------------
# The two neighbour forms
# ---------------------------------------------------------------------------


def test_the_two_forms_share_the_projection_the_ceiling_and_the_ordering() -> None:
    """One set of terms builds both, so neither can drift from the other."""
    for term in (PROJECTION_TERM, CEILING_TERM, ORDERING_TERM):
        assert term in NEAREST_STATEMENT
        assert term in NEAREST_SCAN_STATEMENT

    assert NEAREST_STATEMENT.startswith(PROJECTION_TERM)
    assert NEAREST_SCAN_STATEMENT.startswith(PROJECTION_TERM)
    assert NEAREST_STATEMENT.endswith(ORDERING_TERM + ROW_CAP_TERM)
    assert NEAREST_SCAN_STATEMENT.endswith(ORDERING_TERM + ROW_CAP_TERM)


def test_the_forms_differ_in_the_tenancy_term_and_the_candidate_cap_alone() -> None:
    """The exact scan admits the same rows and bounds what it computes over."""
    assert "WHERE EXISTS (" in NEAREST_STATEMENT
    assert "WHERE e.artifact_id IN (" in NEAREST_SCAN_STATEMENT
    assert "b.client_id = ANY (%s::UUID[])" in NEAREST_SCAN_STATEMENT
    assert "b.superseded_by IS NULL" in NEAREST_SCAN_STATEMENT
    assert NEAREST_STATEMENT.count("%s") == INDEX_PARAMETER_COUNT
    assert NEAREST_SCAN_STATEMENT.count("%s") == SCAN_PARAMETER_COUNT


def test_an_unprobed_cluster_is_answered_by_the_index_served_form() -> None:
    """A missing row is not evidence of a missing index, so nothing degrades."""
    script = Script(answers=[Answer(NEIGHBOUR_FRAGMENT, ())])
    store = build_store(script)

    assert nearest(store, unit_vector(), permitted_clients=[uuid4()]) == ()

    assert NEAREST_STATEMENT in script.statements
    assert NEAREST_SCAN_STATEMENT not in script.statements
    assert RECORD_QUERY not in script.statements, "the critical path spends no read on this"


def test_a_cluster_reporting_the_index_absent_is_answered_by_the_bounded_scan(
    telemetry_sink: io.StringIO,
) -> None:
    """The fallback is taken, the cap is bound, and taking it is recorded."""
    script = Script(answers=[Answer(NEIGHBOUR_FRAGMENT, ())])
    store = build_store(script)
    store.prime_capabilities(CapabilityRecord((Capability(VECTOR_INDEX, available=False),)))
    client_id = uuid4()

    assert nearest(store, unit_vector(), permitted_clients=[client_id]) == ()

    bound = script.parameters_of(NEAREST_SCAN_STATEMENT)
    assert bound is not None
    assert bound[1] == [client_id]
    assert bound[CANDIDATE_CAP_POSITION] == DEFAULT_CANDIDATE_CAP
    assert NEAREST_STATEMENT not in script.statements
    assert (VECTOR_INDEX_UNAVAILABLE_METRIC, ()) in instance().counters()
    assert "bounded exact scan" in telemetry_sink.getvalue()


@pytest.mark.usefixtures("telemetry_sink")
def test_the_reported_index_keeps_the_query_on_the_index_served_form() -> None:
    """A probe that found the index leaves the primary path in place and silent."""
    script = Script(answers=[Answer(NEIGHBOUR_FRAGMENT, ())])
    store = build_store(script)
    store.prime_capabilities(
        CapabilityRecord((Capability(VECTOR_INDEX, available=True, detail=OPERATOR_CLASS),))
    )

    assert index_served(store) is True
    assert nearest(store, unit_vector(), permitted_clients=[uuid4()]) == ()

    assert NEAREST_STATEMENT in script.statements
    assert (VECTOR_INDEX_UNAVAILABLE_METRIC, ()) not in instance().counters()


def test_a_candidate_cap_past_the_ceiling_is_refused_before_anything_is_sent() -> None:
    """No caller turns the bounded scan into an unbounded one."""
    script = Script()

    with pytest.raises(ValueError, match="may not exceed"):
        nearest(
            build_store(script),
            unit_vector(),
            permitted_clients=[uuid4()],
            candidate_cap=MAX_CANDIDATE_CAP + 1,
        )

    assert NEAREST_SCAN_STATEMENT not in script.statements
    assert NEAREST_STATEMENT not in script.statements


def test_a_caller_permitted_no_client_is_answered_by_neither_form() -> None:
    """Neither tenancy term admits a row for an empty set, so neither is sent."""
    script = Script()
    store = build_store(script)
    store.prime_capabilities(CapabilityRecord((Capability(VECTOR_INDEX, available=False),)))
    empty: list[UUID] = []

    assert nearest(store, unit_vector(), permitted_clients=empty) == ()

    assert NEAREST_SCAN_STATEMENT not in script.statements
    assert NEAREST_STATEMENT not in script.statements
