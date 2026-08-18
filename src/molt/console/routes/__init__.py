"""The view modules, each claiming routes the table already declares.

A view module claims one or more declared routes by name with
`molt.console.routing.register` and implements the handler. It declares no path, no
method, and no authentication requirement of its own: those are the route table's,
so the posture of the whole console stays inspectable as one value.

This module imports every view module that exists, so importing the package is what
attaches the handlers. It is deliberately the only import list: a view module that is
written but not named here is not served, which is a visible omission rather than a
silent one.
"""

from __future__ import annotations

from molt.console.routes import (
    approvals,
    certificates,
    erasure,
    fleet,
    lineage,
    procedures,
    residue,
    retention,
    runs,
    sensitivity,
    sessions,
    tiers,
)

__all__: list[str] = [
    "approvals",
    "certificates",
    "erasure",
    "fleet",
    "lineage",
    "procedures",
    "residue",
    "retention",
    "runs",
    "sensitivity",
    "sessions",
    "tiers",
]
