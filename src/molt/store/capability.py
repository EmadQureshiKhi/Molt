"""The capability record: what the cluster reported, probed once and read once.

A capability is a platform fact somebody measured. Every degradation in this
design turns on one of them, and four claims arrange this module so a caller
cannot turn on anything else.

**A branch is driven by a probe result, never by a cluster version string.** A
version string says what a build is called; a probe says what this cluster, on
this tier, under this connection, actually did when asked. Those come apart
exactly where it matters: a tier that carries the same version as another may
still reject the statement the design wants, and a statement that succeeds is
proof no name can give. So nothing here reads a version, nothing anywhere else
needs to, and every row this module writes is the recorded outcome of a statement
that was really sent.

**The record is read once and held, and holding it is explicit rather than
incidental.** The record is a handful of rows that change when an operator
reprovisions, not between two queries, so a component reads it at process start
and consults what it holds afterwards. The holding lives on the store instance
rather than in module state: a caller may prime it with a record of its own, ask
for it to be re-read, or ask what is already held without spending a round trip.
That last form is what a query path uses, because a nearest-neighbour query on
the agent's critical path must not pay a read to discover which statement to
send, and because the reading roles that run those queries are not all granted
`SELECT` on this table.

**An unprobed fact and a fact probed absent are different answers, and the
difference decides a fallback.** A row reporting `available` false is a cluster
that was asked and said no, which is what a fallback path exists for. No row at
all is a cluster nobody asked, which is not evidence of absence: the delivered
tier carries every one of these capabilities, so an unprobed fact leaves the
primary path in place rather than degrading silently to a fallback nobody
chose. The one exception is the garbage-collection horizon, where the historical
read module treats absent and unavailable alike, because there a missing answer
would otherwise be replaced by an assumed interval.

**A detail column records a measurement or a name, never a value that could carry
a secret and never a message the cluster composed.** The horizon detail is a
count of seconds in the exact form the historical read module parses. The vector
index detail is the operator class the cluster reported, because which ordering
the index serves is what makes unit normalisation at write time load-bearing. The
backup detail is the storage scheme of the target that was planned, and not the
target itself: a backup target may carry credentials in its query parameters, so
neither the target nor any refusal text mentioning it is recorded or logged, and
the probe reports the fault by name instead.

Three probes are implemented here, each reading the cluster and recording one
row. The zone-configuration probe measures the collection horizon the historical
read is bounded by. The index-definition probe records what the cluster reports
about the vector index and the operator class it serves, which is a reading of
the index rather than a claim about any query plan. The backup probe asks the
cluster to plan a `BACKUP INTO` against the operator-owned target without running
it, which is what a tier refusing user-issued backups refuses; a target that is
unreachable at run time surfaces then, on the Backup_Manager's own statement, and
is not something a planning probe can answer. The remaining rows come from the
components that own their facts, each recording through the statement and the
names declared here, so no other module spells a capability name.

Every statement here is a whole module-level literal. The name and the detail of a
recorded row and the backup target of the planning probe are bound parameters; the
table and index names the two introspection statements carry are this module's own
literals and no caller value reaches statement text anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from molt.errors import StoreError
from molt.store import Cursor, MemoryStore
from molt.store.historical import GC_HORIZON_CAPABILITY
from molt.telemetry import Severity, log

__all__ = [
    "BACKUP_PLAN_QUERY",
    "CHANGEFEED",
    "COMPONENT",
    "GC_HORIZON_SECONDS",
    "INDEX_DEFINITION_QUERY",
    "ON_DEMAND_BACKUP",
    "PROBED_CAPABILITIES",
    "RANGEFEED_SETTING",
    "RECORD_CAPABILITY_STATEMENT",
    "RECORD_QUERY",
    "SELF_MANAGED_BACKUP",
    "TEXT_PROVIDER_PROMPT_CACHE",
    "VECTOR_INDEX",
    "VECTOR_INDEX_COLUMN",
    "VECTOR_INDEX_NAME",
    "ZONE_CONFIGURATION_QUERY",
    "Capability",
    "CapabilityRecord",
    "capabilities",
    "probe_gc_horizon",
    "probe_platform",
    "probe_self_managed_backup",
    "probe_vector_index",
    "record_capability",
    "select_capabilities",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "store"

# The name of every probed platform fact. They are spelled here once, so a
# component that records one and a component that branches on it cannot disagree
# about the spelling, and the horizon name is imported from the module that reads
# it rather than restated.
VECTOR_INDEX: Final[str] = "vector_index"
CHANGEFEED: Final[str] = "changefeed"
RANGEFEED_SETTING: Final[str] = "rangefeed_setting"
GC_HORIZON_SECONDS: Final[str] = GC_HORIZON_CAPABILITY
SELF_MANAGED_BACKUP: Final[str] = "self_managed_backup"
ON_DEMAND_BACKUP: Final[str] = "on_demand_backup"
TEXT_PROVIDER_PROMPT_CACHE: Final[str] = "text_provider_prompt_cache"

# Every fact the design expects a row for, in the order the record reports them.
# A name absent from a read record is an unprobed fact rather than an absent
# capability, and this is what a caller lists the unprobed ones against.
PROBED_CAPABILITIES: Final[tuple[str, ...]] = (
    CHANGEFEED,
    GC_HORIZON_SECONDS,
    ON_DEMAND_BACKUP,
    RANGEFEED_SETTING,
    SELF_MANAGED_BACKUP,
    TEXT_PROVIDER_PROMPT_CACHE,
    VECTOR_INDEX,
)

# The whole record, in one statement, ordered so two reads of one cluster report
# the same sequence.
RECORD_QUERY: Final[str] = "SELECT name, available, detail FROM capability ORDER BY name ASC"

# The one write. The reading instant is the cluster's own rather than a value a
# caller supplies, because a probe result is evidence about when the cluster was
# asked, and re-probing replaces the row rather than adding a second answer for
# the same fact.
RECORD_CAPABILITY_STATEMENT: Final[str] = (
    "UPSERT INTO capability (name, available, detail, checked_at) VALUES (%s, %s, %s, now())"
)

# The zone-configuration read the horizon is measured from. The Ledger is the
# table named because the Ledger is what a historical read reads: the horizon
# that bounds a point-in-time count is the one governing the table the count is
# taken over, and a table inheriting the cluster default reports that default
# here.
ZONE_CONFIGURATION_QUERY: Final[str] = (
    "SELECT raw_config_sql FROM [SHOW ZONE CONFIGURATION FROM TABLE ledger]"
)

# The index definition read back from the cluster, and the index and column the
# reading is required to name. Reading the definition is what makes the recorded
# operator class the cluster's own report rather than the operator class this
# codebase asked for.
INDEX_DEFINITION_QUERY: Final[str] = "SELECT create_statement FROM [SHOW CREATE TABLE embedding]"
VECTOR_INDEX_NAME: Final[str] = "embedding_vec_idx"
VECTOR_INDEX_COLUMN: Final[str] = "vec"

# The backup probe. The target is a bound parameter, which is what keeps a target
# carrying credentials in its query parameters out of statement text, and the
# planning form is what makes the probe cost no data movement and create no job.
BACKUP_PLAN_QUERY: Final[str] = "EXPLAIN BACKUP INTO %s"

# How the collection interval appears in a zone configuration. The pattern is
# ASCII-only, because the unrestricted digit class would admit digits from other
# scripts that no count parser reads as numbers, and the value is required to end
# where the number ends, so a fractional value is read as no reading at all
# rather than silently truncated to the whole part of itself.
_HORIZON_SETTING: Final[re.Pattern[str]] = re.compile(
    r"gc\.ttlseconds\s*=\s*(?P<seconds>\d+)(?![\d.])", re.ASCII
)

# How the cluster reports the vector index inside a table definition, anchored on
# this schema's own index name so no other index of the table can answer for it.
_INDEX_FORM: Final[re.Pattern[str]] = re.compile(
    r"VECTOR\s+INDEX\s+"
    + re.escape(VECTOR_INDEX_NAME)
    + r"\s*\(\s*(?P<column>\w+)\s+(?P<operator_class>\w+)\s*\)",
    re.ASCII | re.IGNORECASE,
)

# How many columns each read returns, checked before a row is decoded so a
# statement and its decoder cannot drift apart silently.
_RECORD_ROW_WIDTH: Final[int] = 3
_SINGLE_COLUMN: Final[int] = 1

# The label the capability write appears under in a log record and in the note an
# exhausted retry attaches.
_RECORD_LABEL: Final[str] = "capability_record"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capability:
    """One probed platform fact, in the form it is both written and read in.

    The same shape carries a probe's outcome to the recording statement and a
    stored row back to a caller, because a capability row is exactly a name, an
    answer, and a measurement, and giving the two directions separate shapes
    would let them disagree about which is which.

    Attributes:
        name: The fact this row answers for, one of the names this module spells.
        available: Whether the probe answered in the affirmative. False means the
            cluster was asked and the answer was no, or that the probe could not
            complete; either way no caller may take the primary path on it.
        detail: The measurement or the name the probe recorded, or None where it
            recorded neither. Never a value that could carry a secret and never a
            message the cluster composed.
    """

    name: str
    available: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        """Refuse a row that answers for no named fact."""
        if not self.name:
            raise ValueError("a capability row records the name of the fact it answers for")


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    """Every capability row one cluster holds, as one reading of it.

    An empty record is the honest reading of a cluster nobody has probed, and it
    is what a store hands out before anything has been read. Every accessor
    treats it as such: nothing is reported available, nothing is reported absent,
    and every name is reported unprobed.
    """

    entries: tuple[Capability, ...] = ()

    def of(self, name: str) -> Capability | None:
        """The row for one fact, or None when this cluster holds none."""
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def probed(self, name: str) -> bool:
        """Whether this cluster was asked about one fact at all."""
        return self.of(name) is not None

    def available(self, name: str) -> bool:
        """Whether one fact was probed and answered in the affirmative.

        An unprobed fact reads false here, so a caller asking this question is
        never told a capability is present on the strength of nobody having
        looked. A caller deciding whether to degrade asks `unavailable` instead,
        because the two questions are not each other's negation.
        """
        entry = self.of(name)
        return entry is not None and entry.available

    def unavailable(self, name: str) -> bool:
        """Whether one fact was probed and answered in the negative.

        This is the question a fallback turns on: a cluster that was asked and
        said no. An unprobed fact reads false, which leaves the primary path in
        place rather than degrading on the strength of a missing row.
        """
        entry = self.of(name)
        return entry is not None and not entry.available

    def detail(self, name: str) -> str | None:
        """What one probe recorded, or None when it recorded nothing.

        The horizon's detail is a count of seconds, and turning it into a horizon
        belongs to the historical read module, which owns the form that count
        takes and the refusal for every other form.
        """
        entry = self.of(name)
        return None if entry is None else entry.detail

    @property
    def unprobed(self) -> tuple[str, ...]:
        """The facts the design expects an answer for and this cluster holds none for."""
        return tuple(name for name in PROBED_CAPABILITIES if not self.probed(name))

    @property
    def vector_index(self) -> bool:
        """Whether the cluster reported a distributed vector index over the vectors."""
        return self.available(VECTOR_INDEX)

    @property
    def vector_index_operator_class(self) -> str | None:
        """The ordering the reported index serves, or None where none was reported.

        This is the fact that makes write-time unit normalisation load-bearing
        rather than defensive: the reported class orders by squared distance while
        every threshold in this design is expressed in cosine space, and those two
        orderings coincide only over unit vectors.
        """
        return self.detail(VECTOR_INDEX) if self.vector_index else None

    @property
    def changefeed(self) -> bool:
        """Whether the cluster served a sinkless changefeed when one was opened."""
        return self.available(CHANGEFEED)

    @property
    def rangefeed_setting(self) -> bool:
        """Whether the cluster setting a changefeed depends on reads enabled."""
        return self.available(RANGEFEED_SETTING)

    @property
    def self_managed_backup(self) -> bool:
        """Whether the cluster admits a self-managed backup to the operator's target."""
        return self.available(SELF_MANAGED_BACKUP)

    @property
    def on_demand_backup(self) -> bool:
        """Whether the control plane offers an on-demand backup creation operation.

        Recorded false on the delivered control plane, which offers backup listing
        and backup configuration and nothing that creates one, which is why the
        self-managed path is the primary one.
        """
        return self.available(ON_DEMAND_BACKUP)

    @property
    def text_provider_prompt_cache(self) -> bool:
        """Whether the configured text model reported prompt-cache support itself."""
        return self.available(TEXT_PROVIDER_PROMPT_CACHE)


# ---------------------------------------------------------------------------
# Reading the record
# ---------------------------------------------------------------------------


def select_capabilities(cursor: Cursor) -> CapabilityRecord:
    """Read every capability row on a caller's cursor."""
    cursor.execute(RECORD_QUERY)
    return CapabilityRecord(tuple(_capability_of(row) for row in cursor.fetchall()))


def capabilities(store: MemoryStore) -> CapabilityRecord:
    """Read the whole capability record on a leased connection, framing no transaction.

    This is the read the store performs once and holds. A caller wanting what is
    already held asks the store for it instead, which costs no round trip.
    """

    def body(cursor: Cursor) -> CapabilityRecord:
        return select_capabilities(cursor)

    return store.read(body)


# ---------------------------------------------------------------------------
# Recording a probe result
# ---------------------------------------------------------------------------


def record_capability(store: MemoryStore, probed: Capability) -> Capability:
    """Write one probe result, replacing any earlier answer for the same fact.

    Replacing rather than appending is deliberate: a capability row answers what
    this cluster does now, and two answers for one fact would leave every reader
    to decide which of them to believe. The reading instant comes from the cluster
    with the write, so the row records when it was asked.

    Args:
        store: The connection surface the write is framed by.
        probed: The outcome to record.

    Returns:
        The outcome that was written, unchanged, so a probe reads as one
        expression.
    """

    def body(cursor: Cursor) -> None:
        cursor.execute(
            RECORD_CAPABILITY_STATEMENT,
            (probed.name, probed.available, probed.detail),
        )

    store.in_serializable(body, label=_RECORD_LABEL)
    log(
        Severity.INFO,
        COMPONENT,
        "recorded a probed platform capability",
        capability=probed.name,
        available=probed.available,
    )
    return probed


# ---------------------------------------------------------------------------
# The probes
# ---------------------------------------------------------------------------


def probe_gc_horizon(store: MemoryStore) -> Capability:
    """Measure the collection horizon from the zone configuration and record it.

    The horizon is the interval the cluster still holds superseded versions for,
    and so the distance a historical read can reach back. It is measured rather
    than assumed because it is a property of this cluster: the delivered value is
    far shorter than a typical default and shorter than the evidence lifetime of
    a certificate, which is why the derived count mechanism is the primary
    evidence and a historical read is corroboration.

    The recorded detail is the measured count of seconds written as ASCII digits
    and nothing else, which is the one form the historical read module reads. A
    configuration naming no interval, or naming one that reaches no distance, is
    recorded as unavailable with no detail, because a horizon nobody measured must
    refuse a historical read rather than stand in for one.
    """
    measured = _measured_horizon(store)
    if measured is None:
        return record_capability(store, Capability(GC_HORIZON_SECONDS, available=False))
    return record_capability(
        store,
        Capability(GC_HORIZON_SECONDS, available=True, detail=str(measured)),
    )


def probe_vector_index(store: MemoryStore) -> Capability:
    """Read the vector index definition back from the cluster and record it.

    What is recorded is what the cluster reports about the index and the operator
    class it serves, which is a reading of the index and not a claim about any
    query plan: whether a particular filtered search is served by the index is
    the optimiser's decision on the day, while the presence of the index and its
    ordering are facts the cluster states. The neighbour query needs exactly the
    latter, because an absent index is what the bounded exact scan exists for.

    A definition reporting no vector index over the vector column, and a read that
    could not complete, are both recorded as unavailable, which is what puts the
    neighbour query on the fallback path.
    """
    reported = _reported_operator_class(store)
    if reported is None:
        return record_capability(store, Capability(VECTOR_INDEX, available=False))
    return record_capability(store, Capability(VECTOR_INDEX, available=True, detail=reported))


def probe_self_managed_backup(store: MemoryStore, *, target: str) -> Capability:
    """Ask the cluster to plan a backup into the operator's target and record it.

    The statement is planned and not run, so the probe moves no data, creates no
    job, and leaves nothing behind. What it answers is whether this cluster admits
    a user-issued backup at all, which is the question the Backup_Manager's path
    choice turns on: a tier that refuses user-issued backups refuses to plan one.
    What it deliberately does not answer is whether the target is reachable and
    writable, because planning contacts no storage; that surfaces on the
    Backup_Manager's own statement before the first mutation of a run, and is
    recorded there.

    The target is bound rather than written into statement text, and the recorded
    detail is its storage scheme alone. Both follow from the same fact: a backup
    target may carry credentials in its query parameters, so the target never
    reaches statement text, a log record, or the detail column, and a refusal is
    reported by the fault's name rather than by the message the cluster composed
    around the target.

    Args:
        store: The connection surface the planning read is leased from.
        target: The operator-owned backup target, as the deployment configured it.

    Returns:
        The recorded outcome, whose detail is the target's storage scheme.

    Raises:
        ValueError: The target names no storage scheme, so there is nothing to
            plan against. The refusal names the fault and not the target.
    """
    scheme = urlsplit(target).scheme.lower()
    if not scheme:
        raise ValueError(
            "a self-managed backup target names the storage scheme it writes through, "
            "and the configured target names none"
        )

    def body(cursor: Cursor) -> None:
        cursor.execute(BACKUP_PLAN_QUERY, (target,))
        cursor.fetchall()

    try:
        store.read(body)
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "the cluster would not plan a self-managed backup, so the referenced path is left",
            scheme=scheme,
            error_type=type(error).__name__,
        )
        return record_capability(
            store,
            Capability(SELF_MANAGED_BACKUP, available=False, detail=scheme),
        )
    return record_capability(
        store,
        Capability(SELF_MANAGED_BACKUP, available=True, detail=scheme),
    )


def probe_platform(store: MemoryStore, *, backup_target: str | None = None) -> CapabilityRecord:
    """Run the probes this module owns, record each, and return the fresh record.

    This is the process-start entry point: the probes run, their rows land, and
    the record the store holds afterwards is the one they produced rather than one
    read before them. The backup probe runs only where a target is configured,
    because there is no target-free form of the question it asks; where none is
    configured the backup row is left unprobed, which reads as neither present nor
    absent.
    """
    probe_vector_index(store)
    probe_gc_horizon(store)
    if backup_target is not None:
        probe_self_managed_backup(store, target=backup_target)
    return store.capabilities(refresh=True)


# ---------------------------------------------------------------------------
# The readings the probes take
# ---------------------------------------------------------------------------


def _measured_horizon(store: MemoryStore) -> int | None:
    """The collection interval the zone configuration names, or None when it names none.

    A read that could not complete is reported and answers None rather than
    raising, because a probe that cannot measure records an unavailable fact: that
    is a recorded answer a later refusal can name, whereas a raised failure would
    leave the row absent and the reason nowhere.
    """
    try:
        configured = _one_text(store, ZONE_CONFIGURATION_QUERY)
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "the cluster collection interval could not be read, so no horizon is recorded",
            error_type=type(error).__name__,
        )
        return None
    if configured is None:
        return None
    found = _HORIZON_SETTING.search(configured)
    if found is None:
        log(
            Severity.WARNING,
            COMPONENT,
            "the zone configuration names no collection interval, so no horizon is recorded",
        )
        return None
    seconds = int(found.group("seconds"), 10)
    if seconds <= 0:
        log(
            Severity.WARNING,
            COMPONENT,
            "the collection interval reaches no distance, so no horizon is recorded",
            measured_seconds=seconds,
        )
        return None
    return seconds


def _reported_operator_class(store: MemoryStore) -> str | None:
    """The operator class the reported vector index serves, or None when none is reported."""
    try:
        definition = _one_text(store, INDEX_DEFINITION_QUERY)
    except Exception as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "the vector index definition could not be read, so the index is recorded absent",
            error_type=type(error).__name__,
        )
        return None
    if definition is None:
        return None
    found = _INDEX_FORM.search(definition)
    if found is None or found.group("column").lower() != VECTOR_INDEX_COLUMN:
        log(
            Severity.WARNING,
            COMPONENT,
            "the cluster reports no vector index over the embedding column",
            index_name=VECTOR_INDEX_NAME,
        )
        return None
    return found.group("operator_class")


def _one_text(store: MemoryStore, query: str) -> str | None:
    """Read one text column of one row, refusing a result of any other shape."""

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(query)
        return cursor.fetchone()

    row = store.read(body)
    if row is None:
        return None
    if len(row) != _SINGLE_COLUMN:
        raise StoreError(
            f"an introspection read returned {len(row)} column(s) where {_SINGLE_COLUMN} is read"
        )
    value = row[0]
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoreError(
            f"an introspection read returned {type(value).__name__} where text was read"
        )
    return value


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------


def _capability_of(row: Sequence[object]) -> Capability:
    """Build one capability from a stored row, refusing every other shape."""
    if len(row) != _RECORD_ROW_WIDTH:
        raise StoreError(
            f"the capability read returned {len(row)} column(s) where {_RECORD_ROW_WIDTH} are read"
        )
    name, available, detail = row[0], row[1], row[2]
    if not isinstance(name, str):
        raise StoreError("the capability column name did not return text")
    if not isinstance(available, bool):
        raise StoreError("the capability column available did not return a boolean")
    if detail is not None and not isinstance(detail, str):
        raise StoreError("the capability column detail did not return text")
    return Capability(name=name, available=available, detail=detail)
