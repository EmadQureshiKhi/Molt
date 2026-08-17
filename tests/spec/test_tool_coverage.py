"""Every Molt_MCP_Server tool the registry exposes is described, and nothing else is.

The registry is the dispatchable surface: a name absent from it is unreachable, and a
name present in it can be called by any connected client. So the document has to name
exactly that set, and it has to name each tool's arguments, which of them are owed,
the row it returns, and the fields of that row. A described argument the registry does
not accept would send a caller to a refusal; an accepted argument the document omits
would hide the surface the caller is entitled to use.

The tool operations are read out of the same `paths` object as the routes, because the
document deliberately covers both surfaces rather than splitting the vocabulary. They
are told apart by their surface marker and matched to the registry by name.

Credential-free and database-free: the registry is a tuple resolved at import and no
handler is called.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final, cast

import pytest

from molt.mcpserver.tools import REGISTRY, TOOL_NAMES, Tool

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.spec

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOCUMENT_PATH: Final[Path] = REPOSITORY_ROOT / "docs" / "interface.json"

TOOLS_SURFACE: Final[str] = "tools"
JSON_MEDIA_TYPE: Final[str] = "application/json"

DOCUMENT: Final[Mapping[str, Any]] = cast(
    Mapping[str, Any], json.loads(DOCUMENT_PATH.read_text(encoding="utf-8"))
)


def tool_operations() -> Mapping[str, Mapping[str, Any]]:
    """The document's tool operations, keyed by the tool name each one claims."""
    found: dict[str, Mapping[str, Any]] = {}
    for item in cast(Mapping[str, Any], DOCUMENT["paths"]).values():
        for operation in cast(Mapping[str, Any], item).values():
            if not isinstance(operation, dict):
                continue
            body = cast(Mapping[str, Any], operation)
            if body.get("x-molt-surface") != TOOLS_SURFACE:
                continue
            name = body.get("x-molt-tool-name")
            assert isinstance(name, str) and name, "every tool operation names its tool"
            assert name not in found, f"the tool {name!r} is described more than once"
            found[name] = body
    return found


def resolve(reference: str) -> Mapping[str, Any]:
    """One local reference, walked from the document root."""
    node: Any = DOCUMENT
    for step in reference.removeprefix("#/").split("/"):
        assert isinstance(node, dict) and step in node, reference
        node = node[step]
    return cast(Mapping[str, Any], node)


def dereferenced(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """A schema with its reference followed, if it is one."""
    reference = schema.get("$ref")
    return resolve(reference) if isinstance(reference, str) else schema


def argument_schema(operation: Mapping[str, Any]) -> Mapping[str, Any]:
    """The object schema one tool's arguments are described by."""
    body = operation.get("requestBody")
    assert isinstance(body, dict), f"{operation['operationId']} describes no arguments"
    content = cast(Mapping[str, Any], cast(Mapping[str, Any], body)["content"])
    media = cast(Mapping[str, Any], content[JSON_MEDIA_TYPE])
    return dereferenced(cast(Mapping[str, Any], media["schema"]))


def result_row_schema(operation: Mapping[str, Any]) -> Mapping[str, Any]:
    """The row schema one tool's success response returns an array of."""
    responses = cast(Mapping[str, Any], operation["responses"])
    success = cast(Mapping[str, Any], responses["200"])
    media = cast(Mapping[str, Any], cast(Mapping[str, Any], success["content"])[JSON_MEDIA_TYPE])
    answer = dereferenced(cast(Mapping[str, Any], media["schema"]))
    rows = cast(Mapping[str, Any], cast(Mapping[str, Any], answer["properties"])["rows"])
    return dereferenced(cast(Mapping[str, Any], rows["items"]))


def registry_tools() -> Iterator[Tool]:
    """The registry, as the values dispatch consults."""
    yield from REGISTRY


# -- the tool set, in both directions --------------------------------------


def test_the_registry_and_the_document_name_the_same_tools() -> None:
    described = set(tool_operations())
    exposed = set(TOOL_NAMES)
    assert described == exposed, (
        "the tool sets disagree: described only "
        + ", ".join(sorted(described - exposed))
        + "; exposed only "
        + ", ".join(sorted(exposed - described))
    )


def test_the_registry_is_the_name_tuple_it_claims_to_be() -> None:
    assert tuple(tool.name for tool in REGISTRY) == TOOL_NAMES


@pytest.mark.parametrize("tool", tuple(registry_tools()), ids=lambda tool: tool.name)
def test_every_exposed_tool_is_described(tool: Tool) -> None:
    described = tool_operations()
    assert tool.name in described, (
        f"the registry exposes {tool.name!r} and the document describes no such tool"
    )
    operation = described[tool.name]
    assert operation["summary"] == tool.summary, tool.name
    assert operation["x-molt-effect"] == tool.effect.value, tool.name
    assert operation["x-molt-mutation"] is False, "no tool mutates, so none may say it does"


@pytest.mark.parametrize("tool", tuple(registry_tools()), ids=lambda tool: tool.name)
def test_every_argument_is_described_with_its_shape_and_standing(tool: Tool) -> None:
    schema = argument_schema(tool_operations()[tool.name])
    properties = cast(Mapping[str, Any], schema["properties"])
    assert set(properties) == {argument.name for argument in tool.arguments}, tool.name
    for argument in tool.arguments:
        described = cast(Mapping[str, Any], properties[argument.name])
        assert described.get("x-molt-kind") == argument.kind.value, argument.name
        assert described.get("description") == argument.description, argument.name
    required = set(cast(list[str], schema.get("required", [])))
    assert required == {arg.name for arg in tool.arguments if arg.required}, tool.name


@pytest.mark.parametrize("tool", tuple(registry_tools()), ids=lambda tool: tool.name)
def test_every_result_row_is_described_field_for_field(tool: Tool) -> None:
    row = result_row_schema(tool_operations()[tool.name])
    assert row.get("x-molt-row") == tool.result.row, tool.name
    assert set(cast(Mapping[str, Any], row["properties"])) == set(tool.result.fields), tool.name


@pytest.mark.parametrize("tool", tuple(registry_tools()), ids=lambda tool: tool.name)
def test_every_tool_names_its_refusal_and_its_unavailability(tool: Tool) -> None:
    responses = cast(Mapping[str, Any], tool_operations()[tool.name]["responses"])
    statuses = {status for status in responses if status.startswith(("4", "5"))}
    assert "403" in statuses, f"{tool.name} names no refusal for an unpermitted Client"
    assert "503" in statuses, f"{tool.name} names no unavailability"


def test_the_document_describes_no_argument_the_registry_would_refuse() -> None:
    by_name = {tool.name: tool for tool in REGISTRY}
    for name, operation in tool_operations().items():
        accepted = {argument.name for argument in by_name[name].arguments}
        described = set(cast(Mapping[str, Any], argument_schema(operation)["properties"]))
        assert described - accepted == set(), (
            f"{name} is described as accepting arguments the registry does not: "
            + ", ".join(sorted(described - accepted))
        )


def test_no_tool_is_described_as_carrying_a_client_set_argument() -> None:
    """Tenancy is configured, so no described argument may look like a way to widen it."""
    for name, operation in tool_operations().items():
        described = set(cast(Mapping[str, Any], argument_schema(operation)["properties"]))
        assert not {"client", "clients", "client_id", "client_set"} & described, name
