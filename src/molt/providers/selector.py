"""Provider selection, the startup width gate, and the credential loader.

Selection is by name against the registry, so an operator changes which model
Molt calls by changing one configuration value and no source file changes with
it. Resolution stays lazy: a name is checked against the registry keys before
anything is imported, and the implementation module is imported only once a
name has been accepted. Importing this module therefore drags in no client
library for any provider, which is what lets the credential-free suites collect
with none of them installed.

Four obligations are discharged here, in this order.

**An unknown name is a configuration fault, reported with the names on offer.**
Nothing is called and nothing is imported: a value was simply not one of the
values the registry accepts, so the failure names the value and lists the keys
and the operator's next action is to pick one.

**A credential is loaded from a parameter name or an operator-provided file, and
from nowhere else.** The loading itself is not reimplemented here; the secret
accessors already resolve the pair and already answer with a value that renders
as one fixed placeholder in every log record, exception message, error detail,
and output stream. This module maps each role to its pair of configuration keys
and delegates, because two implementations of one resolution rule is exactly the
drift the placeholder discipline cannot survive.

**The width check is a startup gate rather than a per-row check.** The embedding
provider is probed once, the reported width is compared against the width the
schema fixes, and a mismatch is refused before a single vector exists. A
mismatch left to the stored column's own constraint would be discovered one
insert at a time, after a run had already begun writing, which is too late to be
useful to anyone. The refusal carries both widths because the operator has to see
what was reported and what is required in order to choose a different model.

**The text provider's prompt-cache capability is read from the model's own
report.** The caller marks the cache boundary only where marking it means
something, so assuming the capability would either pay for a marker the provider
ignores or skip one the provider honours. An unreachable model is recorded as
reporting no capability rather than as a startup failure, because a model that
cannot be reached at startup is a runtime condition every calling component
already fails closed on.

The gate is expressed twice on purpose. `validate_at_startup` raises, so it is
callable from a test and from a component that wants to handle the refusal, and
`validate_at_startup_or_exit` prints both widths and leaves the process with a
non-zero status. Only the second one ends a process, and it ends it by raising
the interpreter's own exit rather than by killing the process outright, so
buffered output still flushes and a caller that means to trap the refusal can.

Neither the reachability finding nor the capability finding is written to the
cluster here. Both are returned as records, because this module holds no
connection and the component that owns the capability table is where a row
belongs.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, TextIO

from molt.config.resolve import Configuration, InvalidConfigValueError, Kind
from molt.config.secrets import Credential, ParameterReader
from molt.config.secrets import load_credential as load_configured_credential
from molt.errors import ModelUnavailableError, ProviderError, ProviderWidthMismatchError
from molt.providers import (
    SCHEMA_VECTOR_DIMENSIONS,
    EmbeddingProvider,
    ProbeLike,
    ProviderProbe,
    TextProvider,
)
from molt.providers.registry import (
    EMBEDDING_ROLE,
    TEXT_ROLE,
    ProviderEntry,
    embedding_entry,
    load_embedding_builder,
    load_text_builder,
    text_entry,
)
from molt.telemetry import Severity, log

__all__ = [
    "COMPONENT",
    "CONFIGURATION_EXIT_STATUS",
    "EMBEDDING_PROVIDER_ENV",
    "PROMPT_CACHE_CAPABILITY",
    "PROMPT_CACHE_ENV",
    "TEXT_PROVIDER_ENV",
    "CapabilityFinding",
    "ConsumingRole",
    "PromptCachePreference",
    "RoleSelection",
    "StartupReport",
    "load_credential",
    "select_embedding_provider",
    "select_text_provider",
    "selected_embedding_name",
    "selected_text_name",
    "validate_at_startup",
    "validate_at_startup_or_exit",
    "validate_selected_names",
]

# The component name every log record written here carries.
COMPONENT: Final[str] = "provider_selector"

# The status a refused configuration leaves the process with. A configuration
# fault and an operational fault are deliberately distinct statuses, and a width
# mismatch is the former: nothing was attempted and nothing will succeed until a
# value changes.
CONFIGURATION_EXIT_STATUS: Final[int] = 2

# The two selection keys of the configuration surface.
EMBEDDING_PROVIDER_ENV: Final[str] = "MOLT_EMBEDDING_PROVIDER"
TEXT_PROVIDER_ENV: Final[str] = "MOLT_TEXT_PROVIDER"

# The operator's prompt-cache preference, which narrows the model's own report
# and never widens it.
PROMPT_CACHE_ENV: Final[str] = "MOLT_PROMPT_CACHE_ENABLED"

# The capability row name the text probe answers for.
PROMPT_CACHE_CAPABILITY: Final[str] = "text_provider_prompt_cache"

# The model identifier key each consuming role reads. The two text roles share a
# provider and name different models, which is why the identifier is per role
# rather than per provider.
_EMBEDDING_MODEL_ENV: Final[str] = "MOLT_EMBEDDING_MODEL_ID"
_ADJUDICATION_MODEL_ENV: Final[str] = "MOLT_ADJUDICATION_MODEL_ID"
_REWRITE_MODEL_ENV: Final[str] = "MOLT_REWRITE_MODEL_ID"

# The parameter-name key and the file-path key per provider role. A credential
# resolves from one of these two and from nothing else.
_CREDENTIAL_KEYS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        EMBEDDING_ROLE: ("MOLT_EMBEDDING_CREDENTIAL_PARAM", "MOLT_EMBEDDING_CREDENTIAL_FILE"),
        TEXT_ROLE: ("MOLT_TEXT_CREDENTIAL_PARAM", "MOLT_TEXT_CREDENTIAL_FILE"),
    }
)


class ConsumingRole(StrEnum):
    """The three components that hold a selected provider.

    They are three rather than two because the two text-role components name
    different models against one provider, and a record that collapsed them
    would answer *which provider* without answering *which model*.
    """

    EMBEDDER = "embedder"
    ADJUDICATOR = "adjudicator"
    REDACTION_REWRITER = "redaction_rewriter"


class PromptCachePreference(StrEnum):
    """What the operator asked for regarding prompt caching."""

    AUTO = "auto"
    ENABLED = "true"
    DISABLED = "false"


@dataclass(frozen=True, slots=True)
class RoleSelection:
    """Which provider and which model one consuming role ended up with.

    Attributes:
        role: The component the selection belongs to.
        provider_name: The registry key the provider was selected under.
        model_id: The model identifier that role calls the provider with.
    """

    role: ConsumingRole
    provider_name: str
    model_id: str


@dataclass(frozen=True, slots=True)
class CapabilityFinding:
    """One probed fact, shaped as the capability record holds it.

    This is a record rather than a write. The capability table is owned by the
    data-access layer, so a finding travels back to the caller and the caller
    persists it; nothing here holds a connection or issues a statement.

    Attributes:
        name: The capability row name.
        available: Whether the capability is effectively available.
        detail: Why, where the answer needs one, and nothing where it does not.
    """

    name: str
    available: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StartupReport:
    """Everything the startup sequence learned, for the caller to record.

    Attributes:
        embedding: The embedding probe, carrying reachability and the width.
        text: The text probe, carrying reachability and the reported capability.
        roles: One selection per consuming role, in role order.
        capabilities: The findings the caller writes to the capability record.
    """

    embedding: ProbeLike
    text: ProbeLike
    roles: tuple[RoleSelection, ...]
    capabilities: tuple[CapabilityFinding, ...]

    def selection(self, role: ConsumingRole) -> RoleSelection:
        """The selection recorded for one consuming role."""
        for selection in self.roles:
            if selection.role is role:
                return selection
        raise ProviderError(f"the startup report holds no selection for the {role} role")

    def capability(self, name: str) -> CapabilityFinding:
        """The finding recorded under one capability row name."""
        for finding in self.capabilities:
            if finding.name == name:
                return finding
        raise ProviderError(f"the startup report holds no finding named {name!r}")

    @property
    def prompt_cache_available(self) -> bool:
        """Whether the cache boundary is worth marking on a prompt."""
        return self.capability(PROMPT_CACHE_CAPABILITY).available


# --------------------------------------------------------------------------
# Selection by name
# --------------------------------------------------------------------------


def selected_embedding_name(configuration: Configuration) -> str:
    """The embedding provider name the configuration surface resolves to."""
    return configuration.text(EMBEDDING_PROVIDER_ENV)


def selected_text_name(configuration: Configuration) -> str:
    """The text provider name the configuration surface resolves to."""
    return configuration.text(TEXT_PROVIDER_ENV)


def validate_selected_names(configuration: Configuration) -> tuple[ProviderEntry, ProviderEntry]:
    """Check both configured names against the registry without importing either.

    This is the cheap half of selection, and separating it is what lets a
    configuration check run in a process that has no provider client library
    installed: an unknown name is reported here, and only an accepted name goes
    on to be imported.
    """
    return (
        embedding_entry(selected_embedding_name(configuration)),
        text_entry(selected_text_name(configuration)),
    )


def select_embedding_provider(configuration: Configuration) -> EmbeddingProvider:
    """Build the embedding implementation the configuration surface names.

    An unknown name raises before any import, naming the value and the registry
    keys. A registered name whose implementation module is absent or exposes no
    builder raises a provider fault instead, because the name was accepted and
    what failed is the implementation behind it.
    """
    name = selected_embedding_name(configuration)
    builder = load_embedding_builder(name)
    return builder(configuration)


def select_text_provider(configuration: Configuration) -> TextProvider:
    """Build the text implementation the configuration surface names."""
    name = selected_text_name(configuration)
    builder = load_text_builder(name)
    return builder(configuration)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def load_credential(
    configuration: Configuration,
    role: str,
    *,
    directory: Path | None = None,
    reader: ParameterReader | None = None,
) -> Credential:
    """Load one role's provider credential from its parameter name or its file.

    The resolution rule and the placeholder rendering both live in the secret
    accessors, and this delegates to them rather than restating either. What is
    added here is the mapping from a provider role to the pair of configuration
    keys that role's credential is named by, so a caller asks for a role rather
    than remembering two key spellings.

    The returned value renders as one fixed placeholder in every log record,
    exception message, error detail, and output stream; reading the value takes
    an explicit call that is easy to find in a review.
    """
    keys = _CREDENTIAL_KEYS.get(role)
    if keys is None:
        raise ProviderError(
            f"no provider role named {role!r} carries a credential; "
            f"the roles are {', '.join(_CREDENTIAL_KEYS)}"
        )
    parameter_env, file_env = keys
    return load_configured_credential(
        configuration,
        parameter_env=parameter_env,
        file_env=file_env,
        directory=directory,
        reader=reader,
    )


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


def _probe_embedding(provider: EmbeddingProvider) -> ProbeLike:
    """Probe the embedding provider, falling back to its declared width.

    An unreachable model is recorded as unreachable rather than raised, so the
    width gate still runs: a provider declaring a width the schema does not hold
    must be refused whether or not the model answered, because the declared
    width is what every later call would produce.
    """
    try:
        probe = provider.probe()
    except ModelUnavailableError:
        log(
            Severity.WARNING,
            COMPONENT,
            "the embedding provider did not answer its probe",
            provider=provider.name,
            model_id=provider.model_id,
        )
        return ProviderProbe(
            name=provider.name,
            model_id=provider.model_id,
            reachable=False,
            dimensions=provider.dimensions,
        )
    if probe.dimensions is None:
        return ProviderProbe(
            name=probe.name,
            model_id=probe.model_id,
            reachable=probe.reachable,
            dimensions=provider.dimensions,
        )
    return probe


def _probe_text(provider: TextProvider) -> ProbeLike:
    """Probe the text provider, recording an unreachable model as reporting nothing.

    Reachability is a finding rather than a refusal. Every component that calls a
    text model already fails closed on an unavailable one, so a model that cannot
    be reached at startup is recorded and reported, not turned into a startup
    failure that would make one provider's availability a precondition of the
    whole process.
    """
    try:
        return provider.probe()
    except ModelUnavailableError:
        log(
            Severity.WARNING,
            COMPONENT,
            "the text provider did not answer its probe",
            provider=provider.name,
            model_id=provider.model_id,
        )
        return ProviderProbe(name=provider.name, model_id=provider.model_id, reachable=False)


def _prompt_cache_preference(configuration: Configuration) -> PromptCachePreference:
    """Read the operator's prompt-cache preference, refusing any other value."""
    raw = configuration.text(PROMPT_CACHE_ENV).strip().lower()
    for preference in PromptCachePreference:
        if raw == preference.value:
            return preference
    permitted = ", ".join(preference.value for preference in PromptCachePreference)
    raise InvalidConfigValueError(
        PROMPT_CACHE_ENV, Kind.TEXT, f"the value reads none of {permitted}"
    )


def _prompt_cache_finding(
    probe: ProbeLike,
    preference: PromptCachePreference,
) -> CapabilityFinding:
    """Turn the model's own report and the operator's preference into one finding.

    The preference narrows the report and never widens it. Claiming a capability
    the model does not report would mark a boundary the provider ignores, and the
    whole point of reading the capability is to mark it only where marking it
    means something.
    """
    reported = probe.supports_prompt_cache
    if preference is PromptCachePreference.DISABLED:
        return CapabilityFinding(
            PROMPT_CACHE_CAPABILITY,
            False,
            "the configured preference disables prompt caching",
        )
    if reported is None:
        detail = (
            "the text provider was unreachable, so the model reported no prompt-cache capability"
            if not probe.reachable
            else "the text provider reported no prompt-cache capability"
        )
        return CapabilityFinding(PROMPT_CACHE_CAPABILITY, False, detail)
    if not reported:
        return CapabilityFinding(
            PROMPT_CACHE_CAPABILITY,
            False,
            "the model reports no prompt-cache support",
        )
    return CapabilityFinding(PROMPT_CACHE_CAPABILITY, True, None)


def _role_model_id(configuration: Configuration, env: str, fallback: str) -> str:
    """The model identifier a role names, or the one its provider was built with.

    The configured identifier wins where one is set. Where none is, the
    provider's own reported identifier stands in, so the record answers *which
    model* in every case rather than leaving a role blank.
    """
    return configuration.optional_text(env) or fallback


def _role_selections(
    configuration: Configuration,
    embedding: ProbeLike,
    text: ProbeLike,
) -> tuple[RoleSelection, ...]:
    """One selection per consuming role, in role order."""
    return (
        RoleSelection(
            ConsumingRole.EMBEDDER,
            embedding.name,
            _role_model_id(configuration, _EMBEDDING_MODEL_ENV, embedding.model_id),
        ),
        RoleSelection(
            ConsumingRole.ADJUDICATOR,
            text.name,
            _role_model_id(configuration, _ADJUDICATION_MODEL_ENV, text.model_id),
        ),
        RoleSelection(
            ConsumingRole.REDACTION_REWRITER,
            text.name,
            _role_model_id(configuration, _REWRITE_MODEL_ENV, text.model_id),
        ),
    )


# --------------------------------------------------------------------------
# The startup gate
# --------------------------------------------------------------------------


def validate_at_startup(
    configuration: Configuration,
    embedding: EmbeddingProvider,
    text: TextProvider,
) -> StartupReport:
    """Run the startup sequence, refusing a width the schema does not hold.

    The order matters. The width comparison happens first and raises, so the
    refusal is reached before the text provider is contacted and long before any
    component holds a vector to store. Nothing in this function writes anything
    anywhere: the two findings and the three role selections travel back as a
    report for the caller to record.

    Raises:
        ProviderWidthMismatchError: The embedding provider reports or declares a
            width other than the one the schema fixes. Both widths are carried
            and both appear in the message.
    """
    embedding_probe = _probe_embedding(embedding)
    # A probe that answers no width at all is held to the width its provider
    # declares, because the declared width is what every later call would produce.
    reported = (
        embedding.dimensions if embedding_probe.dimensions is None else embedding_probe.dimensions
    )
    if reported != SCHEMA_VECTOR_DIMENSIONS:
        raise ProviderWidthMismatchError(reported, SCHEMA_VECTOR_DIMENSIONS)

    text_probe = _probe_text(text)
    finding = _prompt_cache_finding(text_probe, _prompt_cache_preference(configuration))
    roles = _role_selections(configuration, embedding_probe, text_probe)

    for selection in roles:
        log(
            Severity.INFO,
            COMPONENT,
            "a consuming role holds a selected provider",
            role=str(selection.role),
            provider=selection.provider_name,
            model_id=selection.model_id,
        )
    # The field names avoid the content markers the log surface filters on, so
    # the record actually carries the width and the two reachability answers
    # rather than having them dropped as if they were memory content.
    log(
        Severity.INFO,
        COMPONENT,
        "the startup probes completed",
        embedder_reachable=embedding_probe.reachable,
        reported_width=reported,
        text_reachable=text_probe.reachable,
        prompt_cache=finding.available,
    )
    return StartupReport(
        embedding=embedding_probe,
        text=text_probe,
        roles=roles,
        capabilities=(finding,),
    )


def validate_at_startup_or_exit(
    configuration: Configuration,
    embedding: EmbeddingProvider,
    text: TextProvider,
    *,
    stream: TextIO | None = None,
) -> StartupReport:
    """Run the startup sequence, leaving the process non-zero on a width mismatch.

    Both widths are printed on the error stream and the interpreter's own exit is
    raised with the configuration status. Raising the exit rather than ending the
    process outright is deliberate: buffered output still flushes, an entry point
    that wants to add its own reporting still can, and the gate stays drivable
    from a test without a subprocess.
    """
    try:
        return validate_at_startup(configuration, embedding, text)
    except ProviderWidthMismatchError as mismatch:
        target = sys.stderr if stream is None else stream
        print(str(mismatch), file=target)
        print(
            f"reported width: {mismatch.reported}; required width: {mismatch.required}; "
            f"select a different embedding model through {EMBEDDING_PROVIDER_ENV} "
            f"or {_EMBEDDING_MODEL_ENV}",
            file=target,
        )
        raise SystemExit(CONFIGURATION_EXIT_STATUS) from mismatch
