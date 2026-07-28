"""One thousand real hook invocations stay inside the 250 millisecond p95 budget.

Why the bound exists. A hook runs while the agent waits for it. Requirement 1.8
states the budget as wall-clock time per invocation at the 95th percentile across a
thousand-invocation benchmark, and that is what this module is: the real entry point,
a thousand times, against a Collector listening on loopback.

What is inside the measurement. Everything the entry point does: reading the payload
from standard input, resolving the configuration surface, importing the one adapter
the token names, mapping the payload to Events, redacting them, claiming the spool,
computing the ingress signature over the exact batch bytes, writing the request to a
real socket, reading the response envelope back, and confirming the spool claim. The
two credentials are both configured, so the signed ingest path is the path taken;
with either unset the batch would be spooled and the benchmark would time a file
append rather than a round trip, which is the wrong measurement made to look fast.
That degradation is ruled out by assertion rather than by hope: every request the
stub received is re-signed here from the recorded body and the presented timestamp,
and the header must match.

Why a socket rather than a transport double. The signature is computed immediately
before transmission and the bytes signed are the bytes sent, so a recorded transport
would remove the one part of the path the requirement's budget is spent on. The stub
is a raw listener on an ephemeral loopback port, served on a thread, which is the
same shape the proxy suite drives its upstream with: what is under test is a byte
sequence and a duration, and a request framework would answer a different question.

In-process rather than a process per invocation, and what that omits. Each timed
sample is one `main` call in this interpreter, so interpreter start-up and module
import are paid once rather than a thousand times. That is a real omission: the
design's own budget list is largely about import cost, and in the field every
invocation pays it. A thousand real spawns would take minutes and would measure the
interpreter rather than the hook, so the omission is quantified instead of hidden.
The second case below spawns a small number of real processes running the same entry
point against the same stub and reports the difference, which is the start-up cost
the in-process figure leaves out.

The first invocation is not discarded silently. It is run and timed before the
sample, reported on its own line, and left out of the sample, because it pays the
adapter import, the redaction pattern table, and the first spool and index file
creation, and charging those to a percentile computed over a thousand samples would
describe neither the first invocation nor the other nine hundred and ninety-nine.

The whole spread is reported rather than the one number asserted: the median, the
95th and 99th percentiles, and the worst sample. A percentile that passes while the
maximum has quietly grown is worth seeing.

**Validates: Requirements 1.8**
"""

from __future__ import annotations

import io
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pytest

from molt.capture.adapters import claude_code
from molt.capture.hook import EVENTS_PATH, EXIT_OK, main
from molt.capture.signing import (
    AUTHORIZATION_HEADER,
    BEARER_SCHEME,
    COLLECTOR_BEARER_ENV,
    INGRESS_KEY_ENV,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign_ingress,
)

if TYPE_CHECKING:  # pragma: no cover - imported for the casts alone
    from typing import TextIO

# A bound measured against this machine alone. The far end is the stub listener
# below, bound on loopback and answering from a thread of this process, and the
# entry point stores nothing beyond the sample tree, so no cluster takes part. The
# performance marker is therefore the only one, with no instance marker beside it,
# and the thousand invocations are made on a bare checkout rather than skipped.
pytestmark = pytest.mark.perf

# The bound and the percentile Requirement 1.8 states, and the sample size it
# states them across.
LATENCY_BOUND_MS: Final[float] = 250.0
ASSERTED_FRACTION: Final[float] = 0.95
INVOCATIONS: Final[int] = 1000

# How many real processes are spawned to report the start-up cost the in-process
# figure omits, and the discarded spawn ahead of them that pays for compiling the
# package into the sample tree's bytecode directory.
SPAWNS: Final[int] = 20
SPAWN_WARMUPS: Final[int] = 1

# The vendor and the hook event driven. This event maps to one tool result Event,
# names no recall query, and opens no Session, so one invocation places exactly one
# signed ingest request: the shape the budget's own note describes as one call.
TOOL: Final[str] = "claude_code"
EVENT_NAME: Final[str] = "PostToolUse"

# The same two tokens as the spawned case passes on its command line, restated so
# the literals written out at that call site are held to these.
SPAWN_ARGUMENTS: Final[tuple[str, str]] = ("claude_code", "PostToolUse")

LOOPBACK: Final[str] = "127.0.0.1"
MACHINE: Final[str] = "machine-under-test"
SESSION_KEY: Final[str] = "a-conversation-under-test"
WORKSPACE: Final[str] = "/work/acme"

# The two credentials, shaped like the values an operator's shell profile injects
# and obviously synthetic, so this module states nothing that could be one.
SHARED_VALUE: Final[str] = "an-ingress-shared-value"
BEARER_VALUE: Final[str] = "a-collector-bearer-value"

# The settings one invocation resolves. The cap and the retry count are the values
# Requirements 6.4 and 6.3 state; the soft deadline is the design's own.
CAP_SECONDS: Final[str] = "5"
RETRIES: Final[str] = "3"
SOFT_DEADLINE_MS: Final[str] = "1200"

# Every name of the configuration surface begins with this, and all of them are
# removed before the sample runs so nothing of the developer's own leaks in.
SETTING_PREFIX: Final[str] = "MOLT_"

# How long the stub waits on a socket, and how long a spawned invocation is given.
SOCKET_TIMEOUT_SECONDS: Final[float] = 5.0
POLL_SECONDS: Final[float] = 0.25
SPAWN_TIMEOUT_SECONDS: Final[float] = 60.0

# Where the package is imported from by a spawned process, which does not inherit
# the path this suite is collected with.
SOURCE_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "src"

_HEAD_END: Final[bytes] = b"\r\n\r\n"
_ENVELOPE: Final[bytes] = b'{"accepted":1,"rejected":0,"halted":false,"pending_approvals":[]}'
_RESPONSE: Final[bytes] = (
    b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
    + str(len(_ENVELOPE)).encode("ascii")
    + b"\r\nConnection: close"
    + _HEAD_END
    + _ENVELOPE
)


# ---------------------------------------------------------------------------
# The payload one invocation carries
# ---------------------------------------------------------------------------


def hook_payload() -> bytes:
    """One tool-result payload of this vendor's own shape.

    Modest rather than minimal: a command, a captured output, and the correlation
    identifier the result links by, so redaction and Event construction walk a
    realistic structure rather than an empty one.
    """
    document = {
        "hook_event_name": EVENT_NAME,
        "session_id": SESSION_KEY,
        "cwd": WORKSPACE,
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/unit -q", "description": "run the unit suite"},
        "tool_response": {
            "stdout": "the suite passed and nothing was written",
            "stderr": "",
            "exit_code": 0,
        },
        "tool_use_id": "a-tool-use-identifier",
        "duration_ms": 412,
    }
    return json.dumps(document).encode("utf-8")


# ---------------------------------------------------------------------------
# The stub Collector
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Received:
    """One request the stub answered, reduced to what is asserted about it."""

    path: str
    body: bytes
    timestamp: str | None
    signature: str | None
    presented: str | None


@dataclass(slots=True)
class StubCollector:
    """A loopback listener answering one canned ingest envelope per connection.

    A raw listener rather than a request framework: what the measurement needs from
    the far end is an accept, a read, and a write, and what the assertions need is
    the exact bytes and headers that arrived.
    """

    listener: socket.socket
    received: list[Received] = field(default_factory=list)
    stopping: threading.Event = field(default_factory=threading.Event)

    @classmethod
    def opened(cls) -> StubCollector:
        """Bind a listener on an ephemeral loopback port."""
        listener = socket.create_server((LOOPBACK, 0))
        listener.settimeout(POLL_SECONDS)
        return cls(listener=listener)

    @property
    def address(self) -> str:
        """The address the hook is pointed at, read back from the bound socket."""
        host, port = self.listener.getsockname()[:2]
        return f"http://{host}:{port}"

    def serve(self) -> None:
        """Answer connections until asked to stop."""
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(SOCKET_TIMEOUT_SECONDS)
                try:
                    head, body = _read_message(connection)
                    self.received.append(_received(head, body))
                    connection.sendall(_RESPONSE)
                except OSError:
                    continue

    def close(self) -> None:
        """Stop answering and release the listener."""
        self.stopping.set()
        self.listener.close()


def _read_message(connection: socket.socket) -> tuple[bytes, bytes]:
    """Read one request off a socket: its head, then its declared body."""
    raw = bytearray()
    while _HEAD_END not in raw:
        chunk = connection.recv(4096)
        if not chunk:
            return bytes(raw), b""
        raw += chunk
    head, _, body = bytes(raw).partition(_HEAD_END)
    declared = _declared_length(head)
    while len(body) < declared:
        chunk = connection.recv(4096)
        if not chunk:
            break
        body += chunk
    return head, body


def _declared_length(head: bytes) -> int:
    """The body length the request head declares, or zero when it declares none."""
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value.strip())
    return 0


def _received(head: bytes, body: bytes) -> Received:
    """Reduce one request to its route, its body, and the three headers asserted."""
    lines = head.split(b"\r\n")
    target = lines[0].split(b" ") if lines else []
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.strip().decode("latin-1").lower()] = value.strip().decode("latin-1")
    return Received(
        path=target[1].decode("latin-1") if len(target) > 1 else "",
        body=body,
        timestamp=headers.get(TIMESTAMP_HEADER.lower()),
        signature=headers.get(SIGNATURE_HEADER.lower()),
        presented=headers.get(AUTHORIZATION_HEADER.lower()),
    )


# ---------------------------------------------------------------------------
# The environment and the streams one invocation runs against
# ---------------------------------------------------------------------------


def settings_for(root: Path, address: str) -> dict[str, str]:
    """The configuration surface an invocation resolves, credentials included."""
    return {
        "MOLT_COLLECTOR_URL": address,
        "MOLT_SPOOL_DIR": str(root / "spool"),
        "MOLT_MACHINE_ID": MACHINE,
        "MOLT_HTTP_TIMEOUT_SECONDS": CAP_SECONDS,
        "MOLT_HTTP_RETRIES": RETRIES,
        "MOLT_HOOK_SOFT_DEADLINE_MS": SOFT_DEADLINE_MS,
        INGRESS_KEY_ENV: SHARED_VALUE,
        COLLECTOR_BEARER_ENV: BEARER_VALUE,
    }


@contextmanager
def hook_environment(root: Path, address: str) -> Iterator[None]:
    """Run the benchmark against an environment and a working directory of its own.

    Every name of the surface is removed first, so a developer's own settings cannot
    change what is measured. The home directory and the working directory both point
    inside the sample tree, because the spool and the invocation index default to
    locations stated relative to home and a configuration file is looked for in the
    working directory, and this tree holds none.
    """
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    previous_environment = dict(os.environ)
    previous_directory = Path.cwd()
    for name in [name for name in os.environ if name.startswith(SETTING_PREFIX)]:
        del os.environ[name]
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.environ.update(settings_for(root, address))
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous_directory)
        os.environ.clear()
        os.environ.update(previous_environment)


@dataclass(slots=True)
class ByteStandardInput:
    """A standard input carrying bytes, which is how the entry point reads a payload."""

    buffer: io.BytesIO


@dataclass(slots=True)
class DiscardingStream:
    """A stream accepting and counting writes without performing any output.

    The invocation writes one diagnostic line, and a thousand lines to a terminal
    would put the terminal inside the measurement.
    """

    written: int = 0

    def write(self, text: str) -> int:
        """Count a text write and discard it."""
        self.written += len(text)
        return len(text)

    def flush(self) -> None:
        """Answer a flush, which a counter needs nothing for."""
        return


def timed_invocation(payload: bytes) -> float:
    """Run the entry point once and return its wall-clock cost in milliseconds.

    Only the call itself is inside the timer: placing the payload on standard input
    and redirecting the two streams are the harness's cost rather than the hook's.
    """
    sink = DiscardingStream()
    previous = sys.stdin
    sys.stdin = cast("TextIO", ByteStandardInput(io.BytesIO(payload)))
    try:
        with redirect_stdout(cast("TextIO", sink)), redirect_stderr(cast("TextIO", sink)):
            started = time.perf_counter()
            status = main([TOOL, EVENT_NAME])
            elapsed = (time.perf_counter() - started) * 1000.0
    finally:
        sys.stdin = previous
    assert status == EXIT_OK, f"the entry point exited {status}"
    return elapsed


def timed_spawn(root: Path, payload: bytes) -> float:
    """Run the entry point in a real process and return its wall-clock cost.

    The interpreter is this suite's own, the package is imported from the checkout,
    and the bytecode cache is directed into the sample tree so the measurement does
    not write into the source tree.

    The two shim arguments are written out as literals rather than assembled from the
    constants above, because a spawn whose argument vector is composed from names is a
    spawn a reader cannot audit at the call site. The case below asserts the literals
    and the constants say the same thing, so the two cannot drift apart unnoticed.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    environment["PYTHONPYCACHEPREFIX"] = str(root / "pycache")
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "molt.capture.hook", "claude_code", "PostToolUse"],
        input=payload,
        capture_output=True,
        check=False,
        cwd=root,
        env=environment,
        timeout=SPAWN_TIMEOUT_SECONDS,
    )
    elapsed = (time.perf_counter() - started) * 1000.0
    assert completed.returncode == EXIT_OK, completed.stderr.decode("utf-8", errors="replace")
    return elapsed


# ---------------------------------------------------------------------------
# Statistics and reporting
# ---------------------------------------------------------------------------


def percentile(samples: tuple[float, ...], fraction: float) -> float:
    """The sample at a fraction of the sorted order, by nearest rank.

    Nearest rank rather than an interpolation, because the requirement names a
    percentile across a sample of invocations and the answer should be one of the
    invocations that happened.
    """
    if not samples:
        raise ValueError("a percentile needs at least one sample")
    ordered = sorted(samples)
    rank = math.ceil(fraction * len(ordered)) - 1
    return ordered[min(len(ordered) - 1, max(0, rank))]


@dataclass(frozen=True, slots=True)
class Spread:
    """The samples of one measurement, in milliseconds."""

    label: str
    samples: tuple[float, ...]

    def summary(self) -> str:
        """One line naming the measurement and the whole spread."""
        return (
            f"{self.label}: {len(self.samples)} invocations, "
            f"p50 {percentile(self.samples, 0.50):.2f} ms, "
            f"p95 {percentile(self.samples, ASSERTED_FRACTION):.2f} ms, "
            f"p99 {percentile(self.samples, 0.99):.2f} ms, "
            f"max {max(self.samples):.2f} ms, "
            f"bound {LATENCY_BOUND_MS:.0f} ms"
        )


# ---------------------------------------------------------------------------
# The harness both cases share
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Harness:
    """The sample tree and the stub the benchmark runs against."""

    root: Path
    collector: StubCollector
    payload: bytes


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Harness]:
    """A stub Collector, a tree of its own, and the environment pointing at both.

    Module scope so the thousand-invocation case and the spawned case share one
    listener and one tree, which is also what lets the spawned case be asserted
    against requests that reached the same stub.

    The delivered adapter's invocation index is released afterwards. A hook is a
    fresh process in the field, but here the adapter singleton binds its index
    directory on first use and keeps it for the life of the interpreter, and this
    tree is removed when the session ends.
    """
    root = tmp_path_factory.mktemp("molt_hook_latency")
    collector = StubCollector.opened()
    answering = threading.Thread(target=collector.serve, name="stub-collector")
    answering.start()
    try:
        with hook_environment(root, collector.address):
            yield Harness(root=root, collector=collector, payload=hook_payload())
    finally:
        collector.close()
        answering.join(timeout=SOCKET_TIMEOUT_SECONDS)
        claude_code.ADAPTER.index = None


def assert_signed(requests: list[Received], *, at_least: int) -> None:
    """Every recorded request went to the ingest route and carried a real signature.

    Re-signing here from the recorded body and the presented timestamp is what rules
    out the failure this benchmark would otherwise be vulnerable to: an invocation
    that spooled instead of transmitting would be fast and wrong, and one that
    transmitted with the headers absent would not be exercising the signing step the
    budget has to accommodate.
    """
    assert len(requests) >= at_least, f"the stub received {len(requests)} requests"
    for request in requests:
        assert request.path == EVENTS_PATH
        assert request.presented == f"{BEARER_SCHEME} {BEARER_VALUE}"
        assert request.timestamp is not None
        assert request.signature is not None
        assert request.signature == sign_ingress(request.body, SHARED_VALUE, request.timestamp)
        assert request.body


# ---------------------------------------------------------------------------
# The benchmark
# ---------------------------------------------------------------------------


def test_one_thousand_invocations_hold_the_p95_within_the_budget(harness: Harness) -> None:
    """A thousand real invocations, signed and transmitted, inside 250 ms at p95.

    The warm-up invocation ahead of the sample is timed and reported rather than
    dropped, so the cost the first invocation of a fresh interpreter pays is visible
    beside the cost the rest pay.
    """
    first = timed_invocation(harness.payload)
    samples = tuple(timed_invocation(harness.payload) for _ in range(INVOCATIONS))
    spread = Spread(label="in-process invocations", samples=samples)

    print(f"first invocation, including the lazy imports: {first:.2f} ms")
    print(spread.summary())
    assert_signed(harness.collector.received, at_least=INVOCATIONS + 1)
    assert len(samples) == INVOCATIONS
    assert percentile(samples, ASSERTED_FRACTION) <= LATENCY_BOUND_MS, spread.summary()


def test_spawned_invocations_report_the_start_up_cost_the_sample_omits(
    harness: Harness,
) -> None:
    """The same entry point in real processes, to quantify what in-process omits.

    No bound is asserted on this figure. The requirement's budget is stated per
    invocation and the hook is a process in the field, so the honest reading of the
    case above is *the hook's own work fits the budget with this much room*, and the
    room is what this reports. Asserting a bound here would make the benchmark a
    measurement of the interpreter's start-up on the machine that ran it.
    """
    assert (TOOL, EVENT_NAME) == SPAWN_ARGUMENTS
    for _ in range(SPAWN_WARMUPS):
        timed_spawn(harness.root, harness.payload)
    before = len(harness.collector.received)
    samples = tuple(timed_spawn(harness.root, harness.payload) for _ in range(SPAWNS))
    spread = Spread(label="spawned invocations", samples=samples)

    print(spread.summary())
    assert_signed(harness.collector.received[before:], at_least=SPAWNS)
    assert len(samples) == SPAWNS
