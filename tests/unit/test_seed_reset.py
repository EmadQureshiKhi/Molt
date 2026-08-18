"""Unit tests for the seed reset: its scope, its order, its counts, and its refusal.

Nothing here opens a socket. A scripted cursor answers each statement from a script
and keeps what it was sent, following the shape the store suites already use, so
every claim below is asserted by reading the statements the module produced and the
report it built from what those statements answered.

Six properties are checked.

The scope is the corpus definition's tenants. The lookup binds the definition's
slugs and nothing else, every delete binds the identifiers that lookup returned, and
no statement carries a slug or an identifier inside its text.

The order is the one the surviving reference permits. The Events go before the
Sessions, because `ledger.session_id` is the reference migration 017 left enforced,
and each statement whose predicate reads a table is issued before the statement that
empties that table.

The counts are the cluster's. Each delete computes its own aggregate inside the same
statement, and the reported number per table is the number that statement answered
rather than anything the module assumed.

An empty corpus reports zero. A cluster holding none of the definition's slugs is
sent no delete at all and reports zero for every table; a cluster holding the tenants
with nothing in them is sent the deletes and reports zero as well.

A slug is not enough to authorise a delete. A stored tenant whose display name,
jurisdiction, or content markers are not the definition's ends the reset with a
refusal, before any delete is sent.

The tenant rows survive. No statement of the module deletes from `client`, so a reset
empties a seeded tenant and never removes one.

What no test here can establish is that the cluster accepts these statements in this
order under the privileges the verb connects with: whether the references hold, what
the aggregates really count, and whether the acting role may delete from each table
are facts about a running cluster. Those belong to the instance-backed suite.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.errors import StoreError
from molt.seed.corpora import DOMAINS, ClientDomain
from molt.seed.reset import (
    DELETE_BINDINGS_STATEMENT,
    DELETE_DERIVED_STATEMENT,
    DELETE_EDGES_STATEMENT,
    DELETE_EMBEDDINGS_STATEMENT,
    DELETE_EVENTS_STATEMENT,
    DELETE_SESSIONS_STATEMENT,
    DELETE_WORKING_STATEMENT,
    RESET_ORDER,
    SELECT_SEEDED_CLIENTS_STATEMENT,
    ResetRefusedError,
    TableRemoval,
    reset_corpus,
    seeded_slugs,
)
from molt.store import Connection, MemoryStore
from molt.store.retry import COMMIT_STATEMENT, SERIALIZABLE_STATEMENT

# Every statement the module holds, so the containment claims are asserted over the
# whole set rather than over the ones a test happened to drive.
ALL_STATEMENTS: Final[tuple[str, ...]] = (
    SELECT_SEEDED_CLIENTS_STATEMENT,
    DELETE_WORKING_STATEMENT,
    DELETE_EMBEDDINGS_STATEMENT,
    DELETE_EDGES_STATEMENT,
    DELETE_BINDINGS_STATEMENT,
    DELETE_DERIVED_STATEMENT,
    DELETE_EVENTS_STATEMENT,
    DELETE_SESSIONS_STATEMENT,
)

# The tables the reset empties, read from the module's own order rather than
# restated, so the suite and the module cannot disagree about what a reset touches.
RESET_TABLES: Final[tuple[str, ...]] = tuple(table for table, _, _ in RESET_ORDER)

# The tenant row the reset never removes, and the evidence tables it never reaches. The
# removal is written as the whole fragment rather than composed from the table name,
# because composing statement text is exactly what this module refuses to do.
TENANT_TABLE: Final[str] = "client"
TENANT_REMOVAL_FRAGMENT: Final[str] = "DELETE FROM client "
TENANT_REMOVAL_TAIL: Final[str] = "DELETE FROM client"
EVIDENCE_TABLES: Final[tuple[str, ...]] = (
    "erasure_request",
    "erasure_run",
    "erasure_candidate",
    "disposition",
    "erasure_certificate",
    "ledger_checkpoint",
    "checkpoint_session",
)

# Fragments the script matches a statement by.
LOOKUP_FRAGMENT: Final[str] = "FROM client WHERE slug"
WORKING_FRAGMENT: Final[str] = "DELETE FROM working_memory"
EMBEDDING_FRAGMENT: Final[str] = "DELETE FROM embedding"
EDGE_FRAGMENT: Final[str] = "DELETE FROM lineage_edge"
BINDING_FRAGMENT: Final[str] = "DELETE FROM client_binding"
DERIVED_FRAGMENT: Final[str] = "DELETE FROM derived_artifact"
LEDGER_FRAGMENT: Final[str] = "DELETE FROM ledger"
SESSION_FRAGMENT: Final[str] = "DELETE FROM session"

DELETE_FRAGMENTS: Final[tuple[str, ...]] = (
    WORKING_FRAGMENT,
    EMBEDDING_FRAGMENT,
    EDGE_FRAGMENT,
    BINDING_FRAGMENT,
    DERIVED_FRAGMENT,
    LEDGER_FRAGMENT,
    SESSION_FRAGMENT,
)

# The count each table is scripted to report, one distinct number per table so a
# report that mixed two of them up could not pass.
SCRIPTED_COUNTS: Final[dict[str, int]] = {
    "working_memory": 12,
    "embedding": 34,
    "lineage_edge": 5,
    "client_binding": 21,
    "derived_artifact": 9,
    "ledger": 260,
    "session": 28,
}

# The two tenants every driven reset resolves, and their identifiers.
FIRST_DOMAIN: Final[ClientDomain] = DOMAINS[0]
SECOND_DOMAIN: Final[ClientDomain] = DOMAINS[1]
FIRST_ID: Final[UUID] = uuid4()
SECOND_ID: Final[UUID] = uuid4()

# A display name a real tenant might carry on a slug the seed also uses.
FOREIGN_NAME: Final[str] = "a tenant the seed did not write"

# Text an operator might have put in a tenant row that would end a statement or
# comment out the rest of one, if any value ever reached statement text.
HOSTILE_TEXT: Final[str] = "'; DROP TABLE ledger; --"


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()


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
        """Record the statement, then arm the rows the script says it answers with."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        self._script.armed = () if answer is None else answer.rows
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


def stored_row(
    domain: ClientDomain,
    identifier: UUID,
    *,
    display_name: str | None = None,
    jurisdiction: str | None = None,
    markers: Sequence[str] | None = None,
) -> tuple[object, ...]:
    """One tenant row of the width the lookup selects, as the cluster would report it."""
    return (
        identifier,
        domain.slug,
        domain.display_name if display_name is None else display_name,
        domain.jurisdiction if jurisdiction is None else jurisdiction,
        list(domain.content_markers) if markers is None else list(markers),
    )


def seeded_script(*, rows: tuple[tuple[object, ...], ...] | None = None) -> Script:
    """A script answering the lookup with two seeded tenants and each delete with a count."""
    resolved = (
        (stored_row(FIRST_DOMAIN, FIRST_ID), stored_row(SECOND_DOMAIN, SECOND_ID))
        if rows is None
        else rows
    )
    answers = [Answer(LOOKUP_FRAGMENT, resolved)]
    for table, fragment in zip(RESET_TABLES, DELETE_FRAGMENTS, strict=True):
        answers.append(Answer(fragment, ((SCRIPTED_COUNTS[table],),)))
    return Script(answers=answers)


def empty_script() -> Script:
    """A script whose lookup finds no tenant and whose deletes are never reached."""
    return Script(answers=[Answer(LOOKUP_FRAGMENT, ())])


def zeroed_script() -> Script:
    """A script resolving both tenants and answering every delete with no rows removed."""
    answers = [
        Answer(
            LOOKUP_FRAGMENT,
            (stored_row(FIRST_DOMAIN, FIRST_ID), stored_row(SECOND_DOMAIN, SECOND_ID)),
        )
    ]
    answers.extend(Answer(fragment, ((0,),)) for fragment in DELETE_FRAGMENTS)
    return Script(answers=answers)


def issued(script: Script) -> list[str]:
    """Every delete statement the script was sent, in the order it was sent."""
    return [
        query
        for query in script.statements
        if any(fragment in query for fragment in DELETE_FRAGMENTS)
    ]


# ---------------------------------------------------------------------------
# The scope: the corpus definition's tenants, and nothing else
# ---------------------------------------------------------------------------


def test_the_scope_is_every_slug_the_corpus_definition_names() -> None:
    """The reset targets the definition's tenants, whatever volumes a run asked for."""
    assert seeded_slugs() == tuple(domain.slug for domain in DOMAINS)


def test_the_lookup_binds_the_definition_slugs_and_names_none_in_its_text() -> None:
    """One array of slugs, bound, with the tenant table and no slug inside the statement."""
    script = seeded_script()

    reset_corpus(build_store(script))

    assert script.parameters_of(SELECT_SEEDED_CLIENTS_STATEMENT) == (list(seeded_slugs()),)
    for slug in seeded_slugs():
        assert slug not in SELECT_SEEDED_CLIENTS_STATEMENT


def test_every_delete_binds_the_identifiers_the_lookup_returned() -> None:
    """No delete names a tenant any other way than by the identifiers that came back."""
    script = seeded_script()

    reset_corpus(build_store(script))

    identifiers = [FIRST_ID, SECOND_ID]
    for _, statement, arity in RESET_ORDER:
        bound = script.parameters_of(statement)
        assert bound == tuple([identifiers] * arity)
        assert statement.count("%s") == arity


def test_a_narrowed_scope_resolves_fewer_tenants_and_widens_to_none() -> None:
    """A caller may name fewer tenants, and may not name one the definition does not hold."""
    script = seeded_script(rows=(stored_row(FIRST_DOMAIN, FIRST_ID),))

    report = reset_corpus(build_store(script), slugs=(FIRST_DOMAIN.slug,))

    assert report.client_slugs == (FIRST_DOMAIN.slug,)
    assert script.parameters_of(SELECT_SEEDED_CLIENTS_STATEMENT) == ([FIRST_DOMAIN.slug],)
    with pytest.raises(KeyError, match="corpus definition"):
        reset_corpus(build_store(seeded_script()), slugs=("a-tenant-the-seed-never-invented",))


def test_no_value_from_a_stored_row_reaches_statement_text() -> None:
    """A hostile display name is compared rather than interpolated, and is refused."""
    script = seeded_script(
        rows=(stored_row(FIRST_DOMAIN, FIRST_ID, display_name=HOSTILE_TEXT),),
    )

    with pytest.raises(ResetRefusedError):
        reset_corpus(build_store(script), slugs=(FIRST_DOMAIN.slug,))

    for statement in script.statements:
        assert HOSTILE_TEXT not in statement


# ---------------------------------------------------------------------------
# The order the surviving reference dictates
# ---------------------------------------------------------------------------


def test_the_events_are_removed_before_the_sessions() -> None:
    """`ledger.session_id` survived migration 017, so no Session goes before its Events."""
    script = seeded_script()

    reset_corpus(build_store(script))

    sent = issued(script)
    assert sent.index(DELETE_EVENTS_STATEMENT) < sent.index(DELETE_SESSIONS_STATEMENT)


def test_the_statements_are_issued_in_the_declared_order() -> None:
    """The order is a value the module holds, and the run follows it exactly."""
    script = seeded_script()

    reset_corpus(build_store(script))

    assert issued(script) == [statement for _, statement, _ in RESET_ORDER]


@pytest.mark.parametrize(
    ("reader", "table"),
    [
        (DELETE_EDGES_STATEMENT, "derived_artifact"),
        (DELETE_EDGES_STATEMENT, "ledger"),
        (DELETE_EDGES_STATEMENT, "session"),
        (DELETE_BINDINGS_STATEMENT, "ledger"),
        (DELETE_BINDINGS_STATEMENT, "derived_artifact"),
    ],
)
def test_a_statement_reading_a_table_is_issued_before_that_table_is_emptied(
    reader: str, table: str
) -> None:
    """An edge and a binding are recognised from rows that must still exist to be read."""
    script = seeded_script()

    reset_corpus(build_store(script))

    sent = issued(script)
    emptying = {name: statement for name, statement, _ in RESET_ORDER}[table]
    assert sent.index(reader) < sent.index(emptying)


def test_the_whole_delete_commits_as_one_transaction() -> None:
    """Seven statements, one serializable transaction, so half a corpus is never left."""
    script = seeded_script()

    reset_corpus(build_store(script))

    statements = script.statements
    assert statements.count(SERIALIZABLE_STATEMENT) == 1
    first = statements.index(RESET_ORDER[0][1])
    last = statements.index(RESET_ORDER[-1][1])
    assert statements.index(SERIALIZABLE_STATEMENT) < first
    assert last < statements.index(COMMIT_STATEMENT)
    assert statements.index(SELECT_SEEDED_CLIENTS_STATEMENT) < statements.index(
        SERIALIZABLE_STATEMENT
    ), "the tenants are resolved before the transaction opens"


# ---------------------------------------------------------------------------
# The counts, as the statements reported them
# ---------------------------------------------------------------------------


def test_each_delete_carries_its_own_aggregate_count() -> None:
    """One deletion and one count inside one statement, with no row identifier bound."""
    for _, statement, _ in RESET_ORDER:
        assert statement.startswith("WITH removed AS (")
        assert statement.endswith("SELECT count(*) FROM removed")
        assert statement.count("RETURNING 1") == 1
        assert "LIMIT" not in statement.upper()


def test_the_report_carries_the_count_each_statement_answered() -> None:
    """Every reported number is the cluster's report of what that statement removed."""
    script = seeded_script()

    report = reset_corpus(build_store(script))

    assert report.counts == SCRIPTED_COUNTS
    assert report.total == sum(SCRIPTED_COUNTS.values())
    assert report.client_slugs == (FIRST_DOMAIN.slug, SECOND_DOMAIN.slug)


def test_the_report_renders_one_line_per_table_in_the_issued_order() -> None:
    """An operator reads a count per table rather than a claim that it worked."""
    script = seeded_script()

    report = reset_corpus(build_store(script))

    lines = report.lines()
    assert len(lines) == len(RESET_TABLES)
    for line, table in zip(lines, RESET_TABLES, strict=True):
        assert table in line
        assert str(SCRIPTED_COUNTS[table]) in line


def test_the_document_reports_the_tenants_and_the_counts() -> None:
    """The machine-readable object carries the same numbers the narration does."""
    script = seeded_script()

    document = reset_corpus(build_store(script)).as_document()

    assert document["clients"] == [FIRST_DOMAIN.slug, SECOND_DOMAIN.slug]
    assert document["rows_removed"] == sum(SCRIPTED_COUNTS.values())
    assert document["per_table"] == SCRIPTED_COUNTS


def test_a_delete_reporting_no_count_is_refused() -> None:
    """A number nobody read cannot be reported as a number of removed rows."""
    script = Script(
        answers=[
            Answer(
                LOOKUP_FRAGMENT,
                (stored_row(FIRST_DOMAIN, FIRST_ID),),
            ),
            Answer(WORKING_FRAGMENT, ()),
        ]
    )

    with pytest.raises(StoreError, match="no count"):
        reset_corpus(build_store(script), slugs=(FIRST_DOMAIN.slug,))


def test_a_tenant_row_of_the_wrong_width_is_refused() -> None:
    """The lookup and its decoder cannot drift apart silently."""
    script = Script(answers=[Answer(LOOKUP_FRAGMENT, ((FIRST_ID, FIRST_DOMAIN.slug),))])

    with pytest.raises(StoreError, match="width"):
        reset_corpus(build_store(script))


def test_a_removal_count_cannot_be_negative_or_nameless() -> None:
    """A reported removal names a table and counts rows, or it is not a removal."""
    with pytest.raises(ValueError, match="table"):
        TableRemoval(table="", removed=0)
    with pytest.raises(ValueError, match="negative"):
        TableRemoval(table="ledger", removed=-1)


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_a_cluster_holding_none_of_the_tenants_is_sent_no_delete() -> None:
    """An empty corpus reports zero everywhere and is not written to at all."""
    script = empty_script()

    report = reset_corpus(build_store(script))

    assert report.client_slugs == ()
    assert report.total == 0
    assert report.counts == dict.fromkeys(RESET_TABLES, 0)
    assert issued(script) == []
    assert SERIALIZABLE_STATEMENT not in script.statements


def test_tenants_holding_nothing_report_zero() -> None:
    """A second reset over an already-reset corpus succeeds and removes nothing."""
    script = zeroed_script()

    report = reset_corpus(build_store(script))

    assert report.total == 0
    assert report.counts == dict.fromkeys(RESET_TABLES, 0)
    assert issued(script) == [statement for _, statement, _ in RESET_ORDER]


# ---------------------------------------------------------------------------
# The guard: a slug alone authorises nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        stored_row(FIRST_DOMAIN, FIRST_ID, display_name=FOREIGN_NAME),
        stored_row(FIRST_DOMAIN, FIRST_ID, jurisdiction="somewhere-else"),
        stored_row(FIRST_DOMAIN, FIRST_ID, markers=()),
        stored_row(FIRST_DOMAIN, FIRST_ID, markers=("a-marker-the-seed-never-wrote",)),
    ],
)
def test_a_tenant_that_is_not_the_seeds_is_refused_before_anything_is_deleted(
    row: tuple[object, ...],
) -> None:
    """A real tenant sitting on a seeded slug ends the reset with every row still there."""
    script = seeded_script(rows=(row,))

    with pytest.raises(ResetRefusedError, match="nothing was removed"):
        reset_corpus(build_store(script), slugs=(FIRST_DOMAIN.slug,))

    assert issued(script) == []
    assert SERIALIZABLE_STATEMENT not in script.statements


def test_one_unrecognised_tenant_refuses_the_whole_reset() -> None:
    """The refusal is not per tenant: a cluster with one such row is not a seeded corpus."""
    script = seeded_script(
        rows=(
            stored_row(FIRST_DOMAIN, FIRST_ID),
            stored_row(SECOND_DOMAIN, SECOND_ID, display_name=FOREIGN_NAME),
        ),
    )

    with pytest.raises(ResetRefusedError):
        reset_corpus(build_store(script))

    assert issued(script) == []


def test_a_matching_tenant_is_recognised_by_its_whole_declared_identity() -> None:
    """The check is the definition's, read from it rather than restated here."""
    script = seeded_script()

    reset_corpus(build_store(script))

    bound = script.parameters_of(SELECT_SEEDED_CLIENTS_STATEMENT)
    assert bound is not None
    assert "display_name" in SELECT_SEEDED_CLIENTS_STATEMENT
    assert "jurisdiction" in SELECT_SEEDED_CLIENTS_STATEMENT
    assert "content_markers" in SELECT_SEEDED_CLIENTS_STATEMENT


# ---------------------------------------------------------------------------
# Containment: the tenant rows and the evidence survive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("statement", ALL_STATEMENTS)
def test_no_statement_removes_a_tenant_row(statement: str) -> None:
    """A reset empties a seeded tenant; it never removes one."""
    assert TENANT_TABLE in TENANT_REMOVAL_FRAGMENT, "the fragment names the tenant table"
    assert TENANT_REMOVAL_FRAGMENT not in statement
    assert not statement.endswith(TENANT_REMOVAL_TAIL)


@pytest.mark.parametrize("statement", ALL_STATEMENTS)
def test_no_statement_names_an_evidence_table(statement: str) -> None:
    """Nothing here reaches the record of an erasure or a checkpoint over the ledger."""
    for table in EVIDENCE_TABLES:
        assert table not in statement, f"{table} is reachable from the seed reset"


def test_the_reset_names_the_seven_tables_the_seeder_writes() -> None:
    """The tables are the ones the generation and the planting write, and no others."""
    assert RESET_TABLES == (
        "working_memory",
        "embedding",
        "lineage_edge",
        "client_binding",
        "derived_artifact",
        "ledger",
        "session",
    )
