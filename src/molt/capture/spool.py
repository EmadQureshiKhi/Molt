"""The bounded local spool the capture side buffers Events into when ingest fails.

The spool exists so that an unreachable Collector costs the engineer nothing: the
Events are written to a file, the hook exits 0, and the next hook invocation sends
them (Requirements 6.1, 6.2). Five claims arrange this module.

**The file holds records, never signed requests, and that is the reason for the
whole shape of it.** A transmission carries a timestamp header and a signature
over that timestamp and the body, and the Collector refuses a request whose
timestamp is older than the configured maximum age (Requirement 47.5). Spooling a
prepared request would therefore mean spooling its timestamp, so an outage longer
than the age bound would produce a spool full of batches that are rejected the
moment they are finally sendable, and the longer the outage the more certain the
loss. Holding the Event records alone means the batch flushed after an outage is
signed at transmission with a fresh timestamp and lands inside the age bound
whatever the outage cost (Requirement 47.10). Nothing in this module computes a
signature or holds a secret.

**One JSON record per line, appended, so concurrent hook processes interleave at
record granularity.** Several agent tools may fire hooks on one machine at once,
and they share one spool file per machine. Each append opens the file in append
mode, which carries `O_APPEND`, so the kernel places every write at the current
end of file rather than at an offset the process remembered, and one batch is
written with a single call so its records land contiguously. The record form is
the Event wire form, which is single-line by construction: it is produced by
`json.dumps` with no insignificant whitespace and with control characters escaped,
so no field value can introduce a newline and split one record into two.

**The bound is enforced by dropping whole records from the head.** When the file
exceeds the configured maximum, records are counted off the front until what
remains fits, the survivors are streamed into a sibling temporary file, and that
file is renamed over the original (Requirement 6.5). The rename is what makes the
operation safe to be interrupted: a reader either sees the whole old file or the
whole new one, never a half-rewritten one, because a rename within one directory
replaces the name atomically. Compaction is serialised by an advisory lock on a
sibling lock file, taken without waiting: a process that cannot take it does
nothing, because whoever holds it is already bringing the file inside the bound,
and waiting would spend the hook's latency budget to duplicate that work.

**The discarded count lives in its own file, so the rename cannot destroy it.**
The count is evidence of loss and it is what the next successful transmission
reports (Requirement 6.5), so it must outlive the compaction that produced it. It
is kept in a sibling counter file that compaction never renames over, updated by
the same write-and-rename that compaction uses, and cleared only once a
transmission has succeeded and the count has been reported. A record longer than
the bound is itself discarded and counted, because no sequence of head drops can
bring a file inside a bound that one of its records exceeds; the alternative would
be a file that permanently reports itself over bound.

**Nothing here reaches a database and nothing here imports a driver.** The hook
process has a 250 ms budget at p95 (Requirement 1.8) and a driver import alone
would spend a large part of it, so the imports of this module are the standard
library, the configuration surface, the Event model, and the telemetry surface.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import platform
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from molt.config.resolve import Configuration
from molt.models.event import Event, deserialise_event, serialise_event
from molt.telemetry import Severity, log, metric

__all__ = [
    "BYTES_UNIT",
    "CLAIM_INFIX",
    "COMPONENT",
    "COUNTER_SUFFIX",
    "DEFAULT_MAX_BYTES",
    "LOCK_SUFFIX",
    "RECORD_SEPARATOR",
    "SPOOL_BYTES_METRIC",
    "SPOOL_DISCARDED_METRIC",
    "SPOOL_PREFIX",
    "SPOOL_SUFFIX",
    "Spool",
    "SpoolAppend",
    "SpooledBatch",
    "resolve_machine_id",
    "spool_path",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "capture"

# The two measurements the design's metric table names for the spool. Both are
# undimensioned: a per-Client dimension would multiply into the billable metric
# bound the telemetry surface exists to hold, and the identities belong in the
# log record instead.
SPOOL_BYTES_METRIC: Final[str] = "capture.spool_bytes"
SPOOL_DISCARDED_METRIC: Final[str] = "capture.spool_discarded"

# The unit a byte count is published under, alongside the telemetry surface's
# default count unit.
BYTES_UNIT: Final[str] = "Bytes"

# The file names, all four derived from the machine identifier so that spool
# files from two machines sharing a directory over a synchronised folder do not
# collide.
SPOOL_PREFIX: Final[str] = "spool-"
SPOOL_SUFFIX: Final[str] = ".ndjson"
COUNTER_SUFFIX: Final[str] = ".discarded"
LOCK_SUFFIX: Final[str] = ".lock"

# What a claimed batch's file name carries between the spool name and its unique
# tail. A claim name is unique per claim rather than fixed, so two processes
# claiming at once cannot rename their spool over each other's claim.
CLAIM_INFIX: Final[str] = ".claim-"

# One record ends here. The wire form carries no newline of its own, so the
# separator is the only newline in the file.
RECORD_SEPARATOR: Final[bytes] = b"\n"

# The bound of Requirement 6.5, matching the configured default.
DEFAULT_MAX_BYTES: Final[int] = 67108864

# How much is moved per read while surviving records are streamed into the
# temporary file. Bounded so compaction of a full spool holds one buffer rather
# than the file.
_COPY_CHUNK_BYTES: Final[int] = 262144

# Characters a machine identifier may contribute to a file name. Anything else is
# replaced, so an identifier read from configuration cannot steer a write out of
# the spool directory.
_FILE_SAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")

# How much of the host digest a derived machine identifier carries. Enough that
# two hosts do not collide, short enough to keep a file name readable.
_DERIVED_ID_LENGTH: Final[int] = 16

# What a derived identifier is prefixed with, so an operator reading a spool
# directory can tell a derived identifier from a configured one.
_DERIVED_ID_PREFIX: Final[str] = "host"

# The permissions a spool file is created with: readable and writable by its
# owner alone, because a payload has already passed the Redactor but is still
# memory content.
_FILE_MODE: Final[int] = 0o600
_DIRECTORY_MODE: Final[int] = 0o700


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpoolAppend:
    """What one append to the spool did.

    Attributes:
        written: How many records were appended.
        discarded: How many records were dropped from the head to hold the bound,
            which is zero on every append that did not breach it.
        size_bytes: What the file measured once the bound had been enforced.
    """

    written: int
    discarded: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SpooledBatch:
    """The records one claim took out of the spool, and the file they came from.

    The claim file is carried because the batch is not yet accounted for: the
    caller confirms it once a transmission has succeeded, or releases it back into
    the spool when the transmission failed, and either way this is the file that
    is acted on.

    Attributes:
        events: The claimed records, in the order the file held them.
        discarded: The count standing in the counter file when the claim was made,
            which the caller reports on a successful transmission.
        unreadable: How many lines of the claimed file could not be read back as
            an Event, so a caller can report loss it did not cause.
        size_bytes: How many bytes the claimed file held.
        claim_path: The file the records were taken from, or None when the spool
            was empty and nothing was claimed.
    """

    events: tuple[Event, ...]
    discarded: int
    unreadable: int
    size_bytes: int
    claim_path: Path | None

    @property
    def empty(self) -> bool:
        """Whether the claim found nothing to transmit."""
        return not self.events and self.claim_path is None


# ---------------------------------------------------------------------------
# Machine identity
# ---------------------------------------------------------------------------


def resolve_machine_id(configuration: Configuration) -> str:
    """The machine identifier the spool file is named for.

    The configured value wins. An empty configured value means derive a stable one
    from the host, which is what the configuration surface's default expresses,
    and the derivation is a digest of the host name rather than the host name
    itself so a file name in a shared directory names no host.

    The capture side reads its machine identifier through this rather than
    deriving one of its own, so the identifier on an Event and the identifier in
    the spool file name are the same value.
    """
    configured = configuration.text("MOLT_MACHINE_ID").strip()
    if configured:
        return _file_safe(configured)
    digest = hashlib.sha256(platform.node().encode("utf-8")).hexdigest()
    return f"{_DERIVED_ID_PREFIX}-{digest[:_DERIVED_ID_LENGTH]}"


def _file_safe(value: str) -> str:
    """Reduce a value to the characters a file name may carry.

    A machine identifier arrives from configuration, so it is untrusted input to
    a path join. Replacing every other character means no identifier can name a
    parent directory or a separator, and the result is still stable for a given
    identifier.
    """
    reduced = _FILE_SAFE.sub("-", value).strip("-.")
    if not reduced:
        raise ValueError("a machine identifier must carry at least one usable character")
    return reduced


def spool_path(directory: Path, machine_id: str) -> Path:
    """The spool file one machine appends to inside a spool directory."""
    return directory / f"{SPOOL_PREFIX}{_file_safe(machine_id)}{SPOOL_SUFFIX}"


# ---------------------------------------------------------------------------
# The spool
# ---------------------------------------------------------------------------


class Spool:
    """One machine's bounded spool file, with its counter and its compaction lock.

    An instance holds no open file between calls. Every operation opens what it
    needs and closes it, which is what lets one process compact or claim the file
    while another appends to it: an append that opened the file after a rename
    writes to the new file, and an append that opened it before writes to the file
    that was renamed aside, whose records the claim that renamed it will carry.
    """

    __slots__ = ("_directory", "_machine_id", "_max_bytes")

    def __init__(
        self,
        directory: Path,
        machine_id: str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("the spool bound must admit at least one byte")
        self._directory = directory.expanduser()
        self._machine_id = _file_safe(machine_id)
        self._max_bytes = max_bytes

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration,
        *,
        machine_id: str | None = None,
    ) -> Spool:
        """Build a spool from the resolved configuration surface.

        The machine identifier may be supplied by a caller that has already
        resolved one for the Events it is about to spool, so the two cannot
        disagree.
        """
        return cls(
            configuration.path("MOLT_SPOOL_DIR"),
            machine_id if machine_id is not None else resolve_machine_id(configuration),
            max_bytes=configuration.integer("MOLT_SPOOL_MAX_BYTES"),
        )

    # -- properties ------------------------------------------------------

    @property
    def directory(self) -> Path:
        """The directory holding this machine's spool file and its siblings."""
        return self._directory

    @property
    def machine_id(self) -> str:
        """The machine identifier every one of the file names is derived from."""
        return self._machine_id

    @property
    def max_bytes(self) -> int:
        """The bound the file is held within by dropping records from its head."""
        return self._max_bytes

    @property
    def path(self) -> Path:
        """The spool file itself."""
        return spool_path(self._directory, self._machine_id)

    @property
    def counter_path(self) -> Path:
        """The counter file the discarded count survives compaction in."""
        return self._sibling(COUNTER_SUFFIX)

    @property
    def lock_path(self) -> Path:
        """The file compaction takes its advisory lock on."""
        return self._sibling(LOCK_SUFFIX)

    # -- reading the state -----------------------------------------------

    def size_bytes(self) -> int:
        """What the spool file measures, or zero when there is no file yet."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def is_empty(self) -> bool:
        """Whether the spool holds nothing to transmit."""
        return self.size_bytes() == 0

    def discarded_count(self) -> int:
        """How many records have been discarded since the last reported success.

        A counter file that is absent, empty, or unreadable reads as zero: the
        count is a report of loss, and failing a caller over the report would turn
        one loss into two.
        """
        try:
            text = self.counter_path.read_text(encoding="utf-8")
        except OSError:
            return 0
        try:
            count = int(text.strip() or "0", 10)
        except ValueError:
            return 0
        return max(count, 0)

    def records(self) -> tuple[Event, ...]:
        """Every record the spool currently holds, leaving the file untouched.

        This is a read for a caller that wants to look without taking, such as a
        diagnostic command. A transmission uses `claim`, because a read that does
        not take leaves the records to be sent twice.
        """
        events, _ = _read_records(self.path)
        return events

    # -- appending -------------------------------------------------------

    def append(self, events: Sequence[Event]) -> SpoolAppend:
        """Append records to the spool and hold the file within its bound.

        The batch is serialised in full before the file is opened and written with
        one call, so a concurrent appender's records cannot land in the middle of
        one of these records and the interleaving happens between records instead.

        Args:
            events: The Events to buffer, which may be empty.

        Returns:
            What was written, what the bound cost, and the resulting size.

        Raises:
            OSError: The spool directory or the spool file could not be written.
                The capture entry point's total handler is what turns this into a
                diagnostic line and an exit status of 0.
        """
        if not events:
            return SpoolAppend(written=0, discarded=0, size_bytes=self.size_bytes())
        blob = b"".join(
            serialise_event(event).encode("utf-8") + RECORD_SEPARATOR for event in events
        )
        self._append_bytes(blob)
        discarded = self.enforce_bound()
        return SpoolAppend(written=len(events), discarded=discarded, size_bytes=self.size_bytes())

    def _append_bytes(self, blob: bytes) -> None:
        """Append already-serialised record bytes, creating the file if absent.

        Append mode carries `O_APPEND`, so the write is placed at the end of the
        file as it stands at that moment rather than at a remembered offset. There
        is no flush to the platter here on purpose: the hook's latency budget does
        not admit one per invocation, and the records are already out of the
        process and in the operating system's hands.
        """
        self._directory.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(blob)

    # -- the bound -------------------------------------------------------

    def enforce_bound(self) -> int:
        """Drop records from the head until the file is within the bound.

        Returns how many records were discarded, which is zero when the file was
        already within the bound and zero when another process holds the
        compaction lock, because that process is performing this same work.

        A failure to compact is reported as a log record rather than raised: the
        records that provoked it are already appended, and failing the caller over
        a file that is merely too large would lose the batch that succeeded.
        """
        if self.size_bytes() <= self._max_bytes:
            return 0
        try:
            return self._locked_compaction()
        except OSError as error:
            log(
                Severity.WARNING,
                COMPONENT,
                "the spool file could not be compacted, so it remains above its bound",
                spool_path=str(self.path),
                max_bytes=self._max_bytes,
                error_type=type(error).__name__,
            )
            return 0

    def _locked_compaction(self) -> int:
        """Take the compaction lock without waiting, and compact while holding it.

        The lock is advisory and per open file description, so it serialises the
        rename against other processes rather than against other threads of this
        one. Taking it without waiting is deliberate: the holder is already
        bringing the file inside the bound, so a waiter would spend the hook's
        latency budget to arrive at work that had already been done.
        """
        self._directory.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
        descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT, _FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return 0
            try:
                return self._compact()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _compact(self) -> int:
        """Rewrite the spool as its surviving tail, and account for what was lost.

        The size is read again here rather than trusted from the caller, because
        the lock was taken after that reading and the file may have been compacted
        by whoever held the lock before this call.
        """
        size = self.size_bytes()
        if size <= self._max_bytes:
            return 0
        with self.path.open("rb") as source:
            discarded, offset = _first_surviving_offset(source, size, self._max_bytes)
            if discarded == 0:
                return 0
            source.seek(offset)
            self._replace_with_tail(source)
        self._add_discarded(discarded)
        log(
            Severity.WARNING,
            COMPONENT,
            "the oldest spooled records were discarded to hold the spool within its bound",
            spool_path=str(self.path),
            discarded=discarded,
            dropped_bytes=offset,
            max_bytes=self._max_bytes,
        )
        return discarded

    def _replace_with_tail(self, source: BinaryIO) -> None:
        """Stream what is left of an open spool file over the spool file itself.

        The temporary file is a sibling so that the rename stays inside one
        directory and therefore inside one filesystem, which is what makes it a
        replacement of a name rather than a copy. The contents are flushed to the
        platter before the rename, because a rename that outlives the data it
        names would leave a truncated file behind a valid name.
        """
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._directory,
            prefix=f"{SPOOL_PREFIX}{self._machine_id}.",
            suffix=".partial",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                shutil.copyfileobj(source, target, _COPY_CHUNK_BYTES)
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(self.path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    # -- the discarded count ---------------------------------------------

    def _add_discarded(self, count: int) -> None:
        """Add to the counter file, which the compaction rename never touches.

        The counter is written to a sibling temporary file and renamed over the
        counter, so an interrupted update leaves either the previous count or the
        new one and never a partial number.
        """
        if count <= 0:
            return
        total = self.discarded_count() + count
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._directory,
            prefix=f"{SPOOL_PREFIX}{self._machine_id}.",
            suffix=".counter",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{total}\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.counter_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def clear_discarded(self) -> None:
        """Forget the discarded count, once a transmission has reported it."""
        self.counter_path.unlink(missing_ok=True)

    # -- claiming, confirming, releasing ---------------------------------

    def claim(self) -> SpooledBatch:
        """Take the spooled records out of the file, for one transmission attempt.

        The file is renamed to a claim name unique to this claim, which is what
        makes the take atomic: from that instant an appending process creates a
        fresh spool file and adds to it, so nothing appended during the
        transmission is claimed twice and nothing claimed is appended over. Two
        processes claiming at the same moment cannot collide either, because one
        of the two renames finds no spool file at all and returns an empty batch.

        A caller confirms the batch once the transmission has succeeded, or
        releases it back into the spool when it failed.
        """
        if self.is_empty():
            return SpooledBatch(
                events=(),
                discarded=self.discarded_count(),
                unreadable=0,
                size_bytes=0,
                claim_path=None,
            )
        claim = self._claim_path()
        try:
            self.path.replace(claim)
        except OSError:
            return SpooledBatch(
                events=(),
                discarded=self.discarded_count(),
                unreadable=0,
                size_bytes=0,
                claim_path=None,
            )
        events, unreadable = _read_records(claim)
        if unreadable:
            log(
                Severity.WARNING,
                COMPONENT,
                "spooled lines could not be read back as Events and were dropped",
                spool_path=str(self.path),
                unreadable=unreadable,
            )
        size = claim.stat().st_size
        return SpooledBatch(
            events=events,
            discarded=self.discarded_count(),
            unreadable=unreadable,
            size_bytes=size,
            claim_path=claim,
        )

    def confirm(self, batch: SpooledBatch) -> None:
        """Account for a claimed batch that was transmitted, and report the loss.

        This is where the discarded count is published, which is what makes it a
        report on the next successful transmission rather than a report at the
        moment of loss: an operator learns of the loss on a path that also proves
        the spool is draining again.
        """
        if batch.claim_path is not None:
            batch.claim_path.unlink(missing_ok=True)
        if batch.size_bytes:
            metric(SPOOL_BYTES_METRIC, float(batch.size_bytes), unit=BYTES_UNIT)
        if batch.discarded:
            metric(SPOOL_DISCARDED_METRIC, float(batch.discarded))
            log(
                Severity.WARNING,
                COMPONENT,
                "spooled records discarded during the outage are reported on this success",
                discarded=batch.discarded,
                spool_bytes=batch.size_bytes,
            )
            self.clear_discarded()

    def release(self, batch: SpooledBatch) -> int:
        """Put a claimed batch back into the spool, its transmission having failed.

        The records are re-appended rather than the file being renamed back,
        because the spool may already hold records appended during the attempt and
        a rename would destroy them. Order across the two groups is not preserved
        and does not need to be: every record carries the instant it was observed,
        and interleaved appends from several hook processes never gave the file a
        global order to begin with.

        Returns how many records were put back.
        """
        claim = batch.claim_path
        if claim is None:
            return 0
        try:
            blob = claim.read_bytes()
        except OSError:
            return 0
        if blob:
            self._append_bytes(_terminated(blob))
        claim.unlink(missing_ok=True)
        self.enforce_bound()
        return len(batch.events)

    def abandoned_claims(self) -> tuple[Path, ...]:
        """Claim files left behind by a process that died mid-transmission.

        These are reported rather than folded back in automatically, because a
        claim file belonging to a transmission still in flight in another process
        is indistinguishable from one belonging to a process that died, and
        folding a live claim back into the spool would send its records twice.
        Recovery is therefore a decision a caller makes.
        """
        pattern = f"{SPOOL_PREFIX}{self._machine_id}{CLAIM_INFIX}*"
        try:
            return tuple(sorted(self._directory.glob(pattern)))
        except OSError:
            return ()

    def recover(self, claim: Path) -> int:
        """Fold one abandoned claim file back into the spool, returning its records.

        The caller is asserting that no transmission holds this file. Nothing here
        can check that, which is why the assertion is the caller's to make.
        """
        try:
            blob = claim.read_bytes()
        except OSError:
            return 0
        events, _ = _read_records(claim)
        if blob:
            self._append_bytes(_terminated(blob))
        claim.unlink(missing_ok=True)
        self.enforce_bound()
        return len(events)

    # -- names -----------------------------------------------------------

    def _sibling(self, suffix: str) -> Path:
        """A file sitting beside the spool file, named from the same identifier."""
        return self._directory / f"{SPOOL_PREFIX}{self._machine_id}{suffix}"

    def _claim_path(self) -> Path:
        """A claim name no other claim on this machine will choose."""
        unique = os.urandom(8).hex()
        return self._directory / f"{SPOOL_PREFIX}{self._machine_id}{CLAIM_INFIX}{unique}"


# ---------------------------------------------------------------------------
# Record scanning
# ---------------------------------------------------------------------------


def _first_surviving_offset(source: BinaryIO, size: int, max_bytes: int) -> tuple[int, int]:
    """Count records off the head until the rest fits, and report where it starts.

    The offset returned is a record boundary by construction, because it is the
    sum of the lengths of whole lines, so the tail streamed from it begins at the
    first record that leaves the file within the bound. A record longer than the
    bound is counted off like any other and the scan continues past it, which is
    what stops a single oversized record from holding the file permanently above
    its bound; when that record is the last one, the file is left empty and the
    record is counted as discarded, because nothing else can be true of a bound
    one record cannot fit inside.

    Returns:
        The number of records dropped and the byte offset the survivors start at.
    """
    dropped = 0
    offset = 0
    while size - offset > max_bytes:
        line = source.readline()
        if not line:
            break
        offset += len(line)
        dropped += 1
    return dropped, offset


def _read_records(path: Path) -> tuple[tuple[Event, ...], int]:
    """Read a spool file back into Events, counting the lines that would not read.

    A line that does not parse is dropped rather than raised over. An appending
    process may have been killed mid-write, so a torn final line is a state the
    reader has to have an answer for, and the answer that keeps the other records
    is to skip it.
    """
    events: list[Event] = []
    unreadable = 0
    try:
        with path.open("rb") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    events.append(deserialise_event(line.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    unreadable += 1
    except OSError:
        return tuple(events), unreadable
    return tuple(events), unreadable


def _terminated(blob: bytes) -> bytes:
    """Ensure a block of record bytes ends at a record boundary.

    A claim file whose last line was torn by a killed writer would otherwise be
    appended without its separator, and the next record appended after it would
    join the torn one into a single unreadable line rather than being lost with
    it.
    """
    return blob if blob.endswith(RECORD_SEPARATOR) else blob + RECORD_SEPARATOR
