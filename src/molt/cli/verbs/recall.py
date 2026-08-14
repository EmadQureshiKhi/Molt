"""The recall verb: ranked prior work, with the distances it was ranked on."""

from __future__ import annotations

from uuid import UUID

from molt.cli.context import READER_ROLE, VerbContext, client_id_for
from molt.cli.exits import ExitCode, UsageError
from molt.cli.verbs.common import ProviderEmbedder
from molt.models.event import JsonObject
from molt.providers.selector import select_embedding_provider
from molt.recall import RecallEngine

__all__ = ["run"]


def run(context: VerbContext) -> ExitCode:
    """Print the ranked page with distances, outcomes, machines, and timestamps."""
    emitter = context.emitter
    query = context.text("query")
    if not query:
        raise UsageError("a recall names the query it is asking about")
    session = context.text("session_id")
    session_id = _identifier(session) if session is not None else None

    embedder = ProviderEmbedder(select_embedding_provider(context.configuration))
    with context.store(role=READER_ROLE) as store:
        permitted = tuple(client_id_for(store, slug) for slug in context.repeated("client"))
        engine = RecallEngine.from_configuration(store, embedder, context.configuration)
        results = engine.recall(
            query,
            context.integer("k", 10),
            permitted=permitted,
            session_id=session_id,
        )

    rows: list[JsonObject] = []
    for result in results:
        document = result.as_document()
        emitter.narrate(str(document))
        rows.append(document)
    return emitter.succeed(context.name, {"count": len(rows), "results": rows})


def _identifier(raw: str) -> UUID:
    """One Session identifier, refusing text that is not one."""
    try:
        return UUID(raw)
    except ValueError as exc:
        raise UsageError("--session-id names an identifier") from exc
