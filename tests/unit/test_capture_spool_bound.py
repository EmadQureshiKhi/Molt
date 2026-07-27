"""Unit checks over the bounded capture spool: append, the bound, and the counter.

These cover the claims the spool is built on rather than every branch of it: one
record per line under append, whole records dropped from the head at the bound,
the discarded count surviving the compaction rename, a record larger than the
bound resolving rather than wedging, and a claimed batch holding records rather
than a prepared request.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from molt.capture.spool import (
    RECORD_SEPARATOR,
    Spool,
    resolve_machine_id,
    spool_path,
)
from molt.config.resolve import Configuration
from molt.models.event import Event, EventCategory

MACHINE_ID = "test-machine"


def build_event(*, filler: int = 0) -> Event:
    """One Event whose payload can be grown to a chosen size."""
    return Event(
        id=uuid4(),
        session_id=UUID(int=1),
        client_id=UUID(int=2),
        category=EventCategory.TOOL_CALL,
        occurred_at=datetime.fromtimestamp(0, tz=UTC),
        agent_cli="decorator",
        machine_id=MACHINE_ID,
        parent_event_id=None,
        payload={"tool": "read", "filler": "x" * filler},
        redacted=False,
        text_body=None,
    )


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    """A spool in a fresh directory, bounded generously unless a test rebinds it."""
    return Spool(tmp_path, MACHINE_ID, max_bytes=1_000_000)


def test_each_appended_record_is_one_line_in_the_machine_named_file(spool: Spool) -> None:
    events = [build_event(), build_event(), build_event()]

    outcome = spool.append(events)

    assert outcome.written == 3
    assert outcome.discarded == 0
    assert spool.path == spool_path(spool.directory, MACHINE_ID)
    raw = spool.path.read_bytes()
    assert raw.endswith(RECORD_SEPARATOR)
    assert len(raw.splitlines()) == 3


def test_a_second_append_adds_to_the_file_rather_than_replacing_it(spool: Spool) -> None:
    spool.append([build_event()])
    spool.append([build_event(), build_event()])

    assert len(spool.records()) == 3


def test_the_bound_drops_whole_records_from_the_head_and_counts_them(tmp_path: Path) -> None:
    wide = Spool(tmp_path, MACHINE_ID, max_bytes=1_000_000)
    wide.append([build_event() for _ in range(10)])
    record_size = wide.size_bytes() // 10
    keep = 4
    bounded = Spool(tmp_path, MACHINE_ID, max_bytes=record_size * keep)

    discarded = bounded.enforce_bound()

    assert discarded == 10 - keep
    assert bounded.size_bytes() <= bounded.max_bytes
    survivors = bounded.records()
    assert len(survivors) == keep
    # Whole records survive: every line still reads back as an Event.
    assert all(event.category is EventCategory.TOOL_CALL for event in survivors)


def test_the_discarded_count_survives_the_compaction_rename(tmp_path: Path) -> None:
    wide = Spool(tmp_path, MACHINE_ID, max_bytes=1_000_000)
    wide.append([build_event() for _ in range(6)])
    record_size = wide.size_bytes() // 6
    bounded = Spool(tmp_path, MACHINE_ID, max_bytes=record_size * 2)

    bounded.enforce_bound()

    assert bounded.counter_path.is_file()
    assert bounded.discarded_count() == 4
    # A second breach adds to the count rather than replacing it.
    bounded.append([build_event() for _ in range(3)])
    assert bounded.discarded_count() > 4


def test_a_record_larger_than_the_bound_is_discarded_rather_than_wedging_the_file(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path, MACHINE_ID, max_bytes=256)

    outcome = spool.append([build_event(filler=4096)])

    assert outcome.written == 1
    assert outcome.discarded == 1
    assert spool.size_bytes() == 0
    assert spool.discarded_count() == 1


def test_a_claim_takes_the_records_and_confirming_clears_the_count(spool: Spool) -> None:
    spool.append([build_event(), build_event()])

    batch = spool.claim()

    assert len(batch.events) == 2
    assert batch.unreadable == 0
    assert spool.is_empty()
    assert batch.claim_path is not None
    spool.confirm(batch)
    assert batch.claim_path is not None
    assert not batch.claim_path.exists()
    assert spool.discarded_count() == 0


def test_releasing_a_claim_puts_the_records_back_with_records_appended_meanwhile(
    spool: Spool,
) -> None:
    spool.append([build_event(), build_event()])
    batch = spool.claim()
    spool.append([build_event()])

    restored = spool.release(batch)

    assert restored == 2
    assert len(spool.records()) == 3
    assert batch.claim_path is not None
    assert not batch.claim_path.exists()


def test_a_claim_of_an_empty_spool_reports_nothing_to_transmit(spool: Spool) -> None:
    batch = spool.claim()

    assert batch.empty
    assert batch.events == ()
    assert batch.claim_path is None


def test_a_torn_final_line_is_skipped_and_the_whole_records_survive(spool: Spool) -> None:
    spool.append([build_event(), build_event()])
    with spool.path.open("ab") as handle:
        handle.write(b'{"id": "not a whole record')

    batch = spool.claim()

    assert len(batch.events) == 2
    assert batch.unreadable == 1


def test_a_machine_identifier_cannot_steer_a_write_out_of_the_spool_directory(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path, "../escape/attempt")

    spool.append([build_event()])

    assert spool.path.parent == tmp_path
    assert spool.path.is_file()


def test_the_configured_machine_identifier_names_the_file_and_an_empty_one_is_derived() -> None:
    configured = Configuration(environ={"MOLT_MACHINE_ID": "workstation-7"}, file_values={})
    derived = Configuration(environ={}, file_values={})

    assert resolve_machine_id(configured) == "workstation-7"
    assert resolve_machine_id(derived).startswith("host-")


def test_the_spool_is_built_from_the_configuration_surface(tmp_path: Path) -> None:
    configuration = Configuration(
        environ={
            "MOLT_SPOOL_DIR": str(tmp_path / "spool"),
            "MOLT_SPOOL_MAX_BYTES": "4096",
            "MOLT_MACHINE_ID": MACHINE_ID,
        },
        file_values={},
    )

    spool = Spool.from_configuration(configuration)

    assert spool.directory == tmp_path / "spool"
    assert spool.max_bytes == 4096
    assert spool.machine_id == MACHINE_ID


def test_no_database_driver_is_reachable_from_the_spool_module() -> None:
    for name in ("molt.capture.spool", "psycopg"):
        sys.modules.pop(name, None)
    module = importlib.import_module("molt.capture.spool")

    assert module.DEFAULT_MAX_BYTES == 67108864
    assert "psycopg" not in sys.modules
