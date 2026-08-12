"""The ASGI-to-Lambda adapter: one invocation becomes one ASGI request.

The console is one application object. A local run serves it with an ASGI server
and the deployment invokes it through a function endpoint, and the point of this
module is that neither the application nor any handler can tell the difference. So
this is a translation and nothing else: it holds no routing, no authentication, and
no knowledge of any route.

Three details are what make the translation faithful.

**The event shape is the function URL's payload.** The method, the path, the raw
query string, the headers, and the cookie list are read from `requestContext.http`
and the top-level fields, and a body marked as base64-encoded is decoded before it
becomes the request body, so a form submission and a binary upload both arrive as
the bytes the caller sent.

**`Set-Cookie` travels in the `cookies` field.** A single-valued header map cannot
carry two cookies, and the session cookie must survive with its attributes intact,
so cookie headers are collected separately and every other repeated header is joined
with a comma as the interchange rules allow.

**The response body is base64-encoded unless it is text.** The adapter looks at the
content type rather than guessing from the bytes, so a rendered page arrives as text
and anything else arrives intact.

The event loop is created per invocation. A function container serves one request at
a time, so a per-invocation loop costs one construction and leaves no loop bound to a
container that may be frozen between invocations.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from typing import Final, cast
from urllib.parse import urlencode

__all__ = [
    "ASGI_VERSION",
    "TEXT_MEDIA_PREFIXES",
    "AsgiApplication",
    "LambdaEvent",
    "LambdaResponse",
    "invoke",
    "scope_of",
]

# The ASGI version the scope declares, and the HTTP version a function endpoint
# presents a request as.
ASGI_VERSION: Final[str] = "3.0"
_HTTP_VERSION: Final[str] = "1.1"

# The content types returned as text rather than as base64. Everything else is
# encoded, so no byte sequence is corrupted by an assumption about its encoding.
TEXT_MEDIA_PREFIXES: Final[tuple[str, ...]] = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "image/svg+xml",
)

_SET_COOKIE: Final[str] = "set-cookie"
_COOKIE: Final[str] = "cookie"
_CONTENT_TYPE: Final[str] = "content-type"
_DEFAULT_METHOD: Final[str] = "GET"
_DEFAULT_PATH: Final[str] = "/"
_DEFAULT_STATUS: Final[int] = 500

# What the two payload shapes are read from. The function URL sends the second
# version; the first is read as a fallback so a test may present either.
_REQUEST_CONTEXT: Final[str] = "requestContext"

LambdaEvent = Mapping[str, object]
LambdaResponse = dict[str, object]
AsgiApplication = Callable[
    [
        MutableMapping[str, object],
        Callable[[], Awaitable[MutableMapping[str, object]]],
        Callable[[MutableMapping[str, object]], Awaitable[None]],
    ],
    Awaitable[None],
]


def _text(value: object, fallback: str) -> str:
    """Read a payload field as text, falling back rather than raising."""
    return value if isinstance(value, str) else fallback


def _http_context(event: LambdaEvent) -> Mapping[str, object]:
    """The `requestContext.http` object of the payload, or an empty mapping."""
    context = event.get(_REQUEST_CONTEXT)
    if not isinstance(context, Mapping):
        return {}
    http = context.get("http")
    return http if isinstance(http, Mapping) else {}


def _query_string(event: LambdaEvent) -> str:
    """The raw query string, rebuilt from the parsed parameters when absent."""
    raw = event.get("rawQueryString")
    if isinstance(raw, str):
        return raw
    parameters = event.get("queryStringParameters")
    if isinstance(parameters, Mapping):
        pairs = [(str(key), str(value)) for key, value in parameters.items()]
        return urlencode(pairs)
    return ""


def _header_pairs(event: LambdaEvent) -> list[tuple[bytes, bytes]]:
    """The request headers as ASGI pairs, with the cookie list folded in.

    The function endpoint delivers cookies in their own list rather than as a
    header, so they are rejoined here into the one `Cookie` header the application
    reads.
    """
    pairs: list[tuple[bytes, bytes]] = []
    headers = event.get("headers")
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if not isinstance(key, str) or value is None:
                continue
            if key.lower() == _COOKIE:
                continue
            pairs.append((key.lower().encode("latin-1"), str(value).encode("latin-1")))
    cookies = event.get("cookies")
    if isinstance(cookies, Sequence) and not isinstance(cookies, str | bytes):
        rendered = "; ".join(str(cookie) for cookie in cookies)
        if rendered:
            pairs.append((_COOKIE.encode("latin-1"), rendered.encode("latin-1")))
    elif isinstance(headers, Mapping):
        carried = headers.get("cookie") or headers.get("Cookie")
        if isinstance(carried, str) and carried:
            pairs.append((_COOKIE.encode("latin-1"), carried.encode("latin-1")))
    return pairs


def _body_of(event: LambdaEvent) -> bytes:
    """The request body as bytes, decoding a base64-marked payload first."""
    body = event.get("body")
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    text = body if isinstance(body, str) else str(body)
    if event.get("isBase64Encoded") is True:
        try:
            return base64.b64decode(text, validate=False)
        except (ValueError, TypeError):
            return b""
    return text.encode("utf-8")


def scope_of(event: LambdaEvent) -> MutableMapping[str, object]:
    """The ASGI scope one invocation becomes.

    Exposed on its own so a test can state the translation without running an
    application, which is how the adapter's own behaviour stays checkable.
    """
    http = _http_context(event)
    method = _text(http.get("method"), _text(event.get("httpMethod"), _DEFAULT_METHOD))
    path = _text(http.get("path"), _text(event.get("rawPath"), _DEFAULT_PATH))
    return {
        "type": "http",
        "asgi": {"version": ASGI_VERSION, "spec_version": "2.3"},
        "http_version": _HTTP_VERSION,
        "method": method.upper(),
        "scheme": "https",
        "path": path or _DEFAULT_PATH,
        "raw_path": (path or _DEFAULT_PATH).encode("utf-8"),
        "root_path": "",
        "query_string": _query_string(event).encode("latin-1"),
        "headers": _header_pairs(event),
        "client": None,
        "server": None,
    }


def _textual(content_type: str) -> bool:
    """Whether a response body of this content type is returned as text."""
    lowered = content_type.lower()
    return any(lowered.startswith(prefix) for prefix in TEXT_MEDIA_PREFIXES)


async def _run(app: AsgiApplication, event: LambdaEvent) -> LambdaResponse:
    """Drive one request through the application and collect its response."""
    body = _body_of(event)
    sent = False
    status = _DEFAULT_STATUS
    headers: list[tuple[str, str]] = []
    chunks: list[bytes] = []

    async def receive() -> MutableMapping[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: MutableMapping[str, object]) -> None:
        nonlocal status
        kind = message.get("type")
        if kind == "http.response.start":
            status = cast(int, message.get("status", _DEFAULT_STATUS))
            raw = message.get("headers") or []
            for key, value in cast(Sequence[tuple[bytes, bytes]], raw):
                headers.append((key.decode("latin-1").lower(), value.decode("latin-1")))
        elif kind == "http.response.body":
            chunk = message.get("body") or b""
            if isinstance(chunk, bytes | bytearray):
                chunks.append(bytes(chunk))

    await app(scope_of(event), receive, send)
    return _rendered(status, headers, b"".join(chunks))


def _rendered(status: int, headers: Sequence[tuple[str, str]], body: bytes) -> LambdaResponse:
    """The invocation's answer, with cookies in their own field."""
    single: dict[str, str] = {}
    cookies: list[str] = []
    for key, value in headers:
        if key == _SET_COOKIE:
            cookies.append(value)
        elif key in single:
            single[key] = f"{single[key]}, {value}"
        else:
            single[key] = value
    textual = _textual(single.get(_CONTENT_TYPE, ""))
    answer: LambdaResponse = {
        "statusCode": status,
        "headers": single,
        "cookies": cookies,
        "isBase64Encoded": not textual,
        "body": body.decode("utf-8", errors="replace")
        if textual
        else base64.b64encode(body).decode("ascii"),
    }
    return answer


def invoke(app: AsgiApplication, event: LambdaEvent) -> LambdaResponse:
    """Serve one function invocation with the application object, synchronously."""
    return asyncio.run(_run(app, event))
