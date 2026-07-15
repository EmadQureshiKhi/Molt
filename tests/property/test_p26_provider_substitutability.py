"""Property 26: which provider answered changes the vectors and nothing else.

Provider choice is a configuration value. That claim is only worth making if
swapping the value changes the vectors and leaves everything the rest of the
system reads unchanged, so this property drives text of 1 to 8192 characters —
prose, source-code shaped fragments, non-ASCII text, and whitespace-only input —
through a stub per configured Embedding_Provider implementation and through the
deliberately non-normalising stub, and asserts four things together: every vector
is 1024 components wide, every vector is unit length within the Embedder's own
tolerance, the schema and the nearest-neighbour query text are byte-identical
across the selections, and the row that lands carries the selected provider name
beside the model identifier and the unit-norm assertion.

Six decisions shape what is generated and what is asserted.

**Each stub reports its own implementation's declared width and normalisation
behaviour, and nothing else about it.** The documented default implementation
declares the width its request asks for and returns vectors that are not
unit-normalised; the delivered implementation declares the width the
configuration surface names and returns vectors that already are. Those two
answers are the whole of what a provider contributes to this property, so the
stubs answer exactly that and no model is called. The distinction is not
cosmetic: it is the reason the scaling step exists at all.

**The non-normalising stub is what makes the norm assertion mean anything.** A
suite drawing only from stubs modelled on the delivered implementation would pass
with the scaling deleted, because the vectors would arrive unit length and be
stored unit length with the Embedder having done nothing. So every non-normalising
arm asserts both halves: the answer the stub gave is *not* unit length, and the
vector the Embedder produced from it *is*. The stub is the one the plan already
ships beside the Embedder rather than a second one written here.

**The query text is asserted as sent, not as declared.** For each selection the
neighbour query is driven against a recording cursor in both of its forms, and the
statements that arrive are compared across selections and against the two module
constants by identity. A provider switch that rebuilt one character of that SQL
would show up as a different string; one that rebuilt the same string would show
up as a different object.

**The row is asserted as bound, not as requested.** The sink drives the real
Embedding insert against the same recording cursor, so what is checked is the
parameter tuple that reaches the statement: the provider name, the model
identifier, the fixed width, and the unit-norm assertion the writing statement
sets. Requirement 37.15 is about what the row carries, and a request object is not
a row.

**Whitespace-only input is embedded rather than skipped, and that is asserted
rather than assumed.** The drain leaves out an Artifact whose text is absent or
empty, because there is nothing to embed and a provider call would learn that
again on every pass. Whitespace-only text is neither absent nor empty, so it takes
the ordinary path and a row lands for it. The two explicit cases at the foot of
the module pin that boundary from both sides, which is what the whitespace arm of
the generator rests on.

**The mismatched-width stub is drawn as an arm and used only to be refused.** It
declares a width the schema does not hold, and it is refused twice over: by the
startup gate, which reports both widths before the text provider is contacted at
all, and by the Embedder's own construction. Neither refusal reaches the stub's
embed call and neither writes anything, which is what "before any Embedding is
written" has to mean.

The example budget is 100 at the default per-example deadline. The text length
spans four orders of magnitude but the vector width does not, and the width is
what the work is proportional to: an example embeds at most three texts through
three selections and renders the query vector twice per selection, which measures
well inside the deadline for the longest text the generator admits. Peak live
allocation stays under a mebibyte: at most three texts of 8192 characters, and one
1024-component vector per text per selection.

**Validates: Requirements 37.5, 37.8, 37.9, 37.15, 10.2, 10.10**
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from io import StringIO
from types import MappingProxyType
from typing import Final
from uuid import UUID, uuid4

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st
from tests.property.embedding_provider_stubs import (
    NonNormalisingEmbeddingProvider,
    StubProbeReport,
    non_unit_vector,
)

from molt.config.resolve import Configuration
from molt.embed import (
    MAX_BATCH_TEXTS,
    NORM_TOLERANCE,
    Embedder,
    EmbeddingSink,
    TextSource,
    unit_scale,
)
from molt.errors import ProviderWidthMismatchError
from molt.models.artifact import EMBEDDING_DIMENSION, ArtifactKind
from molt.models.event import EmbeddingState
from molt.providers import SCHEMA_VECTOR_DIMENSIONS, Prompt, ProviderProbe, TextResult
from molt.providers.bedrock import PROVIDER_NAME as DEFAULT_PROVIDER_NAME
from molt.providers.external_embedding import PROVIDER_NAME as DELIVERED_PROVIDER_NAME
from molt.providers.selector import (
    CONFIGURATION_EXIT_STATUS,
    PROMPT_CACHE_ENV,
    validate_at_startup,
    validate_at_startup_or_exit,
)
from molt.store.embeddings import (
    INSERT_EMBEDDING_STATEMENT,
    NEAREST_SCAN_STATEMENT,
    NEAREST_STATEMENT,
    EmbeddingWrite,
    PendingArtifact,
    insert_embedding,
    select_nearest,
    vector_text,
)
from molt.store.migrate import MIGRATIONS_DIRECTORY, file_digest

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

# The width the property names in so many words. The two constants the source
# holds it under are asserted equal to it, so a reader of the property text and a
# reader of the schema are looking at one number.
FIXED_WIDTH: Final[int] = 1024

# A width the schema does not hold, for the rejection arm. Far enough from the
# fixed width that no arithmetic accident produces it.
MISMATCHED_WIDTH: Final[int] = 512

# The longest text the property draws, and the most texts one example carries. The
# ceiling is the one the property names; the count is small because the batching
# bound is the unit suite's subject and multiplying a maximal text by a maximal
# batch would buy padding rather than shapes.
MAX_TEXT_CHARACTERS: Final[int] = 8192
MAX_TEXTS_PER_EXAMPLE: Final[int] = 3

# The tenant every generated Artifact belongs to, and the instant they are created
# at, read from a fixed offset so no example depends on when it ran.
CLIENT: Final[UUID] = UUID(int=2600)
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
EXPIRY: Final[timedelta] = timedelta(days=90)

# Where the provider name, the model identifier, the width, the unit-norm
# assertion, and the vector sit in the insert's parameter tuple. Named rather than
# spelled as offsets at the assertion, so a reader sees what is being checked.
PROVIDER_PARAM: Final[int] = 3
MODEL_PARAM: Final[int] = 4
WIDTH_PARAM: Final[int] = 5
NORMALISED_PARAM: Final[int] = 6
VECTOR_PARAM: Final[int] = 7

# The batch sizes an example may run at: one text per call, two, and the ceiling.
BATCH_SIZES: Final[tuple[int, ...]] = (1, 2, MAX_BATCH_TEXTS)

# Lengths worth reaching on purpose, drawn alongside a uniform draw over the whole
# range: the shortest text the property admits, a handful of small ones, and the
# ceiling with the character either side of it.
NOTABLE_LENGTHS: Final[tuple[int, ...]] = (1, 2, 3, 25, 512, 4096, 8191, 8192)


# ---------------------------------------------------------------------------
# What each selection is, as a contract rather than as an implementation
# ---------------------------------------------------------------------------


class Selection(StrEnum):
    """The selections the property is quantified over.

    The first two stand for the two registered embedding implementations, the
    third is the stub whose whole purpose is to answer at the wrong magnitude, and
    the fourth declares a width the schema does not hold and exists to be refused.
    """

    DEFAULT = "the documented default implementation"
    DELIVERED = "the delivered implementation"
    NON_NORMALISING = "the non-normalising stub"
    MISMATCHED = "the mismatched-width stub"


@dataclass(frozen=True, slots=True)
class Implementation:
    """One selection's declared width and normalisation behaviour, and nothing more.

    These two facts are all a provider contributes to this property, which is why
    the record holds them and no client, no address, and no credential. The
    normalisation flag is read from the requirements rather than chosen: the
    delivered implementation returns unit-normalised vectors and the documented
    default does not, and that difference is what the scaling step exists for.

    Attributes:
        selection: Which arm of the generator this record answers for.
        name: The provider name stored on every row the selection writes.
        model_id: The model identifier stored beside it.
        dimensions: The width the selection declares to the startup gate.
        normalises: Whether the selection's own answers are already unit length.
    """

    selection: Selection
    name: str
    model_id: str
    dimensions: int
    normalises: bool


# The non-normalising stub, built once so its own declared name, model identifier,
# and width are read off it rather than restated here.
_NON_NORMALISING: Final[NonNormalisingEmbeddingProvider] = NonNormalisingEmbeddingProvider()

IMPLEMENTATIONS: Final[Mapping[Selection, Implementation]] = MappingProxyType(
    {
        Selection.DEFAULT: Implementation(
            selection=Selection.DEFAULT,
            name=DEFAULT_PROVIDER_NAME,
            model_id=f"{DEFAULT_PROVIDER_NAME}-embedding-stub",
            dimensions=SCHEMA_VECTOR_DIMENSIONS,
            normalises=False,
        ),
        Selection.DELIVERED: Implementation(
            selection=Selection.DELIVERED,
            name=DELIVERED_PROVIDER_NAME,
            model_id=f"{DELIVERED_PROVIDER_NAME}-embedding-stub",
            dimensions=SCHEMA_VECTOR_DIMENSIONS,
            normalises=True,
        ),
        Selection.NON_NORMALISING: Implementation(
            selection=Selection.NON_NORMALISING,
            name=_NON_NORMALISING.name,
            model_id=_NON_NORMALISING.model_id,
            dimensions=_NON_NORMALISING.dimensions,
            normalises=False,
        ),
        Selection.MISMATCHED: Implementation(
            selection=Selection.MISMATCHED,
            name="mismatched-width-stub",
            model_id="mismatched-width-stub-embedding",
            dimensions=MISMATCHED_WIDTH,
            normalises=False,
        ),
    }
)

# The selections a vector is actually produced through. The mismatched one is
# absent because no vector is ever obtained from it.
PRODUCING: Final[tuple[Implementation, ...]] = (
    IMPLEMENTATIONS[Selection.DEFAULT],
    IMPLEMENTATIONS[Selection.DELIVERED],
    IMPLEMENTATIONS[Selection.NON_NORMALISING],
)

# How often each selection is drawn as the one that writes. The three producing
# selections share the weight evenly and the rejection arm takes a seventh, which
# is enough examples to reach it many times over without spending a quarter of the
# budget on an arm that writes nothing.
SELECTION_POOL: Final[tuple[Selection, ...]] = (
    Selection.DEFAULT,
    Selection.DEFAULT,
    Selection.DELIVERED,
    Selection.DELIVERED,
    Selection.NON_NORMALISING,
    Selection.NON_NORMALISING,
    Selection.MISMATCHED,
)


def answered_vector(text: str, *, dimensions: int, normalises: bool) -> tuple[float, ...]:
    """The vector one selection answers for one text, as its contract says it does.

    Reproducible from the text alone, so the property can hold the answer the
    provider gave beside the vector the Embedder produced from it and assert about
    both. A selection that normalises answers the unit vector; one that does not
    answers the same direction at a magnitude drawn well away from one.
    """
    raw = non_unit_vector(text, dimensions)
    return unit_scale(raw) if normalises else raw


@dataclass(slots=True)
class FaithfulEmbeddingProvider:
    """A stub answering as one configured implementation's contract says it answers.

    It carries the declared width and the normalisation behaviour and no other
    trait of the implementation it stands for: no client, no address, no
    credential, and no call that leaves this process. Every text it is asked about
    is recorded, so the property reads what was sent without a round trip.
    """

    name: str
    model_id: str
    dimensions: int
    normalises: bool
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Return one vector per input text, in the input order."""
        self.calls.append(tuple(texts))
        return [
            answered_vector(text, dimensions=self.dimensions, normalises=self.normalises)
            for text in texts
        ]

    def probe(self) -> StubProbeReport:
        """Report reachability and the declared width the startup gate reads."""
        return StubProbeReport(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            dimensions=self.dimensions,
        )


Stub = FaithfulEmbeddingProvider | NonNormalisingEmbeddingProvider


def build_stub(spec: Implementation) -> Stub:
    """The stub standing in for one selection.

    The non-normalising selection is the module the plan already ships beside the
    Embedder rather than a second stub written here, which is what keeps one
    statement of why it exists rather than two.
    """
    if spec.selection is Selection.NON_NORMALISING:
        return NonNormalisingEmbeddingProvider()
    return FaithfulEmbeddingProvider(
        name=spec.name,
        model_id=spec.model_id,
        dimensions=spec.dimensions,
        normalises=spec.normalises,
    )


@dataclass(slots=True)
class CountingTextProvider:
    """The text role, counting probes so the width refusal can be seen to precede it.

    The startup gate takes both providers, and the width comparison is meant to
    happen before the text provider is contacted at all. A count of zero after a
    refusal is what says so.
    """

    name: str = "text-stub"
    model_id: str = "text-stub-generation"
    supports_prompt_cache: bool = False
    probes: int = 0

    def generate(self, prompt: Prompt) -> TextResult:
        """Answer a prompt with its own suffix, which nothing here asks for."""
        return TextResult(
            text=prompt.variable_suffix,
            model_id=self.model_id,
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )

    def probe(self) -> ProviderProbe:
        """Record the probe and report the capability this stub declares."""
        self.probes += 1
        return ProviderProbe(
            name=self.name,
            model_id=self.model_id,
            reachable=True,
            supports_prompt_cache=self.supports_prompt_cache,
        )


# ---------------------------------------------------------------------------
# The store, driven as statements and bound parameters rather than as rows
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecordingCursor:
    """A cursor collecting the statements and the parameters that reach it.

    This is what lets the row assertion be about the row: the real Embedding
    insert runs against this, so the provider name, the model identifier, the
    width, and the unit-norm assertion are read out of the parameter tuple the
    statement was sent with rather than out of the request that asked for it. No
    driver is installed and no cluster is reached.
    """

    sent: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    returning: tuple[object, ...] = ()

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Collect one statement and its bound parameters, sending nothing."""
        self.sent.append((query, () if params is None else tuple(params)))
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """The row the insert's own RETURNING clause is read from."""
        return self.returning

    def fetchall(self) -> list[tuple[object, ...]]:
        """No rows, because nothing here holds a corpus to answer from."""
        return []

    def close(self) -> None:
        """Release this cursor, which a collector in memory needs nothing for."""
        return

    @property
    def statements(self) -> tuple[str, ...]:
        """Every statement sent, in the order it was sent."""
        return tuple(statement for statement, _ in self.sent)


def a_cursor() -> RecordingCursor:
    """A recording cursor whose insert answers an identifier and a creation instant."""
    return RecordingCursor(returning=(uuid4(), MOMENT))


@dataclass(slots=True)
class WritingSink:
    """The store surface the drain uses, sending the real insert to a recording cursor.

    The three calls are the three the Embedder needs. The write is not simulated:
    it goes through the module that owns the Embedding statement, so the property
    asserts about what that statement was sent rather than about what this double
    chose to remember.
    """

    owed: list[PendingArtifact] = field(default_factory=list)
    cursor: RecordingCursor = field(default_factory=a_cursor)
    written: list[EmbeddingWrite] = field(default_factory=list)
    transitions: list[tuple[UUID, EmbeddingState]] = field(default_factory=list)

    def pending_artifacts(self, *, limit: int | None = None) -> Sequence[PendingArtifact]:
        """The Artifacts owing a vector, oldest first and bounded by the limit."""
        bound = len(self.owed) if limit is None else limit
        return tuple(self.owed[:bound])

    def write_embedding(self, request: EmbeddingWrite) -> UUID:
        """Write one vector through the statement that owns the Embedding row."""
        written = insert_embedding(self.cursor, request)
        self.written.append(request)
        return written

    def mark_embedding_state(
        self,
        artifact_id: UUID,
        client_id: UUID,
        state: EmbeddingState,
    ) -> EmbeddingState | None:
        """Record one state transition and report the state as taken."""
        assert client_id == CLIENT
        self.transitions.append((artifact_id, state))
        return state


def owing(index: int, *, kind: ArtifactKind = ArtifactKind.DERIVED_ARTIFACT) -> PendingArtifact:
    """One Artifact owing a vector, created `index` seconds after the fixed instant."""
    return PendingArtifact(
        artifact_id=UUID(int=index + 1),
        artifact_kind=kind,
        client_id=CLIENT,
        created_at=MOMENT + timedelta(seconds=index),
    )


def reader(bodies: Mapping[UUID, str]) -> TextSource:
    """A text reader answering the drawn text per Artifact, and nothing for others."""

    def read(group: Sequence[PendingArtifact]) -> Mapping[UUID, str]:
        return {
            artifact.artifact_id: bodies[artifact.artifact_id]
            for artifact in group
            if artifact.artifact_id in bodies
        }

    return read


def build_embedder(
    provider: Stub,
    sink: EmbeddingSink,
    *,
    texts: TextSource,
    batch_size: int = MAX_BATCH_TEXTS,
) -> Embedder:
    """An Embedder over one stub, waiting for nothing and drawing no jitter."""
    return Embedder(
        provider=provider,
        sink=sink,
        texts=texts,
        expiry=EXPIRY,
        batch_size=batch_size,
        sleep=lambda _: None,
        jitter=lambda low, _: low,
    )


def startup_configuration() -> Configuration:
    """The configuration surface the startup gate reads.

    Only the prompt-cache preference is named, because the width comparison is
    reached before anything else on the surface is consulted and this property is
    about that comparison.
    """
    return Configuration(environ={PROMPT_CACHE_ENV: "auto"})


def norm_of(vec: Sequence[float]) -> float:
    """The L2 norm of a vector, summed the way both the Embedder and the store sum it."""
    return math.sqrt(math.fsum(component * component for component in vec))


def query_statements(query_vector: Sequence[float]) -> tuple[str, ...]:
    """The neighbour statements a selection's vector is actually queried with.

    Both forms are driven, because a tier serving the ordering from the index and
    a tier answering by exact scan must each be the same question under either
    provider selection. Nothing is read back: the statement text is the subject.
    """
    cursor = a_cursor()
    for served in (True, False):
        select_nearest(
            cursor,
            query_vector,
            permitted_clients=(CLIENT,),
            index_served=served,
        )
    return cursor.statements


def schema_digest() -> tuple[tuple[str, str], ...]:
    """The digest of every migration file, which is the schema byte for byte.

    A digest rather than a parse: the claim is byte-identity across provider
    selections, and a digest over the file's bytes is exactly that claim.
    """
    return tuple(
        (path.name, file_digest(path)) for path in sorted(MIGRATIONS_DIRECTORY.glob("*.sql"))
    )


# The schema as it stands before any selection is made, captured once.
SCHEMA: Final[tuple[tuple[str, str], ...]] = schema_digest()


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


class Shape(StrEnum):
    """What the drawn text is made of, which is the dimension the property names."""

    PROSE = "prose"
    SOURCE = "source-code shaped"
    NON_ASCII = "non-ASCII text"
    WHITESPACE = "whitespace only"


# The fragments each shape is built from. A text of a drawn length is one fragment
# repeated and cut to that length, so an 8192-character example costs one small
# draw rather than eight thousand. Every non-ASCII fragment opens with a character
# outside ASCII and every whitespace fragment holds nothing but whitespace, so a
# text cut to a single character still belongs to the shape it was drawn for.
FRAGMENTS: Final[Mapping[Shape, tuple[str, ...]]] = MappingProxyType(
    {
        Shape.PROSE: (
            "the drain leaves the remainder owed and loses nothing ",
            "a provider that failed one call is unavailable rather than selective ",
            "one vector per artifact per provider and model pair ",
        ),
        Shape.SOURCE: (
            "def scale(vec: Sequence[float]) -> tuple[float, ...]:\n    return unit(vec)\n",
            "class Writer:\n    def flush(self) -> None:\n        self._cursor.close()\n",
            "SELECT vec FROM embedding WHERE client_id = %s ORDER BY vec <-> %s LIMIT 10;\n",
            "if (x !== null) { return x.map((y) => y + 1); }\n",
            "for i in range(0, n):\n\ttotal += weights[i] * values[i]\n",
        ),
        Shape.NON_ASCII: (
            "ключ доступа к памяти ",
            "λ の 中文注释 ünïcode ",
            "ᚠᛇᚻ᛫ᛒᛦᚦ᛫ᚠᚱᚩᚠᚢᚱ ",
            "日本語のコメント と 記号 ",
        ),
        Shape.WHITESPACE: (
            " ",
            "\t",
            "\n",
            " \t\n\r",
            "\u00a0\u2003 ",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ProviderCase:
    """One whole example: the texts, what they are made of, and which selection writes.

    Attributes:
        texts: The drawn texts, each 1 to 8192 characters long.
        shape: What every text in this example is made of.
        selection: Which selection drains and writes, including the mismatched one
            that is refused instead.
        batch_size: How many texts one provider call carries.
    """

    texts: tuple[str, ...]
    shape: Shape
    selection: Selection
    batch_size: int

    @property
    def longest(self) -> int:
        """The length of the longest drawn text, for the coverage record."""
        return max(len(text) for text in self.texts)


def sized(fragment: str, length: int) -> str:
    """A text of exactly that many characters, built by repeating one fragment."""
    return (fragment * (length // len(fragment) + 1))[:length]


def lengths() -> st.SearchStrategy[int]:
    """A drawn text length: the notable ones, and the whole range beside them."""
    return st.one_of(
        st.sampled_from(NOTABLE_LENGTHS),
        st.integers(min_value=1, max_value=MAX_TEXT_CHARACTERS),
    )


@st.composite
def provider_inputs(draw: st.DrawFn) -> ProviderCase:
    """Draw one example of text crossed with a provider selection.

    Every dimension the property is quantified over is drawn here: what the text is
    made of, how long each text is, how many texts one example carries, which
    selection drains and writes, and how many texts one provider call carries.
    """
    shape = draw(st.sampled_from(Shape))
    fragment = draw(st.sampled_from(FRAGMENTS[shape]))
    count = draw(st.integers(min_value=1, max_value=MAX_TEXTS_PER_EXAMPLE))
    texts = tuple(sized(fragment, draw(lengths())) for _ in range(count))
    return ProviderCase(
        texts=texts,
        shape=shape,
        selection=draw(st.sampled_from(SELECTION_POOL)),
        batch_size=draw(st.sampled_from(BATCH_SIZES)),
    )


# ---------------------------------------------------------------------------
# The coverage record
# ---------------------------------------------------------------------------


def length_band(length: int) -> str:
    """Which part of the admitted range a text's length sits in."""
    if length == 1:
        return "one character"
    if length < 1024:
        return "under a kibibyte"
    if length < MAX_TEXT_CHARACTERS:
        return "under the ceiling"
    return "at the ceiling"


def record(case: ProviderCase) -> None:
    """Report what one example covered, so every arm can be seen to be reached."""
    event(f"selection={case.selection}")
    event(f"text shape={case.shape}")
    event(f"longest text={length_band(case.longest)}")
    event(f"texts per example={len(case.texts)}")
    event(f"batch size={case.batch_size}")
    event(f"provider calls={'one' if len(case.texts) <= case.batch_size else 'several'}")


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: molt, Property 26: For any text input of length 1 to 8192 characters,
# paired with each configured Embedding_Provider implementation and with a
# deliberately non-normalising stub, the returned vector has exactly 1024
# dimensions and an L2 norm equal to 1 within floating-point tolerance —
# including every vector produced through the non-normalising stub, which is what
# makes the property exercise the Embedder's own normalisation rather than a
# provider's — the schema and the nearest-neighbour query text are byte-identical
# across provider selections, and the written Embedding row carries the selected
# provider name alongside the model identifier and the unit-norm assertion.
@settings(max_examples=MAX_EXAMPLES)
@given(case=provider_inputs())
def test_every_selection_produces_unit_vectors_over_one_schema_and_one_query(
    case: ProviderCase,
) -> None:
    record(case)

    # The width the property names and the two constants the source holds it under
    # are one number, so a reader of either is reading the same claim.
    assert SCHEMA_VECTOR_DIMENSIONS == EMBEDDING_DIMENSION == FIXED_WIDTH

    # The drawn texts really are what the arm says they are, so an assertion about
    # whitespace-only input is about whitespace-only input.
    for text in case.texts:
        assert 1 <= len(text) <= MAX_TEXT_CHARACTERS
        if case.shape is Shape.WHITESPACE:
            assert text.strip() == ""
        if case.shape is Shape.NON_ASCII:
            assert any(ord(character) > 127 for character in text)

    # Requirements 37.8 and 10.2, and Requirement 10.10 with the step shown to be
    # doing the work: every selection answers at the fixed width, and every vector
    # the Embedder produces is unit length whether the answer was or not. The
    # non-normalising arms assert both halves, because a vector that arrived unit
    # length proves nothing about the scaling that would have made it so.
    queries: dict[str, tuple[str, ...]] = {}
    for spec in PRODUCING:
        stub = build_stub(spec)
        embedder = build_embedder(stub, WritingSink(), texts=reader({}), batch_size=case.batch_size)

        vectors = embedder.embed_texts(case.texts)

        assert len(vectors) == len(case.texts)
        assert embedder.provider_name == spec.name
        for text, vector in zip(case.texts, vectors, strict=True):
            answered = answered_vector(text, dimensions=spec.dimensions, normalises=spec.normalises)
            assert len(answered) == FIXED_WIDTH, (
                f"{spec.selection} declared {spec.dimensions} and answered {len(answered)}"
            )
            assert len(vector) == FIXED_WIDTH, (
                f"{spec.selection} produced a vector of {len(vector)} component(s)"
            )
            assert abs(norm_of(vector) - 1.0) <= NORM_TOLERANCE, (
                f"{spec.selection} produced a vector whose L2 norm is {norm_of(vector)} "
                f"for a {len(text)}-character {case.shape} text"
            )
            if spec.normalises:
                assert abs(norm_of(answered) - 1.0) <= NORM_TOLERANCE
            else:
                # The whole point of the arm: the provider's own answer is a long
                # way from unit length, so the stored norm is the Embedder's work.
                assert abs(norm_of(answered) - 1.0) > NORM_TOLERANCE, (
                    f"{spec.selection} answered at unit length, so this example asserts "
                    "nothing about the scaling"
                )
                assert vector == unit_scale(answered)
        queries[spec.name] = query_statements(vectors[0])

    # Requirement 37.5 read as an invariance rather than as a capability: the
    # neighbour query is the same statement under every selection, and it is the
    # same object, so a selection cannot have rebuilt it identically either. The
    # schema is compared by digest, which is byte-identity by construction.
    assert len(set(queries.values())) == 1, (
        f"the neighbour query text differs across selections: {sorted(queries)}"
    )
    for statements in queries.values():
        assert statements[0] is NEAREST_STATEMENT
        assert statements[1] is NEAREST_SCAN_STATEMENT
    assert schema_digest() == SCHEMA

    spec = IMPLEMENTATIONS[case.selection]
    bodies = {owing(index).artifact_id: text for index, text in enumerate(case.texts)}
    sink = WritingSink(owed=[owing(index) for index in range(len(case.texts))])
    stub = build_stub(spec)

    if case.selection is Selection.MISMATCHED:
        # Requirement 37.9: a declared width the schema does not hold is refused by
        # the startup gate, which reports both widths, and refused again at
        # construction. Neither refusal reaches the provider and neither writes.
        text_provider = CountingTextProvider()

        with pytest.raises(ProviderWidthMismatchError) as gate:
            validate_at_startup(startup_configuration(), stub, text_provider)
        with pytest.raises(ProviderWidthMismatchError) as built:
            build_embedder(stub, sink, texts=reader(bodies), batch_size=case.batch_size)

        assert (gate.value.reported, gate.value.required) == (MISMATCHED_WIDTH, FIXED_WIDTH)
        assert (built.value.reported, built.value.required) == (MISMATCHED_WIDTH, FIXED_WIDTH)
        assert text_provider.probes == 0, (
            "the width comparison happens before the text provider is contacted"
        )
        assert stub.calls == [], "a refused provider is never asked for a vector"
        assert sink.written == []
        assert sink.cursor.sent == [], "no embedding is written before the gate is passed"
        return

    # Requirement 37.15, asserted on the parameters the insert was sent rather than
    # on the request that asked for it: the provider name, the model identifier, the
    # fixed width, the unit-norm assertion, and the vector as it was rendered.
    # Whitespace-only text takes this path like any other, because the drain leaves
    # out absent and empty text and whitespace-only text is neither.
    outcome = build_embedder(
        stub,
        sink,
        texts=reader(bodies),
        batch_size=case.batch_size,
    ).drain(len(case.texts))

    assert outcome.written == len(case.texts)
    assert outcome.skipped == 0, "whitespace-only text is embedded rather than skipped"
    assert outcome.deferred == 0
    assert len(sink.cursor.sent) == len(case.texts)
    for write, (statement, params) in zip(sink.written, sink.cursor.sent, strict=True):
        assert statement is INSERT_EMBEDDING_STATEMENT
        assert params[PROVIDER_PARAM] == spec.name == stub.name
        assert params[MODEL_PARAM] == spec.model_id == stub.model_id
        assert params[WIDTH_PARAM] == FIXED_WIDTH
        assert params[NORMALISED_PARAM] is True
        assert params[VECTOR_PARAM] == vector_text(write.vec)
        assert abs(norm_of(write.vec) - 1.0) <= NORM_TOLERANCE


# ---------------------------------------------------------------------------
# Two explicit cases the property rests on
# ---------------------------------------------------------------------------


def test_whitespace_only_text_is_embedded_and_empty_text_is_not() -> None:
    """The boundary the whitespace arm of the generator stands on, from both sides.

    The drain leaves out an Artifact whose text is absent or empty, because there is
    nothing to embed and a provider call would learn that again on every pass.
    Whitespace-only text is neither absent nor empty, so it is embedded like any
    other text and a row lands for it. Stating that here is what stops the
    whitespace arm from asserting an assumption.
    """
    spec = IMPLEMENTATIONS[Selection.NON_NORMALISING]
    stub = build_stub(spec)
    sink = WritingSink(owed=[owing(0), owing(1), owing(2)])
    bodies = {owing(0).artifact_id: "   \t\n", owing(1).artifact_id: ""}

    outcome = build_embedder(stub, sink, texts=reader(bodies)).drain(3)

    assert (outcome.written, outcome.skipped) == (1, 2)
    assert [write.artifact_id for write in sink.written] == [owing(0).artifact_id]
    assert stub.calls == [("   \t\n",)]


def test_a_mismatched_width_leaves_the_process_non_zero_and_names_both_widths() -> None:
    """Requirement 37.9's other half: the refusal is reported and the status is non-zero.

    The gate is raised as the interpreter's own exit rather than by ending the
    process, so buffered output still flushes and the refusal is drivable without a
    subprocess. Both widths are on the stream, because an operator choosing a
    different model needs to see what was reported and what is required.
    """
    stub = build_stub(IMPLEMENTATIONS[Selection.MISMATCHED])
    stream = StringIO()

    with pytest.raises(SystemExit) as exit_status:
        validate_at_startup_or_exit(
            startup_configuration(),
            stub,
            CountingTextProvider(),
            stream=stream,
        )

    assert exit_status.value.code == CONFIGURATION_EXIT_STATUS
    reported = stream.getvalue()
    assert str(MISMATCHED_WIDTH) in reported
    assert str(FIXED_WIDTH) in reported
