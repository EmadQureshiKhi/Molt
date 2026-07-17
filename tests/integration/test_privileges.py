"""What each role may do to the first generation, asserted against a live instance.

Every claim here is a privilege claim, and each is asserted the only way that
makes it a guarantee rather than a convention: by asking the cluster.

Two of the four claims are introspection. No application role holds the privilege
to revise the episodic record, and the read-only role holds nothing beyond
reading. Both are set comparisons over the recorded grants.

The third claim, that the erasure role may remove a ledger row and may not edit
one, is the same kind of comparison narrowed to one role and one table.

The fourth claim needs a different shape than a grant comparison, and the reason
is a platform fact rather than a preference. This platform's privilege model has
no column list: a grant names a table and a privilege and nothing finer, and a
view narrowed to the writable columns is not updatable either. Migration 007
therefore expresses column scoping as a guard that runs before every update and
refuses a statement changing a column the acting role may not change. The
enforcement point is still the database, which is what the requirement asks for,
but it is not visible as a grant and so cannot be introspected as one. The check
that carries the same meaning is to act as the role, attempt the refused write,
and be refused — which is what the two guard tests below do. Each also performs a
write the guard permits, so the guard is shown to be selective rather than a
blanket refusal.

The administrative path is exempt from both guards, deliberately and not by
oversight: a database administrator can already drop a table, so a guard
pretending otherwise would be theatre. That is why the acting role is switched
away from the administrative one before each refused write is attempted, and why
the revocation assertions are made over the four application roles rather than
over every role the cluster knows.

Only the first migration generation is applied, staged into a directory of its
own, so the privileges under test are exactly the ones migration 007 grants. A
later generation carries its own grants in the migration that closes it, and
those are that generation's tests to make.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from molt.store.migrate import MigrationReport, apply_migrations, discover_migrations

pytestmark = pytest.mark.integration

# The last version of the first migration generation.
FIRST_GENERATION_LAST_VERSION: Final[int] = 7
FIRST_GENERATION_VERSIONS: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 6, 7)

# The four application roles migration 007 creates. Every privilege assertion
# below is scoped to these, because the administrative role is exempt by design.
APPLICATION_ROLES: Final[tuple[str, ...]] = (
    "molt_writer",
    "molt_eraser",
    "molt_reader",
    "molt_watcher",
)

WRITER: Final[str] = "molt_writer"
ERASER: Final[str] = "molt_eraser"
READER: Final[str] = "molt_reader"

# The tenant row the first migration reserves, used as the owner of the rows the
# guard tests write against.
RESERVED_CLIENT_ID: Final[str] = "00000000-0000-4000-8000-000000000000"

# The state the platform raises a guard refusal under. A guard is a raised
# condition rather than a privilege denial, so the code distinguishes the two: a
# refusal carrying this state means the guard ran, and one carrying the
# insufficient-privilege state would mean the grant was simply absent.
RAISED_BY_GUARD: Final[str] = "P0001"

Connection = Any


@dataclass(frozen=True, slots=True)
class AppliedSchema:
    """A schema holding exactly the first generation, and how it was produced."""

    connection: Connection
    schema: str
    directory: Path
    report: MigrationReport
    driver: ModuleType


def stage_first_generation(destination: Path) -> tuple[int, ...]:
    """Copy the first-generation migration files into a directory of their own.

    The bytes are copied rather than the text, so each staged file digests to
    exactly what the original digests to and a second run over the same directory
    is recognised as already applied.
    """
    staged: list[int] = []
    for migration in discover_migrations():
        if migration.version > FIRST_GENERATION_LAST_VERSION:
            continue
        destination.joinpath(migration.path.name).write_bytes(migration.path.read_bytes())
        staged.append(migration.version)
    return tuple(staged)


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


def execute(connection: Connection, statement: object) -> None:
    """Send one statement that returns nothing."""
    with connection.cursor() as cursor:
        cursor.execute(statement)


@contextmanager
def acting_as(connection: Connection, role: str) -> Iterator[None]:
    """Run a block as one application role, then return to the opening identity.

    Switching identity rather than opening a second connection is what keeps the
    suite credential-free: no password is set, none is read, and none is stored.
    The guards read the acting identity, so a block inside this is subject to
    exactly the refusals the role is subject to.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {role}")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE NONE")


def refusal_state(error: BaseException) -> str:
    """The condition code a refusal carries, or an empty value when it carries none."""
    state = getattr(error, "sqlstate", None)
    return str(state) if state is not None else ""


def privileges(connection: Connection, schema: str) -> set[tuple[str, str, str]]:
    """Every grant the application roles hold in one schema."""
    return {
        (str(grantee), str(table), str(privilege))
        for grantee, table, privilege in rows(
            connection,
            "SELECT grantee, table_name, privilege_type "
            "FROM information_schema.table_privileges "
            "WHERE table_schema = %s AND grantee = ANY(%s)",
            (schema, list(APPLICATION_ROLES)),
        )
    }


def state_snapshot(connection: Connection, schema: str) -> tuple[object, ...]:
    """Everything a re-application could disturb, as one comparable value."""
    inventory = rows(
        connection,
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = %s ORDER BY table_name",
        (schema,),
    )
    indexes = rows(
        connection,
        "SELECT table_name, index_name, column_name, seq_in_index "
        "FROM information_schema.statistics WHERE table_schema = %s ORDER BY 1, 2, 4",
        (schema,),
    )
    constraints = rows(
        connection,
        "SELECT table_name, constraint_name, constraint_type "
        "FROM information_schema.table_constraints WHERE table_schema = %s ORDER BY 1, 2",
        (schema,),
    )
    history = rows(
        connection,
        "SELECT version, name, file_digest FROM schema_migration ORDER BY version",
    )
    tenants = rows(connection, "SELECT id, slug FROM client ORDER BY id")
    capabilities = rows(connection, "SELECT name, available, detail FROM capability ORDER BY 1")
    return (
        tuple(str(row) for row in inventory),
        tuple(str(row) for row in indexes),
        tuple(str(row) for row in constraints),
        tuple(str(row) for row in history),
        tuple(str(row) for row in tenants),
        tuple(str(row) for row in capabilities),
        tuple(sorted(privileges(connection, schema))),
    )


@pytest.fixture(scope="module")
def applied(
    fresh_schema: Connection,
    database_driver: ModuleType,
    tmp_path_factory: pytest.TempPathFactory,
) -> AppliedSchema:
    """Apply the first generation into this module's own schema and open it to the roles.

    Reading the schema is granted here rather than by a migration because the
    schema is a fixture of this run: a migration grants on the tables it creates,
    and which namespace a test places them in is no migration's business. Without
    it a role would be refused at the namespace and never reach the guard the
    tests are about.
    """
    directory = tmp_path_factory.mktemp("molt_first_generation_privileges")
    staged = stage_first_generation(directory)
    assert staged == FIRST_GENERATION_VERSIONS, (
        f"the first generation should stage as seven consecutive files, not {staged}"
    )
    report = apply_migrations(fresh_schema, directory=directory)
    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        schema = str(cursor.fetchall()[0][0])

    composer = database_driver.sql
    for role in APPLICATION_ROLES:
        execute(
            fresh_schema,
            composer.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                composer.Identifier(schema), composer.Identifier(role)
            ),
        )
    return AppliedSchema(
        connection=fresh_schema,
        schema=schema,
        directory=directory,
        report=report,
        driver=database_driver,
    )


def test_no_application_role_may_revise_the_ledger(applied: AppliedSchema) -> None:
    """No application role holds the privilege to edit an episodic row.

    This is the whole basis of the chain's tamper evidence: a chain whose rows can
    be edited in place commits to nothing. Rows leave the ledger by an authorised
    erasure or by row-level expiry, never by revision.
    """
    held = privileges(applied.connection, applied.schema)
    revisers = sorted(
        grantee for grantee, table, privilege in held if table == "ledger" and privilege == "UPDATE"
    )
    assert revisers == [], f"these roles may revise the ledger: {revisers}"


def test_eraser_may_remove_a_ledger_row_and_may_not_edit_one(applied: AppliedSchema) -> None:
    """The erasure role removes episodic content and never restates it."""
    held = {
        privilege
        for grantee, table, privilege in privileges(applied.connection, applied.schema)
        if grantee == ERASER and table == "ledger"
    }
    assert "DELETE" in held
    assert "UPDATE" not in held
    assert held == {"SELECT", "DELETE"}, f"the erasure role holds {sorted(held)} on the ledger"


def test_reader_holds_reading_and_nothing_else(applied: AppliedSchema) -> None:
    """The read-only role holds one privilege kind, on everything it can reach.

    It is what the independent certificate verifier, the sensitivity analyser, the
    write-stream read path, the memory-protocol server, and the auditor views
    connect with, so each no-mutation guarantee is structural rather than promised.
    """
    held = {
        privilege
        for grantee, _table, privilege in privileges(applied.connection, applied.schema)
        if grantee == READER
    }
    beyond_reading = sorted(held - {"SELECT"})
    assert held == {"SELECT"}, f"the read-only role also holds {beyond_reading}"


def test_writer_update_on_attribution_is_confined_to_closing_a_version(
    applied: AppliedSchema,
) -> None:
    """The capture role holds the privilege but may restate no stored field.

    The grant is table-wide because this platform offers no finer grant. The guard
    is what narrows it, and the refusal below is the observable form of that
    narrowing: the write reaches the table, the guard runs, and the statement is
    refused with a raised condition rather than with a privilege denial. Every
    column the first generation declares on the attribution table is immutable, so
    the closure columns a later migration adds are writable by construction and
    are that migration's tests to cover.
    """
    granted = {
        privilege
        for grantee, table, privilege in privileges(applied.connection, applied.schema)
        if grantee == WRITER and table == "client_binding"
    }
    assert "UPDATE" in granted, (
        "the refusal below must come from the guard, so the grant has to be present"
    )

    with applied.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO client_binding "
            "(artifact_id, artifact_kind, client_id, method, confidence) "
            "VALUES (gen_random_uuid(), 'event', %s, 'scope', 0.5) RETURNING id",
            (RESERVED_CLIENT_ID,),
        )
        binding_id = cursor.fetchall()[0][0]

    with (
        acting_as(applied.connection, WRITER),
        pytest.raises(applied.driver.Error) as refused,
        applied.connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE client_binding SET confidence = 0.9 WHERE id = %s",
            (binding_id,),
        )
    assert refusal_state(refused.value) == RAISED_BY_GUARD, (
        f"the guard should refuse the write; the platform said {refused.value}"
    )

    unchanged = rows(
        applied.connection,
        "SELECT confidence FROM client_binding WHERE id = %s",
        (binding_id,),
    )
    assert unchanged[0][0] == pytest.approx(0.5)


def test_session_guard_admits_a_counter_and_refuses_the_tenant(applied: AppliedSchema) -> None:
    """The same mechanism permits what a role's job needs and refuses the rest.

    The capture role moves a session's counters and may not move the session to
    another tenant. Showing both halves is what distinguishes a scoped grant from
    a table the role simply cannot write.
    """
    with applied.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO session (client_id, agent_cli, machine_id) "
            "VALUES (%s, 'stub', 'stub-machine') RETURNING id",
            (RESERVED_CLIENT_ID,),
        )
        session_id = cursor.fetchall()[0][0]

    with acting_as(applied.connection, WRITER):
        with applied.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE session SET tool_call_count = tool_call_count + 1 WHERE id = %s",
                (session_id,),
            )
        with (
            pytest.raises(applied.driver.Error) as refused,
            applied.connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE session SET client_id = gen_random_uuid() WHERE id = %s",
                (session_id,),
            )

    assert refusal_state(refused.value) == RAISED_BY_GUARD, (
        f"the guard should refuse the tenancy write; the platform said {refused.value}"
    )
    settled = rows(
        applied.connection,
        "SELECT tool_call_count, client_id FROM session WHERE id = %s",
        (session_id,),
    )
    assert settled[0][0] == 1
    assert str(settled[0][1]) == RESERVED_CLIENT_ID


def test_re_application_changes_no_state(applied: AppliedSchema) -> None:
    """A second run over the same files skips every version and disturbs nothing.

    Two mechanisms make this true and the assertion covers both: the runner skips
    a version its history already records, and every statement the files hold is
    written to be re-runnable anyway. The comparison is over the object inventory,
    the indexes, the constraints, the recorded history, the seeded rows, and the
    grants, so a repeat that quietly re-issued a statement would still be caught.
    """
    before = state_snapshot(applied.connection, applied.schema)

    second = apply_migrations(applied.connection, directory=applied.directory)

    assert second.applied_versions == ()
    assert second.skipped_versions == FIRST_GENERATION_VERSIONS
    assert second.changed_state is False
    assert state_snapshot(applied.connection, applied.schema) == before
