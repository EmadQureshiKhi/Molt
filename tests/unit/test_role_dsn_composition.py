"""Assertions on the connection strings the role provisioning writes into the store.

Each of the four service roles is reached through a connection string composed once,
by the provisioning run, and written straight into the parameter store. Nothing reads
it again until a deployed process resolves it at start-up, so a defect in its shape is
not visible until a function refuses to connect — which is the latest possible moment
and the least legible one.

That is exactly what happened. The composed string required full certificate
verification and named no authority set to verify against, so the client looked for an
authority file in the calling user's home directory. No function runtime and no task
image holds one, so every deployed process would have failed to connect, reporting a
missing file in a home directory: a fault that names neither the cluster, nor the
credential, nor the deployment.

So both halves are asserted here, together, because either alone is unusable. Full
verification without an authority set cannot connect; an authority set without full
verification is a chain nobody checks. The composition is also asserted to encode
every component it interpolates, since a credential is the one field most likely to
carry a character that would otherwise change the string's meaning.

Nothing here reaches a cluster or a cloud account. Every value is composed locally and
read back as a parsed connection string.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Final
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
COMPOSER_SOURCE: Final[Path] = REPOSITORY_ROOT / "scripts" / "compose_role_dsn.py"


def _load_composer() -> ModuleType:
    """Load the composer from its script path, since scripts form no import package."""
    specification = importlib.util.spec_from_file_location(
        "molt_role_dsn_composer_under_test", COMPOSER_SOURCE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    # Bytecode writing is suppressed for the load, so running this suite leaves no
    # cache directory beside the script it exercises.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        specification.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


COMPOSER: Final[ModuleType] = _load_composer()
compose_dsn: Final[Callable[..., str]] = COMPOSER.compose_dsn
DATABASE_NAME: Final[str] = COMPOSER.DATABASE_NAME
DEFAULT_PORT: Final[int] = COMPOSER.DEFAULT_PORT
REQUIRED_SSL_MODE: Final[str] = COMPOSER.REQUIRED_SSL_MODE
REQUIRED_ROOT_AUTHORITY: Final[str] = COMPOSER.REQUIRED_ROOT_AUTHORITY
SSL_MODE_PARAMETER: Final[str] = COMPOSER.SSL_MODE_PARAMETER
ROOT_AUTHORITY_PARAMETER: Final[str] = COMPOSER.ROOT_AUTHORITY_PARAMETER

# The four roles the provisioning composes a connection string for, and a host of the
# shape a managed cluster answers with.
SERVICE_ROLES: Final[tuple[str, ...]] = (
    "molt_writer",
    "molt_eraser",
    "molt_reader",
    "molt_watcher",
)
CLUSTER_HOST: Final[str] = "cluster.example.cockroachlabs.cloud"

# A credential holding a character of its own in every position that would change the
# meaning of a connection string if it were interpolated rather than encoded: the
# credential separator, the authority separator, the path separator, the query
# separator, and the pair separator.
AWKWARD_CREDENTIAL: Final[str] = "a:b@c/d?e&f=g h"


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_every_composed_connection_string_requires_verification_and_names_an_authority(
    role: str,
) -> None:
    """The pair of values that has to travel together, asserted together.

    A deployed process resolves this string and dials it with no chance to amend it,
    so what is composed here is the whole of what the connection can rely on. Full
    verification is the claim that the server is the named host and its certificate
    chains to an authority; the authority set is what the chain is checked against.
    Requiring the first while naming no second is not a weaker check, it is no
    connection at all, and it is the shape that shipped.
    """
    composed = compose_dsn(role, CLUSTER_HOST, "a-credential")
    query = parse_qs(urlsplit(composed).query)

    assert query.get(SSL_MODE_PARAMETER) == [REQUIRED_SSL_MODE], (
        f"the connection string for {role} does not require {REQUIRED_SSL_MODE}, so a "
        "role could connect to a host merely holding some valid certificate"
    )
    assert query.get(ROOT_AUTHORITY_PARAMETER) == [REQUIRED_ROOT_AUTHORITY], (
        f"the connection string for {role} requires {REQUIRED_SSL_MODE} while naming "
        "no authority set to verify against, so the client looks for an authority file "
        "in the calling user's home directory and no deployed runtime holds one"
    )


def test_the_composed_string_names_the_role_the_database_and_the_port() -> None:
    """The three components a deployed process cannot supply for itself.

    The role is the identity every privilege in the migrations is granted to, the
    database is the one the schema lives in, and the port is the cluster's. A string
    that verified correctly and named the wrong database would pass the case above and
    fail on a missing table, so it is read back here rather than assumed.
    """
    composed = compose_dsn("molt_writer", CLUSTER_HOST, "a-credential")
    parts = urlsplit(composed)

    assert parts.scheme == "postgresql"
    assert parts.username == "molt_writer"
    assert parts.hostname == CLUSTER_HOST
    assert parts.port == DEFAULT_PORT
    assert parts.path == f"/{DATABASE_NAME}"


def test_every_interpolated_component_is_encoded_rather_than_concatenated() -> None:
    """A credential may hold any character, and the string must still parse as one.

    Read back through the same two steps a client performs — split the string into
    fields, then decode the field — so the assertion is that the credential survives
    the round trip rather than that some particular escaping was applied. The decode
    is a step rather than an afterthought: splitting alone answers the encoded text,
    so a test comparing the split field directly would fail against a correctly
    composed string and pass against a concatenated one.

    A concatenated credential holding the query separator would silently truncate the
    string and drop the verification parameters entirely, which is the failure this
    refuses: the credential is the field most likely to carry such a character and the
    least likely to be looked at.
    """
    composed = compose_dsn("molt_reader", CLUSTER_HOST, AWKWARD_CREDENTIAL)
    parts = urlsplit(composed)
    query = parse_qs(parts.query)

    assert unquote(parts.password or "") == AWKWARD_CREDENTIAL, (
        "the credential did not survive composition, so it was concatenated rather than encoded"
    )
    assert parts.hostname == CLUSTER_HOST, "the credential displaced the host"
    assert query.get(SSL_MODE_PARAMETER) == [REQUIRED_SSL_MODE], (
        "the credential displaced the verification parameters, so a string carrying an "
        "awkward credential would connect without verification"
    )


def test_no_composed_string_carries_the_credential_outside_its_own_field() -> None:
    """The credential appears once, where a parser expects it, and nowhere else.

    Composition is string work, and the way string work leaks a secret is by putting
    it somewhere a reader does not expect to find one — a query parameter, a path
    segment, the host. The composed string goes to a parameter write, and anything
    that later logs a connection string with its credential field masked would still
    print a credential that had landed somewhere else.
    """
    composed = compose_dsn("molt_eraser", CLUSTER_HOST, AWKWARD_CREDENTIAL)
    parts = urlsplit(composed)

    assert AWKWARD_CREDENTIAL not in parts.path
    assert AWKWARD_CREDENTIAL not in parts.query
    assert AWKWARD_CREDENTIAL not in (parts.hostname or "")
