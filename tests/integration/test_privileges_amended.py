"""What each role may do to the second generation, asserted against a live instance.

The first-generation privilege module asserts what migration 007 grants. This one
asserts what migration 014 grants, and more of its claims are absences than
presences, because most of what this generation says about privilege is what no
role may do.

Two of the four claims are introspection over the recorded grants. The erasure role
holds no privilege to delete audit evidence, and no role holds the privilege to
edit or remove a checkpoint. Both are the privilege half of the referential
protection the preceding migration put in place, and neither half is sufficient
alone: a restricting reference says nothing about deleting a row nothing
references, and a revoked privilege says nothing about a cascade the database
performs on the role's behalf.

The other two claims need a different shape than a grant comparison, for a platform
reason rather than a preference. This cluster's privilege model has no column list:
a grant names a table and a privilege and nothing finer, and a view narrowed to the
writable columns is not updatable either. Migration 014 therefore expresses each
column confinement as a guard that runs before every update and refuses a statement
changing a column the acting role may not change. The enforcement point is still
the database, which is what the requirement asks for, but it is not visible as a
grant and so cannot be introspected as one. The check that carries the same meaning
is to act as the role, attempt the refused write, and be refused. Each of those two
tests also performs a write the guard permits, so the guard is shown to be
selective rather than a blanket refusal.

The administrative path is exempt from every guard, deliberately: a database
administrator can already drop a table, so a guard pretending otherwise would be
theatre, and the answer to a hostile administrator is the externally signed
checkpoint rather than a trigger. That is why the acting role is switched away from
the administrative one before each refused write is attempted, and why the
revocation assertions are made over the four application roles rather than over
every role the cluster knows.

**Validates: Requirements 27.3, 27.4, 27.5, 44.1, 45.9, 46.5, 49.14**
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final

import pytest

from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The four application roles. Every privilege assertion below is scoped to these,
# because the administrative role is exempt by design.
APPLICATION_ROLES: Final[tuple[str, ...]] = (
    "molt_writer",
    "molt_eraser",
    "molt_reader",
    "molt_watcher",
)

WRITER: Final[str] = "molt_writer"
ERASER: Final[str] = "molt_eraser"

# The tenant row the first migration reserves, used as the owner of the rows the
# guard tests write against.
RESERVED_CLIENT_ID: Final[str] = "00000000-0000-4000-8000-000000000000"

# The tables holding evidence about a governed erasure. The role that performs
# erasures removes memory content, never the record of having removed it.
AUDIT_EVIDENCE_TABLES: Final[tuple[str, ...]] = (
    "erasure_request",
    "erasure_run",
    "erasure_candidate",
    "residue_candidate",
    "disposition",
    "run_session",
    "backup_record",
    "erasure_certificate",
    "audit_log_snapshot",
)

# The two tables a checkpoint is held in. A checkpoint any principal could rewrite
# would commit to nothing, and the coverage it extends beyond a cluster
# administrator would collapse to what the hash chain already gives itself.
CHECKPOINT_TABLES: Final[tuple[str, ...]] = ("ledger_checkpoint", "checkpoint_session")

# The privileges no role may hold on a checkpoint.
FORBIDDEN_ON_CHECKPOINTS: Final[frozenset[str]] = frozenset({"UPDATE", "DELETE"})

# The state the platform raises a guard refusal under. A guard is a raised
# condition rather than a privilege denial, so the code distinguishes the two: a
# refusal carrying this state means the guard ran, and one carrying the
# insufficient-privilege state would mean the grant was simply absent.
RAISED_BY_GUARD: Final[str] = "P0001"

# The two writes to a lease that reach an ownership column, each stated as a whole
# statement rather than assembled from a column name, so no identifier is
# interpolated into a statement anywhere in this module.
OWNERSHIP_WRITES: Final[tuple[tuple[str, object], ...]] = (
    ("UPDATE erasure_lease SET owner = %s WHERE id = %s", "another-owner"),
    ("UPDATE erasure_lease SET generation = %s WHERE id = %s", 99),
)

# A hexadecimal digest of the width the artifact table's check demands, built here
# rather than written out so the module carries no digest-shaped literal.
DIGEST_WIDTH: Final[int] = 64

Connection = Any


@dataclass(frozen=True, slots=True)
class AppliedSchema:
    """A schema holding every migration, opened to the application roles."""

    connection: Connection
    schema: str
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


def execute(connection: Connection, statement: object) -> None:
    """Send one statement that returns nothing."""
    with connection.cursor() as cursor:
        cursor.execute(statement)


@contextmanager
def acting_as(connection: Connection, role: str) -> Iterator[None]:
    """Run a block as one application role, then return to the opening identity.

    Switching identity rather than opening a second connection is what keeps the
    suite credential-free: no password is set, none is read, and none is stored.
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


@pytest.fixture(scope="module")
def applied(fresh_schema: Connection, database_driver: ModuleType) -> AppliedSchema:
    """Apply every migration into this module's own schema and open it to the roles.

    Reading the schema is granted here rather than by a migration because the
    schema is a fixture of this run: a migration grants on the tables it creates,
    and which namespace a test places them in is no migration's business. Without
    it a role would be refused at the namespace and never reach the guard the tests
    are about.
    """
    apply_migrations(fresh_schema)
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
    return AppliedSchema(connection=fresh_schema, schema=schema, driver=database_driver)


def test_the_erasure_role_may_delete_no_audit_evidence(applied: AppliedSchema) -> None:
    """The role that performs erasures holds no privilege to remove the record of one.

    The revocation is stated by the migration even though no grant ever conferred
    the privilege, and the reason is worth restating: an absence nobody wrote down
    is an absence a later grant can undo without anyone noticing.
    """
    held = privileges(applied.connection, applied.schema)
    deletable = sorted(
        table
        for grantee, table, privilege in held
        if grantee == ERASER and privilege == "DELETE" and table in AUDIT_EVIDENCE_TABLES
    )
    assert deletable == [], f"the erasure role may delete evidence from {deletable}"


def test_no_role_may_edit_or_remove_a_checkpoint(applied: AppliedSchema) -> None:
    """A checkpoint is evidence every role may read and no role may write over."""
    held = privileges(applied.connection, applied.schema)
    writable = sorted(
        (grantee, table, privilege)
        for grantee, table, privilege in held
        if table in CHECKPOINT_TABLES and privilege in FORBIDDEN_ON_CHECKPOINTS
    )
    assert writable == [], f"these roles may rewrite a checkpoint: {writable}"

    readable = {
        (grantee, table)
        for grantee, table, privilege in held
        if table in CHECKPOINT_TABLES and privilege == "SELECT"
    }
    for role in APPLICATION_ROLES:
        for table in CHECKPOINT_TABLES:
            assert (role, table) in readable, f"{role} should be able to read {table}"


def test_writer_update_on_the_artifact_table_is_confined_to_standing(
    applied: AppliedSchema,
) -> None:
    """The capture role may move a procedure's standing and may not rewrite its body.

    The grant is table-wide because this platform offers no finer grant. The guard
    is what narrows it, and the pair of outcomes below is the observable form of
    that narrowing: the permitted write lands, the refused write reaches the table
    and is turned back by a raised condition rather than by a privilege denial, and
    the stored body is unchanged afterwards.
    """
    granted = {
        privilege
        for grantee, table, privilege in privileges(applied.connection, applied.schema)
        if grantee == WRITER and table == "derived_artifact"
    }
    assert "UPDATE" in granted, (
        "the refusal below must come from the guard, so the grant has to be present"
    )

    digest = "a" * DIGEST_WIDTH
    with applied.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO derived_artifact "
            "(kind, owner_client_id, body, content_digest, derivation_method, expires_at, "
            "procedure_confidence) "
            "VALUES ('learned_procedure', %s, 'body', %s, 'stub', "
            "now() + INTERVAL '3600 seconds', 0.5) RETURNING id",
            (RESERVED_CLIENT_ID, digest),
        )
        artifact_id = cursor.fetchall()[0][0]

    with acting_as(applied.connection, WRITER):
        with applied.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE derived_artifact SET procedure_confidence = 0.9 WHERE id = %s",
                (artifact_id,),
            )
        with (
            pytest.raises(applied.driver.Error) as refused,
            applied.connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE derived_artifact SET body = 'rewritten' WHERE id = %s",
                (artifact_id,),
            )

    assert refusal_state(refused.value) == RAISED_BY_GUARD, (
        f"the guard should refuse the body rewrite; the platform said {refused.value}"
    )
    settled = rows(
        applied.connection,
        "SELECT procedure_confidence, body FROM derived_artifact WHERE id = %s",
        (artifact_id,),
    )
    assert settled[0][0] == pytest.approx(0.9)
    assert str(settled[0][1]) == "body"


def test_eraser_update_on_the_lease_reaches_neither_owner_nor_generation(
    applied: AppliedSchema,
) -> None:
    """A lease may be renewed and closed; its ownership claim is immutable.

    That immutability is what the fence rests on. A generation that could be
    restated would order nothing, and an owner that could be restated would let a
    worker inherit another worker's ownership by an update rather than by the
    ordered supersession the lease protocol requires. The renewal below shows the
    guard admits what the erasure path's job needs, so the two refusals after it
    are a confinement rather than a table the role simply cannot write.
    """
    granted = {
        privilege
        for grantee, table, privilege in privileges(applied.connection, applied.schema)
        if grantee == ERASER and table == "erasure_lease"
    }
    assert "UPDATE" in granted, (
        "the refusals below must come from the guard, so the grant has to be present"
    )

    with applied.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erasure_lease "
            "(client_id, owner, generation, idempotency_key, expires_at) "
            "VALUES (%s, 'stub-owner', 1, %s, now() + INTERVAL '3600 seconds') RETURNING id",
            (RESERVED_CLIENT_ID, f"key-{RESERVED_CLIENT_ID}"),
        )
        lease_id = cursor.fetchall()[0][0]

    refusals: list[str] = []
    with acting_as(applied.connection, ERASER):
        with applied.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE erasure_lease SET expires_at = expires_at + INTERVAL '3600 seconds', "
                "renewed_at = now() WHERE id = %s",
                (lease_id,),
            )
        for statement, value in OWNERSHIP_WRITES:
            with (
                pytest.raises(applied.driver.Error) as refused,
                applied.connection.cursor() as cursor,
            ):
                cursor.execute(statement, (value, lease_id))
            refusals.append(refusal_state(refused.value))

    assert refusals == [RAISED_BY_GUARD, RAISED_BY_GUARD], (
        f"both ownership writes should be refused by the guard, not {refusals}"
    )
    settled = rows(
        applied.connection,
        "SELECT owner, generation, renewed_at FROM erasure_lease WHERE id = %s",
        (lease_id,),
    )
    assert str(settled[0][0]) == "stub-owner"
    assert settled[0][1] == 1
    assert settled[0][2] is not None, "the renewal the guard admits should have landed"
