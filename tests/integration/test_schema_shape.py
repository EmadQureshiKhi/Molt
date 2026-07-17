"""The shape the first migration generation leaves on a live instance.

These assertions are introspection rather than behaviour: they ask the cluster
what it holds and compare that against what migrations 001 through 007 declare.
That is the point of the suite. A constraint that exists in a file and not in the
catalog enforces nothing, and a schema fact stated only in a design document is a
claim rather than a guarantee.

Three choices shape the module.

**Only the first generation is applied.** The files for it are staged into a
directory of their own and the runner is pointed at that directory, so the schema
under test is exactly what the first generation produces. This is not tidiness:
the second generation deliberately removes two of the objects asserted here, the
total uniqueness of the attribution pair and the sixteen-value form of the ledger
category constraint, replacing each with a shape suited to an immutable version
history. A test that applied every file present could therefore not assert what
the first generation created, and would additionally change its own meaning every
time a later file landed beside it.

**Presence is asserted, absence almost never is.** Every set comparison below is
a containment rather than an equality, so an object a later generation adds
breaks nothing here. The one place absence carries meaning is the privilege
suite, which is a separate module.

**The capability row is not asserted.** Migration 003 creates the vector index as
a statement permitted to fail and creates the table that records which platform
facts were probed, but it writes no row into that table: a permitted statement is
applied after the migration body has committed, so no statement in the body can
observe its outcome. The caller that reads the reported outcome inserts that row
instead. What is asserted here is the index, the operator class the platform
reports for it, the reported outcome itself, and the existence of the table the
row will land in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from molt.store.migrate import MigrationReport, apply_migrations, discover_migrations

pytestmark = pytest.mark.integration

# The last version of the first migration generation. Everything above it belongs
# to the second generation and is deliberately excluded from the staged set.
FIRST_GENERATION_LAST_VERSION: Final[int] = 7

# The versions the first generation is made of, in application order.
FIRST_GENERATION_VERSIONS: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 6, 7)

# The tenant row the first migration reserves, named by a fixed identifier so the
# capture side can fall back to it without a lookup.
RESERVED_CLIENT_ID: Final[str] = "00000000-0000-4000-8000-000000000000"
RESERVED_CLIENT_SLUG: Final[str] = "unassigned"

# The width the vector column fixes and the check restates.
VECTOR_WIDTH: Final[int] = 1024

# The label migration 003 reports the vector index statement under.
VECTOR_INDEX_LABEL: Final[str] = "vector_index"
VECTOR_INDEX_NAME: Final[str] = "embedding_vec_idx"

# How the platform names the vector index and the distance ordering it serves.
# The operator class is read back from the cluster rather than assumed, because
# which ordering the index serves is what makes unit normalisation at write time
# load-bearing rather than defensive.
VECTOR_INDEX_REPORTED: Final[str] = "VECTOR INDEX embedding_vec_idx (vec vector_l2_ops)"

# Tables whose key is a single UUID column named id.
UUID_KEYED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "client",
        "session",
        "ledger",
        "derived_artifact",
        "lineage_edge",
        "client_binding",
        "embedding",
        "erasure_request",
        "erasure_run",
        "residue_candidate",
        "disposition",
        "backup_record",
        "erasure_certificate",
        "audit_log_snapshot",
        "policy_rule",
        "policy_match",
        "approval_queue",
        "watcher_watermark",
    }
)

# Tables whose key is a composite of UUID columns rather than a single one.
COMPOSITE_UUID_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "erasure_candidate": ("run_id", "artifact_id"),
    "run_session": ("run_id", "session_id"),
}

# The two tables deliberately keyed by something other than a UUID, recorded here
# so the exception is a decision rather than an oversight. The runner's own
# history is keyed by the version it applies, and the probed-capability table is
# keyed by the name of the fact it records.
NON_UUID_KEYS: Final[dict[str, tuple[str, str]]] = {
    "schema_migration": ("version", "bigint"),
    "capability": ("name", "text"),
}

# Payload columns the design declares as the native document type.
JSONB_COLUMNS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("session", "attribution"),
        ("ledger", "payload"),
        ("policy_match", "detail"),
        ("erasure_certificate", "payload"),
        ("audit_log_snapshot", "records"),
    }
)

# The sixteen event categories the first generation admits. The constraint is
# replaced by a later migration that adds a seventeenth, so each of these is
# asserted present and none is asserted to be the last.
FIRST_GENERATION_CATEGORIES: Final[tuple[str, ...]] = (
    "session_start",
    "session_end",
    "user_prompt",
    "assistant_response",
    "tool_call",
    "tool_result",
    "model_request",
    "model_response",
    "file_read",
    "file_write",
    "shell_command",
    "decision",
    "error",
    "cost_record",
    "recall",
    "policy_halt",
)

# Every index the first generation names, by the table it sits on. Primary keys
# and the indexes that back a uniqueness constraint are asserted through the
# constraint set instead, so they are absent here.
NAMED_INDEXES: Final[dict[str, frozenset[str]]] = {
    "session": frozenset(
        {"session_by_client", "session_by_parent", "session_by_machine", "session_halted"}
    ),
    "ledger": frozenset(
        {
            "ledger_by_session_seq",
            "ledger_by_client_time",
            "ledger_by_recorded",
            "ledger_pending_embedding",
            "ledger_by_parent",
        }
    ),
    "derived_artifact": frozenset(
        {"derived_by_client", "derived_by_kind", "derived_pending_embedding"}
    ),
    "lineage_edge": frozenset({"lineage_by_parent", "lineage_by_child"}),
    "client_binding": frozenset({"binding_by_client", "binding_by_artifact"}),
    "embedding": frozenset({"embedding_by_client", "embedding_by_artifact", VECTOR_INDEX_NAME}),
    "erasure_run": frozenset({"run_active_by_client"}),
    "residue_candidate": frozenset({"residue_by_run"}),
    "disposition": frozenset({"disposition_by_run"}),
    "policy_match": frozenset({"match_by_session"}),
    "approval_queue": frozenset({"approval_pending"}),
}

# Every constraint the first generation names, by the table it guards. The
# generated non-null constraints the platform records for each declared column
# are not named by any migration and so are not listed.
NAMED_CONSTRAINTS: Final[dict[str, frozenset[str]]] = {
    "client": frozenset({"client_slug_unique", "client_retention_positive"}),
    "session": frozenset(
        {
            "session_outcome_known",
            "session_depth_non_negative",
            "session_root_depth",
            "session_spawning_event_fk",
        }
    ),
    "ledger": frozenset(
        {
            "ledger_category_known",
            "ledger_embedding_state_known",
            "ledger_seq_positive",
            "ledger_digest_hex",
            "ledger_seq_unique_in_session",
            "ledger_one_successor_per_predecessor",
        }
    ),
    "derived_artifact": frozenset(
        {
            "derived_kind_known",
            "derived_embedding_state_known",
            "derived_digest_hex",
            "derived_revision_positive",
        }
    ),
    "lineage_edge": frozenset(
        {"lineage_parent_kind_known", "lineage_no_self_edge", "lineage_edge_unique"}
    ),
    "client_binding": frozenset(
        {
            "binding_kind_known",
            "binding_method_known",
            "binding_confidence_range",
            "binding_unique_pair",
        }
    ),
    "embedding": frozenset(
        {"embedding_kind_known", "embedding_dimension_fixed", "embedding_unique_per_model"}
    ),
    "erasure_request": frozenset({"request_status_known"}),
    "erasure_run": frozenset({"run_status_known", "run_phase_known", "run_thresholds_ordered"}),
    "erasure_candidate": frozenset({"candidate_pk", "candidate_reason_known"}),
    "residue_candidate": frozenset(
        {"residue_band_known", "residue_classification_known", "residue_unique_per_run"}
    ),
    "disposition": frozenset({"disposition_known", "disposition_unique_per_run"}),
    "run_session": frozenset({"run_session_pk"}),
    "backup_record": frozenset(
        {
            "backup_status_known",
            "backup_path_known",
            "backup_flags_exclusive",
            "backup_flag_matches_path",
        }
    ),
    "erasure_certificate": frozenset(
        {"certificate_storage_status_known", "certificate_unique_per_run"}
    ),
    "policy_rule": frozenset(
        {"rule_name_unique", "rule_match_kind_known", "rule_action_known", "rule_shape_valid"}
    ),
    "policy_match": frozenset({"match_unique"}),
    "approval_queue": frozenset(
        {"approval_status_known", "approval_decision_known", "approval_unique"}
    ),
    "watcher_watermark": frozenset({"watermark_mode_known"}),
}

# The columns the provider-spanning uniqueness constraint covers, in order. The
# provider belongs in the span because a corpus re-embedded under a second
# provider must sit beside the first rather than collide with it.
EMBEDDING_UNIQUE_SPAN: Final[tuple[str, ...]] = (
    "artifact_id",
    "artifact_kind",
    "provider",
    "model_id",
)

# The kinds the artifact reference view spans, and the one it must not, because
# an embedding is a representation of an artifact rather than something derived
# from one and so may never become a lineage parent.
VIEW_SPANNED_TABLES: Final[tuple[str, ...]] = ("ledger", "session", "derived_artifact")
VIEW_EXCLUDED_TABLE: Final[str] = "embedding"

# A table reference inside a reported view definition, matched so that the name
# can be compared as an identifier rather than as a substring. The platform
# reports the definition with every name qualified by the database and the
# schema it resolved in, so a substring test over the raw text answers for the
# names of those enclosing objects as readily as for the table: it would deny a
# view that reads no embedding whenever the database is called something like
# molt_embeddings, and admit a view that reads no session whenever it is called
# something like molt_sessions. Only the final component of each reference is
# taken, which leaves the comparison independent of where the schema lives.
_QUALIFIED_IDENTIFIER: Final[str] = r'"?[A-Za-z_][A-Za-z0-9_$]*"?'
_TABLE_REFERENCE: Final[re.Pattern[str]] = re.compile(
    rf"\b(?:FROM|JOIN)\s+(?:{_QUALIFIED_IDENTIFIER}\.)*({_QUALIFIED_IDENTIFIER})",
    re.IGNORECASE,
)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which is what keeps this module collectable with no
# driver installed.
Connection = Any


@dataclass(frozen=True, slots=True)
class AppliedSchema:
    """A schema holding exactly the first generation, and what applying it said."""

    connection: Connection
    schema: str
    report: MigrationReport


def stage_first_generation(destination: Path) -> tuple[int, ...]:
    """Copy the first-generation migration files into a directory of their own.

    The bytes are copied rather than the text, so each staged file digests to
    exactly what the original digests to and the runner's history records the
    same value it would have recorded from the source tree.
    """
    staged: list[int] = []
    for migration in discover_migrations():
        if migration.version > FIRST_GENERATION_LAST_VERSION:
            continue
        destination.joinpath(migration.path.name).write_bytes(migration.path.read_bytes())
        staged.append(migration.version)
    return tuple(staged)


def rows(connection: Connection, query: str, params: tuple[object, ...]) -> list[tuple[Any, ...]]:
    """Send one parameterised query and return every row it produced."""
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return list(cursor.fetchall())


def referenced_tables(definition: str) -> frozenset[str]:
    """The unqualified names of the tables a reported view definition reads.

    Each reference is reduced to its final identifier, so a view reading a table
    is distinguishable from a view whose enclosing database or schema merely
    happens to be named after one.
    """
    return frozenset(
        match.group(1).strip('"').lower() for match in _TABLE_REFERENCE.finditer(definition)
    )


@pytest.fixture(scope="module")
def applied(fresh_schema: Connection, tmp_path_factory: pytest.TempPathFactory) -> AppliedSchema:
    """Apply the first generation into this module's own schema, once."""
    directory = tmp_path_factory.mktemp("molt_first_generation")
    staged = stage_first_generation(directory)
    assert staged == FIRST_GENERATION_VERSIONS, (
        f"the first generation should stage as seven consecutive files, not {staged}"
    )
    report = apply_migrations(fresh_schema, directory=directory)
    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        schema = str(cursor.fetchall()[0][0])
    return AppliedSchema(connection=fresh_schema, schema=schema, report=report)


def test_first_generation_applies_in_order(applied: AppliedSchema) -> None:
    """Every first-generation file applies, in ascending version order."""
    assert applied.report.applied_versions == FIRST_GENERATION_VERSIONS
    assert applied.report.skipped_versions == ()
    assert applied.report.changed_state is True


def test_keys_are_uuid(applied: AppliedSchema) -> None:
    """Every content table is keyed by UUID, and the two exceptions are recorded."""
    found = {
        (str(table), str(column), str(kind))
        for table, column, kind in rows(
            applied.connection,
            "SELECT c.table_name, c.column_name, c.data_type "
            "FROM information_schema.columns AS c "
            "JOIN information_schema.key_column_usage AS k "
            "ON k.table_schema = c.table_schema AND k.table_name = c.table_name "
            "AND k.column_name = c.column_name "
            "JOIN information_schema.table_constraints AS t "
            "ON t.table_schema = k.table_schema AND t.constraint_name = k.constraint_name "
            "WHERE c.table_schema = %s AND t.constraint_type = 'PRIMARY KEY'",
            (applied.schema,),
        )
    }

    for table in sorted(UUID_KEYED_TABLES):
        assert (table, "id", "uuid") in found, f"{table} should be keyed by a UUID id column"

    for table, columns in sorted(COMPOSITE_UUID_KEYS.items()):
        for column in columns:
            assert (table, column, "uuid") in found, (
                f"the composite key of {table} should carry {column} as a UUID"
            )

    for table, (column, kind) in sorted(NON_UUID_KEYS.items()):
        assert (table, column, kind) in found, (
            f"{table} is deliberately keyed by {column}, not by a UUID"
        )


def test_ledger_carries_the_tenant_from_the_first_migration(applied: AppliedSchema) -> None:
    """The ledger holds a non-null tenant column and a reference for it.

    The column is denormalised from the session row on purpose: the explicit
    erasure sweep, the per-tenant index, and the tenancy filter on every read all
    need it without a join. Carrying it from the first migration is what leaves no
    window in which the ledger cannot answer whose data a row holds.
    """
    column = rows(
        applied.connection,
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'ledger' AND column_name = 'client_id'",
        (applied.schema,),
    )
    assert column == [("uuid", "NO")]

    referenced = rows(
        applied.connection,
        "SELECT count(*) FROM information_schema.table_constraints "
        "WHERE table_schema = %s AND table_name = 'ledger' "
        "AND constraint_type = 'FOREIGN KEY' AND constraint_name = 'ledger_client_id_fkey'",
        (applied.schema,),
    )
    assert referenced[0][0] == 1

    reserved = rows(
        applied.connection,
        "SELECT slug FROM client WHERE id = %s",
        (RESERVED_CLIENT_ID,),
    )
    assert [(str(row[0]),) for row in reserved] == [(RESERVED_CLIENT_SLUG,)]


def test_native_column_types(applied: AppliedSchema) -> None:
    """Timestamps carry a zone, payloads are documents, and the vector is fixed."""
    naive = rows(
        applied.connection,
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        r"WHERE table_schema = %s AND column_name LIKE '%%\_at' "
        "AND data_type != 'timestamp with time zone'",
        (applied.schema,),
    )
    assert naive == [], f"every instant column should carry a zone; these do not: {naive}"

    documents = {
        (str(table), str(column))
        for table, column in rows(
            applied.connection,
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND data_type = 'jsonb'",
            (applied.schema,),
        )
    }
    assert documents >= JSONB_COLUMNS

    vector = rows(
        applied.connection,
        "SELECT data_type, crdb_sql_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'embedding' AND column_name = 'vec'",
        (applied.schema,),
    )
    assert vector == [("vector", f"VECTOR({VECTOR_WIDTH})", "NO")]


def test_embedding_provider_column_and_uniqueness_span(applied: AppliedSchema) -> None:
    """A provider sits beside the model, and uniqueness spans both."""
    provider = rows(
        applied.connection,
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'embedding' AND column_name = 'provider'",
        (applied.schema,),
    )
    assert provider == [("text", "NO")]

    span = tuple(
        str(column)
        for (column,) in rows(
            applied.connection,
            "SELECT column_name FROM information_schema.key_column_usage "
            "WHERE table_schema = %s AND table_name = 'embedding' "
            "AND constraint_name = 'embedding_unique_per_model' ORDER BY ordinal_position",
            (applied.schema,),
        )
    )
    assert span == EMBEDDING_UNIQUE_SPAN

    width = rows(
        applied.connection,
        "SELECT cc.check_clause FROM information_schema.check_constraints AS cc "
        "WHERE cc.constraint_schema = %s AND cc.constraint_name = 'embedding_dimension_fixed'",
        (applied.schema,),
    )
    assert str(VECTOR_WIDTH) in str(width[0][0])


def test_vector_index_present_with_the_reported_operator_class(applied: AppliedSchema) -> None:
    """The index exists, the platform names its ordering, and the record table waits.

    Migration 003 creates the index as a statement permitted to fail, so its
    outcome is reported rather than raised. On this platform it succeeds, and the
    reported operator class is what makes unit normalisation at write time
    load-bearing: the index orders by squared distance while the thresholds are
    expressed in cosine space, and those two orderings agree only on unit vectors.
    """
    outcome = applied.report.permitted_outcome(VECTOR_INDEX_LABEL)
    assert outcome is not None, "migration 003 should report the vector index statement"
    assert outcome.succeeded, f"the vector index statement was rejected: {outcome.detail}"

    indexed = rows(
        applied.connection,
        "SELECT column_name FROM information_schema.statistics "
        "WHERE table_schema = %s AND table_name = 'embedding' AND index_name = %s "
        "AND implicit = 'NO'",
        (applied.schema, VECTOR_INDEX_NAME),
    )
    assert [(str(row[0]),) for row in indexed] == [("vec",)]

    with applied.connection.cursor() as cursor:
        cursor.execute("SHOW CREATE TABLE embedding")
        definition = str(cursor.fetchall()[0][1])
    assert VECTOR_INDEX_REPORTED in definition, (
        "the platform should report the vector index with its operator class"
    )

    # The table that records probed platform facts exists here, and the caller
    # that reads the reported outcome above inserts the row for the vector index
    # into it, because a permitted statement is applied after the migration body
    # has committed and so no statement in the body can observe its result.
    recorded = rows(
        applied.connection,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = 'capability'",
        (applied.schema,),
    )
    assert recorded[0][0] == 1


def test_every_named_index_is_present(applied: AppliedSchema) -> None:
    """Each index the first generation names exists on the table it names it for."""
    found: dict[str, set[str]] = {}
    for table, index in rows(
        applied.connection,
        "SELECT table_name, index_name FROM information_schema.statistics WHERE table_schema = %s",
        (applied.schema,),
    ):
        found.setdefault(str(table), set()).add(str(index))

    for table, expected in sorted(NAMED_INDEXES.items()):
        present = found.get(table, set())
        assert expected <= present, f"{table} is missing {sorted(expected - present)}"


def test_every_named_constraint_is_present(applied: AppliedSchema) -> None:
    """Each constraint the first generation names exists on the table it guards."""
    found: dict[str, set[str]] = {}
    for table, constraint in rows(
        applied.connection,
        "SELECT table_name, constraint_name FROM information_schema.table_constraints "
        "WHERE table_schema = %s",
        (applied.schema,),
    ):
        found.setdefault(str(table), set()).add(str(constraint))

    for table, expected in sorted(NAMED_CONSTRAINTS.items()):
        present = found.get(table, set())
        assert expected <= present, f"{table} is missing {sorted(expected - present)}"


def test_ledger_category_constraint_admits_the_first_generation_set(
    applied: AppliedSchema,
) -> None:
    """The category constraint names each of the sixteen categories.

    A later migration replaces this constraint with one that adds a seventeenth
    category, so each of the sixteen is asserted present and none is asserted to
    be the last.
    """
    clause = str(
        rows(
            applied.connection,
            "SELECT check_clause FROM information_schema.check_constraints "
            "WHERE constraint_schema = %s AND constraint_name = 'ledger_category_known'",
            (applied.schema,),
        )[0][0]
    )
    missing = [category for category in FIRST_GENERATION_CATEGORIES if category not in clause]
    assert missing == [], f"the category constraint does not admit {missing}"


def test_artifact_reference_view_spans_three_kinds(applied: AppliedSchema) -> None:
    """The view offers the three kinds a lineage parent may be, and no fourth.

    The omission is the enforcement rather than a documentation choice: an
    inserting statement proves a parent exists by joining this view, and a kind
    the view does not return cannot pass that join.

    The tables the definition names are compared as identifiers rather than as
    substrings of the reported text, because the platform qualifies each name
    with the database and schema it resolved in and so the raw text carries the
    names of those enclosing objects too.
    """
    definition = rows(
        applied.connection,
        "SELECT view_definition FROM information_schema.views "
        "WHERE table_schema = %s AND table_name = 'artifact_ref'",
        (applied.schema,),
    )
    assert len(definition) == 1, "the artifact reference view should exist"
    read = referenced_tables(str(definition[0][0]))
    for table in VIEW_SPANNED_TABLES:
        assert table in read, f"the view should read {table}, and it reads {sorted(read)}"
    assert VIEW_EXCLUDED_TABLE not in read, (
        "the view must not span embeddings, so one cannot become a lineage parent"
    )
