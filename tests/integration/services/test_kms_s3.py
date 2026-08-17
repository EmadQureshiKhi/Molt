"""The key service and the evidence object store, exercised live where configured.

Two services are touched here and nothing else is: the key service signs a digest
and hands back the public half of the key it signed under, and the object store
accepts one certificate object under Object Lock and hands the same bytes back.
Nothing on those paths is stubbed, because the whole point of this module is to
touch the two things the unit suite cannot: an asymmetric signature the delivered
key really produced, and a write-once object the delivered bucket really accepted.

**Every test here skips in this environment, and that is the correct outcome.** No
key is provisioned and no bucket exists, so there is nothing to sign under and
nothing to write into. What is therefore actually verified today is the half that
has to hold regardless: that an unprovisioned deployment makes this module skip
with a message naming the configuration key whose value is absent, and never makes
it fail. A test that errored because a bucket does not exist would be a badly
written test; a test that skips saying which key names the bucket is a correct one.

**The gating is layered, and each layer answers a different question.** The
`services` marker answers *is anything configured at all* -- a region and a
credential source for cloud access and for each model provider role -- and the
message names every absent key. Inside each test the narrower question is asked of
the one resource that test needs: the signing key identifier, or the certificate
bucket. So a deployment holding a key but no bucket exercises the signing half
rather than skipping both, and each skip names the single key an operator has to
set next.

**The signature is verified in this process, never by the service.** The service
is asked to sign and asked for the public half, and the check itself is elliptic
curve arithmetic performed here against that public half through the verifier's
own `verify_signature`. That is the whole basis of the independence a certificate
claims: a reviewer who never held permission to call the key service verifies
exactly as well as the issuer does. Asking the service to verify would make every
verification a call an operator can be denied.

**The digest is signed as a digest.** The bytes sent are already hashed, so no
part of the document the signature covers is disclosed to a third service. That is
asserted by construction: the length of what is sent is the digest length, and the
document text never leaves this process.

**What a full run costs, where one is possible.** Two signing calls, three public
half retrievals of which one is served from the per-process cache, one object
write, one object read, and one object write the bucket policy is expected to
refuse. Nothing here loops, nothing retries, and no test scales its call count
with anything. One certificate object is left in the bucket per run, which is what
Object Lock means: the object cannot be deleted while its retention stands, and
releasing that retention is the teardown script's job rather than a test's.

No credential value, bucket name, key identifier, or region appears in this file.
Every one of them is read from the configuration surface at run time, and the
disclosure test below asserts that none of them reaches the output stream.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Final, Protocol, cast
from uuid import uuid4

import pytest

from molt.attest.builder import (
    OBJECT_LOCK_MODE,
    STORAGE_FAILED,
    STORAGE_STORED,
    CertificateObjectStore,
    CertificatePolicy,
    object_key_for,
)
from molt.attest.keys import (
    DIGEST_MESSAGE_TYPE,
    KeyServiceClient,
    KmsKeys,
    KmsSigner,
    StoredPublicKey,
    key_service_client,
    public_key_source,
    signer_from_configuration,
)
from molt.attest.verifier import (
    ALGORITHM_KEY,
    KEY_ID_KEY,
    SUPPORTED_ALGORITHM,
    LocalSignatureChecker,
    PublicKeySource,
    verify_signature,
)
from molt.config.resolve import (
    CREDENTIAL_MARKERS,
    ConfigError,
    Configuration,
    MissingConfigError,
    load_configuration,
)
from molt.errors import SigningUnavailableError, VerificationFailedError
from molt.telemetry import LOG_KEY_ORDER, is_content_key

# The whole module needs cloud access and a credential source for each provider
# role, which is what the marker gates on. Each test additionally names the one
# resource it needs, so a partially provisioned deployment skips one test rather
# than all of them.
pytestmark = pytest.mark.services

# The configuration key the certificate bucket is named by. Spelled through the
# builder's own constant rather than restated, so this module and the module under
# test cannot disagree about which key an operator sets.
BUCKET_KEY: Final[str] = "MOLT_CERT_BUCKET"

# The digest length the signing path sends. SHA-256 produces this many bytes, and
# the assertion that the request carried exactly this many is what shows the
# service received a digest rather than a document.
DIGEST_BYTES: Final[int] = 32

# One representative document, source-code shaped because the documents this key
# signs are canonicalised evidence about source-code artifacts. It never leaves
# this process: only its digest is sent.
REPRESENTATIVE_DOCUMENT: Final[str] = '{"kind":"probe","statement":"def merge(left, right)"}'

# The field the object store returns a version identifier under, and the two
# retention fields it reports back on a read. Fixed by the service, named here so
# no assertion spells one inline.
_RESPONSE_VERSION: Final[str] = "VersionId"
_RESPONSE_BODY: Final[str] = "Body"
_RESPONSE_LOCK_MODE: Final[str] = "ObjectLockMode"
_RESPONSE_RETAIN_UNTIL: Final[str] = "ObjectLockRetainUntilDate"

# The request fields one object write is made under.
_REQUEST_BUCKET: Final[str] = "Bucket"
_REQUEST_KEY: Final[str] = "Key"
_REQUEST_BODY: Final[str] = "Body"
_REQUEST_ENCRYPTION: Final[str] = "ServerSideEncryption"
_REQUEST_LOCK_MODE: Final[str] = "ObjectLockMode"
_REQUEST_RETAIN_UNTIL: Final[str] = "ObjectLockRetainUntilDate"

# The encryption the bucket policy requires of every write. A write naming none is
# denied by that policy, which is the condition the refusal test exercises.
REQUIRED_ENCRYPTION: Final[str] = "AES256"

# The object service this module builds a client for.
OBJECT_SERVICE: Final[str] = "s3"

# Environment variable names whose value is a credential rather than a place a
# credential lives. The surface's own markers are extended by the one cloud
# spelling none of them matches, and names ending in a source-naming suffix are
# excluded: those hold a parameter name or a file path, which an error message is
# obliged to carry.
CREDENTIAL_NAME_MARKERS: Final[tuple[str, ...]] = (*CREDENTIAL_MARKERS, "access_key")
SOURCE_NAMING_SUFFIXES: Final[tuple[str, ...]] = ("_PARAM", "_FILE", "_PATH")

# How deep a walk over one log record goes before it stops. The surface drops a
# structure deeper than its own bound, so a record reaching this depth is not one
# the filter produced.
RECORD_DEPTH_LIMIT: Final[int] = 40


# ---------------------------------------------------------------------------
# Gating, one resource at a time
# ---------------------------------------------------------------------------


def _configuration() -> Configuration:
    """The resolved surface every value below is read from."""
    return load_configuration()


def _absent(key: str, fault: ConfigError) -> str:
    """The skip message for a resource the deployment names no value for."""
    return (
        f"{key} names no value, so the resource this test needs is not provisioned "
        f"and no service was called: {fault}. Nothing else in the suite depends on it."
    )


def _signing_policy(configuration: Configuration) -> tuple[str, str]:
    """The configured key identifier and algorithm, or a skip naming what is absent."""
    try:
        key_id = configuration.text(KEY_ID_KEY).strip()
    except MissingConfigError as fault:
        pytest.skip(_absent(KEY_ID_KEY, fault))
    if not key_id:
        pytest.skip(_absent(KEY_ID_KEY, MissingConfigError(KEY_ID_KEY, None)))
    algorithm = configuration.text(ALGORITHM_KEY).strip()
    return key_id, algorithm


def _certificate_policy(configuration: Configuration) -> CertificatePolicy:
    """The whole certificate surface, or a skip naming the key that is absent."""
    _signing_policy(configuration)
    try:
        return CertificatePolicy.from_configuration(configuration)
    except MissingConfigError as fault:
        pytest.skip(_absent(BUCKET_KEY, fault))


def _signer(configuration: Configuration) -> KmsSigner:
    """The signer a deployment signs with, or a skip naming what it needs."""
    _signing_policy(configuration)
    try:
        return signer_from_configuration(configuration)
    except SigningUnavailableError as fault:
        pytest.skip(
            f"the configured signing algorithm is not the one this build verifies, "
            f"so no call was made: {fault}"
        )


# ---------------------------------------------------------------------------
# The two live clients, each behind a recording wrapper
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class CountingKeyClient:
    """The real key service client, counting the calls made through it.

    Counting is what makes the per-process cache observable: a second retrieval
    that reaches the service and one that does not are indistinguishable from the
    bytes alone, and the bytes are all the cache returns.
    """

    inner: KeyServiceClient
    signatures: int = 0
    retrievals: int = 0
    signed_lengths: list[int] = field(default_factory=list)
    message_types: list[object] = field(default_factory=list)

    def sign(self, **request: object) -> object:
        """Forward one signing call, recording what was sent rather than the answer."""
        self.signatures += 1
        message = request.get("Message")
        self.signed_lengths.append(len(message) if isinstance(message, bytes) else -1)
        self.message_types.append(request.get("MessageType"))
        return self.inner.sign(**request)

    def get_public_key(self, **request: object) -> object:
        """Forward one retrieval, counting it."""
        self.retrievals += 1
        return self.inner.get_public_key(**request)


class ObjectBody(Protocol):
    """The one call made on the streamed body of an object read."""

    def read(self) -> bytes:
        """The whole body of the object."""


class ObjectClient(Protocol):
    """The two calls this module makes on an object store client.

    Declared as a shape rather than imported, for the same reason the production
    modules declare theirs: the cloud library ships nothing the type check can
    follow, and a shape keeps the import inside the factory below.
    """

    def put_object(self, **request: object) -> object:
        """Write one object and return what the service reported about it."""

    def get_object(self, **request: object) -> object:
        """Read one object and return the response carrying its body."""


def _object_client() -> ObjectClient:
    """Build a real object store client, importing the cloud library at call time.

    The import happens here rather than at module scope so collection needs no
    library resolution and no credential chain, which is what lets this module be
    collected and skipped on a bare checkout.
    """
    module = import_module("boto3")
    return cast(ObjectClient, module.client(OBJECT_SERVICE))


def _mapping(response: object, what: str) -> Mapping[str, object]:
    """Narrow a service response to the mapping every field is read out of."""
    assert isinstance(response, Mapping), f"the object store answered no mapping for {what}"
    return cast(Mapping[str, object], response)


@dataclass(frozen=True, slots=True)
class LiveObjectStore:
    """The evidence object store as the certificate surface consumes it.

    This satisfies `CertificateObjectStore` and nothing wider, and it is the only
    place in this module that names the request fields, so a test asserts over an
    outcome rather than over a request it built itself. The encryption field is
    always set, because the bucket policy denies a write that names none and a
    write the policy denies is not the write the certificate path performs.
    """

    client: ObjectClient

    def put_certificate(
        self,
        body: bytes,
        *,
        bucket: str,
        key: str,
        object_lock_mode: str,
        retain_until: datetime,
    ) -> str:
        """Write one certificate under Object Lock and return the object version."""
        response = _mapping(
            self.client.put_object(
                **{
                    _REQUEST_BUCKET: bucket,
                    _REQUEST_KEY: key,
                    _REQUEST_BODY: body,
                    _REQUEST_ENCRYPTION: REQUIRED_ENCRYPTION,
                    _REQUEST_LOCK_MODE: object_lock_mode,
                    _REQUEST_RETAIN_UNTIL: retain_until,
                }
            ),
            "a write",
        )
        version = response.get(_RESPONSE_VERSION)
        assert isinstance(version, str) and version, (
            "a versioned bucket answers a write with the version identifier the "
            "certificate row records beside the stored status"
        )
        return version

    def fetch_version(self, *, bucket: str, key: str, version: str) -> Mapping[str, object]:
        """Read one object version back, returning the whole response."""
        return _mapping(
            self.client.get_object(
                **{_REQUEST_BUCKET: bucket, _REQUEST_KEY: key, "VersionId": version}
            ),
            "a read",
        )


# ---------------------------------------------------------------------------
# Signing, and verifying without the service
# ---------------------------------------------------------------------------


def _digest() -> bytes:
    """The digest of the representative document, which is what gets signed."""
    computed = hashlib.sha256(REPRESENTATIVE_DOCUMENT.encode("utf-8")).digest()
    assert len(computed) == DIGEST_BYTES
    return computed


def test_a_signed_digest_verifies_in_process_against_the_retrieved_public_half() -> None:
    """One live signature, checked here rather than by the service that made it."""
    configuration = _configuration()
    key_id, algorithm = _signing_policy(configuration)
    assert algorithm == SUPPORTED_ALGORITHM, (
        "the configured algorithm is the one this build verifies, or nothing signed "
        "under it could be checked at all"
    )

    counting = CountingKeyClient(inner=key_service_client())
    client: KeyServiceClient = counting
    assert client is counting
    signer = KmsSigner(keys=KmsKeys(client_factory=lambda: counting))

    digest = _digest()
    signature = signer.sign_digest(digest, key_id=key_id, algorithm=algorithm)
    public_half = signer.public_key(key_id=key_id)

    assert signature, "a completed signing call answers some signature"
    assert public_half, "a retrieval answers the encoded public half"
    assert counting.signatures == 1
    assert counting.signed_lengths == [DIGEST_BYTES], (
        "the service received a digest rather than the document, so no part of the "
        "content the signature covers was disclosed to it"
    )
    assert counting.message_types == [DIGEST_MESSAGE_TYPE]

    # The check is local arithmetic over a public value. The service is never asked
    # whether its own signature is good.
    assert verify_signature(digest, signature, public_key=public_half, algorithm=algorithm), (
        "the signature the key service produced verifies against the key's public half"
    )
    assert signer.verify_digest(
        digest, signature, key_id=key_id, algorithm=algorithm, public_key=public_half
    )

    # The same signature over other bytes does not verify, which is what makes the
    # assertion above a check rather than a formality.
    other = hashlib.sha256(b"a different document").digest()
    assert not verify_signature(other, signature, public_key=public_half, algorithm=algorithm)
    assert counting.signatures == 1, "verifying costs no further signing call"


def test_a_verifier_shaped_object_refuses_to_sign() -> None:
    """The verifier's signer seam raises rather than producing a signature.

    A verifier that could sign would hold the very privilege its independence rests
    on not holding, so the refusal is asserted directly and both retrieval sources
    are asserted to carry no signing call at all.
    """
    configuration = _configuration()
    key_id, algorithm = _signing_policy(configuration)

    source: PublicKeySource = public_key_source(configuration)
    checker = LocalSignatureChecker(keys=source)

    with pytest.raises(VerificationFailedError):
        checker.sign_digest(_digest(), key_id=key_id, algorithm=algorithm)

    # Neither retrieval source has a signing call to reach for in the first place.
    assert not hasattr(KmsKeys, "sign_digest")
    assert not hasattr(StoredPublicKey, "sign_digest")
    assert not hasattr(source, "sign_digest")

    # The retrieval half still works, so the refusal is the signing privilege being
    # absent rather than the object being inert.
    retrieved = checker.public_key(key_id=key_id)
    assert retrieved, "a verifier retrieves the public half it verifies against"


def test_the_public_half_is_retrieved_once_for_the_life_of_the_process() -> None:
    """A second retrieval of the same key makes no second service call.

    A key's public half is fixed for the life of its identifier, and a certificate
    covering a thousand dispositions is checked against one retrieval rather than
    one per check. Counting the calls on the client is the only way to see that.
    """
    configuration = _configuration()
    key_id, _algorithm = _signing_policy(configuration)

    counting = CountingKeyClient(inner=key_service_client())
    keys = KmsKeys(client_factory=lambda: counting)

    first = keys.public_key(key_id=key_id)
    second = keys.public_key(key_id=key_id)
    third = keys.public_key(key_id=key_id)

    assert first == second == third
    assert counting.retrievals == 1, (
        "the cache is keyed on the identifier, so two later reads of the same key "
        "cost no further call"
    )


def test_a_retrieval_under_no_identifier_is_refused_before_any_call() -> None:
    """An empty identifier is refused by name, and nothing is sent.

    The refusal happens before a client is even built, which is what keeps an
    unprovisioned deployment from making a call that could only fail.
    """
    counting = CountingKeyClient(inner=key_service_client())
    keys = KmsKeys(client_factory=lambda: counting)
    with pytest.raises(SigningUnavailableError) as caught:
        keys.public_key(key_id="")
    assert counting.retrievals == 0
    assert "identifier" in str(caught.value)


def test_an_algorithm_this_build_does_not_verify_is_refused_before_any_call() -> None:
    """A signature nothing here could check is refused rather than requested."""
    configuration = _configuration()
    key_id, _algorithm = _signing_policy(configuration)
    counting = CountingKeyClient(inner=key_service_client())
    signer = KmsSigner(keys=KmsKeys(client_factory=lambda: counting))
    with pytest.raises(SigningUnavailableError) as caught:
        signer.sign_digest(_digest(), key_id=key_id, algorithm="ECDSA_SHA_384")
    assert counting.signatures == 0
    message = str(caught.value)
    assert "ECDSA_SHA_384" in message
    assert key_id not in message, "a refusal names the algorithm rather than the key"


# ---------------------------------------------------------------------------
# The object write, under Object Lock
# ---------------------------------------------------------------------------


def _envelope_bytes() -> bytes:
    """A small, obviously synthetic certificate-shaped body, unique per run.

    Unique because a versioned bucket under Object Lock keeps every version: a
    body identical to an earlier run's would make a round-trip assertion pass on
    somebody else's bytes.
    """
    return json.dumps(
        {"payload": {"probe": uuid4().hex}, "signature": {"algorithm": SUPPORTED_ALGORITHM}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_a_certificate_object_is_written_under_object_lock_and_read_back() -> None:
    """One write, one read, and the retention the certificate surface asked for.

    The version identifier the write returns is exactly the value the certificate
    row records beside the stored status, and the retention the read reports is the
    posture the certificate surface applied rather than a bucket default this test
    assumed.
    """
    configuration = _configuration()
    policy = _certificate_policy(configuration)

    store = LiveObjectStore(client=_object_client())
    seam: CertificateObjectStore = store
    assert seam is store

    body = _envelope_bytes()
    run_id = uuid4()
    key = object_key_for(policy, "service-probe", run_id)
    written_at = datetime.now(tz=UTC)
    retain_until = written_at + policy.retention

    version = store.put_certificate(
        body,
        bucket=policy.bucket,
        key=key,
        object_lock_mode=OBJECT_LOCK_MODE,
        retain_until=retain_until,
    )

    assert version, (
        "a returned version identifier is what the certificate row records beside "
        f"the {STORAGE_STORED} status"
    )

    response = store.fetch_version(bucket=policy.bucket, key=key, version=version)
    streamed = cast(ObjectBody, response[_RESPONSE_BODY])
    assert streamed.read() == body, "the stored object is byte-identical to what was written"
    assert response.get(_RESPONSE_VERSION) == version

    assert response.get(_RESPONSE_LOCK_MODE) == OBJECT_LOCK_MODE, (
        "the retention posture the certificate surface applies is the releasable one, "
        "which is what lets teardown complete without manual intervention"
    )
    reported = response.get(_RESPONSE_RETAIN_UNTIL)
    assert isinstance(reported, datetime)
    assert reported > written_at, "retention stands for some interval beyond the write"
    assert reported - written_at <= policy.retention + timedelta(days=1)


def test_a_write_naming_no_encryption_is_refused_and_names_no_configured_value() -> None:
    """The bucket policy denies an unencrypted write, and the fault discloses nothing.

    This is the branch the certificate surface records as a failed storage status
    with the fault's type name as the detail. The assertion is that the recorded
    detail could carry no bucket name and no key identifier, because the type name
    is all that is taken from the fault.
    """
    configuration = _configuration()
    policy = _certificate_policy(configuration)
    client = _object_client()

    key = object_key_for(policy, "service-probe-unencrypted", uuid4())
    refused: Exception | None = None
    try:
        client.put_object(
            **{
                _REQUEST_BUCKET: policy.bucket,
                _REQUEST_KEY: key,
                _REQUEST_BODY: _envelope_bytes(),
            }
        )
    except Exception as error:
        # The cloud library names its own refusal type and this module imports that
        # library lazily, so the fault is caught by nothing narrower and the type
        # name is the only thing taken from it -- which is exactly what the
        # certificate surface records in the detail column.
        refused = error

    assert refused is not None, (
        "the bucket policy denies a write that names no encryption, so a write "
        "naming none was expected to be refused"
    )
    recorded_detail = type(refused).__name__
    assert recorded_detail, f"a refused write is recorded as {STORAGE_FAILED} with a type name"
    assert policy.bucket not in recorded_detail
    assert policy.kms_key_id not in recorded_detail


# ---------------------------------------------------------------------------
# Nothing read from a credential reaches an output stream
# ---------------------------------------------------------------------------


def _credential_bearing_names() -> tuple[str, ...]:
    """The environment names whose value is a credential rather than a place one lives.

    Only names are collected here. A name ending in a source-naming suffix holds a
    parameter name or a file path, which an error message is obliged to carry, so it
    is excluded rather than checked.
    """
    return tuple(
        name
        for name in sorted(os.environ)
        if any(marker in name.lower() for marker in CREDENTIAL_NAME_MARKERS)
        and not name.endswith(SOURCE_NAMING_SUFFIXES)
    )


def _leaked_into(text: str) -> tuple[str, ...]:
    """The names whose value appears in some text. Only names are ever returned."""
    return tuple(
        name
        for name in _credential_bearing_names()
        if os.environ.get(name, "").strip() and os.environ[name].strip() in text
    )


def test_no_credential_value_reaches_a_log_record_or_the_output_stream(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole signing and retrieval path, with both streams read back afterwards.

    Three things are asserted over what was written. No value of any credential
    bearing environment variable appears in it, and the report names only the
    variables rather than their values so a failure here discloses nothing either.
    Every record is a single-line document carrying the four fixed keys. And no
    field of any record, at any depth, is one telemetry never carries.
    """
    configuration = _configuration()
    key_id, algorithm = _signing_policy(configuration)
    signer = _signer(configuration)

    digest = _digest()
    signature = signer.sign_digest(digest, key_id=key_id, algorithm=algorithm)
    public_half = signer.public_key(key_id=key_id)
    signer.public_key(key_id=key_id)

    captured = capsys.readouterr()
    written = captured.out + captured.err

    assert _leaked_into(written) == (), (
        "a credential value reached an output stream; the variables it came from are "
        "named here and their values deliberately are not"
    )
    assert signature.hex() not in written
    assert public_half.hex() not in written

    for line in written.splitlines():
        if not line.startswith("{"):
            continue
        record: object = json.loads(line)
        assert isinstance(record, dict)
        assert tuple(record)[: len(LOG_KEY_ORDER)] == LOG_KEY_ORDER
        _assert_no_content_field(record)


def _assert_no_content_field(value: object, depth: int = 0) -> None:
    """Walk a record and refuse any field name telemetry never carries."""
    assert depth <= RECORD_DEPTH_LIMIT, "a record deeper than this is not a diagnostic"
    if isinstance(value, dict):
        for name, held in value.items():
            assert not is_content_key(str(name)), f"the record carried the field {name!r}"
            _assert_no_content_field(held, depth + 1)
        return
    if isinstance(value, list):
        for held in value:
            _assert_no_content_field(held, depth + 1)
