"""A fake cluster that records every statement the tool server sends.

The claims the tool server makes are claims about statements: the tenancy
admission is a bound array inside the statement, the row bound is a limit inside
the statement, and the read-only guarantee is that no statement mutates. A harness
that records statement text and bound parameters is therefore evidence about the
server rather than about a mock of the server, and it needs no cluster to be it.

What the fake answers is deliberately narrow. It answers the two closure
statements, the permitted-client resolution, and the reachability probe, and it
answers everything else with no rows. It applies the tenancy predicate and the
limit itself, from the parameters the statement bound, which is the only way an
emulated answer can be wrong in the same direction the real one would be.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from molt.erase.residue import ResiduePolicy
from molt.mcpserver import EventSink, McpServer
from molt.mcpserver.tools import (
    PERMITTED_CLIENT_IDS_STATEMENT,
    SELECT_PERMITTED_ANCESTORS_STATEMENT,
    SELECT_PERMITTED_DESCENDANTS_STATEMENT,
    McpSettings,
)
from molt.models.artifact import ArtifactKind
from molt.models.event import Event
from molt.recall import RecallEngine
from molt.store import MemoryStore

# The reader role name the server requires, in the short form the schema also
# admits.
READER_ROLE: Final[str] = "molt_reader"

# The vector width the stub provider answers with. Nothing reaches a vector index
# here, so one component is enough and keeps the statement log small.
STUB_VECTOR: Final[tuple[float, ...]] = (1.0,)

# The residue thresholds the harness runs under, inside the range the policy
# admits.
HARNESS_POLICY: Final[ResiduePolicy] = ResiduePolicy(
    auto_include_threshold=0.2,
    review_threshold=0.4,
    query_limit=10,
    top_k=10,
    excerpt_characters=256,
)

# The statement fragments no invocation may send. A statement carrying any of them
# would be a mutation, and the reader role would refuse it on a real cluster.
MUTATING_KEYWORDS: Final[tuple[str, ...]] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "UPSERT",
    "TRUNCATE",
    "DROP",
    "ALTER",
    "CREATE",
    "GRANT",
)


@dataclass(frozen=True, slots=True)
class Artifact:
    """One planted Artifact and the Clients holding a current binding to it."""

    artifact_id: UUID
    kind: ArtifactKind
    bound_clients: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class Corpus:
    """One corpus: its Clients, the permitted subset, and the Artifacts placed."""

    clients: tuple[UUID, ...]
    permitted: tuple[UUID, ...]
    slugs: tuple[str, ...]
    artifacts: tuple[Artifact, ...]

    @property
    def unpermitted(self) -> tuple[UUID, ...]:
        """The Clients this corpus holds that the server may not answer for."""
        allowed = set(self.permitted)
        return tuple(client for client in self.clients if client not in allowed)

    def visible_ids(self) -> tuple[UUID, ...]:
        """Every planted Artifact a permitted Client holds a current binding to."""
        allowed = set(self.permitted)
        return tuple(
            artifact.artifact_id for artifact in self.artifacts if artifact.bound_clients & allowed
        )

    def hidden_ids(self) -> tuple[UUID, ...]:
        """Every planted Artifact no permitted Client holds a current binding to."""
        visible = set(self.visible_ids())
        return tuple(
            artifact.artifact_id
            for artifact in self.artifacts
            if artifact.artifact_id not in visible
        )


@dataclass(frozen=True, slots=True)
class Sent:
    """One statement the server sent, with the parameters it bound."""

    statement: str
    parameters: tuple[object, ...]


@dataclass
class StatementLog:
    """Every statement one server sent, in order."""

    sent: list[Sent] = field(default_factory=list)

    def clear(self) -> None:
        """Forget what was sent, so one invocation can be read on its own."""
        self.sent.clear()

    def statements(self) -> tuple[str, ...]:
        """The statement text of everything sent."""
        return tuple(item.statement for item in self.sent)


class FakeCursor:
    """One cursor that records what it was asked and answers the read it knows."""

    def __init__(self, corpus: Corpus, log: StatementLog) -> None:
        self._corpus = corpus
        self._log = log
        self._rows: list[tuple[object, ...]] = []

    def execute(self, statement: str, parameters: Sequence[object] | None = None) -> None:
        bound = tuple(parameters or ())
        self._log.sent.append(Sent(statement=statement, parameters=bound))
        self._rows = self._answer(statement, bound)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self._rows = []

    def _answer(self, statement: str, bound: tuple[object, ...]) -> list[tuple[object, ...]]:
        if statement == PERMITTED_CLIENT_IDS_STATEMENT:
            return [(client,) for client in self._corpus.permitted]
        if statement == SELECT_PERMITTED_ANCESTORS_STATEMENT:
            return [(found.artifact_id, found.kind.value) for found in self._closure(bound)]
        if statement == SELECT_PERMITTED_DESCENDANTS_STATEMENT:
            return [(found.artifact_id,) for found in self._closure(bound)]
        if statement.strip() == "SELECT 1":
            return [(1,)]
        return []

    def _closure(self, bound: tuple[object, ...]) -> list[Artifact]:
        """The closure answer, applying the statement's own tenancy term and limit.

        The predicate is read off the parameters the statement bound rather than
        off the corpus, so a server that bound the wrong array would be answered
        with the wrong rows and the assertions would see it.
        """
        seeds = set(_uuids(bound[0]))
        permitted = set(_uuids(bound[1]))
        limit = bound[2] if isinstance(bound[2], int) else 0
        admitted = [
            artifact
            for artifact in self._corpus.artifacts
            if artifact.artifact_id not in seeds and artifact.bound_clients & permitted
        ]
        admitted.sort(key=lambda artifact: str(artifact.artifact_id))
        return admitted[:limit]


def _uuids(value: object) -> tuple[UUID, ...]:
    """The identifiers one bound array carries."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, UUID))


class FakeConnection:
    """One connection handing out recording cursors."""

    def __init__(self, corpus: Corpus, log: StatementLog) -> None:
        self._corpus = corpus
        self._log = log
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._corpus, self._log)

    def close(self) -> None:
        self._closed = True


class StubEmbedder:
    """One vector for one text, so recall reaches its statement without a provider."""

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [STUB_VECTOR for _ in texts]


class RecordingSink:
    """The recording seam, holding what the Collector would have been handed."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, events: Sequence[Event]) -> None:
        self.events.extend(events)


def fake_store(corpus: Corpus, log: StatementLog) -> MemoryStore:
    """A store over the recording connection, authenticated as the reader role."""

    def connect() -> FakeConnection:
        return FakeConnection(corpus, log)

    return MemoryStore(connect_with=connect, role=READER_ROLE)


def settings_for(corpus: Corpus, *, max_results: int, transport: str = "stdio") -> McpSettings:
    """The configured surface for one corpus, with the slugs it declares."""
    return McpSettings(
        transport=transport,
        bind_host="127.0.0.1",
        bind_port=0,
        permitted_client_slugs=corpus.slugs,
        max_results=max_results,
    )


def build_server(
    corpus: Corpus,
    *,
    max_results: int = 50,
    sink: EventSink | None = None,
    transport: str = "stdio",
) -> tuple[McpServer, StatementLog]:
    """One server over the fake cluster, with its permitted set already resolved."""
    log = StatementLog()
    store = fake_store(corpus, log)
    engine = RecallEngine(store, StubEmbedder(), recall_floor=0.5)
    server = McpServer(
        store,
        settings_for(corpus, max_results=max_results, transport=transport),
        engine=engine,
        policy=HARNESS_POLICY,
        permitted_clients=corpus.permitted,
        sink=sink,
    )
    return server, log


def artifact_ids(rows: Sequence[object]) -> Iterator[UUID]:
    """Every Artifact identifier a result's rows name."""
    for row in rows:
        if isinstance(row, dict):
            found = row.get("artifact_id")
            if isinstance(found, str):
                yield UUID(found)
