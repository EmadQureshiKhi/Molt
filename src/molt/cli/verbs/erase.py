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

from molt.attest.builder import CertificatePolicy, IssuedCertificate, issue
from molt.attest.keys import signer_from_configuration
from molt.attest.objects import S3CertificateStore
from molt.backup import BackupSettings
from molt.cli.context import AGENT_CLI, VerbContext, client_id_for, machine_identifier
from molt.cli.exits import ExitCode, UsageError
from molt.cli.verbs.common import BATCH_SIZE_KEY, integer_overrides, threshold_overrides
from molt.config.resolve import Configuration
from molt.erase.engine import (
    EngineSeams,
    ErasureRequest,
    PhaseProgress,
    RunOutcome,
    run_erasure,
)
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
        # Issued here, inside the store's own block, because the certificate is read from
        # the evidence the run has just committed and the connection that committed it is
        # the one to read it back on.
        issued = _issue(store, outcome, configuration)

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
        "certificate_object_key": certificate_key if issued is None else issued.object_key,
        "certificate_id": None if issued is None else str(issued.certificate_id),
        "certificate_digest": None if issued is None else issued.signed.payload_digest,
        "certificate_storage_status": None if issued is None else issued.storage_status,
        "certificate_storage_detail": None if issued is None else issued.storage_detail,
        "error_detail": outcome.error_detail,
    }
    emitter.narrate(
        f"deleted {outcome.deleted}, redacted {outcome.redacted}, retained {outcome.retained}"
    )
    if issued is not None:
        emitter.narrate(f"certificate {issued.certificate_id} signed, digest recorded")
        emitter.narrate(
            f"certificate object {issued.bucket}/{issued.object_key}: {issued.storage_status}"
        )
    elif outcome.certificate_admissible and certificate_key is not None:
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
    """The object key the certificate for this run is written under.

    This is what the verb reports when no certificate was issued, which is a dry run or a
    run that did not reach a state one may be assembled from. It is the key the certificate
    *would* take rather than one that exists, so it is only ever reported alongside the
    admissibility flag that says whether anything will be written there.
    """
    if run_id is None:
        return None
    return f"{configuration.text(_CERT_PREFIX_KEY)}{run_id}.json"


def _issue(
    store: MemoryStore,
    outcome: RunOutcome,
    configuration: Configuration,
) -> IssuedCertificate | None:
    """Assemble, sign, and store the certificate for a run that earned one.

    The engine records a completion and deliberately assembles no certificate, because a
    certificate is built from the evidence a run has committed rather than from the run in
    progress. Nothing then built one: the verb reported the object key a certificate would
    have taken and returned success, so every completed run in a deployment ended with its
    evidence in the cluster, no signed document anywhere, and an exit code saying it had
    gone well. This is where that is closed.

    None is returned for a run no certificate may be assembled from, which is the dry run
    and any run that did not complete. Both are the surface's own judgement, read off the
    outcome rather than decided here.

    A failure to sign or to persist propagates. It is not softened into a warning, because
    the certificate is the deliverable of a governed erasure: a run that deleted a tenant's
    memory and produced no attestation of having done so is a run an operator has to know
    about, and an exit code is how they find out. A failure to write the *object* does not
    propagate and is not meant to — the signed document is already in the cluster, the row
    records that the object write did not complete, and the storage status is reported.
    """
    if outcome.run_id is None or outcome.dry_run or not outcome.certificate_admissible:
        return None
    return issue(
        store,
        outcome.run_id,
        signer=signer_from_configuration(configuration),
        object_store=S3CertificateStore(),
        policy=CertificatePolicy.from_configuration(configuration),
    )
