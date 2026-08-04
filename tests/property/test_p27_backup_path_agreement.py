"""Property 27: a backup record names the path that was actually taken.

**Validates: Requirements 19.2, 19.3, 19.5, 19.6, 19.7, 21.7**

The claim is about agreement between three things that are written in three
different places: the path value on the backup row, the pair of flags beside it,
and the backup evidence a certificate later states. A run that took a self-managed
backup and recorded a referenced one, or recorded a taken backup after every path
refused, would present a certificate whose backup clause is false while every
other clause of it is true. That is the failure this property exists to exclude.

Five decisions shape what is generated and what is asserted.

**Nothing here reaches a cluster, a control plane, or a subprocess.** The path
decision is a function of the capability record and the skip flag, and both effects
it can have leave through injected seams: the statement issuer and the command
runner. So the whole decision, including both refusal arms, is drivable in process,
and the property is about the recording discipline rather than about any cluster's
mood. A real invocation would also be a real backup, which is not something a
property may take a hundred of.

**The path actually taken is observed rather than assumed.** The stub issuer and
the stub runner each record whether they were reached and whether they answered, so
every assertion compares the recorded row against what the seams saw happen. If
the module recorded the managed path while the statement was the thing that
succeeded, the seams would say so.

**The failure selector fails zero, one, or both paths independently of the
capability selector.** That crossing is what separates the two questions a wrong
implementation conflates: which path the capability record chooses, and which path
happens to work. It is what makes the arm where the primary path refuses while the
fallback would have answered reachable, and that arm is the one where a
fall-through would be visible as a referenced backup under an available capability.

**A capability record that was never probed is generated beside the two the
property names.** An unprobed fact is not evidence of absence, and the delivered
tier carries the capability, so an unprobed record must leave the primary path in
place. Generating it is what keeps the fallback condition an equality with
"probed and unavailable" rather than a negation of "available".

**The abort clause is asserted against memory content, not against a return
value.** Each example carries a small memory graph standing for the content tables,
and the stub engine mutates it only after the backup gate has passed. A failing
backup with no skip flag must leave every table byte-identical, which is what
Requirement 19.3 asks and what a returned status alone would not show.

The example budget is 100 with no per-example deadline. An example builds a graph
of at most a few dozen rows and performs one decision, so the cost is uniform, but
a deadline would still be a wall-clock assertion about the machine rather than
about the code.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from hypothesis import event, given, settings
from hypothesis import strategies as st

from molt.backup import (
    MANAGED_BACKUP_ARGUMENTS,
    MANAGED_BACKUP_SUBCOMMAND,
    SELF_MANAGED_BACKUP_STATEMENT,
    BackupPath,
    BackupRecord,
    BackupSettings,
    BackupStatus,
    CommandResult,
    managed_backup_vector,
    render_command_vector,
    take_backup,
)
from molt.store.capability import SELF_MANAGED_BACKUP, Capability, CapabilityRecord

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

# The instant every generated value is placed relative to, read from a fixed
# offset rather than from the host, so no run embeds a reading of the machine it
# ran on.
FIXED_INSTANT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# The run the scenario is executed for. Fixed, because the identifier is carried
# rather than decided, and a drawn one would vary an input nothing branches on.
RUN_ID: Final[UUID] = UUID(int=(1 << 100) + 27)

# The settings every scenario resolves against. The target names a scheme and a
# bucket that resolve nowhere, which is the second guard against a real call:
# even if a seam leaked, there is nothing on the other side of it.
SETTINGS: Final[BackupSettings] = BackupSettings(
    target="s3://operator-owned-bucket.invalid/cluster-backups",
    ccloud_binary="control-plane-command",
    cluster_id="00000000-0000-0000-0000-000000000000",
    timeout_seconds=600,
)

# How many backups a generated listing reports, and how far apart their instants
# stand. More than one, so the fallback's choice of the most recent is a choice.
MIN_LISTED: Final[int] = 1
MAX_LISTED: Final[int] = 4
LISTING_STEP_SECONDS: Final[int] = 3600

# The content tables a memory graph stands for, and how many rows one may hold.
CONTENT_TABLES: Final[tuple[str, ...]] = ("ledger", "derived_artifact", "client_binding")
MIN_ROWS: Final[int] = 0
MAX_ROWS: Final[int] = 12


class CapabilitySelector(StrEnum):
    """What the capability record says about the self-managed path."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNPROBED = "unprobed"


class FailureSelector(StrEnum):
    """Which of the two paths refuses when it is reached."""

    NEITHER = "neither"
    PRIMARY = "primary"
    FALLBACK = "fallback"
    BOTH = "both"


class FallbackFault(StrEnum):
    """How the control plane fails to answer, when it fails.

    Three ways, because they leave by different arms of the reading code and a
    single one would leave two of them unexercised.
    """

    EXIT_STATUS = "exit_status"
    UNREADABLE = "unreadable"
    EMPTY_LISTING = "empty_listing"


# ---------------------------------------------------------------------------
# What a drawn example is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryGraph:
    """The memory content one run would mutate, as a row count per content table.

    A count is enough for what this property asserts. The claim is that a refused
    backup leaves every content table exactly as it was, and a count that changed
    is what a mutation looks like from outside the table.
    """

    rows: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        """How many rows the whole graph holds."""
        return sum(count for _, count in self.rows)


@dataclass(frozen=True, slots=True)
class Scenario:
    """One drawn run: a graph, a capability record, a failure pattern, a skip flag."""

    graph: MemoryGraph
    capability: CapabilitySelector
    failure: FailureSelector
    fallback_fault: FallbackFault
    listed: int
    skip: bool

    @property
    def primary_refuses(self) -> bool:
        """Whether the self-managed statement refuses when it is issued."""
        return self.failure in (FailureSelector.PRIMARY, FailureSelector.BOTH)

    @property
    def fallback_refuses(self) -> bool:
        """Whether the control plane refuses to answer when it is asked."""
        return self.failure in (FailureSelector.FALLBACK, FailureSelector.BOTH)

    def capability_record(self) -> CapabilityRecord:
        """The capability record this scenario runs under."""
        if self.capability is CapabilitySelector.UNPROBED:
            return CapabilityRecord()
        available = self.capability is CapabilitySelector.AVAILABLE
        return CapabilityRecord((Capability(SELF_MANAGED_BACKUP, available=available),))


# ---------------------------------------------------------------------------
# The seams, stubbed, recording what they saw
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubIssuer:
    """The statement seam. Records every statement and refuses when told to."""

    refuses: bool
    issued: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def __call__(self, statement: str, parameters: tuple[object, ...]) -> None:
        """Record one statement and its bound parameters, or refuse it."""
        self.issued.append((statement, parameters))
        if self.refuses:
            raise RuntimeError("the cluster refused the backup")

    @property
    def answered(self) -> bool:
        """Whether a statement was issued and accepted."""
        return bool(self.issued) and not self.refuses


@dataclass(slots=True)
class StubRunner:
    """The subprocess seam. Records every vector and never starts a process."""

    fault: FallbackFault | None
    listed: int
    invoked: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, vector: Sequence[str], *, timeout_seconds: int) -> CommandResult:
        """Record one argument vector and answer as the drawn fault requires."""
        assert not isinstance(vector, str), (
            "the control plane was invoked with a shell string rather than an argument vector"
        )
        assert timeout_seconds > 0
        self.invoked.append(tuple(vector))
        if self.fault is FallbackFault.EXIT_STATUS:
            return CommandResult(exit_status=1, stdout="", stderr="the command would not answer")
        if self.fault is FallbackFault.UNREADABLE:
            return CommandResult(exit_status=0, stdout="not a listing at all")
        if self.fault is FallbackFault.EMPTY_LISTING:
            return CommandResult(exit_status=0, stdout=json.dumps({"backups": []}))
        return CommandResult(exit_status=0, stdout=listing_of(self.listed))

    @property
    def answered(self) -> bool:
        """Whether the control plane was asked and named a backup."""
        return bool(self.invoked) and self.fault is None


def listing_of(count: int) -> str:
    """A control-plane listing of the given number of backups, newest last.

    Every instant is rendered from the fixed offset at run time rather than
    written as a literal, so this module carries no timestamp of its own.
    """
    entries = [
        {
            "id": f"managed-backup-{index:02d}",
            "as_of": (FIXED_INSTANT + timedelta(seconds=LISTING_STEP_SECONDS * index)).isoformat(),
        }
        for index in range(count)
    ]
    return json.dumps({"backups": entries})


def newest_identifier(count: int) -> str:
    """The identifier of the most recent backup in a listing of that size."""
    return f"managed-backup-{count - 1:02d}"


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def memory_graphs() -> st.SearchStrategy[MemoryGraph]:
    """Draw the memory content a run would mutate, one row count per content table."""
    return st.builds(
        MemoryGraph,
        st.tuples(
            *(
                st.tuples(st.just(table), st.integers(min_value=MIN_ROWS, max_value=MAX_ROWS))
                for table in CONTENT_TABLES
            )
        ),
    )


def backup_scenarios() -> st.SearchStrategy[Scenario]:
    """Draw a run over a memory graph, crossed with the three selectors.

    The capability selector and the failure selector are drawn independently on
    purpose: which path the record chooses and which path happens to work are
    different questions, and only the crossing reaches the arm where the chosen
    path refuses while the other one would have answered.
    """
    return st.builds(
        Scenario,
        memory_graphs(),
        st.sampled_from(CapabilitySelector),
        st.sampled_from(FailureSelector),
        st.sampled_from(FallbackFault),
        st.integers(min_value=MIN_LISTED, max_value=MAX_LISTED),
        st.booleans(),
    )


# ---------------------------------------------------------------------------
# The stub engine: the backup gate, then the mutation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one stubbed run produced: the record, the graph after, and the seams."""

    record: BackupRecord
    graph_after: MemoryGraph
    issuer: StubIssuer
    runner: StubRunner
    aborted: bool

    @property
    def path_taken(self) -> BackupPath | None:
        """The path the seams say actually secured a backup, or None where none did."""
        if self.issuer.answered:
            return BackupPath.SELF_MANAGED
        if self.runner.answered:
            return BackupPath.MANAGED_REFERENCED
        return None


def certificate_evidence(record: BackupRecord) -> dict[str, object]:
    """The backup clause a certificate would carry for one record.

    Assembled from the stored row alone, the way the certificate builder assembles
    it, so what this property compares is the row's own story rather than a value
    the decision handed sideways to a certificate.
    """
    return {
        "path": None if record.backup_path is None else record.backup_path.value,
        "taken": record.taken,
        "referenced": record.referenced,
        "backup_id": record.backup_id,
        "target_uri": record.target_uri,
        "command": record.command,
        "status": record.status.value,
    }


def execute(scenario: Scenario) -> RunOutcome:
    """Run the backup gate and mutate the graph only if the gate let the run through.

    This is the ordering Requirement 19.3 is about: the backup is secured before
    the first mutation, and a fatal record means no mutation happens at all.
    """
    issuer = StubIssuer(refuses=scenario.primary_refuses)
    runner = StubRunner(
        fault=scenario.fallback_fault if scenario.fallback_refuses else None,
        listed=scenario.listed,
    )
    instants = iter(
        FIXED_INSTANT + timedelta(seconds=index) for index in range(1, MAX_EXAMPLES + 1)
    )
    record = take_backup(
        RUN_ID,
        capabilities=scenario.capability_record(),
        settings=SETTINGS,
        issuer=issuer,
        runner=runner,
        clock=lambda: next(instants),
        skip=scenario.skip,
    )
    if record.fatal:
        return RunOutcome(record, scenario.graph, issuer, runner, aborted=True)
    emptied = replace(scenario.graph, rows=tuple((table, 0) for table, _ in scenario.graph.rows))
    return RunOutcome(record, emptied, issuer, runner, aborted=False)


def record_coverage(scenario: Scenario, outcome: RunOutcome) -> None:
    """Report what one example covered, so the arms can be seen to be reached."""
    event(f"capability={scenario.capability}")
    event(f"failure={scenario.failure}")
    event(f"skip flag passed={scenario.skip}")
    event(f"path actually taken={outcome.path_taken}")
    event(f"recorded status={outcome.record.status}")
    event(f"run aborted={outcome.aborted}")
    event(f"graph held rows={scenario.graph.total > 0}")
    if scenario.fallback_refuses:
        event(f"fallback fault={scenario.fallback_fault}")


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 27: For any Erasure_Run executed under any capability
# record marking the Self_Managed_Backup path available or unavailable, the
# recorded backup path value, the taken and referenced flags on the backup record,
# and the backup evidence in the Erasure_Certificate all name the path actually
# taken; no run records a taken backup when no backup succeeded; and when no path
# succeeds and no skip flag was passed, the run aborts with every memory-content
# table unchanged.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(scenario=backup_scenarios())
def test_the_recorded_path_and_flags_name_the_path_actually_taken(scenario: Scenario) -> None:
    outcome = execute(scenario)
    record = outcome.record
    actual = outcome.path_taken
    evidence = certificate_evidence(record)
    record_coverage(scenario, outcome)

    # Requirement 19.5 and 19.6: the fallback is entered exactly where the record
    # reports the path probed and unavailable, so an unprobed fact leaves the
    # primary path in place and a working fallback never substitutes for a
    # refused primary.
    if scenario.skip:
        assert not outcome.issuer.issued, "the skip flag was passed and a backup was issued anyway"
        assert not outcome.runner.invoked, (
            "the skip flag was passed and the control plane was asked"
        )
    elif scenario.capability is CapabilitySelector.UNAVAILABLE:
        assert outcome.runner.invoked, "the capability record reported no self-managed path"
        assert not outcome.issuer.issued, "a backup statement was issued on the fallback path"
    else:
        assert outcome.issuer.issued, "the primary path was available and no statement was issued"
        assert not outcome.runner.invoked, (
            "the control plane was asked although the capability record reported "
            "the self-managed path usable"
        )

    # Requirements 19.2 and 19.7: the flags name the path actually taken, and the
    # two are alternatives rather than degrees of one thing.
    assert not (record.taken and record.referenced)
    assert record.evidence == actual, (
        f"the flags name {record.evidence} where the seams say {actual} secured the backup"
    )
    assert evidence["taken"] == (actual is BackupPath.SELF_MANAGED)
    assert evidence["referenced"] == (actual is BackupPath.MANAGED_REFERENCED)

    if actual is BackupPath.SELF_MANAGED:
        # Requirement 19.2: the target, the statement as issued, and the instant.
        assert record.status is BackupStatus.SUCCEEDED
        assert record.backup_path is BackupPath.SELF_MANAGED
        assert record.taken and not record.referenced
        assert record.target_uri == SETTINGS.target
        assert record.command == SELF_MANAGED_BACKUP_STATEMENT
        assert outcome.issuer.issued == [(SELF_MANAGED_BACKUP_STATEMENT, (SETTINGS.target,))]
        assert record.taken_at is not None
        assert evidence["path"] == BackupPath.SELF_MANAGED.value
    elif actual is BackupPath.MANAGED_REFERENCED:
        # Requirements 19.6 and 19.7: the identifier, that backup's own instant,
        # the exact command vector, and referenced rather than taken.
        assert record.status is BackupStatus.SUCCEEDED
        assert record.backup_path is BackupPath.MANAGED_REFERENCED
        assert record.referenced and not record.taken
        assert record.backup_id == newest_identifier(scenario.listed)
        assert record.taken_at == FIXED_INSTANT + timedelta(
            seconds=LISTING_STEP_SECONDS * (scenario.listed - 1)
        )
        assert record.command_vector == managed_backup_vector(SETTINGS)
        assert record.command == render_command_vector(record.command_vector)
        assert outcome.runner.invoked == [managed_backup_vector(SETTINGS)]
        assert evidence["path"] == BackupPath.MANAGED_REFERENCED.value
    else:
        # No run records a taken backup when no backup succeeded, whichever way
        # the paths were arranged to fail.
        assert not record.taken, "a taken backup was recorded although no path succeeded"
        assert not record.referenced, "a referenced backup was recorded although none was named"
        assert record.status is not BackupStatus.SUCCEEDED
        assert record.backup_id is None
        assert evidence["path"] in {
            None,
            BackupPath.SELF_MANAGED.value,
            BackupPath.MANAGED_REFERENCED.value,
        }

    # Requirement 19.3: no successful path and no skip flag aborts the run, and
    # every content table stands exactly as it did.
    if actual is None and not scenario.skip:
        assert record.status is BackupStatus.FAILED
        assert record.fatal, "the run was left free to mutate memory with no backup evidence"
        assert record.detail, "a failed backup records no detail of what refused"
        assert outcome.aborted
        assert outcome.graph_after == scenario.graph, (
            "a run that secured no backup evidence mutated memory content"
        )
    else:
        assert not record.fatal
        assert outcome.graph_after.total == 0, "a run the gate admitted mutated nothing"

    # Requirement 19.4: the skip flag reaches neither path and records neither
    # flag, so the certificate records an absent backup rather than a false one.
    if scenario.skip:
        assert record.status is BackupStatus.SKIPPED
        assert record.backup_path is None
        assert not record.taken and not record.referenced
        assert record.evidence is None
        assert evidence["path"] is None

    # The invocation is an argument vector throughout, never a shell string, and
    # the recorded vector is the one that was invoked.
    for invoked in outcome.runner.invoked:
        assert invoked[0] == SETTINGS.ccloud_binary
        assert invoked[1 : 1 + len(MANAGED_BACKUP_SUBCOMMAND)] == MANAGED_BACKUP_SUBCOMMAND
        assert SETTINGS.cluster_id in invoked
        assert invoked[-len(MANAGED_BACKUP_ARGUMENTS) :] == MANAGED_BACKUP_ARGUMENTS
        assert json.loads(render_command_vector(invoked)) == list(invoked)
