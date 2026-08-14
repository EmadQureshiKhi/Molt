"""The few things more than one verb needs, so no verb restates them.

Two of these are worth naming rather than inlining. A threshold given on the
command line is applied by replacing the environment value the surface resolves
from, so the precedence rule stays the surface's and a verb holds no second one.
And a read-only pass names a synthetic run identifier: the residue walk and the
sensitivity analysis both take a run row's identifier, and neither is erasing
anything, so the identifier they present belongs to no stored run and no row is
written under it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

from molt.cli.context import VerbContext
from molt.lifecycle import Closeable, current_termination, install_signal_handlers
from molt.providers import EmbeddingProvider

__all__ = [
    "AUTO_INCLUDE_KEY",
    "BATCH_SIZE_KEY",
    "QUERY_LIMIT_KEY",
    "REVIEW_KEY",
    "TOP_K_KEY",
    "ProviderEmbedder",
    "integer_overrides",
    "serving",
    "synthetic_run_id",
    "threshold_overrides",
]

AUTO_INCLUDE_KEY: Final[str] = "MOLT_AUTO_INCLUDE_THRESHOLD"
REVIEW_KEY: Final[str] = "MOLT_REVIEW_THRESHOLD"
QUERY_LIMIT_KEY: Final[str] = "MOLT_RESIDUE_QUERY_LIMIT"
TOP_K_KEY: Final[str] = "MOLT_RESIDUE_TOP_K"
BATCH_SIZE_KEY: Final[str] = "MOLT_ERASURE_BATCH_SIZE"


def threshold_overrides(context: VerbContext) -> dict[str, str]:
    """The threshold environment values the given flags replace."""
    overrides: dict[str, str] = {}
    for option, key in (
        ("auto_include_threshold", AUTO_INCLUDE_KEY),
        ("review_threshold", REVIEW_KEY),
    ):
        value = getattr(context.args, option, None)
        if isinstance(value, float):
            overrides[key] = format(value, ".17g")
    return overrides


def integer_overrides(context: VerbContext, mapping: dict[str, str]) -> dict[str, str]:
    """The whole-number environment values the given flags replace."""
    overrides: dict[str, str] = {}
    for option, key in mapping.items():
        value = getattr(context.args, option, None)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        overrides[key] = str(value)
    return overrides


@contextmanager
def serving(pool: Closeable) -> Iterator[None]:
    """Run a long-lived verb under the graceful termination sequence.

    Every verb that stays up -- the Collector, the watcher, the console, the tool
    server -- enters through here, so the shutdown order lives in one place rather
    than once per verb. Entering registers the connection pool and installs the
    signal handlers; leaving performs the sequence in the fixed order, however the
    block ended: stop accepting, let in-flight work settle, close the pool, flush
    telemetry.

    The sequence runs on the failure path too, because a verb that raised still
    holds connections and still has buffered measurements worth delivering.
    """
    termination = current_termination()
    termination.register_pool(pool)
    install_signal_handlers(termination)
    try:
        yield
    finally:
        termination.terminate()


def synthetic_run_id() -> UUID:
    """An identifier for a pass that writes nothing, so it names no stored run."""
    return uuid4()


@dataclass(frozen=True, slots=True)
class ProviderEmbedder:
    """The query-embedding surface, over a selected Embedding_Provider.

    The recall path and the tool server both declare the narrow one-call surface
    they need rather than importing a provider, which is what keeps the dependency
    pointing one way. This is the adapter from the wider provider interface onto
    that surface, held in one place so neither caller writes it again.
    """

    provider: EmbeddingProvider

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One vector per text, in the input order."""
        return self.provider.embed(texts)
