"""The local invocation index a tool result Event is linked to its tool call through.

Requirement 7.8 puts the relationship between a result and its call in a parent
Event identifier column rather than in nested payload, and a hook fires as a fresh
process, so the process that observes a tool *result* has no memory of the Event
identifier the process that observed the tool *call* minted. Something outside both
processes has to hold that identifier for the few seconds between them. This module
is that something.

Five claims arrange it.

**It is a file, because the hook holds no database credential.** The capture path
imports no driver at all (Requirement 1.8), so the index cannot be a table. It
lives beside the spool, under the directory the operator already configured for
capture state, and it follows the spool's discipline: owner-only permissions, a
bounded record count, and every write performed as a rename of a fully written
temporary file, so a reader sees one whole state or another and never half of one.

**One file per Session, so contention is per Session rather than per machine.**
Several agent tools may run at once on one machine, and a single index file would
put all of their hook processes into one read-modify-write race. Keyed by Session,
the only processes that contend belong to the same agent run, and those are
serialised by the agent itself: it fires a hook and waits for it.

**A correlation identifier is looked up by name, and its absence falls back to
recency.** Where the vendor payload carries an identifier for the tool call, the
entry is recorded under it and the result finds its call exactly. Where the
specification defines none, the result takes the most recent unlinked call of the
same Session, preferring one whose tool name matches, which is the fallback the
design names.

**An entry expires, and the file is bounded.** A call whose result never arrived
would otherwise sit in the list and adopt an unrelated later result, so an entry
older than the retention window is discarded on the next access. The list is
bounded as well, and the file is removed once nothing is pending, so the directory
does not grow with the number of Sessions a machine has ever run.

**A failure here costs one link and nothing else.** Every filesystem fault is
swallowed: an unwritable directory means a tool result Event whose parent is unset,
which is a far smaller loss than a hook that failed. Nothing here raises.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Final
from uuid import UUID

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "INDEX_LOCK_SUFFIX",
    "INDEX_PREFIX",
    "INDEX_SUFFIX",
    "InvocationIndex",
    "PendingCall",
    "index_path",
]

# How the file is named. The machine identifier participates so that two machines
# sharing a directory over a synchronised folder do not collide, and the Session
# identifier participates because there is one file per Session.
INDEX_PREFIX: Final[str] = "calls-"
INDEX_SUFFIX: Final[str] = ".json"
INDEX_LOCK_SUFFIX: Final[str] = ".lock"

# How many unlinked calls one Session may hold. A batch of parallel tool calls is a
# handful, so this admits several batches and still bounds the file.
DEFAULT_MAX_ENTRIES: Final[int] = 64

# How long an unlinked call stays eligible to adopt a result, in seconds. Beyond
# this the call is treated as one whose result never arrived.
DEFAULT_TTL_SECONDS: Final[float] = 3600.0

# How the advisory lock is attempted: a few tries a few milliseconds apart, then
# the update proceeds without it. Waiting properly would spend the latency budget
# the lock exists to protect, and proceeding is safe because every write is a
# rename of a whole file.
_LOCK_ATTEMPTS: Final[int] = 3
_LOCK_PAUSE_SECONDS: Final[float] = 0.005

# The permissions the index is created with, matching the spool: a payload has
# passed the Redactor but a tool name is still memory content.
_FILE_MODE: Final[int] = 0o600
_DIRECTORY_MODE: Final[int] = 0o700

# Characters a machine identifier may contribute to a file name. An identifier
# arrives from configuration, so it is untrusted input to a path join.
_FILE_SAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")

# The document fields, named once so a reader and a writer cannot disagree.
_PENDING: Final[str] = "pending"
_EVENT_ID: Final[str] = "event_id"
_CORRELATION_ID: Final[str] = "correlation_id"
_TOOL_NAME: Final[str] = "tool_name"
_AT: Final[str] = "at"


class PendingCall:
    """One recorded tool call awaiting the result that will link to it."""

    __slots__ = ("at", "correlation_id", "event_id", "tool_name")

    def __init__(
        self,
        event_id: UUID,
        *,
        correlation_id: str | None,
        tool_name: str | None,
        at: float,
    ) -> None:
        self.event_id = event_id
        self.correlation_id = correlation_id
        self.tool_name = tool_name
        self.at = at

    def as_document(self) -> dict[str, object]:
        """The record form, with an absent field omitted rather than written as null."""
        document: dict[str, object] = {_EVENT_ID: str(self.event_id), _AT: self.at}
        if self.correlation_id is not None:
            document[_CORRELATION_ID] = self.correlation_id
        if self.tool_name is not None:
            document[_TOOL_NAME] = self.tool_name
        return document

    @classmethod
    def from_document(cls, document: object) -> PendingCall | None:
        """Read one record back, or report that it was not one."""
        if not isinstance(document, dict):
            return None
        raw_identifier = document.get(_EVENT_ID)
        if not isinstance(raw_identifier, str):
            return None
        try:
            identifier = UUID(raw_identifier)
        except ValueError:
            return None
        moment = document.get(_AT)
        correlation = document.get(_CORRELATION_ID)
        name = document.get(_TOOL_NAME)
        return cls(
            identifier,
            correlation_id=correlation if isinstance(correlation, str) else None,
            tool_name=name if isinstance(name, str) else None,
            at=float(moment) if isinstance(moment, (int, float)) else 0.0,
        )


def _file_safe(value: str) -> str:
    """Reduce a value to the characters a file name may carry."""
    reduced = _FILE_SAFE.sub("-", value).strip("-.")
    return reduced or "unnamed"


def index_path(directory: Path, machine_id: str, session_id: UUID) -> Path:
    """The index file one Session on one machine records its calls in."""
    return directory / f"{INDEX_PREFIX}{_file_safe(machine_id)}-{session_id.hex}{INDEX_SUFFIX}"


class InvocationIndex:
    """One machine's per-Session record of tool calls awaiting their results.

    An instance holds no open file between calls, so two hook processes may use the
    same index without either holding state the other cannot see.
    """

    __slots__ = ("_directory", "_machine_id", "_max_entries", "_ttl_seconds")

    def __init__(
        self,
        directory: Path,
        machine_id: str,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if max_entries < 1:
            raise ValueError("the invocation index must admit at least one pending call")
        if ttl_seconds <= 0.0:
            raise ValueError("the invocation index retention window must be positive")
        self._directory = directory.expanduser()
        self._machine_id = machine_id
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds

    @classmethod
    def from_environment(cls, machine_id: str) -> InvocationIndex:
        """Build an index in the configured capture directory.

        The configuration surface is read here rather than imported at module scope,
        because a hook event naming no tool call needs no index and should not pay
        for resolving one.
        """
        from molt.config.resolve import load_configuration

        return cls(load_configuration().path("MOLT_SPOOL_DIR"), machine_id)

    @property
    def directory(self) -> Path:
        """The directory the index files sit in, beside the spool."""
        return self._directory

    def path_for(self, session_id: UUID) -> Path:
        """The index file one Session records its calls in."""
        return index_path(self._directory, self._machine_id, session_id)

    def pending(self, session_id: UUID) -> tuple[PendingCall, ...]:
        """Every unlinked call currently recorded for a Session, oldest first."""
        return tuple(self._read(session_id))

    def record_call(
        self,
        session_id: UUID,
        event_id: UUID,
        *,
        at: float,
        correlation_id: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        """Record a tool call Event as awaiting its result."""
        descriptor = self._lock(session_id)
        try:
            entries = self._live(session_id, at)
            entries.append(
                PendingCall(
                    event_id,
                    correlation_id=correlation_id,
                    tool_name=tool_name,
                    at=at,
                )
            )
            self._write(session_id, entries[-self._max_entries :])
        finally:
            _release(descriptor)

    def take_call(
        self,
        session_id: UUID,
        *,
        at: float,
        correlation_id: str | None = None,
        tool_name: str | None = None,
    ) -> UUID | None:
        """Take the call a result links to, or None when no call is recorded.

        The vendor's correlation identifier decides when the payload carried one.
        Otherwise the most recent unlinked call of the same Session is taken, one of
        the same tool name in preference to one of another, which is the fallback the
        design names for a specification defining no correlation identifier.
        """
        descriptor = self._lock(session_id)
        try:
            entries = self._live(session_id, at)
            position = _match(entries, correlation_id, tool_name)
            if position is None:
                self._write(session_id, entries)
                return None
            taken = entries.pop(position)
            self._write(session_id, entries)
            return taken.event_id
        finally:
            _release(descriptor)

    def forget(self, session_id: UUID) -> None:
        """Drop a Session's index, as the end of that Session does."""
        try:
            self.path_for(session_id).unlink(missing_ok=True)
        except OSError:
            return

    # -- the file --------------------------------------------------------

    def _live(self, session_id: UUID, at: float) -> list[PendingCall]:
        """The recorded calls still inside the retention window."""
        return [entry for entry in self._read(session_id) if at - entry.at <= self._ttl_seconds]

    def _read(self, session_id: UUID) -> list[PendingCall]:
        """The recorded calls of one Session, or none when there is no readable file."""
        try:
            raw = self.path_for(session_id).read_bytes()
        except OSError:
            return []
        try:
            decoded: object = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return []
        if not isinstance(decoded, dict):
            return []
        listed = decoded.get(_PENDING)
        if not isinstance(listed, list):
            return []
        entries: list[PendingCall] = []
        for item in listed:
            entry = PendingCall.from_document(item)
            if entry is not None:
                entries.append(entry)
        return entries

    def _write(self, session_id: UUID, entries: list[PendingCall]) -> None:
        """Replace one Session's index, or remove it once nothing is pending."""
        target = self.path_for(session_id)
        if not entries:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                return
            return
        document = {_PENDING: [entry.as_document() for entry in entries]}
        blob = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
            handle, name = tempfile.mkstemp(
                dir=self._directory, prefix=INDEX_PREFIX, suffix=".partial"
            )
        except OSError:
            return
        temporary = Path(name)
        try:
            with os.fdopen(handle, "wb") as writer:
                writer.write(blob)
            temporary.chmod(_FILE_MODE)
            temporary.replace(target)
        except OSError:
            temporary.unlink(missing_ok=True)

    def _lock(self, session_id: UUID) -> int | None:
        """Take the Session's advisory lock, or report that it could not be taken."""
        lock_path = self.path_for(session_id).with_suffix(INDEX_LOCK_SUFFIX)
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT, _FILE_MODE)
        except OSError:
            return None
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if attempt + 1 < _LOCK_ATTEMPTS:
                    time.sleep(_LOCK_PAUSE_SECONDS)
                continue
            return descriptor
        try:
            os.close(descriptor)
        except OSError:
            return None
        return None


def _release(descriptor: int | None) -> None:
    """Release an advisory lock and close its descriptor, if one was taken."""
    if descriptor is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(descriptor)


def _match(
    entries: list[PendingCall],
    correlation_id: str | None,
    tool_name: str | None,
) -> int | None:
    """The position of the call a result belongs to, or None when there is none."""
    if correlation_id is not None:
        for position in reversed(range(len(entries))):
            if entries[position].correlation_id == correlation_id:
                return position
        return None
    if tool_name is not None:
        for position in reversed(range(len(entries))):
            if entries[position].tool_name == tool_name:
                return position
    return len(entries) - 1 if entries else None
