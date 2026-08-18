"""The mcp verb: the tool server on one of its two transports.

The permitted Client set is resolved here, at startup, from the verb's own
arguments and the configuration file, and is then fixed for the life of the
process. No tool argument reaches it, which is what makes the tenancy boundary a
property of the server rather than of a caller's good behaviour.
"""

from __future__ import annotations

import sys
from typing import Final

from molt.cli.context import READER_ROLE, VerbContext
from molt.cli.exits import ExitCode, UsageError
from molt.cli.verbs.common import ProviderEmbedder, integer_overrides, serving
from molt.mcpserver import McpServer
from molt.mcpserver.transport import HttpTransport, serve_stdio
from molt.models.event import JsonObject
from molt.providers.selector import select_embedding_provider

__all__ = ["run"]

_TRANSPORT_KEY: Final[str] = "MOLT_MCP_TRANSPORT"
_BIND_KEY: Final[str] = "MOLT_MCP_BIND"
_MAX_RESULTS_KEY: Final[str] = "MOLT_MCP_MAX_RESULTS"
_PERMITTED_KEY: Final[str] = "MOLT_MCP_PERMITTED_CLIENTS"

# How many requests the hosted transport serves before the verb returns.
_HOSTED_REQUEST_BOUND: Final[int] = 10_000


def run(context: VerbContext) -> ExitCode:
    """Serve the four read-only tools over the chosen transport."""
    emitter = context.emitter
    overrides = integer_overrides(context, {"max_results": _MAX_RESULTS_KEY})
    transport = context.text("transport")
    if transport is not None:
        overrides[_TRANSPORT_KEY] = transport
    bind = context.text("bind")
    if bind is not None:
        overrides[_BIND_KEY] = bind
    slugs = context.repeated("client")
    if slugs:
        # The verb's own list wins over the file, and both are read here rather
        # than anywhere a tool call could reach.
        overrides[_PERMITTED_KEY] = ",".join(slugs)
    configuration = context.configuration_for(overrides)

    embedder = ProviderEmbedder(select_embedding_provider(configuration))
    with context.store(role=READER_ROLE) as store, serving(store):
        server = McpServer.from_configuration(store, embedder, configuration)
        settings = server.settings
        tools = server.tools()
        emitter.warn(f"serving {len(tools)} read-only tools over {settings.transport}")
        document: JsonObject = {
            "transport": settings.transport,
            "bind_host": settings.bind_host,
            "bind_port": settings.bind_port,
            "permitted_clients": len(server.permitted_clients),
            "tools": [str(tool.get("name", "")) for tool in tools],
        }
        if settings.transport == "stdio":
            answered = serve_stdio(server, sys.stdin, sys.stdout)
        elif settings.transport == "http":
            hosted = HttpTransport(server)
            try:
                # A bounded loop rather than an endless one, so the verb ends on
                # its own rather than needing a signal to be shut down. The bound
                # counts answers, so an idle server is not one that has run out.
                hosted.serve_bounded(_HOSTED_REQUEST_BOUND)
            finally:
                hosted.stop()
            # What it answered rather than what it was asked for: a loop stopped
            # early would otherwise report a count it never reached.
            answered = hosted.answered
        else:
            raise UsageError("--transport names either stdio or http")
    document["requests_answered"] = answered
    return emitter.succeed(context.name, document)
