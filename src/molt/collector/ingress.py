"""Ingress_Signature verification: the other half of what the capture side signs.

A bearer token authenticates a caller and resists no replay. A captured request
body carrying a valid token can be re-sent indefinitely, and every replay writes
Ledger rows indistinguishable from the originals. This module closes that window
to the configured maximum request age (Requirement 47.14). It is the verifying
half of `molt.capture.signing`, and it is deliberately a plain function over a
header mapping and a byte string: it leases no connection, holds no state, and
reaches nothing, so the whole of it is exercisable without a cluster.

Six claims arrange it.

**The material is built by the signer's own function.** The digest is taken over
`signing_material(presented_timestamp, body)`, keyed with the shared value, under
`DIGEST_NAME`, and compared with `signatures_match`. All four come from the module
the capture side signs with, so the two sides agree because they call one
definition rather than because two modules were asked to remember a convention
(Requirements 47.2, 47.9). The body is the exact bytes received, taken before any
decode of the content, which is what the caller's ordering guarantees.

**Both headers are read before any body handling.** The timestamp header and the
signature header are read first, and an absent one is answered without the body
being touched at all (Requirement 47.3). Their two absences are separate causes
because an operator reading a rejection needs to know which header a caller is
failing to send.

**Every rejection is one exception and one measurement.** Each of the four causes
raises `IngressRejectedError` and emits `collector.signature_rejected`, and the
caller answers all four with the same status and the same body, so the response
distinguishes them no more than a constant-time comparison does (Requirements
47.4, 47.5, 47.7, 47.8, 47.13). The cause is named in the log record rather than
in the response and rather than in a metric dimension: the metric is undimensioned
for the same reason `collector.write_failure` is, so it adds one fixed billable
combination out of the ten the telemetry surface admits instead of four, and the
question *which cause* is answered by a record that is not billed. Nothing written
out names the computed digest, the presented digest, or the shared value.

**The age bound is an absolute difference and is inclusive on the accepted side.**
A timestamp as far in the future as the bound allows is as admissible as one that
far in the past, and one further out in either direction is refused: a request
whose age *exceeds* the configured maximum is rejected, so a difference of exactly
the maximum is accepted and anything beyond it is not (Requirements 47.5, 47.6).
That matches the inclusive convention the request-size bound already states, so
one reading of *maximum* covers both bounds.

**A present-but-unreadable timestamp is refused as an out-of-window timestamp.**
It is not the absent-header cause, because the header arrived. It is not the
mismatch cause either, and that distinction is load-bearing rather than
pedantic: the digest is taken over the timestamp as presented, so a value naming
no instant can still carry a signature that matches it perfectly, and calling
that a mismatch would report a comparison that did not happen. What such a value
fails is the bound: a request whose position on the timeline cannot be
established has not been shown to fall inside the window, so it is refused under
the cause that owns the window.

**The bound is measured against this process's reading, and that is a stated
deviation.** Requirement 47.5 names the cluster's current timestamp. The store
exposes no cluster-instant accessor, and adding one to this path would put a
network round trip and a leased connection in front of every ingest request,
including every forged one: an attacker spraying signatures it cannot compute
would each cost a cluster round trip, which is precisely the amplification that
verifying before the transaction opens exists to avoid. The reading is therefore
the host's, taken here, and the cost is that the window's accuracy depends on the
Collector host and the capture host being synchronised to within a fraction of the
configured maximum age rather than on one authority. Both hosts are
platform-synchronised and the bound is measured in minutes, so the margin is
wide. The reading is injectable, so a test drives the bound instead of waiting it
out.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, NoReturn

from molt.capture.signing import (
    DIGEST_NAME,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    signatures_match,
    signing_material,
)
from molt.errors import IngressRejectedError
from molt.models.event import parse_timestamp, require_aware
from molt.telemetry import Severity, log, metric

__all__ = [
    "COMPONENT",
    "SIGNATURE_REJECTED_METRIC",
    "RejectionCause",
    "verify_ingress",
]

# The component name every record from this module carries. Spelled here rather
# than imported from the handler, because the handler resolves this module by
# name precisely so the two stay separately replaceable; a unit test asserts the
# two spellings agree, which is the same arrangement the route paths use.
COMPONENT: Final[str] = "collector"

# The counter every rejection increments (Requirement 47.13). Undimensioned, so
# it costs one fixed billable combination however many causes it counts and
# however much traffic arrives.
SIGNATURE_REJECTED_METRIC: Final[str] = "collector.signature_rejected"


class RejectionCause(StrEnum):
    """The four causes Requirement 47.13 counts, told apart in the log record.

    They are named rather than described so an operator reads one vocabulary and
    a test asserts against a name rather than against a sentence. All four are one
    status and one response body to the caller.
    """

    TIMESTAMP_ABSENT = "timestamp_header_absent"
    SIGNATURE_ABSENT = "signature_header_absent"
    OUTSIDE_WINDOW = "timestamp_outside_window"
    MISMATCH = "signature_mismatch"


def verify_ingress(
    headers: Mapping[str, str],
    body: bytes,
    key: str,
    max_age_s: int,
    *,
    now: datetime | None = None,
) -> None:
    """Accept a signed, in-window request, or refuse it and say why.

    Returning is acceptance. The caller runs this after the request bound and the
    transport decode and before any record is read and before any transaction
    opens, which is what makes *nothing persisted* structural for every cause
    rather than something this function has to arrange (Requirement 47.4).

    The parameter carrying the shared value is named for its role in the digest
    rather than for what it is, so no call site of this function names a
    credential and no lint suppression is needed to say so. The caller passes the
    four leading arguments positionally.

    Args:
        headers: The request's headers. A case-insensitive mapping is expected and
            relied on: the transport may deliver header names in any case, and
            `molt.collector.routes.Headers` resolves that once for every caller,
            so nothing here lowercases a name of its own.
        body: The exact bytes the request carried, taken before any decode of the
            content, because that is what the signature covers.
        key: The shared value the digest is keyed with, revealed by the caller for
            the length of this call alone.
        max_age_s: The configured maximum request age in seconds. A difference of
            exactly this much is accepted.
        now: The reading the age bound is measured against, or None to take the
            host's. An injected reading must carry an offset, for the same reason
            a presented one must.

    Raises:
        IngressRejectedError: The timestamp header was absent, the signature
            header was absent, the presented timestamp fell outside the age
            bound or named no instant, or the presented signature did not match
            the computed one. Nothing was read from the body in the first two
            cases, and nothing is persisted in any of them.
    """
    presented_timestamp = headers.get(TIMESTAMP_HEADER)
    presented_signature = headers.get(SIGNATURE_HEADER)
    if presented_timestamp is None:
        _reject(RejectionCause.TIMESTAMP_ABSENT, "the request presented no timestamp header")
    if presented_signature is None:
        _reject(RejectionCause.SIGNATURE_ABSENT, "the request presented no signature header")
    if not key:
        raise IngressRejectedError(
            "no ingress shared value is available, so no signature could be verified "
            "and the request was refused"
        )

    _require_inside_window(presented_timestamp, max_age_s, _reading(now))
    computed = hmac.new(
        key.encode("utf-8"),
        signing_material(presented_timestamp, body),
        DIGEST_NAME,
    ).hexdigest()
    _require_match(presented_signature, computed, len(body))


def _require_inside_window(presented: str, max_age_s: int, reading: datetime) -> None:
    """Refuse a timestamp the age bound does not cover, or one naming no instant.

    The difference is taken in absolute value, so a timestamp too far ahead of the
    reading is as rejectable as one too far behind it: a caller whose clock runs
    fast is presenting a request that will still be replayable when the reading
    catches up, which is the window the bound exists to close.
    """
    try:
        moment = parse_timestamp(presented)
    except ValueError:
        _reject(
            RejectionCause.OUTSIDE_WINDOW,
            "the presented timestamp names no instant, so no age could be established",
            max_age_seconds=max_age_s,
        )
    age = abs((reading - moment).total_seconds())
    if age > float(max_age_s):
        _reject(
            RejectionCause.OUTSIDE_WINDOW,
            "the presented timestamp lies outside the configured maximum request age",
            max_age_seconds=max_age_s,
            presented_age_seconds=int(age),
        )


def _require_match(presented: str, computed: str, request_bytes: int) -> None:
    """Refuse a signature that is not the computed one, comparing in constant time.

    The comparison is the signer's own, which is constant time in the length of
    the shorter value, tolerates the surrounding space a transport may leave
    around a header value, and answers for a presented value carrying anything
    outside the ASCII range rather than raising on one (Requirement 47.9). No
    question is asked here ahead of it: a second reading of what a presented value
    is allowed to carry would be a second place that has to agree with the first,
    which is exactly what calling the signer's own definition exists to avoid.
    """
    if signatures_match(presented, computed):
        return
    _reject(
        RejectionCause.MISMATCH,
        "the presented signature is not the one computed over the presented timestamp",
        request_bytes=request_bytes,
    )


def _reading(now: datetime | None) -> datetime:
    """The reading the age bound is measured against, refusing a naive one.

    A reading without an offset has no defined position on the timeline, so a
    request could be judged stale or fresh according to where the two sides run.
    """
    if now is None:
        return datetime.now(UTC)
    return require_aware(now, "the current reading")


def _reject(cause: RejectionCause, detail: str, **fields: object) -> NoReturn:
    """Count one rejection, record which cause it was, and refuse the request.

    The measurement and the record are taken together so no path can refuse a
    request without counting it. The record names the cause and the arithmetic
    around it and names neither digest, so a rejection discloses to a reader of
    the logs exactly what it discloses to the caller: that the request was
    refused.
    """
    metric(SIGNATURE_REJECTED_METRIC)
    log(
        Severity.WARNING,
        COMPONENT,
        "an ingest request was refused before any record was read",
        cause=str(cause),
        detail=detail,
        **fields,
    )
    raise IngressRejectedError(detail)
