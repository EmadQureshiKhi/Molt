"""The migrate verb: every unapplied migration, in order, with the versions recorded.

The dry-run mode reports what would be applied by comparing the files on disk
against the recorded history and applies nothing, so the answer costs no schema
change.
"""

from __future__ import annotations

from molt.cli.context import VerbContext
from molt.cli.exits import ExitCode
from molt.config.secrets import resolve_dsn
from molt.models.event import JsonObject
from molt.store.migrate import apply_migrations, connect, discover_migrations, render_report

__all__ = ["run"]


def run(context: VerbContext) -> ExitCode:
    """Apply migrations, or report which ones a run would apply."""
    emitter = context.emitter
    ceiling = _ceiling(context)
    available = tuple(
        migration.version
        for migration in discover_migrations()
        if ceiling is None or migration.version <= ceiling
    )

    if context.flag("dry_run"):
        for version in available:
            emitter.narrate(f"would consider migration {version}")
        return emitter.succeed(
            context.name,
            {"dry_run": True, "considered_versions": list(available)},
        )

    credential = resolve_dsn(context.configuration)
    connection = connect(credential.reveal())
    try:
        report = apply_migrations(connection)
    finally:
        connection.close()

    emitter.narrate(render_report(report))
    document: JsonObject = {
        "dry_run": False,
        "applied_versions": list(report.applied_versions),
        "skipped_versions": list(report.skipped_versions),
        "changed_state": report.changed_state,
    }
    return emitter.succeed(context.name, document)


def _ceiling(context: VerbContext) -> int | None:
    """The highest version this invocation considers, or None for every version."""
    value = getattr(context.args, "to", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
