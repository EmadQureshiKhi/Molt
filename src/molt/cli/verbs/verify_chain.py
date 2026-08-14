"""The verify-chain verb: verified row counts and terminal digests, or the first mismatch.

Every chain is recomputed from the stored rows rather than compared against a
stored summary, which is the whole point: a summary a tamper could rewrite would
verify anything.
"""

from __future__ import annotations

from uuid import UUID

from molt.cli.context import READER_ROLE, VerbContext, client_id_for
from molt.cli.exits import ExitCode, UsageError
from molt.models.event import JsonObject, JsonValue
from molt.store.chain import ChainReport, verify_chain
from molt.store.sessions import sessions_of_client

__all__ = ["run"]


def run(context: VerbContext) -> ExitCode:
    """Verify one Session's chain, or every Session of one Client."""
    emitter = context.emitter
    session = context.text("session_id")
    slug = context.text("client")
    if (session is None) == (slug is None):
        raise UsageError("name exactly one of --session-id or --client")

    reports: list[ChainReport] = []
    with context.store(role=READER_ROLE) as store:
        if session is not None:
            reports.append(verify_chain(store, _identifier(session)))
        else:
            client_id = client_id_for(store, str(slug))
            for record in sessions_of_client(store, client_id):
                reports.append(verify_chain(store, record.id))

    rows: list[JsonValue] = []
    for report in reports:
        emitter.narrate(
            f"{report.session_id} rows={report.rows} terminal={report.terminal_digest} "
            f"{'intact' if report.ok else f'mismatch at {report.first_mismatch_seq}'}"
        )
        rows.append(
            {
                "session_id": str(report.session_id),
                "ok": report.ok,
                "rows": report.rows,
                "terminal_digest": report.terminal_digest,
                "first_mismatch_seq": report.first_mismatch_seq,
                "mismatch": report.mismatch,
            }
        )

    document: JsonObject = {"chains": rows, "verified_rows": sum(report.rows for report in reports)}
    if all(report.ok for report in reports):
        return emitter.succeed(context.name, document)
    emitter.emit(
        {
            "verb": context.name,
            "ok": False,
            "exit_code": int(ExitCode.VERIFICATION_FAILED),
            **document,
        }
    )
    return ExitCode.VERIFICATION_FAILED


def _identifier(raw: str) -> UUID:
    """One Session identifier, refusing text that is not one."""
    try:
        return UUID(raw)
    except ValueError as exc:
        raise UsageError("--session-id names an identifier") from exc
