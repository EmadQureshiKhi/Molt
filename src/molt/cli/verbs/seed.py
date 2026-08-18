"""The seed verb: a deterministic corpus, with its ground truth kept separate.

The mapping of planted fragments to their owners is written to a file of its own
rather than into the corpus, because a corpus that carried the answers would make
every recall measurement over it meaningless.

`--reset` removes the seeded corpus before the generation runs. It is scoped to the
tenants the corpus definition names and it removes their memory content -- working
rows, vectors, derivation edges, attribution claims, Artifacts, Events, and Sessions
-- leaving the tenant rows themselves in place for the generation to write into. A
stored tenant carrying a seeded slug with any other identity ends the verb with a
refusal and no delete, so the flag cannot empty a real tenant's memory because the
verb was pointed at the wrong database. What went is reported per table, one line
each and the same counts in the one machine-readable object, and a reset of a corpus
that is already gone reports zero rather than failing.
"""

from __future__ import annotations

from molt.cli.context import VerbContext
from molt.cli.exits import ExitCode
from molt.models.event import JsonObject
from molt.seed.contaminate import plant_contamination
from molt.seed.corpora import SeedVolumes
from molt.seed.generator import generate
from molt.seed.reset import ResetReport, reset_corpus
from molt.store import MemoryStore

__all__ = ["run"]


def run(context: VerbContext) -> ExitCode:
    """Reset the corpus where asked, generate it, plant the fragments, and report.

    The reset and the generation share one connection, and the reset commits before
    the first row of the new corpus is written, so a run under the flag seeds into a
    corpus it emptied rather than on top of one it warned about.
    """
    emitter = context.emitter
    defaults = SeedVolumes()
    volumes = SeedVolumes(
        clients=context.integer("clients", defaults.clients),
        sessions=context.integer("sessions", defaults.sessions),
        events=context.integer("events", defaults.events),
    )

    with MemoryStore.from_configuration(context.configuration) as store:
        removed = reset_corpus(store) if context.flag("reset") else None
        result = generate(store, seed=context.integer("seed", 0), volumes=volumes)
        truth = plant_contamination(
            store,
            result,
            volumes=volumes,
            path=context.path("ground_truth"),
            configuration=context.configuration,
        )

    document: JsonObject = {
        "seed": result.seed,
        "clients": len(result.clients),
        "sessions": len(result.sessions),
        "events": result.events,
        "embeddings": result.embeddings,
        "artifacts": len(result.artifacts),
        "blended_artifacts": len(result.blended_artifacts),
        "working_rows": result.working_rows,
        "planted_fragments": len(truth.fragments),
    }
    if removed is not None:
        document["reset"] = removed.as_document()
        _narrate_reset(context, removed)
    emitter.narrate(
        f"seeded {len(result.clients)} clients, {len(result.sessions)} sessions, "
        f"{result.events} events"
    )
    return emitter.succeed(context.name, document)


def _narrate_reset(context: VerbContext, removed: ResetReport) -> None:
    """Report what the reset removed, per table and in total, as narration."""
    emitter = context.emitter
    for line in removed.lines():
        emitter.narrate(line)
    emitter.narrate(
        f"reset removed {removed.total} row(s) in all "
        f"from {len(removed.client_slugs)} seeded client(s)"
    )
