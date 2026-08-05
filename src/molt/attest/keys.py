"""The key service client: signing through it, and verifying without it.

Two documents are signed asymmetrically — the erasure certificate and the
Ledger_Checkpoint — and both reach the key service through one shape, `DigestSigner`.
Until now that shape had no implementation at all, so a deployment could assemble a
certificate and never sign one, and a reviewer could hold a certificate and never
check it. This module is that implementation, and the way it is split is the point.

**Signing needs the service. Verifying does not.** The signing privilege belongs to
one role and is exercised by one call; the check is elliptic-curve arithmetic over a
public value. So `KmsSigner` asks the service to sign and asks it for the public half,
while every verification path — this module's own included — performs the check in
process through the verifier's `verify_signature`. A reviewer who never held
permission to call the key service verifies exactly as well as the issuer does, which
is the whole basis of the independence a certificate claims.

**The public half is a public value, so an auditor may hold it as a file.**
`StoredPublicKey` reads the encoded key from a local path and satisfies the same
`PublicKeySource` the verifier consumes. That is not a convenience: an auditor
verifying a departing tenant's erasure has no account on the erasing party's cloud,
and a verification that required one would be a verification the erasing party could
withhold. The two sources are interchangeable at the seam, and which one a run used is
reported rather than inferred.

**The public half is cached per process and the signature is not.** A key's public
half does not change for the life of the key identifier, and a certificate covering a
thousand dispositions is verified with one retrieval rather than one per check. A
signature is a fresh call every time, because each one is over different bytes.

**Nothing here widens what a verifier can do.** `StoredPublicKey` and `KmsKeys` both
refuse to sign — they have no signing call at all — so the only object in this module
that can produce a signature is the one built from a signing configuration. A
verifier holding a signer would be a verifier holding the privilege its independence
rests on not holding.

**A missing key configuration is refused by name rather than defaulted.** There is no
plausible default for a signing key: a key identifier absent from the surface means a
deployment was not provisioned, and inventing one would make an unsigned document look
signed. Both factories raise, naming the key they read.

Every value the service is called with is a bound field of the request rather than
interpolated text, the library import happens inside the factory so this module stays
importable with no credential resolution, and the digest is sent as a digest rather
than as a message so the service never receives the document itself.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Final, Protocol, cast

from molt.attest.verifier import (
    ALGORITHM_KEY,
    KEY_ID_KEY,
    SUPPORTED_ALGORITHM,
    PublicKeySource,
    verify_signature,
)
from molt.config.resolve import ConfigError, Configuration, load_configuration
from molt.errors import SigningUnavailable, SigningUnavailableError
from molt.telemetry import Severity, log

__all__ = [
    "COMPONENT",
    "DIGEST_MESSAGE_TYPE",
    "PUBLIC_KEY_PATH_KEY",
    "SERVICE_NAME",
    "KeyServiceClient",
    "KmsKeys",
    "KmsSigner",
    "StoredPublicKey",
    "key_service_client",
    "public_key_source",
    "signer_from_configuration",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "key_service"

# The service this module calls, and the message type that tells it the bytes it was
# given are already a digest. Sending the document itself would put the memory content
# a certificate describes into a request to a third service, which is the one thing
# the signing path must not do.
SERVICE_NAME: Final[str] = "kms"
DIGEST_MESSAGE_TYPE: Final[str] = "DIGEST"

# Where an auditor's saved public half is read from. It is a path rather than a
# parameter because the reader of it has no account on the signing party's cloud, which
# is the situation the offline source exists for.
PUBLIC_KEY_PATH_KEY: Final[str] = "MOLT_CERTIFICATE_PUBLIC_KEY_PATH"

# The request and response fields, fixed by the service. Named here so no call site
# spells one, and so a response of an unexpected shape is refused by field name.
_REQUEST_KEY_ID: Final[str] = "KeyId"
_REQUEST_MESSAGE: Final[str] = "Message"
_REQUEST_MESSAGE_TYPE: Final[str] = "MessageType"
_REQUEST_ALGORITHM: Final[str] = "SigningAlgorithm"
_RESPONSE_SIGNATURE: Final[str] = "Signature"
_RESPONSE_PUBLIC_KEY: Final[str] = "PublicKey"


class KeyServiceClient(Protocol):
    """The two calls this module makes on a key service.

    Declared as a shape rather than as a library type, so the signing path depends on
    an interface a test can satisfy with a local key pair and the cloud import stays
    inside the factory below.
    """

    def sign(self, **request: object) -> object:
        """Sign a digest under a named key, returning the signature."""

    def get_public_key(self, **request: object) -> object:
        """Retrieve the public half of a named key."""


def key_service_client() -> KeyServiceClient:
    """Build a real key service client, importing the cloud library at call time.

    The import is here rather than at module scope for the reason the metrics client
    gives: the module must be importable, and the credential-free suites runnable, with
    no library resolution and no credential chain at import time. Region and
    credentials resolve through the library's own chain, so nothing about either is
    restated here.
    """
    module = import_module("boto3")
    return cast(KeyServiceClient, module.client(SERVICE_NAME))


def _field(response: object, name: str) -> bytes:
    """One bytes field of a service response, refused by name when it is absent.

    A response missing the field is a fault rather than an empty answer: a signing
    call that returned no signature has not signed, and treating an absent field as
    empty bytes would produce a document carrying a signature nothing can verify.
    """
    if not isinstance(response, dict):
        raise SigningUnavailable(
            f"the key service answered with {type(response).__name__} where a response was read"
        )
    carried = response.get(name)
    if isinstance(carried, bytes | bytearray):
        return bytes(carried)
    if carried is None:
        raise SigningUnavailable(f"the key service response carried no {name} field")
    raise SigningUnavailable(
        f"the key service response carried {name} as {type(carried).__name__} rather than bytes"
    )


# ---------------------------------------------------------------------------
# Retrieval, cached, in the two forms a reader may hold the key
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class KmsKeys:
    """Retrieval of a public half from the key service, cached for this process.

    Satisfies `PublicKeySource` and nothing wider: there is no signing call here, so an
    object of this class cannot be mistaken for one that can sign. The cache is keyed
    on the identifier because a key's public half is fixed for the life of that
    identifier, and a certificate covering many dispositions is checked against one
    retrieval rather than one per check.
    """

    client_factory: Callable[[], KeyServiceClient] = key_service_client
    _cached: dict[str, bytes] = field(default_factory=dict)
    _client: list[KeyServiceClient] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def public_key(self, *, key_id: str) -> bytes:
        """The public half of the named key, retrieved once and then remembered."""
        if not key_id:
            raise SigningUnavailable("a public half was asked for under no key identifier")
        with self._lock:
            held = self._cached.get(key_id)
            if held is not None:
                return held
        retrieved = _field(
            self._resolved().get_public_key(**{_REQUEST_KEY_ID: key_id}),
            _RESPONSE_PUBLIC_KEY,
        )
        with self._lock:
            self._cached[key_id] = retrieved
        log(
            Severity.INFO,
            COMPONENT,
            "retrieved the public half of the signing key",
            key_id=key_id,
            source=SERVICE_NAME,
        )
        return retrieved

    def _resolved(self) -> KeyServiceClient:
        """The client this process uses, built once on first use."""
        with self._lock:
            if not self._client:
                self._client.append(self.client_factory())
            return self._client[0]


@dataclass(frozen=True, slots=True)
class StoredPublicKey:
    """The public half as an auditor holds it: an encoded key in a local file.

    This is the source that makes a verification independent of the party that
    performed the erasure. It satisfies the same seam the service-backed source does
    and reaches no network at all, so a reviewer with the file and the certificate can
    check the signature with no account anywhere.

    The identifier is checked rather than ignored. A file holding one key cannot answer
    for a certificate signed under another, and answering anyway would verify the wrong
    document against the wrong key and report agreement.
    """

    path: Path
    key_id: str

    def public_key(self, *, key_id: str) -> bytes:
        """The stored public half, refusing an identifier this file does not hold."""
        if key_id != self.key_id:
            raise SigningUnavailable(
                f"the stored public half is for {self.key_id!r} and the document names {key_id!r}"
            )
        try:
            return self.path.read_bytes()
        except OSError as error:
            raise SigningUnavailable(
                "the stored public half could not be read from the configured path"
            ) from error


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class KmsSigner:
    """The full signing seam: sign through the service, verify in this process.

    `verify_digest` deliberately does not ask the service whether a signature is good.
    The service can answer that question, and using it would make every verification a
    call an operator can be denied; the local check is the same arithmetic over the
    same public value, and it is what the certificate's independence claim rests on.
    """

    keys: KmsKeys = field(default_factory=KmsKeys)

    def sign_digest(self, digest: bytes, *, key_id: str, algorithm: str) -> bytes:
        """Sign an already-computed digest under the named key.

        The digest is sent as a digest, so the service receives no part of the document
        it is signing for. An algorithm this build does not verify is refused before the
        call, because a signature nothing here can check is worse than no signature.
        """
        if algorithm != SUPPORTED_ALGORITHM:
            raise SigningUnavailable(
                f"the signing algorithm {algorithm!r} is not the one this build verifies"
            )
        if not key_id:
            raise SigningUnavailable("a signature was asked for under no key identifier")
        signature = _field(
            self.keys._resolved().sign(
                **{
                    _REQUEST_KEY_ID: key_id,
                    _REQUEST_MESSAGE: digest,
                    _REQUEST_MESSAGE_TYPE: DIGEST_MESSAGE_TYPE,
                    _REQUEST_ALGORITHM: algorithm,
                }
            ),
            _RESPONSE_SIGNATURE,
        )
        log(
            Severity.INFO,
            COMPONENT,
            "signed a document digest with the configured asymmetric key",
            key_id=key_id,
            algorithm=algorithm,
        )
        return signature

    def public_key(self, *, key_id: str) -> bytes:
        """The public half of the named key, through the cached retrieval."""
        return self.keys.public_key(key_id=key_id)

    def verify_digest(
        self,
        digest: bytes,
        signature: bytes,
        *,
        key_id: str,
        algorithm: str,
        public_key: bytes,
    ) -> bool:
        """Check a signature locally against the retrieved public half."""
        del key_id
        return verify_signature(digest, signature, public_key=public_key, algorithm=algorithm)


# ---------------------------------------------------------------------------
# Resolution from the configuration surface
# ---------------------------------------------------------------------------


def _configured_key_id(configuration: Configuration) -> str:
    """The signing key identifier, refused by name when the surface declares none.

    The key carries no default, so an unprovisioned surface refuses the read rather than
    answering emptily. Both that refusal and an empty value are reported as the same
    condition — no signing key is configured — because the answer a caller gives for
    either is *this deployment cannot verify* and never *this certificate is invalid*.
    """
    try:
        key_id = configuration.text(KEY_ID_KEY).strip()
    except ConfigError as error:
        raise SigningUnavailable(
            f"no signing key is configured, so {KEY_ID_KEY} names the value to provision"
        ) from error
    if not key_id:
        raise SigningUnavailable(
            f"no signing key is configured, so {KEY_ID_KEY} names the value to provision"
        )
    return key_id


def _configured_path(configuration: Configuration) -> Path | None:
    """The saved public half's path, or None where the surface declares none.

    An unset key is absence rather than a fault: a deployment verifying inside itself
    holds no saved file and asks the key service instead.
    """
    try:
        declared = configuration.text(PUBLIC_KEY_PATH_KEY).strip()
    except ConfigError:
        return None
    return Path(declared) if declared else None


def public_key_source(configuration: Configuration | None = None) -> PublicKeySource:
    """The retrieval a verification uses: the saved file where one is configured.

    The file is preferred when it is configured, because a deployment that went to the
    trouble of saving the public half wants verification not to depend on the key
    service, and because it is the path an auditor's own run takes. Absent the file the
    service is asked, which is what an operator verifying inside the deployment does.
    Which source answered is logged, so a verification's independence is observable
    rather than assumed.
    """
    resolved = load_configuration() if configuration is None else configuration
    key_id = _configured_key_id(resolved)
    stored = _configured_path(resolved)
    if stored is not None:
        log(
            Severity.INFO,
            COMPONENT,
            "verifying against the saved public half, so no key service is called",
            key_id=key_id,
        )
        return StoredPublicKey(path=stored, key_id=key_id)
    return KmsKeys()


def signer_from_configuration(configuration: Configuration | None = None) -> KmsSigner:
    """The signer a deployment signs with, refusing when no key is provisioned.

    Returned only from a configuration that names a key, so an unsigned document is a
    refusal an operator sees rather than a certificate that quietly carries nothing.
    """
    resolved = load_configuration() if configuration is None else configuration
    _configured_key_id(resolved)
    try:
        algorithm = resolved.text(ALGORITHM_KEY).strip()
    except ConfigError as error:
        raise SigningUnavailableError(
            f"no signing algorithm is configured, so {ALGORITHM_KEY} names the value to set"
        ) from error
    if algorithm != SUPPORTED_ALGORITHM:
        raise SigningUnavailableError(
            f"the configured signing algorithm {algorithm!r} is not the one this build verifies"
        )
    return KmsSigner()
