"""The shared exception taxonomy.

One class means one failure. Every deliberate failure in the codebase is raised
as one of the classes named here, and a caller that wants to treat a whole family
alike catches the family's base rather than enumerating leaves.

Three rules shape the module.

**A class is defined once, at the place that must be able to import with nothing
else present, and re-exported here.** Configuration resolution and the secret
accessors run before anything else exists, so their classes live beside the code
that raises them; this module imports and re-exports those names rather than
restating them, because two classes spelling one failure is exactly the drift the
taxonomy exists to prevent.

**Every class name ends in `Error`.** The linter enforces the suffix, and the
enforcement is worth more than matching the design document's shorter spellings
letter for letter. Each shorter spelling is therefore bound below as a
module-level alias of its suffixed class, so a reader following the design's name
and a reader following the linter's name reach the same object.

**A message names the fault and never the content.** Messages reach log records,
and the values passing through Molt are memory content, vectors, and credentials.
A class carries a structured attribute where a caller needs the offending value,
so the value is reachable by a handler that has a reason to look at it without
being interpolated into text that is written out.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from molt.config.resolve import (
    ConfigError,
    InvalidConfigValueError,
    MissingConfigError,
    UnknownSettingError,
)
from molt.config.secrets import (
    CredentialFileError,
    LocalBypassRefusedError,
    ParameterMissingError,
    ParameterUnavailableError,
)

__all__ = [
    "AttributionImmutable",
    "AttributionImmutableError",
    "AuditRecordProtected",
    "AuditRecordProtectedError",
    "BackupFailed",
    "BackupFailedError",
    "CheckpointDisagreement",
    "CheckpointDisagreementError",
    "ConfigError",
    "CredentialFileError",
    "EmbeddingAlreadyStoredError",
    "ErasureInFlightError",
    "HistoricalHorizonError",
    "IngressRejected",
    "IngressRejectedError",
    "InvalidConfigValueError",
    "LeaseNotHeld",
    "LeaseNotHeldError",
    "LeaseRefused",
    "LeaseRefusedError",
    "LineageCycleError",
    "LocalBypassRefusedError",
    "MissingConfigError",
    "MissingParentError",
    "ModelUnavailable",
    "ModelUnavailableError",
    "MoltError",
    "ParameterMissingError",
    "ParameterUnavailableError",
    "ProviderError",
    "ProviderWidthMismatch",
    "ProviderWidthMismatchError",
    "RequestTooLarge",
    "RequestTooLargeError",
    "SerializationExhausted",
    "SerializationExhaustedError",
    "SigningUnavailable",
    "SigningUnavailableError",
    "StaleFencingGeneration",
    "StaleFencingGenerationError",
    "StorageUnavailable",
    "StorageUnavailableError",
    "StoreError",
    "UnknownProviderError",
    "UnknownSettingError",
    "VerificationFailed",
    "VerificationFailedError",
]


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------


class MoltError(Exception):
    """The base of every failure Molt raises deliberately.

    Catching this catches a decision the code made and nothing else: a driver
    fault, a cloud client fault, and a programming mistake all sit outside it, so
    a handler that means *a documented failure happened* cannot accidentally
    swallow a fault it has no answer for.
    """


# --------------------------------------------------------------------------
# Store and transaction faults
# --------------------------------------------------------------------------


class StoreError(MoltError):
    """A database interaction was refused or could not be completed."""


class SerializationExhaustedError(StoreError):
    """A transaction kept conflicting after the configured number of attempts."""

    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(f"a transaction still conflicted after {attempts} attempts")


class LineageCycleError(StoreError):
    """A lineage edge would have made the lineage graph cyclic."""


class MissingParentError(StoreError):
    """A row referenced a parent row that does not exist."""


class EmbeddingAlreadyStoredError(StoreError):
    """A vector for this Artifact under this provider and model is already stored.

    This is a refusal a caller acts on rather than merely reports, which is why it
    is a class of its own rather than the family base. The schema holds one vector
    per Artifact per provider-and-model pair, and an Event's pending flag can
    never be cleared, so a drain meeting this refusal has learned that the work is
    already done. Telling that apart from a cluster that cannot be written to is
    the difference between settling an Artifact and leaving it owed, and a caller
    should not have to read a message to make it.

    The three values that identify the stored row are carried as attributes and
    named in the message, because none of them is memory content: they are an
    identifier and the two names of the service that produced the vector.
    """

    def __init__(self, artifact_id: UUID, provider: str, model_id: str) -> None:
        self.artifact_id = artifact_id
        self.provider = provider
        self.model_id = model_id
        super().__init__(
            f"a vector for the artifact {artifact_id} produced by {provider} with "
            f"{model_id} is already stored, and one vector per artifact per provider "
            "and model pair is held unique, so nothing was written"
        )


class ErasureInFlightError(StoreError):
    """The erasure guard read refused a write because a run is in flight for the Client."""


class HistoricalHorizonError(StoreError):
    """A historical read named an instant outside the cluster's retention horizon."""


class StaleFencingGenerationError(StoreError):
    """A write presented a fencing generation that is no longer the current one.

    Both generations are carried because the point of the refusal is that the
    caller learns it was superseded rather than merely that it failed. Nothing is
    persisted by the refused transaction.
    """

    def __init__(self, presented: int, current: int) -> None:
        self.presented = presented
        self.current = current
        super().__init__(
            f"the presented fencing generation {presented} is superseded by "
            f"generation {current}, so nothing was persisted"
        )


class LeaseNotHeldError(StoreError):
    """An erasure run reached a mutation without holding a lease, and aborts."""


class LeaseRefusedError(StoreError):
    """A lease acquisition was refused because a lease is current for the Client.

    The current owner and the current generation are carried so the loser of a
    contest learns who won rather than only that it lost.
    """

    def __init__(self, owner: str, generation: int) -> None:
        self.owner = owner
        self.generation = generation
        super().__init__(f"an erasure lease is already held by {owner} at generation {generation}")


class AuditRecordProtectedError(StoreError):
    """A deletion was refused because it would have removed audit evidence.

    The refusal comes from a referential action in the schema rather than from an
    application rule, so the table and the count of protected rows are what the
    database reported.
    """

    def __init__(self, table: str, protected_rows: int) -> None:
        self.table = table
        self.protected_rows = protected_rows
        super().__init__(
            f"the deletion was refused because {protected_rows} row(s) of "
            f"{table} hold audit evidence"
        )


class AttributionImmutableError(StoreError):
    """A stored attribution version was restated rather than superseded."""


# --------------------------------------------------------------------------
# Attestation faults
# --------------------------------------------------------------------------


class CheckpointDisagreementError(MoltError):
    """A checkpoint recomputation disagrees with the stored root digest.

    The changed sessions are partitioned before the failure is raised: a change
    accounted for by a recorded erasure run is an explanation, and a change
    accounted for by nothing is the finding. Both sets are carried so a caller
    can report the distinction rather than collapse it.
    """

    def __init__(
        self,
        changed_sessions: Sequence[UUID],
        accounting_runs: Sequence[UUID],
    ) -> None:
        self.changed_sessions = tuple(changed_sessions)
        self.accounting_runs = tuple(accounting_runs)
        super().__init__(
            f"{len(self.changed_sessions)} session(s) changed since the checkpoint, "
            f"of which {len(self.accounting_runs)} erasure run(s) account for some part"
        )


class VerificationFailedError(MoltError):
    """A verification could not confirm the claim it was asked to confirm."""


class SigningUnavailableError(MoltError):
    """The signing key could not be used, so no signed document was produced."""


class StorageUnavailableError(MoltError):
    """The evidence object store could not be written to or read from."""


class BackupFailedError(MoltError):
    """Pre-erasure backup evidence could not be secured, so no mutation happens."""


# --------------------------------------------------------------------------
# Ingress faults
# --------------------------------------------------------------------------


class IngressRejectedError(MoltError):
    """A request's signature, timestamp, or required header was faulty."""


class RequestTooLargeError(MoltError):
    """A request body exceeded the configured bound, so nothing was persisted."""


# --------------------------------------------------------------------------
# Model provider faults
# --------------------------------------------------------------------------


class ProviderError(MoltError):
    """The base of every model provider fault.

    Provider access is an interface rather than a vendor, so a caller catches
    this and never a fault type belonging to one provider's client library.
    """


class ModelUnavailableError(ProviderError):
    """A model could not be reached or would not answer, with all causes collapsed.

    Collapsing the causes is deliberate. A caller's response to an unreachable
    model, a throttle, a timeout, and a malformed answer is the same in every
    place Molt calls a model: retry a bounded number of times, then fail closed.
    Distinguishing them would invite a branch that no requirement asks for.
    """


class ProviderWidthMismatchError(ProviderError):
    """An embedding provider reports a vector width the schema does not hold.

    Both widths are carried and both appear in the message, because this is a
    startup gate: the operator has to see what was reported and what is required
    in order to pick a different model, and the check happens before any vector
    is written rather than one insert at a time.
    """

    def __init__(self, reported: int, required: int) -> None:
        self.reported = reported
        self.required = required
        super().__init__(
            f"the embedding provider reports a width of {reported} where the "
            f"schema requires {required}; no embedding was written"
        )


class UnknownProviderError(ConfigError):
    """A configured provider name matches no key of the provider registry.

    This is a configuration fault rather than a provider fault: nothing was
    called, a name was simply not one of the names on offer. The message lists
    the registered names, because the operator's next action is to pick one.
    """

    def __init__(self, role: str, name: str, registered: Sequence[str]) -> None:
        self.role = role
        self.name = name
        self.registered = tuple(registered)
        super().__init__(
            f"no {role} provider is registered under the name {name!r}; "
            f"the registered names are {', '.join(self.registered)}"
        )


# --------------------------------------------------------------------------
# Design spellings
#
# The design document names several of the classes above without the `Error`
# suffix the linter requires. Each shorter spelling is bound here to its
# suffixed class, so following either name reaches one object and no failure
# gains a second identity.
# --------------------------------------------------------------------------

SerializationExhausted: Final[type[SerializationExhaustedError]] = SerializationExhaustedError
StaleFencingGeneration: Final[type[StaleFencingGenerationError]] = StaleFencingGenerationError
LeaseNotHeld: Final[type[LeaseNotHeldError]] = LeaseNotHeldError
LeaseRefused: Final[type[LeaseRefusedError]] = LeaseRefusedError
AuditRecordProtected: Final[type[AuditRecordProtectedError]] = AuditRecordProtectedError
AttributionImmutable: Final[type[AttributionImmutableError]] = AttributionImmutableError
CheckpointDisagreement: Final[type[CheckpointDisagreementError]] = CheckpointDisagreementError
IngressRejected: Final[type[IngressRejectedError]] = IngressRejectedError
ModelUnavailable: Final[type[ModelUnavailableError]] = ModelUnavailableError
ProviderWidthMismatch: Final[type[ProviderWidthMismatchError]] = ProviderWidthMismatchError
RequestTooLarge: Final[type[RequestTooLargeError]] = RequestTooLargeError
SigningUnavailable: Final[type[SigningUnavailableError]] = SigningUnavailableError
StorageUnavailable: Final[type[StorageUnavailableError]] = StorageUnavailableError
BackupFailed: Final[type[BackupFailedError]] = BackupFailedError
VerificationFailed: Final[type[VerificationFailedError]] = VerificationFailedError
