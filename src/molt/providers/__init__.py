"""Model access expressed as protocols, with one implementation per provider.

No provider software development kit is imported outside this subpackage, so
provider choice stays a configuration decision. That containment only holds if
the two protocols below are complete enough that no caller ever needs to reach
around them, which is why the prompt shape, the result shape, and the probe shape
are declared here as well rather than left to each implementation.

Four shapes and four protocols:

- `EmbeddingProvider` turns texts into vectors and reports the width it declares.
  The width is read once at startup and compared against the width the schema
  fixes; a provider declaring any other width is refused before a single vector
  is written, because a mismatch discovered one insert at a time by a column
  constraint is a mismatch discovered too late.
- `TextProvider` answers a structured prompt and reports whether the model itself
  supports prompt caching. That capability is read from the model rather than
  assumed, because the caller marks the cache boundary only where marking it
  means something.
- `Prompt` is the two-part prompt every text call sends: a stable prefix that is
  byte-identical across every candidate within one batch, a variable suffix that
  differs per candidate, and a flag marking the boundary that sits exactly at the
  end of the prefix. Freezing it is what makes the prefix safe to memoise and
  reuse; a mutable prompt would let one candidate's edit change another's bytes.
- `TextResult` carries the generated text, the model that produced it, and four
  token counts. The two cache counts are part of the shape rather than an
  optional extra, because the cost argument for prompt caching is only as good as
  the measured hit ratio and a count nobody records is a count nobody can check.
- `ProviderProbe` is what a probe answers with: reachability always, the declared
  width where the provider embeds, and the prompt-cache capability where the
  provider generates.

Both protocol methods return a structural type rather than the concrete shape,
`ProbeLike` and `TextResultLike`. That is deliberate: the deterministic stubs the
test suites drive every property with are not built from these classes and must
not have to be, so the contract is *carries these fields* rather than *is this
class*. The concrete shapes are what implementations construct, and they satisfy
the structural types by construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "SCHEMA_VECTOR_DIMENSIONS",
    "EmbeddingProvider",
    "ProbeLike",
    "Prompt",
    "PromptLike",
    "ProviderProbe",
    "TextProvider",
    "TextResult",
    "TextResultLike",
]

# The vector width the schema fixes and the startup gate compares against. It is
# a constant rather than a knob: the stored column and the distributed vector
# index are declared at this width, so a different width is a refusal rather than
# a reconfiguration.
SCHEMA_VECTOR_DIMENSIONS: Final[int] = 1024


# --------------------------------------------------------------------------
# Structural shapes
# --------------------------------------------------------------------------


@runtime_checkable
class PromptLike(Protocol):
    """The two parts of a prompt a text implementation reads.

    Declaring the shape structurally lets a caller hold a prompt built elsewhere,
    including one built by a test double, without that prompt having to be an
    instance of the concrete shape below.
    """

    @property
    def stable_prefix(self) -> str:
        """The portion of the prompt that repeats byte-identically across calls."""

    @property
    def variable_suffix(self) -> str:
        """The portion of the prompt that differs on each call."""


@runtime_checkable
class ProbeLike(Protocol):
    """What a probe answers with, as fields rather than as a class."""

    @property
    def name(self) -> str:
        """The provider name, which is stored alongside every vector written."""

    @property
    def model_id(self) -> str:
        """The model identifier the provider answered from."""

    @property
    def reachable(self) -> bool:
        """Whether the probe reached the model at all."""

    @property
    def dimensions(self) -> int | None:
        """The declared vector width, or nothing where the role is not embedding."""

    @property
    def supports_prompt_cache(self) -> bool | None:
        """The reported cache capability, or nothing where the role is not text."""


@runtime_checkable
class TextResultLike(Protocol):
    """What a text call answers with, as fields rather than as a class."""

    @property
    def text(self) -> str:
        """The generated text."""

    @property
    def model_id(self) -> str:
        """The model that produced the text."""

    @property
    def input_tokens(self) -> int:
        """Tokens charged as input, excluding any charged as a cache write or read."""

    @property
    def output_tokens(self) -> int:
        """Tokens charged as output."""

    @property
    def cache_creation_tokens(self) -> int:
        """Tokens charged for writing a prefix into the cache, zero where none were."""

    @property
    def cache_read_tokens(self) -> int:
        """Tokens charged for reading a prefix from the cache, zero where none were."""


# --------------------------------------------------------------------------
# Concrete shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Prompt:
    """A two-part prompt with the cache boundary sitting at the end of the prefix.

    Frozen because the prefix is built once per query artifact and reused for
    every candidate in the batch, and slotted because one is built per candidate.
    The boundary is a flag rather than an offset: it always sits immediately after
    the prefix, so an offset would be a second encoding of a position the two
    fields already fix.
    """

    stable_prefix: str
    variable_suffix: str
    cache_boundary: bool = False


@dataclass(frozen=True, slots=True)
class TextResult:
    """A generated result together with the token accounting the cost story reads.

    The two cache counts are separate from `input_tokens` rather than folded into
    it, because the hit ratio is the quantity worth measuring and it is not
    recoverable from a single total.
    """

    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    """The answer to a probe: reachability, plus whichever capability the role has.

    Both capability fields are optional because a provider fills the one its role
    has and leaves the other absent, and absent is not the same as false: a text
    provider reporting nothing about vector width is silent, not one-dimensional.
    """

    name: str
    model_id: str
    reachable: bool
    dimensions: int | None = None
    supports_prompt_cache: bool | None = None


# --------------------------------------------------------------------------
# The two protocols, which are the only way model access happens
# --------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """The one way a vector is obtained.

    `name` and `model_id` are both stored on every vector row, so a corpus
    embedded across a provider change stays distinguishable row by row rather
    than silently mixed.
    """

    name: str
    model_id: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one vector per input text, in the input order.

        The return is a sequence of sequences rather than a list of lists so an
        implementation may answer with tuples, which are cheaper to hold and
        cannot be mutated after the caller normalises them.
        """
        ...

    def probe(self) -> ProbeLike:
        """Report reachability and the declared vector width the startup gate reads."""
        ...


@runtime_checkable
class TextProvider(Protocol):
    """The one way generated text is obtained.

    `supports_prompt_cache` is the model's own reported capability rather than an
    assumption, and it is what decides whether the caller marks the cache
    boundary on a prompt.

    The per-call bounds the design sketches — a token ceiling and a timeout — are
    constructor state on each implementation rather than parameters here, because
    both resolve from the configuration surface once per process rather than
    varying per call, and because keeping the call to one argument keeps the
    protocol satisfiable by a double that computes its answer.
    """

    name: str
    model_id: str
    supports_prompt_cache: bool

    def generate(self, prompt: Prompt) -> TextResultLike:
        """Answer a two-part prompt, reporting the token accounting for the call."""
        ...

    def probe(self) -> ProbeLike:
        """Report reachability and the prompt-cache capability the selector records."""
        ...
