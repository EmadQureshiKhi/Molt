"""Contamination planting and the ground truth that is deliberately kept outside.

The contamination this module plants is genuine in the sense that matters. The
fragment's content belongs to one tenant's domain while the row it lives in is
scoped to another, and nothing in or around the fragment names the owning tenant:
not its slug, not its display name, not its repository name, not a content marker,
not a directory name, and not any path-like string. The host's own scope binding
is the only binding the detector concludes, so an explicit sweep for the owner
cannot reach the row and only vector similarity can. That is the condition the
residue phase exists for, and planting it any other way would let a detector pass
by matching a label.

Three safeguards make that claim checkable rather than asserted.

**The stripper asserts its own result.** Every revealing token is removed and then
its absence is verified, and a surviving token raises rather than being written, so
a planting bug cannot quietly produce a label-detectable fragment.

**The planted row is re-read after it is written.** The stored text is checked for
every owner token again, and the stored bindings are checked for a binding naming
the owner, both from the cluster rather than from what was sent. A finding raises
before any ground truth is written.

**The ground truth lives outside the cluster.** The mapping is written to a file
the repository does not track, so nothing in the database references it and no
component that reads the database can read the answer. A detector that could reach
this mapping would not be demonstrating detection.

The owner keeps its own copy of each fragment, written into one of its own
Sessions with its paths and markers intact, because that is what a real leak looks
like: the original is in the owner's history and the copy is in someone else's.
The recovery test draws its query from the owner's own stored content and never
from this mapping.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from random import Random
from typing import Final
from uuid import UUID, uuid4

from molt.config.resolve import Configuration
from molt.errors import MoltError
from molt.models.artifact import ArtifactKind, ArtifactRef
from molt.models.event import EmbeddingState, Event, EventCategory
from molt.seed.corpora import SeedVolumes, code_fragment, fragment_line_count
from molt.seed.generator import DEFAULT_RETENTION_DAYS, SeededClient, SeededSession, SeedResult
from molt.seed.vectors import SeedEmbedder
from molt.store import Cursor, MemoryStore
from molt.store.attribution import SupersessionContext
from molt.store.binding_detector import DetectionRequest, write_bindings
from molt.store.chain import LedgerAppend, append_in_transaction
from molt.store.embeddings import EmbeddingWrite, insert_embedding

__all__ = [
    "COMPONENT",
    "DEFAULT_GROUND_TRUTH_PATH",
    "GROUND_TRUTH_KEY",
    "OWNER_BINDING_QUERY",
    "PLANTED_TEXT_QUERY",
    "GroundTruth",
    "PlantedFragment",
    "SeedIntegrityError",
    "ground_truth_path",
    "load_ground_truth",
    "plant_contamination",
    "strip_owner_tokens",
    "write_ground_truth",
]

# The component name a failure from this module names itself by.
COMPONENT: Final[str] = "seed"

# Where the mapping is written when the configuration surface names no path, and
# the surface key that overrides it. The path is untracked on purpose.
GROUND_TRUTH_KEY: Final[str] = "MOLT_SENSITIVITY_GROUND_TRUTH"
DEFAULT_GROUND_TRUTH_PATH: Final[Path] = Path("seed/ground_truth.json")

# The two read-backs the integrity check performs, both against the cluster rather
# than against what was sent.
PLANTED_TEXT_QUERY: Final[str] = "SELECT text_body FROM ledger WHERE id = %s"
OWNER_BINDING_QUERY: Final[str] = (
    "SELECT count(*) FROM client_binding "
    "WHERE artifact_id = %s AND client_id = %s AND superseded_by IS NULL"
)

# What the label of a planted host action says, in the host's own terms. It names
# a question an operator would plausibly ask about pasted code and nothing about
# where the code came from.
_HOST_PROMPT_LABEL: Final[str] = "Review this helper a colleague sent over and tell me what breaks."

# The label the transactions of this module appear under in a log record.
_PLANT_LABEL: Final[str] = "seed_plant_fragment"

# The keys the mapping is written under. They are named here so a reader of the
# file and a reader of this module see one set of names.
_SEED_KEY: Final[str] = "seed"
_FRAGMENTS_KEY: Final[str] = "fragments"
_BLENDED_KEY: Final[str] = "blended_artifacts"


class SeedIntegrityError(MoltError):
    """A planted fragment or its stored row still reveals the owning tenant.

    This is raised rather than reported because a fragment that reveals its owner
    makes the whole residue demonstration meaningless: a detector would find it by
    matching a token instead of by measuring a distance.
    """


@dataclass(frozen=True, slots=True)
class PlantedFragment:
    """One planted fragment, as the ground-truth mapping records it."""

    fragment_index: int
    owner_client_slug: str
    host_client_slug: str
    host_session_id: UUID
    host_event_id: UUID
    origin_event_id: UUID
    fragment_digest: str
    line_count: int

    def as_document(self) -> dict[str, object]:
        """The mapping entry, with every identifier rendered as text."""
        return {
            "fragment_index": self.fragment_index,
            "owner_client_slug": self.owner_client_slug,
            "host_client_slug": self.host_client_slug,
            "host_session_id": str(self.host_session_id),
            "host_event_id": str(self.host_event_id),
            "origin_event_id": str(self.origin_event_id),
            "fragment_digest": self.fragment_digest,
            "line_count": self.line_count,
        }


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """The whole mapping: which tenant owns each planted fragment, and where it sits."""

    seed: int
    fragments: tuple[PlantedFragment, ...]
    blended_artifacts: tuple[tuple[UUID, tuple[str, ...]], ...]

    def as_document(self) -> dict[str, object]:
        """The mapping as the document that is written to the untracked file."""
        return {
            _SEED_KEY: self.seed,
            _FRAGMENTS_KEY: [fragment.as_document() for fragment in self.fragments],
            _BLENDED_KEY: [
                {"artifact_id": str(artifact_id), "client_slugs": list(slugs)}
                for artifact_id, slugs in self.blended_artifacts
            ],
        }

    @property
    def host_event_ids(self) -> tuple[UUID, ...]:
        """Every planted Event, which is the answer a recovery test checks against."""
        return tuple(fragment.host_event_id for fragment in self.fragments)


def ground_truth_path(configuration: Configuration | None = None) -> Path:
    """Where the mapping is written, read from the configuration surface.

    The surface key carries no default, because a path an operator did not choose
    is a file an operator does not know exists. Absent a configured value the
    package's own default path is used, which is the one the design names and the
    one the repository leaves untracked.
    """
    view = Configuration() if configuration is None else configuration
    configured = view.optional(GROUND_TRUTH_KEY)
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    return DEFAULT_GROUND_TRUTH_PATH


def strip_owner_tokens(text: str, tokens: tuple[str, ...]) -> str:
    """Remove every revealing token and every path-like string from a fragment.

    A path-like string is removed whole rather than having its separator removed,
    because a path with its slashes taken out still names the directories it ran
    through. Comparison is case-insensitive, because a token spelled differently
    reveals exactly as much.

    Raises:
        SeedIntegrityError: A token survived the removal, which would leave the
            fragment detectable by matching rather than by meaning.
    """
    stripped = text
    for token in sorted(tokens, key=len, reverse=True):
        if not token:
            continue
        stripped = _remove_case_insensitive(stripped, token)
    stripped = "\n".join(_without_paths(line) for line in stripped.split("\n"))
    _require_absent(stripped, tokens)
    return stripped


def plant_contamination(
    store: MemoryStore,
    result: SeedResult,
    *,
    volumes: SeedVolumes | None = None,
    embedder: SeedEmbedder | None = None,
    path: Path | None = None,
    configuration: Configuration | None = None,
) -> GroundTruth:
    """Plant cross-tenant fragments and write the separated ground truth.

    For each fragment a distinct owner and host are drawn, a fragment is generated
    from the owner's idiom, the owner's revealing tokens are stripped and their
    absence asserted, the owner's own copy is written into one of the owner's
    Sessions, and the stripped copy is written into one of the host's Sessions
    through the ordinary write path so only the host's scope binding is detected.
    Each planted row is then re-read and checked, and the mapping is written last.

    Args:
        store: The connection surface every transaction is framed by.
        result: What the generation produced, which the owner and host are drawn
            from and whose seed the mapping records.
        volumes: How many fragments to plant, defaulting to the design's count.
        embedder: The vector function each fragment is embedded through.
        path: Where to write the mapping, overriding the configured path.
        configuration: The configuration view the path is read from.

    Returns:
        The mapping that was written, which a recovery test reads to check an
        answer it produced without it.

    Raises:
        SeedIntegrityError: A planted fragment or its stored row still reveals its
            owner. Nothing is written to the mapping file.
        ValueError: Fewer than two tenants were seeded, so no fragment can cross a
            tenant boundary.
    """
    chosen = SeedVolumes() if volumes is None else volumes
    vectors = SeedEmbedder() if embedder is None else embedder
    if len(result.clients) < 2:
        raise ValueError("planting a cross-client fragment needs at least two seeded clients")
    rng = Random(result.seed)  # noqa: S311 - reproducible sample content, never a secret

    planted: list[PlantedFragment] = []
    for index in range(chosen.planted_fragments):
        owner, host = _draw_pair(result.clients, index)
        owner_session = _session_of(result, owner.id, index)
        host_session = _session_of(result, host.id, index + 1)
        lines = fragment_line_count(rng)
        fragment = code_fragment(owner.domain, rng, lines=lines)
        stripped = strip_owner_tokens(fragment, owner.domain.owner_tokens)
        origin_id = _write_origin(
            store,
            owner=owner,
            session=owner_session,
            fragment=fragment,
            index=index,
            base=result.generated_at,
            vectors=vectors,
        )
        host_event_id = _write_planted(
            store,
            host=host,
            session=host_session,
            fragment=stripped,
            index=index,
            base=result.generated_at,
            vectors=vectors,
        )
        _verify_planted(
            store,
            event_id=host_event_id,
            owner=owner,
        )
        planted.append(
            PlantedFragment(
                fragment_index=index,
                owner_client_slug=owner.slug,
                host_client_slug=host.slug,
                host_session_id=host_session.id,
                host_event_id=host_event_id,
                origin_event_id=origin_id,
                fragment_digest=hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
                line_count=len(stripped.split("\n")),
            )
        )

    truth = GroundTruth(
        seed=result.seed,
        fragments=tuple(planted),
        blended_artifacts=tuple(
            (
                artifact.id,
                tuple(
                    sorted(_slug_of(result.clients, client_id) for client_id in artifact.client_ids)
                ),
            )
            for artifact in result.blended_artifacts
        ),
    )
    write_ground_truth(truth, path=path, configuration=configuration)
    return truth


def write_ground_truth(
    truth: GroundTruth,
    *,
    path: Path | None = None,
    configuration: Configuration | None = None,
) -> Path:
    """Write the mapping to the untracked file and report where it landed."""
    destination = ground_truth_path(configuration) if path is None else path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(truth.as_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def load_ground_truth(path: Path) -> GroundTruth:
    """Read a mapping back from the file it was written to."""
    document = json.loads(path.read_text(encoding="utf-8"))
    entries = document[_FRAGMENTS_KEY]
    blended = document.get(_BLENDED_KEY, [])
    return GroundTruth(
        seed=int(document[_SEED_KEY]),
        fragments=tuple(
            PlantedFragment(
                fragment_index=int(entry["fragment_index"]),
                owner_client_slug=str(entry["owner_client_slug"]),
                host_client_slug=str(entry["host_client_slug"]),
                host_session_id=UUID(str(entry["host_session_id"])),
                host_event_id=UUID(str(entry["host_event_id"])),
                origin_event_id=UUID(str(entry["origin_event_id"])),
                fragment_digest=str(entry["fragment_digest"]),
                line_count=int(entry["line_count"]),
            )
            for entry in entries
        ),
        blended_artifacts=tuple(
            (UUID(str(item["artifact_id"])), tuple(str(slug) for slug in item["client_slugs"]))
            for item in blended
        ),
    )


# ---------------------------------------------------------------------------
# The two writes
# ---------------------------------------------------------------------------


def _write_origin(
    store: MemoryStore,
    *,
    owner: SeededClient,
    session: SeededSession,
    fragment: str,
    index: int,
    base: datetime,
    vectors: SeedEmbedder,
) -> UUID:
    """Write the owner's own copy, with its repository and markers left in place."""
    marker = owner.domain.content_markers[0]
    text = f"{owner.domain.repository}/{owner.domain.directories[0]}\n{marker}\n{fragment}"
    return _write_fragment_event(
        store,
        client_id=owner.id,
        session=session,
        category=EventCategory.FILE_WRITE,
        text=text,
        payload_label=owner.domain.repository,
        index=index,
        base=base,
        vectors=vectors,
    )


def _write_planted(
    store: MemoryStore,
    *,
    host: SeededClient,
    session: SeededSession,
    fragment: str,
    index: int,
    base: datetime,
    vectors: SeedEmbedder,
) -> UUID:
    """Write the stripped copy as a host action inside a host-scoped Session."""
    text = f"{_HOST_PROMPT_LABEL}\n{fragment}"
    return _write_fragment_event(
        store,
        client_id=host.id,
        session=session,
        category=EventCategory.USER_PROMPT,
        text=text,
        payload_label=host.domain.repository,
        index=index,
        base=base,
        vectors=vectors,
    )


def _write_fragment_event(
    store: MemoryStore,
    *,
    client_id: UUID,
    session: SeededSession,
    category: EventCategory,
    text: str,
    payload_label: str,
    index: int,
    base: datetime,
    vectors: SeedEmbedder,
) -> UUID:
    """Append one fragment-carrying Event, its vector, and its bindings together.

    The write is the ordinary one: the chain append, the Embedding in the same
    transaction, and the Binding_Detector over the stored text. Nothing here writes
    a binding of its own, which is why the host's scope claim is the only claim the
    planted row carries.
    """
    expires_at = base + timedelta(days=DEFAULT_RETENTION_DAYS)
    occurred_at = session.started_at + timedelta(seconds=index + 1)
    event = Event(
        id=uuid4(),
        session_id=session.id,
        client_id=client_id,
        category=category,
        occurred_at=occurred_at,
        agent_cli=session.agent_cli,
        machine_id=session.machine_id,
        parent_event_id=None,
        payload={"workspace": payload_label},
        redacted=False,
        text_body=text,
    )
    context = SupersessionContext(
        session_id=session.id,
        agent_cli=session.agent_cli,
        machine_id=session.machine_id,
        expires_at=expires_at,
    )

    def body(cursor: Cursor) -> UUID:
        append_in_transaction(
            cursor,
            LedgerAppend(
                event=event,
                expires_at=expires_at,
                embedding_state=EmbeddingState.PENDING,
            ),
        )
        insert_embedding(
            cursor,
            EmbeddingWrite(
                artifact_id=event.id,
                artifact_kind=ArtifactKind.EVENT,
                client_id=client_id,
                provider=vectors.provider,
                model_id=vectors.model_id,
                vec=vectors.embed_one(text),
                expires_at=expires_at,
            ),
        )
        write_bindings(
            cursor,
            DetectionRequest(
                artifact=ArtifactRef(id=event.id, kind=ArtifactKind.EVENT, client_id=client_id),
                scope_client_id=client_id,
                text=text,
            ),
            context=context,
            detected_at=occurred_at,
        )
        return event.id

    return store.in_serializable(body, label=_PLANT_LABEL)


# ---------------------------------------------------------------------------
# The integrity check, read back from the cluster
# ---------------------------------------------------------------------------


def _verify_planted(store: MemoryStore, *, event_id: UUID, owner: SeededClient) -> None:
    """Re-read a planted row and refuse it if anything about it names the owner."""

    def body(cursor: Cursor) -> tuple[str | None, int]:
        cursor.execute(PLANTED_TEXT_QUERY, (event_id,))
        row = cursor.fetchone()
        if row is None:
            raise SeedIntegrityError("a planted fragment was not stored, so nothing was planted")
        stored = None if row[0] is None else str(row[0])
        cursor.execute(OWNER_BINDING_QUERY, (event_id, owner.id))
        counted = cursor.fetchone()
        return stored, 0 if counted is None else int(str(counted[0]))

    stored, bindings = store.read(body)
    if stored is None:
        raise SeedIntegrityError("a planted fragment was stored with no text to be found by")
    _require_absent(stored, owner.domain.owner_tokens)
    if bindings != 0:
        raise SeedIntegrityError(
            "a planted fragment carries a binding naming the owning client, so an "
            "explicit sweep would find it and no similarity search is needed"
        )


def _require_absent(text: str, tokens: tuple[str, ...]) -> None:
    """Refuse text still carrying a revealing token or a path-like string."""
    lowered = text.lower()
    for token in tokens:
        if token and token.lower() in lowered:
            raise SeedIntegrityError(
                "a planted fragment still carries a token that reveals its owning client"
            )
    for line in text.split("\n"):
        for word in line.split():
            if _is_path_like(word):
                raise SeedIntegrityError(
                    "a planted fragment still carries a path-like string, which could "
                    "reveal its owning client"
                )


def _remove_case_insensitive(text: str, token: str) -> str:
    """Remove every occurrence of a token, whatever case it was written in."""
    lowered_token = token.lower()
    produced = text
    while True:
        found = produced.lower().find(lowered_token)
        if found < 0:
            return produced
        produced = produced[:found] + produced[found + len(token) :]


def _without_paths(line: str) -> str:
    """Drop every path-like word from one line, keeping the rest of the line."""
    kept = [word for word in line.split(" ") if not _is_path_like(word)]
    return " ".join(kept)


def _is_path_like(word: str) -> bool:
    """Whether a word reads as a filesystem path rather than as code.

    A separator between two non-empty parts is what makes a word a path. A
    division inside an expression is written with spaces around it by every
    generator here, so nothing that is arithmetic is mistaken for a path.
    """
    trimmed = word.strip("\"'(),;:")
    if "/" not in trimmed and "\\" not in trimmed:
        return False
    parts = [part for part in trimmed.replace("\\", "/").split("/") if part]
    return len(parts) >= 2


# ---------------------------------------------------------------------------
# Drawing the owner and the host
# ---------------------------------------------------------------------------


def _draw_pair(clients: tuple[SeededClient, ...], index: int) -> tuple[SeededClient, SeededClient]:
    """Draw a distinct owner and host, in a fixed traversal order over the tenants."""
    owner = clients[index % len(clients)]
    host = clients[(index + 1 + (index // len(clients))) % len(clients)]
    if host.id == owner.id:
        host = clients[(index + 1) % len(clients)]
    return owner, host


def _session_of(result: SeedResult, client_id: UUID, offset: int) -> SeededSession:
    """One Session of a tenant, chosen in a fixed traversal order.

    Raises:
        ValueError: The tenant holds no seeded Session, so nothing can be planted
            inside one.
    """
    owned = tuple(session for session in result.sessions if session.client_id == client_id)
    if not owned:
        raise ValueError("a planted fragment needs a Session of the client it is scoped to")
    return owned[offset % len(owned)]


def _slug_of(clients: tuple[SeededClient, ...], client_id: UUID) -> str:
    """The slug of a seeded tenant identifier."""
    for client in clients:
        if client.id == client_id:
            return client.slug
    raise KeyError("the identifier names no seeded client")
