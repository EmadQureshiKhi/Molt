"""The console's own authentication: credential verification, sessions, and CSRF.

The console's function endpoint declares no request signing, so nothing outside
this module authenticates a console request. Four decisions follow from that, and
each is arranged so a caller cannot reach the value it protects by accident.

**The stored credential is a hash and the comparison is constant time.** The
Parameter_Store value is a `pbkdf2_sha256$iterations$salt$digest` record rather
than a password, so the parameter's disclosure does not disclose the credential,
and verification derives the same digest and compares it with
`hmac.compare_digest`. A malformed record is a refusal rather than a fallback to a
plain comparison: a deployment whose credential record cannot be read must let
nobody in rather than everybody.

**The session cookie is signed, not encrypted, and carries an absolute expiry.**
The cookie is `payload.signature`, the signature is HMAC-SHA256 over the payload
keyed with the session key, and the payload names the issue instant, the expiry
instant, the subject, and the session's CSRF token. Expiry is absolute: it is
checked against the payload rather than against the cookie's own `Max-Age`, so a
client that keeps the cookie past its expiry holds an expired session rather than
an eternal one, and there is no sliding renewal to extend one indefinitely.

**Cookie attributes are not a caller's choice.** `issue` is the only way to set
the cookie and it always sets `HttpOnly`, `Secure`, `SameSite=Strict`, and
`Path=/`, so no route can weaken them.

**Every mutation route requires the session's own CSRF token.** The token is
minted with the session and carried in the signed payload rather than stored, so
verifying it needs no server-side session table, and the comparison is constant
time like the others.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from molt.errors import MoltError

__all__ = [
    "COOKIE_NAME",
    "COOKIE_PATH",
    "CREDENTIAL_FIELD",
    "CSRF_FIELD",
    "DEFAULT_ITERATIONS",
    "DEFAULT_SESSION_LIFETIME",
    "HASH_SCHEME",
    "OPERATOR_SUBJECT",
    "CredentialRecordError",
    "Session",
    "cookie_attributes",
    "credential_record",
    "csrf_accepted",
    "issue",
    "mint_session_key",
    "verify_cookie",
    "verify_credential",
]

# The cookie the session travels in, and the path it is scoped to.
COOKIE_NAME: Final[str] = "molt_session"
COOKIE_PATH: Final[str] = "/"

# The form field names the login form and every mutation form submit.
CREDENTIAL_FIELD: Final[str] = "credential"
CSRF_FIELD: Final[str] = "csrf_token"

# There is one operator credential set and no user management, so every session
# names the same subject. Recording it in the payload keeps the payload shape
# stable if a second subject is ever introduced.
OPERATOR_SUBJECT: Final[str] = "operator"

# How long a session lasts from issue, absolutely. Short enough that a leaked
# cookie expires within a working period and long enough that an operator running
# an erasure is not signed out mid-run.
DEFAULT_SESSION_LIFETIME: Final[timedelta] = timedelta(hours=8)

# The credential record's scheme and its default work factor. The scheme name is
# part of the record, so a record written under a scheme this module does not know
# is refused by name rather than mis-verified.
HASH_SCHEME: Final[str] = "pbkdf2_sha256"
DEFAULT_ITERATIONS: Final[int] = 600_000

# The number of random bytes a salt, a session key, and a CSRF token each carry.
_SALT_BYTES: Final[int] = 16
_SESSION_KEY_BYTES: Final[int] = 32
_CSRF_BYTES: Final[int] = 32

# The field separators of the credential record and of the cookie. The cookie
# separator is absent from the base64url alphabet, so a payload cannot contain one.
_RECORD_SEPARATOR: Final[str] = "$"
_COOKIE_SEPARATOR: Final[str] = "."
_PAYLOAD_SEPARATOR: Final[str] = ":"

_MINIMUM_ITERATIONS: Final[int] = 1
_PAYLOAD_FIELDS: Final[int] = 4


class CredentialRecordError(MoltError):
    """The stored credential record could not be read, so nobody is let in."""


@dataclass(frozen=True, slots=True)
class Session:
    """One authenticated session as the signed cookie carries it."""

    subject: str
    issued_at: datetime
    expires_at: datetime
    csrf_token: str

    def expired_at(self, now: datetime) -> bool:
        """Whether this session's absolute expiry has passed at an instant."""
        return now >= self.expires_at


def _b64(raw: bytes) -> str:
    """Base64url without padding, which is what the cookie and record carry."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    """Read base64url without padding, refusing anything that is not that."""
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as error:
        raise CredentialRecordError("a base64url field could not be read") from error


def credential_record(credential: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Derive the record a deployment writes to Parameter_Store for a credential.

    Provisioning and the tests both go through this, so the record the console
    verifies against is always one this module can read.
    """
    if not credential:
        raise CredentialRecordError("an empty credential has no record")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", credential.encode("utf-8"), salt, iterations)
    return _RECORD_SEPARATOR.join((HASH_SCHEME, str(iterations), _b64(salt), _b64(digest)))


def verify_credential(presented: str, record: str) -> bool:
    """Whether the presented credential matches the stored record, in constant time.

    A record whose scheme, work factor, or field count this module cannot read
    raises rather than returning False, because an unreadable record is a
    deployment fault an operator must see rather than a wrong password to retry.
    """
    parts = record.strip().split(_RECORD_SEPARATOR)
    if len(parts) != _PAYLOAD_FIELDS:
        raise CredentialRecordError("the credential record does not carry four fields")
    scheme, iteration_text, salt_text, digest_text = parts
    if scheme != HASH_SCHEME:
        raise CredentialRecordError(f"the credential record names an unknown scheme {scheme!r}")
    try:
        iterations = int(iteration_text, 10)
    except ValueError as error:
        raise CredentialRecordError("the credential record's work factor is not a count") from error
    if iterations < _MINIMUM_ITERATIONS:
        raise CredentialRecordError("the credential record's work factor is not positive")
    expected = _unb64(digest_text)
    derived = hashlib.pbkdf2_hmac(
        "sha256", presented.encode("utf-8"), _unb64(salt_text), iterations
    )
    return hmac.compare_digest(derived, expected)


def mint_session_key() -> str:
    """A fresh session-signing key, for provisioning and for a local run."""
    return _b64(secrets.token_bytes(_SESSION_KEY_BYTES))


def _payload_of(session: Session) -> str:
    """The cookie payload one session becomes, as one base64url field."""
    body = _PAYLOAD_SEPARATOR.join(
        (
            session.subject,
            str(int(session.issued_at.timestamp())),
            str(int(session.expires_at.timestamp())),
            session.csrf_token,
        )
    )
    return _b64(body.encode("utf-8"))


def _signature_of(payload: str, key: str) -> str:
    """The HMAC-SHA256 signature over one payload, keyed with the session key."""
    mac = hmac.new(key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256)
    return _b64(mac.digest())


def issue(
    key: str,
    *,
    now: datetime,
    lifetime: timedelta = DEFAULT_SESSION_LIFETIME,
    subject: str = OPERATOR_SUBJECT,
) -> tuple[Session, str]:
    """Mint a session and its signed cookie value, with an absolute expiry."""
    if lifetime <= timedelta(0):
        raise CredentialRecordError("a session lifetime must be positive")
    session = Session(
        subject=subject,
        issued_at=now.astimezone(UTC),
        expires_at=(now + lifetime).astimezone(UTC),
        csrf_token=_b64(secrets.token_bytes(_CSRF_BYTES)),
    )
    payload = _payload_of(session)
    return session, f"{payload}{_COOKIE_SEPARATOR}{_signature_of(payload, key)}"


def verify_cookie(value: str | None, key: str, *, now: datetime) -> Session | None:
    """The session a cookie names, or None if it names none this key issued.

    Every failure is the same answer: an absent cookie, a malformed one, one signed
    with another key, one whose payload was edited, and one whose absolute expiry
    has passed are all *no session*, so the response distinguishes them no more
    than the comparison does.
    """
    if not value:
        return None
    payload, separator, signature = value.partition(_COOKIE_SEPARATOR)
    if not separator or not payload or not signature:
        return None
    if not hmac.compare_digest(signature, _signature_of(payload, key)):
        return None
    try:
        fields = _unb64(payload).decode("utf-8").split(_PAYLOAD_SEPARATOR)
    except (CredentialRecordError, UnicodeDecodeError):
        return None
    if len(fields) != _PAYLOAD_FIELDS:
        return None
    subject, issued_text, expires_text, csrf = fields
    try:
        issued_at = datetime.fromtimestamp(int(issued_text, 10), tz=UTC)
        expires_at = datetime.fromtimestamp(int(expires_text, 10), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    session = Session(subject=subject, issued_at=issued_at, expires_at=expires_at, csrf_token=csrf)
    if session.expired_at(now.astimezone(UTC)) or not csrf or not subject:
        return None
    return session


def csrf_accepted(session: Session, submitted: str | None) -> bool:
    """Whether a mutation carries this session's own CSRF token, in constant time."""
    if not submitted:
        return False
    return hmac.compare_digest(submitted, session.csrf_token)


def cookie_attributes() -> dict[str, object]:
    """The attributes every session cookie is set with, so no route may weaken them."""
    return {
        "path": COOKIE_PATH,
        "httponly": True,
        "secure": True,
        "samesite": "strict",
    }
