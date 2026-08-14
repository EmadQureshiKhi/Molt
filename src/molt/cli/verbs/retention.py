"""The retention verb: each Client's regime, and what is about to leave under it."""

from __future__ import annotations

from molt.cli.context import READER_ROLE, VerbContext
from molt.cli.exits import ExitCode
from molt.models.event import JsonObject
from molt.retention import report as retention_report

__all__ = ["run"]


def run(context: VerbContext) -> ExitCode:
    """Print one line per Client, filtered to one Client where a slug is named."""
    emitter = context.emitter
    slug = context.text("client")

    with context.store(role=READER_ROLE) as store:
        lines = retention_report(store)

    selected = tuple(line for line in lines if slug is None or line.slug == slug)
    rows: list[JsonObject] = []
    for line in selected:
        emitter.narrate(
            f"{line.slug} {line.jurisdiction} {line.interval} "
            f"expiring={line.expiring_soon} expired={line.already_expired}"
        )
        rows.append(
            {
                "client_id": str(line.client_id),
                "slug": line.slug,
                "jurisdiction": line.jurisdiction,
                "interval_seconds": int(line.interval.total_seconds()),
                "expiring_soon": line.expiring_soon,
                "already_expired": line.already_expired,
            }
        )
    return emitter.succeed(context.name, {"clients": rows})
