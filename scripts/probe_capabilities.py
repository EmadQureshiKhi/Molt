#!/usr/bin/env python3.12
"""The provisioning-time capability probes, recorded rather than assumed.

Every degradation this deployment can take turns on a platform fact somebody
measured, so provisioning ends by measuring each of them and writing what it
measured into the capability record the components read at start-up. Nothing here
decides a fact from a version string or from a tier name: a row is written only
where a statement was really sent or a reachability call really answered.

Five facts are recorded:

* the distributed vector index over the vector column, together with the operator
  class the cluster reports for it, because that ordering is what makes unit
  normalisation at write time load-bearing;
* the cluster setting a sinkless change stream depends on, read from the cluster
  rather than presumed enabled, because the watcher's polling fallback exists for
  the case where it is off;
* the measured garbage-collection horizon, which bounds how far a historical read
  can reach and is far shorter here than a typical default;
* the absence of an on-demand backup creation operation on the control plane,
  recorded as an absence with the detail that the control plane offers listing and
  configuration only, which is why the self-managed path is the primary one;
* the self-managed backup path, probed by asking the cluster to plan a backup into
  the operator-owned target without running it.

The control-plane interrogation itself belongs to the shell caller, which owns
every command-line invocation; what reaches this module is the observed outcome as
a choice from a fixed set, so no caller value ever becomes part of a command.

Reachability of every required model identifier is verified last, through each
configured provider's own probe. An unreachable identifier is named on standard
error and makes the run exit non-zero, because a deployment whose embedding model
cannot be reached is not a deployment that works.

No connection string, credential, or bearer token is read, printed, or logged
here. The store resolves its own connection string through the secret accessors
and holds it inside its connection factory, and the summary this module prints
carries capability names, availability, and measurements only.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Final

from molt.config.resolve import Configuration, load_configuration
from molt.providers import ProbeLike
from molt.providers.registry import load_embedding_builder, load_text_builder
from molt.store import Cursor, MemoryStore
from molt.store.capability import (
    ON_DEMAND_BACKUP,
    RANGEFEED_SETTING,
    Capability,
    CapabilityRecord,
    probe_platform,
    record_capability,
)

# The cluster setting a sinkless change stream depends on, read rather than set.
RANGEFEED_SETTING_QUERY: Final[str] = "SHOW CLUSTER SETTING kv.rangefeed.enabled"

# What the caller may report having seen on the control plane. The absent form is
# the delivered one, and the offered form exists so that a control plane which
# gains the operation later is recorded as having it rather than silently
# mismatched.
CONTROL_PLANE_ABSENT: Final[str] = "listing-and-configuration-only"
CONTROL_PLANE_OFFERED: Final[str] = "creation-offered"
CONTROL_PLANE_CHOICES: Final[tuple[str, ...]] = (CONTROL_PLANE_ABSENT, CONTROL_PLANE_OFFERED)

# The detail recorded beside the absence, which states what the control plane does
# offer so a reader of the record needs no second source.
CONTROL_PLANE_DETAIL: Final[str] = "listing and configuration only"

# Exit statuses, matching the command-line surface: an operational failure, a
# usage or configuration fault, and success.
EXIT_OK: Final[int] = 0
EXIT_OPERATIONAL: Final[int] = 1
EXIT_USAGE: Final[int] = 2

# The configuration surface is read by a setting's environment name rather than by its
# key. Both spellings exist for every setting and only one of them resolves, so naming
# the key here meant every provider build raised an unknown-setting fault before a
# provider was selected. The consequence was quiet rather than loud: the two provider
# probes are reported as a warning and the run still succeeds, so the prompt-cache
# capability was left permanently unprobed and the text provider always took the
# unprobed path, on every cluster this had ever been run against.
EMBEDDING_PROVIDER_ENV: Final[str] = "MOLT_EMBEDDING_PROVIDER"
TEXT_PROVIDER_ENV: Final[str] = "MOLT_TEXT_PROVIDER"


def _rangefeed_enabled(store: MemoryStore) -> bool:
    """Read the change-stream cluster setting, reporting a failed read as disabled.

    A setting that cannot be read is recorded as unavailable rather than left
    unprobed, because an unprobed fact leaves the primary path in place and a
    change stream that cannot be confirmed must put the watcher on its polling
    fallback instead.
    """

    def body(cursor: Cursor) -> tuple[object, ...] | None:
        cursor.execute(RANGEFEED_SETTING_QUERY)
        return cursor.fetchone()

    try:
        row = store.read(body)
    # A read that could not complete is a recorded absence rather than a crash.
    except Exception:
        return False
    if row is None or not row:
        return False
    reported = row[0]
    if isinstance(reported, bool):
        return reported
    return str(reported).strip().lower() in {"true", "on", "t", "yes"}


def record_rangefeed_setting(store: MemoryStore) -> Capability:
    """Record what the cluster reports about the change-stream setting."""
    enabled = _rangefeed_enabled(store)
    return record_capability(store, Capability(RANGEFEED_SETTING, available=enabled))


def record_control_plane_backup(store: MemoryStore, observed: str) -> Capability:
    """Record whether the control plane offers an on-demand backup creation operation.

    The delivered control plane offers none, and the recorded detail states what it
    does offer, so the Backup_Manager's choice of the self-managed path reads as a
    measured absence rather than as an assumption.
    """
    offered = observed == CONTROL_PLANE_OFFERED
    return record_capability(
        store,
        Capability(ON_DEMAND_BACKUP, available=offered, detail=CONTROL_PLANE_DETAIL),
    )


def required_model_probes(configuration: Configuration) -> tuple[ProbeLike, ...]:
    """Probe every configured provider, one probe per required model role.

    The providers are constructed through the registry, so which models are
    required follows from the configuration rather than from a list restated here,
    and an operator who selects a different provider changes a configuration value
    rather than this module.
    """
    embedding = load_embedding_builder(configuration.text(EMBEDDING_PROVIDER_ENV))(configuration)
    text = load_text_builder(configuration.text(TEXT_PROVIDER_ENV))(configuration)
    return (embedding.probe(), text.probe())


def unreachable_identifiers(probes: Sequence[ProbeLike]) -> tuple[str, ...]:
    """The model identifiers whose probe did not answer reachable."""
    return tuple(probe.model_id for probe in probes if not probe.reachable)


def render_record(record: CapabilityRecord) -> str:
    """Render the recorded facts as one line each, carrying no value that could be secret."""
    lines = [
        f"{entry.name}: available={entry.available} detail={entry.detail or 'none'}"
        for entry in record.entries
    ]
    unprobed = record.unprobed
    if unprobed:
        lines.append(f"unprobed: {', '.join(unprobed)}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_capabilities",
        description="Probe the platform facts the deployment branches on and record each one.",
    )
    parser.add_argument(
        "--backup-target",
        default=None,
        help="operator-owned backup target the self-managed path is planned against",
    )
    parser.add_argument(
        "--control-plane-backup",
        choices=CONTROL_PLANE_CHOICES,
        default=CONTROL_PLANE_ABSENT,
        help="what the caller observed the control plane offering for backups",
    )
    parser.add_argument(
        "--skip-model-check",
        action="store_true",
        help="record the cluster facts and verify no model identifier",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run every probe, record each result, and report the record."""
    arguments = _build_parser().parse_args(argv)
    try:
        configuration = load_configuration()
        store = MemoryStore.from_configuration(configuration)
    # A fault is reported by the name of its type, never by any value it carries.
    except Exception as error:
        print(
            f"probe: configuration could not be resolved: {type(error).__name__}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        probe_platform(store, backup_target=arguments.backup_target)
        record_rangefeed_setting(store)
        record_control_plane_backup(store, arguments.control_plane_backup)
        record = store.capabilities(refresh=True)
    except Exception as error:
        print(
            f"probe: the capability record could not be written: {type(error).__name__}",
            file=sys.stderr,
        )
        return EXIT_OPERATIONAL

    print(render_record(record))

    if arguments.skip_model_check:
        return EXIT_OK

    try:
        probes = required_model_probes(configuration)
    except Exception as error:
        print(
            f"probe: a configured provider could not be built: {type(error).__name__}",
            file=sys.stderr,
        )
        return EXIT_OPERATIONAL

    unreachable = unreachable_identifiers(probes)
    for identifier in unreachable:
        print(
            f"probe: model identifier unreachable in the deployment region: {identifier}",
            file=sys.stderr,
        )
    if unreachable:
        return EXIT_OPERATIONAL
    for probe in probes:
        print(f"model reachable: {probe.model_id}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
