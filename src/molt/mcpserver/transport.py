"""The two transports, one framing, and the health route.

A locally spawned server speaks over the process streams; the hosted service
speaks over HTTP. Both carry the same JSON-RPC framing and both reach the tools
through `dispatch`, so the surface a client sees does not depend on how it reached
the server and neither transport can expose a tool the other does not.

**Neither loop runs unbounded.** The stdio loop reads until its input ends, until
a caller-supplied stop condition is met, or until a request count is reached, and
the HTTP loop serves a bounded number of requests with a poll timeout between
them. That is what lets a caller drive either transport and then stop it.

**A lost cluster costs a result, not a session.** A tool that could not reach the
cluster answers an empty result with a note, and the framing returns that as a
result rather than as an error, so the session stays open and the next call is
attempted.

**The HTTP transport authenticates nobody.** The configuration surface declares no
credential for it and this module invents none. Its only control is the network:
the hosted task has no ingress listener, and a client on the same machine uses the
process transport. See `HTTP_AUTHENTICATION_POSTURE`.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO, Final

from molt.mcpserver import (
    COMPONENT,
    HTTP_AUTHENTICATION_POSTURE,
    McpServer,
    UnknownToolError,
)
from molt.models.event import JsonObject, JsonValue
from molt.telemetry import Severity, log

__all__ = [
    "HEALTH_PATH",
    "HTTP_AUTHENTICATION_POSTURE",
    "INVALID_PARAMS_CODE",
    "METHOD_NOT_FOUND_CODE",
    "PARSE_ERROR_CODE",
    "PROTOCOL_VERSION",
    "RPC_PATH",
    "SERVER_NAME",
    "HttpResponse",
    "HttpTransport",
    "answer",
    "handle_http",
    "serve_stdio",
]

# The framing this server speaks and the name it announces itself under. The
# version is a plain revision number rather than a dated release string, because a
# dated literal in tracked source is what the metadata hygiene gate refuses.
PROTOCOL_VERSION: Final[str] = "1"
JSONRPC_VERSION: Final[str] = "2.0"
SERVER_NAME: Final[str] = "molt-mcp"

# The three methods a client may call, and the two HTTP paths.
INITIALIZE_METHOD: Final[str] = "initialize"
LIST_METHOD: Final[str] = "tools/list"
CALL_METHOD: Final[str] = "tools/call"
RPC_PATH: Final[str] = "/rpc"
HEALTH_PATH: Final[str] = "/health"

# The framing's own failure codes, which are the standard ones.
PARSE_ERROR_CODE: Final[int] = -32700
INVALID_REQUEST_CODE: Final[int] = -32600
METHOD_NOT_FOUND_CODE: Final[int] = -32601
INVALID_PARAMS_CODE: Final[int] = -32602

# How long the HTTP loop waits for a connection before checking whether it should
# still be serving, in seconds. A poll rather than a block is what makes the loop
# stoppable from outside it.
POLL_SECONDS: Final[float] = 0.2

_JSON_CONTENT_TYPE: Final[str] = "application/json"
_CONTENT_LENGTH_HEADER: Final[str] = "Content-Length"
_CONTENT_TYPE_HEADER: Final[str] = "Content-Type"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One HTTP answer: a status and the document rendered into the body."""

    status: int
    document: JsonObject

    def body(self) -> bytes:
        """The response body as bytes, which is what the socket is handed."""
        return json.dumps(self.document, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# The framing
# ---------------------------------------------------------------------------


def answer(server: McpServer, request: Mapping[str, JsonValue]) -> JsonObject:
    """The response one well-formed request produces.

    The three methods are the whole surface. `tools/call` reaches the registry
    through the server, so a name the registry lacks is a method-not-found failure
    rather than something that got as far as a handler.
    """
    identifier = request.get("id")
    method = request.get("method")
    if not isinstance(method, str):
        return _failure(identifier, INVALID_REQUEST_CODE, "a request must name a method")
    if method == INITIALIZE_METHOD:
        return _result(
            identifier,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME},
                "capabilities": {"tools": {"listChanged": False}},
            },
        )
    if method == LIST_METHOD:
        return _result(identifier, {"tools": list(server.tools())})
    if method == CALL_METHOD:
        return _call(server, identifier, request.get("params"))
    return _failure(identifier, METHOD_NOT_FOUND_CODE, f"no method named {method!r} is served")


def _call(server: McpServer, identifier: JsonValue, params: JsonValue) -> JsonObject:
    """Invoke one tool named by a call's parameters."""
    if not isinstance(params, dict):
        return _failure(identifier, INVALID_PARAMS_CODE, "a call must carry its parameters")
    name = params.get("name")
    if not isinstance(name, str):
        return _failure(identifier, INVALID_PARAMS_CODE, "a call must name a tool")
    arguments = params.get("arguments")
    supplied: Mapping[str, JsonValue] = arguments if isinstance(arguments, dict) else {}
    try:
        produced = server.invoke(name, supplied)
    except UnknownToolError as error:
        return _failure(identifier, METHOD_NOT_FOUND_CODE, str(error))
    document: JsonObject = {"rows": list(produced.rows), "count": produced.count}
    if produced.note is not None:
        document["note"] = produced.note
    return _result(identifier, document)


def _result(identifier: JsonValue, document: JsonObject) -> JsonObject:
    return {"jsonrpc": JSONRPC_VERSION, "id": identifier, "result": document}


def _failure(identifier: JsonValue, code: int, message: str) -> JsonObject:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": identifier,
        "error": {"code": code, "message": message},
    }


def _parsed(line: str) -> Mapping[str, JsonValue] | None:
    """One request read from a line, or None when the line is not one."""
    try:
        decoded: object = json.loads(line)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


# ---------------------------------------------------------------------------
# The process transport
# ---------------------------------------------------------------------------


def serve_stdio(
    server: McpServer,
    reader: IO[str],
    writer: IO[str],
    *,
    max_requests: int | None = None,
    stop: Callable[[], bool] | None = None,
) -> int:
    """Answer line-framed requests on the process streams, bounded and stoppable.

    Returns:
        How many requests were answered. The loop ends when the input ends, when
        the stop condition is met, or when the request bound is reached, so a
        caller always has a way to end it.
    """
    served = 0
    while max_requests is None or served < max_requests:
        if stop is not None and stop():
            break
        line = reader.readline()
        if not line:
            break
        if not line.strip():
            continue
        request = _parsed(line)
        response = (
            _failure(None, PARSE_ERROR_CODE, "a request line must carry one JSON object")
            if request is None
            else answer(server, request)
        )
        writer.write(json.dumps(response, separators=(",", ":")) + "\n")
        writer.flush()
        served += 1
    return served


# ---------------------------------------------------------------------------
# The HTTP transport
# ---------------------------------------------------------------------------


def handle_http(server: McpServer, method: str, path: str, body: bytes) -> HttpResponse:
    """The answer one HTTP request produces, framing and routing in one place.

    Written as a function over values rather than inside a handler class so the
    routing is testable without a socket, and so both the socket handler and a
    caller driving it directly take the same path.
    """
    if method == "GET" and path == HEALTH_PATH:
        return HttpResponse(status=200, document=server.health().as_document())
    if method != "POST" or path != RPC_PATH:
        return HttpResponse(
            status=404,
            document={"error": {"code": METHOD_NOT_FOUND_CODE, "message": "no such route"}},
        )
    request = _parsed(body.decode("utf-8", errors="replace"))
    if request is None:
        return HttpResponse(
            status=400,
            document=_failure(None, PARSE_ERROR_CODE, "a request body must carry one JSON object"),
        )
    return HttpResponse(status=200, document=answer(server, request))


class HttpTransport:
    """The hosted transport: one bounded server loop over the configured address.

    The loop is never entered on construction. A caller serves a bounded number of
    requests, or starts the bounded loop on a thread of its own and stops it, so
    nothing here blocks a process that only wanted the address.
    """

    __slots__ = ("_http", "_server", "_serving", "_thread")

    def __init__(
        self,
        server: McpServer,
        *,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self._server = server
        bind_host = server.settings.bind_host if host is None else host
        bind_port = server.settings.bind_port if port is None else port
        self._http = ThreadingHTTPServer((bind_host, bind_port), _make_handler(server))
        self._http.timeout = POLL_SECONDS
        self._serving = threading.Event()
        self._thread: threading.Thread | None = None
        log(
            Severity.WARNING,
            COMPONENT,
            "the http transport authenticates no caller, so its only control is the network",
            posture=HTTP_AUTHENTICATION_POSTURE,
            host=bind_host,
            port=bind_port,
        )

    @property
    def address(self) -> tuple[str, int]:
        """The host and port the transport actually bound."""
        bound = self._http.server_address
        return str(bound[0]), int(bound[1])

    def serve_bounded(self, requests: int) -> None:
        """Serve at most this many requests, returning when they are served or stopped."""
        self._serving.set()
        served = 0
        while served < requests and self._serving.is_set():
            self._http.handle_request()
            served += 1

    def start(self, requests: int) -> None:
        """Run the bounded loop on a thread of its own."""
        thread = threading.Thread(target=self.serve_bounded, args=(requests,), daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop the loop and release the socket."""
        self._serving.clear()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=POLL_SECONDS * 10)
            self._thread = None
        self._http.server_close()


def _make_handler(server: McpServer) -> type[BaseHTTPRequestHandler]:
    """The request handler class bound to one server instance."""

    class Handler(BaseHTTPRequestHandler):
        """One request, routed through the same function a direct caller uses."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._answer(handle_http(server, "GET", self.path, b""))

        def do_POST(self) -> None:
            declared = self.headers.get(_CONTENT_LENGTH_HEADER, "0")
            length = int(declared) if declared.isdigit() else 0
            self._answer(handle_http(server, "POST", self.path, self.rfile.read(length)))

        def log_message(self, *args: object) -> None:
            """Route the access record through telemetry rather than standard error.

            The base class writes an access line to standard error, which on the
            process transport is a stream a client is reading framed responses
            from. The line's own fields are counted rather than carried: a request
            line names a path, and a path is the one part of a call this server
            has no reason to repeat into a log a client agent may read.
            """
            log(
                Severity.DEBUG,
                COMPONENT,
                "served one tool transport request",
                access_line_fields=len(args),
            )

        def _answer(self, response: HttpResponse) -> None:
            body = response.body()
            self.send_response(response.status)
            self.send_header(_CONTENT_TYPE_HEADER, _JSON_CONTENT_TYPE)
            self.send_header(_CONTENT_LENGTH_HEADER, str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler
