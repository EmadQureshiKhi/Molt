"""One handshake over the process transport and one over the HTTP transport.

Both loops are entered with a bound: the stdio loop is given its requests on a
stream that ends, and the HTTP loop serves a counted number of requests on a thread
the test joins. Neither test can hang a run.
"""

from __future__ import annotations

import io
import json
import time
import urllib.request
from typing import Final
from uuid import uuid4

import pytest
from tests.mcp.harness import Artifact, Corpus, RecordingSink, build_server

from molt.mcpserver.transport import (
    HEALTH_PATH,
    METHOD_NOT_FOUND_CODE,
    POLL_SECONDS,
    PROTOCOL_VERSION,
    RPC_PATH,
    HttpTransport,
    handle_http,
    serve_stdio,
)
from molt.models.artifact import ArtifactKind

pytestmark = pytest.mark.mcp

# How long the bounded HTTP loop is given, in seconds, before the test gives up on
# it. A generous bound that still fails rather than hangs.
REQUEST_TIMEOUT: Final[float] = 5.0

# How many poll intervals the idle case waits before it asks anything. Enough that the
# loop has certainly polled and found nothing more than once.
IDLE_POLLS: Final[int] = 3


@pytest.fixture(name="corpus")
def corpus_fixture() -> Corpus:
    """One tenant, one Artifact it holds, and one Client it does not."""
    permitted = uuid4()
    other = uuid4()
    return Corpus(
        clients=(permitted, other),
        permitted=(permitted,),
        slugs=("tenant-0",),
        artifacts=(
            Artifact(
                artifact_id=uuid4(),
                kind=ArtifactKind.DERIVED_ARTIFACT,
                bound_clients=frozenset({permitted}),
            ),
        ),
    )


def test_the_stdio_transport_completes_one_handshake_and_lists_the_tools(
    corpus: Corpus,
) -> None:
    server, _ = build_server(corpus, sink=RecordingSink())
    requests = "\n".join(
        (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        )
    )
    writer = io.StringIO()

    served = serve_stdio(server, io.StringIO(requests + "\n"), writer, max_requests=4)

    assert served == 2
    answers = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert answers[0]["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert [tool["name"] for tool in answers[1]["result"]["tools"]] == [
        tool["name"] for tool in server.tools()
    ]


def test_the_stdio_loop_stops_when_the_caller_asks_it_to(corpus: Corpus) -> None:
    server, _ = build_server(corpus, sink=RecordingSink())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"

    served = serve_stdio(server, io.StringIO(line * 8), io.StringIO(), stop=lambda: True)

    assert served == 0


def test_an_undispatchable_name_is_refused_by_the_framing(corpus: Corpus) -> None:
    server, _ = build_server(corpus, sink=RecordingSink())
    request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "molt.erase", "arguments": {}},
    }
    writer = io.StringIO()

    serve_stdio(server, io.StringIO(json.dumps(request) + "\n"), writer, max_requests=1)

    answer = json.loads(writer.getvalue())
    assert answer["error"]["code"] == METHOD_NOT_FOUND_CODE


def test_the_health_route_reports_status_and_no_memory_content(corpus: Corpus) -> None:
    server, _ = build_server(corpus, sink=RecordingSink())

    response = handle_http(server, "GET", HEALTH_PATH, b"")

    assert response.status == 200
    document = response.document
    assert document["status"] == "ok"
    assert document["database_reachable"] is True
    assert document["permitted_client_count"] == 1
    assert isinstance(document["tools"], list)
    rendered = json.dumps(document)
    for artifact in corpus.artifacts:
        assert str(artifact.artifact_id) not in rendered
    for client in corpus.clients:
        assert str(client) not in rendered


def test_the_http_transport_completes_one_handshake_over_a_socket(corpus: Corpus) -> None:
    server, _ = build_server(corpus, sink=RecordingSink(), transport="http")
    transport = HttpTransport(server, host="127.0.0.1", port=0)
    host, port = transport.address
    transport.start(requests=1)
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize"}).encode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{port}{RPC_PATH}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as answer:  # noqa: S310
            document = json.loads(answer.read())
    finally:
        transport.stop()

    assert document["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert document["id"] == 7
    assert transport.answered == 1, "the answer is counted, and only the answer"


def test_a_request_arriving_after_several_idle_polls_is_still_answered(
    corpus: Corpus,
) -> None:
    """The bound counts answers, so a quiet server has not used any of it up.

    The socket is polled with a timeout so a stop is noticed without a signal. An
    expired poll used to count as a served request, which had two consequences: a
    client that took longer than one poll interval to connect met a server that had
    already finished — the failure this case reproduces, seen first as a handshake that
    timed out under parallel load — and the hosted verb's bound of ten thousand ran out
    after ten thousand polls, a little over half an hour of quiet, while reporting the
    full count as though it had answered that many calls.

    The wait here is a multiple of the poll interval rather than a duration of its own,
    so it stays meaningful if the interval changes, and the case is about what the loop
    counts rather than about how fast the machine is.
    """
    server, _ = build_server(corpus, sink=RecordingSink(), transport="http")
    transport = HttpTransport(server, host="127.0.0.1", port=0)
    host, port = transport.address
    transport.start(requests=1)
    try:
        time.sleep(POLL_SECONDS * IDLE_POLLS)
        assert transport.answered == 0, "nothing was asked of it yet"
        with urllib.request.urlopen(
            urllib.request.Request(
                f"http://{host}:{port}{HEALTH_PATH}",
                method="GET",
            ),
            timeout=REQUEST_TIMEOUT,
        ) as answer:
            document = json.loads(answer.read())
    finally:
        transport.stop()

    assert document["status"] == "ok", document
    assert transport.answered == 1
