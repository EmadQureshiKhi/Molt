"""The permitted Client set comes from configuration, and an argument cannot name one."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.mcp.harness import (
    READER_ROLE,
    Artifact,
    Corpus,
    RecordingSink,
    StubEmbedder,
    build_server,
    fake_store,
)

from molt.config.resolve import Configuration
from molt.mcpserver import McpServer, ReaderRoleRequiredError
from molt.mcpserver.tools import (
    ANCESTORS_TOOL,
    PERMITTED_CLIENT_IDS_STATEMENT,
    McpSettings,
)
from molt.models.artifact import ArtifactKind
from molt.models.event import JsonValue
from molt.store import Connection, MemoryStore

pytestmark = pytest.mark.mcp


def corpus_of(permitted_count: int) -> Corpus:
    """One corpus whose Artifacts are each held by exactly one of its Clients."""
    clients = tuple(uuid4() for _ in range(permitted_count + 1))
    return Corpus(
        clients=clients,
        permitted=clients[:permitted_count],
        slugs=tuple(f"tenant-{index}" for index in range(permitted_count)),
        artifacts=tuple(
            Artifact(
                artifact_id=uuid4(),
                kind=ArtifactKind.DERIVED_ARTIFACT,
                bound_clients=frozenset({client}),
            )
            for client in clients
        ),
    )


def configuration_for(corpus: Corpus, *, max_results: int = 7) -> Configuration:
    """A configuration view naming the permitted slugs and the bound, and nothing else."""
    return Configuration(
        environ={},
        file_values={
            "mcp.transport": "stdio",
            "mcp.bind": "127.0.0.1:8090",
            "mcp.permitted_clients": list(corpus.slugs),
            "mcp.max_results": max_results,
        },
    )


def test_the_settings_read_every_value_from_the_configuration_surface() -> None:
    corpus = corpus_of(2)

    settings = McpSettings.from_configuration(configuration_for(corpus, max_results=9))

    assert settings.permitted_client_slugs == corpus.slugs
    assert settings.max_results == 9
    assert settings.transport == "stdio"
    assert (settings.bind_host, settings.bind_port) == ("127.0.0.1", 8090)


def test_the_permitted_set_is_resolved_from_the_configured_slugs_at_startup() -> None:
    corpus = corpus_of(2)
    _, log = build_server(corpus)
    store = fake_store(corpus, log)

    server = McpServer.from_configuration(
        store,
        StubEmbedder(),
        configuration_for(corpus),
        sink=RecordingSink(),
    )

    assert server.permitted_clients == corpus.permitted
    assert PERMITTED_CLIENT_IDS_STATEMENT in log.statements()
    resolution = next(sent for sent in log.sent if sent.statement == PERMITTED_CLIENT_IDS_STATEMENT)
    assert resolution.parameters[0] == list(corpus.slugs)


def test_an_argument_naming_a_client_set_is_ignored() -> None:
    corpus = corpus_of(1)
    server, log = build_server(corpus, max_results=5, sink=RecordingSink())
    outside = corpus.unpermitted[0]
    hidden = corpus.hidden_ids()

    named: list[JsonValue] = [str(found) for found in hidden]
    widened = server.invoke(
        ANCESTORS_TOOL,
        {
            "artifact_ids": named,
            "permitted_clients": [str(outside)],
            "client_ids": [str(outside)],
        },
    )
    plain = server.invoke(ANCESTORS_TOOL, {"artifact_ids": named})

    # The widening argument changed nothing: the same call without it answers the
    # same rows, and every row is one the configured set admits.
    assert widened.rows == plain.rows
    visible = {str(found) for found in corpus.visible_ids()}
    assert {str(row["artifact_id"]) for row in widened.rows} <= visible
    assert server.permitted_clients == corpus.permitted
    for sent in log.sent:
        assert outside not in sent.parameters
        assert str(outside) not in str(sent.parameters)


def test_a_store_that_is_not_the_reader_role_builds_no_server() -> None:
    corpus = corpus_of(1)
    _, log = build_server(corpus)

    def connect() -> Connection:
        raise AssertionError("no connection is opened before the role is refused")

    writer = MemoryStore(connect_with=connect, role="molt_writer")
    with pytest.raises(ReaderRoleRequiredError):
        McpServer.from_configuration(writer, StubEmbedder(), configuration_for(corpus))

    assert fake_store(corpus, log).role == READER_ROLE
