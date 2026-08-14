"""The serve verb: the Web_Console locally, against the deployed ASGI object.

The point of this verb is that it serves the same application object the deployed
function serves rather than a development variant, so what an operator sees locally
is what the deployment answers with. The object is built by
`molt.console.app.build_app` and handed to an ASGI server here; the deployed path
hands the same object to the Lambda adapter instead, and no handler knows which.

The bind comes from `--bind` when given and from the configured console bind
otherwise, and `--demo` re-resolves the same surface with the demonstration flag set
rather than mutating the console after the fact, so the flag reaches the middleware
that reads it at construction.
"""

from __future__ import annotations

from molt.cli.context import VerbContext
from molt.cli.exits import ComponentUnavailableError, ExitCode, UsageError

__all__ = ["run"]

# The configuration key the demonstration flag is set through, so the flag travels
# the same path a deployment would set it by.
DEMO_KEY = "MOLT_DEMO_MODE"


def run(context: VerbContext) -> ExitCode:
    """Serve the console application on the configured address."""
    try:
        import uvicorn

        from molt.console.app import build_app
        from molt.console.deps import Console, ConsoleSettings
    except ModuleNotFoundError as error:
        raise ComponentUnavailableError(context.name, "the ASGI server package") from error

    configuration = (
        context.configuration_for({DEMO_KEY: "true"})
        if context.flag("demo")
        else context.configuration
    )
    settings = ConsoleSettings.from_configuration(configuration)
    host, port = _bind_of(context, settings.bind)
    console = Console.from_configuration(configuration)
    context.emitter.narrate(f"serving the console on {host}:{port}")
    uvicorn.run(build_app(console), host=host, port=port, log_level="info")
    return ExitCode.SUCCESS


def _bind_of(context: VerbContext, configured: str) -> tuple[str, int]:
    """The address to listen on: the flag when given, the configured bind otherwise."""
    text = context.text("bind") or configured
    host, separator, port_text = text.rpartition(":")
    if not separator:
        raise UsageError("--bind takes a host and a port separated by a colon")
    try:
        return (host or "127.0.0.1", int(port_text, 10))
    except ValueError as error:
        raise UsageError("the port given to --bind is not a whole number") from error
