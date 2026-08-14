"""The erase verb: the three-phase run, its progress, and its disposition summary.

Confirmation is required rather than assumed. An erasure is irreversible by
design, so an invocation that neither confirms nor asks for a dry run is refused
as a usage fault rather than run and regretted.

The seams the engine reaches the world through are assembled here, which is the
point of them being a parameter: a deployment supplies configured providers, and
a provider that cannot be selected leaves the seam unset so the run takes the
fail-closed path rather than proceeding as though a model had answered.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from molt.backup import BackupSettings
from molt.cli.context import AGENT_CLI, VerbContext, client_id_for, machine_identifier
from molt.cli.exits import ExitCode, UsageError
from molt.cli.verbs.common import BATCH_SIZE_KEY, integer_overrides, threshold_overrides
from molt.config.resolve import Configuration
from molt.erase.engine import EngineSeams, ErasureRequest, PhaseProgress, run_erasure
from molt.models.event import JsonObject
from molt.providers import EmbeddingProvider, TextProvider
from molt.providers.selector import select_embedding_provider, select_text_provider
from molt.retention import default_interval, expiry_for
from molt.store import MemoryStore
from molt.store.attribution import SupersessionContext

__all__ = ["run"]

# Placeholders standing in for a backup target that is never consulted, used only
# where the operator asked for the backup to be skipped: the settings shape
# refuses empty fields and the skipped path reads none of them.
_UNUSED: Final[str] = "unused-because-the-backup-was-skipped"

_CCLOUD_KEY: Final[str] = "MOLT_CCLOUD_BIN"
_BACKUP_TIMEOUT_KEY: Final[str] = "MOLT_BACKUP_TIMEOUT_SECONDS"
_CERT_PREFIX_KEY: Final[str] = "MOLT_CERT_PREFIX"


def run(context: VerbContext) -> ExitCode:
    """Perform one erasure run and report what it did."""
    emitter = context.emitter
    dry_run = context.flag("dry_run")
    if not dry_run and not context.flag("yes"):
        raise UsageError("an erasure is irreversible, so it runs only with --yes or --dry-run")

    overrides = threshold_overrides(context)
    overrides.update(integer_overrides(context, {"batch_size": BATCH_SIZE_KEY}))
    configuration = context.configuration_for(overrides)
    skip_backup = context.flag("skip_backup")

    with MemoryStore.from_configuration(configuration) as store:
        client_id = client_id_for(store, context.required_text("client"))
        request = ErasureRequest(
            client_id=client_id,
            requester=context.required_text("requester"),
            justification=context.required_text("justification"),
            idempotency_key=uuid4().hex,
            dry_run=dry_run,
            skip_backup=skip_backup,
        )
        now = datetime.now(tz=UTC)
        seams = EngineSeams(
            configuration=configuration,
            backup=_backup_settings(configuration, skip_backup=skip_backup),
            capabilities=store.capabilities(),
            supersession=SupersessionContext(
                session_id=uuid4(),
                agent_cli=AGENT_CLI,
                machine_id=machine_identifier(configuration),
                expires_at=expiry_for(now, default_interval(configuration)),
            ),
            text_provider=_text_provider(configuration),
            embedding_provider=_embedding_provider(configuration),
            progress=lambda progress: _report(context, progress),
        )
        outcome = run_erasure(store, request, seams)

    certificate_key = _certificate_key(outcome.run_id, configuration)
    document: JsonObject = {
        "client_id": str(outcome.client_id),
        "status": str(outcome.status),
        "phase": str(outcome.phase),
        "dry_run": outcome.dry_run,
        "run_id": None if outcome.run_id is None else str(outcome.run_id),
        "generation": outcome.generation,
        "working_rows_deleted": outcome.working_rows_deleted,
        "deleted": outcome.deleted,
        "redacted": outcome.redacted,
        "retained": outcome.retained,
        "fail_closed_rewrites": outcome.fail_closed_rewrites,
        "replayed": outcome.replayed,
        "certificate_admissible": outcome.certificate_admissible,
        "certificate_object_key": certificate_key,
        "error_detail": outcome.error_detail,
    }
    emitter.narrate(
        f"deleted {outcome.deleted}, redacted {outcome.redacted}, retained {outcome.retained}"
    )
    if outcome.certificate_admissible and certificate_key is not None:
        emitter.narrate(f"certificate object key: {certificate_key}")
    if not outcome.completed:
        return emitter.fail(
            context.name,
            outcome.error_detail or f"the run ended {outcome.status}",
            ExitCode.OPERATIONAL,
        )
    return emitter.succeed(context.name, document)


def _report(context: VerbContext, progress: PhaseProgress) -> None:
    """One progress line per phase advance."""
    context.emitter.narrate(f"{progress.phase}: {progress.count}")


def _backup_settings(configuration: Configuration, *, skip_backup: bool) -> BackupSettings:
    """The backup settings, or unconsulted placeholders where the backup is skipped."""
    if not skip_backup:
        return BackupSettings.from_configuration(configuration)
    return BackupSettings(
        target=_UNUSED,
        ccloud_binary=configuration.text(_CCLOUD_KEY),
        cluster_id=_UNUSED,
        timeout_seconds=configuration.integer(_BACKUP_TIMEOUT_KEY),
    )


def _text_provider(configuration: Configuration) -> TextProvider | None:
    """The configured Text_Provider, or None so adjudication fails closed."""
    provider: TextProvider | None = None
    with suppress(Exception):
        provider = select_text_provider(configuration)
    return provider


def _embedding_provider(configuration: Configuration) -> EmbeddingProvider | None:
    """The configured Embedding_Provider, or None so a rewrite fails closed."""
    provider: EmbeddingProvider | None = None
    with suppress(Exception):
        provider = select_embedding_provider(configuration)
    return provider


def _certificate_key(run_id: UUID | None, configuration: Configuration) -> str | None:
    """The object key the certificate for this run is written under."""
    if run_id is None:
        return None
    return f"{configuration.text(_CERT_PREFIX_KEY)}{run_id}.json"
