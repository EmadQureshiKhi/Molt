"""Pre-erasure backup evidence, taken directly or recorded as a reference.

An erasure deletes memory, and the claim a certificate makes about unrelated data
surviving is only checkable against something that held the cluster as it stood
before the first mutation. This module secures that something, and four claims
shape it.

**The path is chosen from the capability record, never from a version string.** A
tier that refuses a user-issued backup refuses to plan one, and the planning probe
that asked is recorded as a capability row. So the primary path here is entered
whenever that row does not report the fact probed absent, which includes the case
where nobody probed: an unprobed fact is not evidence of absence, and degrading on
a missing row would take the fallback nobody chose. The fallback is entered on one
condition alone, that the record reports the self-managed path probed and
unavailable.

**The self-managed path is primary because the control plane has no on-demand
backup operation at all.** It offers backup listing and backup configuration, so
the fallback can name a backup that already exists and can never create one. That
is exactly the distinction the two flags carry: `taken` says this run issued the
backup, `referenced` says this run pointed at somebody else's, and the two are
alternatives rather than degrees of the same thing.

**A failing primary path does not fall through to the fallback.** The fallback
answers a capability question, not a run-time fault: a cluster that admits
self-managed backups and then refused this one has left the run without the
evidence it asked for, and naming a scheduled backup taken hours earlier would
present weaker evidence under the same certificate. So a refusal on the chosen
path is recorded failed with the detail, and the engine treats that as fatal
before the first mutation.

**Every effect this module has passes through an injected seam.** The statement is
issued through a caller-supplied issuer, the control-plane command through a
caller-supplied runner, and the instant through a caller-supplied clock. That is
what lets the recording discipline be driven exhaustively in process, and it is
why the subprocess form is a small function at the edge rather than a call buried
in the decision.

The control-plane command is always invoked as an argument vector and never as a
shell string, and the vector is recorded verbatim in machine-readable form so the
claim is reproducible. Nothing here interpolates a caller value into statement
text: the backup target is a bound parameter, because a target may carry
credentials in its query parameters, and a refusal is reported by the fault's name
rather than by the message the cluster or the command composed around it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from molt.config.resolve import Configuration
from molt.store import Cursor, MemoryStore
from molt.store.capability import SELF_MANAGED_BACKUP, CapabilityRecord
from molt.telemetry import Severity, log

__all__ = [
    "BACKUP_RECORD_STATEMENT",
    "CCLOUD_BINARY_KEY",
    "CCLOUD_CLUSTER_KEY",
    "COMPONENT",
    "MANAGED_BACKUP_ARGUMENTS",
    "MANAGED_BACKUP_SUBCOMMAND",
    "SELF_MANAGED_BACKUP_STATEMENT",
    "TARGET_KEY",
    "TIMEOUT_KEY",
    "BackupPath",
    "BackupRecord",
    "BackupSettings",
    "BackupStatus",
    "Clock",
    "CommandResult",
    "CommandRunner",
    "ManagedBackup",
    "StatementIssuer",
    "managed_backup_vector",
    "record_backup",
    "reference_managed",
    "render_command_vector",
    "run_command",
    "self_managed",
    "store_issuer",
    "system_clock",
    "take_backup",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "backup"

# The configuration surface keys this module reads. The target carries no default,
# so a deployment that names none is refused at resolution rather than backed up
# somewhere no operator chose.
TARGET_KEY: Final[str] = "MOLT_BACKUP_TARGET"
CCLOUD_BINARY_KEY: Final[str] = "MOLT_CCLOUD_BIN"
CCLOUD_CLUSTER_KEY: Final[str] = "MOLT_CCLOUD_CLUSTER_ID"
TIMEOUT_KEY: Final[str] = "MOLT_BACKUP_TIMEOUT_SECONDS"

# The primary path's whole statement. The target is bound rather than written into
# statement text, because a backup target may carry credentials in its query
# parameters and statement text is recorded as evidence.
SELF_MANAGED_BACKUP_STATEMENT: Final[str] = "BACKUP INTO %s"

# The one write this module performs. Every column of the row is a bound
# parameter.
BACKUP_RECORD_STATEMENT: Final[str] = (
    "INSERT INTO backup_record "
    "(run_id, backup_id, backup_path, target_uri, taken_at, command, taken, referenced, "
    "status, detail) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

# The fallback's command, in the two halves the cluster identifier sits between.
# The control plane lists backups and configures them and creates none, which is
# why this reads rather than writes.
MANAGED_BACKUP_SUBCOMMAND: Final[tuple[str, ...]] = ("cluster", "backup", "list")
MANAGED_BACKUP_ARGUMENTS: Final[tuple[str, ...]] = ("--output", "json")

# Where a listing may hold its entries, and where an entry may hold its identifier
# and its instant. Several spellings are read because the recorded evidence is the
# command vector and the values it returned, and a listing that names its entries
# by another of these keys is the same answer under another name.
_LISTING_KEYS: Final[tuple[str, ...]] = ("backups", "results", "data", "items")
_IDENTIFIER_KEYS: Final[tuple[str, ...]] = ("id", "backup_id", "name")
_INSTANT_KEYS: Final[tuple[str, ...]] = ("as_of", "created_at", "completed_at", "taken_at")

# The exit status a control-plane command reports when it answered.
_COMMAND_SUCCEEDED: Final[int] = 0

# What a row records where no path ran at all, which the column holds rather than
# nothing, because the column is not nullable and an absent command is a fact.
NO_COMMAND: Final[str] = ""


class BackupPath(StrEnum):
    """Which of the two paths a backup record answers for."""

    SELF_MANAGED = "self_managed"
    MANAGED_REFERENCED = "managed_referenced"


class BackupStatus(StrEnum):
    """What became of the backup this run asked for."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


# The time source the recorded instant is read from, injected so a caller can
# drive it and no recorded instant is a reading of whichever machine ran.
Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """The delivered clock, reading the host in the one timezone this design uses."""
    return datetime.now(tz=UTC)


class StatementIssuer(Protocol):
    """The seam the primary path's statement reaches the cluster through.

    Both values are positional, because the seam is always invoked positionally and
    a protocol naming them would oblige every substitute -- including the stubs a
    test supplies -- to repeat those names rather than describe its own role.
    """

    def __call__(self, statement: str, parameters: tuple[object, ...], /) -> None:
        """Issue one statement with its bound parameters, raising on refusal."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one control-plane invocation returned."""

    exit_status: int
    stdout: str
    stderr: str = ""


class CommandRunner(Protocol):
    """The seam the fallback path's command is invoked through."""

    def __call__(self, vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
        """Invoke one argument vector, never a shell string, and return what it said."""


@dataclass(frozen=True, slots=True)
class ManagedBackup:
    """One backup the control plane reported, as an identifier and an instant."""

    backup_id: str
    taken_at: datetime


@dataclass(frozen=True, slots=True)
class BackupSettings:
    """What the deployment configured about backups, resolved once.

    The target is the operator-owned bucket the primary path writes into, read from
    a key of its own that carries no default. A default would write a cluster
    backup somewhere no operator chose, so a deployment that names no target is
    refused by the resolution rather than sent somewhere plausible.
    """

    target: str
    ccloud_binary: str
    cluster_id: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        """Refuse settings that name no target, no command, and no cluster."""
        if not self.target:
            raise ValueError("a self-managed backup names the target it writes into")
        if not self.ccloud_binary:
            raise ValueError("the control-plane fallback names the command it invokes")
        if not self.cluster_id:
            raise ValueError("the control-plane fallback names the cluster it asks about")
        if self.timeout_seconds <= 0:
            raise ValueError("a backup is allowed a positive amount of time to complete")

    @classmethod
    def from_configuration(
        cls,
        configuration: Configuration,
        *,
        target: str | None = None,
    ) -> BackupSettings:
        """Read the target, the control-plane settings, and the timeout from the surface.

        Args:
            configuration: The surface every value is read from.
            target: A target naming itself rather than being resolved, which an
                operator-supplied override on one run uses. The configured target
                is read where none is named.
        """
        return cls(
            target=target if target is not None else configuration.text(TARGET_KEY),
            ccloud_binary=configuration.text(CCLOUD_BINARY_KEY),
            cluster_id=configuration.text(CCLOUD_CLUSTER_KEY),
            timeout_seconds=configuration.integer(TIMEOUT_KEY),
        )


def render_command_vector(vector: Sequence[str]) -> str:
    """Render an argument vector as the exact machine-readable list it was.

    A rendered shell string would be a different thing from the vector that was
    invoked and would read as though a shell had been involved, so the recorded
    form keeps the arguments separate and reversible.
    """
    return json.dumps(list(vector))


def managed_backup_vector(settings: BackupSettings) -> tuple[str, ...]:
    """The argument vector the fallback path invokes, built from the settings alone."""
    return (
        settings.ccloud_binary,
        *MANAGED_BACKUP_SUBCOMMAND,
        settings.cluster_id,
        *MANAGED_BACKUP_ARGUMENTS,
    )


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """The evidence one run's backup left, in the shape the stored row holds.

    Every invariant the table's checks hold is checked here too, so a record that
    could not be stored cannot be built and handed to a certificate: the two flags
    are alternatives, each agrees with the path it belongs to, and a status of
    succeeded carries exactly one of them. A row where neither flag holds is free
    to name the path that was attempted and failed, which is what makes a failure
    say what it tried.

    Attributes:
        run_id: The run this backup was secured for.
        status: Whether a path succeeded, no path did, or the operator skipped.
        backup_path: Which path this row answers for, or None where none ran.
        backup_id: The control plane's identifier for a referenced backup.
        target_uri: The target the primary path wrote into.
        taken_at: The instant the backup was taken, read from the clock on the
            primary path and from the control plane's answer on the fallback.
        command: The statement issued, or the argument vector invoked, verbatim.
        command_vector: The argument vector itself, where one was invoked.
        taken: This run created the backup. Only the primary path sets it.
        referenced: This run named a backup that already existed.
        detail: Why no path succeeded, naming the fault rather than quoting it.
    """

    run_id: UUID
    status: BackupStatus
    command: str
    backup_path: BackupPath | None = None
    backup_id: str | None = None
    target_uri: str | None = None
    taken_at: datetime | None = None
    command_vector: tuple[str, ...] = field(default_factory=tuple)
    taken: bool = False
    referenced: bool = False
    detail: str | None = None

    def __post_init__(self) -> None:
        """Refuse a record whose flags, path, and status could tell two stories."""
        if self.taken and self.referenced:
            raise ValueError("a backup is one this run took or one it referenced, never both")
        if self.taken and self.backup_path is not BackupPath.SELF_MANAGED:
            raise ValueError("a taken backup is the self-managed path and no other")
        if self.referenced and self.backup_path is not BackupPath.MANAGED_REFERENCED:
            raise ValueError("a referenced backup is the managed-reference path and no other")
        if self.status is BackupStatus.SUCCEEDED and not (self.taken or self.referenced):
            raise ValueError("a succeeded backup was either taken or referenced")
        if self.status is not BackupStatus.SUCCEEDED and (self.taken or self.referenced):
            raise ValueError("only a succeeded backup carries a taken or referenced flag")
        if self.command_vector and self.command != render_command_vector(self.command_vector):
            raise ValueError("the recorded command disagrees with the vector that was invoked")

    @property
    def fatal(self) -> bool:
        """Whether the engine must abort this run before its first mutation."""
        return self.status is BackupStatus.FAILED

    @property
    def evidence(self) -> BackupPath | None:
        """The path this row's flags actually name, which a certificate states.

        Derived from the flags rather than from the recorded path value, so a
        certificate's backup evidence and the flags cannot come apart: a row that
        took nothing and referenced nothing names no path here however the
        attempted path was recorded.
        """
        if self.taken:
            return BackupPath.SELF_MANAGED
        if self.referenced:
            return BackupPath.MANAGED_REFERENCED
        return None

    def parameters(self) -> tuple[object, ...]:
        """This row's columns, in the order the recording statement binds them."""
        return (
            self.run_id,
            self.backup_id,
            None if self.backup_path is None else self.backup_path.value,
            self.target_uri,
            self.taken_at,
            self.command,
            self.taken,
            self.referenced,
            self.status.value,
            self.detail,
        )


# ---------------------------------------------------------------------------
# The delivered seams
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoreIssuer:
    """The delivered issuer: one statement on one leased connection, no transaction.

    A backup statement frames no transaction of its own here on purpose. It is a
    long job statement rather than a row write, and the retry wrapper reruns a
    body from the beginning on a conflict, which for this statement would mean
    issuing a second backup.
    """

    store: MemoryStore

    def __call__(self, statement: str, parameters: tuple[object, ...]) -> None:
        """Issue one statement with its bound parameters."""

        def body(cursor: Cursor) -> None:
            cursor.execute(statement, parameters)

        self.store.read(body)


def store_issuer(store: MemoryStore) -> StatementIssuer:
    """The issuer a deployment uses, bound to one store."""
    return StoreIssuer(store)


def run_command(vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
    """Invoke one argument vector as a subprocess, with no shell anywhere.

    The vector is passed as a list, so no argument is parsed by a shell and no
    value in it can be read as shell syntax. A non-zero status is returned rather
    than raised, because the caller records the status as evidence.
    """
    completed = subprocess.run(  # noqa: S603 - an argument vector, never a shell string
        list(vector),
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


# ---------------------------------------------------------------------------
# The two paths
# ---------------------------------------------------------------------------


def self_managed(
    run_id: UUID,
    *,
    settings: BackupSettings,
    issuer: StatementIssuer,
    clock: Clock = system_clock,
) -> BackupRecord:
    """Issue a backup into the operator-owned target and record what was issued.

    Recorded on success: the target, the statement as it was issued, the instant,
    the self-managed path value, taken true and referenced false.

    A refusal is recorded as a failure naming the fault's type and nothing else.
    The message a cluster composes around a refused backup names the target, and a
    target may carry credentials in its query parameters, so no such message
    reaches the detail column or a log record.
    """
    try:
        issuer(SELF_MANAGED_BACKUP_STATEMENT, (settings.target,))
    except Exception as error:
        log(
            Severity.ERROR,
            COMPONENT,
            "the self-managed backup was refused, so the run has no backup evidence",
            run_id=str(run_id),
            error_type=type(error).__name__,
        )
        return BackupRecord(
            run_id=run_id,
            status=BackupStatus.FAILED,
            command=SELF_MANAGED_BACKUP_STATEMENT,
            backup_path=BackupPath.SELF_MANAGED,
            detail=f"the self-managed backup statement was refused: {type(error).__name__}",
        )
    taken_at = clock()
    log(
        Severity.INFO,
        COMPONENT,
        "a self-managed backup was issued before the first mutation of the run",
        run_id=str(run_id),
    )
    return BackupRecord(
        run_id=run_id,
        status=BackupStatus.SUCCEEDED,
        command=SELF_MANAGED_BACKUP_STATEMENT,
        backup_path=BackupPath.SELF_MANAGED,
        target_uri=settings.target,
        taken_at=taken_at,
        taken=True,
    )


def reference_managed(
    run_id: UUID,
    *,
    settings: BackupSettings,
    runner: CommandRunner,
) -> BackupRecord:
    """Name the most recent backup the control plane holds, through its command.

    Recorded on success: the identifier, that backup's own instant, the exact
    argument vector invoked, the managed-referenced path value, taken false and
    referenced true. A referenced backup is evidence that a backup exists and not
    evidence that this run made one, which is the whole reason the two flags are
    kept apart.
    """
    vector = managed_backup_vector(settings)
    rendered = render_command_vector(vector)
    try:
        result = runner(vector, timeout_seconds=settings.timeout_seconds)
        if result.exit_status != _COMMAND_SUCCEEDED:
            raise ValueError(f"the command exited with status {result.exit_status}")
        newest = _most_recent(result.stdout)
    except Exception as error:
        log(
            Severity.ERROR,
            COMPONENT,
            "no managed backup could be named, so the run has no backup evidence",
            run_id=str(run_id),
            error_type=type(error).__name__,
        )
        return BackupRecord(
            run_id=run_id,
            status=BackupStatus.FAILED,
            command=rendered,
            command_vector=vector,
            backup_path=BackupPath.MANAGED_REFERENCED,
            detail=f"the managed backup listing was not answered: {type(error).__name__}",
        )
    log(
        Severity.WARNING,
        COMPONENT,
        "the self-managed path is unavailable, so an existing managed backup is referenced",
        run_id=str(run_id),
    )
    return BackupRecord(
        run_id=run_id,
        status=BackupStatus.SUCCEEDED,
        command=rendered,
        command_vector=vector,
        backup_path=BackupPath.MANAGED_REFERENCED,
        backup_id=newest.backup_id,
        taken_at=newest.taken_at,
        referenced=True,
    )


def take_backup(
    run_id: UUID,
    *,
    capabilities: CapabilityRecord,
    settings: BackupSettings,
    issuer: StatementIssuer,
    runner: CommandRunner,
    clock: Clock = system_clock,
    skip: bool = False,
) -> BackupRecord:
    """Secure backup evidence for one run before its first mutation.

    The skip flag is answered first, because an operator who passed it has asked
    for no backup at all and neither path should be reached. Otherwise the path is
    the one the capability record implies: the fallback exactly where the record
    reports the self-managed path probed and unavailable, and the primary path in
    every other case, including where nobody probed.

    Returns:
        The record of what happened, whose `fatal` property is what the engine
        aborts on. The returned record is not stored; `record_backup` writes it,
        so the caller frames that write with the rest of its evidence.
    """
    if skip:
        log(
            Severity.WARNING,
            COMPONENT,
            "the operator passed the skip flag, so the run proceeds with no backup",
            run_id=str(run_id),
        )
        return BackupRecord(
            run_id=run_id,
            status=BackupStatus.SKIPPED,
            command=NO_COMMAND,
            detail="the operator passed the skip-backup flag",
        )
    if capabilities.unavailable(SELF_MANAGED_BACKUP):
        return reference_managed(run_id, settings=settings, runner=runner)
    return self_managed(run_id, settings=settings, issuer=issuer, clock=clock)


# ---------------------------------------------------------------------------
# Recording the row
# ---------------------------------------------------------------------------


def record_backup(store: MemoryStore, record: BackupRecord) -> BackupRecord:
    """Write one backup record and return it unchanged.

    The write is its own short transaction, outside the statement and the
    subprocess above, so no transaction is held open across either.
    """

    def body(cursor: Cursor) -> None:
        cursor.execute(BACKUP_RECORD_STATEMENT, record.parameters())

    store.in_serializable(body, label="backup_record")
    return record


# ---------------------------------------------------------------------------
# Reading the control plane's answer
# ---------------------------------------------------------------------------


def _most_recent(stdout: str) -> ManagedBackup:
    """The newest backup a listing reports, refusing a listing that names none."""
    entries = _entries_of(stdout)
    reported = [found for found in (_managed_backup_of(entry) for entry in entries) if found]
    if not reported:
        raise ValueError("the listing named no backup carrying both an identifier and an instant")
    return max(reported, key=lambda found: found.taken_at)


def _entries_of(stdout: str) -> list[object]:
    """The entries of a listing, whether it arrived as a list or wrapped in an object."""
    payload = json.loads(stdout)
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict):
        for key in _LISTING_KEYS:
            held = payload.get(key)
            if isinstance(held, list):
                return list(held)
    raise ValueError("the listing was not shaped as a sequence of backups")


def _managed_backup_of(entry: object) -> ManagedBackup | None:
    """One entry as an identifier and an instant, or None when it carries neither."""
    if not isinstance(entry, dict):
        return None
    identifier = _first_text(entry, _IDENTIFIER_KEYS)
    reported = _first_text(entry, _INSTANT_KEYS)
    if identifier is None or reported is None:
        return None
    instant = _instant_of(reported)
    return None if instant is None else ManagedBackup(identifier, instant)


def _first_text(entry: dict[str, object], keys: Sequence[str]) -> str | None:
    """The first of several keys the entry holds text under."""
    for key in keys:
        held = entry.get(key)
        if isinstance(held, str) and held:
            return held
    return None


def _instant_of(reported: str) -> datetime | None:
    """One reported instant, read in the timezone this design keeps every instant in."""
    try:
        parsed = datetime.fromisoformat(reported)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
