"""What the residue view and the erasure console both need, held once.

Three things are shared and nothing else: the fleet read that fills the Client
select, the configuration surface the thresholds resolve from, and the template
environment the application put on its own state. Each is here rather than twice,
because a second spelling of the fleet statement would be a second thing to keep in
step with the schema.

The configuration is read through the application state when a deployment or a test
put one there and resolved from the surface otherwise, so a credential-free test
drives these views against values of its own without a cluster and without a
parameter store.

Every statement is a whole module-level literal with bound parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast
from uuid import UUID

from starlette.requests import Request
from starlette.templating import Jinja2Templates

from molt.config.resolve import Configuration, load_configuration
from molt.errors import StoreError
from molt.store import Cursor, MemoryStore

__all__ = [
    "CONFIGURATION_STATE_KEY",
    "SELECT_FLEET_STATEMENT",
    "TEMPLATES_STATE_KEY",
    "UNAVAILABLE_STATUS",
    "ClientRow",
    "configuration_of",
    "fleet",
    "templates_of",
]

# Where the application puts the template environment, and where a deployment or a
# test may put a resolved configuration for these views to read.
TEMPLATES_STATE_KEY: Final[str] = "molt_templates"
CONFIGURATION_STATE_KEY: Final[str] = "molt_configuration"

UNAVAILABLE_STATUS: Final[int] = 503

# The fleet, ordered by slug so the select's option order is total and stable. No
# memory content appears: a Client's slug and display name are tenancy labels.
SELECT_FLEET_STATEMENT: Final[str] = "SELECT id, slug, display_name FROM client ORDER BY slug"

_FLEET_ROW_WIDTH: Final[int] = 3


@dataclass(frozen=True, slots=True)
class ClientRow:
    """One Client of the fleet, as the Client select renders it."""

    client_id: UUID
    slug: str
    display_name: str


def fleet(store: MemoryStore) -> tuple[ClientRow, ...]:
    """Every Client an operator may pick, read at request time on a leased connection."""

    def body(cursor: Cursor) -> tuple[ClientRow, ...]:
        cursor.execute(SELECT_FLEET_STATEMENT, ())
        return tuple(_client_row(row) for row in cursor.fetchall())

    return store.read(body)


def _client_row(row: object) -> ClientRow:
    """One fleet row, refusing a width the statement does not project."""
    carried = cast("tuple[object, ...]", row)
    if len(carried) != _FLEET_ROW_WIDTH:
        raise StoreError(f"a fleet row carried {len(carried)} columns rather than 3")
    identifier = carried[0]
    return ClientRow(
        client_id=identifier if isinstance(identifier, UUID) else UUID(str(identifier)),
        slug=str(carried[1]),
        display_name=str(carried[2]),
    )


def configuration_of(request: Request) -> Configuration:
    """The configuration surface these views read their thresholds from."""
    held = getattr(request.app.state, CONFIGURATION_STATE_KEY, None)
    if held is None:
        return load_configuration()
    return cast(Configuration, held)


def templates_of(request: Request) -> Jinja2Templates | None:
    """The template environment, or None where the asset directory is absent."""
    return cast("Jinja2Templates | None", getattr(request.app.state, TEMPLATES_STATE_KEY, None))
