"""The one object write a certificate performs, against the real object service.

The certificate surface declares the write it needs as a structural protocol and takes
it as a parameter, which is what lets the whole issuing path be exercised with no
credential and no network call. What was missing was any implementation of that protocol
outside a test: the builder, the signer, and the policy were all written, and nothing in
the deployment could put an object anywhere, so a run that finished reported the key its
certificate would have had and no certificate was ever written.

Three decisions, each the same one the signing module makes for the key service.

**The cloud library is imported at call time rather than at module scope.** The module
must be importable, and the credential-free suites runnable, with no library resolution
and no credential chain at import time. Region and credentials resolve through the
library's own chain, so nothing about either is restated here.

**Retention is applied as the caller states it, not as this module decides.** The lock
mode and the instant retention runs until are both parameters, because how long a
tenant's evidence is held is a governance decision recorded on the certificate surface
and not a property of the code that writes bytes.

**A refused write raises rather than returning a sentinel.** The issuing path records the
storage outcome on the certificate row itself and distinguishes a stored certificate from
a signed one that could not be stored, so it needs the failure rather than an empty
version identifier that would read as a successful write of nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from importlib import import_module
from typing import Final, Protocol, cast

from molt.errors import StoreError
from molt.models.event import require_aware

__all__ = [
    "COMPONENT",
    "SERVICE_NAME",
    "ObjectServiceClient",
    "S3CertificateStore",
    "object_service_client",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "certificate_store"

# The service this module calls.
SERVICE_NAME: Final[str] = "s3"

# The request and response fields, fixed by the service. Named here so no call site
# spells one, and so a response of an unexpected shape is refused by field name.
_REQUEST_BUCKET: Final[str] = "Bucket"
_REQUEST_KEY: Final[str] = "Key"
_REQUEST_BODY: Final[str] = "Body"
_REQUEST_CONTENT_TYPE: Final[str] = "ContentType"
_REQUEST_LOCK_MODE: Final[str] = "ObjectLockMode"
_REQUEST_RETAIN_UNTIL: Final[str] = "ObjectLockRetainUntilDate"
_REQUEST_ENCRYPTION: Final[str] = "ServerSideEncryption"
_RESPONSE_VERSION: Final[str] = "VersionId"

# What a certificate envelope is. Declared on the object so a reader fetching one is
# served a document rather than an unnamed stream of bytes.
_CONTENT_TYPE: Final[str] = "application/json"

# The encryption the write declares. The evidence bucket's own policy refuses a put that
# does not ask for this one by name, deliberately: the template says an unencrypted write
# is refused rather than silently encrypted, so that a caller which omits the requirement
# learns it did. This caller omitted it, and learned it did. Stated here as the request
# field's own value rather than read from configuration, because it is not a deployment
# choice — it is the value the bucket policy names, and a mismatch between the two is a
# refused write rather than a weaker object.
_ENCRYPTION: Final[str] = "AES256"

# What the service reports for a bucket that is not versioned. A certificate bucket is
# versioned and locked by the template that creates it, so this is the honest answer to
# record rather than a failure: the object was written and the service named no version.
_UNVERSIONED: Final[str] = "null"


class ObjectServiceClient(Protocol):
    """The one call this module makes on an object service.

    Declared as a shape rather than as a library type, for the same reason the key
    service is: the storing path depends on an interface a test can satisfy, and the
    cloud import stays inside the factory below.
    """

    def put_object(self, **request: object) -> object:
        """Write one object and return what the service reported about it."""


def object_service_client() -> ObjectServiceClient:
    """Build a real object service client, importing the cloud library at call time."""
    module = import_module("boto3")
    return cast(ObjectServiceClient, module.client(SERVICE_NAME))


class S3CertificateStore:
    """The certificate object write, against the deployment's own evidence bucket.

    Satisfies the certificate surface's object-store protocol and nothing wider: there
    is no read here and no delete, so an object of this class cannot be mistaken for one
    that can retrieve or remove a tenant's evidence. The client is built once on first
    use rather than in the constructor, so constructing one costs no credential
    resolution and a path that never issues a certificate never builds a client.
    """

    __slots__ = ("_client", "_client_factory")

    def __init__(
        self,
        client_factory: Callable[[], ObjectServiceClient] = object_service_client,
    ) -> None:
        self._client_factory = client_factory
        self._client: ObjectServiceClient | None = None

    def _service(self) -> ObjectServiceClient:
        """The client, built on first use and held for this object's life."""
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def put_certificate(
        self,
        body: bytes,
        *,
        bucket: str,
        key: str,
        object_lock_mode: str,
        retain_until: datetime,
    ) -> str:
        """Write the envelope under Object Lock and return the object version.

        The retention instant is required to carry an offset, because a naive instant
        would be applied by the service in a zone this process did not choose and the
        interval a tenant's evidence is locked for would then be an accident.
        """
        until = require_aware(retain_until, "a certificate retention instant")
        try:
            answered = self._service().put_object(
                **{
                    _REQUEST_BUCKET: bucket,
                    _REQUEST_KEY: key,
                    _REQUEST_BODY: body,
                    _REQUEST_CONTENT_TYPE: _CONTENT_TYPE,
                    _REQUEST_ENCRYPTION: _ENCRYPTION,
                    _REQUEST_LOCK_MODE: object_lock_mode,
                    _REQUEST_RETAIN_UNTIL: until,
                }
            )
        # The service's own words are carried into the failure, not just the exception
        # type. A refused object write is diagnosed from the reason it was refused — a
        # missing lock configuration and a denied permission need different repairs — and
        # the caller records this text on the certificate row, where it is the only
        # account of why a signed document was not stored.
        except Exception as error:
            raise StoreError(
                f"the certificate could not be written to {bucket}/{key}: "
                f"{type(error).__name__}: {error}"
            ) from error
        return _version_of(answered)


def _version_of(response: object) -> str:
    """The version the service reported, or the unversioned answer where it named none.

    A response of an unexpected shape is a fault rather than an absent version: the call
    returned, so something answered, and treating an unreadable answer as a successful
    write would record a certificate as stored on the strength of nothing.
    """
    if not isinstance(response, dict):
        raise StoreError(
            f"the object service answered with {type(response).__name__} where a response was read"
        )
    version = response.get(_RESPONSE_VERSION)
    if isinstance(version, str) and version:
        return version
    if version is None:
        return _UNVERSIONED
    raise StoreError(
        f"the object service reported a version as {type(version).__name__} rather than text"
    )
