"""The Seed_Generator: tenants, Sessions, Events, Artifacts, standing, and history.

What this module writes is written through the paths the product already owns.
Ledger rows go through the hash-chain append, so a seeded corpus verifies as a
chain rather than as a set of rows that happen to carry digests. Bindings go
through the Binding_Detector and the attribution write path, so a seeded binding
was detected rather than asserted, and a blended Artifact is blended because its
parents were. Procedural standing goes through the Confidence_Tracker, driven by
the outcomes of the Sessions that retrieved each procedure, so the console shows
values that were earned. Working rows go through the working-tier write, so the
disposability property has state to compare.

Three properties are structural rather than promised.

**One seeded generator, one traversal order.** Every drawn value comes from a
single `Random` seeded by the caller's value, consumed in a fixed order, so the
same seed produces the same content. Identifiers and timestamps are the only
values that vary between two runs of one seed, which is exactly what Requirement
28.9 allows and what the determinism test normalises away.

**One transaction per Session, not one per row.** A Session's Events, their
Embeddings, their bindings, and its working rows commit together, so seeding a
corpus of thousands of Events costs a transaction per Session rather than per row.

**No provider call.** Vectors come from the deterministic local function in this
package, recorded on the row as their own provider name, so seeding needs no
credential and produces the same corpus on any machine.

Every statement here is a whole module-level literal with bound parameters, and no
identifier and no domain value is ever interpolated into statement text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from random import Random
from typing import Final
from uuid import UUID, uuid4

from molt.confidence import (
    ConfidencePolicy,
    initial_standing,
    record_outcome,
    record_retrieval,
)
from molt.models.artifact import (
    ArtifactKind,
    ArtifactRef,
    DerivedArtifact,
    DerivedArtifactKind,
)
from molt.models.binding import BindingMethod
from molt.models.event import EmbeddingState, Event, EventCategory, JsonObject
from molt.models.session import Session, SessionOutcome
from molt.seed.corpora import (
    AGENT_CLI_NAMES,
    DOMAINS,
    MACHINE_IDS,
    ClientDomain,
    SeedVolumes,
    assistant_text,
    baseline_body,
    error_text,
    path_text,
    procedure_body,
    prompt_text,
    scratch_value,
    shell_text,
    summary_body,
    tool_name,
)
from molt.seed.vectors import SeedEmbedder
from molt.store import Cursor, MemoryStore
from molt.store.attribution import AttributionSubmission, SupersessionContext, record_attribution
from molt.store.binding_detector import (
    MARKER_CONFIDENCE,
    SCOPE_CONFIDENCE,
    DetectionRequest,
    write_bindings,
)
from molt.store.chain import LedgerAppend, append_in_transaction
from molt.store.embeddings import (
    EmbeddingWrite,
    insert_artifact_with_embedding,
    insert_embedding,
)
from molt.store.lineage import ParentRef, insert_edges
from molt.store.sessions import (
    end_session,
    insert_session_in_transaction,
    insert_spawned_session,
)
from molt.store.working import ScratchWrite, WorkingInterval, upsert_scratch

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "EVENT_CYCLE",
    "INSERT_CLIENT_STATEMENT",
    "SeedResult",
    "SeededArtifact",
    "SeededClient",
    "SeededSession",
    "generate",
    "seeded_clients",
]

# The tenant insert. Client rows are few and are written once per generation, so
# this is the one write here that is a row at a time; every high-volume write is
# batched into its Session's transaction instead.
INSERT_CLIENT_STATEMENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction, retention_interval, "
    "content_markers) VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (slug) DO UPDATE SET display_name = excluded.display_name "
    "RETURNING id"
)

# How long a seeded tenant retains content for. It is a property of the tenant
# rather than of this module's choosing, and every seeded row's expiry follows
# from it, so a seeded corpus expires the way a real one would.
DEFAULT_RETENTION_DAYS: Final[int] = 90

# The categories a Session's body Events cycle through, and which of them carry
# embeddable text. The cycle is fixed so the traversal order is fixed, and the
# text-carrying members are a quarter of it, which is what keeps the embedding
# count proportional to the Event count rather than equal to it.
EVENT_CYCLE: Final[tuple[EventCategory, ...]] = (
    EventCategory.USER_PROMPT,
    EventCategory.TOOL_CALL,
    EventCategory.TOOL_RESULT,
    EventCategory.FILE_READ,
    EventCategory.ASSISTANT_RESPONSE,
    EventCategory.SHELL_COMMAND,
    EventCategory.MODEL_REQUEST,
    EventCategory.MODEL_RESPONSE,
    EventCategory.FILE_WRITE,
    EventCategory.COST_RECORD,
    EventCategory.DECISION,
    EventCategory.ERROR,
)
_TEXT_CATEGORIES: Final[frozenset[EventCategory]] = frozenset(
    {
        EventCategory.USER_PROMPT,
        EventCategory.ASSISTANT_RESPONSE,
        EventCategory.FILE_WRITE,
    }
)

# The outcome cycle seeded Sessions are closed with, so all three terminal
# classifications appear and failures are plentiful enough to drive a procedure
# below the recall floor.
_OUTCOME_CYCLE: Final[tuple[SessionOutcome, ...]] = (
    SessionOutcome.SUCCEEDED,
    SessionOutcome.SUCCEEDED,
    SessionOutcome.FAILED,
    SessionOutcome.SUCCEEDED,
    SessionOutcome.ABANDONED,
    SessionOutcome.FAILED,
)

# How far apart two seeded Events sit, and how far a Session's start sits from the
# generation instant. Both are intervals rather than instants, so the content is
# free of any calendar value and only the base reading varies between runs.
_EVENT_SPACING: Final[timedelta] = timedelta(seconds=7)
_SESSION_SPACING: Final[timedelta] = timedelta(minutes=17)

# How many Artifacts receive a marker detection after their scope detection, so a
# genuine supersession with a closed validity interval exists.
_SUPERSEDED_ARTIFACTS: Final[int] = 3

# The labels the transactions of this module appear under in a log record.
_CLIENT_LABEL: Final[str] = "seed_clients"
_SESSION_LABEL: Final[str] = "seed_session_body"
_ARTIFACT_LABEL: Final[str] = "seed_artifact"

# What a seeded derivation is recorded as having been produced by.
_DERIVATION_SUMMARY: Final[str] = "summarised"
_DERIVATION_BASELINE: Final[str] = "generalised"
_DERIVATION_PROCEDURE: Final[str] = "distilled"


@dataclass(frozen=True, slots=True)
class SeededClient:
    """One seeded tenant: the row's identifier and the domain it was built from."""

    id: UUID
    domain: ClientDomain

    @property
    def slug(self) -> str:
        """The tenant's slug, which is what the ground truth names it by."""
        return self.domain.slug


@dataclass(frozen=True, slots=True)
class SeededSession:
    """One seeded Session and what was written inside it."""

    id: UUID
    client_id: UUID
    agent_cli: str
    machine_id: str
    depth: int
    outcome: SessionOutcome
    started_at: datetime
    event_ids: tuple[UUID, ...]
    text_event_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SeededArtifact:
    """One seeded Derived_Artifact and the tenants its bindings named."""

    id: UUID
    kind: DerivedArtifactKind
    client_ids: tuple[UUID, ...]
    superseded: bool = False

    @property
    def blended(self) -> bool:
        """Whether the Artifact's bindings named more than one tenant."""
        return len(self.client_ids) > 1


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What one generation produced, in the shapes a caller and a test read."""

    seed: int
    clients: tuple[SeededClient, ...]
    sessions: tuple[SeededSession, ...]
    events: int
    embeddings: int
    artifacts: tuple[SeededArtifact, ...]
    procedures_below_floor: tuple[UUID, ...]
    supersessions: tuple[UUID, ...]
    working_rows: int
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def blended_artifacts(self) -> tuple[SeededArtifact, ...]:
        """The Artifacts whose bindings named more than one tenant."""
        return tuple(artifact for artifact in self.artifacts if artifact.blended)

    def client_of(self, slug: str) -> SeededClient:
        """The seeded tenant carrying a slug.

        Raises:
            KeyError: No seeded tenant carries that slug.
        """
        for client in self.clients:
            if client.slug == slug:
                return client
        raise KeyError(f"no seeded client carries the slug {slug!r}")


def seeded_clients(volumes: SeedVolumes) -> tuple[ClientDomain, ...]:
    """The domains a generation of this size uses, in declared order."""
    return DOMAINS[: volumes.clients]


def generate(
    store: MemoryStore,
    *,
    seed: int,
    volumes: SeedVolumes | None = None,
    embedder: SeedEmbedder | None = None,
    policy: ConfidencePolicy | None = None,
    now: datetime | None = None,
) -> SeedResult:
    """Generate a whole seeded corpus, driven by one seed value.

    Args:
        store: The connection surface every transaction is framed by.
        seed: The value every drawn value derives from. The same value produces
            the same content, identifiers and timestamps aside.
        volumes: How much of each shape to produce, defaulting to the design's.
        embedder: The vector function every embeddable Artifact is embedded
            through, defaulting to this package's deterministic local one.
        policy: The confidence numbers procedural standing moves by, resolved from
            the configuration surface when a caller names none.
        now: The instant the corpus is dated from, defaulting to a reading taken
            here. Every seeded timestamp is an offset from it.

    Returns:
        What was written, including the tenants and Sessions the contamination
        path draws its owner and host from.
    """
    chosen = SeedVolumes() if volumes is None else volumes
    vectors = SeedEmbedder() if embedder is None else embedder
    standing = ConfidencePolicy.from_configuration() if policy is None else policy
    base = datetime.now(UTC) if now is None else now
    rng = Random(seed)  # noqa: S311 - reproducible sample content, never a secret
    interval = WorkingInterval.from_configuration()

    clients = _write_clients(store, chosen)
    plan = _session_plan(chosen)
    sessions: list[SeededSession] = []
    events = 0
    embeddings = 0
    working = 0
    for index, (client_index, parent_index, per_session) in enumerate(plan):
        client = clients[client_index % len(clients)]
        parent = None if parent_index is None else sessions[parent_index]
        written = _write_session(
            store,
            client=client,
            parent=parent,
            index=index,
            event_count=per_session,
            outcome=_OUTCOME_CYCLE[index % len(_OUTCOME_CYCLE)],
            base=base,
            rng=rng,
            vectors=vectors,
            interval=interval,
            working_rows=chosen.working_rows_per_session,
        )
        sessions.append(written.session)
        events += len(written.session.event_ids)
        embeddings += written.embeddings
        working += written.working_rows

    artifacts = _write_artifacts(
        store,
        clients=clients,
        sessions=tuple(sessions),
        volumes=chosen,
        base=base,
        rng=rng,
        vectors=vectors,
    )
    embeddings += len(artifacts)
    supersessions = _supersede_some(store, artifacts=artifacts, sessions=tuple(sessions), base=base)
    below_floor = _drive_standing(
        store,
        artifacts=artifacts,
        sessions=tuple(sessions),
        policy=standing,
    )

    return SeedResult(
        seed=seed,
        clients=clients,
        sessions=tuple(sessions),
        events=events,
        embeddings=embeddings,
        artifacts=artifacts,
        procedures_below_floor=below_floor,
        supersessions=supersessions,
        working_rows=working,
        generated_at=base,
    )


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


def _write_clients(store: MemoryStore, volumes: SeedVolumes) -> tuple[SeededClient, ...]:
    """Write one tenant per domain in one transaction, resolving a repeat."""
    domains = seeded_clients(volumes)
    retention = timedelta(days=DEFAULT_RETENTION_DAYS)

    def body(cursor: Cursor) -> tuple[SeededClient, ...]:
        written: list[SeededClient] = []
        for domain in domains:
            cursor.execute(
                INSERT_CLIENT_STATEMENT,
                (
                    uuid4(),
                    domain.slug,
                    domain.display_name,
                    domain.jurisdiction,
                    retention,
                    list(domain.content_markers),
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("the seeded client write reported no row")
            written.append(SeededClient(id=_as_uuid(row[0]), domain=domain))
        return tuple(written)

    return store.in_serializable(body, label=_CLIENT_LABEL)


# ---------------------------------------------------------------------------
# Sessions and their bodies
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _WrittenSession:
    """What one Session's two transactions produced."""

    session: SeededSession
    embeddings: int
    working_rows: int


def _session_plan(volumes: SeedVolumes) -> tuple[tuple[int, int | None, int], ...]:
    """Decide, in one pass, which tenant and parent each Session has and its size.

    The nested Sessions come last so their parents already exist in the list, and
    the depth-three Session hangs off a depth-two one, which is what produces a
    nesting depth of three rather than two Sessions at depth two.
    """
    total = volumes.sessions
    nested_two = volumes.subagent_sessions_depth_two
    nested_three = volumes.subagent_sessions_depth_three
    roots = total - nested_two - nested_three
    base_events = max(1, volumes.events // total)
    remainder = max(0, volumes.events - base_events * total)

    if roots < 1:
        raise ValueError("a generation needs at least one root Session")
    plan: list[tuple[int, int | None, int]] = []
    for index in range(roots):
        size = base_events + (1 if index < remainder else 0)
        plan.append((index, None, size))
    for offset in range(nested_two):
        parent = offset % roots
        plan.append((plan[parent][0], parent, max(1, base_events // 2)))
    for offset in range(nested_three):
        parent = roots + (offset % max(1, nested_two)) if nested_two else offset % roots
        plan.append((plan[parent][0], parent, max(1, base_events // 3)))
    return tuple(plan)


def _write_session(
    store: MemoryStore,
    *,
    client: SeededClient,
    parent: SeededSession | None,
    index: int,
    event_count: int,
    outcome: SessionOutcome,
    base: datetime,
    rng: Random,
    vectors: SeedEmbedder,
    interval: WorkingInterval,
    working_rows: int,
) -> _WrittenSession:
    """Write one Session, then its whole body in one further transaction."""
    domain = client.domain
    session_id = uuid4()
    agent_cli = AGENT_CLI_NAMES[index % len(AGENT_CLI_NAMES)]
    machine_id = MACHINE_IDS[index % len(MACHINE_IDS)]
    started_at = base + _SESSION_SPACING * index
    parent_id = None if parent is None else parent.id
    depth = 0 if parent is None else parent.depth + 1
    expires_at = started_at + timedelta(days=DEFAULT_RETENTION_DAYS)

    record = Session(
        id=session_id,
        client_id=client.id,
        agent_cli=agent_cli,
        machine_id=machine_id,
        team_id=None,
        attribution={"workspace": domain.repository},
        workspace_path=domain.repository,
        started_at=started_at,
        ended_at=None,
        outcome=SessionOutcome.IN_PROGRESS,
        parent_session_id=parent_id,
        spawning_event_id=None,
        depth=depth,
        tool_call_count=0,
        model_request_count=0,
        error_count=0,
        token_count=0,
        cost_usd=Decimal(0),
        halted=False,
        halted_at=None,
        halt_reason=None,
        halt_rule_id=None,
    )

    if parent is None:
        store.in_serializable(
            lambda cursor: insert_session_in_transaction(cursor, record, spawning_event_id=None),
            label=_SESSION_LABEL,
        )
    else:
        spawning = _spawning_event(
            parent=parent,
            client_id=parent.client_id,
            occurred_at=started_at,
            child_id=session_id,
        )
        insert_spawned_session(
            store,
            record,
            append_spawning_event=lambda cursor: (
                append_in_transaction(
                    cursor,
                    LedgerAppend(event=spawning, expires_at=expires_at),
                ).event_id
            ),
        )

    plan = _event_plan(domain, event_count, rng)
    context = SupersessionContext(
        session_id=session_id,
        agent_cli=agent_cli,
        machine_id=machine_id,
        expires_at=expires_at,
    )

    def body(cursor: Cursor) -> tuple[tuple[UUID, ...], tuple[UUID, ...], int, int]:
        appended: list[UUID] = []
        embedded: list[UUID] = []
        for offset, (category, payload, text) in enumerate(plan):
            event = Event(
                id=uuid4(),
                session_id=session_id,
                client_id=client.id,
                category=category,
                occurred_at=started_at + _EVENT_SPACING * offset,
                agent_cli=agent_cli,
                machine_id=machine_id,
                parent_event_id=None,
                payload=payload,
                redacted=False,
                text_body=text,
            )
            state = EmbeddingState.PENDING if text is not None else EmbeddingState.NOT_REQUIRED
            append_in_transaction(
                cursor,
                LedgerAppend(event=event, expires_at=expires_at, embedding_state=state),
            )
            appended.append(event.id)
            if text is None:
                continue
            _embed_event(
                cursor,
                event=event,
                text=text,
                vectors=vectors,
                expires_at=expires_at,
            )
            write_bindings(
                cursor,
                DetectionRequest(
                    artifact=ArtifactRef(
                        id=event.id,
                        kind=ArtifactKind.EVENT,
                        client_id=client.id,
                    ),
                    scope_client_id=client.id,
                    text=text,
                ),
                context=context,
                detected_at=event.occurred_at,
            )
            embedded.append(event.id)
        scratch = 0
        for slot in range(working_rows):
            upsert_scratch(
                cursor,
                ScratchWrite(
                    session_id=session_id,
                    scratch_key=f"note-{slot}",
                    client_id=client.id,
                    value={"note": scratch_value(domain, rng)},
                ),
                interval=interval,
                now=started_at,
            )
            scratch += 1
        return tuple(appended), tuple(embedded), len(embedded), scratch

    appended, embedded, embeddings, scratch = store.in_serializable(body, label=_SESSION_LABEL)
    _close_session(
        store, session_id=session_id, client_id=client.id, outcome=outcome, base=started_at
    )

    return _WrittenSession(
        session=SeededSession(
            id=session_id,
            client_id=client.id,
            agent_cli=agent_cli,
            machine_id=machine_id,
            depth=depth,
            outcome=outcome,
            started_at=started_at,
            event_ids=appended,
            text_event_ids=embedded,
        ),
        embeddings=embeddings,
        working_rows=scratch,
    )


def _event_plan(
    domain: ClientDomain,
    count: int,
    rng: Random,
) -> tuple[tuple[EventCategory, JsonObject, str | None], ...]:
    """Decide every Event of one Session up front, in one traversal of the generator.

    Deciding the whole body before the transaction opens is what keeps the drawn
    values independent of how many times a serializable conflict re-runs the
    transaction: a retry re-sends the same rows rather than drawing new ones.
    """
    produced: list[tuple[EventCategory, JsonObject, str | None]] = [
        (EventCategory.SESSION_START, {"workspace": domain.repository}, None)
    ]
    body_count = max(0, count - 2)
    for offset in range(body_count):
        category = EVENT_CYCLE[offset % len(EVENT_CYCLE)]
        produced.append(_event_content(domain, category, rng))
    produced.append((EventCategory.SESSION_END, {"workspace": domain.repository}, None))
    return tuple(produced[:count]) if count >= 1 else ()


def _event_content(
    domain: ClientDomain,
    category: EventCategory,
    rng: Random,
) -> tuple[EventCategory, JsonObject, str | None]:
    """The payload and the text one body Event of a category carries."""
    if category is EventCategory.USER_PROMPT:
        text = prompt_text(domain, rng)
        return category, {"role": "operator"}, text
    if category is EventCategory.ASSISTANT_RESPONSE:
        text = assistant_text(domain, rng)
        return category, {"role": "assistant"}, text
    if category is EventCategory.FILE_WRITE:
        path = path_text(domain, rng)
        text = f"{path}\n{error_text(domain, rng)}"
        return category, {"path": path}, text
    if category is EventCategory.TOOL_CALL:
        return category, {"tool": tool_name(rng)}, None
    if category is EventCategory.TOOL_RESULT:
        return category, {"tool": tool_name(rng), "rows": rng.randint(1, 40)}, None
    if category is EventCategory.FILE_READ:
        return category, {"path": path_text(domain, rng)}, None
    if category is EventCategory.SHELL_COMMAND:
        return category, {"command": shell_text(domain, rng)}, None
    if category is EventCategory.MODEL_REQUEST:
        return category, {"input_tokens": rng.randint(200, 4000)}, None
    if category is EventCategory.MODEL_RESPONSE:
        return category, {"output_tokens": rng.randint(40, 900)}, None
    if category is EventCategory.COST_RECORD:
        return category, {"units": rng.randint(1, 30)}, None
    if category is EventCategory.DECISION:
        return category, {"choice": rng.choice(domain.services)}, None
    return category, {"detail": error_text(domain, rng)}, None


def _spawning_event(
    *,
    parent: SeededSession,
    client_id: UUID,
    occurred_at: datetime,
    child_id: UUID,
) -> Event:
    """The Event in a parent Session that stands for spawning a subagent Session."""
    return Event(
        id=uuid4(),
        session_id=parent.id,
        client_id=client_id,
        category=EventCategory.TOOL_CALL,
        occurred_at=occurred_at,
        agent_cli=parent.agent_cli,
        machine_id=parent.machine_id,
        parent_event_id=None,
        payload={"tool": "spawn_subagent", "child": str(child_id)},
        redacted=False,
        text_body=None,
    )


def _embed_event(
    cursor: Cursor,
    *,
    event: Event,
    text: str,
    vectors: SeedEmbedder,
    expires_at: datetime,
) -> None:
    """Write the vector standing for one Event's text on the caller's cursor."""
    insert_embedding(
        cursor,
        EmbeddingWrite(
            artifact_id=event.id,
            artifact_kind=ArtifactKind.EVENT,
            client_id=event.client_id,
            provider=vectors.provider,
            model_id=vectors.model_id,
            vec=vectors.embed_one(text),
            expires_at=expires_at,
        ),
    )


def _close_session(
    store: MemoryStore,
    *,
    session_id: UUID,
    client_id: UUID,
    outcome: SessionOutcome,
    base: datetime,
) -> None:
    """Close a Session with a terminal outcome so recall and standing can read it."""
    end_session(
        store,
        session_id,
        client_id,
        outcome=outcome,
        ended_at=base + _SESSION_SPACING,
    )


# ---------------------------------------------------------------------------
# Derived Artifacts, their lineage, and their bindings
# ---------------------------------------------------------------------------


def _write_artifacts(
    store: MemoryStore,
    *,
    clients: tuple[SeededClient, ...],
    sessions: tuple[SeededSession, ...],
    volumes: SeedVolumes,
    base: datetime,
    rng: Random,
    vectors: SeedEmbedder,
) -> tuple[SeededArtifact, ...]:
    """Write summaries, baselines, and procedures from real parents.

    A summary's parents are one Session's own text Events, so its bindings are
    that Session's tenant. A baseline's and a procedure's parents span Sessions of
    two or three tenants, so the bindings the detector inherits name more than one
    tenant and the Artifact is blended because its parents were.
    """
    with_text = tuple(session for session in sessions if session.text_event_ids)
    if not with_text:
        return ()
    produced: list[SeededArtifact] = []

    for offset, session in enumerate(with_text[: volumes.blended_artifacts]):
        domain = _domain_of_client(clients, session.client_id)
        produced.append(
            _write_artifact(
                store,
                kind=DerivedArtifactKind.SUMMARY,
                owner=session.client_id,
                body=summary_body(domain, rng),
                parents=tuple(
                    ArtifactRef(id=event_id, kind=ArtifactKind.EVENT, client_id=session.client_id)
                    for event_id in session.text_event_ids[:2]
                ),
                derivation=_DERIVATION_SUMMARY,
                base=base + _SESSION_SPACING * offset,
                session=session,
                vectors=vectors,
            )
        )

    blended_kinds = (DerivedArtifactKind.BEHAVIORAL_BASELINE, DerivedArtifactKind.LEARNED_PROCEDURE)
    for offset in range(volumes.blended_artifacts):
        kind = blended_kinds[offset % len(blended_kinds)]
        group = _cross_client_sessions(with_text, offset)
        if len(group) < 2:
            break
        domains = tuple(_domain_of_client(clients, item.client_id) for item in group)
        parents = tuple(
            ArtifactRef(
                id=item.text_event_ids[0], kind=ArtifactKind.EVENT, client_id=item.client_id
            )
            for item in group
        )
        body = (
            baseline_body(domains, rng)
            if kind is DerivedArtifactKind.BEHAVIORAL_BASELINE
            else procedure_body(domains, rng)
        )
        derivation = (
            _DERIVATION_BASELINE
            if kind is DerivedArtifactKind.BEHAVIORAL_BASELINE
            else _DERIVATION_PROCEDURE
        )
        produced.append(
            _write_artifact(
                store,
                kind=kind,
                owner=group[0].client_id,
                body=body,
                parents=parents,
                derivation=derivation,
                base=base + _SESSION_SPACING * offset,
                session=group[0],
                vectors=vectors,
            )
        )
    return tuple(produced)


def _write_artifact(
    store: MemoryStore,
    *,
    kind: DerivedArtifactKind,
    owner: UUID,
    body: str,
    parents: tuple[ArtifactRef, ...],
    derivation: str,
    base: datetime,
    session: SeededSession,
    vectors: SeedEmbedder,
) -> SeededArtifact:
    """Write one Artifact, its vector, its lineage, and its bindings in one transaction."""
    artifact_id = uuid4()
    expires_at = base + timedelta(days=DEFAULT_RETENTION_DAYS)
    confidence = initial_standing(kind) if kind is DerivedArtifactKind.LEARNED_PROCEDURE else None
    artifact = DerivedArtifact(
        id=artifact_id,
        kind=kind,
        owner_client_id=owner,
        body=body,
        content_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        derivation_method=derivation,
        revision=1,
        created_at=base,
        updated_at=base,
        redacted_at=None,
        embedding_state=EmbeddingState.PENDING,
        expires_at=expires_at,
        procedure_confidence=confidence,
    )
    embedding = EmbeddingWrite(
        artifact_id=artifact_id,
        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
        client_id=owner,
        provider=vectors.provider,
        model_id=vectors.model_id,
        vec=vectors.embed_one(body),
        expires_at=expires_at,
    )
    context = SupersessionContext(
        session_id=session.id,
        agent_cli=session.agent_cli,
        machine_id=session.machine_id,
        expires_at=expires_at,
    )

    def transaction(cursor: Cursor) -> tuple[UUID, ...]:
        insert_artifact_with_embedding(cursor, artifact, embedding)
        insert_edges(
            cursor,
            artifact_id,
            tuple(
                ParentRef(
                    parent_id=parent.id,
                    parent_kind=parent.kind,
                    derivation_method=derivation,
                )
                for parent in parents
            ),
        )
        written = write_bindings(
            cursor,
            DetectionRequest(
                artifact=ArtifactRef(
                    id=artifact_id,
                    kind=ArtifactKind.DERIVED_ARTIFACT,
                    client_id=owner,
                ),
                scope_client_id=owner,
                text=body,
                parents=parents,
            ),
            context=context,
            detected_at=base,
        )
        return tuple(item.detection.client_id for item in written)

    bound = store.in_serializable(transaction, label=_ARTIFACT_LABEL)
    return SeededArtifact(id=artifact_id, kind=kind, client_ids=bound)


def _cross_client_sessions(
    sessions: tuple[SeededSession, ...],
    offset: int,
) -> tuple[SeededSession, ...]:
    """Pick two or three Sessions of distinct tenants, in a fixed traversal order."""
    seen: dict[UUID, SeededSession] = {}
    wanted = 2 + (offset % 2)
    for index in range(len(sessions)):
        session = sessions[(offset + index) % len(sessions)]
        if session.client_id not in seen:
            seen[session.client_id] = session
        if len(seen) == wanted:
            break
    return tuple(seen.values())


def _domain_of_client(clients: tuple[SeededClient, ...], client_id: UUID) -> ClientDomain:
    """The domain a seeded tenant identifier belongs to."""
    for client in clients:
        if client.id == client_id:
            return client.domain
    raise KeyError("the identifier names no seeded client")


# ---------------------------------------------------------------------------
# Attribution history and procedural standing
# ---------------------------------------------------------------------------


def _supersede_some(
    store: MemoryStore,
    *,
    artifacts: tuple[SeededArtifact, ...],
    sessions: tuple[SeededSession, ...],
    base: datetime,
) -> tuple[UUID, ...]:
    """Give a handful of Artifacts a marker detection after their scope detection.

    The second detection carries the marker method and the marker confidence, so
    the attribution write path closes the scope version with a real validity
    interval, appends the supersession Ledger Event, and leaves the marker version
    current. That is what makes the as-of query answer differently at two instants.
    """
    if not sessions:
        return ()
    superseded: list[UUID] = []
    for offset, artifact in enumerate(artifacts[:_SUPERSEDED_ARTIFACTS]):
        session = sessions[offset % len(sessions)]
        expires_at = base + timedelta(days=DEFAULT_RETENTION_DAYS)
        submission = AttributionSubmission(
            artifact_id=artifact.id,
            artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
            client_id=artifact.client_ids[0],
            method=BindingMethod.MARKER,
            confidence=max(MARKER_CONFIDENCE, SCOPE_CONFIDENCE),
            detected_at=base + _SESSION_SPACING * (offset + 1),
        )
        context = SupersessionContext(
            session_id=session.id,
            agent_cli=session.agent_cli,
            machine_id=session.machine_id,
            expires_at=expires_at,
        )
        if _record_marker(store, submission=submission, context=context) is not None:
            superseded.append(artifact.id)
    return tuple(superseded)


def _record_marker(
    store: MemoryStore,
    *,
    submission: AttributionSubmission,
    context: SupersessionContext,
) -> UUID | None:
    """Record one marker detection in a transaction of its own, reporting what closed."""

    def transaction(cursor: Cursor) -> UUID | None:
        return record_attribution(cursor, submission, context=context).superseded_id

    return store.in_serializable(transaction, label=_ARTIFACT_LABEL)


def _drive_standing(
    store: MemoryStore,
    *,
    artifacts: tuple[SeededArtifact, ...],
    sessions: tuple[SeededSession, ...],
    policy: ConfidencePolicy,
) -> tuple[UUID, ...]:
    """Move every seeded procedure's standing through the ordinary tracker path.

    Each procedure is retrieved by Sessions of the tenants it is bound to and then
    reports each of those Sessions' own terminal outcomes, so the value it ends at
    was earned by outcomes rather than assigned. The last procedure is driven by
    failures alone, which is what puts a procedure below the recall floor while the
    store retains it.
    """
    procedures = tuple(
        artifact for artifact in artifacts if artifact.kind is DerivedArtifactKind.LEARNED_PROCEDURE
    )
    if not procedures or not sessions:
        return ()
    terminal = tuple(
        session for session in sessions if session.outcome is not SessionOutcome.IN_PROGRESS
    )
    failures = tuple(session for session in terminal if session.outcome is SessionOutcome.FAILED)
    below: list[UUID] = []

    for offset, procedure in enumerate(procedures):
        last = offset == len(procedures) - 1
        chosen = failures if last else terminal[: 3 + offset]
        value = policy.initial
        for session in chosen:
            record_retrieval(store, procedure.id, session.id)
            change = record_outcome(store, procedure.id, session.id, session.outcome, policy=policy)
            if change is not None:
                value = change.new_value
        if policy.below_floor(value):
            below.append(procedure.id)
    return tuple(below)


# ---------------------------------------------------------------------------
# Narrowing
# ---------------------------------------------------------------------------


def _as_uuid(value: object) -> UUID:
    """Read an identifier out of a returned column, accepting either form."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise ValueError("the seeded write did not return an identifier")
