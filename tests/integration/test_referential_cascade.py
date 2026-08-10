"""Recomputable rows are carried away by the row they depend on, and nothing else is.

The protection migration draws one division and draws it per table: evidence about
a governed operation refuses to be cascaded away, and a row that is a function of
something else follows that thing out. This module asserts the second half. Three
references cascade, each for a reason of its own.

An embedding is a recomputable function of an artifact's text and the configured
model, so a tenant's removal takes its vectors with it: rebuilding one costs a
provider call, and keeping a stale one costs correctness. The cascade sits on the
tenant reference rather than on the artifact identifier, because that identifier is
polymorphic across the artifact kinds carrying embeddable text and so carries no
reference at all.

A lineage edge whose child is gone describes nothing, and the derivation it recorded
is carried on the disposition and in the certificate's lineage subgraph, so the
evidence outlives the edge.

A working-tier scratch row for a session that no longer exists is disposable by
construction, and the whole purpose of that tier is that nothing depends on its rows
surviving.

Each case asserts the cascade and the boundary of the cascade in the same test: the
dependent rows are gone, and a second set of rows of the same kind belonging to a
different parent is still present. Without the second half a cascade and an
over-broad delete are indistinguishable, and an over-broad delete on any of these
three tables is a silent loss of another tenant's or another session's state.

**Validates: Requirements 46.3, 46.7**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import uuid4

import pytest

from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The tenant row the first migration reserves, used as the owner of the rows whose
# survival marks the boundary of each cascade.
RESERVED_CLIENT_ID: Final[str] = "00000000-0000-4000-8000-000000000000"

# The width the vector column fixes. A vector of any other width is refused by the
# column type before the reference under test is reached.
VECTOR_WIDTH: Final[int] = 1024

# A hexadecimal digest of the width the artifact table's check demands, built here
# rather than written out so the module carries no digest-shaped literal.
DIGEST_WIDTH: Final[int] = 64

Connection = Any


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration."""

    connection: Connection


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


def count(connection: Connection, query: str, params: tuple[object, ...]) -> int:
    """The single number one counting query answered with."""
    return int(rows(connection, query, params)[0][0])


def insert_returning(connection: Connection, statement: str, params: tuple[object, ...]) -> str:
    """Send one insert that returns its own key and hand that key back."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return str(cursor.fetchall()[0][0])


def unit_vector() -> str:
    """A vector literal of the fixed width, carrying unit length.

    The length is what the index the recall path relies on assumes: it orders by
    squared distance while the thresholds are expressed in cosine space, and those
    two orderings agree only on unit vectors. Nothing here reads a provider.
    """
    components = ["0"] * VECTOR_WIDTH
    components[0] = "1"
    return "[" + ",".join(components) + "]"


def new_client(connection: Connection) -> str:
    """A tenant of this module's own, so a cascade over one disturbs no other."""
    slug = f"stub-{uuid4().hex}"
    return insert_returning(
        connection,
        "INSERT INTO client (slug, display_name) VALUES (%s, 'Stub workspace') RETURNING id",
        (slug,),
    )


def new_session(connection: Connection, client_id: str) -> str:
    """A session owning the scratch rows one of the cases removes."""
    return insert_returning(
        connection,
        "INSERT INTO session (client_id, agent_cli, machine_id) "
        "VALUES (%s, 'stub', 'stub-machine') RETURNING id",
        (client_id,),
    )


def new_artifact(connection: Connection, client_id: str) -> str:
    """A derived artifact, which a lineage edge may name as a child or a parent."""
    return insert_returning(
        connection,
        "INSERT INTO derived_artifact "
        "(kind, owner_client_id, body, content_digest, derivation_method, expires_at) "
        "VALUES ('summary', %s, 'body', %s, 'stub', now() + INTERVAL '3600 seconds') "
        "RETURNING id",
        (client_id, "a" * DIGEST_WIDTH),
    )


def new_embedding(connection: Connection, client_id: str) -> str:
    """A vector for one tenant, which is the recomputable row under test."""
    return insert_returning(
        connection,
        "INSERT INTO embedding "
        "(artifact_id, artifact_kind, client_id, provider, model_id, vec, expires_at) "
        "VALUES (%s, 'event', %s, 'stub', 'stub-model', %s::VECTOR, "
        "now() + INTERVAL '3600 seconds') RETURNING id",
        (str(uuid4()), client_id, unit_vector()),
    )


def new_edge(connection: Connection, child_id: str, parent_id: str) -> str:
    """An edge recording that one artifact was derived from another."""
    return insert_returning(
        connection,
        "INSERT INTO lineage_edge (child_id, parent_id, parent_kind, derivation_method) "
        "VALUES (%s, %s, 'derived_artifact', 'stub') RETURNING id",
        (child_id, parent_id),
    )


def new_scratch(connection: Connection, session_id: str, client_id: str, key: str) -> None:
    """A scratch row, which is the disposable state one of the cases removes."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO working_memory (session_id, scratch_key, client_id, value) "
            "VALUES (%s, %s, %s, '{}'::JSONB)",
            (session_id, key, client_id),
        )


@pytest.fixture(scope="module")
def cluster(fresh_schema: Connection) -> Cluster:
    """Apply every migration into this module's own schema, once."""
    apply_migrations(fresh_schema)
    return Cluster(connection=fresh_schema)


def test_removing_a_tenant_removes_its_vectors_and_no_others(cluster: Cluster) -> None:
    """A tenant's embeddings go with the tenant, and another tenant's stay.

    An embedding is derivable again from the artifact's text and the configured
    model, so it is the one representation in this schema that a tenant's removal is
    allowed to take with it. The second tenant is what distinguishes a cascade from
    a delete that reached further than the row it started from.
    """
    departing = new_client(cluster.connection)
    remaining = new_client(cluster.connection)
    departing_vector = new_embedding(cluster.connection, departing)
    remaining_vector = new_embedding(cluster.connection, remaining)

    with cluster.connection.cursor() as cursor:
        cursor.execute("DELETE FROM client WHERE id = %s", (departing,))

    assert (
        count(
            cluster.connection,
            "SELECT count(*) FROM embedding WHERE id = %s",
            (departing_vector,),
        )
        == 0
    ), "the departing tenant's vector should have been carried away by the cascade"
    assert (
        count(
            cluster.connection,
            "SELECT count(*) FROM embedding WHERE id = %s",
            (remaining_vector,),
        )
        == 1
    ), "no other tenant's vector should have been touched"


def test_removing_a_derived_artifact_removes_the_edges_naming_it_as_a_child(
    cluster: Cluster,
) -> None:
    """The edge into a removed artifact goes; the edge into its sibling stays.

    An edge whose child is gone describes nothing. What the edge recorded survives
    it: the derivation is carried on the disposition the erasure wrote and in the
    lineage subgraph the certificate holds, so the evidence is not what cascades
    here.
    """
    client_id = new_client(cluster.connection)
    parent = new_artifact(cluster.connection, client_id)
    departing_child = new_artifact(cluster.connection, client_id)
    remaining_child = new_artifact(cluster.connection, client_id)
    departing_edge = new_edge(cluster.connection, departing_child, parent)
    remaining_edge = new_edge(cluster.connection, remaining_child, parent)

    with cluster.connection.cursor() as cursor:
        cursor.execute("DELETE FROM derived_artifact WHERE id = %s", (departing_child,))

    assert (
        count(
            cluster.connection,
            "SELECT count(*) FROM lineage_edge WHERE id = %s",
            (departing_edge,),
        )
        == 0
    ), "the edge naming the removed artifact as its child should be gone"
    assert (
        count(
            cluster.connection,
            "SELECT count(*) FROM lineage_edge WHERE id = %s",
            (remaining_edge,),
        )
        == 1
    ), "the sibling's edge should be untouched"
    assert (
        count(
            cluster.connection,
            "SELECT count(*) FROM derived_artifact WHERE id = %s",
            (parent,),
        )
        == 1
    ), "the parent the removed artifact was derived from should survive it"


def test_removing_a_session_removes_its_scratch_rows_and_no_others(
    cluster: Cluster,
) -> None:
    """A session's working rows go with the session, and another session's stay.

    Every other tier in this schema takes the opposite posture, and the difference
    is the whole point of the tier: scratch state exists because something has to be
    forgettable, so a removed session takes its scratch with it rather than refusing
    to go.
    """
    client_id = new_client(cluster.connection)
    departing = new_session(cluster.connection, client_id)
    remaining = new_session(cluster.connection, client_id)
    new_scratch(cluster.connection, departing, client_id, "plan")
    new_scratch(cluster.connection, departing, client_id, "cursor")
    new_scratch(cluster.connection, remaining, client_id, "plan")

    with cluster.connection.cursor() as cursor:
        cursor.execute("DELETE FROM session WHERE id = %s", (departing,))

    assert (
        count(
            cluster.connection,
            "SELECT count(*) FROM working_memory WHERE session_id = %s",
            (departing,),
        )
        == 0
    ), "the removed session's scratch should have been carried away by the cascade"
    assert (
        count(
            cluster.connection,
            "SELECT count(*) FROM working_memory WHERE session_id = %s",
            (remaining,),
        )
        == 1
    ), "another session's scratch should be untouched"
