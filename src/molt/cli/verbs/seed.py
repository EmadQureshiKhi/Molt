"""The seed verb: a deterministic corpus, with its ground truth kept separate.

The mapping of planted fragments to their owners is written to a file of its own
rather than into the corpus, because a corpus that carried the answers would make
every recall measurement over it meaningless.
"""

from __future__ import annotations

from molt.cli.context import VerbContext
from molt.cli.exits import ExitCode
from molt.models.event import JsonObject
from molt.seed.contaminate import plant_contamination
from molt.seed.corpora import SeedVolumes
from molt.seed.generator import generate
from molt.store import MemoryStore

__all__ = ["run"]


def run(context: VerbContext) -> ExitCode:
    """Generate the corpus, plant the fragments, and report where the mapping landed."""
    emitter = context.emitter
    defaults = SeedVolumes()
    volumes = SeedVolumes(
        clients=context.integer("clients", defaults.clients),
        sessions=context.integer("sessions", defaults.sessions),
        events=context.integer("events", defaults.events),
    )
    if context.flag("reset"):
        emitter.warn("--reset removes no row in this build; seed into an empty corpus instead")

    with MemoryStore.from_configuration(context.configuration) as store:
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
    emitter.narrate(
        f"seeded {len(result.clients)} clients, {len(result.sessions)} sessions, "
        f"{result.events} events"
    )
    return emitter.succeed(context.name, document)
