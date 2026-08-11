"""Property 28: the tool server reads, stays inside its configured tenancy, and records.

**Validates: Requirements 40.6, 40.7, 40.8, 40.9, 40.10**

Every clause of this property is a claim about statements and about the one Event
each invocation owes, so the property is driven against a recording fake cluster
rather than a live one. That is a deliberate choice and not a convenience: the
claim "no invocation changes any row of any memory-content table" is strongest when
it is read as "no invocation sends a statement that could change one", because a
live-cluster reading would only show that these particular rows survived these
particular calls, while the statement log shows that no mutation was attempted at
all. On a deployment the same guarantee is held a second time by the reader role,
which the server refuses to start without.

The harness answers the two closure statements by applying the statement's own
bound tenancy array and its own bound limit, so a server that bound the wrong array
or no limit is answered with rows the assertions reject rather than quietly
tolerated.

The generator crosses a corpus and its permitted subset with 1 to 20 invocations
across all four exposed tools. Arguments carry well-formed identifiers of the
corpus, identifiers no Artifact holds, identifiers only an unpermitted Client is
bound to, requested result counts above and below the configured maximum, and extra
keys attempting to name a client set. Every corpus keeps at least one Client outside
the permitted set, so a widening argument always has something to reach for.
"""

from __future__ import annotations

import json
from typing import Final
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.mcp.harness import (
    MUTATING_KEYWORDS,
    Artifact,
    Corpus,
    RecordingSink,
    build_server,
)

from molt.mcpserver import UnknownToolError
from molt.mcpserver.tools import (
    ANCESTORS_TOOL,
    DESCENDANTS_TOOL,
    RECALL_TOOL,
    RESIDUE_TOOL,
    SELECT_PERMITTED_ANCESTORS_STATEMENT,
    SELECT_PERMITTED_DESCENDANTS_STATEMENT,
)
from molt.models.artifact import ArtifactKind
from molt.models.event import EventCategory, JsonValue

pytestmark = pytest.mark.mcp

# How many examples the property runs.
MAX_EXAMPLES: Final[int] = 100

# The kinds a planted Artifact may carry, which are the kinds a lineage node may be.
PLANTED_KINDS: Final[tuple[ArtifactKind, ...]] = (
    ArtifactKind.EVENT,
    ArtifactKind.DERIVED_ARTIFACT,
)

# The keys an invocation attempts to widen its permitted set with. None of them is
# declared by any tool schema, which is why none of them is read.
WIDENING_KEYS: Final[tuple[str, ...]] = (
    "permitted_clients",
    "client_ids",
    "clients",
    "client_set",
)

# Query text is drawn from letters that appear in no hexadecimal digest, so the
# assertion that a raw argument value never reaches the recorded payload cannot
# pass or fail by coincidence.
_QUERY_ALPHABET: Final[str] = "ghijklmnopqrstuvwxyz "

# The shortest argument value the absence assertion is made about, which is longer
# than any field name of the recorded payload.
_SHORTEST_ASSERTED_VALUE: Final[int] = 5

# The names a client may ask for that the registry does not carry, including the
# mutation verbs of the components the read-only tools are backed by.
UNDISPATCHABLE_NAMES: Final[tuple[str, ...]] = (
    "molt.erase",
    "molt.record",
    "molt.insert_edge",
    "molt.supersede_attribution",
    "molt.residue_record",
    "tools/call",
    "",
)


@st.composite
def corpora_with_permissions(draw: st.DrawFn) -> Corpus:
    """A corpus of 2 to 5 Clients, one to all-but-one of them permitted."""
    clients = draw(st.lists(st.uuids(), min_size=2, max_size=5, unique=True))
    permitted_count = draw(st.integers(min_value=1, max_value=len(clients) - 1))
    permitted = tuple(clients[:permitted_count])
    placed = draw(
        st.lists(
            st.tuples(
                st.uuids(),
                st.sampled_from(PLANTED_KINDS),
                st.lists(st.sampled_from(clients), min_size=1, max_size=len(clients), unique=True),
            ),
            min_size=1,
            max_size=10,
            unique_by=lambda planted: planted[0],
        )
    )
    return Corpus(
        clients=tuple(clients),
        permitted=permitted,
        slugs=tuple(f"tenant-{index}" for index in range(permitted_count)),
        artifacts=tuple(
            Artifact(artifact_id=found, kind=kind, bound_clients=frozenset(bound))
            for found, kind, bound in placed
        ),
    )


@st.composite
def _arguments(
    draw: st.DrawFn,
    corpus: Corpus,
    tool: str,
    max_results: int,
) -> dict[str, JsonValue]:
    """One argument mapping for one tool, with the widening attempts folded in."""
    arguments: dict[str, JsonValue] = {}
    if tool == RECALL_TOOL:
        arguments["query_text"] = draw(
            st.text(alphabet=_QUERY_ALPHABET, min_size=8, max_size=24).filter(
                lambda text: bool(text.strip())
            )
        )
    elif tool in (ANCESTORS_TOOL, DESCENDANTS_TOOL):
        pool = list(corpus.visible_ids()) + list(corpus.hidden_ids())
        named = draw(st.lists(st.sampled_from(pool), min_size=1, max_size=4, unique=True))
        absent = draw(st.lists(st.uuids(), min_size=0, max_size=2, unique=True))
        arguments["artifact_ids"] = [str(found) for found in named + list(absent)]
    else:
        arguments["run_id"] = str(draw(st.uuids()))
    if draw(st.booleans()):
        arguments["limit"] = draw(st.integers(min_value=1, max_value=max_results * 2 + 1))
    for key in draw(st.lists(st.sampled_from(WIDENING_KEYS), max_size=3, unique=True)):
        arguments[key] = [str(client) for client in corpus.unpermitted]
    return arguments


Invocation = tuple[str, dict[str, JsonValue]]
Case = tuple[Corpus, int, tuple[Invocation, ...]]


@st.composite
def mcp_invocations(draw: st.DrawFn) -> Case:
    """A corpus, a configured maximum result count, and 1 to 20 invocations."""
    corpus = draw(corpora_with_permissions())
    max_results = draw(st.integers(min_value=1, max_value=12))
    count = draw(st.integers(min_value=1, max_value=20))
    invocations: list[tuple[str, dict[str, JsonValue]]] = []
    for _ in range(count):
        tool = draw(st.sampled_from((RECALL_TOOL, ANCESTORS_TOOL, DESCENDANTS_TOOL, RESIDUE_TOOL)))
        invocations.append((tool, draw(_arguments(corpus, tool, max_results))))
    return corpus, max_results, tuple(invocations)


# Feature: molt, Property 28: For any corpus with any permitted Client set, and any
# sequence of invocations across every tool the Molt_MCP_Server exposes — including
# invocations whose arguments attempt to name a client set — no invocation changes
# any row of any memory-content table, every returned Artifact carries at least one
# Client_Binding within the configured permitted Client set, every result length is
# at most the configured maximum result count, and each invocation produces exactly
# one recording Event naming the tool and the returned result count with no
# unredacted argument value.
@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=mcp_invocations())
def test_no_invocation_mutates_and_every_result_stays_inside_the_configured_tenancy(
    case: Case,
) -> None:
    corpus, max_results, invocations = case
    sink = RecordingSink()
    server, log = build_server(corpus, max_results=max_results, sink=sink)

    permitted = set(corpus.permitted)
    visible = set(corpus.visible_ids())
    forbidden = {str(client) for client in corpus.unpermitted}

    for name, arguments in invocations:
        log.clear()
        before = len(sink.events)
        result = server.invoke(name, arguments)

        # Requirement 40.6: nothing sent could change a row, whatever the arguments.
        for statement in log.statements():
            upper = statement.upper()
            for keyword in MUTATING_KEYWORDS:
                assert keyword not in upper, (
                    f"the invocation of {name} sent a statement carrying {keyword}, "
                    "so it was not a read"
                )

        # Requirements 40.7 and 40.8: the tenancy inside the statement is the
        # configured set, and no Client an argument named reaches a bound parameter.
        for sent in log.sent:
            rendered = json.dumps(sent.parameters, default=str)
            for outside in forbidden:
                assert outside not in rendered, (
                    f"the invocation of {name} bound the client {outside}, which "
                    "configuration never permitted"
                )
            if sent.statement in (
                SELECT_PERMITTED_ANCESTORS_STATEMENT,
                SELECT_PERMITTED_DESCENDANTS_STATEMENT,
            ):
                assert sent.parameters[1] == list(corpus.permitted)
                assert sent.parameters[2] == _expected_bound(arguments, max_results)

        # Requirement 40.10: the bound holds, whatever was asked for.
        assert result.count <= max_results, (
            f"the invocation of {name} returned {result.count} rows where the "
            f"configured maximum is {max_results}"
        )

        # Requirement 40.8: every Artifact returned is one a permitted Client holds
        # a current binding to.
        for row in result.rows:
            found = row.get("artifact_id")
            if isinstance(found, str):
                assert UUID(found) in visible, (
                    f"the invocation of {name} returned the artifact {found}, which no "
                    "permitted client holds a current binding to"
                )

        # Requirement 40.9: exactly one Event, naming the tool and the count, with
        # every argument value digested rather than carried.
        assert len(sink.events) == before + 1, (
            f"the invocation of {name} produced {len(sink.events) - before} recording events"
        )
        recorded = sink.events[-1]
        assert recorded.category is EventCategory.TOOL_CALL
        assert recorded.redacted
        assert recorded.client_id in permitted
        assert recorded.payload["tool"] == name
        assert recorded.payload["result_count"] == result.count
        digested = recorded.payload["arguments"]
        assert isinstance(digested, dict)
        assert set(digested) == set(arguments)
        rendered_payload = json.dumps(recorded.payload, default=str)
        for key, value in arguments.items():
            assert isinstance(digested[key], str)
            assert str(digested[key]).startswith("sha256:")
            for raw in _leaves(value):
                assert raw not in rendered_payload, (
                    f"the recording event for {name} carried the unredacted argument value of {key}"
                )

    # No name outside the registry is dispatchable, so no mutation tool is reachable
    # and none is recorded.
    for absent in UNDISPATCHABLE_NAMES:
        log.clear()
        recorded_before = len(sink.events)
        with pytest.raises(UnknownToolError):
            server.invoke(absent, {"artifact_ids": [str(uuid4())]})
        assert not log.sent
        assert len(sink.events) == recorded_before


def _expected_bound(arguments: dict[str, JsonValue], max_results: int) -> int:
    """The limit the statement must bind: what was asked, never above the maximum."""
    asked = arguments.get("limit")
    if isinstance(asked, bool) or not isinstance(asked, int) or asked < 1:
        return max_results
    return min(asked, max_results)


def _leaves(value: JsonValue) -> tuple[str, ...]:
    """Every text an argument value carries, which is what must not be restated.

    A value shorter than the shortest key name is not asserted about, because a
    one-character value occurs inside the payload's own field names by coincidence
    rather than by anything having been carried.
    """
    if isinstance(value, str):
        return (value,) if len(value.strip()) >= _SHORTEST_ASSERTED_VALUE else ()
    if isinstance(value, list):
        return tuple(text for item in value for text in _leaves(item))
    return ()
