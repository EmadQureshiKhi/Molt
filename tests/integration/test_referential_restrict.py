"""Deleting audit history is refused by the cluster, one reference at a time.

Removing a row of memory content is a governed operation with a request, a lease, a
candidate set, a disposition per artifact, and a signed certificate behind it.
Removing a row of evidence *about* that operation is not something any principal
should be able to do by accident, and cascading deletes were exactly how it could
have happened: the references the earlier migrations created inline carried a
cascade or the platform's default, so removing one run row would have taken the
whole record of what that run touched with it.

Each case below is built in isolation and asserts three things about one reference:
the delete is refused, the refusal names the referencing table, and the referencing
row is still present afterwards. Isolation is what makes the third claim
attributable. A single graph carrying every referencing row at once would produce a
refusal too, but it would be one refusal for ten references and no case could then
be said to have been covered — the first constraint the cluster happens to check
would satisfy the whole module.

Two notes on what is asserted from the refusal itself. The condition code is
compared rather than the prose, because a code is a contract and a message is not,
and the code distinguishes a referential refusal from a privilege denial or a check
violation. The referencing table name is looked for in the message because that is
the value an operator needs in order to know what to ask about. The count of
referencing rows is asserted by counting them, not by reading it out of the
message: what matters is that the evidence survived the attempt, and a count taken
from the cluster is evidence of that where a count parsed out of prose would only be
evidence about the prose.

**Validates: Requirements 46.1, 46.2, 46.3, 46.6**
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final
from uuid import uuid4

import pytest

from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The tenant row the first migration reserves, used as the owner of every row
# these cases build.
RESERVED_CLIENT_ID: Final[str] = "00000000-0000-4000-8000-000000000000"

# The state the platform refuses a referential violation under. A refusal carrying
# this code is the restricting reference doing its work; a privilege denial or a
# check violation would carry a different one.
REFERENTIAL_REFUSAL: Final[str] = "23503"

# A hexadecimal digest of the width the checkpoint and certificate checks demand,
# built here rather than written out so the module carries no digest-shaped literal.
DIGEST_WIDTH: Final[int] = 64

Connection = Any


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, and the driver its refusals come from."""

    connection: Connection
    driver: ModuleType


@dataclass(frozen=True, slots=True)
class Reference:
    """One protected reference, built and ready to have its parent deleted."""

    parent_table: str
    parent_id: str
    child_table: str
    child_column: str


def count(connection: Connection, statement: object, params: tuple[object, ...]) -> int:
    """The single number one counting statement answered with.

    The statement is taken as a composed object rather than as text, because every
    counting statement here names a table the parametrised case chose and an
    identifier is composed rather than interpolated.
    """
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return int(cursor.fetchall()[0][0])


def insert_returning(connection: Connection, statement: str, params: tuple[object, ...]) -> str:
    """Send one insert that returns its own key and hand that key back."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return str(cursor.fetchall()[0][0])


def refusal_state(error: BaseException) -> str:
    """The condition code a refusal carries, or an empty value when it carries none."""
    state = getattr(error, "sqlstate", None)
    return str(state) if state is not None else ""


def new_request(connection: Connection) -> str:
    """An erasure request, which is what a run is the execution of."""
    return insert_returning(
        connection,
        "INSERT INTO erasure_request (client_id, requester, justification) "
        "VALUES (%s, 'stub-requester', 'stub-justification') RETURNING id",
        (RESERVED_CLIENT_ID,),
    )


def new_lease(connection: Connection) -> str:
    """A lease, which is the proof a finalising worker held ownership."""
    return insert_returning(
        connection,
        "INSERT INTO erasure_lease (client_id, owner, generation, idempotency_key, expires_at) "
        "VALUES (%s, 'stub-owner', 1, %s, now() + INTERVAL '3600 seconds') "
        "RETURNING id",
        (RESERVED_CLIENT_ID, str(uuid4())),
    )


def new_run(connection: Connection, request_id: str, lease_id: str | None = None) -> str:
    """A run of one request, optionally naming the lease it was performed under."""
    return insert_returning(
        connection,
        "INSERT INTO erasure_run (request_id, client_id, requester, t_before, lease_id) "
        "VALUES (%s, %s, 'stub-requester', now(), %s) RETURNING id",
        (request_id, RESERVED_CLIENT_ID, lease_id),
    )


def new_checkpoint(connection: Connection) -> str:
    """A signed checkpoint, which the per-session digests below belong to."""
    return insert_returning(
        connection,
        "INSERT INTO ledger_checkpoint (window_start, window_end, covered_session_count, "
        "root_digest, signature, kms_key_id, signing_algorithm) "
        "VALUES (now(), now() + INTERVAL '3600 seconds', 1, %s, %s, 'stub-key', "
        "'stub-algorithm') "
        "RETURNING id",
        ("a" * DIGEST_WIDTH, b"stub-signature"),
    )


def build_disposition(cluster: Cluster) -> Reference:
    """A disposition, which is the substance a certificate's claim rests on."""
    run_id = new_run(cluster.connection, new_request(cluster.connection))
    with cluster.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO disposition "
            "(run_id, artifact_id, artifact_kind, disposition, reason, selection_reason) "
            "VALUES (%s, %s, 'event', 'hard_delete', 'stub-reason', 'session_scope')",
            (run_id, str(uuid4())),
        )
    return Reference("erasure_run", run_id, "disposition", "run_id")


def build_candidate(cluster: Cluster) -> Reference:
    """The candidate set, which is the record of what the sweep selected."""
    run_id = new_run(cluster.connection, new_request(cluster.connection))
    with cluster.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erasure_candidate "
            "(run_id, artifact_id, artifact_kind, selection_reason) "
            "VALUES (%s, %s, 'event', 'session_scope')",
            (run_id, str(uuid4())),
        )
    return Reference("erasure_run", run_id, "erasure_candidate", "run_id")


def build_residue(cluster: Cluster) -> Reference:
    """A residue candidate, which is the only record of a borderline decision."""
    run_id = new_run(cluster.connection, new_request(cluster.connection))
    with cluster.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO residue_candidate "
            "(run_id, artifact_id, artifact_kind, query_artifact_id, cosine_distance, band, "
            "included, decision_reason) "
            "VALUES (%s, %s, 'event', %s, 0.1, 'auto_include', true, 'stub-reason')",
            (run_id, str(uuid4()), str(uuid4())),
        )
    return Reference("erasure_run", run_id, "residue_candidate", "run_id")


def build_run_session(cluster: Cluster) -> Reference:
    """The per-session terminal digests a verifier re-derives."""
    run_id = new_run(cluster.connection, new_request(cluster.connection))
    with cluster.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO run_session (run_id, session_id) VALUES (%s, %s)",
            (run_id, str(uuid4())),
        )
    return Reference("erasure_run", run_id, "run_session", "run_id")


def build_backup(cluster: Cluster) -> Reference:
    """The backup evidence, which answers whether the erasure was reversible."""
    run_id = new_run(cluster.connection, new_request(cluster.connection))
    with cluster.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO backup_record (run_id, command, status) "
            "VALUES (%s, 'stub-command', 'succeeded')",
            (run_id,),
        )
    return Reference("erasure_run", run_id, "backup_record", "run_id")


def build_certificate(cluster: Cluster) -> Reference:
    """The signed document, which must not be removable by removing its run."""
    run_id = new_run(cluster.connection, new_request(cluster.connection))
    with cluster.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erasure_certificate (run_id, payload, canonical_digest) "
            "VALUES (%s, '{}'::JSONB, %s)",
            (run_id, "a" * DIGEST_WIDTH),
        )
    return Reference("erasure_run", run_id, "erasure_certificate", "run_id")


def build_audit_snapshot(cluster: Cluster) -> Reference:
    """The cluster's own audit records covering the run window."""
    run_id = new_run(cluster.connection, new_request(cluster.connection))
    with cluster.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO audit_log_snapshot (run_id, window_start, window_end, records) "
            "VALUES (%s, now(), now() + INTERVAL '3600 seconds', '[]'::JSONB)",
            (run_id,),
        )
    return Reference("erasure_run", run_id, "audit_log_snapshot", "run_id")


def build_request_reference(cluster: Cluster) -> Reference:
    """A request whose runs exist is history rather than a draft."""
    request_id = new_request(cluster.connection)
    new_run(cluster.connection, request_id)
    return Reference("erasure_request", request_id, "erasure_run", "request_id")


def build_lease_reference(cluster: Cluster) -> Reference:
    """The lease a run names is the ownership claim its certificate states."""
    lease_id = new_lease(cluster.connection)
    new_run(cluster.connection, new_request(cluster.connection), lease_id)
    return Reference("erasure_lease", lease_id, "erasure_run", "lease_id")


def build_checkpoint_session(cluster: Cluster) -> Reference:
    """The per-session digests that localise a checkpoint disagreement."""
    checkpoint_id = new_checkpoint(cluster.connection)
    with cluster.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO checkpoint_session "
            "(checkpoint_id, session_id, terminal_chain_digest, terminal_seq) "
            "VALUES (%s, %s, %s, 1)",
            (checkpoint_id, str(uuid4()), "a" * DIGEST_WIDTH),
        )
    return Reference("ledger_checkpoint", checkpoint_id, "checkpoint_session", "checkpoint_id")


# Every reference the protection migration restricts, each built in isolation so
# that a refusal is attributable to the one reference the case is about.
PROTECTED_REFERENCES: Final[dict[str, Callable[[Cluster], Reference]]] = {
    "disposition": build_disposition,
    "candidate": build_candidate,
    "residue": build_residue,
    "run_session": build_run_session,
    "backup": build_backup,
    "certificate": build_certificate,
    "audit_snapshot": build_audit_snapshot,
    "request": build_request_reference,
    "lease": build_lease_reference,
    "checkpoint_session": build_checkpoint_session,
}


@pytest.fixture(scope="module")
def cluster(fresh_schema: Connection, database_driver: ModuleType) -> Cluster:
    """Apply every migration into this module's own schema, once."""
    apply_migrations(fresh_schema)
    return Cluster(connection=fresh_schema, driver=database_driver)


@pytest.mark.parametrize("case", sorted(PROTECTED_REFERENCES))
def test_a_delete_that_would_remove_audit_history_is_refused(cluster: Cluster, case: str) -> None:
    """The delete is refused, the refusal names the referencing table, the row stays.

    All three matter. A refusal alone could come from a privilege the acting
    identity lacks, so the condition code is compared. A refusal naming nothing
    would leave an operator no way to find out what to ask about, so the
    referencing table is looked for in the message. And a refusal that rolled back
    only part of what it touched would still have destroyed evidence, so the
    referencing rows are counted afterwards rather than assumed.
    """
    reference = PROTECTED_REFERENCES[case](cluster)
    composer = cluster.driver.sql
    referencing = composer.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
        composer.Identifier(reference.child_table),
        composer.Identifier(reference.child_column),
    )
    parent_rows = composer.SQL("SELECT count(*) FROM {} WHERE id = %s").format(
        composer.Identifier(reference.parent_table)
    )
    removal = composer.SQL("DELETE FROM {} WHERE id = %s").format(
        composer.Identifier(reference.parent_table)
    )

    before = count(cluster.connection, referencing, (reference.parent_id,))
    assert before == 1, f"the {case} case should build exactly one referencing row"

    with pytest.raises(cluster.driver.Error) as refused, cluster.connection.cursor() as cursor:
        cursor.execute(removal, (reference.parent_id,))

    assert refusal_state(refused.value) == REFERENTIAL_REFUSAL, (
        f"removing a {reference.parent_table} referenced by a {reference.child_table} "
        f"should be refused referentially; the platform said {refused.value}"
    )
    assert reference.child_table in str(refused.value), (
        f"the refusal should name the referencing table; it said {refused.value}"
    )

    after = count(cluster.connection, referencing, (reference.parent_id,))
    assert after == before, f"the {case} evidence should be present after the refusal"

    assert count(cluster.connection, parent_rows, (reference.parent_id,)) == 1, (
        "the refused delete should have removed nothing at all"
    )
