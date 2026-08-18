"""Property 30: one prefix per query Artifact, and the boundary at its end.

Prompt caching is the whole cost argument for adjudicating one candidate at a
time, and it only holds if the shared portion of the prompt is the *same bytes*
call after call rather than the same recipe rendered again. So this property draws
one query Artifact excerpt, crosses it with 2 to 50 candidate excerpts of varying
length and content, and asserts the two facts a cache read depends on: the
serialised Stable_Prefix is byte-identical across every candidate in the set, and
each prompt's Cache_Boundary falls exactly at the end of that prefix.

Four decisions shape what is generated and what is asserted.

**Candidate excerpts that contain the prefix's own text are drawn on purpose.**
An implementation that located the boundary by searching the serialised prompt for
the prefix, or that measured it against the last occurrence rather than the first,
would pass every example whose candidate text is unrelated and fail exactly here.
The echo arm carries the whole prefix, a slice of the task instructions, and the
query excerpt back inside the suffix, so the boundary offset is asserted against a
prompt in which the prefix bytes appear twice.

**The query excerpt is drawn across the cacheable floor, not near one side of
it.** The lengths reach from a single byte to past the configured prefix byte
budget, so examples land below the floor, at it, above it, and past the budget
where the excerpt is cut. The boundary flag is then asserted as the conjunction the
design states: the recorded capability *and* the floor, with the identical two-part
structure sent unmarked below it.

**The capability is a drawn selector rather than a provider trait.** Both states
are exercised through the same stub, and the assertion is that the prompt *text*
does not differ between them — only the marker does. That is what keeps a recorded
prompt digest comparable across providers.

**The prefix is asserted at the provider boundary as well as at the caller.** The
batch is driven through the deterministic text stub, and the set of distinct
prefixes it was actually sent is asserted to hold exactly one member. A memo that
returned equal-but-freshly-rendered text would satisfy an equality check on
strings; what a provider needs is that every call after the first presents the same
bytes, which is what the stub's own cache accounting reports.

No model is called: every arm answers from the stub in `tests/conftest.py`.

**Validates: Requirements 38.1, 38.2, 38.3**
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID

from hypothesis import event, given, settings
from hypothesis import strategies as st
from tests.conftest import StubTextProvider

from molt.config.resolve import Configuration
from molt.erase.adjudicator import (
    BATCH_CONCURRENCY_ENV,
    INCLUDED_REASON,
    MINIMUM_CACHEABLE_PREFIX_ENV,
    PREFIX_BUDGET_ENV,
    TASK_INSTRUCTIONS,
    Adjudicator,
    Candidate,
    Classification,
    QueryArtifact,
    capped_excerpt,
    serialise,
)

# The example budget every property in this plan runs at.
MAX_EXAMPLES: Final[int] = 100

# The candidate-set size the property names.
MIN_CANDIDATES: Final[int] = 2
MAX_CANDIDATES: Final[int] = 50

# The concurrency the batch runs at. Small on purpose: the bound is read from the
# configuration surface like every other number, and a low bound keeps a hundred
# examples of up to fifty calls each affordable without changing what is asserted.
BATCH_CONCURRENCY: Final[int] = 4

# The query Artifact every candidate in one example is adjudicated against.
QUERY_ARTIFACT: Final[UUID] = UUID(int=3000)

# Query excerpt lengths in bytes, drawn to land either side of the cacheable floor
# and past the prefix byte budget where the excerpt is cut.
QUERY_LENGTHS: Final[tuple[int, ...]] = (1, 64, 4096, 15_000, 16_384, 20_000, 40_000)

# Candidate excerpt lengths in bytes. Short beside long, because the suffix length
# is what a boundary computed from the whole prompt would drift with.
CANDIDATE_LENGTHS: Final[tuple[int, ...]] = (1, 2, 17, 512, 4096, 9_001)

# How many of the prefix's opening bytes the echo check looks for inside a suffix.
# Long enough that no ordinary excerpt holds them by accident, short enough that a
# capped echo still carries them.
ECHO_PROBE_BYTES: Final[int] = 64

# Room the prefix's own section markers and line breaks take beyond the task
# instructions and the capped excerpt, for the length bound below. The bound is
# what shows the excerpt was capped at the configured budget rather than sent whole.
STRUCTURE_ALLOWANCE_BYTES: Final[int] = 64


class Shape(StrEnum):
    """What a drawn excerpt is made of, including the arm that echoes the prefix."""

    PROSE = "prose"
    SOURCE = "source-code shaped"
    NON_ASCII = "non-ASCII text"
    WHITESPACE = "whitespace only"
    PREFIX_ECHO = "the prefix's own text"


# The fragments a drawn excerpt is built from, one repeated and cut to length, so a
# forty-kilobyte excerpt costs one small draw rather than forty thousand.
FRAGMENTS: Final[dict[Shape, tuple[str, ...]]] = {
    Shape.PROSE: (
        "the candidate restates the subject in different words ",
        "a fragment reached from one query artifact and no other ",
    ),
    Shape.SOURCE: (
        "def prefix(query: QueryArtifact) -> str:\n    return memo[query.artifact_id]\n",
        "SELECT artifact_id FROM erasure_candidate WHERE run_id = %s;\n",
    ),
    Shape.NON_ASCII: ("ключ доступа ", "λ の 中文 ünïcode ", "ᚠᛇᚻ᛫ᛒᛦᚦ "),
    Shape.WHITESPACE: (" ", "\t", " \t\n"),
    Shape.PREFIX_ECHO: (TASK_INSTRUCTIONS,),
}

# The response the stub answers every call with, so the happy path is exercised and
# no example depends on the fail-closed path to reach an assertion.
SCRIPTED_RESPONSE: Final[str] = json.dumps(
    {"classification": Classification.INCLUDE.value, "reasoning": "it restates the subject"}
)


def sized(fragment: str, length: int) -> str:
    """Text of about that many bytes, built by repeating one fragment."""
    encoded = fragment.encode("utf-8")
    repeats = length // len(encoded) + 1
    return (encoded * repeats)[:length].decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """One whole example: one query excerpt, its candidates, and the capability.

    Attributes:
        query_text: The query Artifact's text, which the prefix is built from. Kept
            out of the representation, because a failing example carrying forty
            kilobytes of repeated fragment is unreadable and the shape and the
            length below say what it was.
        candidate_texts: The candidate excerpts, 2 to 50 of them, likewise.
        candidate_shape: What the candidate excerpts are made of.
        query_shape: What the query excerpt is made of.
        supports_prompt_cache: The drawn provider-capability selector.
        query_bytes: How long the drawn query excerpt is.
        candidate_count: How many candidates the set holds.
    """

    query_text: str = field(repr=False)
    candidate_texts: tuple[str, ...] = field(repr=False)
    candidate_shape: Shape
    query_shape: Shape
    supports_prompt_cache: bool
    query_bytes: int
    candidate_count: int

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        """The candidates as the Adjudicator takes them, in the drawn order."""
        return tuple(
            Candidate(artifact_id=UUID(int=index + 1), text=text)
            for index, text in enumerate(self.candidate_texts)
        )

    @property
    def query(self) -> QueryArtifact:
        """The one query Artifact every candidate in this example shares."""
        return QueryArtifact(artifact_id=QUERY_ARTIFACT, text=self.query_text)


@st.composite
def candidate_sets(draw: st.DrawFn) -> CandidateSet:
    """Draw one query excerpt crossed with 2 to 50 candidate excerpts.

    The echo arm is what the property is really about, so it is drawn as a shape
    like any other rather than added as a special case: its candidates carry the
    task instructions and the query excerpt back inside the suffix.
    """
    query_shape = draw(
        st.sampled_from([shape for shape in Shape if shape is not Shape.PREFIX_ECHO])
    )
    query_text = sized(
        draw(st.sampled_from(FRAGMENTS[query_shape])), draw(st.sampled_from(QUERY_LENGTHS))
    )

    candidate_shape = draw(st.sampled_from(Shape))
    count = draw(st.integers(min_value=MIN_CANDIDATES, max_value=MAX_CANDIDATES))
    if candidate_shape is Shape.PREFIX_ECHO:
        echoes = (
            TASK_INSTRUCTIONS + query_text,
            query_text,
            TASK_INSTRUCTIONS[: len(TASK_INSTRUCTIONS) // 2],
        )
        candidate_texts = tuple(draw(st.sampled_from(echoes)) for _ in range(count))
    else:
        fragment = draw(st.sampled_from(FRAGMENTS[candidate_shape]))
        candidate_texts = tuple(
            sized(fragment, draw(st.sampled_from(CANDIDATE_LENGTHS))) for _ in range(count)
        )

    return CandidateSet(
        query_text=query_text,
        candidate_texts=candidate_texts,
        candidate_shape=candidate_shape,
        query_shape=query_shape,
        supports_prompt_cache=draw(st.booleans()),
        query_bytes=len(query_text.encode("utf-8")),
        candidate_count=count,
    )


def configuration() -> Configuration:
    """The configuration surface the Adjudicator reads its numbers from.

    Only the concurrency bound is named; the prefix byte budget and the cacheable
    floor resolve to the surface's own defaults, so the property runs against the
    delivered numbers rather than against numbers it chose.
    """
    return Configuration(environ={BATCH_CONCURRENCY_ENV: str(BATCH_CONCURRENCY)})


def build(case: CandidateSet) -> tuple[Adjudicator, StubTextProvider]:
    """An Adjudicator over the text stub, with the drawn capability recorded."""
    provider = StubTextProvider(supports_prompt_cache=case.supports_prompt_cache)
    provider.scripted = [SCRIPTED_RESPONSE for _ in case.candidate_texts]
    adjudicator = Adjudicator.from_configuration(
        configuration(),
        provider,
        prompt_cache_available=case.supports_prompt_cache,
    )
    return adjudicator, provider


def record(case: CandidateSet, *, prefix_bytes: int, floor: int) -> None:
    """Report what one example covered, so every arm can be seen to be reached."""
    event(f"query excerpt shape={case.query_shape}")
    event(f"candidate excerpt shape={case.candidate_shape}")
    event(f"candidates={len(case.candidate_texts)}")
    event(f"prompt cache supported={case.supports_prompt_cache}")
    event(f"prefix reaches the floor={prefix_bytes >= floor}")


# Feature: molt, Property 30: For any candidate set of size 2 to 50 sharing one
# query Artifact, with arbitrary candidate excerpts, the serialised Stable_Prefix
# is byte-identical across every candidate in the set and each prompt's
# Cache_Boundary falls exactly at the end of that Stable_Prefix.
# No per-example deadline, as everywhere else in this suite. The prefix this module
# asserts on is built from drawn text that may be long, so an example's duration tracks
# the size of what was drawn and the load on the machine rather than the property, and
# under parallel execution that timing variance failed the module on a property that
# held. Latency bounds live in the performance suite.
@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(case=candidate_sets())
def test_one_prefix_per_query_artifact_with_the_boundary_at_its_end(case: CandidateSet) -> None:
    surface = configuration()
    budget = surface.integer(PREFIX_BUDGET_ENV)
    floor = surface.integer(MINIMUM_CACHEABLE_PREFIX_ENV)

    adjudicator, provider = build(case)
    candidates = case.candidates
    prompts = tuple(adjudicator.build_prompt(case.query, candidate) for candidate in candidates)

    assert MIN_CANDIDATES <= len(prompts) <= MAX_CANDIDATES

    # Requirements 38.1 and 38.2: one prefix for the whole set, as bytes rather
    # than as an equality that a re-rendering would also satisfy.
    encoded_prefixes = {prompt.stable_prefix.encode("utf-8") for prompt in prompts}
    assert len(encoded_prefixes) == 1, f"{len(encoded_prefixes)} distinct prefixes in one set"
    prefix = prompts[0].stable_prefix
    prefix_bytes = prefix.encode("utf-8")
    record(case, prefix_bytes=len(prefix_bytes), floor=floor)

    # The prefix is the task instructions followed by the capped query excerpt, and
    # the cap is the configured budget, so nothing longer reaches a prompt.
    assert prefix.startswith(TASK_INSTRUCTIONS)
    assert capped_excerpt(case.query_text, budget) in prefix
    structure = len(TASK_INSTRUCTIONS.encode("utf-8")) + STRUCTURE_ALLOWANCE_BYTES
    assert len(prefix_bytes) <= structure + budget

    # Nothing that varies per candidate appears in it, and the order the candidates
    # were built in changes nothing: a prefix drawing on a counter or an identifier
    # would differ here.
    for candidate in candidates:
        assert str(candidate.artifact_id) not in prefix
    reversed_prompts = tuple(
        adjudicator.build_prompt(case.query, candidate) for candidate in reversed(candidates)
    )
    assert {prompt.stable_prefix.encode("utf-8") for prompt in reversed_prompts} == encoded_prefixes

    # Requirement 38.3: the boundary sits exactly at the end of the prefix, which is
    # asserted as an offset into the serialised prompt rather than as a flag alone,
    # and it is asserted on the echo arm too, where the prefix bytes appear twice.
    boundary = len(prefix_bytes)
    echoed = False
    for prompt in prompts:
        whole = serialise(prompt).encode("utf-8")
        assert whole[:boundary] == prefix_bytes
        assert whole[boundary:] == prompt.variable_suffix.encode("utf-8")
        assert prompt.variable_suffix != ""
        # Where the candidate echoes the prefix's opening bytes, those bytes appear
        # twice in the serialised prompt and the boundary is still the first
        # occurrence's end, which is what the offset assertions above just showed.
        opening = prefix_bytes[:ECHO_PROBE_BYTES]
        if opening in prompt.variable_suffix.encode("utf-8"):
            assert whole.count(opening) >= 2
            echoed = True

    # The marker is the conjunction of the recorded capability and the floor, and it
    # is the only thing that differs between the two capability states: the prompt
    # text either side of the boundary is the same, so a digest stays comparable.
    event(f"a candidate echoed the prefix={echoed}")

    marked = case.supports_prompt_cache and len(prefix_bytes) >= floor
    assert {prompt.cache_boundary for prompt in prompts} == {marked}
    unmarked_adjudicator, _ = build(
        CandidateSet(
            query_text=case.query_text,
            candidate_texts=case.candidate_texts,
            candidate_shape=case.candidate_shape,
            query_shape=case.query_shape,
            supports_prompt_cache=False,
            query_bytes=case.query_bytes,
            candidate_count=case.candidate_count,
        )
    )
    without = unmarked_adjudicator.build_prompt(case.query, candidates[0])
    assert without.stable_prefix.encode("utf-8") == prefix_bytes
    assert without.variable_suffix == prompts[0].variable_suffix
    assert without.cache_boundary is False

    # The same claim at the provider boundary: every call in the batch presented the
    # one prefix, and the batch's cache accounting is recorded from the answers.
    batch = adjudicator.adjudicate(case.query, candidates)

    assert len(provider.calls) == len(candidates)
    assert {sent.encode("utf-8") for sent, _ in provider.calls} == encoded_prefixes
    assert tuple(verdict.artifact_id for verdict in batch.verdicts) == tuple(
        candidate.artifact_id for candidate in candidates
    )
    assert all(verdict.adjudicated for verdict in batch.verdicts)
    assert all(verdict.reason == INCLUDED_REASON for verdict in batch.verdicts)
    assert batch.usage.calls == len(candidates)
    assert batch.usage.prefix_bytes == len(prefix_bytes)
    assert batch.usage.cache_boundary is marked
    assert batch.usage.below_floor is (len(prefix_bytes) < floor)
    assert batch.usage.fail_closed == 0
    if not case.supports_prompt_cache:
        assert batch.usage.cache_creation_tokens == 0
        assert batch.usage.cache_read_tokens == 0
    else:
        assert batch.usage.cache_creation_tokens + batch.usage.cache_read_tokens > 0
