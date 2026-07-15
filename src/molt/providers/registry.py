"""The name-to-implementation mapping for both model provider roles.

Selection is by name. An operator changes which model Molt calls by changing one
configuration value, and no source file changes with it. That is the whole reason
this module exists: a provider whose quota is zero, whose region lacks the model,
or whose terms no longer suit is swapped without a rewrite.

**Resolution is lazy, by module path.** An entry holds the module path and the
attribute name of a builder rather than the builder itself, and nothing is
imported until a name is actually loaded. Importing this module therefore drags in
no client library for any provider, which matters twice over: the credential-free
test suites collect with none of them installed, and selecting one provider never
pays the import cost of the others.

**Each implementation module exposes one module-level builder** named by its
entry, taking a resolved configuration and answering an implementation of that
role's protocol. Keeping construction behind a builder keeps each
implementation's own constructor shape private to its module, so an
implementation that needs a client, a region, and a credential and one that needs
a base address and a credential both present the same face here.

The registry keys are stable operator-facing names and are the values the
configuration surface accepts. They are deliberately short and role-suffixed
where a provider serves both roles under different models.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol, cast

from molt.config.resolve import Configuration
from molt.errors import ProviderError, UnknownProviderError
from molt.providers import EmbeddingProvider, TextProvider

__all__ = [
    "EMBEDDING_PROVIDERS",
    "EMBEDDING_ROLE",
    "TEXT_PROVIDERS",
    "TEXT_ROLE",
    "EmbeddingBuilder",
    "ProviderEntry",
    "TextBuilder",
    "embedding_entry",
    "embedding_provider_names",
    "load_embedding_builder",
    "load_text_builder",
    "text_entry",
    "text_provider_names",
]

# The two role names, used in error messages so a reader learns which of the two
# selections was wrong rather than only that a name was unknown.
EMBEDDING_ROLE: Final[str] = "embedding"
TEXT_ROLE: Final[str] = "text"

# The attribute every implementation module exposes.
_BUILDER_ATTRIBUTE: Final[str] = "build"


class EmbeddingBuilder(Protocol):
    """What an embedding implementation module's builder looks like."""

    def __call__(self, configuration: Configuration) -> EmbeddingProvider:
        """Construct the implementation from resolved configuration."""
        ...


class TextBuilder(Protocol):
    """What a text implementation module's builder looks like."""

    def __call__(self, configuration: Configuration) -> TextProvider:
        """Construct the implementation from resolved configuration."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """One registered implementation, held as a path rather than as an object.

    Attributes:
        name: The operator-facing key the configuration surface accepts.
        module: The module path the builder lives in.
        attribute: The module-level builder's name.
    """

    name: str
    module: str
    attribute: str = _BUILDER_ATTRIBUTE


def _entries(*entries: ProviderEntry) -> Mapping[str, ProviderEntry]:
    """Index entries by name as a mapping no caller can add a name to."""
    indexed: dict[str, ProviderEntry] = {}
    for entry in entries:
        if entry.name in indexed:
            raise ProviderError(f"the provider registry declares the name {entry.name!r} twice")
        indexed[entry.name] = entry
    return MappingProxyType(indexed)


# The registered embedding implementations. `bedrock` is the documented default
# for the role; `external` is the code-specialised retrieval model the delivered
# demonstration configuration selects, chosen because residue detection searches
# for semantically similar source code.
EMBEDDING_PROVIDERS: Final[Mapping[str, ProviderEntry]] = _entries(
    ProviderEntry(name="bedrock", module="molt.providers.bedrock"),
    ProviderEntry(name="external", module="molt.providers.external_embedding"),
)

# The registered text implementations, used for both adjudication and rewriting.
# `bedrock` is the documented default for the role; `external` is the
# prompt-caching model the delivered demonstration configuration selects.
TEXT_PROVIDERS: Final[Mapping[str, ProviderEntry]] = _entries(
    ProviderEntry(name="bedrock", module="molt.providers.bedrock"),
    ProviderEntry(name="external", module="molt.providers.external_text"),
)


def embedding_provider_names() -> tuple[str, ...]:
    """Every registered embedding name, in declared order."""
    return tuple(EMBEDDING_PROVIDERS)


def text_provider_names() -> tuple[str, ...]:
    """Every registered text name, in declared order."""
    return tuple(TEXT_PROVIDERS)


def _entry_for(
    role: str,
    registry: Mapping[str, ProviderEntry],
    name: str,
) -> ProviderEntry:
    """Look one name up, raising a configuration fault listing the names on offer."""
    entry = registry.get(name)
    if entry is None:
        raise UnknownProviderError(role, name, tuple(registry))
    return entry


def embedding_entry(name: str) -> ProviderEntry:
    """The registered embedding entry for a name, without importing anything."""
    return _entry_for(EMBEDDING_ROLE, EMBEDDING_PROVIDERS, name)


def text_entry(name: str) -> ProviderEntry:
    """The registered text entry for a name, without importing anything."""
    return _entry_for(TEXT_ROLE, TEXT_PROVIDERS, name)


def _load(entry: ProviderEntry) -> object:
    """Import an entry's module and return its builder attribute.

    A module that cannot be imported or that exposes no builder is a provider
    fault rather than a configuration fault: the name was one of the names on
    offer, so what failed is the implementation behind it.
    """
    try:
        module = importlib.import_module(entry.module)
    except ImportError as exc:
        raise ProviderError(
            f"the provider registered as {entry.name!r} names the module "
            f"{entry.module}, which could not be imported"
        ) from exc
    builder = getattr(module, entry.attribute, None)
    if builder is None or not callable(builder):
        raise ProviderError(
            f"the provider registered as {entry.name!r} names {entry.module}, "
            f"which exposes no callable {entry.attribute!r}"
        )
    return builder


# A builder's signature is checked statically, where each implementation module is
# checked against its role's protocol, rather than dynamically here: a runtime
# check over a callable can see that something is callable but not what it
# accepts, so asserting it would state a guarantee it cannot keep.


def load_embedding_builder(name: str) -> EmbeddingBuilder:
    """Import the embedding implementation registered under a name and return its builder."""
    return cast(EmbeddingBuilder, _load(embedding_entry(name)))


def load_text_builder(name: str) -> TextBuilder:
    """Import the text implementation registered under a name and return its builder."""
    return cast(TextBuilder, _load(text_entry(name)))
