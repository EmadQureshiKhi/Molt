"""The authenticated ingest surface that accepts events from remote machines.

The package holds two modules and re-exports what a deployment and a caller need
from them. `routes` owns everything decided before a connection is leased: the
route table, the request bound, the batch reading, and the response envelope.
`handler` owns the request path itself: the bearer comparison, the signature seam,
the one transaction an absent Session and its Events share, and the health answer.

The function entry point is `handler`, and the deployment reads its concurrency
ceiling and its body bound from the same functions the running handler reads them
from, so the template and the process cannot quote different values.
"""

from molt.collector.handler import (
    Collector,
    Invocation,
    handler,
    invocation_of,
    rendered,
    reset_collector,
)
from molt.collector.routes import (
    AUTHENTICATED_KINDS,
    EVENTS_PATH,
    HEALTH_PATH,
    RECALL_PATH,
    SESSIONS_PREFIX,
    SIGNED_KINDS,
    HaltReport,
    Headers,
    RecallAnswer,
    RecallQuery,
    Request,
    Response,
    RouteKind,
    max_body_bytes,
    reserved_concurrency,
)

__all__ = [
    "AUTHENTICATED_KINDS",
    "EVENTS_PATH",
    "HEALTH_PATH",
    "RECALL_PATH",
    "SESSIONS_PREFIX",
    "SIGNED_KINDS",
    "Collector",
    "HaltReport",
    "Headers",
    "Invocation",
    "RecallAnswer",
    "RecallQuery",
    "Request",
    "Response",
    "RouteKind",
    "handler",
    "invocation_of",
    "max_body_bytes",
    "rendered",
    "reserved_concurrency",
    "reset_collector",
]
