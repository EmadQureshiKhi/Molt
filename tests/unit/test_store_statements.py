"""A sweep over the whole data-access layer: every statement, every bound value.

The per-module suites assert what each statement of the store means. This module
asserts the one claim none of them can make alone, and makes it over every
statement the layer holds rather than over a list written out here: every
caller-supplied value reaches the cluster as a bound parameter, and no identifier
and no value is ever interpolated into statement text.

Nothing here opens a socket and nothing here needs a connection string. A
scripted cursor answers each statement and keeps what it was sent, so the claim
is asserted by reading what the modules produced.

The statements are discovered rather than listed. Every module of the store
package is imported and its attributes are read, so a statement constant added to
an existing module, and every statement of a module added later, is swept without
this file being edited. A component outside the package that holds statements of
its own is named alongside it and swept the same way, because the claim is about
every statement a caller's value reaches rather than about a directory. Two levels
of discovery are used, because two different claims are made. Every SQL text the
layer holds, whole statements and the composable terms the neighbour query is
built from alike, is checked for the driver's own parameter marker and for the
absence of every other way a value could be carried. Every whole statement that
binds at least one value is then driven against the scripted cursor, so the claim
rests on statements that really went out with really bound parameters rather than
on reading text.

Four properties are checked.

Each statement carries the driver's own marker and nothing else. There is no
positional marker of another dialect, no named marker, and no formatting
placeholder, so there is no mechanism by which a value could be substituted
anywhere but server-side.

Each statement that binds values binds exactly as many as it holds markers, and
the bound tuple is the values the caller supplied. Hostile text carrying a quote,
a statement terminator, a comment marker, and a newline travels as data through
every operation, and no statement the operation sent holds any of it.

Every statement the sweep sent is one of the discovered module-level literals.
That is what rules out composition at call time: a statement assembled around a
caller's value would not be found among the constants.

The `AS OF SYSTEM TIME` rendering of the historical read is the single exception,
because the cluster resolves that argument while planning and a parameter is
substituted later. It is asserted to be the only statement the layer renders a
value into, and to be admitted only by that module's anchored form check.

What is not swept by execution here is the migration runner. The statements it
sends are the schema's own text, read from the numbered files rather than from a
caller, and its three history statements are driven by the migration suite. Their
text is still swept for the marker claim alongside every other statement.

**Validates: Requirements 30.6, 36.1**
"""

from __future__ import annotations

import json
import pkgutil
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from types import ModuleType
from typing import Final
from uuid import UUID, uuid4

import pytest

import molt.store
from molt.erase.lease import (
    LeaseGrant,
    acquire,
    finalisation_for,
    finalise,
    register_run,
    release,
    renew,
)
from molt.erase.residue import ResiduePolicy
from molt.errors import MissingParentError, StoreError
from molt.mcpserver.tools import (
    ANCESTORS_TOOL,
    DESCENDANTS_TOOL,
    PERMITTED_CLIENT_IDS_STATEMENT,
    SELECT_PERMITTED_ANCESTORS_STATEMENT,
    SELECT_PERMITTED_DESCENDANTS_STATEMENT,
    ToolBackend,
    dispatch,
    permitted_client_ids,
)
from molt.models.artifact import (
    CONFIDENCE_CEILING,
    CONFIDENCE_FLOOR,
    EMBEDDING_DIMENSION,
    ArtifactKind,
    ArtifactRef,
    DerivedArtifact,
    DerivedArtifactKind,
)
from molt.models.binding import BindingMethod
from molt.models.event import EmbeddingState, Event, EventCategory, JsonObject
from molt.models.session import Session, SessionOutcome
from molt.recall import RecallEngine
from molt.store import STATEMENT_TIMEOUT_STATEMENT, Connection, Cursor, MemoryStore
from molt.store.attribution import (
    ATTRIBUTION_AS_OF_QUERY,
    CLOSE_CURRENT_VERSION_STATEMENT,
    CURRENT_ATTRIBUTION_QUERY,
    CURRENT_PAIR_QUERY,
    FIRST_ATTRIBUTION_QUERY,
    INSERT_ERASURE_MARKER_STATEMENT,
    INSERT_SUCCESSOR_STATEMENT,
    INSERT_VERSION_STATEMENT,
    AttributionSubmission,
    SupersessionContext,
    attribution_as_of,
    current_attribution,
    first_attributions,
    remove_attribution,
    write_attribution,
)
from molt.store.binding_detector import (
    PARENT_BINDINGS_QUERY,
    DetectionRequest,
    record_bindings,
)
from molt.store.capability import (
    BACKUP_PLAN_QUERY,
    RECORD_CAPABILITY_STATEMENT,
    VECTOR_INDEX,
    Capability,
    CapabilityRecord,
    probe_self_managed_backup,
    record_capability,
)
from molt.store.chain import (
    APPEND_STATEMENT,
    CHAIN_ROWS_QUERY,
    TIP_QUERY,
    LedgerAppend,
    append,
    chain_rows,
    chain_tip,
)
from molt.store.confidence import (
    ADJUST_STANDING_STATEMENT,
    COUNT_RETRIEVALS_QUERY,
    INSERT_CHANGE_STATEMENT,
    INSERT_OUTCOME_STATEMENT,
    INSERT_RETRIEVAL_STATEMENT,
    OUTCOME_CONTEXT_QUERY,
    SELECT_CHANGES_QUERY,
    SELECT_OUTCOME_COUNTS_QUERY,
    SELECT_STANDING_QUERY,
    apply_outcome,
    change_history,
    procedure_standing,
    record_retrieval,
)
from molt.store.embeddings import (
    INSERT_ARTIFACT_STATEMENT,
    INSERT_EMBEDDING_STATEMENT,
    MARK_STATE_STATEMENT,
    NEAREST_SCAN_STATEMENT,
    NEAREST_STATEMENT,
    PRINCIPAL_SCOPE_QUERY,
    RECALL_SCAN_STATEMENT,
    RECALL_STATEMENT,
    SELECT_PENDING_STATEMENT,
    EmbeddingWrite,
    mark_embedding_state,
    nearest,
    pending_artifacts,
    principal_scope,
    recall_page,
    write_derived_artifact,
    write_embedding,
)
from molt.store.erasure_lease import (
    CLOSE_LEASE_STATEMENT,
    CURRENT_LEASE_QUERY,
    FINALISATION_QUERY,
    HIGHEST_GENERATION_QUERY,
    INSERT_LEASE_STATEMENT,
    MARK_FINALISED_STATEMENT,
    NO_GENERATION,
    RECORD_RUN_KEY_STATEMENT,
    RENEW_LEASE_STATEMENT,
    SURRENDER_LEASE_STATEMENT,
    LeaseInterval,
    LeaseRecord,
)
from molt.store.fencing import (
    ACTIVE_RUN_QUERY,
    CURRENT_GENERATION_QUERY,
    RUNNING_STATUS,
    fenced,
    guarded_binding_write,
)
from molt.store.historical import (
    AS_OF_STATEMENT_PREFIX,
    CAPABILITY_QUERY,
    GC_HORIZON_CAPABILITY,
    GcHorizon,
    gc_horizon,
    historical,
    render_as_of_timestamp,
    require_rendered_form,
)
from molt.store.lineage import (
    INSERT_EDGE_STATEMENT,
    PARENT_EXISTS_STATEMENT,
    SELECT_ANCESTORS_STATEMENT,
    SELECT_DESCENDANTS_STATEMENT,
    ParentRef,
    ancestors_of,
    descendants_of,
    insert_lineage_edge,
)
from molt.store.sessions import (
    BUMP_COUNTERS_FOR_CLIENT_STATEMENT,
    BUMP_COUNTERS_STATEMENT,
    END_SESSION_STATEMENT,
    END_SESSION_WITH_COUNTERS_STATEMENT,
    INSERT_SESSION_STATEMENT,
    SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT,
    SELECT_CHILD_SESSIONS_STATEMENT,
    SELECT_EVENTS_FOR_SESSION_STATEMENT,
    SELECT_SESSION_STATEMENT,
    SELECT_SESSIONS_FOR_CLIENT_STATEMENT,
    CounterDelta,
    SessionCounters,
    artifacts_of_client,
    bump_session_counters,
    child_sessions,
    end_session,
    events_of_session,
    session_of_client,
    sessions_of_client,
    upsert_session,
)
from molt.store.working import (
    PURGE_CLIENT_SCRATCH_STATEMENT,
    SELECT_SCRATCH_STATEMENT,
    SELECT_SESSION_SCRATCH_STATEMENT,
    UPSERT_SCRATCH_STATEMENT,
    ScratchWrite,
    WorkingInterval,
    purge_working_rows,
    read_scratch,
    session_scratch,
    write_scratch,
)

# The one parameter marker the driver substitutes for. Every statement of the
# layer is required to carry this and no other mechanism.
PARAMETER_MARKER: Final[str] = "%s"

# The leading words a statement or a composable term of one may begin with. A
# module attribute beginning with one of these, or holding a parameter marker at
# all, is swept as SQL.
SQL_LEADING_WORDS: Final[frozenset[str]] = frozenset(
    {
        "BEGIN",
        "COMMIT",
        "DELETE",
        "EXPLAIN",
        "INSERT",
        "RELEASE",
        "ROLLBACK",
        "SAVEPOINT",
        "SELECT",
        "SET",
        "UPDATE",
        "UPSERT",
        "WITH",
    }
)

# How a whole statement is named throughout the layer, as against a composable
# term of one. The distinction decides which texts the execution sweep is
# required to drive.
WHOLE_STATEMENT_SUFFIXES: Final[tuple[str, ...]] = ("_STATEMENT", "_QUERY")

# The module whose statements are the schema's own text rather than a caller's,
# and which the migration suite drives.
UNDRIVEN_MODULES: Final[frozenset[str]] = frozenset({"molt.store.migrate"})

# Modules outside the store package that hold statements of their own, and are
# therefore swept alongside it. The tool server is one: its two lineage closures
# carry a tenancy admission and a row bound that the store's unbounded closure must
# not carry, so they are its statements rather than the layer's, and the claim this
# module makes has to reach them because every value in them arrives from a caller
# this server did not write.
MODULES_OUTSIDE_THE_STORE_PACKAGE: Final[tuple[str, ...]] = ("molt.mcpserver.tools",)

# Text a caller might supply that would end a statement, comment out the rest of
# one, or open a second one, if it ever reached statement text. Every operation
# below is driven with these where it takes text at all, and no statement any of
# them sent may hold any of it.
INJECTED_TERMINATOR: Final[str] = "'; DROP TABLE ledger; --"
INJECTED_IDENTIFIER: Final[str] = 'ledger" AS other, (SELECT 1) AS more'
INJECTED_NEWLINE: Final[str] = "first line\nSELECT 1; --"
HOSTILE_VALUES: Final[tuple[str, ...]] = (
    INJECTED_TERMINATOR,
    INJECTED_IDENTIFIER,
    INJECTED_NEWLINE,
)

# An instant with an offset, fixed so no example depends on when it ran.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[datetime] = MOMENT + timedelta(days=90)

# The instant a scripted supersession reports as the closed version's validity
# end, and the instant an as-of attribution read asks about. Distinct from the
# fixed instant, so a bound value carrying one cannot be satisfied by the other.
CLOSED_AT: Final[datetime] = MOMENT + timedelta(seconds=1)
AS_OF: Final[datetime] = MOMENT + timedelta(days=1)

# A digest-shaped value for the columns the schema fixes at sixty-four characters.
DIGEST: Final[str] = "a" * 64

# The bound every statement of a driven store runs under. A value distinct from
# the default, so the assertion that it is bound is not satisfied by a coincidence.
TIMEOUT_MS: Final[int] = 7000

# The bounds the driven reads ask for, each distinct so no expectation is
# satisfied by another read's default.
READ_LIMIT: Final[int] = 37
PENDING_LIMIT: Final[int] = 11
NEIGHBOUR_LIMIT: Final[int] = 5
CANDIDATE_CAP: Final[int] = 512
COSINE_LIMIT: Final[float] = 0.25
TOOL_LIMIT: Final[int] = 19
TOOL_MAX_RESULTS: Final[int] = 50

# The horizon the scripted capability row records, in the form the detail column
# holds it in.
HORIZON_SECONDS: Final[int] = 4500

# The confidences the attribution cases carry: what the pair already holds, a
# stronger submission that supersedes it, and a weaker one saying nothing new.
# Three distinct values, so no expected bound tuple is satisfied by another's.
PRIOR_CONFIDENCE: Final[float] = 0.6
STRONGER_CONFIDENCE: Final[float] = 0.9
WEAKER_CONFIDENCE: Final[float] = 0.3

# What a parent's current claim carries when the driven detection inherits it, and
# the marker the same Client has configured. The marker is the leading token of the
# hostile text below, so the detection really matches: the detector compares
# markers in this process rather than in a predicate, and the text must therefore
# appear in no statement the case sent.
INHERITED_CONFIDENCE: Final[float] = 0.45
MARKER_TERM: Final[str] = "first line"

# How long the driven working row lives, and the aggregate the scripted purge
# reports. Neither is a default of that module, so an expectation about either
# cannot be met by a constant the module holds.
SCRATCH_TTL_SECONDS: Final[int] = 300
PURGED_ROWS: Final[int] = 12

# The generation the scripted lease holds, well above the floor so the admitted
# write is not admitted by a coincidence with it, and the generation a takeover of
# it records. The successor is restated here rather than computed by the module
# whose arithmetic the assertion is about.
CURRENT_GENERATION: Final[int] = 7
TAKEOVER_GENERATION: Final[int] = 8

# How long a scripted lease window runs for. Not the configured default, so an
# expectation about the bound interval cannot be met by a value the surface holds.
LEASE_SECONDS: Final[int] = 45

# The statement a historical read is driven with. It belongs to this module, as
# every caller's statement belongs to the caller's, and it travels unchanged.
CALLER_STATEMENT: Final[str] = "SELECT count(*) FROM derived_artifact WHERE derivation_method = %s"

# The identifiers every driven operation names. Fixed at module scope so the
# expected bound tuples name the same values the operations were given.
SESSION_ID: Final[UUID] = uuid4()
PARENT_SESSION_ID: Final[UUID] = uuid4()
CLIENT_ID: Final[UUID] = uuid4()
OTHER_CLIENT_ID: Final[UUID] = uuid4()
EVENT_ID: Final[UUID] = uuid4()
SPAWNING_EVENT_ID: Final[UUID] = uuid4()
ARTIFACT_ID: Final[UUID] = uuid4()
CHILD_ARTIFACT_ID: Final[UUID] = uuid4()
PARENT_ARTIFACT_ID: Final[UUID] = uuid4()
FIRST_VERSION_ID: Final[UUID] = uuid4()
PRIOR_VERSION_ID: Final[UUID] = uuid4()
SUCCESSOR_VERSION_ID: Final[UUID] = uuid4()
MARKER_VERSION_ID: Final[UUID] = uuid4()
LEASE_ID: Final[UUID] = uuid4()
SUCCESSOR_LEASE_ID: Final[UUID] = uuid4()
RUN_ID: Final[UUID] = uuid4()
PROCEDURE_ID: Final[UUID] = uuid4()
RETRIEVAL_ID: Final[UUID] = uuid4()
OUTCOME_ID: Final[UUID] = uuid4()
CHANGE_ID: Final[UUID] = uuid4()

# What the procedural-standing statements carry. The standing moves from one value
# to another by a delta, and all three are distinct and none is a surface default,
# so no expected bound tuple can be satisfied by another's value or by a constant
# the module might have held instead of the caller's. These operations take no
# caller text at all: every value they bind is an identifier, a classification of a
# fixed set, a number, or an interval bound read from the model.
PRIOR_STANDING: Final[float] = 0.5
RAISED_STANDING: Final[float] = 0.55
STANDING_DELTA: Final[float] = 0.05
PROCEDURE_KIND: Final[str] = DerivedArtifactKind.LEARNED_PROCEDURE.value
HISTORY_LIMIT: Final[int] = 23
RETRIEVAL_TOTAL: Final[int] = 4


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Discovered:
    """One SQL text found on a swept module."""

    module: str
    name: str
    text: str

    @property
    def label(self) -> str:
        """How this text is named in a parametrised identifier and a failure."""
        within = self.module.removeprefix("molt.store.").removeprefix("molt.")
        return f"{within}.{self.name}"

    @property
    def whole(self) -> bool:
        """Whether this is a whole statement rather than a term of one."""
        return self.name.endswith(WHOLE_STATEMENT_SUFFIXES)

    @property
    def placeholders(self) -> int:
        """How many values this text binds."""
        return self.text.count(PARAMETER_MARKER)


def swept_modules() -> tuple[ModuleType, ...]:
    """Every module of the store package, plus the named modules outside it.

    The store package is discovered rather than listed, so a module added there
    later is swept without this file being edited. The modules outside it are named
    because they are the exception: a component holding statements of its own is a
    decision rather than a default, and naming it here is what puts its statements
    under the same claim.
    """
    found: list[ModuleType] = [molt.store]
    for info in sorted(pkgutil.iter_modules(molt.store.__path__), key=lambda item: item.name):
        if info.ispkg:
            continue
        found.append(import_module(f"{molt.store.__name__}.{info.name}"))
    found.extend(import_module(name) for name in MODULES_OUTSIDE_THE_STORE_PACKAGE)
    return tuple(found)


def texts_of(value: object) -> tuple[str, ...]:
    """The strings one module attribute holds, a collection of them included.

    A statement held inside a collection is still a statement, so a module that
    groups several of them is swept as thoroughly as one that names each.
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, tuple | list | frozenset | set):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def looks_like_sql(text: str) -> bool:
    """Whether a string is a statement or a composable term of one."""
    stripped = text.strip()
    if not stripped:
        return False
    if PARAMETER_MARKER in stripped:
        return True
    leading = stripped.split(maxsplit=1)[0].upper().rstrip("(")
    return leading in SQL_LEADING_WORDS


def discover() -> tuple[Discovered, ...]:
    """Every SQL text the store package holds, one entry per distinct text."""
    seen: set[str] = set()
    found: list[Discovered] = []
    for module in swept_modules():
        for name in sorted(dir(module)):
            for text in texts_of(getattr(module, name)):
                if text in seen or not looks_like_sql(text):
                    continue
                seen.add(text)
                found.append(Discovered(module=module.__name__, name=name, text=text))
    return tuple(sorted(found, key=lambda item: item.label))


SQL_TEXTS: Final[tuple[Discovered, ...]] = discover()

# Every whole statement, by text, so a statement the sweep sent can be recognised
# as one of the layer's own literals.
WHOLE_TEXTS: Final[frozenset[str]] = frozenset(item.text for item in SQL_TEXTS if item.whole)

# Every whole statement that binds a value and belongs to a module the execution
# sweep drives. This is what the coverage assertion is made against.
VALUE_CARRYING: Final[tuple[Discovered, ...]] = tuple(
    item
    for item in SQL_TEXTS
    if item.whole and item.placeholders > 0 and item.module not in UNDRIVEN_MODULES
)


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
        """Record the statement, then arm the rows the script answers with."""
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


class ScriptedEmbedder:
    """A query embedder answering one fixed vector, so no provider is reached.

    The tool server's backend holds a Recall_Engine whether or not the driven call
    is the recall one, and an engine needs an embedder. This is that embedder and
    nothing more.
    """

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One unit vector per text, in the input order."""
        return tuple(unit_vector() for _ in texts)


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(
        connect_with=connect_with,
        statement_timeout_ms=TIMEOUT_MS,
        sleep=lambda _: None,
        jitter=lambda low, _: low,
    )


# ---------------------------------------------------------------------------
# The records every driven operation carries
# ---------------------------------------------------------------------------


def unit_vector() -> tuple[float, ...]:
    """A unit-length vector of the fixed width, with its mass on one component."""
    components = [0.0] * EMBEDDING_DIMENSION
    components[0] = 1.0
    return tuple(components)


def rendered_vector(vec: Sequence[float]) -> str:
    """The text form a vector is bound as, rendered here rather than by the module."""
    return "[" + ",".join(repr(float(component)) for component in vec) + "]"


def canonical_json(document: JsonObject) -> str:
    """The canonical text a JSON column is bound as, rendered here independently."""
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


ATTRIBUTION: Final[JsonObject] = {INJECTED_IDENTIFIER: INJECTED_TERMINATOR}
PAYLOAD: Final[JsonObject] = {"tool": INJECTED_TERMINATOR, "path": INJECTED_NEWLINE}
SCRATCH_VALUE: Final[JsonObject] = {
    INJECTED_IDENTIFIER: INJECTED_TERMINATOR,
    "note": INJECTED_NEWLINE,
}

QUERY_VECTOR: Final[tuple[float, ...]] = unit_vector()
PERMITTED_CLIENTS: Final[tuple[UUID, ...]] = (CLIENT_ID, OTHER_CLIENT_ID)
BACKUP_TARGET: Final[str] = "s3://molt-backup" + INJECTED_TERMINATOR

# The tool server's own driven state. The policy is present because the backend
# holds one; the two lineage closures are what the cases below drive.
TOOL_POLICY: Final[ResiduePolicy] = ResiduePolicy(
    auto_include_threshold=0.1,
    review_threshold=0.3,
    query_limit=1,
    top_k=TOOL_MAX_RESULTS,
    excerpt_characters=200,
)

# One invocation's arguments, carrying hostile text under a key the tool declares
# and under two keys it does not. The extra keys are the point: an argument
# attempting to name a client set, and an argument for another tool entirely, must
# reach no statement, and the hostile-value assertion is what shows they did not.
TOOL_ARGUMENTS: Final[JsonObject] = {
    "artifact_ids": [str(ARTIFACT_ID), INJECTED_IDENTIFIER],
    "limit": TOOL_LIMIT,
    "client_ids": [INJECTED_TERMINATOR],
    "query_text": INJECTED_NEWLINE,
}


def tool_backend(store: MemoryStore) -> ToolBackend:
    """The tool server's backend over one scripted store, with its permitted set."""
    return ToolBackend(
        store=store,
        engine=RecallEngine(store, ScriptedEmbedder(), recall_floor=0.5),
        policy=TOOL_POLICY,
        permitted_clients=PERMITTED_CLIENTS,
        max_results=TOOL_MAX_RESULTS,
    )


SESSION_RECORD: Final[Session] = Session(
    id=SESSION_ID,
    client_id=CLIENT_ID,
    agent_cli=INJECTED_TERMINATOR,
    machine_id=INJECTED_IDENTIFIER,
    team_id=INJECTED_TERMINATOR,
    attribution=ATTRIBUTION,
    workspace_path=INJECTED_NEWLINE,
    started_at=MOMENT,
    ended_at=None,
    outcome=SessionOutcome.IN_PROGRESS,
    parent_session_id=PARENT_SESSION_ID,
    spawning_event_id=SPAWNING_EVENT_ID,
    depth=1,
    tool_call_count=1,
    model_request_count=2,
    error_count=3,
    token_count=4,
    cost_usd=Decimal("1.50"),
    halted=False,
    halted_at=None,
    halt_reason=None,
    halt_rule_id=None,
)

LEDGER_APPEND: Final[LedgerAppend] = LedgerAppend(
    event=Event(
        id=EVENT_ID,
        session_id=SESSION_ID,
        client_id=CLIENT_ID,
        category=EventCategory.TOOL_CALL,
        occurred_at=MOMENT,
        agent_cli=INJECTED_TERMINATOR,
        machine_id=INJECTED_IDENTIFIER,
        parent_event_id=None,
        payload=PAYLOAD,
        redacted=False,
        text_body=INJECTED_NEWLINE,
    ),
    expires_at=EXPIRY,
    embedding_state=EmbeddingState.PENDING,
)

ARTIFACT_RECORD: Final[DerivedArtifact] = DerivedArtifact(
    id=ARTIFACT_ID,
    kind=DerivedArtifactKind.SUMMARY,
    owner_client_id=CLIENT_ID,
    body=INJECTED_TERMINATOR,
    content_digest=DIGEST,
    derivation_method=INJECTED_IDENTIFIER,
    revision=2,
    created_at=MOMENT,
    updated_at=MOMENT,
    redacted_at=None,
    embedding_state=EmbeddingState.PENDING,
    expires_at=EXPIRY,
    procedure_confidence=None,
)

EMBEDDING_RECORD: Final[EmbeddingWrite] = EmbeddingWrite(
    artifact_id=ARTIFACT_ID,
    artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
    client_id=CLIENT_ID,
    provider=INJECTED_TERMINATOR,
    model_id=INJECTED_IDENTIFIER,
    vec=QUERY_VECTOR,
    expires_at=EXPIRY,
)

PARENT_REFERENCE: Final[ParentRef] = ParentRef(
    parent_id=PARENT_ARTIFACT_ID,
    parent_kind=ArtifactKind.DERIVED_ARTIFACT,
    derivation_method=INJECTED_IDENTIFIER,
)

COUNTER_DELTA: Final[CounterDelta] = CounterDelta(
    tool_calls=1,
    model_requests=2,
    errors=3,
    tokens=4,
    cost_usd=Decimal("0.50"),
)

TERMINAL_COUNTERS: Final[SessionCounters] = SessionCounters(
    tool_call_count=9,
    model_request_count=8,
    error_count=7,
    token_count=6,
    cost_usd=Decimal("5.25"),
)

HOSTILE_CAPABILITY: Final[Capability] = Capability(
    name=INJECTED_IDENTIFIER,
    available=True,
    detail=INJECTED_TERMINATOR,
)

# The detection result the attribution cases submit, and the repeated one that
# says nothing the current version does not already say. A submission carries no
# text of its own: its method is one of a fixed set and everything else is an
# identifier, an instant, or a number, so the hostile text of this module travels
# through the Session context the supersession Event is recorded within.
SUBMISSION: Final[AttributionSubmission] = AttributionSubmission(
    artifact_id=ARTIFACT_ID,
    artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
    client_id=CLIENT_ID,
    method=BindingMethod.SCOPE,
    confidence=STRONGER_CONFIDENCE,
    detected_at=MOMENT,
)
RESTATEMENT: Final[AttributionSubmission] = AttributionSubmission(
    artifact_id=ARTIFACT_ID,
    artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
    client_id=CLIENT_ID,
    method=BindingMethod.SCOPE,
    confidence=WEAKER_CONFIDENCE,
    detected_at=MOMENT,
)
SUPERSESSION: Final[SupersessionContext] = SupersessionContext(
    session_id=SESSION_ID,
    agent_cli=INJECTED_TERMINATOR,
    machine_id=INJECTED_IDENTIFIER,
    expires_at=EXPIRY,
)

# The Artifact one driven detection runs over, and the one parent whose current
# claims it inherits. The Artifact's text is hostile, which is what makes the
# interpolation assertion meaningful for this operation: the text is evidence the
# detector reads rather than a value it binds, so no statement of the detection may
# hold any of it.
BINDING_REQUEST: Final[DetectionRequest] = DetectionRequest(
    artifact=ArtifactRef(
        id=ARTIFACT_ID,
        kind=ArtifactKind.DERIVED_ARTIFACT,
        client_id=CLIENT_ID,
    ),
    scope_client_id=CLIENT_ID,
    text=INJECTED_NEWLINE,
    parents=(
        ArtifactRef(
            id=PARENT_ARTIFACT_ID,
            kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=OTHER_CLIENT_ID,
        ),
    ),
)

# The working row the driven write stores, keyed by a scratch key that would end a
# statement if it ever reached statement text, and carrying hostile text in a
# document key and in two document values.
SCRATCH_ENTRY: Final[ScratchWrite] = ScratchWrite(
    session_id=SESSION_ID,
    scratch_key=INJECTED_TERMINATOR,
    client_id=CLIENT_ID,
    value=SCRATCH_VALUE,
)
SCRATCH_INTERVAL: Final[WorkingInterval] = WorkingInterval(seconds=SCRATCH_TTL_SECONDS)
SCRATCH_EXPIRY: Final[datetime] = SCRATCH_INTERVAL.expiry_from(MOMENT)

# The rows the scripts answer with, each of the width its statement selects.
COUNTER_ROW: Final[tuple[object, ...]] = (1, 2, 3, 4, Decimal("0.50"))
APPENDED_ROW: Final[tuple[object, ...]] = (1, "b" * 64, "0" * 64, "c" * 64)
PAIR_ROW: Final[tuple[object, ...]] = (
    PRIOR_VERSION_ID,
    BindingMethod.SCOPE.value,
    PRIOR_CONFIDENCE,
)
CLOSED_ROW: Final[tuple[object, ...]] = (
    PRIOR_VERSION_ID,
    ArtifactKind.DERIVED_ARTIFACT.value,
    BindingMethod.SCOPE.value,
    PRIOR_CONFIDENCE,
    MOMENT,
    CLOSED_AT,
)
SCRATCH_ROW: Final[tuple[object, ...]] = (
    SESSION_ID,
    INJECTED_TERMINATOR,
    CLIENT_ID,
    canonical_json(SCRATCH_VALUE),
    MOMENT,
    SCRATCH_EXPIRY,
)
PURGED_ROW: Final[tuple[object, ...]] = (PURGED_ROWS,)
LEASE_ROW: Final[tuple[object, ...]] = (LEASE_ID, INJECTED_IDENTIFIER, CURRENT_GENERATION)


def touch_pool(store: MemoryStore) -> None:
    """Take one connection from the pool and give it back, sending nothing else."""
    with store.lease():
        pass


def drive_scan(store: MemoryStore) -> object:
    """Answer the neighbour query on a tier that probed the index absent."""
    store.prime_capabilities(CapabilityRecord((Capability(VECTOR_INDEX, available=False),)))
    return nearest(
        store,
        QUERY_VECTOR,
        permitted_clients=PERMITTED_CLIENTS,
        limit=NEIGHBOUR_LIMIT,
        max_cosine=COSINE_LIMIT,
        candidate_cap=CANDIDATE_CAP,
    )


# What the recall page is driven with. Four numbers distinct from every other
# read's bounds and from that module's own defaults, so a bound value asserted
# here cannot be satisfied by a default or by another case's number.
RECALL_LIMIT: Final[int] = 9
RECALL_POOL: Final[int] = 640
RECALL_EXCERPT: Final[int] = 480
RECALL_FLOOR: Final[float] = 0.35


def drive_recall(store: MemoryStore) -> object:
    """Answer the recall page on a tier that serves the ordering from the index."""
    return recall_page(
        store,
        QUERY_VECTOR,
        permitted_clients=PERMITTED_CLIENTS,
        limit=RECALL_LIMIT,
        recall_floor=RECALL_FLOOR,
        candidate_pool=RECALL_POOL,
        excerpt_characters=RECALL_EXCERPT,
    )


def drive_recall_scan(store: MemoryStore) -> object:
    """Answer the recall page on a tier that probed the vector index absent."""
    store.prime_capabilities(CapabilityRecord((Capability(VECTOR_INDEX, available=False),)))
    return drive_recall(store)


def supersession_answers() -> tuple[Answer, ...]:
    """The rows a whole supersession consumes: the decision read, both halves, the Event.

    The two halves of a supersession are one operation, so one script answers
    both, and the Ledger Event the supersession appends on the same cursor is
    answered here too rather than left to arm nothing.
    """
    return (
        Answer("SELECT id, method, confidence FROM client_binding", (PAIR_ROW,)),
        Answer("UPDATE client_binding SET valid_to", (CLOSED_ROW,)),
        Answer("greatest(", ((SUCCESSOR_VERSION_ID, STRONGER_CONFIDENCE, CLOSED_AT),)),
        Answer("INSERT INTO ledger", (APPENDED_ROW,)),
    )


def drive_supersession(store: MemoryStore) -> object:
    """Supersede the pair's current version, which sends both halves in one transaction.

    The closing statement and the successor's insert are the two halves of one
    write and cannot be sent apart, so each is a case of its own driven by this.
    """
    return write_attribution(
        store,
        SUBMISSION,
        context=SUPERSESSION,
        version_id=SUCCESSOR_VERSION_ID,
    )


def drive_withdrawal(store: MemoryStore) -> object:
    """Withdraw the pair's claim, which closes the current version and marks it."""
    return remove_attribution(
        store,
        ARTIFACT_ID,
        CLIENT_ID,
        context=SUPERSESSION,
        marker_id=MARKER_VERSION_ID,
    )


def drive_binding_detection(store: MemoryStore) -> object:
    """Detect and record one Artifact's bindings, which reads the parents' claims.

    Two Clients are claimed: the Session's own, at certainty, and the Client the
    parent is bound to, which the same Client's marker also names. Each claim is
    submitted to the attribution write path, and neither pair holds a current
    version, so each takes the plain insert.
    """
    return record_bindings(store, BINDING_REQUEST, context=SUPERSESSION, detected_at=MOMENT)


def guarded_body(_cursor: Cursor) -> None:
    """A fenced body sending no statement of its own.

    The statement under test is the generation read the wrapper sends ahead of the
    body, and a body sending a statement of its own would send one belonging to
    the caller rather than to the layer.
    """


def drive_fence(store: MemoryStore) -> object:
    """Run the fenced wrapper, whose guard read goes out ahead of the body."""
    return fenced(store, CLIENT_ID, CURRENT_GENERATION, guarded_body)


def drive_erasure_guard(store: MemoryStore) -> object:
    """Run the guarded binding wrapper, whose in-flight read goes out ahead of the body.

    The stub answers the in-flight read with no row, so the guard admits the body and
    the statement under test is sent on the write's own cursor. The refusing path is a
    claim about a cluster's ordering rather than about statement text, so it belongs
    to the concurrency suite and not to this sweep.
    """
    return guarded_binding_write(store, CLIENT_ID, guarded_body)


# The rows the procedural-standing statements report. The outcome row carries the
# classification the cluster read off the Session, which is what the insert stores
# rather than the caller's assertion of it, and the diagnostic row reports a pair
# already on the record, so the repeated-report path is driven rather than a refusal.
PROCEDURE_RETRIEVAL_ROW: Final[tuple[object, ...]] = (
    RETRIEVAL_ID,
    PROCEDURE_ID,
    SESSION_ID,
    MOMENT,
)
PROCEDURE_OUTCOME_ROW: Final[tuple[object, ...]] = (
    OUTCOME_ID,
    PROCEDURE_ID,
    SESSION_ID,
    SessionOutcome.SUCCEEDED.value,
    MOMENT,
)
STANDING_ROW: Final[tuple[object, ...]] = (PRIOR_STANDING,)
ADJUSTED_ROW: Final[tuple[object, ...]] = (RAISED_STANDING,)
WRITTEN_CHANGE_ROW: Final[tuple[object, ...]] = (CHANGE_ID, MOMENT)
RECORDED_OUTCOME_ROW: Final[tuple[object, ...]] = (1, SessionOutcome.SUCCEEDED.value, 1, 1)
RETRIEVAL_COUNT_ROW: Final[tuple[object, ...]] = (RETRIEVAL_TOTAL,)

# Fragments the standing answers are matched by, each specific to one statement.
STANDING_FRAGMENT: Final[str] = "SELECT procedure_confidence FROM"
OUTCOME_FRAGMENT: Final[str] = "INSERT INTO procedure_outcome"
ADJUST_FRAGMENT: Final[str] = "UPDATE derived_artifact SET procedure_confidence"
CHANGE_FRAGMENT: Final[str] = "INSERT INTO procedure_confidence_change"
CONTEXT_FRAGMENT: Final[str] = "SELECT (SELECT count(*) FROM derived_artifact"
RETRIEVAL_COUNT_FRAGMENT: Final[str] = "SELECT count(*) FROM procedure_retrieval"


def standing_answers() -> tuple[Answer, ...]:
    """The rows one recorded outcome and its adjustment consume in one transaction.

    The four statements are one operation and cannot be sent apart, so one script
    answers all of them and each is a case of its own driven by this.
    """
    return (
        Answer(STANDING_FRAGMENT, (STANDING_ROW,)),
        Answer(OUTCOME_FRAGMENT, (PROCEDURE_OUTCOME_ROW,)),
        Answer(ADJUST_FRAGMENT, (ADJUSTED_ROW,)),
        Answer(CHANGE_FRAGMENT, (WRITTEN_CHANGE_ROW,)),
    )


def drive_outcome(store: MemoryStore) -> object:
    """Record one succeeded outcome, which reads, records, adjusts, and justifies."""
    return apply_outcome(
        store,
        PROCEDURE_ID,
        SESSION_ID,
        SessionOutcome.SUCCEEDED,
        delta=STANDING_DELTA,
    )


def drive_repeated_outcome(store: MemoryStore) -> object:
    """Record an outcome already on the record, which takes the diagnostic read.

    The insert writes nothing because the arbiter refuses it, so the diagnostic read
    goes out to establish that the refusal is a repeated report rather than a faulty
    one, which is the only path that statement is sent on.
    """
    return drive_outcome(store)


def drive_standing_summary(store: MemoryStore) -> object:
    """Read one procedure's standing, retrieval count, and outcome counts."""
    return procedure_standing(store, PROCEDURE_ID)


# ---------------------------------------------------------------------------
# The lease lifecycle
# ---------------------------------------------------------------------------

# The lease a scripted takeover finds current: another owner's, another attempt's,
# and past its window, which is the one state a takeover is admitted from. Its
# owner and its key are hostile text, so text stored on a lease is seen to travel
# as data through the statements that read it.
HELD_LEASE_ROW: Final[tuple[object, ...]] = (
    LEASE_ID,
    CLIENT_ID,
    INJECTED_IDENTIFIER,
    CURRENT_GENERATION,
    INJECTED_IDENTIFIER,
    MOMENT,
    EXPIRY,
    True,
)

# The same lease as the closing statement returns it, without the cluster's expiry
# verdict, and the successor the granting statement returns.
CLOSED_LEASE_ROW: Final[tuple[object, ...]] = HELD_LEASE_ROW[:-1]
GRANTED_LEASE_ROW: Final[tuple[object, ...]] = (
    SUCCESSOR_LEASE_ID,
    CLIENT_ID,
    INJECTED_TERMINATOR,
    TAKEOVER_GENERATION,
    INJECTED_NEWLINE,
    MOMENT,
    EXPIRY,
)

# The lease every held-lease operation acts through, at the generation the scripted
# guard read reports current, so the fence admits it and the operation's own
# statement goes out behind it.
HELD_GRANT: Final[LeaseGrant] = LeaseGrant(
    lease=LeaseRecord(
        lease_id=LEASE_ID,
        client_id=CLIENT_ID,
        owner=INJECTED_TERMINATOR,
        generation=CURRENT_GENERATION,
        idempotency_key=INJECTED_NEWLINE,
        acquired_at=MOMENT,
        expires_at=EXPIRY,
    )
)

# The lease the renewal and the surrender report back, at the generation they were
# admitted under.
RENEWED_LEASE_ROW: Final[tuple[object, ...]] = (
    LEASE_ID,
    CLIENT_ID,
    INJECTED_TERMINATOR,
    CURRENT_GENERATION,
    INJECTED_NEWLINE,
    MOMENT,
    EXPIRY,
)

# The outcome a finalisation records, and the row a recorded finalisation is read
# back as. The outcome carries hostile text in a key and in a value, because it is
# a caller's document and reaches a column as canonical JSON text.
FINALISATION_RESULT: Final[JsonObject] = {
    INJECTED_IDENTIFIER: INJECTED_TERMINATOR,
    "status": "completed",
}
FINALISED_ROW: Final[tuple[object, ...]] = (
    RUN_ID,
    INJECTED_NEWLINE,
    MOMENT,
    canonical_json(FINALISATION_RESULT),
    CURRENT_GENERATION,
)
RUN_OWNERSHIP_ROW: Final[tuple[object, ...]] = (RUN_ID, INJECTED_NEWLINE, CURRENT_GENERATION)

# The interval every scripted lease write binds.
SCRIPTED_LEASE_INTERVAL: Final[LeaseInterval] = LeaseInterval(seconds=LEASE_SECONDS)

# The guard read the fence sends ahead of every held-lease write, answered with the
# generation the held grant presents.
FENCE_ANSWER: Final[Answer] = Answer(
    "SELECT id, owner, generation FROM erasure_lease",
    (LEASE_ROW,),
)


def takeover_answers() -> tuple[Answer, ...]:
    """The rows one takeover consumes: the current lease, the maximum, both halves.

    A transfer of ownership is one operation sending four statements, so one script
    answers all four, and each of the four is a case of its own driven by it.
    """
    return (
        Answer("expires_at < now() AS expired", (HELD_LEASE_ROW,)),
        Answer("coalesce(max(generation)", ((CURRENT_GENERATION,),)),
        Answer("UPDATE erasure_lease SET superseded_at", (CLOSED_LEASE_ROW,)),
        Answer("INSERT INTO erasure_lease", (GRANTED_LEASE_ROW,)),
    )


def drive_takeover(store: MemoryStore) -> object:
    """Take over an expired lease, which sends the read pair and both write halves."""
    return acquire(
        store,
        CLIENT_ID,
        INJECTED_TERMINATOR,
        INJECTED_NEWLINE,
        interval=SCRIPTED_LEASE_INTERVAL,
        now=MOMENT,
        lease_id=SUCCESSOR_LEASE_ID,
    )


def drive_finalisation(store: MemoryStore) -> object:
    """Finalise a run whose finalisation instant the scripted marking sets."""
    return finalise(store, HELD_GRANT, RUN_ID, FINALISATION_RESULT, now=MOMENT)


# ---------------------------------------------------------------------------
# The cases the sweep drives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One operation driven against the scripted cursor.

    Attributes:
        name: How the case is named in a parametrised identifier.
        statement: The module-level statement the operation is expected to send.
        bound: The parameters that statement is expected to bind, in order.
        run: The operation, invoked on a store over the scripted connection.
        answers: The rows the script answers the operation's statements with.
        raises: The failure the operation is expected to raise, or None.
    """

    name: str
    statement: str
    bound: tuple[object, ...]
    run: Callable[[MemoryStore], object]
    answers: tuple[Answer, ...] = ()
    raises: type[Exception] | None = None


def build_cases() -> tuple[Case, ...]:
    """Every operation the sweep drives, one per value-carrying statement."""
    return (
        Case(
            name="connection_statement_timeout",
            statement=STATEMENT_TIMEOUT_STATEMENT,
            bound=(str(TIMEOUT_MS),),
            run=touch_pool,
        ),
        Case(
            name="chain_append",
            statement=APPEND_STATEMENT,
            bound=(
                SESSION_ID,
                EVENT_ID,
                SESSION_ID,
                CLIENT_ID,
                EventCategory.TOOL_CALL.value,
                MOMENT,
                INJECTED_TERMINATOR,
                INJECTED_IDENTIFIER,
                None,
                canonical_json(PAYLOAD),
                False,
                EVENT_ID,
                SESSION_ID,
                CLIENT_ID,
                EventCategory.TOOL_CALL.value,
                MOMENT,
                INJECTED_TERMINATOR,
                INJECTED_IDENTIFIER,
                None,
                canonical_json(PAYLOAD),
                False,
                INJECTED_NEWLINE,
                EmbeddingState.PENDING.value,
                EXPIRY,
            ),
            run=lambda store: append(store, LEDGER_APPEND),
            answers=(Answer("INSERT INTO ledger", (APPENDED_ROW,)),),
        ),
        Case(
            name="chain_tip",
            statement=TIP_QUERY,
            bound=(SESSION_ID,),
            run=lambda store: chain_tip(store, SESSION_ID),
            answers=(Answer("SELECT seq, chain_digest FROM ledger", ((3, "c" * 64),)),),
        ),
        Case(
            name="chain_rows",
            statement=CHAIN_ROWS_QUERY,
            bound=(SESSION_ID,),
            run=lambda store: chain_rows(store, SESSION_ID),
        ),
        Case(
            name="session_upsert",
            statement=INSERT_SESSION_STATEMENT,
            bound=(
                SESSION_ID,
                CLIENT_ID,
                INJECTED_TERMINATOR,
                INJECTED_IDENTIFIER,
                INJECTED_TERMINATOR,
                canonical_json(ATTRIBUTION),
                INJECTED_NEWLINE,
                MOMENT,
                None,
                SessionOutcome.IN_PROGRESS.value,
                PARENT_SESSION_ID,
                SPAWNING_EVENT_ID,
                1,
                2,
                3,
                4,
                Decimal("1.50"),
                SPAWNING_EVENT_ID,
                PARENT_SESSION_ID,
                SPAWNING_EVENT_ID,
                PARENT_SESSION_ID,
            ),
            run=lambda store: upsert_session(store, SESSION_RECORD),
            answers=(Answer("INSERT INTO session", ((1,),)),),
        ),
        Case(
            name="session_counters",
            statement=BUMP_COUNTERS_STATEMENT,
            bound=(1, 2, 3, 4, Decimal("0.50"), SESSION_ID),
            run=lambda store: bump_session_counters(store, SESSION_ID, COUNTER_DELTA),
            answers=(Answer("UPDATE session SET tool_call_count", (COUNTER_ROW,)),),
        ),
        Case(
            name="session_counters_for_client",
            statement=BUMP_COUNTERS_FOR_CLIENT_STATEMENT,
            bound=(1, 2, 3, 4, Decimal("0.50"), SESSION_ID, CLIENT_ID),
            run=lambda store: bump_session_counters(
                store, SESSION_ID, COUNTER_DELTA, client_id=CLIENT_ID
            ),
            answers=(Answer("UPDATE session SET tool_call_count", (COUNTER_ROW,)),),
        ),
        Case(
            name="session_end",
            statement=END_SESSION_STATEMENT,
            bound=(MOMENT, SessionOutcome.SUCCEEDED.value, SESSION_ID, CLIENT_ID),
            run=lambda store: end_session(
                store,
                SESSION_ID,
                CLIENT_ID,
                outcome=SessionOutcome.SUCCEEDED,
                ended_at=MOMENT,
            ),
            answers=(Answer("UPDATE session SET ended_at", (COUNTER_ROW,)),),
        ),
        Case(
            name="session_end_with_counters",
            statement=END_SESSION_WITH_COUNTERS_STATEMENT,
            bound=(
                MOMENT,
                SessionOutcome.FAILED.value,
                9,
                8,
                7,
                6,
                Decimal("5.25"),
                SESSION_ID,
                CLIENT_ID,
            ),
            run=lambda store: end_session(
                store,
                SESSION_ID,
                CLIENT_ID,
                outcome=SessionOutcome.FAILED,
                ended_at=MOMENT,
                counters=TERMINAL_COUNTERS,
            ),
            answers=(Answer("UPDATE session SET ended_at", (COUNTER_ROW,)),),
        ),
        Case(
            name="session_read",
            statement=SELECT_SESSION_STATEMENT,
            bound=(SESSION_ID, CLIENT_ID),
            run=lambda store: session_of_client(store, SESSION_ID, CLIENT_ID),
        ),
        Case(
            name="sessions_of_client",
            statement=SELECT_SESSIONS_FOR_CLIENT_STATEMENT,
            bound=(CLIENT_ID, READ_LIMIT),
            run=lambda store: sessions_of_client(store, CLIENT_ID, limit=READ_LIMIT),
        ),
        Case(
            name="child_sessions",
            statement=SELECT_CHILD_SESSIONS_STATEMENT,
            bound=(CLIENT_ID, PARENT_SESSION_ID, READ_LIMIT),
            run=lambda store: child_sessions(store, PARENT_SESSION_ID, CLIENT_ID, limit=READ_LIMIT),
        ),
        Case(
            name="events_of_session",
            statement=SELECT_EVENTS_FOR_SESSION_STATEMENT,
            bound=(SESSION_ID, CLIENT_ID, READ_LIMIT),
            run=lambda store: events_of_session(store, SESSION_ID, CLIENT_ID, limit=READ_LIMIT),
        ),
        Case(
            name="artifacts_of_client",
            statement=SELECT_ARTIFACTS_FOR_CLIENT_STATEMENT,
            bound=(CLIENT_ID, READ_LIMIT),
            run=lambda store: artifacts_of_client(store, CLIENT_ID, limit=READ_LIMIT),
        ),
        Case(
            name="lineage_insert",
            statement=INSERT_EDGE_STATEMENT,
            bound=(
                CHILD_ARTIFACT_ID,
                PARENT_ARTIFACT_ID,
                ArtifactKind.DERIVED_ARTIFACT.value,
                CHILD_ARTIFACT_ID,
                PARENT_ARTIFACT_ID,
                PARENT_ARTIFACT_ID,
                CHILD_ARTIFACT_ID,
                PARENT_ARTIFACT_ID,
                ArtifactKind.DERIVED_ARTIFACT.value,
                INJECTED_IDENTIFIER,
            ),
            run=lambda store: insert_lineage_edge(store, CHILD_ARTIFACT_ID, PARENT_REFERENCE),
            answers=(Answer("INSERT INTO lineage_edge", ((uuid4(),),)),),
        ),
        Case(
            name="lineage_parent_existence",
            statement=PARENT_EXISTS_STATEMENT,
            bound=(PARENT_ARTIFACT_ID, ArtifactKind.DERIVED_ARTIFACT.value),
            run=lambda store: insert_lineage_edge(store, CHILD_ARTIFACT_ID, PARENT_REFERENCE),
            answers=(Answer("INSERT INTO lineage_edge", ()), Answer("FROM artifact_ref", ())),
            raises=MissingParentError,
        ),
        Case(
            name="lineage_descendants",
            statement=SELECT_DESCENDANTS_STATEMENT,
            bound=([PARENT_ARTIFACT_ID],),
            run=lambda store: descendants_of(store, [PARENT_ARTIFACT_ID, PARENT_ARTIFACT_ID]),
        ),
        Case(
            name="lineage_ancestors",
            statement=SELECT_ANCESTORS_STATEMENT,
            bound=([CHILD_ARTIFACT_ID],),
            run=lambda store: ancestors_of(store, [CHILD_ARTIFACT_ID]),
        ),
        Case(
            name="artifact_insert",
            statement=INSERT_ARTIFACT_STATEMENT,
            bound=(
                ARTIFACT_ID,
                DerivedArtifactKind.SUMMARY.value,
                CLIENT_ID,
                INJECTED_TERMINATOR,
                DIGEST,
                INJECTED_IDENTIFIER,
                2,
                MOMENT,
                MOMENT,
                None,
                EmbeddingState.PENDING.value,
                EXPIRY,
                None,
            ),
            run=lambda store: write_derived_artifact(store, ARTIFACT_RECORD),
            answers=(
                Answer(
                    "INSERT INTO derived_artifact",
                    ((ARTIFACT_ID, EmbeddingState.PENDING.value),),
                ),
            ),
        ),
        Case(
            name="embedding_insert",
            statement=INSERT_EMBEDDING_STATEMENT,
            bound=(
                ARTIFACT_ID,
                ArtifactKind.DERIVED_ARTIFACT.value,
                CLIENT_ID,
                INJECTED_TERMINATOR,
                INJECTED_IDENTIFIER,
                EMBEDDING_DIMENSION,
                True,
                rendered_vector(QUERY_VECTOR),
                EXPIRY,
            ),
            run=lambda store: write_embedding(store, EMBEDDING_RECORD),
            answers=(Answer("INSERT INTO embedding", ((uuid4(), MOMENT),)),),
        ),
        Case(
            name="embedding_state_transition",
            statement=MARK_STATE_STATEMENT,
            bound=(EmbeddingState.EMBEDDED.value, ARTIFACT_ID, CLIENT_ID),
            run=lambda store: mark_embedding_state(
                store, ARTIFACT_ID, CLIENT_ID, EmbeddingState.EMBEDDED
            ),
            answers=(Answer("UPDATE derived_artifact SET", ((EmbeddingState.EMBEDDED.value,),)),),
        ),
        Case(
            name="pending_sweep",
            statement=SELECT_PENDING_STATEMENT,
            bound=(PENDING_LIMIT,),
            run=lambda store: pending_artifacts(store, limit=PENDING_LIMIT),
        ),
        Case(
            name="neighbour_query",
            statement=NEAREST_STATEMENT,
            bound=(
                rendered_vector(QUERY_VECTOR),
                [CLIENT_ID, OTHER_CLIENT_ID],
                COSINE_LIMIT,
                rendered_vector(QUERY_VECTOR),
                COSINE_LIMIT,
                rendered_vector(QUERY_VECTOR),
                NEIGHBOUR_LIMIT,
            ),
            run=lambda store: nearest(
                store,
                QUERY_VECTOR,
                permitted_clients=PERMITTED_CLIENTS,
                limit=NEIGHBOUR_LIMIT,
                max_cosine=COSINE_LIMIT,
            ),
        ),
        Case(
            name="neighbour_query_exact_scan",
            statement=NEAREST_SCAN_STATEMENT,
            bound=(
                rendered_vector(QUERY_VECTOR),
                [CLIENT_ID, OTHER_CLIENT_ID],
                CANDIDATE_CAP,
                COSINE_LIMIT,
                rendered_vector(QUERY_VECTOR),
                COSINE_LIMIT,
                rendered_vector(QUERY_VECTOR),
                NEIGHBOUR_LIMIT,
            ),
            run=drive_scan,
        ),
        Case(
            name="recall_page",
            statement=RECALL_STATEMENT,
            bound=(
                rendered_vector(QUERY_VECTOR),
                rendered_vector(QUERY_VECTOR),
                RECALL_POOL,
                [CLIENT_ID, OTHER_CLIENT_ID],
                RECALL_EXCERPT,
                RECALL_EXCERPT,
                RECALL_FLOOR,
                RECALL_LIMIT,
            ),
            run=drive_recall,
        ),
        Case(
            name="recall_page_exact_scan",
            statement=RECALL_SCAN_STATEMENT,
            bound=(
                rendered_vector(QUERY_VECTOR),
                [CLIENT_ID, OTHER_CLIENT_ID],
                RECALL_POOL,
                rendered_vector(QUERY_VECTOR),
                RECALL_POOL,
                [CLIENT_ID, OTHER_CLIENT_ID],
                RECALL_EXCERPT,
                RECALL_EXCERPT,
                RECALL_FLOOR,
                RECALL_LIMIT,
            ),
            run=drive_recall_scan,
        ),
        Case(
            name="recall_principal_scope",
            statement=PRINCIPAL_SCOPE_QUERY,
            bound=(SESSION_ID,),
            run=lambda store: principal_scope(store, SESSION_ID),
        ),
        Case(
            name="horizon_read",
            statement=CAPABILITY_QUERY,
            bound=(GC_HORIZON_CAPABILITY,),
            run=gc_horizon,
            answers=(Answer("FROM capability", ((True, str(HORIZON_SECONDS)),)),),
        ),
        Case(
            name="capability_record",
            statement=RECORD_CAPABILITY_STATEMENT,
            bound=(INJECTED_IDENTIFIER, True, INJECTED_TERMINATOR),
            run=lambda store: record_capability(store, HOSTILE_CAPABILITY),
        ),
        Case(
            name="backup_planning_probe",
            statement=BACKUP_PLAN_QUERY,
            bound=(BACKUP_TARGET,),
            run=lambda store: probe_self_managed_backup(store, target=BACKUP_TARGET),
            answers=(Answer("EXPLAIN BACKUP INTO", (("distribution: local",),)),),
        ),
        Case(
            name="attribution_first_version",
            statement=INSERT_VERSION_STATEMENT,
            bound=(
                FIRST_VERSION_ID,
                ARTIFACT_ID,
                ArtifactKind.DERIVED_ARTIFACT.value,
                CLIENT_ID,
                BindingMethod.SCOPE.value,
                STRONGER_CONFIDENCE,
                MOMENT,
            ),
            run=lambda store: write_attribution(
                store,
                SUBMISSION,
                context=SUPERSESSION,
                version_id=FIRST_VERSION_ID,
            ),
            answers=(
                Answer(
                    "INSERT INTO client_binding",
                    ((FIRST_VERSION_ID, STRONGER_CONFIDENCE, MOMENT),),
                ),
            ),
        ),
        Case(
            name="attribution_supersession_closure",
            statement=CLOSE_CURRENT_VERSION_STATEMENT,
            bound=(SUCCESSOR_VERSION_ID, ARTIFACT_ID, CLIENT_ID),
            run=drive_supersession,
            answers=supersession_answers(),
        ),
        Case(
            name="attribution_supersession_successor",
            statement=INSERT_SUCCESSOR_STATEMENT,
            bound=(
                SUCCESSOR_VERSION_ID,
                ARTIFACT_ID,
                ArtifactKind.DERIVED_ARTIFACT.value,
                CLIENT_ID,
                BindingMethod.SCOPE.value,
                STRONGER_CONFIDENCE,
                PRIOR_CONFIDENCE,
                MOMENT,
            ),
            run=drive_supersession,
            answers=supersession_answers(),
        ),
        Case(
            name="attribution_withdrawal_marker",
            statement=INSERT_ERASURE_MARKER_STATEMENT,
            bound=(
                MARKER_VERSION_ID,
                ARTIFACT_ID,
                ArtifactKind.DERIVED_ARTIFACT.value,
                CLIENT_ID,
                BindingMethod.SCOPE.value,
                PRIOR_CONFIDENCE,
                CLOSED_AT,
                PRIOR_VERSION_ID,
            ),
            run=drive_withdrawal,
            answers=(
                Answer("UPDATE client_binding SET valid_to", (CLOSED_ROW,)),
                Answer(
                    "valid_from, valid_to, superseded_by)",
                    ((MARKER_VERSION_ID, PRIOR_CONFIDENCE, CLOSED_AT),),
                ),
                Answer("INSERT INTO ledger", (APPENDED_ROW,)),
            ),
        ),
        Case(
            name="attribution_pair_read",
            statement=CURRENT_PAIR_QUERY,
            bound=(ARTIFACT_ID, CLIENT_ID),
            run=lambda store: write_attribution(store, RESTATEMENT, context=SUPERSESSION),
            answers=(Answer("SELECT id, method, confidence FROM client_binding", (PAIR_ROW,)),),
        ),
        Case(
            name="attribution_current_read",
            statement=CURRENT_ATTRIBUTION_QUERY,
            bound=(ARTIFACT_ID,),
            run=lambda store: current_attribution(store, ARTIFACT_ID),
        ),
        Case(
            name="attribution_as_of_read",
            statement=ATTRIBUTION_AS_OF_QUERY,
            bound=(ARTIFACT_ID, AS_OF, AS_OF),
            run=lambda store: attribution_as_of(store, ARTIFACT_ID, AS_OF),
        ),
        Case(
            name="attribution_earliest_versions",
            statement=FIRST_ATTRIBUTION_QUERY,
            bound=(CLIENT_ID, [ARTIFACT_ID]),
            run=lambda store: first_attributions(store, CLIENT_ID, [ARTIFACT_ID, ARTIFACT_ID]),
        ),
        Case(
            name="binding_parent_claims",
            statement=PARENT_BINDINGS_QUERY,
            bound=([PARENT_ARTIFACT_ID],),
            run=drive_binding_detection,
            answers=(
                Answer("FROM client WHERE array_length", ((OTHER_CLIENT_ID, [MARKER_TERM]),)),
                Answer("max(confidence)", ((OTHER_CLIENT_ID, INHERITED_CONFIDENCE),)),
                Answer(
                    "INSERT INTO client_binding",
                    ((FIRST_VERSION_ID, STRONGER_CONFIDENCE, MOMENT),),
                ),
                Answer(
                    "INSERT INTO client_binding",
                    ((MARKER_VERSION_ID, STRONGER_CONFIDENCE, MOMENT),),
                ),
            ),
        ),
        Case(
            name="working_write",
            statement=UPSERT_SCRATCH_STATEMENT,
            bound=(
                SESSION_ID,
                INJECTED_TERMINATOR,
                CLIENT_ID,
                canonical_json(SCRATCH_VALUE),
                MOMENT,
                SCRATCH_EXPIRY,
            ),
            run=lambda store: write_scratch(
                store,
                SCRATCH_ENTRY,
                interval=SCRATCH_INTERVAL,
                now=MOMENT,
            ),
            answers=(Answer("UPSERT INTO working_memory", (SCRATCH_ROW,)),),
        ),
        Case(
            name="working_point_read",
            statement=SELECT_SCRATCH_STATEMENT,
            bound=(SESSION_ID, INJECTED_TERMINATOR, CLIENT_ID),
            run=lambda store: read_scratch(store, SESSION_ID, INJECTED_TERMINATOR, CLIENT_ID),
        ),
        Case(
            name="working_session_listing",
            statement=SELECT_SESSION_SCRATCH_STATEMENT,
            bound=(SESSION_ID, CLIENT_ID, READ_LIMIT),
            run=lambda store: session_scratch(store, SESSION_ID, CLIENT_ID, limit=READ_LIMIT),
        ),
        Case(
            name="working_purge",
            statement=PURGE_CLIENT_SCRATCH_STATEMENT,
            bound=(CLIENT_ID,),
            run=lambda store: purge_working_rows(store, CLIENT_ID),
            answers=(Answer("DELETE FROM working_memory", (PURGED_ROW,)),),
        ),
        Case(
            name="fenced_generation_read",
            statement=CURRENT_GENERATION_QUERY,
            bound=(CLIENT_ID,),
            run=drive_fence,
            answers=(Answer("FROM erasure_lease", (LEASE_ROW,)),),
        ),
        Case(
            name="erasure_guard_read",
            statement=ACTIVE_RUN_QUERY,
            bound=(CLIENT_ID, RUNNING_STATUS),
            run=drive_erasure_guard,
        ),
        Case(
            name="lease_current_read",
            statement=CURRENT_LEASE_QUERY,
            bound=(CLIENT_ID,),
            run=drive_takeover,
            answers=takeover_answers(),
        ),
        Case(
            name="lease_history_maximum",
            statement=HIGHEST_GENERATION_QUERY,
            bound=(NO_GENERATION, CLIENT_ID),
            run=drive_takeover,
            answers=takeover_answers(),
        ),
        Case(
            name="lease_supersession_closure",
            statement=CLOSE_LEASE_STATEMENT,
            bound=(MOMENT, SUCCESSOR_LEASE_ID, LEASE_ID),
            run=drive_takeover,
            answers=takeover_answers(),
        ),
        Case(
            name="lease_supersession_successor",
            statement=INSERT_LEASE_STATEMENT,
            bound=(
                SUCCESSOR_LEASE_ID,
                CLIENT_ID,
                INJECTED_TERMINATOR,
                TAKEOVER_GENERATION,
                INJECTED_NEWLINE,
                MOMENT,
                MOMENT,
                LEASE_SECONDS,
            ),
            run=drive_takeover,
            answers=takeover_answers(),
        ),
        Case(
            name="lease_renewal",
            statement=RENEW_LEASE_STATEMENT,
            bound=(MOMENT, LEASE_SECONDS, MOMENT, LEASE_ID),
            run=lambda store: renew(
                store,
                HELD_GRANT,
                interval=SCRIPTED_LEASE_INTERVAL,
                now=MOMENT,
            ),
            answers=(FENCE_ANSWER, Answer("renewed_at = coalesce", (RENEWED_LEASE_ROW,))),
        ),
        Case(
            name="lease_release",
            statement=SURRENDER_LEASE_STATEMENT,
            bound=(MOMENT, LEASE_ID),
            run=lambda store: release(store, HELD_GRANT, now=MOMENT),
            answers=(FENCE_ANSWER, Answer("greatest(coalesce(", (RENEWED_LEASE_ROW,))),
        ),
        Case(
            name="lease_run_ownership",
            statement=RECORD_RUN_KEY_STATEMENT,
            bound=(
                INJECTED_NEWLINE,
                LEASE_ID,
                CURRENT_GENERATION,
                RUN_ID,
                CLIENT_ID,
                INJECTED_NEWLINE,
            ),
            run=lambda store: register_run(store, HELD_GRANT, RUN_ID),
            answers=(FENCE_ANSWER, Answer("SET idempotency_key", (RUN_OWNERSHIP_ROW,))),
        ),
        Case(
            name="lease_finalisation_marking",
            statement=MARK_FINALISED_STATEMENT,
            bound=(
                MOMENT,
                canonical_json(FINALISATION_RESULT),
                RUN_ID,
                CLIENT_ID,
                INJECTED_NEWLINE,
            ),
            run=drive_finalisation,
            answers=(FENCE_ANSWER, Answer("SET finalised_at", (FINALISED_ROW,))),
        ),
        Case(
            name="lease_recorded_finalisation",
            statement=FINALISATION_QUERY,
            bound=(INJECTED_NEWLINE,),
            run=lambda store: finalisation_for(store, INJECTED_NEWLINE),
            answers=(Answer("FROM erasure_run WHERE idempotency_key", (FINALISED_ROW,)),),
        ),
        Case(
            name="procedure_retrieval_record",
            statement=INSERT_RETRIEVAL_STATEMENT,
            bound=(SESSION_ID, PROCEDURE_ID, PROCEDURE_KIND),
            run=lambda store: record_retrieval(store, PROCEDURE_ID, SESSION_ID),
            answers=(Answer("INSERT INTO procedure_retrieval", (PROCEDURE_RETRIEVAL_ROW,)),),
        ),
        Case(
            name="procedure_standing_read",
            statement=SELECT_STANDING_QUERY,
            bound=(PROCEDURE_ID, PROCEDURE_KIND),
            run=drive_outcome,
            answers=standing_answers(),
        ),
        Case(
            name="procedure_outcome_record",
            statement=INSERT_OUTCOME_STATEMENT,
            bound=(
                PROCEDURE_ID,
                PROCEDURE_KIND,
                SESSION_ID,
                SessionOutcome.SUCCEEDED.value,
            ),
            run=drive_outcome,
            answers=standing_answers(),
        ),
        Case(
            name="procedure_standing_adjustment",
            statement=ADJUST_STANDING_STATEMENT,
            bound=(
                CONFIDENCE_FLOOR,
                CONFIDENCE_CEILING,
                STANDING_DELTA,
                PROCEDURE_ID,
                PROCEDURE_KIND,
            ),
            run=drive_outcome,
            answers=standing_answers(),
        ),
        Case(
            name="procedure_confidence_change_record",
            statement=INSERT_CHANGE_STATEMENT,
            bound=(PROCEDURE_ID, PRIOR_STANDING, RAISED_STANDING, OUTCOME_ID),
            run=drive_outcome,
            answers=standing_answers(),
        ),
        Case(
            name="procedure_outcome_diagnosis",
            statement=OUTCOME_CONTEXT_QUERY,
            bound=(
                PROCEDURE_ID,
                PROCEDURE_KIND,
                SESSION_ID,
                PROCEDURE_ID,
                SESSION_ID,
                PROCEDURE_ID,
                SESSION_ID,
            ),
            run=drive_repeated_outcome,
            answers=(
                Answer(STANDING_FRAGMENT, (STANDING_ROW,)),
                Answer(CONTEXT_FRAGMENT, (RECORDED_OUTCOME_ROW,)),
            ),
        ),
        Case(
            name="procedure_change_history",
            statement=SELECT_CHANGES_QUERY,
            bound=(PROCEDURE_ID, HISTORY_LIMIT),
            run=lambda store: change_history(store, PROCEDURE_ID, limit=HISTORY_LIMIT),
        ),
        Case(
            name="procedure_retrieval_count",
            statement=COUNT_RETRIEVALS_QUERY,
            bound=(PROCEDURE_ID,),
            run=drive_standing_summary,
            answers=(
                Answer(STANDING_FRAGMENT, (STANDING_ROW,)),
                Answer(RETRIEVAL_COUNT_FRAGMENT, (RETRIEVAL_COUNT_ROW,)),
            ),
        ),
        Case(
            name="procedure_outcome_counts",
            statement=SELECT_OUTCOME_COUNTS_QUERY,
            bound=(PROCEDURE_ID,),
            run=drive_standing_summary,
            answers=(
                Answer(STANDING_FRAGMENT, (STANDING_ROW,)),
                Answer(RETRIEVAL_COUNT_FRAGMENT, (RETRIEVAL_COUNT_ROW,)),
            ),
        ),
        Case(
            name="tool_server_permitted_clients",
            statement=PERMITTED_CLIENT_IDS_STATEMENT,
            bound=([INJECTED_TERMINATOR],),
            run=lambda store: permitted_client_ids(store, (INJECTED_TERMINATOR,)),
            answers=(Answer("SELECT id FROM client WHERE slug", ((CLIENT_ID,),)),),
        ),
        Case(
            name="tool_server_lineage_ancestors",
            statement=SELECT_PERMITTED_ANCESTORS_STATEMENT,
            bound=([ARTIFACT_ID], list(PERMITTED_CLIENTS), TOOL_LIMIT),
            run=lambda store: dispatch(tool_backend(store), ANCESTORS_TOOL, TOOL_ARGUMENTS),
            answers=(
                Answer(
                    "FROM ancestors AS a",
                    ((ARTIFACT_ID, ArtifactKind.DERIVED_ARTIFACT.value),),
                ),
            ),
        ),
        Case(
            name="tool_server_lineage_descendants",
            statement=SELECT_PERMITTED_DESCENDANTS_STATEMENT,
            bound=([ARTIFACT_ID], list(PERMITTED_CLIENTS), TOOL_LIMIT),
            run=lambda store: dispatch(tool_backend(store), DESCENDANTS_TOOL, TOOL_ARGUMENTS),
            answers=(Answer("FROM descendants AS d", ((ARTIFACT_ID,),)),),
        ),
    )


CASES: Final[tuple[Case, ...]] = build_cases()
CASE_IDS: Final[list[str]] = [case.name for case in CASES]


def drive(case: Case) -> Script:
    """Run one case against a scripted store and return what it was sent."""
    script = Script(answers=list(case.answers))
    store = build_store(script)
    if case.raises is None:
        case.run(store)
    else:
        with pytest.raises(case.raises):
            case.run(store)
    return script


# ---------------------------------------------------------------------------
# The marker every statement carries, and the mechanisms none of them does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "discovered",
    SQL_TEXTS,
    ids=[item.label for item in SQL_TEXTS],
)
def test_a_statement_carries_the_drivers_marker_and_no_other_mechanism(
    discovered: Discovered,
) -> None:
    """One way to carry a value, and it is the one the cluster substitutes for.

    A marker of another dialect, a named marker, or a formatting placeholder would
    each be a second mechanism, and a second mechanism is how a value reaches
    statement text by accident.
    """
    text = discovered.text
    for found in re.findall(r"%.", text, flags=re.DOTALL):
        assert found == PARAMETER_MARKER, f"{discovered.label} carries {found!r}"
    assert not text.endswith("%"), f"{discovered.label} ends in an incomplete marker"
    assert "{" not in text and "}" not in text, f"{discovered.label} holds a format placeholder"
    assert "?" not in text, f"{discovered.label} holds a positional marker of another dialect"
    assert re.search(r"\$\d", text) is None, f"{discovered.label} holds a numbered marker"


def test_the_sweep_discovered_statements_to_check() -> None:
    """A discovery that found nothing would assert nothing, so it is asserted itself."""
    assert VALUE_CARRYING, "the sweep discovered no value-carrying statement"
    assert WHOLE_TEXTS, "the sweep discovered no whole statement"


def test_the_sweep_discovered_every_statement_it_drives() -> None:
    """Each driven statement is one discovery found, so neither half can rot alone."""
    undiscovered = sorted(case.name for case in CASES if case.statement not in WHOLE_TEXTS)
    assert undiscovered == []


# ---------------------------------------------------------------------------
# The bound values, read off statements that really went out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_an_operation_binds_every_value_and_interpolates_none(case: Case) -> None:
    """The bound tuple is the caller's values, and no statement holds any of them.

    Hostile text carrying a quote, a terminator, a comment marker, and a newline
    travels through each operation, so a value that reached statement text would
    be visible as that text rather than inferred from the absence of a quote.
    """
    script = drive(case)

    assert script.parameters_of(case.statement) == case.bound
    assert case.statement.count(PARAMETER_MARKER) == len(case.bound)
    for query in script.statements:
        for hostile in HOSTILE_VALUES:
            assert hostile not in query, "a caller value reached statement text"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_every_statement_an_operation_sent_is_a_module_level_literal(case: Case) -> None:
    """Nothing is composed at call time, so nothing can be composed around a value."""
    composed = sorted(set(drive(case).statements) - WHOLE_TEXTS)
    assert composed == []


def test_every_value_carrying_statement_is_driven_by_the_sweep() -> None:
    """The bound-parameter claim is made over every statement, not a chosen few.

    A statement added to the layer, whether to a module that exists or to one that
    arrives later, is discovered by the sweep and reported here until an operation
    drives it.
    """
    driven = {case.statement for case in CASES}
    undriven = sorted(item.label for item in VALUE_CARRYING if item.text not in driven)
    assert undriven == []


# ---------------------------------------------------------------------------
# The one exception
# ---------------------------------------------------------------------------


def test_the_historical_instant_is_the_only_rendered_value_in_the_layer() -> None:
    """The clause the cluster resolves while planning is the single exception.

    A historical read sends the composed clause, then the caller's own statement
    with the caller's values bound. Those are the only two statements of the read
    that are not literals of the store, and the second belongs to the caller.
    """
    now = MOMENT + timedelta(days=30)
    at = now - timedelta(seconds=60)
    script = Script(answers=[Answer("FROM derived_artifact", ((7,),))])

    rows = historical(
        build_store(script),
        CALLER_STATEMENT,
        (INJECTED_TERMINATOR,),
        at=at,
        now=now,
        horizon=GcHorizon(seconds=HORIZON_SECONDS),
    )

    assert rows == ((7,),)
    rendered = render_as_of_timestamp(at)
    clause = f"{AS_OF_STATEMENT_PREFIX}'{rendered}'"
    composed = sorted(set(script.statements) - WHOLE_TEXTS)
    assert composed == sorted({clause, CALLER_STATEMENT})
    assert script.parameters_of(CALLER_STATEMENT) == (INJECTED_TERMINATOR,)
    assert INJECTED_TERMINATOR not in clause


def test_the_rendered_instant_is_admitted_only_by_the_anchored_form_check() -> None:
    """The form check is anchored at both ends, so nothing rides alongside a rendering.

    This is what makes the exception safe rather than merely narrow: the rendering
    the clause is composed from has to be the fixed digits and their punctuation
    and nothing else, whatever produced it.
    """
    rendered = render_as_of_timestamp(MOMENT)

    assert require_rendered_form(rendered) == rendered
    for hostile in HOSTILE_VALUES:
        with pytest.raises(StoreError, match="fixed digit form"):
            require_rendered_form(rendered + hostile)
        with pytest.raises(StoreError, match="fixed digit form"):
            require_rendered_form(hostile + rendered)
