"""Property 21: an expiry is the write instant plus the Jurisdiction's interval, always.

The generated stream places Artifact writes across many Jurisdictions, over retention
intervals from one hour to ten years, at write instants carrying every offset a
capture host might report. Three claims are asserted over each example.

The stored expiry is the write instant plus that Jurisdiction's interval, checked as a
difference rather than by restating the sum, so the assertion is not the
implementation written twice.

The relation is monotone in both arguments. Two writes under one Jurisdiction expire
in the order they were written, and one write instant under a longer interval expires
later. That is what makes retention orderly rather than merely arithmetic: an
Artifact cannot be made to outlive one written after it by any choice of instant a
caller passes.

The working tier is outside the relation entirely. Its interval is fixed by the tier,
so the same write instant under any Jurisdiction produces the same working expiry,
which is asserted here so that a later reader cannot quietly route working state
through a Client's regime.

**Validates: Requirements 14.3, 14.4**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import NAMESPACE_OID, UUID, uuid5

from hypothesis import given, settings
from hypothesis import strategies as st

from molt.retention import ClientRetention, expiry_for
from molt.store.working import WorkingInterval

# The interval range the requirement's per-Jurisdiction support has to cover, stated
# as counts of seconds because that is the form arithmetic over it is exact in.
ONE_HOUR_SECONDS: Final[int] = 3600
TEN_YEARS_SECONDS: Final[int] = 3653 * 86400

# The working tier's own interval, fixed by the tier rather than by any regime.
WORKING_SECONDS: Final[int] = 3600

# The Jurisdiction names examples draw from. Opaque labels: nothing about retention
# turns on what a Jurisdiction is called, and a test that depended on a name would be
# asserting a vocabulary this module has no business fixing.
JURISDICTIONS: Final[tuple[str, ...]] = ("default", "alpha", "beta", "gamma", "delta")

# The offsets a write instant may carry. A capture host reports its own offset, so an
# expiry must be correct for an instant that is not stated in UTC.
OFFSETS: Final[tuple[timezone, ...]] = tuple(
    timezone(timedelta(hours=hours)) for hours in range(-12, 15)
)

# The instant range write timestamps are drawn from. The upper bound leaves room for
# the longest interval, so no example asks for an expiry the calendar cannot hold.
_EARLIEST: Final[datetime] = datetime(1, 1, 2)  # noqa: DTZ001 - a bound, made aware below
_LATEST: Final[datetime] = datetime(9000, 1, 1)  # noqa: DTZ001 - a bound, made aware below

MAX_WRITES: Final[int] = 25


@dataclass(frozen=True, slots=True)
class ArtifactWrite:
    """One Artifact write: the regime it falls under and the instant it happened."""

    jurisdiction: str
    interval: timedelta
    written_at: datetime


@st.composite
def artifacts_across_jurisdictions(draw: st.DrawFn) -> tuple[ArtifactWrite, ...]:
    """Generate Artifact writes across Jurisdictions, intervals, and offsets.

    Each Jurisdiction drawn keeps one interval for the whole example, because a
    Jurisdiction naming two intervals at once would make the claim untestable rather
    than false. The bounds of the interval range are drawn from explicitly, so the
    one-hour and ten-year ends are exercised rather than merely admitted.
    """
    names = draw(st.lists(st.sampled_from(JURISDICTIONS), min_size=1, max_size=len(JURISDICTIONS)))
    seconds = st.one_of(
        st.sampled_from((ONE_HOUR_SECONDS, TEN_YEARS_SECONDS)),
        st.integers(min_value=ONE_HOUR_SECONDS, max_value=TEN_YEARS_SECONDS),
    )
    intervals = {name: timedelta(seconds=draw(seconds)) for name in names}

    instants = st.datetimes(
        min_value=_EARLIEST,
        max_value=_LATEST,
        timezones=st.sampled_from(OFFSETS),
    )
    writes = draw(
        st.lists(
            st.tuples(st.sampled_from(sorted(intervals)), instants),
            min_size=1,
            max_size=MAX_WRITES,
        )
    )
    return tuple(
        ArtifactWrite(jurisdiction=name, interval=intervals[name], written_at=instant)
        for name, instant in writes
    )


# Feature: molt, Property 21: For any Artifact written under any Jurisdiction at any write
# timestamp, the Artifact's expiry timestamp equals its write timestamp plus that
# Jurisdiction's retention interval.
@given(artifacts_across_jurisdictions())
@settings(max_examples=100)
def test_an_expiry_is_the_write_instant_plus_the_jurisdiction_interval(
    writes: tuple[ArtifactWrite, ...],
) -> None:
    """Every write's expiry is its own instant plus its own regime's interval."""
    working = WorkingInterval(seconds=WORKING_SECONDS)
    by_jurisdiction: dict[str, list[tuple[datetime, datetime]]] = {}

    for write in writes:
        regime = ClientRetention(
            client_id=_client_of(write.jurisdiction),
            jurisdiction=write.jurisdiction,
            interval=write.interval,
        )
        expiry = regime.expiry_from(write.written_at)

        assert expiry - write.written_at == write.interval
        assert expiry > write.written_at
        assert expiry == expiry_for(write.written_at, write.interval)

        longer = expiry_for(write.written_at, write.interval + timedelta(seconds=1))
        assert longer > expiry, "a longer interval cannot expire an Artifact sooner"

        assert working.expiry_from(write.written_at) - write.written_at == timedelta(
            seconds=WORKING_SECONDS
        ), "the working tier's lifetime is a property of the tier and not of the regime"

        by_jurisdiction.setdefault(write.jurisdiction, []).append((write.written_at, expiry))

    for pairs in by_jurisdiction.values():
        ordered = sorted(pairs, key=lambda pair: pair[0])
        expiries = [expiry for _, expiry in ordered]
        assert expiries == sorted(expiries), (
            "under one Jurisdiction, Artifacts expire in the order they were written"
        )


def _client_of(jurisdiction: str) -> UUID:
    """A stable Client identifier per Jurisdiction, so a regime is a whole shape."""
    return uuid5(NAMESPACE_OID, jurisdiction)
