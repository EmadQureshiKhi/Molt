"""The certificate display and the live verification trigger.

Two routes the table already declares, claimed by name: `GET /certificates/{run_id}`
and `POST /certificates/{run_id}/verify`.

**The display renders the stored document rather than rebuilding it.** The signed
payload is read from the `erasure_certificate` row and rendered as it was signed, so
what the page shows is what a verifier checks. Three fields the schema gained after
the first certificate shape are surfaced deliberately, because a field that is stored
and never displayed is a field nobody notices is wrong: the ownership block's
Fencing_Generation, the per-Disposition first-attribution moment and method, and the
named Ledger_Checkpoint.

**Verification is live, read-only, and reuses the Certificate_Verifier whole.** The
handler builds the envelope from the stored row and calls `verify_certificate`, which
performs the same checks the `attest verify` verb performs. Nothing here re-implements
a check and nothing here writes.

**An absent key service is an operational failure, never a verification outcome.**
The verifier needs the public half of the signing key through a `PublicKeySource`,
resolved here through `molt.attest.keys.public_key_source` against the console's own
configuration surface: the saved public half where one is provisioned, and the key
service otherwise. A surface naming no signing key at all resolves nothing, and
reporting that as *failed* would libel a valid certificate — so the page reports the
missing component with its own status and states plainly that no verification was
attempted. The distinction is the same one the `attest verify` verb draws between an
operational failure and a verification whose answer was negative.

**The verification reads through the console's read-only handle.** The verifier refuses
a connection whose configured role is not the read-only one, so that its no-mutation
claim rests on a privilege rather than on care. The console function holds the eraser
role, because the erasure console runs erasures from it, and a verification offered
that handle would be declined before reading anything — reported, correctly but
uselessly, as unattempted in every deployment. So both routes here read through
`Console.read_only_store()`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, cast
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.templating import Jinja2Templates

from molt.attest.canonical import CanonicalValue
from molt.attest.keys import public_key_source
from molt.attest.verifier import (
    Envelope,
    PublicKeySource,
    VerificationReport,
    verify_certificate,
)
from molt.console import routing
from molt.console.app import console_of, session_of
from molt.console.deps import COMPONENT, Console
from molt.console.routes.erasure_common import configuration_of
from molt.errors import MoltError, SigningUnavailableError, VerificationFailedError
from molt.store import Cursor
from molt.telemetry import Severity, log

__all__ = [
    "CERTIFICATE_QUERY",
    "KEY_SERVICE_COMPONENT",
    "CertificateRow",
    "KeyServiceUnavailableError",
    "certificate",
    "certificate_verify",
    "envelope_of",
    "read_certificate",
]

# The stored certificate of one run. The payload is the signed document itself, so
# the page renders what a verifier reads rather than a second rendering of the run.
CERTIFICATE_QUERY: Final[str] = (
    "SELECT id, payload, canonical_digest, signature, kms_key_id, signing_algorithm, "
    "s3_bucket, s3_key, s3_version_id, storage_status, storage_detail, created_at "
    "FROM erasure_certificate WHERE run_id = %s"
)

# What the console reports as missing when a verification cannot be attempted. Named
# as a value so a test asserts the name rather than a sentence.
KEY_SERVICE_COMPONENT: Final[str] = (
    "a key service client for the public half of the certificate signing key"
)

_NOT_FOUND: Final[dict[str, str]] = {"error": "no certificate for this run"}
_NOT_FOUND_STATUS: Final[int] = 404
_UNAVAILABLE_STATUS: Final[int] = 503
_CERTIFICATE_WIDTH: Final[int] = 12

# The three vocabulary values the verification block carries. `unattempted` is the
# one that matters: it is neither `verified` nor `failed`, so a reader cannot mistake
# a missing component for a negative answer about the certificate.
OUTCOME_UNATTEMPTED: Final[str] = "unattempted"


class KeyServiceUnavailableError(MoltError):
    """The key service this verification needs is not present in this build.

    Raised rather than returning a negative report, because a caller that could not
    tell an absent key client from a bad signature would have to treat a deployment
    gap as evidence against the certificate.
    """

    def __init__(self, component: str) -> None:
        self.component = component
        super().__init__(f"a live verification needs {component}, which this build lacks")


class CertificateRow:
    """One stored certificate, narrowed to what the page renders."""

    __slots__ = ("fields",)

    def __init__(self, fields: dict[str, object]) -> None:
        self.fields = fields

    @property
    def payload(self) -> Mapping[str, CanonicalValue]:
        """The signed payload as the mapping it was signed as."""
        return cast("Mapping[str, CanonicalValue]", self.fields["payload"])

    @property
    def signed(self) -> bool:
        """Whether the row carries a signature at all."""
        return self.fields["signature"] is not None


def read_certificate(cursor: Cursor, run_id: UUID) -> CertificateRow | None:
    """The certificate of one run, or None when no certificate was issued for it."""
    cursor.execute(CERTIFICATE_QUERY, (run_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    if len(row) != _CERTIFICATE_WIDTH:
        raise ValueError("the certificate projection returned an unexpected column count")
    return CertificateRow(
        {
            "certificate_id": str(row[0]),
            "payload": _payload_of(row[1]),
            "canonical_digest": row[2],
            "signature": row[3],
            "kms_key_id": row[4],
            "signing_algorithm": row[5],
            "s3_bucket": row[6],
            "s3_key": row[7],
            "s3_version_id": row[8],
            "storage_status": row[9],
            "storage_detail": row[10],
            "created_at": row[11],
        }
    )


def _payload_of(value: object) -> Mapping[str, object]:
    """The payload column as a mapping, whether the driver decoded it or not."""
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    if isinstance(value, str | bytes):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return cast("Mapping[str, object]", decoded)
    raise ValueError("the stored certificate payload is not an object")


def envelope_of(row: CertificateRow) -> Envelope:
    """Rebuild the signed envelope from the stored row, for the verifier to check.

    Nothing is recomputed here: the payload, the digest, the key, the algorithm, and
    the signature are all the stored ones, so the check the verifier performs is a
    check of the document as issued.
    """
    signature = row.fields["signature"]
    if signature is None:
        raise VerificationFailedError(
            "the stored certificate carries no signature, so there is nothing to verify"
        )
    return Envelope(
        payload=row.payload,
        algorithm=str(row.fields["signing_algorithm"] or ""),
        kms_key_id=str(row.fields["kms_key_id"] or ""),
        payload_digest=str(row.fields["canonical_digest"]),
        signature=bytes(cast(bytes, signature)),
    )


@routing.register("certificate")
async def certificate(request: Request) -> Response:
    """Render one run's certificate, with no verification attempted yet.

    The display's own status is success: the page is the stored document, and the fact
    that nobody has verified it on this page yet is a statement in the page rather
    than a status of the request.
    """
    return await _render(request, verification=_unattempted_block(None), status=200)


@routing.register("certificate_verify")
async def certificate_verify(request: Request) -> Response:
    """Verify the stored certificate live and display the outcome (Requirement 25.7).

    The route mutates nothing: it is classified as a mutation only because it is a
    form submission carrying a CSRF token, which is why demonstration mode permits it
    while blocking the two routes that do write. The verifier itself refuses any
    connection that is not the read-only role, so this page is served through the
    console's read-only handle rather than the eraser handle the function holds — on
    the eraser handle the verifier would refuse, and the page would report the
    verification as unattempted in every deployment.
    """
    return await _render(request, verification=None)


async def _render(
    request: Request,
    *,
    verification: dict[str, object] | None,
    status: int | None = None,
) -> Response:
    """Read the certificate, optionally verify it, and render the one page.

    Both routes render the same template, so the display and the outcome are one page
    rather than two that could describe the same certificate differently.
    """
    identifier = _identifier(request)
    if identifier is None:
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)
    console = console_of(request)
    templates = _templates(request)
    if templates is None:
        return JSONResponse({"error": "unavailable"}, status_code=_UNAVAILABLE_STATUS)

    row = console.read_only_store().read(lambda cursor: read_certificate(cursor, identifier))
    if row is None:
        log(Severity.INFO, COMPONENT, "a certificate was requested for a run holding none")
        return JSONResponse(dict(_NOT_FOUND), status_code=_NOT_FOUND_STATUS)

    block = verification if verification is not None else _verified_block(console, row, request)
    # A verification that ran carries its own answer, whichever answer that is, and
    # the request succeeded. A verification that could not run at all is this
    # deployment's failure to provide a component, which is what the unavailable
    # status says; a `failed` status here would attribute that to the certificate.
    served = (
        status
        if status is not None
        else (_UNAVAILABLE_STATUS if block["outcome"] == OUTCOME_UNATTEMPTED else 200)
    )
    return templates.TemplateResponse(
        request,
        "certificate.html",
        _page(console, request)
        | {
            "title": "Erasure certificate",
            "run_id": str(identifier),
            "certificate": row.fields,
            "payload": row.payload,
            "signed": row.signed,
            "verification": block,
        },
        status_code=served,
    )


def _verified_block(console: Console, row: CertificateRow, request: Request) -> dict[str, object]:
    """Run the verification, or report the component that stopped it from running."""
    try:
        keys = _key_source(request)
    except KeyServiceUnavailableError as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "a live verification was requested and the key service is absent",
            component=error.component,
        )
        return _unattempted_block(error.component)
    try:
        report = verify_certificate(
            envelope_of(row), store=console.read_only_store(), keys=keys, now=console.now()
        )
    except VerificationFailedError as error:
        log(
            Severity.WARNING,
            COMPONENT,
            "a live verification could not be carried out",
            error_type=type(error).__name__,
        )
        return _unattempted_block(None)
    return _report_block(report)


def _report_block(report: VerificationReport) -> dict[str, object]:
    """One completed verification as the values the template renders."""
    return {
        "attempted": True,
        "outcome": report.outcome,
        "verified": report.verified,
        "failed_checks": [
            {"check": entry.check, "subject": entry.subject} for entry in report.failed_checks
        ],
        "notes": [{"name": entry.name, "detail": entry.detail} for entry in report.notes],
        "missing_component": None,
    }


def _unattempted_block(component: str | None) -> dict[str, object]:
    """The block that says no verification ran, which is not a negative answer.

    `verified` is absent rather than false. A false there would read as a claim about
    the certificate, and the claim being made is about this deployment.
    """
    return {
        "attempted": False,
        "outcome": OUTCOME_UNATTEMPTED,
        "verified": None,
        "failed_checks": [],
        "notes": [],
        "missing_component": component,
    }


def _key_source(request: Request) -> PublicKeySource:
    """The public half of the signing key, resolved from the configuration surface.

    The saved file answers where one is configured and the key service answers
    otherwise, which is the resolution `public_key_source` states. A surface naming no
    signing key at all is still reported as a missing component rather than as a
    verification outcome: a deployment that was never provisioned says nothing about
    whether a certificate is valid.
    """
    try:
        return public_key_source(configuration_of(request))
    except SigningUnavailableError as error:
        raise KeyServiceUnavailableError(KEY_SERVICE_COMPONENT) from error


def _identifier(request: Request) -> UUID | None:
    """The run identifier from the path, or None when the text is not one."""
    raw = request.path_params.get("run_id")
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError):
        return None


def _page(console: Console, request: Request) -> dict[str, object]:
    """The layout's own context: the mode banner, the session, and the CSRF token."""
    session = session_of(request)
    return {
        "demo_mode": console.demo_mode,
        "authenticated": session is not None,
        "csrf_token": "" if session is None else session.csrf_token,
    }


def _templates(request: Request) -> Jinja2Templates | None:
    """The template environment the application resolved, or None when absent."""
    return cast("Jinja2Templates | None", getattr(request.app.state, "molt_templates", None))
