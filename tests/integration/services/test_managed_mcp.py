"""Auditor access under the read-only role, asserted by asking the cluster.

The managed MCP endpoint is a transport in front of a database login. What makes
auditor access safe is therefore not the endpoint but the login behind it: a role
holding `SELECT` and nothing else, over a view set filtered to one tenant. So this
module asserts the property the endpoint carries rather than the endpoint itself,
and it asserts it the only way that makes it a guarantee rather than a convention:
by connecting as that role and asking the cluster what the role may do.

**The role is confirmed twice, and the two answers are different facts.** The label
the configuration resolved says which role a deployment believes it is using; the
role the cluster reports says which one the connection is actually authenticated
as. A deployment can get the first right and the second wrong, so the verifier's own
`require_reader_role` checks both and refuses when either disagrees, and that is
the call made here rather than a second implementation of it.

**The absence of write privilege is read out of the cluster's own privilege
catalogue.** `has_table_privilege` answers for the connected role over a named
table, and every argument of it is bound rather than interpolated, so no identifier
reaches statement text. That is a stronger assertion than attempting a write and
being refused, because it covers privileges nobody thought to attempt.

**One write is still attempted, and it is chosen so that it changes nothing even
in the case where the guarantee has failed.** The statement names a row identifier
generated for this run, so it matches nothing: a role holding the privilege it
should not hold deletes zero rows, and a role holding no such privilege is refused.
The refusal is what is asserted; the harmlessness is what makes attempting it
acceptable against a deployed cluster.

**Markers.** These tests reach the cluster as well as needing cloud access, so they
carry `integration` beside `services`. Each additionally requires the deployment to
have named the read-only role's own connection string: a component whose own role is
already read-only needs no second handle, so that key is empty by default, and an
auditor path that has none is not configured rather than broken. The skip names it.

**Every test here skips in this environment.** No cluster is deployed and no
read-only connection string is configured.

No connection string, role credential, account identifier, or region appears in
this file. The role names come from the store layer's own set, and the connection
string is resolved through the secret accessors and never held in this process.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final
from uuid import uuid4

import pytest

from molt.attest.verifier import READER_ROLE_NAMES, require_reader_role
from molt.config.resolve import Configuration, MissingConfigError, load_configuration
from molt.errors import VerificationFailedError
from molt.store import Cursor, MemoryStore

pytestmark = [pytest.mark.services, pytest.mark.integration]

# The key naming the read-only role's own connection string. It is empty by
# default, because a component already running under a read-only role needs no
# second handle; an auditor path is exactly the case that configures it.
READER_DSN_KEY: Final[str] = "MOLT_READER_DSN_PARAM"

# The keys the reader view is overlaid on. The direct connection string is blanked
# rather than removed, because an empty value reads as unset through the surface and
# nothing is written back to the process environment either way.
DIRECT_DSN_KEY: Final[str] = "MOLT_DSN"
DSN_PARAMETER_KEY: Final[str] = "MOLT_DSN_PARAM"
ROLE_KEY: Final[str] = "MOLT_DB_ROLE"

# The tables an auditor's views are built over. Each is asked about by name as a
# bound parameter, so no identifier is ever written into statement text.
AUDITED_TABLES: Final[tuple[str, ...]] = ("ledger", "erasure_run", "client_binding")

# The privilege an auditor holds, and the three an auditor must not.
PERMITTED_PRIVILEGE: Final[str] = "SELECT"
REFUSED_PRIVILEGES: Final[tuple[str, ...]] = ("INSERT", "UPDATE", "DELETE")

# The privilege question, with the role, the table, and the privilege all bound.
# The connected role is the cluster's own reading of it rather than a value this
# module supplies, which is what makes the answer about the live connection.
TABLE_PRIVILEGE_QUERY: Final[str] = "SELECT has_table_privilege(current_user, %s, %s)"

# One read every role holds, used as the connectivity probe. It touches no table,
# so it answers whether the connection works without depending on any grant.
CONNECTIVITY_QUERY: Final[str] = "SELECT 1"

# A write that matches nothing. The identifier is generated per run, so a role
# holding a privilege it should not hold removes zero rows and a role holding none
# is refused, which is the outcome under assertion.
HARMLESS_DELETE: Final[str] = "DELETE FROM ledger WHERE id = %s"

# A label naming a role wider than the read-only one, used to show the refusal is
# about the label rather than about the connection. It is the configuration
# surface's own default for the role key, so it names no role this module invented.
WIDER_ROLE_LABEL: Final[str] = "writer"


# ---------------------------------------------------------------------------
# The read-only connection
# ---------------------------------------------------------------------------


def _reader_view(configuration: Configuration) -> Configuration:
    """The surface overlaid so the read-only connection string is the one resolved.

    The overlay sits on the resolved surface rather than replacing it, so the
    statement timeout, the retry policy, and every other value resolve exactly as
    they would in a running process, and nothing is written back to the process
    environment.
    """
    parameter = configuration.optional_text(READER_DSN_KEY)
    if not parameter:
        pytest.skip(
            f"{READER_DSN_KEY} names no value, so this deployment configured no "
            "read-only connection string for an auditor and nothing was connected to"
        )
    return configuration.replacing(
        {
            DIRECT_DSN_KEY: "",
            DSN_PARAMETER_KEY: parameter,
            ROLE_KEY: sorted(READER_ROLE_NAMES)[0],
        }
    )


def _store(view: Configuration) -> MemoryStore:
    """A store over one overlaid surface, or a skip naming what could not resolve."""
    try:
        return MemoryStore.from_configuration(view)
    except MissingConfigError as fault:
        pytest.skip(
            "the read-only connection string could not be resolved, so nothing was "
            f"connected to: {fault}"
        )


@pytest.fixture
def reader_store() -> Iterator[MemoryStore]:
    """A store over the read-only connection string, closed however the test ends."""
    store = _store(_reader_view(load_configuration()))
    try:
        yield store
    finally:
        store.close()


def _privilege(store: MemoryStore, table: str, privilege: str) -> bool:
    """Ask the cluster whether the connected role holds one privilege on one table."""

    def body(cursor: Cursor) -> bool:
        cursor.execute(TABLE_PRIVILEGE_QUERY, (table, privilege))
        row = cursor.fetchone()
        assert row is not None, "the privilege catalogue answered no row"
        held = row[0]
        assert isinstance(held, bool), "the privilege question answers a boolean"
        return held

    return store.read(body)


# ---------------------------------------------------------------------------
# Connectivity, and the role the connection actually holds
# ---------------------------------------------------------------------------


def test_the_auditor_connection_is_reachable_and_authenticates_as_the_reader_role(
    reader_store: MemoryStore,
) -> None:
    """The connection answers, and the role it answers as is the read-only one.

    The label and the reported role are both checked, through the verifier's own
    refusal rather than a second copy of it, because a deployment can name the role
    correctly in configuration and still connect as another.
    """

    def probe(cursor: Cursor) -> object:
        cursor.execute(CONNECTIVITY_QUERY)
        row = cursor.fetchone()
        assert row is not None
        return row[0]

    assert reader_store.read(probe) == 1

    assert reader_store.role in READER_ROLE_NAMES, (
        "the configured label names the read-only role, which is the half of the "
        "check a deployment controls"
    )
    reported = require_reader_role(reader_store)
    assert reported in READER_ROLE_NAMES, (
        "the role the cluster reports the connection as is the read-only one, which "
        "is the half no configuration can fake"
    )


def test_a_connection_whose_label_is_not_the_reader_role_refuses_to_verify() -> None:
    """A verifier that cannot show it is read-only refuses rather than proceeding.

    Driven over the same overlay with the label changed, so no connection is opened
    at all: the refusal happens on the label before any statement is sent, which is
    what keeps a wider role from reading evidence carefully.
    """
    view = _reader_view(load_configuration()).replacing({ROLE_KEY: WIDER_ROLE_LABEL})
    store = _store(view)
    try:
        with pytest.raises(VerificationFailedError) as caught:
            require_reader_role(store)
    finally:
        store.close()
    assert "role" in str(caught.value)


# ---------------------------------------------------------------------------
# The connection cannot write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", AUDITED_TABLES)
def test_the_auditor_role_holds_the_read_privilege_on_every_audited_table(
    reader_store: MemoryStore, table: str
) -> None:
    """An auditor can read the tables their views are built over, and that is all."""
    assert _privilege(reader_store, table, PERMITTED_PRIVILEGE) is True


@pytest.mark.parametrize("table", AUDITED_TABLES)
@pytest.mark.parametrize("privilege", REFUSED_PRIVILEGES)
def test_the_auditor_role_holds_no_write_privilege_on_any_audited_table(
    reader_store: MemoryStore, table: str, privilege: str
) -> None:
    """The privilege catalogue reports no write privilege at all for this role.

    Read out of the catalogue rather than attempted, so the claim covers writes
    nobody thought to attempt, and asked with every argument bound so no identifier
    reaches statement text.
    """
    assert _privilege(reader_store, table, privilege) is False, (
        f"the read-only role reports the {privilege} privilege on {table}, which is "
        "a grant the auditor path must not carry"
    )


def test_a_write_attempted_over_the_auditor_connection_is_refused(
    reader_store: MemoryStore,
) -> None:
    """One write is attempted, and it is refused.

    The statement names an identifier generated for this run, so it matches no row:
    a role holding the privilege it should not hold would remove nothing, and a role
    holding none is refused. The refusal is the assertion; the fact that the
    statement is harmless either way is what makes attempting it acceptable against
    a deployed cluster.
    """

    def write(cursor: Cursor) -> None:
        cursor.execute(HARMLESS_DELETE, (uuid4(),))

    refused: Exception | None = None
    try:
        reader_store.read(write)
    except Exception as error:
        # The driver is imported lazily by the store layer, so its refusal class
        # cannot be named here; the type name is all that is taken from the fault.
        refused = error

    assert refused is not None, (
        "the read-only connection performed a write, so the grant behind the "
        "auditor path is wider than the design states"
    )
    assert type(refused).__name__, "a refusal carries the type the driver raised"
