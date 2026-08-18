"""Removing a seeded corpus: what the reset targets, in what order, and what it reports.

The reset exists so `molt seed --reset` means something. It removes the memory
content of the tenants the corpus definition names and reports, per table, how many
rows went, so an operator reads evidence rather than a claim.

Four decisions carry the module.

**The scope is the corpus definition's tenants and nothing else.** Every slug in
`DOMAINS` is looked up, the identifiers that came back are the only values any
delete binds, and a cluster holding none of those slugs is left entirely untouched:
no delete is issued at all and every count is reported as zero. Nothing here reaches
a tenant the seed did not invent, because nothing here can name one.

**A slug alone does not authorise a delete.** A stored Client is removed only when
its display name, its jurisdiction, and its content markers are the ones the
definition declares as well. A row carrying a seeded slug with any other identity is
a real tenant sitting on that name, and it ends the reset with a refusal naming the
slug rather than with a delete. That is the guard against a reset pointed at the
wrong database: a cluster of real memory either holds none of these slugs, in which
case the reset removes nothing, or holds one with a different identity, in which case
it is refused.

**The `client` row itself stays.** The reset removes what the seeder wrote into a
tenant, which is what an authorised erasure of that tenant removes too, and it leaves
the tenant row alone. Two reasons rather than one: the seeder's own insert resolves a
repeated slug in place, so a re-seed reuses the row and reports the same corpus; and
the grants confer DELETE on `client` to no role the product connects as, so a reset
that tried would fail on a privilege rather than finish. The tenants therefore survive
the reset as empty tenants, which is exactly the state the seeder expects to write
into.

**The order is the erasure path's, and it is load-bearing twice over.** Migration
017 dropped the spawning reference, the parent-Session reference, and the answering
Event reference, and left `ledger.session_id` enforced, so an Event may not outlive
its Session's removal: the ledger delete goes before the Session delete. The
remaining order is the same one the disposition phase fixes -- dependents before the
rows they hang off -- and it carries a second requirement of its own here: the edge
and binding deletes recognise their rows by reading `ledger` and `derived_artifact`,
so they have to run while those rows still exist. A reordering would not raise; it
would silently leave rows behind.

Every statement is a whole module-level literal with bound parameters. No identifier
and no slug is ever interpolated into statement text, and each delete carries its own
aggregate count computed inside the same statement, so a reported number is the
cluster's report of what it removed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from molt.errors import MoltError, StoreError
from molt.models.event import JsonObject
from molt.seed.corpora import DOMAINS, ClientDomain
from molt.store import Cursor, MemoryStore

__all__ = [
    "COMPONENT",
    "DELETE_BINDINGS_STATEMENT",
    "DELETE_DERIVED_STATEMENT",
    "DELETE_EDGES_STATEMENT",
    "DELETE_EMBEDDINGS_STATEMENT",
    "DELETE_EVENTS_STATEMENT",
    "DELETE_SESSIONS_STATEMENT",
    "DELETE_WORKING_STATEMENT",
    "RESET_LABEL",
    "RESET_ORDER",
    "SELECT_SEEDED_CLIENTS_STATEMENT",
    "ResetRefusedError",
    "ResetReport",
    "TableRemoval",
    "reset_corpus",
    "seeded_slugs",
]

# The component name a failure from this module names itself by.
COMPONENT: Final[str] = "seed"

# The label the one delete transaction appears under in a log record.
RESET_LABEL: Final[str] = "seed_reset_corpus"

# How many columns the tenant lookup returns, checked before a row is decoded so
# the statement and its decoder cannot drift apart silently.
_CLIENT_ROW_WIDTH: Final[int] = 5

# The tenant lookup. Slugs are bound as one array, and the ordering is stated so two
# resets over one cluster resolve the same tenants in the same order.
SELECT_SEEDED_CLIENTS_STATEMENT: Final[str] = (
    "SELECT id, slug, display_name, jurisdiction, content_markers FROM client "
    "WHERE slug = ANY (%s::STRING[]) ORDER BY slug"
)

# The working tier, which goes first for the reason the erasure engine purges it
# first: it is disposable by construction, so nothing else in the graph is waiting
# on it and removing it early narrows what a later refusal could leave behind.
DELETE_WORKING_STATEMENT: Final[str] = (
    "WITH removed AS ("
    "DELETE FROM working_memory WHERE client_id = ANY (%s::UUID[]) RETURNING 1"
    ") SELECT count(*) FROM removed"
)

# The vectors. Every seeded vector records the tenant it was written for, so the
# tenant column is the whole predicate and no artifact identifier crosses the wire.
DELETE_EMBEDDINGS_STATEMENT: Final[str] = (
    "WITH removed AS ("
    "DELETE FROM embedding WHERE client_id = ANY (%s::UUID[]) RETURNING 1"
    ") SELECT count(*) FROM removed"
)

# The derivation graph. An edge is recognised from either end, because a parent
# identifier carries no reference of its own and a seeded Artifact may be either
# half of an edge. Both halves are read from the tables they live in, which is why
# this statement runs before either of those tables is emptied.
DELETE_EDGES_STATEMENT: Final[str] = (
    "WITH removed AS ("
    "DELETE FROM lineage_edge WHERE "
    "child_id IN (SELECT id FROM derived_artifact WHERE owner_client_id = ANY (%s::UUID[])) "
    "OR parent_id IN (SELECT id FROM derived_artifact WHERE owner_client_id = ANY (%s::UUID[])) "
    "OR parent_id IN (SELECT id FROM ledger WHERE client_id = ANY (%s::UUID[])) "
    "OR parent_id IN (SELECT id FROM session WHERE client_id = ANY (%s::UUID[])) "
    "RETURNING 1"
    ") SELECT count(*) FROM removed"
)

# The attribution claims. A seeded tenant's own claims go, and so does every claim
# any tenant holds on a seeded Artifact: a claim on a row that no longer exists
# describes nothing. A superseded version names the version that replaced it, and a
# whole chain leaves inside this one statement, so that reference is satisfied.
DELETE_BINDINGS_STATEMENT: Final[str] = (
    "WITH removed AS ("
    "DELETE FROM client_binding WHERE client_id = ANY (%s::UUID[]) "
    "OR artifact_id IN (SELECT id FROM ledger WHERE client_id = ANY (%s::UUID[])) "
    "OR artifact_id IN (SELECT id FROM derived_artifact WHERE owner_client_id = ANY (%s::UUID[])) "
    "RETURNING 1"
    ") SELECT count(*) FROM removed"
)

# The summaries, baselines, and procedures. Their standing history hangs off them
# with a cascading reference, so the retrieval, outcome, and change rows leave with
# them rather than needing statements of their own.
DELETE_DERIVED_STATEMENT: Final[str] = (
    "WITH removed AS ("
    "DELETE FROM derived_artifact WHERE owner_client_id = ANY (%s::UUID[]) RETURNING 1"
    ") SELECT count(*) FROM removed"
)

# The Events, which go before the Sessions they were recorded in. This is the one
# reference of the four that migration 017 left enforced, and it is the reason this
# statement cannot be moved after the next one.
DELETE_EVENTS_STATEMENT: Final[str] = (
    "WITH removed AS ("
    "DELETE FROM ledger WHERE client_id = ANY (%s::UUID[]) RETURNING 1"
    ") SELECT count(*) FROM removed"
)

# The Sessions, last. The parent-Session and spawning-Event references are no longer
# enforced, so a whole tenant's Session tree leaves in one statement whatever order
# the scan reaches its rows in.
DELETE_SESSIONS_STATEMENT: Final[str] = (
    "WITH removed AS ("
    "DELETE FROM session WHERE client_id = ANY (%s::UUID[]) RETURNING 1"
    ") SELECT count(*) FROM removed"
)

# The whole reset, as the table it names and the statement that empties it, in the
# order they are issued. Held as one sequence rather than as a body of calls so the
# order is a value a reader and a test can both read.
RESET_ORDER: Final[tuple[tuple[str, str, int], ...]] = (
    ("working_memory", DELETE_WORKING_STATEMENT, 1),
    ("embedding", DELETE_EMBEDDINGS_STATEMENT, 1),
    ("lineage_edge", DELETE_EDGES_STATEMENT, 4),
    ("client_binding", DELETE_BINDINGS_STATEMENT, 3),
    ("derived_artifact", DELETE_DERIVED_STATEMENT, 1),
    ("ledger", DELETE_EVENTS_STATEMENT, 1),
    ("session", DELETE_SESSIONS_STATEMENT, 1),
)


class ResetRefusedError(MoltError):
    """A stored Client carries a seeded slug with an identity the seed did not write.

    Raised rather than reported, and raised before anything is deleted, because the
    alternative is removing a real tenant's memory on the strength of a name
    collision. An operator who meant to reset a seeded corpus learns that this
    cluster is not one; an operator who pointed the verb at the wrong database learns
    it while every row is still there.
    """


@dataclass(frozen=True, slots=True)
class TableRemoval:
    """How many rows one table gave up, as that table's own statement counted them."""

    table: str
    removed: int

    def __post_init__(self) -> None:
        """Refuse a removal that names no table or reports a negative count."""
        if not self.table:
            raise ValueError("a removal names the table it was counted from")
        if self.removed < 0:
            raise ValueError("a removal count cannot be negative")


@dataclass(frozen=True, slots=True)
class ResetReport:
    """What one reset removed: the tenants it targeted and the count per table."""

    client_slugs: tuple[str, ...]
    removals: tuple[TableRemoval, ...]

    @property
    def total(self) -> int:
        """How many rows went in all, across every table the reset names."""
        return sum(removal.removed for removal in self.removals)

    @property
    def counts(self) -> dict[str, int]:
        """The per-table counts, keyed by table name."""
        return {removal.table: removal.removed for removal in self.removals}

    def as_document(self) -> JsonObject:
        """The report in the shape the verb's one machine-readable object carries."""
        document: JsonObject = {
            "clients": list(self.client_slugs),
            "rows_removed": self.total,
            "per_table": dict(self.counts),
        }
        return document

    def lines(self) -> tuple[str, ...]:
        """One narration line per table, in the order the statements were issued."""
        return tuple(
            f"reset removed {removal.removed} row(s) from {removal.table}"
            for removal in self.removals
        )


def seeded_slugs() -> tuple[str, ...]:
    """Every slug the corpus definition names, which is the whole scope of a reset.

    All of them rather than the ones the current volumes would write, because a
    corpus seeded at a larger tenant count is still the corpus this reset removes.
    """
    return tuple(domain.slug for domain in DOMAINS)


def reset_corpus(store: MemoryStore, *, slugs: Sequence[str] | None = None) -> ResetReport:
    """Remove the seeded corpus of the definition's tenants and report what went.

    The tenants are resolved first, in a read that frames no transaction, and their
    identity is checked against the definition before anything is deleted. The whole
    delete is then one serializable transaction: the seven statements commit together
    or not at all, so a reset never leaves half a corpus behind.

    Args:
        store: The connection surface the read and the transaction are framed by.
        slugs: The tenant slugs to remove, defaulting to every slug the corpus
            definition names. A caller narrowing this narrows the scope; nothing
            widens it, because a slug the definition does not name resolves to no
            domain to check an identity against.

    Returns:
        The tenants that were found and the count of rows each table gave up. A
        cluster holding none of the slugs reports no tenant and zero everywhere, and
        issues no delete.

    Raises:
        ResetRefusedError: A stored Client carries a seeded slug with another
            identity. Nothing was deleted.
        KeyError: A caller named a slug the corpus definition does not hold.
        StoreError: A statement reported no count where one was required.
    """
    wanted = seeded_slugs() if slugs is None else tuple(slugs)
    definitions = {slug: _domain_named(slug) for slug in wanted}
    found = _seeded_clients(store, wanted)
    for slug, identity in found.items():
        _require_seeded(definitions[slug], identity)
    if not found:
        return ResetReport(
            client_slugs=(),
            removals=tuple(TableRemoval(table=table, removed=0) for table, _, _ in RESET_ORDER),
        )

    identifiers = [identity.id for identity in found.values()]

    def body(cursor: Cursor) -> tuple[TableRemoval, ...]:
        counted: list[TableRemoval] = []
        for table, statement, arity in RESET_ORDER:
            cursor.execute(statement, tuple([identifiers] * arity))
            counted.append(TableRemoval(table=table, removed=_counted(cursor, table)))
        return tuple(counted)

    removals = store.in_serializable(body, label=RESET_LABEL)
    return ResetReport(client_slugs=tuple(found), removals=removals)


# ---------------------------------------------------------------------------
# Resolving the tenants, and refusing one that is not the seed's
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StoredIdentity:
    """One stored Client as the lookup reports it, before it is checked."""

    id: UUID
    slug: str
    display_name: str
    jurisdiction: str
    content_markers: tuple[str, ...]


def _domain_named(slug: str) -> ClientDomain:
    """The corpus definition's domain for a slug, refusing one it does not name."""
    for domain in DOMAINS:
        if domain.slug == slug:
            return domain
    raise KeyError(f"the corpus definition names no tenant with the slug {slug!r}")


def _seeded_clients(store: MemoryStore, slugs: Sequence[str]) -> dict[str, _StoredIdentity]:
    """Look up every stored Client carrying one of the slugs, keyed by slug."""

    def body(cursor: Cursor) -> dict[str, _StoredIdentity]:
        cursor.execute(SELECT_SEEDED_CLIENTS_STATEMENT, (list(slugs),))
        resolved: dict[str, _StoredIdentity] = {}
        for row in cursor.fetchall():
            if len(row) != _CLIENT_ROW_WIDTH:
                raise StoreError("the tenant lookup reported a row of the wrong width")
            identity = _StoredIdentity(
                id=_as_uuid(row[0]),
                slug=str(row[1]),
                display_name=str(row[2]),
                jurisdiction=str(row[3]),
                content_markers=_as_markers(row[4]),
            )
            resolved[identity.slug] = identity
        return resolved

    return store.read(body)


def _require_seeded(domain: ClientDomain, identity: _StoredIdentity) -> None:
    """Refuse a stored Client whose identity is not the one the definition declares."""
    matches = (
        identity.display_name == domain.display_name
        and identity.jurisdiction == domain.jurisdiction
        and identity.content_markers == domain.content_markers
    )
    if not matches:
        raise ResetRefusedError(
            f"the stored client {domain.slug!r} carries an identity the seed did not "
            "write, so this corpus is not a seeded one and nothing was removed"
        )


def _counted(cursor: Cursor, table: str) -> int:
    """The aggregate count one delete statement computed for itself."""
    row = cursor.fetchone()
    if row is None:
        raise StoreError(f"the reset of {table} reported no count of what it removed")
    return int(str(row[0]))


def _as_uuid(value: object) -> UUID:
    """One identifier, however the driver rendered it."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _as_markers(value: object) -> tuple[str, ...]:
    """The stored content markers as a tuple, however the driver rendered the array."""
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()
