"""The key service client: signing through it, and verifying without it.

Credential-free and network-free. The service is stood in for by a local key pair
answering the two calls the real one answers, which is the whole of what the signing
path uses, so what is exercised here is this module's own request shaping, caching,
refusals, and the split between what needs the service and what does not.

The load-bearing case is the round trip: a digest signed through the client verifies
against the public half the client retrieved, using the verifier's own local check. If
the request fields were wrong, the digest were sent as a message rather than as a
digest, or the retrieved key were not the signing key's counterpart, that case fails.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from molt.attest.keys import (
    DIGEST_MESSAGE_TYPE,
    PUBLIC_KEY_PATH_KEY,
    KmsKeys,
    KmsSigner,
    StoredPublicKey,
    public_key_source,
    signer_from_configuration,
)
from molt.attest.verifier import KEY_ID_KEY, SUPPORTED_ALGORITHM, verify_signature
from molt.config.resolve import Configuration
from molt.errors import SigningUnavailableError

KEY_ID: Final[str] = "a-provisioned-signing-key"
OTHER_KEY_ID: Final[str] = "a-key-this-file-does-not-hold"
DOCUMENT: Final[bytes] = b"the canonical bytes of one erasure certificate"


def _digest() -> bytes:
    """The digest a document is signed over, computed as the signing flow computes it."""
    return hashlib.sha256(DOCUMENT).digest()


class LocalKeyService:
    """A stand-in answering the two calls, backed by one local key pair.

    Every request is recorded, because the fields the real service is called with are
    part of what this module is responsible for: a digest sent as a message rather than
    as a digest would put the document itself into a third-party request.
    """

    def __init__(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.sign_requests: list[dict[str, object]] = []
        self.retrievals: list[dict[str, object]] = []

    def sign(self, **request: object) -> object:
        self.sign_requests.append(dict(request))
        if request.get("KeyId") != KEY_ID:
            raise AssertionError("the stand-in holds no key under that identifier")
        digest = request["Message"]
        assert isinstance(digest, bytes)
        signature = self.private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return {"Signature": signature}

    def get_public_key(self, **request: object) -> object:
        self.retrievals.append(dict(request))
        encoded = self.private.public_key().public_bytes(
            encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo
        )
        return {"PublicKey": encoded}


def _signer(service: LocalKeyService) -> KmsSigner:
    """A signer whose client is the stand-in rather than a cloud client."""
    return KmsSigner(keys=KmsKeys(client_factory=lambda: service))


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_a_digest_signed_through_the_client_verifies_against_the_retrieved_key() -> None:
    """The whole point: what this signs, the verifier's own local check accepts."""
    service = LocalKeyService()
    signer = _signer(service)
    digest = _digest()

    signature = signer.sign_digest(digest, key_id=KEY_ID, algorithm=SUPPORTED_ALGORITHM)
    retrieved = signer.public_key(key_id=KEY_ID)

    assert verify_signature(
        digest, signature, public_key=retrieved, algorithm=SUPPORTED_ALGORITHM
    ), "a signature this client produced did not verify against the key it retrieved"
    assert signer.verify_digest(
        digest,
        signature,
        key_id=KEY_ID,
        algorithm=SUPPORTED_ALGORITHM,
        public_key=retrieved,
    )


def test_a_signature_over_other_bytes_does_not_verify() -> None:
    """The check is a real check: it distinguishes the document it was made over."""
    service = LocalKeyService()
    signer = _signer(service)
    signature = signer.sign_digest(_digest(), key_id=KEY_ID, algorithm=SUPPORTED_ALGORITHM)
    other = hashlib.sha256(DOCUMENT + b" altered").digest()

    assert not signer.verify_digest(
        other,
        signature,
        key_id=KEY_ID,
        algorithm=SUPPORTED_ALGORITHM,
        public_key=signer.public_key(key_id=KEY_ID),
    )


def test_the_digest_is_sent_as_a_digest_and_never_as_the_document() -> None:
    """The service receives a hash, so no part of the signed document reaches it."""
    service = LocalKeyService()
    _signer(service).sign_digest(_digest(), key_id=KEY_ID, algorithm=SUPPORTED_ALGORITHM)

    assert len(service.sign_requests) == 1
    sent = service.sign_requests[0]
    assert sent["MessageType"] == DIGEST_MESSAGE_TYPE
    assert sent["SigningAlgorithm"] == SUPPORTED_ALGORITHM
    assert sent["KeyId"] == KEY_ID
    carried = sent["Message"]
    assert isinstance(carried, bytes)
    assert carried == _digest()
    assert DOCUMENT not in carried, "the document itself reached the key service"


# ---------------------------------------------------------------------------
# Retrieval and its cache
# ---------------------------------------------------------------------------


def test_the_public_half_is_retrieved_once_per_identifier() -> None:
    """A certificate covering many dispositions costs one retrieval, not one per check."""
    service = LocalKeyService()
    keys = KmsKeys(client_factory=lambda: service)

    first = keys.public_key(key_id=KEY_ID)
    for _ in range(5):
        assert keys.public_key(key_id=KEY_ID) == first

    assert len(service.retrievals) == 1
    assert service.retrievals[0] == {"KeyId": KEY_ID}


def test_a_retrieval_under_no_identifier_is_refused_before_a_call() -> None:
    service = LocalKeyService()
    with pytest.raises(SigningUnavailableError):
        KmsKeys(client_factory=lambda: service).public_key(key_id="")
    assert service.retrievals == []


def test_a_response_missing_its_field_is_a_fault_rather_than_empty_bytes() -> None:
    """A signing call that returned no signature has not signed."""

    class Silent:
        def sign(self, **request: object) -> object:
            del request
            return {}

        def get_public_key(self, **request: object) -> object:
            del request
            return {}

    signer = KmsSigner(keys=KmsKeys(client_factory=Silent))
    with pytest.raises(SigningUnavailableError):
        signer.sign_digest(_digest(), key_id=KEY_ID, algorithm=SUPPORTED_ALGORITHM)
    with pytest.raises(SigningUnavailableError):
        signer.public_key(key_id=KEY_ID)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_algorithm_this_build_cannot_verify_is_refused_before_signing() -> None:
    """A signature nothing here can check is worse than no signature."""
    service = LocalKeyService()
    with pytest.raises(SigningUnavailableError):
        _signer(service).sign_digest(_digest(), key_id=KEY_ID, algorithm="RSASSA_PSS_SHA_512")
    assert service.sign_requests == []


def test_signing_under_no_identifier_is_refused_before_a_call() -> None:
    service = LocalKeyService()
    with pytest.raises(SigningUnavailableError):
        _signer(service).sign_digest(_digest(), key_id="", algorithm=SUPPORTED_ALGORITHM)
    assert service.sign_requests == []


# ---------------------------------------------------------------------------
# The saved public half, which is what makes a verification independent
# ---------------------------------------------------------------------------


def test_a_saved_public_half_verifies_with_no_service_call_at_all(tmp_path: Path) -> None:
    """An auditor with the file and the certificate needs no account anywhere."""
    service = LocalKeyService()
    signer = _signer(service)
    digest = _digest()
    signature = signer.sign_digest(digest, key_id=KEY_ID, algorithm=SUPPORTED_ALGORITHM)

    saved = tmp_path / "public.der"
    saved.write_bytes(signer.public_key(key_id=KEY_ID))
    retrievals_before = len(service.retrievals)

    offline = StoredPublicKey(path=saved, key_id=KEY_ID)
    assert verify_signature(
        digest,
        signature,
        public_key=offline.public_key(key_id=KEY_ID),
        algorithm=SUPPORTED_ALGORITHM,
    )
    assert len(service.retrievals) == retrievals_before, "the offline source called the service"


def test_a_saved_half_refuses_an_identifier_it_does_not_hold(tmp_path: Path) -> None:
    """A file holding one key cannot answer for a document signed under another."""
    saved = tmp_path / "public.der"
    saved.write_bytes(b"an encoded key")
    with pytest.raises(SigningUnavailableError):
        StoredPublicKey(path=saved, key_id=KEY_ID).public_key(key_id=OTHER_KEY_ID)


def test_an_unreadable_saved_half_is_refused_rather_than_read_as_empty(tmp_path: Path) -> None:
    absent = tmp_path / "not-written.der"
    with pytest.raises(SigningUnavailableError):
        StoredPublicKey(path=absent, key_id=KEY_ID).public_key(key_id=KEY_ID)


# ---------------------------------------------------------------------------
# Resolution from the surface
# ---------------------------------------------------------------------------


def _surface(**values: str) -> Configuration:
    """A configuration view carrying only the named environment values."""
    return Configuration(environ=values, file_values={})


def test_a_configured_file_is_preferred_so_verification_calls_no_service(
    tmp_path: Path,
) -> None:
    saved = tmp_path / "public.der"
    saved.write_bytes(b"an encoded key")
    source = public_key_source(_surface(**{KEY_ID_KEY: KEY_ID, PUBLIC_KEY_PATH_KEY: str(saved)}))
    assert isinstance(source, StoredPublicKey)
    assert source.key_id == KEY_ID


def test_the_service_answers_where_no_file_is_configured() -> None:
    source = public_key_source(_surface(**{KEY_ID_KEY: KEY_ID}))
    assert isinstance(source, KmsKeys)


def test_a_surface_naming_no_signing_key_is_refused_by_name() -> None:
    """An unprovisioned deployment is reported as such, never as an invalid document."""
    with pytest.raises(SigningUnavailableError) as raised:
        public_key_source(_surface())
    assert KEY_ID_KEY in str(raised.value)

    with pytest.raises(SigningUnavailableError):
        signer_from_configuration(_surface())


def test_a_signer_is_returned_only_for_a_provisioned_surface() -> None:
    built = signer_from_configuration(
        _surface(**{KEY_ID_KEY: KEY_ID, "MOLT_KMS_SIGNING_ALGORITHM": SUPPORTED_ALGORITHM})
    )
    assert isinstance(built, KmsSigner)

    with pytest.raises(SigningUnavailableError):
        signer_from_configuration(
            _surface(**{KEY_ID_KEY: KEY_ID, "MOLT_KMS_SIGNING_ALGORITHM": "RSASSA_PSS_SHA_512"})
        )


def test_no_retrieval_source_can_sign() -> None:
    """Only an object built from a signing configuration can produce a signature."""
    assert not hasattr(StoredPublicKey(path=Path(), key_id=KEY_ID), "sign_digest")
    assert not hasattr(KmsKeys(), "sign_digest")
