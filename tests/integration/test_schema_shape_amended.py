"""The shape the second migration generation leaves on a live instance.

The first-generation module asserts what migrations 001 through 007 declare. This
one asserts what 008 through 014 change, and the two are separate modules for the
same reason the generations are separate files: the second generation deliberately
removes objects the first one created, so a single module could not assert both
states at once.

Every assertion here is introspection. A constraint that exists in a migration
file and not in the catalog enforces nothing, and a storage parameter stated in a
design document is a claim rather than a guarantee.

Three choices shape the module.

**Every migration is applied.** The runner is pointed at the source directory
rather than at a staged subset, because the subject is the schema as it stands
once the whole second generation has landed. Two first-generation objects are
consequently asserted absent: the total uniqueness of the attribution pair, which
a version history cannot satisfy, and nothing else. Absence is asserted only where
a later generation is what removed the object.

**The expiry configuration is read back off the committed descriptor.** This is
the one read-back the module cannot do without, and the failure mode it guards is
worse than a refusal. Configuring row-level expiry on a table created earlier in
the same transaction is not rejected: the statement reports success, the
transaction commits, and the storage parameters are simply absent from the
committed descriptor afterwards. A reader who checked only for an error would
conclude the working tier expires its rows while the cluster sweeps nothing, which
is the one failure this tier cannot tolerate quietly. So the expiration
expression, the job recurrence, and the delete batch size are each read back from
what the cluster reports for the table.

**The confidence equivalence is asserted by writing, not by reading the clause.**
A check clause containing the right text and a check clause that refuses the wrong
rows are different claims. Both halves of the equivalence are attempted here: a
summary carrying a standing value, and a learned procedure carrying none. Each is
refused, and a learned procedure carrying a value is accepted, so the constraint is
shown to be selective rather than a blanket refusal.

**Validates: Requirements 14.7, 14.8, 36.2, 42.9, 42.12, 44.2, 45.9, 45.10, 46.5,
49.1, 49.14**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final

import pytest

from molt.store.migrate import MigrationReport, apply_migrations

pytestmark = pytest.mark.integration

# The versions the second generation is made of, in application order. Several of them
# create no table of their own and are amendments to what an earlier generation laid
# down: one adds columns to an existing table, one removes three references an
# authorised erasure has to be able to cut, and the rest grant a role a privilege an
# earlier grant list omitted.
#
# Those grant amendments all have one cause. The suite applies migrations under an
# administrative login, and an administrative login holds every privilege there is, so a
# missing grant is invisible here and becomes load-bearing only where a path runs as the
# narrow role its privileges were written for. Each was found that way: a reader view
# that could not count, a watcher that died before its loop on the capability write its
# start-up probe performs, an erasure that stopped in its disposition phase on the
# procedural reads that weigh a Learned_Procedure, an erasure sweep that removed a
# Session's Events and then could not remove the Session, a change stream that was refused
# for want of a read on one of the two tables it names, and a delete refused while its
# cascade expression was built because the deleting role could not delete a table three
# references away.
SECOND_GENERATION_VERSIONS: Final[tuple[int, ...]] = (
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
)

# The tenant row the first migration reserves, used as the owner of every row the
# behavioural assertions below write.
RESERVED_CLIENT_ID: Final[str] = "00000000-0000-4000-8000-000000000000"

# The working-tier interval the migration defaults the expiry column to. Asserted
# by arithmetic against the stored row rather than against the reported default
# expression, because the platform normalises the interval it was given.
WORKING_TTL_SECONDS: Final[int] = 3600

# The three storage parameters the working tier's expiry rests on, as the platform
# reports them back. The recurrence is the one value that differs from the content
# tables: an hourly sweep against an hourly interval is what makes the tier's
# disposability a property of the cluster rather than a claim in a document.
WORKING_TTL_PARAMETERS: Final[tuple[str, ...]] = (
    "ttl = 'on'",
    "ttl_expiration_expression = 'expires_at'",
    "ttl_job_cron = '@hourly'",
    "ttl_delete_batch_size = 500",
)

# The event category the attribution history adds, so that no supersession is
# silent.
SUPERSESSION_CATEGORY: Final[str] = "attribution_superseded"

# The columns the attribution history adds, with the type and nullability each
# carries. The validity start is not nullable because every version is valid from
# some instant; the closure pair is nullable because a current version carries
# neither half of it.
HISTORY_COLUMNS: Final[dict[str, tuple[str, str]]] = {
    "valid_from": ("timestamp with time zone", "NO"),
    "valid_to": ("timestamp with time zone", "YES"),
    "superseded_by": ("uuid", "YES"),
}

# The fencing columns, by the table each sits on. The generation on the evidence
# itself is what makes the ownership claim on a certificate checkable against the
# lease history rather than against the memory of the process that ran.
FENCING_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "erasure_run": frozenset(
        {
            "fencing_generation",
            "lease_id",
            "idempotency_key",
            "finalised_at",
            "finalisation_result",
            "working_rows_deleted",
        }
    ),
    "disposition": frozenset({"fencing_generation"}),
    "erasure_certificate": frozenset({"fencing_generation"}),
}

# Every constraint the second generation names, by the table it guards.
SECOND_GENERATION_CONSTRAINTS: Final[dict[str, frozenset[str]]] = {
    "client_binding": frozenset({"binding_closure_consistent", "binding_interval_ordered"}),
    "erasure_lease": frozenset(
        {
            "lease_generation_positive",
            "lease_expiry_after_acquisition",
            "lease_closure_consistent",
        }
    ),
    "ledger_checkpoint": frozenset(
        {
            "checkpoint_window_ordered",
            "checkpoint_digest_hex",
            "checkpoint_count_non_negative",
        }
    ),
    "checkpoint_session": frozenset({"checkpoint_session_pk"}),
    "working_memory": frozenset({"working_memory_pk"}),
    "derived_artifact": frozenset({"derived_confidence_range", "derived_confidence_kind"}),
    "procedure_outcome": frozenset({"outcome_known", "outcome_unique_per_session"}),
    "procedure_confidence_change": frozenset({"change_values_in_range", "change_actually_changed"}),
}

# Every index the second generation names, by the table it sits on.
SECOND_GENERATION_INDEXES: Final[dict[str, frozenset[str]]] = {
    "client_binding": frozenset({"binding_current_unique", "binding_as_of"}),
    "erasure_lease": frozenset(
        {"lease_history_by_client", "lease_current_unique", "lease_idempotency_unique"}
    ),
    "erasure_run": frozenset({"run_idempotency_unique"}),
    "ledger_checkpoint": frozenset({"checkpoint_by_window_end"}),
    "working_memory": frozenset({"working_by_client"}),
    "derived_artifact": frozenset({"derived_procedure_confidence"}),
    "procedure_retrieval": frozenset({"retrieval_by_procedure", "retrieval_by_session"}),
    "procedure_outcome": frozenset({"outcome_by_procedure"}),
    "procedure_confidence_change": frozenset({"change_by_procedure"}),
}

# The three tables the procedural tier adds, and the standing column on the
# artifact table they all describe.
PROCEDURE_TABLES: Final[tuple[str, ...]] = (
    "procedure_retrieval",
    "procedure_outcome",
    "procedure_confidence_change",
)
STANDING_COLUMN: Final[str] = "procedure_confidence"

# The two tables whose whole value is that they stay checkable after the rows they
# commit to have gone, so neither may expire.
CHECKPOINT_TABLES: Final[tuple[str, ...]] = ("ledger_checkpoint", "checkpoint_session")

# The uniqueness the first generation declared over the attribution pair, removed
# because a version history necessarily holds many rows per pair.
TOTAL_PAIR_UNIQUENESS: Final[str] = "binding_unique_pair"

# The partial uniqueness that replaces it, and the predicate that makes the
# history accumulate beside a single current claim.
CURRENT_UNIQUENESS: Final[str] = "binding_current_unique"
CURRENT_PREDICATE: Final[str] = "WHERE superseded_by IS NULL"

# The as-of index and the projection it stores, so the interval containment filter
# and the projection both read from the index and no row is fetched.
AS_OF_INDEX: Final[str] = "binding_as_of"
AS_OF_ORDER: Final[str] = "(artifact_id ASC, valid_from DESC, valid_to DESC)"
AS_OF_STORING: Final[str] = "STORING (client_id, method, confidence, superseded_by)"

# The state the platform refuses a row-level check violation under.
CHECK_VIOLATION: Final[str] = "23514"

# A hexadecimal digest of the width the artifact table's check demands, built here
# rather than written out so the module carries no digest-shaped literal.
DIGEST_WIDTH: Final[int] = 64

# The working table is deliberately absent from the artifact reference view: a
# kind that view does not return cannot become a lineage parent, cannot enter a
# candidate set, and cannot carry an attribution binding.
VIEW_EXCLUDED_TABLE: Final[str] = "working_memory"

_QUALIFIED_IDENTIFIER: Final[str] = r'"?[A-Za-z_][A-Za-z0-9_$]*"?'
_TABLE_REFERENCE: Final[re.Pattern[str]] = re.compile(
    rf"\b(?:FROM|JOIN)\s+(?:{_QUALIFIED_IDENTIFIER}\.)*({_QUALIFIED_IDENTIFIER})",
    re.IGNORECASE,
)

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which is what keeps this module collectable with no driver
# installed.
Connection = Any


@dataclass(frozen=True, slots=True)
class AppliedSchema:
    """A schema holding every migration, and what applying them said."""

    connection: Connection
    schema: str
    report: MigrationReport
    driver: ModuleType


def rows(
    connection: Connection,
    query: str,
    params: tuple[object, ...] = (),
) -> list[tuple[Any, ...]]:
    """Send one parameterised query and return every row it produced."""
    with connection.cursor() as cursor:
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return list(cursor.fetchall())


def reported_definition(applied: AppliedSchema, table: str) -> str:
    """What the cluster reports it holds for one table, as one line of text.

    The reported form is collapsed to single spaces so that an assertion about a
    stored projection or a storage parameter compares against the shape the
    platform names rather than against how it happened to wrap the text. The table
    is composed as an identifier rather than interpolated, which is the same rule
    the store layer follows for every statement it sends.
    """
    composer = applied.driver.sql
    statement = composer.SQL("SELECT create_statement FROM [SHOW CREATE TABLE {}]").format(
        composer.Identifier(table)
    )
    with applied.connection.cursor() as cursor:
        cursor.execute(statement)
        return " ".join(str(cursor.fetchall()[0][0]).split())


def named_objects(connection: Connection, schema: str, query: str) -> dict[str, set[str]]:
    """Group a two-column catalog read into a set of names per table."""
    grouped: dict[str, set[str]] = {}
    for table, name in rows(connection, query, (schema,)):
        grouped.setdefault(str(table), set()).add(str(name))
    return grouped


def columns_of(connection: Connection, schema: str, table: str) -> dict[str, tuple[str, str]]:
    """The type and nullability of every column of one table."""
    return {
        str(name): (str(kind), str(nullable))
        for name, kind, nullable in rows(
            connection,
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
    }


def check_clause(connection: Connection, schema: str, constraint: str) -> str:
    """The clause the cluster holds for one named check constraint."""
    found = rows(
        connection,
        "SELECT check_clause FROM information_schema.check_constraints "
        "WHERE constraint_schema = %s AND constraint_name = %s",
        (schema, constraint),
    )
    assert found, f"{constraint} should exist as a check constraint"
    return str(found[0][0])


def referenced_tables(definition: str) -> frozenset[str]:
    """The unqualified names of the tables a reported view definition reads."""
    return frozenset(
        match.group(1).strip('"').lower() for match in _TABLE_REFERENCE.finditer(definition)
    )


def refusal_state(error: BaseException) -> str:
    """The condition code a refusal carries, or an empty value when it carries none."""
    state = getattr(error, "sqlstate", None)
    return str(state) if state is not None else ""


@pytest.fixture(scope="module")
def applied(fresh_schema: Connection, database_driver: ModuleType) -> AppliedSchema:
    """Apply every migration into this module's own schema, once."""
    report = apply_migrations(fresh_schema)
    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        schema = str(cursor.fetchall()[0][0])
    return AppliedSchema(
        connection=fresh_schema,
        schema=schema,
        report=report,
        driver=database_driver,
    )


def test_second_generation_applies_in_order(applied: AppliedSchema) -> None:
    """Every second-generation file applies, in ascending version order."""
    applied_versions = applied.report.applied_versions
    tail = applied_versions[-len(SECOND_GENERATION_VERSIONS) :]
    assert tail == SECOND_GENERATION_VERSIONS, (
        f"the second generation should close the run, not {applied_versions}"
    )


def test_attribution_carries_a_bitemporal_version_history(applied: AppliedSchema) -> None:
    """The validity interval and the successor reference are columns of the table."""
    held = columns_of(applied.connection, applied.schema, "client_binding")
    for column, shape in sorted(HISTORY_COLUMNS.items()):
        assert held.get(column) == shape, f"client_binding.{column} should be {shape}"


def test_current_uniqueness_is_partial_and_the_total_pair_is_gone(
    applied: AppliedSchema,
) -> None:
    """Many versions per pair are admitted, and exactly one of them stays current.

    This is the shape the whole history turns on. A total constraint over the
    artifact and tenant pair would refuse the second version outright, so it is
    removed and a unique index restricted to the unsuperseded rows takes its
    place: closed versions accumulate without limit while the pair carries one
    unambiguous current claim, and both facts are the database's rather than the
    writing code's.
    """
    definition = reported_definition(applied, "client_binding")
    assert f"UNIQUE INDEX {CURRENT_UNIQUENESS} (artifact_id ASC, client_id ASC)" in definition
    assert f"{CURRENT_UNIQUENESS} (artifact_id ASC, client_id ASC) {CURRENT_PREDICATE}" in (
        definition
    ), "the current-version uniqueness should be restricted to the unsuperseded rows"

    assert TOTAL_PAIR_UNIQUENESS not in definition, (
        "the total uniqueness of the pair should be gone, since a history holds many"
    )
    constraints = rows(
        applied.connection,
        "SELECT constraint_name FROM information_schema.table_constraints "
        "WHERE table_schema = %s AND table_name = 'client_binding'",
        (applied.schema,),
    )
    assert TOTAL_PAIR_UNIQUENESS not in {str(row[0]) for row in constraints}


def test_the_as_of_index_stores_the_projection_it_answers_with(
    applied: AppliedSchema,
) -> None:
    """One artifact's versions in validity order, with the projection stored.

    The stored columns are what keep the as-of query a range scan alone rather
    than a range scan plus one row fetch per version, which is what holds its
    bound for an artifact carrying a long history.
    """
    definition = reported_definition(applied, "client_binding")
    assert f"INDEX {AS_OF_INDEX} {AS_OF_ORDER} {AS_OF_STORING}" in definition, (
        f"the as-of index should carry its stored projection; the cluster reports {definition}"
    )


def test_closure_is_total_and_the_validity_interval_is_ordered(
    applied: AppliedSchema,
) -> None:
    """A version is current in both closure columns or closed in both, never half.

    A half-closed version is a hole in the history: closed with no successor loses
    the thread of what replaced it, and a successor with no validity end leaves two
    rows claiming the same instant. An end preceding its start describes no
    interval at all, so the as-of containment predicate would return nothing for a
    instant inside the intended range.
    """
    closure = check_clause(applied.connection, applied.schema, "binding_closure_consistent")
    assert "valid_to" in closure and "superseded_by" in closure

    ordered = check_clause(applied.connection, applied.schema, "binding_interval_ordered")
    assert "valid_from" in ordered and "valid_to" in ordered


def test_the_ledger_admits_the_supersession_category(applied: AppliedSchema) -> None:
    """The category list is extended so no attribution change is silent."""
    clause = check_clause(applied.connection, applied.schema, "ledger_category_known")
    assert SUPERSESSION_CATEGORY in clause


def test_the_lease_carries_its_uniqueness_constraints(applied: AppliedSchema) -> None:
    """Current ownership is unique per tenant and a granting attempt is unique outright.

    The current-lease uniqueness is partial for the reason the attribution history
    is: prior generations stay resident as history while the constraint governs
    only the rows that are still current. The two idempotency indexes are what make
    a repeated finalisation collide with the recorded attempt rather than produce a
    second run.
    """
    lease = reported_definition(applied, "erasure_lease")
    assert "UNIQUE INDEX lease_current_unique (client_id ASC) WHERE superseded_at IS NULL" in lease
    assert "UNIQUE INDEX lease_idempotency_unique (idempotency_key ASC)" in lease
    assert "INDEX lease_history_by_client (client_id ASC, generation DESC)" in lease

    run = reported_definition(applied, "erasure_run")
    assert (
        "UNIQUE INDEX run_idempotency_unique (idempotency_key ASC) "
        "WHERE idempotency_key IS NOT NULL" in run
    ), "the run's idempotency uniqueness should hold only among the rows carrying a key"


def test_the_fencing_generation_reaches_the_evidence(applied: AppliedSchema) -> None:
    """The run, each disposition, and the certificate all state a generation."""
    for table, expected in sorted(FENCING_COLUMNS.items()):
        held = set(columns_of(applied.connection, applied.schema, table))
        assert expected <= held, f"{table} is missing {sorted(expected - held)}"


def test_the_checkpoint_tables_exist_and_expire_nothing(applied: AppliedSchema) -> None:
    """Both checkpoint tables are present, and neither carries row-level expiry.

    The absence is the point rather than an omission. A checkpoint's whole value is
    that it stays checkable after the rows it commits to have gone, so a
    checkpoint that expired would be evidence disappearing exactly when it is
    wanted.
    """
    present = {
        str(row[0])
        for row in rows(
            applied.connection,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (applied.schema,),
        )
    }
    for table in CHECKPOINT_TABLES:
        assert table in present, f"{table} should exist"
        definition = reported_definition(applied, table)
        assert "ttl" not in definition, f"{table} should configure no expiry: {definition}"


def test_the_checkpoint_session_names_a_session_without_referencing_it(
    applied: AppliedSchema,
) -> None:
    """The covered session identifier carries no reference of any kind.

    A reference would make this evidence either refuse the authorised erasure of
    the session it names or vanish along with it, and both destroy the record whose
    purpose is to stay checkable afterwards.
    """
    referencing = rows(
        applied.connection,
        "SELECT k.column_name FROM information_schema.key_column_usage AS k "
        "JOIN information_schema.table_constraints AS t "
        "ON t.table_schema = k.table_schema AND t.constraint_name = k.constraint_name "
        "WHERE k.table_schema = %s AND k.table_name = 'checkpoint_session' "
        "AND t.constraint_type = 'FOREIGN KEY'",
        (applied.schema,),
    )
    assert {str(row[0]) for row in referencing} == {"checkpoint_id"}


def test_the_working_tier_defaults_its_expiry_to_the_configured_interval(
    applied: AppliedSchema,
) -> None:
    """A row written with no expiry expires the configured interval after its write.

    The arithmetic is asserted against the stored row rather than against the
    reported default expression, because the platform normalises the interval it
    was given and the normalised form says nothing a reader can check against the
    configured number of seconds.
    """
    with applied.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO session (client_id, agent_cli, machine_id) "
            "VALUES (%s, 'stub', 'stub-machine') RETURNING id",
            (RESERVED_CLIENT_ID,),
        )
        session_id = cursor.fetchall()[0][0]
        cursor.execute(
            "INSERT INTO working_memory (session_id, scratch_key, client_id, value) "
            "VALUES (%s, 'plan', %s, '{}'::JSONB)",
            (session_id, RESERVED_CLIENT_ID),
        )

    interval = rows(
        applied.connection,
        "SELECT expires_at - updated_at FROM working_memory WHERE session_id = %s",
        (session_id,),
    )
    assert interval[0][0].total_seconds() == pytest.approx(float(WORKING_TTL_SECONDS))


def test_the_working_tier_expiry_configuration_reads_back(applied: AppliedSchema) -> None:
    """Every storage parameter the migration declares is on the committed descriptor.

    This read-back is the only evidence the configuration landed, and the failure
    it guards is silent rather than loud: setting row-level expiry on a table
    created earlier in the same transaction reports success and commits with the
    parameters absent, leaving a tier that expires no row while every statement
    involved claimed to have worked. Trusting the configuring statement's outcome
    would therefore assert nothing at all.
    """
    definition = reported_definition(applied, "working_memory")
    missing = [parameter for parameter in WORKING_TTL_PARAMETERS if parameter not in definition]
    assert missing == [], f"the working tier is missing {missing}; the cluster reports {definition}"


def test_the_procedure_tables_and_the_standing_column_are_present(
    applied: AppliedSchema,
) -> None:
    """The three usage tables exist and the artifact table carries a standing value."""
    present = {
        str(row[0])
        for row in rows(
            applied.connection,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (applied.schema,),
        )
    }
    for table in PROCEDURE_TABLES:
        assert table in present, f"{table} should exist"

    held = columns_of(applied.connection, applied.schema, "derived_artifact")
    assert held.get(STANDING_COLUMN) == ("double precision", "YES")


def test_the_confidence_equivalence_refuses_both_wrong_shapes(
    applied: AppliedSchema,
) -> None:
    """A summary may hold no standing, a learned procedure must hold one.

    The equivalence rather than an implication is what closes both silent failure
    modes: a summary carrying a stray standing value would sort into a recall
    tie-break it has no business in, and a learned procedure with none would be
    excluded by the floor predicate and so disappear from recall while remaining
    stored, which reads as data loss and is not.
    """
    digest = "a" * DIGEST_WIDTH
    insert = (
        "INSERT INTO derived_artifact "
        "(kind, owner_client_id, body, content_digest, derivation_method, expires_at, "
        "procedure_confidence) "
        "VALUES (%s, %s, 'body', %s, 'stub', now() + INTERVAL '3600 seconds', %s)"
    )

    for kind, standing in (("summary", 0.5), ("learned_procedure", None)):
        with (
            pytest.raises(applied.driver.Error) as refused,
            applied.connection.cursor() as cursor,
        ):
            cursor.execute(insert, (kind, RESERVED_CLIENT_ID, digest, standing))
        assert refusal_state(refused.value) == CHECK_VIOLATION, (
            f"a {kind} carrying {standing} should be refused; the platform said {refused.value}"
        )

    with applied.connection.cursor() as cursor:
        cursor.execute(insert, ("learned_procedure", RESERVED_CLIENT_ID, digest, 0.5))

    accepted = rows(
        applied.connection,
        "SELECT count(*) FROM derived_artifact WHERE procedure_confidence IS NOT NULL",
    )
    assert accepted[0][0] == 1, "the shape the equivalence admits should be accepted"


def test_every_second_generation_constraint_is_present(applied: AppliedSchema) -> None:
    """Each constraint the second generation names exists on the table it guards."""
    found = named_objects(
        applied.connection,
        applied.schema,
        "SELECT table_name, constraint_name FROM information_schema.table_constraints "
        "WHERE table_schema = %s",
    )
    for table, expected in sorted(SECOND_GENERATION_CONSTRAINTS.items()):
        present = found.get(table, set())
        assert expected <= present, f"{table} is missing {sorted(expected - present)}"


def test_every_second_generation_index_is_present(applied: AppliedSchema) -> None:
    """Each index the second generation names exists on the table it names it for."""
    found = named_objects(
        applied.connection,
        applied.schema,
        "SELECT table_name, index_name FROM information_schema.statistics WHERE table_schema = %s",
    )
    for table, expected in sorted(SECOND_GENERATION_INDEXES.items()):
        present = found.get(table, set())
        assert expected <= present, f"{table} is missing {sorted(expected - present)}"


def test_the_artifact_reference_view_names_no_working_table(applied: AppliedSchema) -> None:
    """The working tier is unreachable from the provenance and attribution tiers.

    The omission is structural enforcement rather than a documentation choice: an
    inserting statement proves a lineage parent exists by joining this view, and a
    kind the view does not return cannot pass that join. Adding the working table
    to it would silently convert a disposable scratch row into a governed artifact.
    """
    definition = rows(
        applied.connection,
        "SELECT view_definition FROM information_schema.views "
        "WHERE table_schema = %s AND table_name = 'artifact_ref'",
        (applied.schema,),
    )
    assert len(definition) == 1, "the artifact reference view should exist"
    assert VIEW_EXCLUDED_TABLE not in referenced_tables(str(definition[0][0]))
