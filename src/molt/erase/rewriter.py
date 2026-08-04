"""The Redaction_Rewriter: one model call per blended Artifact, and what it accepts.

A blended Artifact carries several Clients' content in one body, so erasing one
Client from it is a rewrite rather than a delete. The rewrite is produced by the
configured Text_Provider through the provider protocol, and this module is the
gate between what the model answered and what the surgical transaction is allowed
to store.

Four claims carry the module.

**One call per Artifact, and the answer is never trusted on its face.** A model
that answers is not a model that answered well: it may return an empty string, a
one-line summary that discards every retained Client's content, or a body that
still names the Client being erased. Each of those is a leak or a loss that the
erasure claim would then be built on, so the answer passes five checks before it
becomes a replacement, and the checks are stated here rather than left to the
caller, because a caller that forgot one would produce an erasure certificate
asserting something untrue.

**Every failure collapses to unavailability, and that is the whole point of the
shape.** A throttle, a timeout, a credential refusal, an unparseable answer, and
an answer that fails validation all mean the same thing to the caller: no usable
replacement exists, so the Artifact is hard-deleted and the reason
`redaction_unavailable_fail_closed` is recorded. Distinguishing them would invite
a branch no requirement asks for, and the branch a caller would be tempted to
write is *keep the body and carry on*, which is exactly the leak the fail-closed
bias exists to refuse. Losing blended memory is a cost; leaking an erased
Client's content is a broken contractual claim.

**Both ends of the ratio band are read from the configuration surface.** The band
has two failures to refuse and they are not the same failure: the degenerate
one-line answer, which the floor catches, and the padded essay, which the ceiling
catches. Deriving the ceiling from the floor would tie them together, so tightening
the one a deployment cares about would move the other. Each end is therefore its
own setting, and the pair is validated as an ordered band: the floor lies above
zero, and the ceiling lies at or above the floor.

**No request or response body is persisted, and the pre-redaction body never
leaves this call.** What travels onward is the replacement text, its digest, the
provider and model that produced it, and a count-only structural summary. The
summary is counts of segments removed and retained rather than a text diff,
because a stored diff would be a copy of the original body under another name.

The prompt is the two-part shape the provider protocol declares: fixed
instructions in the stable prefix, the body and the naming constraints in the
variable suffix. The cache boundary is deliberately left unmarked. The portion
that repeats across Artifacts is the instruction text alone, which is far below
any provider's minimum cacheable prefix length, and a cache write that no
subsequent read amortises costs more than no caching at all.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from molt.config.resolve import Configuration, load_configuration
from molt.errors import ModelUnavailable, ModelUnavailableError
from molt.providers import Prompt, TextProvider
from molt.telemetry import Severity, log, metric

__all__ = [
    "COMPONENT",
    "FAIL_CLOSED_REASON",
    "RATIO_MAXIMUM_KEY",
    "RATIO_MINIMUM_KEY",
    "REDACTION_FAIL_CLOSED_METRIC",
    "REWRITE_INSTRUCTIONS",
    "ClientIdentity",
    "RatioBand",
    "Replacement",
    "RewriteRequest",
    "StructuralDiff",
    "body_digest",
    "prompt_for",
    "rewrite",
    "segments_of",
]

# The component name every record from this module carries.
COMPONENT: Final[str] = "erase"

# The measurement emitted once per Artifact whose rewrite could not be used, for
# whichever of the collapsed causes. Undimensioned: the tenant and the Artifact are
# both unbounded, so attaching either would turn one billable metric into as many
# as there are tenants, and both belong in the log record instead.
REDACTION_FAIL_CLOSED_METRIC: Final[str] = "erasure.redaction_fail_closed"

# What a Disposition records when no usable replacement was produced. The reason is
# one value for every collapsed cause, matching the collapse the failure itself
# performs.
FAIL_CLOSED_REASON: Final[str] = "redaction_unavailable_fail_closed"

# The two configuration surface keys the ends of the ratio band are read from. Each
# end is a setting of its own, for the reason the module docstring gives.
RATIO_MINIMUM_KEY: Final[str] = "MOLT_REWRITE_LENGTH_RATIO_MIN"
RATIO_MAXIMUM_KEY: Final[str] = "MOLT_REWRITE_LENGTH_RATIO_MAX"

# The fixed instructions the stable prefix carries. They name no Client, no
# Artifact, and no length, so the prefix is byte-identical across every Artifact of
# a run and the whole per-Artifact part of the prompt sits in the suffix.
REWRITE_INSTRUCTIONS: Final[str] = (
    "You are removing one organisation's content from a shared document under a "
    "contractual erasure obligation.\n"
    "Rewrite the document so that every trace of the named organisation is gone: "
    "its name, its identifiers, its markers, and any statement that describes only "
    "it.\n"
    "Keep every other organisation's content intact, keep its markers exactly as "
    "they appear, and keep the document's structure and length comparable to the "
    "original.\n"
    "Answer with the rewritten document alone and no commentary.\n"
)

# The labels the two parts of the suffix carry, so the model is told which name is
# being removed and which names must survive.
_ERASED_LABEL: Final[str] = "Organisation to remove:"
_RETAINED_LABEL: Final[str] = "Organisations to keep:"
_DOCUMENT_LABEL: Final[str] = "Document:"
_MARKER_SEPARATOR: Final[str] = ", "

# The value a ratio floor lies strictly above. A floor at or below zero admits an
# empty replacement, which the emptiness check refuses anyway, so admitting it here
# would leave the band saying something its own check contradicts.
_RATIO_FLOOR_LOWEST: Final[float] = 0.0


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """The names a Client can be recognised by inside a body.

    All three name shapes are held together because the erased Client's check is
    over the union of them: a replacement that dropped the slug but kept the
    display name has not removed the Client, it has renamed the leak.
    """

    client_id: UUID
    slug: str
    display_name: str
    content_markers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an identity that names nothing a check could look for."""
        if not self.slug:
            raise ValueError("a Client identity carries the slug it is recognised by")

    @property
    def mentions(self) -> tuple[str, ...]:
        """Every name whose presence in a replacement means the Client is still there."""
        named = (self.slug, self.display_name, *self.content_markers)
        return tuple(name for name in named if name)

    @property
    def markers(self) -> tuple[str, ...]:
        """The configured markers alone, which is what a retained Client is checked by.

        A retained Client's presence is asserted through its markers rather than
        through its display name, because a marker is what the corpus was labelled
        with and a display name may never have appeared in the body at all.
        """
        return tuple(marker for marker in self.content_markers if marker)


@dataclass(frozen=True, slots=True)
class RatioBand:
    """The length band a replacement is admitted inside, from two configured bounds."""

    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        """Refuse a pair of bounds that describes no ordered band."""
        if self.minimum <= _RATIO_FLOOR_LOWEST:
            raise ValueError(
                "a rewrite length ratio floor lies above zero, "
                "because a floor at or below zero admits an empty replacement"
            )
        if self.maximum < self.minimum:
            raise ValueError(
                "a rewrite length ratio ceiling lies at or above the floor, "
                "so that the band the two fix is not empty"
            )

    def admits(self, original: int, replacement: int) -> bool:
        """Whether a replacement's length sits inside the band the original fixes.

        An original of no length fixes no band, so any non-empty replacement is
        admitted; the emptiness check is what refuses the degenerate answer there.
        """
        if original == 0:
            return True
        ratio = replacement / original
        return self.minimum <= ratio <= self.maximum

    @classmethod
    def from_configuration(cls, configuration: Configuration | None = None) -> RatioBand:
        """The band the configuration surface fixes, resolved once per call site.

        Both ends are read independently, so a deployment tightens the ceiling that
        refuses a padded answer without touching the floor that refuses a
        degenerate one.
        """
        resolved = configuration if configuration is not None else load_configuration()
        return cls(
            minimum=resolved.number(RATIO_MINIMUM_KEY),
            maximum=resolved.number(RATIO_MAXIMUM_KEY),
        )


@dataclass(frozen=True, slots=True)
class RewriteRequest:
    """One blended Artifact to rewrite, with the identities both checks read.

    The body travels in and never travels out: the request is consumed by the call
    and nothing derived from it beyond a segment count reaches a stored row.
    """

    artifact_id: UUID
    body: str
    erased: ClientIdentity
    retained: tuple[ClientIdentity, ...]

    def __post_init__(self) -> None:
        """Refuse a request that is not a blended Artifact at all."""
        if not self.retained:
            raise ValueError(
                "a rewrite is for a blended Artifact, so at least one Client is retained; "
                "an Artifact bound to the erased Client alone is hard-deleted instead"
            )


@dataclass(frozen=True, slots=True)
class StructuralDiff:
    """Counts of segments the rewrite dropped and kept, and nothing of their content.

    Counts rather than a text diff, because a stored diff of a redaction is a copy
    of the pre-redaction body under another name.
    """

    removed_segments: int
    retained_segments: int


@dataclass(frozen=True, slots=True)
class Replacement:
    """A validated replacement body, with the evidence a Disposition records.

    The digest is computed here rather than by the caller so that the value the
    optimistic guard writes and the value the certificate states come from one
    place.
    """

    artifact_id: UUID
    text: str
    digest: str
    provider: str
    model_id: str
    diff: StructuralDiff


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def rewrite(
    provider: TextProvider,
    request: RewriteRequest,
    *,
    band: RatioBand | None = None,
) -> Replacement:
    """Produce one validated replacement body, or fail closed.

    Exactly one provider call is made. Everything that can go wrong with it, and
    everything that can be wrong with what it answered, leaves by the same door:
    the failure below, which the caller reads as *hard-delete this Artifact and
    record the fail-closed reason*.

    Args:
        provider: The configured Text_Provider, reached through the protocol.
        request: The Artifact to rewrite and the identities the checks read.
        band: The length band to admit inside, resolved from the configuration
            surface when the caller names none.

    Returns:
        The validated replacement, its digest, and the count-only diff summary.

    Raises:
        ModelUnavailableError: No usable replacement exists, for any cause. The
            measurement has been emitted and nothing has been stored.
    """
    limits = band if band is not None else RatioBand.from_configuration()
    try:
        answered = provider.generate(prompt_for(request))
    except Exception as cause:
        raise _unavailable(request, "the provider did not answer") from cause
    candidate = answered.text
    refusal = _first_failed_check(candidate, request, limits)
    if refusal is not None:
        raise _unavailable(request, refusal)
    return Replacement(
        artifact_id=request.artifact_id,
        text=candidate,
        digest=body_digest(candidate),
        provider=provider.name,
        model_id=answered.model_id,
        diff=_diff_of(request.body, candidate),
    )


def prompt_for(request: RewriteRequest) -> Prompt:
    """The two-part prompt one rewrite sends.

    The prefix is the fixed instructions and the suffix is everything that varies
    per Artifact. The boundary is left unmarked deliberately: the repeating portion
    is the instruction text alone, which no provider would cache, so marking it
    would bill a cache write that no read amortises.
    """
    retained = _MARKER_SEPARATOR.join(
        name for identity in request.retained for name in identity.mentions
    )
    suffix = (
        f"{_ERASED_LABEL} {_MARKER_SEPARATOR.join(request.erased.mentions)}\n"
        f"{_RETAINED_LABEL} {retained}\n"
        f"{_DOCUMENT_LABEL}\n{request.body}"
    )
    return Prompt(stable_prefix=REWRITE_INSTRUCTIONS, variable_suffix=suffix)


def body_digest(text: str) -> str:
    """The digest a Derived_Artifact row records for a body."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def segments_of(text: str) -> tuple[str, ...]:
    """The comparable segments of a body: its non-empty lines, stripped.

    Lines are the unit because a rewrite reorganises sentences within a line far
    more often than it reorders lines, so a line-level count reports what a reader
    of the comparison view would call a removed segment.
    """
    return tuple(stripped for line in text.splitlines() if (stripped := line.strip()))


# ---------------------------------------------------------------------------
# Validation, and the single failure every cause collapses to
# ---------------------------------------------------------------------------


def _first_failed_check(
    candidate: str,
    request: RewriteRequest,
    band: RatioBand,
) -> str | None:
    """The first check the answer fails, named without quoting any content."""
    if not candidate.strip():
        return "the answer is empty once stripped"
    folded = candidate.casefold()
    if any(name.casefold() in folded for name in request.erased.mentions):
        return "the answer still names the erased Client"
    if not band.admits(len(request.body), len(candidate)):
        return "the answer's length falls outside the configured ratio band"
    if _loses_a_retained_marker(request, folded):
        return "a retained Client's marker carried by the original is absent from the answer"
    return None


def _loses_a_retained_marker(request: RewriteRequest, folded: str) -> bool:
    """Whether a retained Client the original marked is unmarked in the answer.

    The condition is per retained Client rather than over the union of markers: an
    answer that kept one retained Client's marker and dropped another's has lost
    that other Client's content, and a union test would report the pair as fine.
    """
    original = request.body.casefold()
    for identity in request.retained:
        carried = [marker for marker in identity.markers if marker.casefold() in original]
        if carried and not any(marker.casefold() in folded for marker in carried):
            return True
    return False


def _unavailable(request: RewriteRequest, detail: str) -> ModelUnavailableError:
    """The one failure every cause collapses to, recorded as it is built.

    The measurement and the log record are emitted here rather than by the caller,
    so every path that gives up on a rewrite is counted, and counted once. The
    detail names the check rather than the content, because a message reaches a log
    record and the values passing through here are memory content.
    """
    metric(REDACTION_FAIL_CLOSED_METRIC)
    log(
        Severity.WARNING,
        COMPONENT,
        "a redaction rewrite produced no usable replacement, so the artifact fails closed",
        artifact_id=str(request.artifact_id),
        client_id=str(request.erased.client_id),
        detail=detail,
    )
    refusal = ModelUnavailable(
        f"no usable redaction replacement was produced because {detail}, "
        f"so the artifact is recorded as {FAIL_CLOSED_REASON}"
    )
    refusal.add_note(
        "every provider cause and every validation failure collapses to this one "
        "outcome, because the caller's response to all of them is the hard delete"
    )
    return refusal


def _diff_of(original: str, replacement: str) -> StructuralDiff:
    """The count-only summary of what the rewrite dropped and kept."""
    before = segments_of(original)
    after = frozenset(segments_of(replacement))
    retained = sum(1 for segment in before if segment in after)
    return StructuralDiff(
        removed_segments=len(before) - retained,
        retained_segments=retained,
    )
