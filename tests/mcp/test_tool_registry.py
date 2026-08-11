"""The registry is four read-only tools, and nothing else is reachable through it."""

from __future__ import annotations

from typing import Final, cast

import pytest

from molt.mcpserver import REGISTRY, TOOL_NAMES, UnknownToolError, tool_named
from molt.mcpserver.tools import (
    ANCESTORS_TOOL,
    DESCENDANTS_TOOL,
    RECALL_TOOL,
    RESIDUE_TOOL,
    ToolBackend,
    ToolEffect,
    dispatch,
)

pytestmark = pytest.mark.mcp

# The four names the requirement obliges, in registry order.
EXPECTED_NAMES: Final[tuple[str, ...]] = (
    RECALL_TOOL,
    ANCESTORS_TOOL,
    DESCENDANTS_TOOL,
    RESIDUE_TOOL,
)

# The argument schema each tool declares, as the names and the required subset.
EXPECTED_SCHEMAS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    RECALL_TOOL: (("query_text", "limit"), ("query_text",)),
    ANCESTORS_TOOL: (("artifact_ids", "limit"), ("artifact_ids",)),
    DESCENDANTS_TOOL: (("artifact_ids", "limit"), ("artifact_ids",)),
    RESIDUE_TOOL: (("run_id", "limit"), ("run_id",)),
}

# Every spelling of a client set a caller might hope a schema declares. None does,
# which is what makes the permitted set unreachable from an argument.
CLIENT_SET_NAMES: Final[frozenset[str]] = frozenset(
    {"client", "clients", "client_id", "client_ids", "client_set", "permitted_clients", "tenant"}
)


def test_the_registry_is_an_immutable_tuple_of_the_four_expected_tools() -> None:
    assert isinstance(REGISTRY, tuple)
    assert TOOL_NAMES == EXPECTED_NAMES
    assert len(REGISTRY) == len(EXPECTED_NAMES)


def test_every_tool_declares_its_schema_its_result_shape_and_a_read_only_effect() -> None:
    for tool in REGISTRY:
        names, required = EXPECTED_SCHEMAS[tool.name]
        assert tuple(argument.name for argument in tool.arguments) == names
        assert tuple(argument.name for argument in tool.arguments if argument.required) == required
        assert tool.result.fields, f"{tool.name} declares no result shape"
        assert tool.effect is ToolEffect.READ_ONLY
        schema = tool.schema()
        assert schema["effect"] == ToolEffect.READ_ONLY.value
        assert schema["name"] == tool.name


def test_no_tool_schema_declares_a_client_set_parameter() -> None:
    for tool in REGISTRY:
        for argument in tool.arguments:
            assert argument.name not in CLIENT_SET_NAMES


def test_the_effect_enumeration_admits_no_mutating_value() -> None:
    assert tuple(ToolEffect) == (ToolEffect.READ_ONLY,)


def test_no_mutation_tool_exists_and_no_absent_name_is_dispatchable() -> None:
    for absent in ("molt.erase", "molt.write", "molt.record_finding", "recall", ""):
        assert tool_named(absent) is None
        with pytest.raises(UnknownToolError):
            _refuse(absent)


def _refuse(name: str) -> None:
    """Ask dispatch for a name the registry lacks, with no backend needed.

    Dispatch resolves the name before it reaches a handler, so the refusal happens
    without a backend and the absence is shown to be structural rather than a
    failure some handler produced. The absent backend is stated as a cast rather
    than silenced, because the point of the case is that the value is never
    touched: a backend supplied here would leave the claim untested, since the
    refusal could then have come from the backend instead of from the registry.
    """
    dispatch(cast("ToolBackend", None), name, {})
