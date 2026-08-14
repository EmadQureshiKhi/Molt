"""The console's read-only handle: why it exists, and what it refuses.

The deployed console function authenticates as the eraser role, because the erasure
console runs erasures from that same function. The Sensitivity_Analyzer refuses any
connection but the read-only one, so that its no-mutation claim rests on a privilege the
cluster enforces rather than on this code being careful. Those two facts cannot both be
served by one connection.

So the console opens a second handle, and the cases here state the three answers that
matter: a console already holding the read-only role opens nothing extra, a console
holding a wider role uses the configured read-only connection, and a console holding a
wider role with none configured reports the analysis unavailable rather than running it
under a role that can write.

The last one is the case worth having. Falling back to the wider handle would leave every
assertion about the analysis passing while quietly removing the guarantee the analysis
is for.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.deps import (
    Console,
    ConsoleSettings,
    ReaderRoleUnavailableError,
    reader_store_factory,
)
from molt.erase.sensitivity import READ_ONLY_ROLE, store_residue_walk
from molt.errors import StoreError
from molt.store import READER_DSN_PARAM_KEY, READER_ROLE_NAMES

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

ERASER_ROLE: Final[str] = "eraser"
QUALIFIED_READER: Final[str] = "molt_reader"


class RoledStore:
    """A store that reports a role and answers nothing, which is all these cases read."""

    def __init__(self, role: str) -> None:
        self.role = role

    def read(self, body: Callable[[Any], object]) -> object:
        del body
        return None


def _console(
    role: str,
    *,
    reader_factory: Callable[[], Any] | None = None,
) -> Console:
    root = Path(__file__).resolve().parents[2]
    settings = ConsoleSettings(
        host="127.0.0.1",
        port=8080,
        demo_mode=False,
        interface_spec_path=root / "docs" / "interface.json",
        template_directory=root / "web" / "templates",
        static_directory=root / "web" / "static",
    )
    return Console(
        settings=settings,
        store=cast(Any, RoledStore(role)),
        credential=Credential(
            auth.credential_record(CREDENTIAL, iterations=2),
            source_name="test",
            source=CredentialSource.ENVIRONMENT,
        ),
        session_key=Credential(
            SESSION_KEY, source_name="test", source=CredentialSource.ENVIRONMENT
        ),
        clock=lambda: NOW,
        reader_factory=cast(Any, reader_factory),
    )


# ---------------------------------------------------------------------------
# The three answers
# ---------------------------------------------------------------------------


def test_a_console_already_read_only_opens_no_second_connection() -> None:
    """A read-only deployment needs no second handle, so it is handed its own."""
    for role in sorted(READER_ROLE_NAMES):
        console = _console(role)
        assert console.reader_store() is console.store, role


def test_a_wider_console_uses_the_configured_read_only_connection() -> None:
    opened: list[RoledStore] = []

    def factory() -> RoledStore:
        built = RoledStore(QUALIFIED_READER)
        opened.append(built)
        return built

    console = _console(ERASER_ROLE, reader_factory=factory)
    first = console.reader_store()

    assert first.role == QUALIFIED_READER
    assert first is not console.store
    # Opened once and remembered, so a page rendering several views costs one connection.
    assert console.reader_store() is first
    assert len(opened) == 1


def test_a_wider_console_with_no_reader_configured_reports_it_unavailable() -> None:
    """The refusal is the point: no fallback to the handle that can write."""
    console = _console(ERASER_ROLE)
    with pytest.raises(ReaderRoleUnavailableError) as raised:
        console.reader_store()
    message = str(raised.value)
    assert ERASER_ROLE in message
    assert READER_DSN_PARAM_KEY in message, "the refusal does not name the value to provision"


def test_a_configured_connection_reporting_a_wider_role_is_refused() -> None:
    """The role is checked on what was opened, not on what was intended."""
    console = _console(ERASER_ROLE, reader_factory=lambda: RoledStore(ERASER_ROLE))
    with pytest.raises(ReaderRoleUnavailableError):
        console.reader_store()


# ---------------------------------------------------------------------------
# The analyser's own refusal, which is what the handle exists to satisfy
# ---------------------------------------------------------------------------


def test_the_analyser_admits_both_spellings_of_the_read_only_role() -> None:
    """The migrations create `molt_reader` and a surface commonly names `reader`.

    A check against one spelling alone refused the very deployment the grants were
    written for, which made the analysis unreachable wherever it was provisioned.
    """
    assert READ_ONLY_ROLE in READER_ROLE_NAMES
    assert QUALIFIED_READER in READER_ROLE_NAMES
    for role in sorted(READER_ROLE_NAMES):
        walk = store_residue_walk(cast(Any, RoledStore(role)), _identifier(), permitted_clients=())
        assert callable(walk), role


def test_the_analyser_refuses_a_role_that_can_write() -> None:
    with pytest.raises(StoreError) as raised:
        store_residue_walk(cast(Any, RoledStore(ERASER_ROLE)), _identifier(), permitted_clients=())
    assert ERASER_ROLE in str(raised.value)


def _identifier() -> UUID:
    """One synthetic run identifier, which the read-only walk names and never writes."""
    return uuid4()


# ---------------------------------------------------------------------------
# Resolution from the surface
# ---------------------------------------------------------------------------


def test_a_surface_naming_no_reader_parameter_yields_no_factory() -> None:
    """Absence is absence: the key carries no default because it names a credential."""
    assert reader_store_factory(Configuration(environ={}, file_values={})) is None


def test_a_surface_naming_a_reader_parameter_yields_a_factory() -> None:
    surface = Configuration(
        environ={READER_DSN_PARAM_KEY: "/molt/store/dsn/reader"}, file_values={}
    )
    assert reader_store_factory(surface) is not None
