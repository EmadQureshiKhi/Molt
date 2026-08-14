"""The watch verb: the Policy_Watcher in the foreground, or one batch and out.

The single-batch mode is what the integration test drives, so it is a mode of the
same watcher rather than a second code path: one batch is consumed, the health
reading is reported, and the process ends.
"""

from __future__ import annotations

from typing import Final

from molt.cli.context import VerbContext
from molt.cli.exits import ExitCode
from molt.cli.verbs.common import integer_overrides, serving
from molt.models.event import JsonObject
from molt.policy.watcher import Watcher
from molt.store import MemoryStore

__all__ = ["run"]

_POLL_INTERVAL_KEY: Final[str] = "MOLT_WATCHER_POLL_INTERVAL_SECONDS"
_RULES_KEY: Final[str] = "MOLT_POLICY_RULES_PATH"

# How many batches the foreground mode consumes before it reports. Bounded rather
# than endless so the verb terminates on its own where a demonstration needs it to.
_FOREGROUND_BATCHES: Final[int] = 1000


def run(context: VerbContext) -> ExitCode:
    """Consume mutations, once or in the foreground, and report the health reading."""
    emitter = context.emitter
    overrides = integer_overrides(context, {"interval": _POLL_INTERVAL_KEY})
    rules = context.text("rules")
    if rules is not None:
        overrides[_RULES_KEY] = rules
    configuration = context.configuration_for(overrides)
    once = context.flag("once")

    with MemoryStore.from_configuration(configuration) as store, serving(store):
        watcher = Watcher.from_configuration(store, configuration)
        with watcher:
            mode = watcher.start()
            emitter.narrate(f"consuming through {mode}")
            applied = watcher.consume_once() if once else watcher.run(batches=_FOREGROUND_BATCHES)
            health = watcher.health()

    document: JsonObject = {
        "mode": str(mode),
        "once": once,
        "applied": applied,
        "health": health.body,
    }
    return emitter.succeed(context.name, document)
