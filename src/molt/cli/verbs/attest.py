"""The attest verify verb: the whole verification algorithm, or one checkpoint.

A verification that ran to completion and concluded `failed` is a successful
verification with a negative answer, so it ends with its own status rather than
with the operational one. That distinction is the reason this verb exists as
something an operator can script around.

The public half of the signing key is retrieved through a key source, which is either
the saved file an auditor holds or the key service a deployment can reach. Either way
the check itself happens in this process, and the seam handed to the verification
refuses to sign: a verify verb holding a signing privilege would undermine the
independence the certificate claims. A surface that names no signing key at all is
reported as a missing component rather than as a certificate that failed to verify,
because an unprovisioned deployment and a bad signature are different findings and
must not share an exit status.
"""

from __future__ import annotations

from uuid import UUID

from molt.attest.checkpoint import CheckpointVerification, DigestSigner
from molt.attest.checkpoint import verify as verify_checkpoint
from molt.attest.keys import public_key_source
from molt.attest.verifier import (
    CertificateLocation,
    CertificateSettings,
    LocalSignatureChecker,
    VerificationReport,
    verify_source,
)
from molt.cli.context import READER_ROLE, VerbContext
from molt.cli.exits import ComponentUnavailableError, ExitCode, UsageError
from molt.errors import SigningUnavailableError
from molt.models.event import JsonObject

__all__ = ["verify"]


def verify(context: VerbContext) -> ExitCode:
    """Verify one certificate end to end, or one named checkpoint on its own."""
    certificate = context.text("certificate")
    object_key = context.text("s3_key")
    checkpoint = context.text("checkpoint")
    named = tuple(
        flag
        for flag, value in (
            ("--certificate", certificate),
            ("--s3-key", object_key),
            ("--checkpoint", checkpoint),
        )
        if value
    )
    if len(named) != 1:
        raise UsageError("name exactly one of --certificate, --s3-key, or --checkpoint")

    keys = _key_source(context)
    with context.store(role=READER_ROLE) as store:
        if checkpoint is not None:
            return _report_checkpoint(
                context, verify_checkpoint(store, _identifier(checkpoint), signer=keys)
            )
        location = (
            CertificateLocation.at_path(certificate)
            if certificate is not None
            else CertificateLocation.at_object_key(str(object_key))
        )
        report = verify_source(
            location,
            store=store,
            keys=keys,
            settings=CertificateSettings.from_configuration(context.configuration),
        )
    return _report_certificate(context, report)


def _report_certificate(context: VerbContext, report: VerificationReport) -> ExitCode:
    """Print each check's outcome and end with the status the outcome names."""
    emitter = context.emitter
    for check in report.failed_checks:
        emitter.narrate(f"failed: {check.check}")
    emitter.narrate(f"outcome: {report.outcome}")
    document: JsonObject = {
        "outcome": report.outcome,
        "verified": report.verified,
        "failed_checks": list(report.failed_check_names),
    }
    if report.verified:
        return emitter.succeed(context.name, document)
    emitter.emit(
        {
            "verb": context.name,
            "ok": False,
            "exit_code": int(ExitCode.VERIFICATION_FAILED),
            **document,
        }
    )
    return ExitCode.VERIFICATION_FAILED


def _report_checkpoint(context: VerbContext, outcome: CheckpointVerification) -> ExitCode:
    """Report the standalone checkpoint check, with the same two statuses."""
    emitter = context.emitter
    emitter.narrate(f"checkpoint {outcome.checkpoint_id} agrees: {outcome.agrees}")
    document: JsonObject = {
        "checkpoint_id": str(outcome.checkpoint_id),
        "agrees": outcome.agrees,
        "signature_verified": outcome.signature_verified,
        "changed_sessions": [str(session) for session in outcome.changed_sessions],
        "unaccounted_changes": len(outcome.unaccounted_changes),
    }
    if outcome.agrees:
        return emitter.succeed(context.name, document)
    emitter.emit(
        {
            "verb": context.name,
            "ok": False,
            "exit_code": int(ExitCode.VERIFICATION_FAILED),
            **document,
        }
    )
    return ExitCode.VERIFICATION_FAILED


def _identifier(raw: str) -> UUID:
    """One checkpoint identifier, refusing text that is not one."""
    try:
        return UUID(raw)
    except ValueError as exc:
        raise UsageError("--checkpoint names an identifier") from exc


def _key_source(context: VerbContext) -> DigestSigner:
    """The signing key's public half, wrapped in a seam that verifies and cannot sign.

    The shape is the signing seam rather than a read-only key source because the
    checkpoint check verifies through the same seam the signer holds, and a deployment
    has exactly one of them. What is supplied here is the *checking* half of it:
    retrieval plus a local check, with the signing call present and refusing. A verify
    verb that could sign would hold the privilege its independence rests on not
    holding.

    Where a saved public half is configured it answers, so a verification calls no key
    service at all. A surface naming no signing key is reported as a missing component
    rather than as a negative answer about the certificate.
    """
    try:
        return LocalSignatureChecker(keys=public_key_source(context.configuration))
    except SigningUnavailableError as error:
        raise ComponentUnavailableError(
            "attest verify",
            f"a provisioned certificate signing key: {error}",
        ) from error
