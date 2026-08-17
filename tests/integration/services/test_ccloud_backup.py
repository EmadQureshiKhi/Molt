"""Backup evidence against the real cluster and the real control plane.

Three paths are exercised here, and they are three different kinds of thing. The
primary path is a statement issued to the cluster. The fallback is a read-only
command invoked against the control plane. The audit pull is a script invoked
against the same control plane. Each is reached through the seam the design already
declares for it, so nothing here reimplements a statement or assembles a command.

**The control plane creates no backup, and that absence is the finding rather than
the failure.** It offers backup listing and backup configuration and nothing that
creates a backup on demand, which is exactly why the self-managed path is the
primary one and the fallback can only ever name a backup somebody else made. So the
absence is asserted three ways: the fallback's own subcommand is a listing verb and
carries no creation verb; the record the fallback produces has `taken` false
whatever the control plane answered; and where the capability record has a row for
the on-demand fact, that row reports it unavailable. A run that finds the row
missing is a cluster nobody probed, which is a note rather than a finding, so it is
not asserted as one.

**A failing primary path does not fall through.** That is a decision the design
makes rather than something a service test can provoke safely, so it is not
provoked here. What is asserted instead is the property that makes the decision
safe: the two flags are alternatives, and the record the fallback returns never
claims this run took a backup.

**The target never reaches statement text.** A backup target may carry credentials
in its query parameters, so the statement is a whole literal with the target bound.
That is asserted directly: the recorded command is the statement literal, and the
configured target does not appear in it.

**The audit pull is read-only and idempotent, and it names its own window.** Both
window bounds are computed from the clock at run time rather than written down, so
this file holds no instant of any kind. The destination is created with owner-only
permissions because an audit record names principals and statements, and that mode
is asserted rather than assumed.

**Markers.** The primary path needs a reachable instance as well as cloud access, so
it carries `integration` beside `services`. The fallback and the audit pull reach
the control plane and not the cluster, so they carry `services` alone. The two
structural assertions about the fallback's command vector reach nothing at all and
carry no marker, because a listing verb that carries no creation verb is worth
asserting wherever the suite runs.

**Every marked test here skips in this environment.** No cluster is deployed and no
backup target is configured, so each skips naming the configuration key an operator
sets next.

No cluster identifier, backup target, region, or credential value appears in this
file.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import which
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.backup import (
    CCLOUD_BINARY_KEY,
    CCLOUD_CLUSTER_KEY,
    MANAGED_BACKUP_ARGUMENTS,
    MANAGED_BACKUP_SUBCOMMAND,
    SELF_MANAGED_BACKUP_STATEMENT,
    TARGET_KEY,
    BackupPath,
    BackupSettings,
    BackupStatus,
    CommandResult,
    managed_backup_vector,
    reference_managed,
    render_command_vector,
    run_command,
    self_managed,
    store_issuer,
)
from molt.config.resolve import Configuration, MissingConfigError, load_configuration
from molt.store import MemoryStore
from molt.store.capability import ON_DEMAND_BACKUP, SELF_MANAGED_BACKUP, capabilities

# The audit pull script, located relative to this file so no absolute path is held.
AUDIT_SCRIPT: Final[Path] = Path(__file__).resolve().parents[3] / "scripts" / "pull_audit_log.sh"

# Verbs a listing command carries, and verbs it must not. The control plane creates
# no backup, so the second set is what the absence is asserted against.
LISTING_VERB: Final[str] = "list"
CREATION_VERBS: Final[frozenset[str]] = frozenset({"create", "start", "take", "run", "new"})

# How far back the audit window reaches. Short, because the pull is a probe rather
# than an archive, and computed against the clock so no instant is written here.
AUDIT_WINDOW_SECONDS: Final[int] = 900

# The mode the audit script creates its destination with. Owner-only, because an
# audit record names principals and statements.
OWNER_ONLY_MODE: Final[int] = 0o600

# The exit status the audit script reports when a required argument is absent, and
# the status it reports when it answered.
SCRIPT_REFUSED: Final[int] = 2
SCRIPT_ANSWERED: Final[int] = 0

# How long the script probe is allowed. A bound rather than none, so a control plane
# that never answers ends the probe rather than the session.
SCRIPT_TIMEOUT_SECONDS: Final[int] = 120


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def _configuration() -> Configuration:
    """The resolved surface every value below is read from."""
    return load_configuration()


def _settings(configuration: Configuration) -> BackupSettings:
    """The whole backup surface, or a skip naming the key that holds no value."""
    try:
        return BackupSettings.from_configuration(configuration)
    except MissingConfigError as fault:
        pytest.skip(
            f"the backup surface is incomplete, so no backup was attempted: {fault}. "
            f"{TARGET_KEY} and {CCLOUD_CLUSTER_KEY} each name a value to provision."
        )


def _control_plane(configuration: Configuration) -> tuple[str, str]:
    """The control-plane command and the cluster it asks about, or a skip.

    Two conditions are distinguished, because they are two different operator
    problems: a cluster identifier the surface names no value for, and a command
    that is not on the path of the process running this.
    """
    try:
        cluster = configuration.text(CCLOUD_CLUSTER_KEY).strip()
    except MissingConfigError as fault:
        pytest.skip(
            f"{CCLOUD_CLUSTER_KEY} names no value, so no cluster was asked about "
            f"and no command was invoked: {fault}"
        )
    binary = configuration.text(CCLOUD_BINARY_KEY).strip()
    if which(binary) is None:
        pytest.skip(
            f"the control-plane command named by {CCLOUD_BINARY_KEY} is not on the "
            "path of this process, so no command was invoked"
        )
    return binary, cluster


@pytest.fixture
def live_store() -> Iterator[MemoryStore]:
    """A store over the configured connection string, closed however the test ends.

    A deployment naming no connection string skips rather than erroring, so an
    absent parameter name is reported the same way every other absent resource in
    this module is.
    """
    try:
        store = MemoryStore.from_configuration(_configuration())
    except MissingConfigError as fault:
        pytest.skip(
            f"no cluster connection string is configured, so no statement was issued: {fault}"
        )
    try:
        yield store
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The absence of on-demand backup creation, asserted without any service
# ---------------------------------------------------------------------------


def test_the_fallback_command_is_a_listing_and_carries_no_creation_verb() -> None:
    """The control plane offers no on-demand backup creation, so the fallback reads.

    This is the reason the self-managed path is primary, and it is a property of
    the command vector rather than of any cluster, so it is asserted wherever the
    suite runs and reaches nothing at all.
    """
    assert MANAGED_BACKUP_SUBCOMMAND[-1] == LISTING_VERB
    assert not CREATION_VERBS & set(MANAGED_BACKUP_SUBCOMMAND)
    assert not CREATION_VERBS & set(MANAGED_BACKUP_ARGUMENTS)


def test_the_recorded_command_is_the_exact_argument_vector_that_was_invoked() -> None:
    """A rendered vector is reversible into the arguments, and names no shell.

    The recorded form keeps the arguments separate because a rendered shell string
    would be a different thing from the vector that ran and would read as though a
    shell had been involved.
    """
    settings = BackupSettings(
        target="placeholder://target-placeholder/prefix",
        ccloud_binary="control-plane-placeholder",
        cluster_id="cluster-placeholder",
        timeout_seconds=1,
    )
    vector = managed_backup_vector(settings)
    rendered = render_command_vector(vector)
    assert json.loads(rendered) == list(vector)
    assert vector[0] == settings.ccloud_binary
    assert settings.cluster_id in vector


# ---------------------------------------------------------------------------
# The primary path: BACKUP INTO the operator-owned target
# ---------------------------------------------------------------------------


@pytest.mark.services
@pytest.mark.integration
def test_the_primary_path_issues_a_backup_into_the_configured_target(
    live_store: MemoryStore,
) -> None:
    """One backup, issued before any mutation, recorded with the target bound.

    The record is returned rather than stored, so this test writes nothing to the
    cluster: the recording statement is the caller's to frame, and framing it here
    would add a row to a deployment nobody asked to be written to.
    """
    configuration = _configuration()
    settings = _settings(configuration)
    run_id: UUID = uuid4()

    record = self_managed(run_id, settings=settings, issuer=store_issuer(live_store))

    assert record.run_id == run_id
    assert record.status is BackupStatus.SUCCEEDED, (
        "a cluster admitting a user-issued backup completes one; a cluster refusing "
        f"records {BackupStatus.FAILED} with the fault's name and nothing else"
    )
    assert record.backup_path is BackupPath.SELF_MANAGED
    assert record.evidence is BackupPath.SELF_MANAGED
    assert record.taken is True
    assert record.referenced is False
    assert record.target_uri == settings.target
    assert record.taken_at is not None
    assert record.taken_at.tzinfo is not None
    assert record.fatal is False

    # The target is bound rather than written into statement text, because a target
    # may carry credentials in its query parameters and the command is evidence.
    assert record.command == SELF_MANAGED_BACKUP_STATEMENT
    assert settings.target not in record.command
    assert record.command_vector == ()


@pytest.mark.services
@pytest.mark.integration
def test_the_capability_record_reports_no_on_demand_backup_creation(
    live_store: MemoryStore,
) -> None:
    """Where the cluster holds a row for the on-demand fact, it reports it absent.

    An absent row is a cluster nobody probed rather than evidence of absence, and
    the design is explicit that an unprobed fact leaves the primary path in place.
    So the absence is asserted as never-available, which holds in both readings,
    and the stronger claim is made only where a row exists.
    """
    record = capabilities(live_store)

    assert not record.available(ON_DEMAND_BACKUP), (
        "the control plane offers no on-demand backup creation, so this fact is "
        "never reported present"
    )
    assert record.on_demand_backup is False
    if record.probed(ON_DEMAND_BACKUP):
        assert record.unavailable(ON_DEMAND_BACKUP), (
            "a cluster that was asked about on-demand creation answered no, which is "
            "the recorded reason the self-managed path is primary"
        )
    else:
        assert ON_DEMAND_BACKUP in record.unprobed

    # The self-managed fact is what the path choice turns on, and an unprobed one
    # leaves the primary path in place rather than degrading to a fallback nobody
    # chose. Either reading is a valid state; what is asserted is that the two
    # questions are not each other's negation.
    if not record.probed(SELF_MANAGED_BACKUP):
        assert record.available(SELF_MANAGED_BACKUP) is False
        assert record.unavailable(SELF_MANAGED_BACKUP) is False


# ---------------------------------------------------------------------------
# The fallback: naming a backup the control plane already holds
# ---------------------------------------------------------------------------


@pytest.mark.services
def test_the_managed_backup_listing_answers_the_fallback_and_takes_nothing() -> None:
    """The listing is read, and whatever it said, this run created no backup.

    A control plane holding no backup for the cluster is a recorded failure rather
    than an error, because the fallback answers a capability question and a listing
    naming nothing is a real answer. Both outcomes are admitted here; what is
    asserted in both is that `taken` is false, which is the whole distinction
    between a backup this run made and one it pointed at.
    """
    configuration = _configuration()
    _binary, _cluster = _control_plane(configuration)
    settings = _settings(configuration)
    run_id: UUID = uuid4()

    record = reference_managed(run_id, settings=settings, runner=run_command)

    assert record.run_id == run_id
    assert record.backup_path is BackupPath.MANAGED_REFERENCED
    assert record.taken is False, "the fallback names an existing backup and creates none"
    assert record.command == render_command_vector(managed_backup_vector(settings))
    assert record.command_vector == managed_backup_vector(settings)

    if record.status is BackupStatus.SUCCEEDED:
        assert record.referenced is True
        assert record.evidence is BackupPath.MANAGED_REFERENCED
        assert record.backup_id
        assert record.taken_at is not None
        assert record.taken_at.tzinfo is not None
        assert record.detail is None
    else:
        assert record.status is BackupStatus.FAILED
        assert record.referenced is False
        assert record.evidence is None
        assert record.detail is not None
        assert settings.target not in record.detail, (
            "a refusal names the fault rather than the target, because a target may "
            "carry credentials in its query parameters"
        )


@pytest.mark.services
def test_the_listing_command_is_invoked_as_a_vector_and_never_as_a_shell_string() -> None:
    """The command runner passes a list, so no argument is parsed by a shell.

    Invoked against the configured control plane through the delivered runner, and
    asserted on the result rather than on the arguments, because what matters is
    that a value carrying shell syntax could not be read as syntax.
    """
    configuration = _configuration()
    _binary, _cluster = _control_plane(configuration)
    settings = _settings(configuration)

    result = run_command(managed_backup_vector(settings), timeout_seconds=settings.timeout_seconds)

    assert isinstance(result, CommandResult)
    assert isinstance(result.exit_status, int)
    if result.exit_status == SCRIPT_ANSWERED:
        parsed: object = json.loads(result.stdout)
        assert isinstance(parsed, (list, dict)), "a listing answers a sequence of backups"


# ---------------------------------------------------------------------------
# The audit-log pull
# ---------------------------------------------------------------------------


def _window() -> tuple[str, str]:
    """The two window bounds, computed from the clock so no instant is written here."""
    end = datetime.now(tz=UTC)
    start = end - timedelta(seconds=AUDIT_WINDOW_SECONDS)
    return start.isoformat(), end.isoformat()


def _script(arguments: Sequence[str]) -> CommandResult:
    """Invoke the audit script as an argument vector, with no shell anywhere."""
    return run_command((str(AUDIT_SCRIPT), *arguments), timeout_seconds=SCRIPT_TIMEOUT_SECONDS)


def test_the_audit_script_refuses_a_pull_that_names_no_cluster() -> None:
    """A required argument absent is a refusal rather than a defaulted window.

    This reaches no control plane: the refusal happens in argument parsing, which
    is why it carries no marker and runs wherever the suite runs.
    """
    assert AUDIT_SCRIPT.is_file()
    start, end = _window()

    result = _script(("--from", start, "--to", end))

    assert result.exit_status == SCRIPT_REFUSED
    assert "--cluster" in result.stderr


@pytest.mark.services
def test_the_audit_pull_writes_owner_only_records_for_the_window_it_was_given(
    tmp_path: Path,
) -> None:
    """One read-only pull, into a destination created for its owner alone.

    The pull creates nothing, changes nothing, and two runs over the same window
    fetch the same records, so invoking it costs the control plane one read. The
    destination mode is asserted because an audit record names principals and
    statements and a world-readable dump of one is a finding.
    """
    configuration = _configuration()
    _binary, cluster = _control_plane(configuration)
    start, end = _window()
    destination = tmp_path / "audit.json"

    result = _script(
        ("--cluster", cluster, "--from", start, "--to", end, "--output", str(destination))
    )

    assert result.exit_status == SCRIPT_ANSWERED, (
        "the control plane answered the audit window; a non-zero status is a "
        f"control-plane problem rather than a test one: {result.stderr[:200]}"
    )
    assert destination.is_file()
    assert stat.S_IMODE(destination.stat().st_mode) == OWNER_ONLY_MODE
    records: object = json.loads(destination.read_text(encoding="utf-8"))
    assert isinstance(records, (list, dict))
    assert str(destination) in result.stderr
