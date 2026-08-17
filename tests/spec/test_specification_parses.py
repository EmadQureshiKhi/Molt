"""The Interface_Specification parses, and it agrees with the routes the code declares.

Every assertion here is bidirectional, which is the only version of this gate worth
having. A route the document describes but the code does not declare is a finding,
because a caller would read a promise nothing keeps. A route the code declares but
the document does not describe is equally a finding, because that is exactly how a
specification rots: the implementation moves and the document stays agreeable.

The document is read as data and the code surfaces are read as values: the console's
own route table, the Collector's route kinds, and the command-line verb tuple. No
route list is restated here, so this module cannot drift into a third opinion about
what the surface is.

Credential-free and database-free. Nothing below builds an application, opens a
socket, or reads a secret: the document is a tracked file and the route table is a
tuple resolved at import.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final, cast

import pytest

from molt.cli.main import ATTEST_SUBCOMMANDS, VERBS
from molt.collector.routes import (
    AUTHENTICATED_KINDS,
    EVENTS_PATH,
    HEALTH_PATH,
    RECALL_PATH,
    SESSIONS_PREFIX,
    SIGNED_KINDS,
    RouteKind,
    method_of,
)
from molt.console.routing import ROUTE_TABLE, RouteSpec

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.spec

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOCUMENT_PATH: Final[Path] = REPOSITORY_ROOT / "docs" / "interface.json"

# The methods an operation may be keyed by, so a key that is not a method is not
# mistaken for one.
METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
)

CONSOLE_SURFACE: Final[str] = "console"
COLLECTOR_SURFACE: Final[str] = "collector"
TOOLS_SURFACE: Final[str] = "tools"

# The four error responses the specification is obliged to name.
REQUIRED_ERROR_RESPONSES: Final[tuple[str, ...]] = (
    "Unauthorized",
    "Forbidden",
    "PayloadTooLarge",
    "Unavailable",
)


def document() -> Mapping[str, Any]:
    """The tracked document, parsed. A parse failure is the first finding."""
    text = DOCUMENT_PATH.read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert isinstance(parsed, dict), "the document's root is an object"
    return cast(Mapping[str, Any], parsed)


DOCUMENT: Final[Mapping[str, Any]] = document()


def operations() -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    """Every operation as its path key, its method, and its body."""
    paths = DOCUMENT["paths"]
    assert isinstance(paths, dict)
    for key, item in paths.items():
        assert isinstance(item, dict), key
        for method, operation in item.items():
            if method.lower() in METHODS:
                assert isinstance(operation, dict), f"{key} {method}"
                yield (key, method.lower(), cast(Mapping[str, Any], operation))


def on_surface(surface: str) -> tuple[tuple[str, str, Mapping[str, Any]], ...]:
    """Every operation belonging to one named surface."""
    return tuple(entry for entry in operations() if entry[2].get("x-molt-surface") == surface)


def console_operations() -> Mapping[str, tuple[str, Mapping[str, Any]]]:
    """The console operations keyed by the route name each one claims."""
    found: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for _key, method, operation in on_surface(CONSOLE_SURFACE):
        name = operation.get("x-molt-route-name")
        assert isinstance(name, str) and name, "every console operation names its route"
        assert name not in found, f"the route name {name!r} is described more than once"
        found[name] = (method, operation)
    return found


def has_request_shape(operation: Mapping[str, Any]) -> bool:
    """Whether the operation states its request shape rather than leaving it unsaid.

    A body or a parameter is a request shape. So is the explicit statement that the
    request carries neither, which is what keeps silence from passing for a shape.
    """
    if operation.get("requestBody") is not None:
        return True
    parameters = operation.get("parameters")
    if isinstance(parameters, list) and parameters:
        return True
    return operation.get("x-molt-request-shape") == "empty"


def response_shapes(operation: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Every schema a success response of this operation carries."""
    responses = operation.get("responses")
    assert isinstance(responses, dict), "every operation states its responses"
    shapes: list[Mapping[str, Any]] = []
    for status, response in responses.items():
        if not status.startswith(("2", "3")):
            continue
        content = cast(Mapping[str, Any], response).get("content")
        if not isinstance(content, dict):
            continue
        for media in content.values():
            schema = cast(Mapping[str, Any], media).get("schema")
            if isinstance(schema, dict):
                shapes.append(cast(Mapping[str, Any], schema))
    return tuple(shapes)


def error_statuses(operation: Mapping[str, Any]) -> frozenset[str]:
    """The failure statuses this operation declares."""
    responses = cast(Mapping[str, Any], operation["responses"])
    return frozenset(status for status in responses if status.startswith(("4", "5")))


def resolve(reference: str) -> Mapping[str, Any]:
    """One local reference, walked from the document root."""
    assert reference.startswith("#/"), reference
    node: Any = DOCUMENT
    for step in reference.removeprefix("#/").split("/"):
        assert isinstance(node, dict) and step in node, reference
        node = node[step]
    return cast(Mapping[str, Any], node)


def references(node: object) -> Iterator[str]:
    """Every reference anywhere in the document, so none of them can dangle."""
    if isinstance(node, dict):
        for key, value in cast(Mapping[str, Any], node).items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from references(value)
    elif isinstance(node, list):
        for item in node:
            yield from references(item)


# -- the document itself ---------------------------------------------------


def test_the_document_parses_and_declares_its_shape() -> None:
    assert DOCUMENT["openapi"].startswith("3."), "the document states its own version"
    info = DOCUMENT["info"]
    assert isinstance(info, dict)
    for field in ("title", "version", "description"):
        assert isinstance(info.get(field), str) and info[field], field
    assert isinstance(DOCUMENT["paths"], dict) and DOCUMENT["paths"]


def test_the_four_error_responses_are_named_once_and_reused() -> None:
    declared = DOCUMENT["components"]["responses"]
    for name in REQUIRED_ERROR_RESPONSES:
        assert name in declared, f"the document names the {name} response"
        assert isinstance(declared[name].get("description"), str)


def test_the_three_authentication_requirements_are_declared() -> None:
    kinds = DOCUMENT["x-molt-authentication-kinds"]
    for kind in ("none", "bearer", "bearer+signature", "session"):
        assert kind in kinds, kind
    for _key, _method, operation in operations():
        requirement = operation.get("x-molt-authentication")
        assert requirement in kinds, f"{operation.get('operationId')}: {requirement!r}"


def test_every_reference_resolves() -> None:
    for reference in references(DOCUMENT):
        resolve(reference)


def test_every_operation_is_identified_once_and_states_its_surface_and_path() -> None:
    seen: set[str] = set()
    for key, _method, operation in operations():
        identifier = operation.get("operationId")
        assert isinstance(identifier, str) and identifier, key
        assert identifier not in seen, f"the operation identifier {identifier!r} repeats"
        seen.add(identifier)
        assert operation.get("x-molt-surface") in {
            CONSOLE_SURFACE,
            COLLECTOR_SURFACE,
            TOOLS_SURFACE,
        }, key
        assert isinstance(operation.get("x-molt-path"), str), key
        assert isinstance(operation.get("x-molt-mutation"), bool), key
        assert isinstance(operation.get("summary"), str) and operation["summary"], key


# -- the console route table, in both directions ---------------------------


@pytest.mark.parametrize("route", ROUTE_TABLE, ids=lambda route: route.name)
def test_every_declared_console_route_is_described(route: RouteSpec) -> None:
    described = console_operations()
    assert route.name in described, (
        f"the route table declares {route.name!r} and the document describes no such route"
    )
    method, operation = described[route.name]
    assert method == route.method.lower(), route.name
    assert operation["x-molt-path"] == route.path, route.name
    assert operation["x-molt-mutation"] is route.mutation, route.name
    expected = "none" if route.public else "session"
    assert operation["x-molt-authentication"] == expected, route.name
    assert has_request_shape(operation), f"{route.name} states no request shape"
    assert response_shapes(operation), f"{route.name} states no response shape"
    assert error_statuses(operation), f"{route.name} states no error response"


@pytest.mark.parametrize("route", ROUTE_TABLE, ids=lambda route: route.name)
def test_every_authenticated_route_names_the_refusals_it_can_answer(route: RouteSpec) -> None:
    if route.public:
        pytest.skip("a public route refuses no caller for want of a session")
    statuses = error_statuses(console_operations()[route.name][1])
    assert {"401", "403"} <= statuses, route.name


def test_the_document_describes_no_console_route_the_table_does_not_declare() -> None:
    declared = {route.name for route in ROUTE_TABLE}
    described = set(console_operations())
    assert described - declared == set(), (
        "the document describes console routes the route table does not declare: "
        + ", ".join(sorted(described - declared))
    )


def test_the_console_route_set_matches_exactly() -> None:
    assert set(console_operations()) == {route.name for route in ROUTE_TABLE}


def test_the_memory_tier_route_names_its_columns_and_its_two_expiry_figures() -> None:
    _method, operation = console_operations()["tiers"]
    shapes = response_shapes(operation)
    assert shapes, "the tier view states a response shape"
    schema = shapes[0]
    resolved = resolve(schema["$ref"]) if "$ref" in schema else schema
    properties = cast(Mapping[str, Any], resolved["properties"])
    assert "working_expired_rows" in properties, "the working tier's expired count"
    assert "working_next_sweep_seconds" in properties, "the working tier's next sweep"
    row = cast(Mapping[str, Any], properties["tiers"])["items"]
    columns = set(cast(Mapping[str, Any], row["properties"]))
    assert {"tier", "purpose", "mutability", "capability", "retention"} <= columns
    assert "live_rows" in columns, "the live row count is read per tier"


# -- the Collector routes, in both directions ------------------------------


def _collector_paths() -> Mapping[RouteKind, str]:
    """The served path of each Collector route, taken from the module's constants."""
    return {
        RouteKind.EVENTS: EVENTS_PATH,
        RouteKind.SESSION: f"{SESSIONS_PREFIX}{{session_id}}",
        RouteKind.RECALL: RECALL_PATH,
        RouteKind.HEALTH: HEALTH_PATH,
    }


@pytest.mark.parametrize("kind", tuple(RouteKind), ids=lambda kind: kind.value)
def test_every_collector_route_is_described_with_its_requirement(kind: RouteKind) -> None:
    served = _collector_paths()[kind]
    matches = [
        (method, operation)
        for _key, method, operation in on_surface(COLLECTOR_SURFACE)
        if operation["x-molt-path"] == served
    ]
    assert len(matches) == 1, f"the {kind.value} route is described exactly once"
    method, operation = matches[0]
    assert method == method_of(kind).lower(), kind.value
    if kind in SIGNED_KINDS:
        expected = "bearer+signature"
    elif kind in AUTHENTICATED_KINDS:
        expected = "bearer"
    else:
        expected = "none"
    assert operation["x-molt-authentication"] == expected, kind.value
    assert has_request_shape(operation), kind.value
    assert response_shapes(operation), kind.value
    assert error_statuses(operation), kind.value


def test_the_document_describes_no_collector_route_the_code_does_not_serve() -> None:
    served = set(_collector_paths().values())
    described = {operation["x-molt-path"] for _key, _m, operation in on_surface(COLLECTOR_SURFACE)}
    assert described == served, (
        "the collector routes disagree: described only "
        + ", ".join(sorted(described - served))
        + "; served only "
        + ", ".join(sorted(served - described))
    )


def test_every_credentialled_collector_route_names_the_body_bound_refusal() -> None:
    for _key, _method, operation in on_surface(COLLECTOR_SURFACE):
        if operation["x-molt-authentication"] == "none":
            continue
        statuses = error_statuses(operation)
        assert {"401", "403", "413", "503"} <= statuses, operation["operationId"]


# -- the command-line verbs, in both directions ----------------------------


def test_the_verb_list_matches_the_argument_tree_in_both_directions() -> None:
    described = {entry["name"] for entry in cast(list[Any], DOCUMENT["x-molt-cli"]["verbs"])}
    declared = set(VERBS) | {f"attest {word}" for word in ATTEST_SUBCOMMANDS}
    assert described == declared, (
        "the verb list disagrees with the argument tree: described only "
        + ", ".join(sorted(described - declared))
        + "; declared only "
        + ", ".join(sorted(declared - described))
    )


def test_every_described_verb_carries_a_summary() -> None:
    for entry in cast(list[Any], DOCUMENT["x-molt-cli"]["verbs"]):
        assert isinstance(entry.get("summary"), str) and entry["summary"], entry.get("name")
