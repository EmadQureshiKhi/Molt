"""The schema migration runner: discovery, digest history, and application.

Five rules shape this module, and each of them is load-bearing rather than
stylistic.

**A migration applies inside one transaction, and that same transaction writes
its history row.** Either the objects a file creates and the row recording
that file exist together, or neither exists. There is no window in which the
schema has moved and the history has not.

**An applied migration is never edited.** The runner stores the digest of each
file it applies and compares the stored digest against the file on disk before
it applies anything. A mismatch is refused with both digests named, because a
changed file means either the schema on the cluster no longer matches the source
that produced it or the source no longer describes the schema, and silently
re-applying is the one response that hides which. New behaviour arrives as a new
numbered file, never as an edit to an old one.

**A second run changes no state.** A recorded version is skipped, and every
statement a file holds is written to be re-runnable anyway: object creation is
guarded, inserts resolve conflicts, and privilege statements are re-issuable. The
two mechanisms are deliberately redundant.

**A statement permitted to fail is wrapped in a savepoint and its outcome is
reported.** Some capabilities are present on one cluster tier and rejected on
another. Such a statement carries a marker comment, is applied inside a savepoint
of its own, and its success or failure becomes a reported outcome the caller
records, rather than an exception that ends the run.

**A statement the platform serves only outside a transaction is applied in an
implicit transaction of its own, and is still required to succeed.** A few schema
objects — a trigger among them — are created only by the newer schema changer,
and that changer is unavailable inside a multi-statement transaction under any
setting. Such a statement carries a marker of its own, is applied after the
migration's body has committed, and a failure ends the run. Because that leaves
the body committed and the marked statements not yet applied, the history row for
a migration holding one is written only once every marked statement has
succeeded: a recorded version therefore still means a fully applied file, and an
interrupted one is re-applied whole on the next run, which every statement being
re-runnable is what makes safe.

**Two session settings are established before anything is applied**, because the
platform's defaults are incompatible with the first two rules. Statement-level
autocommit ahead of schema changes would split one migration across several
transactions, and schema locking on newly created tables would refuse the later
alterations that additive migrations are built from. Both are turned off for the
runner's own session only.

The driver is reached through the two narrow structural protocols declared here
rather than by importing it, so this module imports and type-checks with no
driver installed and the operational entry point loads it lazily.

A note on where a marked statement sits in the order: the statements a migration
is made of are applied in file order in the migration's transaction, then the
statements marked as needing a transaction of their own, then the statements
marked as permitted to fail. The platform refuses to unwind a savepoint in a
transaction that has already changed the schema, so a permitted statement cannot
share the transaction it follows. A statement in the migration body therefore
must not depend on either kind of marked statement.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from molt.config.resolve import load_configuration
from molt.config.secrets import resolve_dsn
from molt.store.retry import DEFAULT_JITTER, DEFAULT_SLEEP, RetryPolicy, is_serialization_failure

__all__ = [
    "HISTORY_TABLE",
    "MIGRATIONS_DIRECTORY",
    "OWN_TRANSACTION_MARKER",
    "PERMIT_MARKER",
    "SESSION_SETTINGS",
    "AppliedMigration",
    "Connection",
    "Cursor",
    "MigrationApplyError",
    "MigrationError",
    "MigrationFile",
    "MigrationHistoryError",
    "MigrationOutcome",
    "MigrationReport",
    "MigrationSourceError",
    "PermittedOutcome",
    "PermittedResult",
    "Statement",
    "apply_migrations",
    "connect",
    "discover_migrations",
    "file_digest",
    "load_migration",
    "main",
    "parse_statements",
    "permitted_to_fail",
    "prepare_session",
    "recorded_migrations",
    "render_report",
    "verify_history",
]

# Where the numbered files live. Held as a path rather than assembled from text
# so that no caller joins path segments by concatenating strings.
MIGRATIONS_DIRECTORY: Final[Path] = Path(__file__).resolve().parent / "migrations"

# The table the runner reads and writes its own history in. The first migration
# creates it; the runner never creates it, so the schema has exactly one source.
HISTORY_TABLE: Final[str] = "schema_migration"

# The marker that makes the statement following it permitted to fail. The word
# after the marker is the label the outcome is reported under, so a caller can
# recognise one particular capability probe among several.
PERMIT_MARKER: Final[str] = "molt:permit-failure"

# The marker that makes the statement following it apply in an implicit
# transaction of its own, after the migration's body has committed. It is for the
# statements the platform serves only outside a transaction, and unlike a
# permitted statement it is still required to succeed.
OWN_TRANSACTION_MARKER: Final[str] = "molt:own-transaction"

# The session settings the runner establishes before applying anything.
#
# Autocommit ahead of a schema change would commit the statements preceding each
# schema change, which would split one migration across several transactions and
# leave a partially applied file behind on a failure.
#
# Schema locking on a newly created table improves change-stream performance but
# refuses a later alteration of that table, and additive migrations are built
# entirely from later alterations. Locking is therefore a deployment step rather
# than a table default here.
SESSION_SETTINGS: Final[tuple[str, ...]] = (
    "SET autocommit_before_ddl = false",
    "SET create_table_with_schema_locked = false",
)

# The label reported for a permitted statement whose marker names none.
DEFAULT_PERMITTED_LABEL: Final[str] = "permitted"

# The savepoint every permitted statement is wrapped in. One fixed name is
# enough because a permitted statement occupies a transaction of its own, and a
# fixed name means no identifier is ever interpolated into a statement.
SAVEPOINT_NAME: Final[str] = "molt_permitted"
_SAVEPOINT_OPEN: Final[str] = "SAVEPOINT " + SAVEPOINT_NAME
_SAVEPOINT_UNWIND: Final[str] = "ROLLBACK TO SAVEPOINT " + SAVEPOINT_NAME
_SAVEPOINT_RELEASE: Final[str] = "RELEASE SAVEPOINT " + SAVEPOINT_NAME

# The three statements the runner issues against its own history table. Each is
# a whole literal with bound parameters, so no value and no identifier is ever
# interpolated into statement text.
HISTORY_PRESENCE_QUERY: Final[str] = (
    "SELECT count(*) FROM information_schema.tables "
    "WHERE table_schema = current_schema() AND table_name = %s"
)
HISTORY_QUERY: Final[str] = (
    "SELECT version, name, file_digest FROM schema_migration ORDER BY version ASC"
)
RECORD_STATEMENT: Final[str] = (
    "INSERT INTO schema_migration (version, name, file_digest) VALUES (%s, %s, %s)"
)

# A file name is a three-digit version, an underscore, and a lowercase name.
_FILE_NAME: Final[re.Pattern[str]] = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")

# The marker as it appears inside a comment, with its optional label.
_MARKER: Final[re.Pattern[str]] = re.compile(
    re.escape(PERMIT_MARKER) + r"(?:\s+(?P<label>[a-z0-9_]+))?"
)

# The own-transaction marker as it appears inside a comment. It carries no label,
# because its outcome is not reported: the statement either succeeds or ends the
# run.
_OWN_TRANSACTION: Final[re.Pattern[str]] = re.compile(re.escape(OWN_TRANSACTION_MARKER))

# The digest algorithm the history records. Named once so the recorded values and
# any later verification cannot disagree about which digest was taken.
DIGEST_ALGORITHM: Final[str] = "sha256"

_READ_CHUNK_BYTES: Final[int] = 65536

# How a migration whose transaction the cluster aborted is re-attempted. A schema
# change reads and writes descriptors that any concurrent schema change in the
# same database also touches, so the cluster may abort one of the two and require
# the client to present it again. That signal is a instruction to retry rather
# than a defect in the file: the same statements applied again succeed once the
# competing change has committed. Without this, a migration run against a busy
# cluster fails for a reason that has nothing to do with its own correctness.
#
# The schedule is the store's own, so a migration backs off exactly as every other
# write transaction does rather than on a second timetable.
MIGRATION_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()


# ---------------------------------------------------------------------------
# The driver, reached structurally
# ---------------------------------------------------------------------------


class Cursor(Protocol):
    """The three calls the runner makes on a cursor."""

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Send one statement, binding the parameters server-side."""

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every row the last statement produced."""

    def close(self) -> None:
        """Release the cursor."""


class Connection(Protocol):
    """The transaction control the runner needs from a connection.

    `autocommit` is part of the shape because the runner turns it off for the
    duration of a run and restores it afterwards: a migration that committed
    statement by statement would not be atomic, and a caller holding a connection
    for other work should not have that connection's mode changed permanently.
    """

    autocommit: bool

    def cursor(self) -> Cursor:
        """Open a cursor on this connection."""

    def commit(self) -> None:
        """Commit the open transaction."""

    def rollback(self) -> None:
        """Discard the open transaction."""

    def close(self) -> None:
        """Close the connection."""


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


class MigrationError(Exception):
    """A migration could not be discovered, verified, or applied."""


class MigrationSourceError(MigrationError):
    """The migration files on disk are not a well-formed set."""


class MigrationHistoryError(MigrationError):
    """The recorded history and the files on disk disagree.

    This is refused rather than repaired. A recorded digest that no longer
    matches its file means the applied schema and the source that produced it
    have diverged, and re-applying the changed file would hide which of the two
    is wrong.
    """


class MigrationApplyError(MigrationError):
    """A statement of a migration that was required to succeed did not."""

    def __init__(self, version: int, name: str, line: int, detail: str) -> None:
        self.version = version
        self.name = name
        self.line = line
        self.detail = detail
        where = f"line {line}" if line else "the history record"
        super().__init__(
            f"migration {version:03d} ({name}) was rolled back: {where} failed: {detail}"
        )


# ---------------------------------------------------------------------------
# Migration sources
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Statement:
    """One statement of a migration file, with its comments already removed.

    Attributes:
        text: The statement as it will be sent, comments stripped.
        line: The one-based line of the file the statement's text begins on.
        permitted_label: The label a permitted statement is reported under, or
            None when the statement is required to succeed.
        own_transaction: Whether this statement is applied in an implicit
            transaction of its own after the migration's body has committed.
    """

    text: str
    line: int
    permitted_label: str | None
    own_transaction: bool = False

    @property
    def permitted(self) -> bool:
        """Whether this statement is permitted to fail."""
        return self.permitted_label is not None


@dataclass(frozen=True, slots=True)
class MigrationFile:
    """One numbered migration file, parsed and digested."""

    version: int
    name: str
    path: Path
    digest: str
    statements: tuple[Statement, ...]

    @property
    def body(self) -> tuple[Statement, ...]:
        """The statements applied in the migration's own transaction."""
        return tuple(
            statement
            for statement in self.statements
            if not statement.permitted and not statement.own_transaction
        )

    @property
    def own_transaction(self) -> tuple[Statement, ...]:
        """The statements applied afterwards, each in an implicit transaction."""
        return tuple(statement for statement in self.statements if statement.own_transaction)

    @property
    def permitted(self) -> tuple[Statement, ...]:
        """The statements applied afterwards, each in a transaction of its own."""
        return tuple(statement for statement in self.statements if statement.permitted)


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """One row of the recorded history."""

    version: int
    name: str
    file_digest: str


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PermittedResult:
    """What a savepointed block did, filled in by the wrapper as it exits."""

    succeeded: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PermittedOutcome:
    """The reported result of one statement permitted to fail."""

    label: str
    line: int
    succeeded: bool
    detail: str


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """What one run did with one migration."""

    version: int
    name: str
    applied: bool
    permitted: tuple[PermittedOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """What one run did with every migration it found."""

    outcomes: tuple[MigrationOutcome, ...]

    @property
    def applied_versions(self) -> tuple[int, ...]:
        """The versions this run applied, in ascending order."""
        return tuple(item.version for item in self.outcomes if item.applied)

    @property
    def skipped_versions(self) -> tuple[int, ...]:
        """The versions this run found already recorded, in ascending order."""
        return tuple(item.version for item in self.outcomes if not item.applied)

    @property
    def changed_state(self) -> bool:
        """Whether this run applied anything at all."""
        return bool(self.applied_versions)

    @property
    def permitted_outcomes(self) -> tuple[PermittedOutcome, ...]:
        """Every permitted-statement outcome this run produced, in run order."""
        return tuple(outcome for item in self.outcomes for outcome in item.permitted)

    def permitted_outcome(self, label: str) -> PermittedOutcome | None:
        """The outcome reported under one label, or None when this run produced none."""
        for outcome in self.permitted_outcomes:
            if outcome.label == label:
                return outcome
        return None


# ---------------------------------------------------------------------------
# Digests and parsing
# ---------------------------------------------------------------------------


def file_digest(path: Path) -> str:
    """Return the hexadecimal digest of a file's bytes.

    The whole file is digested, comments included, so an edit to a comment is as
    much a change of history as an edit to a statement. That is deliberate: a
    comment is how a migration explains itself, and a silently rewritten
    explanation is worth reporting.
    """
    digest = hashlib.new(DIGEST_ALGORITHM)
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise MigrationSourceError(f"the migration file {path} could not be read") from exc
    return digest.hexdigest()


def parse_statements(sql: str) -> tuple[Statement, ...]:
    """Split migration text into statements, keeping the marker of each.

    The split is aware of single-quoted text, line comments, and block comments,
    so a semicolon inside a quoted value or a comment ends no statement. Comments
    are removed from the statement text and read for the permitted marker, which
    is what lets a file explain itself without the explanation being sent.
    """
    statements: list[Statement] = []
    body: list[str] = []
    comments: list[str] = []
    comment: list[str] = []
    line = 1
    begin = 0
    index = 0
    length = len(sql)
    in_text = False
    in_line_comment = False
    in_block_comment = False

    while index < length:
        char = sql[index]
        following = sql[index + 1] if index + 1 < length else ""

        if in_line_comment:
            if char == "\n":
                comments.append("".join(comment))
                comment = []
                in_line_comment = False
                line += 1
            else:
                comment.append(char)
            index += 1
            continue

        if in_block_comment:
            if char == "*" and following == "/":
                comments.append("".join(comment))
                comment = []
                in_block_comment = False
                index += 2
                continue
            if char == "\n":
                line += 1
            comment.append(char)
            index += 1
            continue

        if in_text:
            body.append(char)
            if char == "'":
                if following == "'":
                    body.append(following)
                    index += 2
                    continue
                in_text = False
            elif char == "\n":
                line += 1
            index += 1
            continue

        if char == "-" and following == "-":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and following == "*":
            in_block_comment = True
            index += 2
            continue
        if char == "'":
            in_text = True
            begin = begin or line
            body.append(char)
            index += 1
            continue
        if char == ";":
            parsed = _statement(body, comments, begin or line)
            if parsed is not None:
                statements.append(parsed)
            body = []
            comments = []
            begin = 0
            index += 1
            continue

        if char == "\n":
            line += 1
        elif not char.isspace():
            begin = begin or line
        body.append(char)
        index += 1

    if in_text:
        raise MigrationSourceError("a migration holds an unterminated quoted value")
    if in_block_comment:
        raise MigrationSourceError("a migration holds an unterminated block comment")
    if in_line_comment:
        comments.append("".join(comment))
    trailing = _statement(body, comments, begin or line)
    if trailing is not None:
        raise MigrationSourceError(
            f"the statement beginning at line {trailing.line} is not terminated by a semicolon"
        )
    return tuple(statements)


def _statement(body: list[str], comments: list[str], line: int) -> Statement | None:
    """Build one statement from accumulated text, or None when there was none.

    A statement carrying both markers is refused: one asks for the outcome to be
    reported instead of raised and the other asks for the statement to be applied
    somewhere the savepoint the first depends on cannot be taken, so the pair has
    no coherent meaning.
    """
    text = "".join(body).strip()
    if not text:
        return None
    label = _permitted_label(comments)
    own = any(_OWN_TRANSACTION.search(entry) for entry in comments)
    if label is not None and own:
        raise MigrationSourceError(
            "one statement is marked both permitted to fail and as needing a transaction of its own"
        )
    return Statement(text=text, line=line, permitted_label=label, own_transaction=own)


def _permitted_label(comments: list[str]) -> str | None:
    """Read the permitted marker out of a statement's comments, refusing two."""
    found: list[str] = []
    for entry in comments:
        for match in _MARKER.finditer(entry):
            found.append(match.group("label") or DEFAULT_PERMITTED_LABEL)
    if len(found) > 1:
        raise MigrationSourceError(
            "one statement carries more than one permitted-failure marker: " + ", ".join(found)
        )
    return found[0] if found else None


def load_migration(path: Path) -> MigrationFile:
    """Parse and digest one numbered migration file."""
    match = _FILE_NAME.match(path.name)
    if match is None:
        raise MigrationSourceError(
            f"the migration file name {path.name!r} is not a three-digit version, an "
            "underscore, a lowercase name, and the sql suffix"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MigrationSourceError(f"the migration file {path} could not be read") from exc
    except UnicodeDecodeError as exc:
        raise MigrationSourceError(f"the migration file {path} is not valid text") from exc
    return MigrationFile(
        version=int(match.group("version"), 10),
        name=path.stem,
        path=path,
        digest=file_digest(path),
        statements=parse_statements(text),
    )


def discover_migrations(directory: Path | None = None) -> tuple[MigrationFile, ...]:
    """Return every migration file in ascending version order.

    A duplicated version is refused rather than resolved, because two files
    claiming one version leave the applied order undefined.
    """
    root = MIGRATIONS_DIRECTORY if directory is None else directory
    if not root.is_dir():
        raise MigrationSourceError(f"the migration directory {root} does not exist")
    found: dict[int, MigrationFile] = {}
    for path in sorted(root.iterdir()):
        if path.suffix != ".sql" or not path.is_file():
            continue
        migration = load_migration(path)
        clash = found.get(migration.version)
        if clash is not None:
            raise MigrationSourceError(
                f"version {migration.version:03d} is claimed by both {clash.path.name} "
                f"and {path.name}"
            )
        found[migration.version] = migration
    return tuple(found[version] for version in sorted(found))


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _as_int(value: object, column: str) -> int:
    """Read a whole number out of a returned row, refusing anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MigrationHistoryError(f"the history column {column} did not return a whole number")
    return value


def _as_text(value: object, column: str) -> str:
    """Read text out of a returned row, refusing anything else."""
    if not isinstance(value, str):
        raise MigrationHistoryError(f"the history column {column} did not return text")
    return value


def prepare_session(cursor: Cursor) -> None:
    """Establish the session settings a correct run depends on."""
    for statement in SESSION_SETTINGS:
        cursor.execute(statement)


def recorded_migrations(cursor: Cursor) -> dict[int, AppliedMigration]:
    """Read the recorded history, returning an empty history before it exists.

    The first migration creates the history table rather than the
    runner, so that the schema has one source. Before that migration has ever
    run the table is simply absent, which is an empty history rather than a
    fault, and the presence check is what distinguishes the two.
    """
    cursor.execute(HISTORY_PRESENCE_QUERY, (HISTORY_TABLE,))
    present = cursor.fetchall()
    if not present or _as_int(present[0][0], "table_name") == 0:
        return {}
    cursor.execute(HISTORY_QUERY)
    history: dict[int, AppliedMigration] = {}
    for row in cursor.fetchall():
        version = _as_int(row[0], "version")
        history[version] = AppliedMigration(
            version=version,
            name=_as_text(row[1], "name"),
            file_digest=_as_text(row[2], "file_digest"),
        )
    return history


def verify_history(
    migrations: Sequence[MigrationFile],
    recorded: Mapping[int, AppliedMigration],
) -> None:
    """Refuse a history that no longer describes the files on disk.

    Two divergences are refused. A recorded digest that differs from its file's
    digest means an applied migration was edited. A recorded version with no file
    at all means an applied migration was removed. Either way the cluster's
    schema and this checkout's source no longer correspond, and applying anything
    further would build on an unknown base.
    """
    by_version = {migration.version: migration for migration in migrations}
    for version in sorted(recorded):
        applied = recorded[version]
        migration = by_version.get(version)
        if migration is None:
            raise MigrationHistoryError(
                f"migration {version:03d} ({applied.name}) is recorded as applied but no "
                "file for it is present; an applied migration was removed"
            )
        if migration.digest != applied.file_digest:
            raise MigrationHistoryError(
                f"migration {version:03d} ({applied.name}) was applied with digest "
                f"{applied.file_digest} but {migration.path.name} now digests to "
                f"{migration.digest}; an applied migration was edited, so it is reported "
                "rather than re-applied"
            )


# ---------------------------------------------------------------------------
# The savepoint wrapper
# ---------------------------------------------------------------------------


@contextmanager
def permitted_to_fail(cursor: Cursor) -> Iterator[PermittedResult]:
    """Run a block inside a savepoint, reporting its outcome instead of raising.

    On success the savepoint is released and the result reports success. On
    failure the savepoint is unwound, the result reports the failure, and the
    surrounding transaction survives, so the run continues and the outcome
    becomes evidence rather than an abort.
    """
    result = PermittedResult()
    cursor.execute(_SAVEPOINT_OPEN)
    try:
        yield result
    except Exception as error:
        result.succeeded = False
        result.detail = _first_line(error)
        cursor.execute(_SAVEPOINT_UNWIND)
    else:
        result.succeeded = True
        cursor.execute(_SAVEPOINT_RELEASE)


def _first_line(error: BaseException) -> str:
    """The first line of a failure's message, or its type when it carries none."""
    lines = str(error).strip().splitlines()
    return lines[0].strip() if lines else type(error).__name__


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def apply_migrations(
    connection: Connection,
    *,
    directory: Path | None = None,
) -> MigrationReport:
    """Apply every unrecorded migration in ascending order and report what ran.

    Nothing is applied until the whole recorded history has been verified against
    the files on disk, so a corrupted history is reported with the schema
    untouched.
    """
    migrations = discover_migrations(directory)
    previous_mode = connection.autocommit
    connection.autocommit = False
    cursor = connection.cursor()
    try:
        prepare_session(cursor)
        recorded = recorded_migrations(cursor)
        verify_history(migrations, recorded)
        connection.commit()
        outcomes: list[MigrationOutcome] = []
        for migration in migrations:
            if migration.version in recorded:
                outcomes.append(
                    MigrationOutcome(
                        version=migration.version,
                        name=migration.name,
                        applied=False,
                    )
                )
                continue
            outcomes.append(_apply_with_retry(connection, cursor, migration))
        return MigrationReport(tuple(outcomes))
    finally:
        # Discarding first is what makes restoring the mode safe: the driver
        # refuses a mode change while a transaction is open, and a failure
        # anywhere above may have left one open.
        connection.rollback()
        cursor.close()
        connection.autocommit = previous_mode


def _apply_with_retry(
    connection: Connection,
    cursor: Cursor,
    migration: MigrationFile,
) -> MigrationOutcome:
    """Apply one migration, presenting it again when the cluster aborts its transaction.

    Only the conflict signal is retried, and only while the schedule permits it.
    Every other failure is raised on its first occurrence, so a genuine defect in a
    file is reported immediately rather than attempted five times.

    Retrying is safe because a rolled-back migration left nothing behind: the whole
    file and its history row share one transaction, so a re-attempt starts from the
    state the first attempt started from.
    """
    policy = MIGRATION_RETRY_POLICY
    for attempt in range(policy.attempts):
        try:
            return _apply_one(connection, cursor, migration)
        except MigrationApplyError as error:
            cause = error.__cause__
            last = attempt == policy.attempts - 1
            if last or cause is None or not is_serialization_failure(cause):
                raise
            DEFAULT_SLEEP(policy.delay(attempt, jitter=DEFAULT_JITTER))
    raise AssertionError("the retry schedule permits at least one attempt")


def _apply_one(
    connection: Connection,
    cursor: Cursor,
    migration: MigrationFile,
) -> MigrationOutcome:
    """Apply one migration atomically, then its marked statements.

    The history row is written inside the migration's transaction when the file
    holds no own-transaction statement, which is the ordinary case and keeps the
    objects and the row recording them inseparable. When the file does hold one,
    the row is written after every such statement has succeeded, so a recorded
    version still means a fully applied file.
    """
    deferred = migration.own_transaction
    line = 0
    try:
        for statement in migration.body:
            line = statement.line
            cursor.execute(statement.text)
        if not deferred:
            line = 0
            cursor.execute(
                RECORD_STATEMENT,
                (migration.version, migration.name, migration.digest),
            )
    except Exception as error:
        connection.rollback()
        raise MigrationApplyError(
            migration.version, migration.name, line, _first_line(error)
        ) from error
    connection.commit()
    if deferred:
        _apply_own_transaction(connection, cursor, migration, deferred)
    return MigrationOutcome(
        version=migration.version,
        name=migration.name,
        applied=True,
        permitted=tuple(
            _apply_permitted(connection, cursor, statement) for statement in migration.permitted
        ),
    )


def _apply_own_transaction(
    connection: Connection,
    cursor: Cursor,
    migration: MigrationFile,
    statements: Sequence[Statement],
) -> None:
    """Apply the marked statements in implicit transactions, then record the file.

    The connection's mode is switched for the duration, so each statement reaches
    the cluster as a transaction of one statement and the schema changer the
    platform reserves for that shape is available. A failure ends the run with the
    history row unwritten, which leaves the next run to apply the whole file
    again.
    """
    previous_mode = connection.autocommit
    connection.autocommit = True
    line = 0
    try:
        for statement in statements:
            line = statement.line
            cursor.execute(statement.text)
        line = 0
        cursor.execute(
            RECORD_STATEMENT,
            (migration.version, migration.name, migration.digest),
        )
    except Exception as error:
        raise MigrationApplyError(
            migration.version, migration.name, line, _first_line(error)
        ) from error
    finally:
        connection.autocommit = previous_mode


def _apply_permitted(
    connection: Connection,
    cursor: Cursor,
    statement: Statement,
) -> PermittedOutcome:
    """Apply one permitted statement in a transaction of its own."""
    label = statement.permitted_label or DEFAULT_PERMITTED_LABEL
    try:
        with permitted_to_fail(cursor) as result:
            cursor.execute(statement.text)
        succeeded = result.succeeded
        detail = result.detail
    except Exception as error:
        # The savepoint itself could not be unwound, so the transaction cannot be
        # continued. The outcome is still reported and the run goes on.
        connection.rollback()
        return PermittedOutcome(
            label=label,
            line=statement.line,
            succeeded=False,
            detail=_first_line(error),
        )
    connection.commit()
    return PermittedOutcome(
        label=label,
        line=statement.line,
        succeeded=succeeded,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Operational entry point
# ---------------------------------------------------------------------------


def connect(dsn: str) -> Connection:
    """Open a connection through the driver, importing it on first use.

    The driver is loaded here rather than at module import so that this module
    imports, and every credential-free suite collects, with no driver installed.
    """
    try:
        package = importlib.import_module("psycopg")
    except ModuleNotFoundError as exc:
        raise MigrationError(
            "the database driver is not installed, so no migration can be applied; "
            "install the project dependencies"
        ) from exc
    created: object = package.connect(dsn, autocommit=False)
    return cast(Connection, created)


def render_report(report: MigrationReport) -> str:
    """Render one line per migration, plus a line per permitted outcome."""
    lines: list[str] = []
    for outcome in report.outcomes:
        state = "applied" if outcome.applied else "already applied"
        lines.append(f"{outcome.version:03d} {outcome.name}: {state}")
        for permitted in outcome.permitted:
            result = "succeeded" if permitted.succeeded else f"rejected: {permitted.detail}"
            lines.append(f"    permitted {permitted.label} at line {permitted.line}: {result}")
    lines.append(
        f"total: {len(report.applied_versions)} applied, "
        f"{len(report.skipped_versions)} already applied, "
        f"state {'changed' if report.changed_state else 'unchanged'}"
    )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="molt-migrate",
        description="Apply the schema migrations in ascending order and report what ran.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Apply every unrecorded migration and return the process exit status.

    The connection string resolves through the configuration surface, so it comes
    from the runtime environment or the parameter store and never from an
    argument, a file in the tree, or a default.
    """
    _build_parser().parse_args(argv)
    dsn = resolve_dsn(load_configuration())
    connection = connect(dsn.reveal())
    try:
        report = apply_migrations(connection)
    except MigrationError as error:
        print(f"migrate: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
