"""Gate assertion that a watcher built from configuration can open its own stream.

The change stream is the Policy_Watcher's primary mechanism and the timestamp poll is its
documented fallback. In every deployment the fallback ran, and the reason was not the
cluster: `Watcher.from_configuration` defaulted its stream opener to `None`, a watcher with
no opener refuses its own stream before the cluster is asked, and that refusal is caught by
the same handler that catches a refusal *from* the cluster. So it was recorded as the
`changefeed` capability being unavailable and logged as a degradation, and the deployment's
own documentation then concluded from that record that the cluster's plan did not serve a
sinkless changefeed.

The cluster served it the whole time. Nothing had ever asked. Probing the deployed cluster as
the watcher's own role, with the watcher's own statement, was permitted on the first attempt.

Three things made it invisible, and each is a pattern rather than an accident. The fallback
works, so nothing failed. The refusal was reported through the same three channels a real
refusal uses — a capability row, a metric, a log record — so the report looked healthy. And
the degradation reason was a fixed sentence naming the cluster, which is the one thing that
was not at fault.

So this asserts the join rather than the halves: a watcher built the way a deployment builds
one holds an opener, and calling that opener reaches the store for a connection. No cluster is
needed to check either, which is the point — the defect was never visible from a cluster.

**Validates: Requirements 23.1, 23.4**
"""

from __future__ import annotations

from typing import Final, cast

import pytest

from molt.config.resolve import Configuration
from molt.policy.watcher import ModePreference, Watcher, WatcherSettings, store_opener
from molt.store import MemoryStore

# Static gate over the wiring: no reachable instance and no credential.
pytestmark: Final[pytest.MarkDecorator] = pytest.mark.quality

# The surface values a watcher reads at construction. Supplied as an environment mapping
# rather than by patching the settings reader, so the surface is exercised as written.
SURFACE: Final[dict[str, str]] = {
    "MOLT_WATCHER_MODE": ModePreference.AUTO.value,
    "MOLT_WATCHER_POLL_INTERVAL_SECONDS": "2",
    "MOLT_WATCHER_RESOLVED_INTERVAL": "2s",
    "MOLT_POLICY_RULES_PATH": "",
}


class _RefusingStore:
    """A store that records whether a dedicated connection was ever asked for.

    It refuses to open one, because what is under test is whether the watcher reaches for a
    connection at all. Refusing is also what keeps this a unit test: a store that returned
    something connection-shaped would have to behave like a cluster to be useful.
    """

    def __init__(self) -> None:
        self.asked = 0

    def open_dedicated(self) -> object:
        self.asked += 1
        raise ConnectionError("this store opens no connection under test")


def _store() -> tuple[MemoryStore, _RefusingStore]:
    """A recording stand-in, narrowed to the shape the watcher asks of a store."""
    recorder = _RefusingStore()
    return cast(MemoryStore, recorder), recorder


def test_a_watcher_built_from_configuration_holds_a_stream_opener() -> None:
    """The opener is supplied by default, so the primary mechanism is the deployed one."""
    store, _ = _store()

    watcher = Watcher.from_configuration(store, Configuration(environ=SURFACE))

    assert watcher.can_open_stream, (
        "a watcher built the way a deployment builds one has no stream opener, so it "
        "refuses its own change stream before the cluster is asked and reports the refusal "
        "as the cluster's"
    )


def test_the_default_opener_reaches_the_store_for_a_connection_of_its_own() -> None:
    """Calling the opener asks the store for a connection outside its pool.

    Holding an opener is not enough: one that never called the store would satisfy the case
    above and still open nothing. This checks that the call arrives.
    """
    store, recorder = _store()
    opener = store_opener(store)

    with pytest.raises(ConnectionError):
        opener("EXPERIMENTAL CHANGEFEED FOR ledger", ())

    assert recorder.asked == 1, (
        "the default opener did not ask the store for a dedicated connection, so no stream "
        f"would ever be opened; the store was asked {recorder.asked} times"
    )


def test_a_watcher_given_no_opener_at_all_still_reports_one_is_absent() -> None:
    """The detector is checked against the state it exists to catch.

    Constructing a watcher directly with no opener is the state every deployment was in, and
    the assertion above has to fail for it or it proves nothing.
    """
    store, _ = _store()

    watcher = Watcher(store, (), settings=WatcherSettings(), opener=None)

    assert not watcher.can_open_stream
