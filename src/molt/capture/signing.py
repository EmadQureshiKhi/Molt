"""The credentials the capture side presents, and the Ingress_Signature it computes.

A bearer token authenticates a caller and resists no replay: a captured request
body carrying a valid token can be re-sent indefinitely, and every replay writes
Ledger rows indistinguishable from the originals. The signature closes that
window to the configured maximum request age (Requirement 47.14). Four claims
shape this module.

**The signed material is the presented timestamp followed by the exact body
bytes, with no separator.** The verifier recomputes over the raw bytes it
received, before any decode or parse, so the two sides agree only when neither
inserts anything the other does not (Requirement 47.2). The material is built
here by one function that both sides call, which is what makes the symmetry
structural rather than a convention two modules are asked to remember.

**A signature is computed at transmission and never held.** The spool holds Event
records rather than prepared requests, so a batch flushed after an outage is
signed with a fresh timestamp and lands inside the age bound however long the
outage lasted (Requirement 47.10). Nothing here stores a signature or a
timestamp; both are produced for one request and discarded with it.

**The capture side reads the secret from the environment alone.** The Collector
retrieves it from the parameter store at cold start, but a hook process holds no
parameter-store access and must not acquire one: an engineer machine has no
deployment credential, and importing a cloud client would cost the hook's
latency budget. The reader below therefore consults the environment-only key the
configuration surface declares and reports absence as absence, so the caller can
spool rather than transmit an unsigned request that would be refused.

**The value is wrapped before it leaves this module.** The secret is returned
inside the credential wrapper, whose text, representation, and format all yield
a fixed placeholder, so an accidental interpolation into a diagnostic line
discloses the placeholder rather than the key.

The bearer reader sits here for the same reason and under the same rule. The
Collector requires the bearer token in addition to the signature on the two
ingest endpoints and requires it alone on the recall endpoint (Requirements
47.11, 47.12), so both values are read on one path, from the environment alone,
and both are returned wrapped. Absence of either is a return value rather than an
error, because the capture side's answer to a missing credential is to spool and
exit 0 rather than to fail the agent.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from hashlib import sha256
from typing import Final

from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.models.event import format_timestamp

__all__ = [
    "AUTHORIZATION_HEADER",
    "BEARER_SCHEME",
    "COLLECTOR_BEARER_ENV",
    "DIGEST_NAME",
    "INGRESS_KEY_ENV",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "authorization",
    "bearer_token",
    "ingress_headers",
    "ingress_timestamp",
    "shared_secret",
    "sign_ingress",
    "signatures_match",
    "signing_material",
]

# The two request headers an ingest request presents. Both are read by the
# verifier before any body handling (Requirement 47.3), and both are named here
# so the signer and the verifier cannot spell them differently.
TIMESTAMP_HEADER: Final[str] = "X-Molt-Timestamp"
SIGNATURE_HEADER: Final[str] = "X-Molt-Signature"

# The digest the keyed hash is taken with (Requirement 47.2).
DIGEST_NAME: Final[str] = "sha256"

# The environment-only keys the operator's shell profile injects the two capture
# credentials into. Neither carries a configuration file key or a default, which
# is why the configuration surface describes both as secrets rather than settings.
INGRESS_KEY_ENV: Final[str] = "MOLT_INGRESS_SECRET"
COLLECTOR_BEARER_ENV: Final[str] = "MOLT_COLLECTOR_TOKEN"

# How the bearer token is presented. Spelled once so the capture side and the
# Collector cannot disagree about the scheme or the header name.
AUTHORIZATION_HEADER: Final[str] = "Authorization"
BEARER_SCHEME: Final[str] = "Bearer"


def ingress_timestamp(moment: datetime) -> str:
    """Render the instant a request is presented with, refusing a naive one.

    The rendering is the canonical timestamp form the Event wire form uses, so a
    request timestamp and an Event timestamp are the same shape and the age bound
    is compared against a value carrying a numeric offset.
    """
    return format_timestamp(moment)


def signing_material(timestamp: str, body: bytes) -> bytes:
    """The bytes the keyed hash is taken over: the timestamp, then the body.

    No separator is inserted. A separator would be safe only if both sides
    inserted the same one, and the way to guarantee that is to have neither side
    insert anything, which is what this does.
    """
    return timestamp.encode("utf-8") + body


def sign_ingress(body: bytes, secret: str, ts: str) -> str:
    """Compute the signature a request presents, as a lowercase hex digest.

    Args:
        body: The exact bytes the request will carry, already serialised.
        secret: The shared value the digest is keyed with.
        ts: The timestamp the request will present in its own header.

    Returns:
        The hex-encoded HMAC-SHA256 digest over the timestamp and the body.

    Raises:
        ValueError: The shared secret is empty, which would key the digest with
            nothing and produce a signature any holder of the body could forge.
    """
    if not secret:
        raise ValueError("an ingress signature cannot be keyed with an empty secret")
    return hmac.new(secret.encode("utf-8"), signing_material(ts, body), sha256).hexdigest()


def ingress_headers(body: bytes, credential: Credential, moment: datetime) -> dict[str, str]:
    """The two headers one ingest request presents, computed for that request.

    The credential is taken wrapped and revealed once, here, immediately before
    the digest is computed, so the value exists as text for the length of one
    call and reaches no other frame.
    """
    timestamp = ingress_timestamp(moment)
    return {
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign_ingress(body, credential.reveal(), timestamp),
    }


def signatures_match(presented: str, computed: str) -> bool:
    """Compare two signatures without leaking the correct prefix through timing.

    The comparison is constant time in the length of the shorter value
    (Requirement 47.9). It lives beside the signer so both sides of the boundary
    compare the same way.

    Both values are encoded before they are compared, which is what the bearer
    comparison does and for the same reason: the comparison over text admits
    ASCII alone and refuses anything else by raising, while a presented value
    arrives from a caller that may send whatever it likes. A presented value
    carrying anything outside the ASCII range is therefore answered here, as
    *not the computed one*, rather than escaping to a caller that asked a
    yes-or-no question. Nothing about the computed value is disclosed by
    answering: a lowercase hex digest is ASCII by construction, so a value that
    is not cannot be it whatever its prefix, and its length was never secret.
    """
    return hmac.compare_digest(presented.strip().encode("utf-8"), computed.encode("utf-8"))


def shared_secret(configuration: Configuration) -> Credential | None:
    """The shared secret the capture side signs with, or None when it is unset.

    Absence is a return value rather than an error because the caller's response
    to it is to spool and exit 0: an unsigned ingest request would be refused and
    the Events lost, whereas a spooled batch is transmitted once an operator sets
    the key. The recall path is unaffected, because recall is authenticated by
    the bearer token alone.
    """
    return _from_environment(configuration, INGRESS_KEY_ENV)


def bearer_token(configuration: Configuration) -> Credential | None:
    """The bearer value the capture side presents, or None when it is unset.

    Read from the environment alone for the same reason the shared secret is: a
    hook process holds no parameter-store access and acquiring one would cost the
    latency budget and require a deployment credential the machine does not have.
    Absence is a return value because the capture side spools rather than failing.
    """
    return _from_environment(configuration, COLLECTOR_BEARER_ENV)


def authorization(credential: Credential) -> dict[str, str]:
    """The one authorisation header a request presents, revealed for that request."""
    return {AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {credential.reveal()}"}


def _from_environment(configuration: Configuration, env: str) -> Credential | None:
    """Wrap an environment-injected secret, or report that it is unset."""
    injected = configuration.environment_value(env)
    if injected is None:
        return None
    return Credential(injected, source_name=env, source=CredentialSource.ENVIRONMENT)
