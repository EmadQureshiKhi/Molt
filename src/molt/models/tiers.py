"""The Memory_Tier taxonomy, encoded once.

Every stored row belongs to exactly one Memory_Tier. A tier exists because its
content carries a different mutability contract and leans on a different
CockroachDB capability, and those two facts together are what make the cluster
the agent's memory rather than its logfile.

This module is the single place that taxonomy is encoded. The console tier view
reads this mapping to render its descriptive columns, and the generator that
produces the memory-tier documentation reads the same mapping to emit its
table, so the design table, the rendered view, and the documentation cannot
state three different taxonomies.

The mapping is immutable at the type level as well as at runtime: the table is
exposed as a read-only mapping over frozen specifications whose table lists are
tuples, so neither the table nor anything reachable from it can be rebound by a
caller and the strict type check refuses an attempt. Iteration follows declared
order, which is fixed by the declaration below rather than by any hash, so the
view and the documentation list the tiers in the same order in every process.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

__all__ = ["MEMORY_TIERS", "TIER_NAMES", "WORKING_TIER", "MemoryTierSpec"]

# The disposable tier. Named as a constant because the console renders two
# expiry figures for this tier alone, and a consumer needs to recognise it
# without matching on prose.
WORKING_TIER: Final[str] = "working"


@dataclass(frozen=True, slots=True)
class MemoryTierSpec:
    """One tier's four descriptive columns.

    Attributes:
        name: The tier name, as stored and as rendered.
        tables: The table identifiers the tier holds, in declared order.
        table_note: The qualification on the table list, where the tier holds
            something narrower than whole rows of every table named, and None
            where the table list needs no qualification.
        mutability: The tier's mutability contract, leading with the short
            summary that names the contract and continuing with what enforces
            it.
        capability: The CockroachDB capability the tier relies on.
    """

    name: str
    tables: tuple[str, ...]
    table_note: str | None
    mutability: str
    capability: str


# Declared order is rendering order. The mapping is keyed from each entry's own
# name so a key and a spec name cannot disagree.
_SPECS: Final[tuple[MemoryTierSpec, ...]] = (
    MemoryTierSpec(
        name="episodic",
        tables=("ledger",),
        table_note=None,
        mutability=(
            "Append-only. No role holds UPDATE; rows leave only by an authorised erasure "
            "or by Row-Level TTL"
        ),
        capability=(
            "SERIALIZABLE isolation, so sequence assignment and digest computation happen "
            "inside the inserting statement; TIMESTAMPTZ and JSONB native types"
        ),
    ),
    MemoryTierSpec(
        name="attribution",
        tables=("client_binding",),
        table_note="held as an Attribution_Version history",
        mutability=(
            "Append-only with closure. Detection method, confidence, Artifact, and Client "
            "are immutable on a stored version; only the validity end and the superseding "
            "reference are ever written, and both exactly once"
        ),
        capability=(
            "SERIALIZABLE isolation, so closing the old version and inserting the new one "
            "is one atomic supersession; partial and covering indexes serving the as-of "
            "query inside its one-second bound"
        ),
    ),
    MemoryTierSpec(
        name="procedural_semantic",
        tables=(
            "derived_artifact",
            "procedure_retrieval",
            "procedure_outcome",
            "procedure_confidence_change",
        ),
        table_note=None,
        mutability=(
            "Revisable. Bodies are surgically rewritten and Procedure_Confidence moves "
            "with recorded outcomes; every change is accompanied by an audited change "
            "record"
        ),
        capability=(
            "Distributed vector index over the VECTOR(1024) column for semantic recall; "
            "column-scoped UPDATE privilege confining what a revision may touch"
        ),
    ),
    MemoryTierSpec(
        name="provenance",
        tables=("lineage_edge", "ledger", "ledger_checkpoint"),
        table_note="of the ledger table this tier holds the Hash_Chain columns only",
        mutability=(
            "Immutable. Edges are inserted and deleted, never edited; chain columns are "
            "never rewritten; checkpoints admit no UPDATE and no DELETE from any role"
        ),
        capability=(
            "Recursive common table expressions for lineage closure; sha256 evaluated "
            "inside the writing statement; referential actions refusing deletions that "
            "would remove audit history"
        ),
    ),
    MemoryTierSpec(
        name="action",
        tables=(
            "erasure_lease",
            "erasure_request",
            "erasure_run",
            "erasure_candidate",
            "residue_candidate",
            "disposition",
            "run_session",
            "backup_record",
            "erasure_certificate",
        ),
        table_note=None,
        mutability=(
            "Write-once evidence, with one current lease per Client. Dispositions and "
            "certificates are inserted and never rewritten; the lease row is the only "
            "mutable member and only its expiry, owner, and generation move"
        ),
        capability=(
            "SERIALIZABLE isolation for monotonic Fencing_Generation assignment; a partial "
            "uniqueness constraint admitting one current lease per Client; ON DELETE "
            "RESTRICT protecting the evidence chain"
        ),
    ),
    MemoryTierSpec(
        name=WORKING_TIER,
        tables=("working_memory",),
        table_note=None,
        mutability=(
            "Disposable. Rows are overwritten freely and physically deleted on expiry; "
            "nothing depends on a working row surviving"
        ),
        capability=(
            "Row-Level TTL with a 3600 second default interval, so expiry is enforced by "
            "the cluster rather than by a scheduled process outside it"
        ),
    ),
)

# The taxonomy itself: a read-only mapping in declared order.
MEMORY_TIERS: Final[Mapping[str, MemoryTierSpec]] = MappingProxyType(
    {spec.name: spec for spec in _SPECS}
)

# The tier set, enumerable without touching the mapping, in the same order.
TIER_NAMES: Final[tuple[str, ...]] = tuple(spec.name for spec in _SPECS)
