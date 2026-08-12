"""The Web_Console: one application object, one route table, one way in.

The console is the only component whose memory content is reachable from the
internet. Its function endpoint declares no request signing, because the
distribution in front of it cannot sign as a cloud principal, so authentication is
the application's own and nothing else stands between an anonymous caller and the
routes. That single fact shapes the whole package:

- `routing` holds the route table as data. Every route is declared there with its
  method, its path, whether it is public, whether it mutates, and how demonstration
  mode treats it, and the invariants *only allowlisted names may be public* and *no
  public route mutates* are checked at import. The application is built from that
  table and from nowhere else, so a property of the table is a property of the
  deployment.
- `auth` holds credential verification against a stored hash, the signed session
  cookie with its absolute expiry, and the per-session CSRF token.
- `deps` holds what resolves once per process: the settings, the store, the two
  credentials, the clock.
- `app` builds the application object and serves the routes this task owns: the
  unauthenticated health and specification routes, and the login pair and logout.
- `lambda_adapter` and `handler` are how the deployed function serves that same
  object, and `molt serve` runs the same object locally.
- `routes/` is where the view modules of the remaining console tasks live. A view
  claims a declared route by name with `routing.register`; a declared route with no
  claimed handler answers `501` *after* the authentication check, so an unfinished
  view is never an unauthenticated one.
"""

from __future__ import annotations

from molt.console.app import build_app, build_routes, console_of, session_of
from molt.console.auth import Session, csrf_accepted, issue, verify_cookie, verify_credential
from molt.console.deps import COMPONENT, Console, ConsoleSettings
from molt.console.handler import handler
from molt.console.lambda_adapter import invoke
from molt.console.routing import (
    MUTATION_ROUTE_NAMES,
    PUBLIC_ROUTE_NAMES,
    ROUTE_TABLE,
    Access,
    DemoDisposition,
    RouteSpec,
    authenticated_routes,
    mutation_routes,
    public_routes,
    register,
    route_named,
)

__all__ = [
    "COMPONENT",
    "MUTATION_ROUTE_NAMES",
    "PUBLIC_ROUTE_NAMES",
    "ROUTE_TABLE",
    "Access",
    "Console",
    "ConsoleSettings",
    "DemoDisposition",
    "RouteSpec",
    "Session",
    "authenticated_routes",
    "build_app",
    "build_routes",
    "console_of",
    "csrf_accepted",
    "handler",
    "invoke",
    "issue",
    "mutation_routes",
    "public_routes",
    "register",
    "route_named",
    "session_of",
    "verify_cookie",
    "verify_credential",
]
