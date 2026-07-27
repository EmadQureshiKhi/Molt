"""The bound the capture spool holds itself within, and the loss it accounts for.

Requirement 6.5 asks for two things of a spool that has reached its configured
maximum: the oldest records are dropped, and the number dropped is reported on the
next successful transmission. The spool's own suite states that records are dropped
and that the count outlives the compaction; what is asserted here is what those two
sentences leave open.

Which records survive is asserted by identity rather than by count, because a
bound that dropped the newest records would satisfy a counting assertion and lose
the work an engineer just did. The surviving bytes are asserted to be the untouched
tail of the file, which is what makes compaction a rewrite of nothing: a record
that survives is the record that was written, not a re-serialisation of it. The
count is asserted arithmetically across two separate breaches and then through the
transmission that reports and clears it, and a file already within its bound is
asserted to be left byte for byte alone, with no sibling left behind.

Nothing here reaches a database, opens a socket, or waits: the spool lives in the
temporary directory and every instant on a record is a fixed one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.capture.spool import (
    COUNTER_SUFFIX,
    LOCK_SUFFIX,
    RECORD_SEPARATOR,
    Spool,
)
from molt.models.event import Event, EventCategory, JsonObject

MACHINE_ID: Final[str] = "test-machine"

# A bound no case reaches, for the phase that fills the file before a bound is
# applied to it.
GENEROUS_BYTES: Final[int] = 1_000_000

# How many records a case appends before bounding the file, and how many of them a
# bound is then sized to admit.
APPENDED: Final[int] = 10
KEPT: Final[int] = 4

# The ordinal is rendered to a fixed width so every record measures the same, which
# is what lets a case state a bound as a number of records.
ORDINAL_DIGITS: Final[int] = 4


def build_event(ordinal: int) -> Event:
    """One Event carrying its position in the appended sequence.

    Every field is fixed width, including the identifier and the rendered instant,
    so the file is a run of equally sized records and a bound expressed in records
    is a bound expressible in bytes.
    """
    return Event(
        id=uuid4(),
        session_id=UUID(int=1),
        client_id=UUID(int=2),
        category=EventCategory.TOOL_CALL,
        occurred_at=datetime.fromtimestamp(0.0, tz=UTC),
        agent_cli="decorator",
        machine_id=MACHINE_ID,
        parent_event_id=None,
        payload={"tool": "read", "ordinal": f"{ordinal:0{ORDINAL_DIGITS}d}"},
        redacted=False,
        text_body=None,
    )


def ordinal_of(payload: JsonObject) -> str:
    """The position marker one spooled record carries."""
    value = payload["ordinal"]
    assert isinstance(value, str)
    return value


def ordinals(spool: Spool) -> list[str]:
    """The position markers of every record the spool currently holds, in order."""
    return [ordinal_of(event.payload) for event in spool.records()]


def expected_ordinals(first: int, count: int) -> list[str]:
    """The markers a case expects to survive, rendered as the records render them."""
    return [f"{ordinal:0{ORDINAL_DIGITS}d}" for ordinal in range(first, first + count)]


@pytest.fixture
def filled(tmp_path: Path) -> Spool:
    """A spool holding a run of equally sized records, bounded generously."""
    spool = Spool(tmp_path, MACHINE_ID, max_bytes=GENEROUS_BYTES)
    spool.append([build_event(ordinal) for ordinal in range(APPENDED)])
    return spool


def record_size(spool: Spool) -> int:
    """What one record of the filled spool measures, separator included."""
    size = spool.size_bytes()
    assert size % APPENDED == 0, "the records of this fixture are of one size"
    return size // APPENDED


def bounded_at(spool: Spool, records: int) -> Spool:
    """A view of the same file whose bound admits a chosen number of records."""
    return Spool(spool.directory, MACHINE_ID, max_bytes=record_size(spool) * records)


# ---------------------------------------------------------------------------
# Which records survive
# ---------------------------------------------------------------------------


def test_the_records_that_survive_the_bound_are_the_newest_ones(filled: Spool) -> None:
    """Requirement 6.5 drops from the head, so the work just observed is what remains.

    A bound that dropped from the tail would count identically and lose the newest
    records, which are the ones an engineer's current attempt produced.
    """
    bounded = bounded_at(filled, KEPT)

    discarded = bounded.enforce_bound()

    assert discarded == APPENDED - KEPT
    assert ordinals(bounded) == expected_ordinals(APPENDED - KEPT, KEPT)
    assert bounded.size_bytes() <= bounded.max_bytes


def test_the_surviving_bytes_are_the_untouched_tail_of_the_file(filled: Spool) -> None:
    """Compaction moves records; it does not re-render them.

    The survivors are streamed from a record boundary into a sibling file that is
    renamed over the original, so what is left is a byte-for-byte suffix of what was
    there. A rewrite that re-serialised the records would produce equal Events and
    unequal bytes, and a signature is taken over bytes.
    """
    original = filled.path.read_bytes()
    bounded = bounded_at(filled, KEPT)

    bounded.enforce_bound()

    survived = bounded.path.read_bytes()
    assert survived == original[-len(survived) :]
    assert original[: -len(survived)].endswith(RECORD_SEPARATOR)
    assert survived.count(RECORD_SEPARATOR) == KEPT


def test_a_file_already_within_its_bound_is_left_exactly_as_it_was(filled: Spool) -> None:
    """A spool below its maximum is not rewritten, counted against, or locked."""
    before = filled.path.read_bytes()

    discarded = filled.enforce_bound()

    assert discarded == 0
    assert filled.path.read_bytes() == before
    assert filled.discarded_count() == 0
    assert not filled.counter_path.exists()
    assert not filled.lock_path.exists()
    assert [path.name for path in sorted(filled.directory.iterdir())] == [filled.path.name]


# ---------------------------------------------------------------------------
# The discarded count
# ---------------------------------------------------------------------------


def test_the_count_totals_the_records_dropped_across_two_separate_breaches(
    filled: Spool,
) -> None:
    """The counter accumulates, so a second outage does not erase the first one's loss."""
    bounded = bounded_at(filled, KEPT)
    first = bounded.enforce_bound()

    second = bounded.append([build_event(ordinal) for ordinal in range(APPENDED, APPENDED + KEPT)])

    assert first == APPENDED - KEPT
    assert second.written == KEPT
    assert second.discarded == KEPT
    assert bounded.discarded_count() == first + second.discarded
    assert ordinals(bounded) == expected_ordinals(APPENDED, KEPT)


def test_the_count_travels_with_the_claim_and_is_cleared_by_the_transmission(
    filled: Spool,
) -> None:
    """The loss is reported on a path that also proves the spool is draining again.

    The count is read into the claimed batch, so the caller reports it once the
    transmission has succeeded, and confirming clears the counter file rather than
    leaving the same loss to be reported on every later success.
    """
    bounded = bounded_at(filled, KEPT)
    bounded.enforce_bound()

    batch = bounded.claim()
    bounded.confirm(batch)

    assert batch.discarded == APPENDED - KEPT
    assert len(batch.events) == KEPT
    assert not bounded.counter_path.exists()
    assert bounded.discarded_count() == 0
    assert bounded.is_empty()


def test_a_release_brings_the_returned_records_back_within_the_bound(filled: Spool) -> None:
    """A failed transmission returns its records to a file that is still bounded.

    The claim was taken while the bound was generous and is released into a spool
    whose bound admits fewer records than the batch, which is the state a machine
    reaches when its configured maximum is lowered during an outage. The bound wins,
    the loss is counted, and the newest records are the ones that remain.
    """
    bounded = bounded_at(filled, KEPT)
    batch = filled.claim()

    restored = bounded.release(batch)

    assert restored == APPENDED
    assert bounded.size_bytes() <= bounded.max_bytes
    assert bounded.discarded_count() == APPENDED - KEPT
    assert ordinals(bounded) == expected_ordinals(APPENDED - KEPT, KEPT)
    assert batch.claim_path is not None
    assert not batch.claim_path.exists()


def test_the_counter_and_the_lock_are_siblings_the_compaction_rename_cannot_replace(
    filled: Spool,
) -> None:
    """The count outlives compaction because it is not held in the file being renamed."""
    bounded = bounded_at(filled, KEPT)

    bounded.enforce_bound()

    assert bounded.counter_path != bounded.path
    assert bounded.counter_path.name.endswith(COUNTER_SUFFIX)
    assert bounded.lock_path.name.endswith(LOCK_SUFFIX)
    assert bounded.counter_path.parent == bounded.path.parent
    assert bounded.counter_path.read_text(encoding="utf-8").strip() == str(APPENDED - KEPT)
