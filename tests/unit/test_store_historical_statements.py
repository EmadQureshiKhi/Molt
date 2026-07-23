"""Unit tests for the historical read: the rendered clause, the horizon, the framing.

Nothing here opens a socket. A scripted cursor answers each statement from a
script and keeps what it was sent, so every claim below is asserted by reading
what the module produced. The claims that need a cluster to be meaningful, that a
read at a past instant really returns the state of that instant, live in the
instance-backed module.

Four properties of the shape are checked here.

The rendered instant is the one value the store does not bind, so it is validated
twice over. The rendering is compared against an independent parse rather than
against a literal, and the form check is hammered with everything that could
carry a quote, a terminator, a comment marker, a newline, or a digit from another
script into statement text.

The horizon is read from the capability record and never assumed. An absent row,
an unavailable reading, and a detail that is not a count of seconds each refuse,
and the refusal names what was missing rather than substituting an interval.

An instant outside the horizon is refused before anything is sent, and an instant
the cluster itself refuses is translated into the same named failure. In neither
case does a second read go out, which is what the no-fallback obligation means in
terms of statements.

The read is framed as one transaction whose first statement is the historical
clause, and the caller's own statement travels unchanged with its values bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

import pytest

from molt.errors import HistoricalHorizonError, StoreError
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.historical import (
    AS_OF_STATEMENT_PREFIX,
    CAPABILITY_QUERY,
    GC_HORIZON_CAPABILITY,
    UTC_OFFSET,
    GcHorizon,
    as_of_system_time,
    gc_horizon,
    historical,
    render_as_of_timestamp,
    require_rendered_form,
    within_gc_horizon,
)
from molt.store.retry import BEGIN_STATEMENT, COMMIT_STATEMENT, ROLLBACK_STATEMENT

# An instant with an offset, fixed so no example depends on when it ran and no
# timestamp is written out anywhere in this module.
MOMENT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# The horizon the scripted capability row records, as the detail column holds it.
HORIZON_SECONDS: Final[int] = 4500
HORIZON_DETAIL: Final[str] = str(HORIZON_SECONDS)

# The caller statement every read in this module sends, and the value it binds.
COUNT_STATEMENT: Final[str] = "SELECT count(*) FROM derived_artifact WHERE owner_client_id = %s"
COUNT_FRAGMENT: Final[str] = "FROM derived_artifact"
CAPABILITY_FRAGMENT: Final[str] = "FROM capability"

# The characters a rendered instant may be built from, so the composition is
# checked against a set rather than against a written-out example.
PERMITTED_CHARACTERS: Final[frozenset[str]] = frozenset("0123456789" + "-.: " + UTC_OFFSET)

# What the cluster says when it refuses an instant older than the threshold it
# still holds versions back to.
CLUSTER_REFUSAL: Final[str] = (
    "batch timestamp must be after replica GC threshold; the versions are collected"
)


# ---------------------------------------------------------------------------
# The scripted cursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """What the script answers for the first statement holding a fragment."""

    fragment: str
    rows: tuple[tuple[object, ...], ...] = ()
    error: Exception | None = None


@dataclass(slots=True)
class Script:
    """The answers a connection hands out, consumed in the order they match."""

    answers: list[Answer] = field(default_factory=list)
    sent: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    armed: tuple[tuple[object, ...], ...] = ()

    @property
    def statements(self) -> list[str]:
        """Every statement the script was sent, in order."""
        return [query for query, _ in self.sent]

    def parameters_of(self, statement: str) -> tuple[object, ...] | None:
        """The bound parameters of the one occurrence of a statement."""
        matches = [params for query, params in self.sent if query == statement]
        assert len(matches) == 1, f"the statement should have been sent once, not {len(matches)}"
        return matches[0]

    def take(self, query: str) -> Answer | None:
        """The next answer matching a statement, removed from the script."""
        for index, answer in enumerate(self.answers):
            if answer.fragment in query:
                return self.answers.pop(index)
        return None


class ScriptedCursor:
    """A cursor answering from a script and recording what it was sent."""

    def __init__(self, script: Script) -> None:
        self._script = script
        self.released = False

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then raise or arm rows as the script says."""
        self._script.sent.append((query, None if params is None else tuple(params)))
        answer = self._script.take(query)
        if answer is None:
            self._script.armed = ()
            return None
        if answer.error is not None:
            raise answer.error
        self._script.armed = answer.rows
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first armed row, or None when the statement armed none."""
        rows = self._script.armed
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every armed row."""
        return list(self._script.armed)

    def close(self) -> None:
        """Mark this cursor released."""
        self.released = True


class ScriptedConnection:
    """A connection handing out scripted cursors over one shared script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        """Open a recording cursor over this connection's script."""
        return ScriptedCursor(self.script)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True


class WidenedYear(datetime):
    """An instant whose year component renders wider than the fixed form admits.

    A hostile shape rather than a realistic one: it stands for anything
    datetime-like whose components render to something the form does not admit,
    and it is what shows the form check rather than the declared type is what
    makes the composition safe. The conversion to UTC returns this same object,
    because the instant is already in UTC, so the widened component survives it.
    """

    @property
    def year(self) -> int:
        """A year of five digits, which no fixed-width field can hold."""
        return 12345


def build_store(script: Script) -> MemoryStore:
    """A store whose only connection is the scripted one, with no waiting."""
    connection = ScriptedConnection(script)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


def capability_answer(available: bool = True, detail: object = HORIZON_DETAIL) -> Answer:
    """The scripted answer for the capability read."""
    return Answer(CAPABILITY_FRAGMENT, ((available, detail),))


def valid_rendering() -> str:
    """One rendering the form admits, built by the module rather than written out."""
    return render_as_of_timestamp(MOMENT)


def issued(script: Script) -> list[str]:
    """What the module sent, with the pool's own setup and reset removed.

    Every lease establishes the statement timeout and every returned connection
    is reset, so those two statements belong to the connection surface rather
    than to the module under test. The reset shares its text with the abandon
    statement, so a claim about what a read sent is read off the ordering below
    rather than off the presence of that word.
    """
    return [
        query
        for query in script.statements
        if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT)
    ]


def follows(statements: list[str], query: str) -> str:
    """The statement sent immediately after the one named."""
    return statements[statements.index(query) + 1]


# ---------------------------------------------------------------------------
# The rendered instant
# ---------------------------------------------------------------------------


def test_a_rendered_instant_parses_back_to_the_instant_it_came_from() -> None:
    """The rendering is checked against an independent parse, not against a literal."""
    moment = MOMENT + timedelta(days=15, hours=7, minutes=42, seconds=13, microseconds=456789)

    rendered = render_as_of_timestamp(moment)

    assert datetime.fromisoformat(rendered) == moment
    assert set(rendered) <= PERMITTED_CHARACTERS
    assert rendered.endswith(UTC_OFFSET)


def test_an_instant_in_another_offset_is_rendered_in_utc() -> None:
    """One instant has one rendering, whatever offset the caller presented it in."""
    elsewhere = timezone(timedelta(hours=5, minutes=30))
    moment = (MOMENT + timedelta(hours=9)).astimezone(elsewhere)

    rendered = render_as_of_timestamp(moment)

    assert datetime.fromisoformat(rendered) == moment
    assert rendered.endswith(UTC_OFFSET)


def test_a_naive_instant_is_refused() -> None:
    """An instant with no offset has no defined position on the timeline."""
    with pytest.raises(ValueError, match="timezone aware"):
        render_as_of_timestamp(MOMENT.replace(tzinfo=None))


def test_the_composed_statement_is_the_prefix_and_one_quoted_instant() -> None:
    """Nothing but the module's prefix, a quote, the rendering, and a quote."""
    composed = as_of_system_time(MOMENT)

    assert composed.startswith(AS_OF_STATEMENT_PREFIX)
    argument = composed[len(AS_OF_STATEMENT_PREFIX) :]
    assert argument.startswith("'")
    assert argument.endswith("'")
    assert composed.count("'") == 2
    assert require_rendered_form(argument[1:-1]) == valid_rendering()


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param(valid_rendering() + "'; DROP TABLE ledger", id="terminator_and_statement"),
        pytest.param(valid_rendering() + "' --", id="comment_marker"),
        pytest.param(valid_rendering() + "/*", id="block_comment"),
        pytest.param(valid_rendering() + "\n" + AS_OF_STATEMENT_PREFIX, id="newline_and_clause"),
        pytest.param("'" + valid_rendering() + "'", id="already_quoted"),
        pytest.param(" " + valid_rendering(), id="leading_space"),
        pytest.param(valid_rendering() + " ", id="trailing_space"),
        pytest.param(valid_rendering() + "0", id="trailing_digit"),
        pytest.param(valid_rendering().replace(" ", "T"), id="other_separator"),
        pytest.param(valid_rendering().replace(UTC_OFFSET, "+01"), id="other_offset"),
        pytest.param(valid_rendering().removesuffix(UTC_OFFSET), id="no_offset"),
        pytest.param(valid_rendering().replace(".", ",", 1), id="other_fraction_mark"),
        pytest.param(valid_rendering().replace("0", "\u0660"), id="digits_of_another_script"),
        pytest.param("", id="empty"),
        pytest.param("now()", id="function_call"),
    ],
)
def test_a_rendering_that_is_not_the_fixed_form_is_refused(hostile: str) -> None:
    """Every way a value could reach statement text is refused by the form check."""
    with pytest.raises(StoreError, match="fixed digit form"):
        require_rendered_form(hostile)


def test_a_component_wider_than_the_fixed_form_is_refused() -> None:
    """The form check rather than the declared type is what makes this safe."""
    with pytest.raises(StoreError, match="fixed digit form"):
        as_of_system_time(WidenedYear.fromtimestamp(0.0, tz=UTC))


# ---------------------------------------------------------------------------
# The horizon, read rather than assumed
# ---------------------------------------------------------------------------


def test_the_horizon_read_binds_the_capability_name() -> None:
    """One statement, one bound name, and the seconds the detail column holds."""
    script = Script(answers=[capability_answer()])

    assert gc_horizon(build_store(script)) == GcHorizon(seconds=HORIZON_SECONDS)

    assert script.parameters_of(CAPABILITY_QUERY) == (GC_HORIZON_CAPABILITY,)
    assert BEGIN_STATEMENT not in script.statements


def test_an_absent_capability_row_refuses_rather_than_assuming_a_default() -> None:
    """A horizon nobody probed is not a horizon, so no read is measured against one."""
    script = Script(answers=[Answer(CAPABILITY_FRAGMENT, ())])

    with pytest.raises(StoreError, match="has not been probed"):
        gc_horizon(build_store(script))


def test_a_capability_row_reporting_the_probe_unavailable_refuses() -> None:
    """A probe that did not answer leaves the horizon unknown, and unknown refuses."""
    script = Script(answers=[capability_answer(available=False)])

    with pytest.raises(StoreError, match="unavailable"):
        gc_horizon(build_store(script))


@pytest.mark.parametrize(
    "detail",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("4500 seconds", id="unit_suffix"),
        pytest.param("-4500", id="signed"),
        pytest.param("4_500", id="separator"),
        pytest.param("4500.0", id="fractional"),
        pytest.param("\u0664\u0665\u0660\u0660", id="digits_of_another_script"),
        pytest.param(4500, id="not_text"),
    ],
)
def test_a_detail_that_is_not_a_count_of_seconds_refuses(detail: object) -> None:
    """The detail column carries digits and nothing else, or the horizon is unread."""
    script = Script(answers=[capability_answer(detail=detail)])

    with pytest.raises(StoreError, match="count of seconds"):
        gc_horizon(build_store(script))


def test_a_capability_row_of_the_wrong_width_is_refused() -> None:
    """A statement and its decoder cannot drift apart silently."""
    script = Script(answers=[Answer(CAPABILITY_FRAGMENT, ((True,),))])

    with pytest.raises(StoreError, match="column"):
        gc_horizon(build_store(script))


def test_a_horizon_of_no_seconds_is_refused() -> None:
    """A horizon reaching no distance would make every instant unreachable silently."""
    with pytest.raises(ValueError, match="positive number of seconds"):
        GcHorizon(seconds=0)


# ---------------------------------------------------------------------------
# The predicate callers consult first
# ---------------------------------------------------------------------------


def test_the_predicate_admits_the_floor_and_refuses_the_instant_before_it() -> None:
    """The horizon's floor is reachable, and one second older is not."""
    horizon = GcHorizon(seconds=HORIZON_SECONDS)
    script = Script(answers=[capability_answer(), capability_answer()])
    store = build_store(script)
    now = MOMENT + timedelta(days=30)

    assert within_gc_horizon(store, now - horizon.interval, now=now) is True
    assert within_gc_horizon(store, now - horizon.interval - timedelta(seconds=1), now=now) is False


def test_the_predicate_refuses_an_instant_ahead_of_the_reading() -> None:
    """A state that does not exist yet is not a state a historical read reaches."""
    script = Script(answers=[capability_answer(), capability_answer()])
    store = build_store(script)
    now = MOMENT + timedelta(days=30)

    assert within_gc_horizon(store, now, now=now) is True
    assert within_gc_horizon(store, now + timedelta(seconds=1), now=now) is False


def test_the_predicate_uses_a_horizon_it_is_handed_without_reading_one() -> None:
    """A caller that already read the horizon does not read it twice."""
    script = Script()
    now = MOMENT + timedelta(days=30)

    reachable = within_gc_horizon(
        build_store(script),
        now - timedelta(seconds=10),
        now=now,
        horizon=GcHorizon(seconds=HORIZON_SECONDS),
    )

    assert reachable is True
    assert script.statements == []


# ---------------------------------------------------------------------------
# The framing of a read
# ---------------------------------------------------------------------------


def test_a_read_frames_one_transaction_and_sets_the_instant_first() -> None:
    """The clause is the transaction's first statement, then the caller's own."""
    client_id = "a-tenant"
    now = MOMENT + timedelta(days=30)
    at = now - timedelta(seconds=60)
    script = Script(answers=[capability_answer(), Answer(COUNT_FRAGMENT, ((7,),))])

    rows = historical(build_store(script), COUNT_STATEMENT, (client_id,), at=at, now=now)

    assert rows == ((7,),)
    statements = script.statements
    clause = as_of_system_time(at)
    assert statements.index(BEGIN_STATEMENT) < statements.index(clause)
    assert statements.index(clause) < statements.index(COUNT_STATEMENT)
    assert statements.count(clause) == 1
    assert follows(statements, COUNT_STATEMENT) == COMMIT_STATEMENT


def test_the_caller_statement_travels_unchanged_with_its_values_bound() -> None:
    """Nothing rewrites, parses, or concatenates the statement a caller supplied."""
    client_id = "a-tenant"
    now = MOMENT + timedelta(days=30)
    script = Script(answers=[capability_answer(), Answer(COUNT_FRAGMENT, ())])

    historical(
        build_store(script),
        COUNT_STATEMENT,
        (client_id,),
        at=now - timedelta(seconds=60),
        now=now,
    )

    assert COUNT_STATEMENT in script.statements
    assert script.parameters_of(COUNT_STATEMENT) == (client_id,)


def test_a_read_at_an_instant_beyond_the_horizon_sends_no_read() -> None:
    """The refusal names the horizon, and nothing reaches the cluster but the probe."""
    now = MOMENT + timedelta(days=30)
    at = now - timedelta(seconds=HORIZON_SECONDS + 1)
    script = Script(answers=[capability_answer()])

    with pytest.raises(HistoricalHorizonError, match=str(HORIZON_SECONDS)):
        historical(build_store(script), COUNT_STATEMENT, (None,), at=at, now=now)

    assert issued(script) == [CAPABILITY_QUERY]
    assert not [query for query in script.statements if AS_OF_STATEMENT_PREFIX in query]


def test_a_read_at_an_instant_ahead_of_the_reading_sends_no_read() -> None:
    """A future instant is refused with the horizon named and nothing sent."""
    now = MOMENT + timedelta(days=30)
    script = Script(answers=[capability_answer()])

    with pytest.raises(HistoricalHorizonError, match="later than the current reading"):
        historical(
            build_store(script),
            COUNT_STATEMENT,
            (None,),
            at=now + timedelta(seconds=1),
            now=now,
        )

    assert issued(script) == [CAPABILITY_QUERY]


def test_a_cluster_refusal_of_the_instant_becomes_the_named_horizon_failure() -> None:
    """A recorded horizon staler than the cluster's own is the case this covers."""
    now = MOMENT + timedelta(days=30)
    at = now - timedelta(seconds=60)
    script = Script(
        answers=[
            capability_answer(),
            Answer(COUNT_FRAGMENT, error=RuntimeError(CLUSTER_REFUSAL)),
        ]
    )

    with pytest.raises(HistoricalHorizonError, match=str(HORIZON_SECONDS)):
        historical(build_store(script), COUNT_STATEMENT, (None,), at=at, now=now)

    statements = script.statements
    assert statements.count(COUNT_STATEMENT) == 1, "no read is retried at any instant"
    assert len([query for query in statements if AS_OF_STATEMENT_PREFIX in query]) == 1
    assert follows(statements, COUNT_STATEMENT) == ROLLBACK_STATEMENT
    assert COMMIT_STATEMENT not in statements


def test_a_failure_this_module_does_not_name_propagates_untouched() -> None:
    """A syntax fault or a permission refusal is not renamed into a horizon failure."""
    now = MOMENT + timedelta(days=30)
    script = Script(
        answers=[
            capability_answer(),
            Answer(COUNT_FRAGMENT, error=RuntimeError("relation does not exist")),
        ]
    )

    with pytest.raises(RuntimeError, match="relation"):
        historical(
            build_store(script),
            COUNT_STATEMENT,
            (None,),
            at=now - timedelta(seconds=60),
            now=now,
        )

    assert follows(script.statements, COUNT_STATEMENT) == ROLLBACK_STATEMENT


def test_a_read_handed_a_horizon_reads_no_capability_row() -> None:
    """A caller that already read the horizon spends no round trip reading it again."""
    now = MOMENT + timedelta(days=30)
    script = Script(answers=[Answer(COUNT_FRAGMENT, ((3,),))])

    rows = historical(
        build_store(script),
        COUNT_STATEMENT,
        (None,),
        at=now - timedelta(seconds=60),
        now=now,
        horizon=GcHorizon(seconds=HORIZON_SECONDS),
    )

    assert rows == ((3,),)
    assert CAPABILITY_QUERY not in script.statements


def test_a_read_with_no_horizon_probed_refuses_before_it_frames_anything() -> None:
    """The horizon is read first, so an unprobed cluster costs no transaction."""
    now = MOMENT + timedelta(days=30)
    script = Script(answers=[Answer(CAPABILITY_FRAGMENT, ())])

    with pytest.raises(StoreError, match="has not been probed"):
        historical(
            build_store(script),
            COUNT_STATEMENT,
            (None,),
            at=now - timedelta(seconds=60),
            now=now,
        )

    assert issued(script) == [CAPABILITY_QUERY]


# ---------------------------------------------------------------------------
# The delegating methods
# ---------------------------------------------------------------------------


def test_the_store_delegates_the_three_calls_to_this_module() -> None:
    """The design names these on the store, and the store forwards them here."""
    now = MOMENT + timedelta(days=30)
    script = Script(
        answers=[
            capability_answer(),
            capability_answer(),
            capability_answer(),
            Answer(COUNT_FRAGMENT, ((11,),)),
        ]
    )
    store = build_store(script)

    assert store.gc_horizon() == GcHorizon(seconds=HORIZON_SECONDS)
    assert store.within_gc_horizon(now - timedelta(seconds=60), now=now) is True
    assert store.historical(COUNT_STATEMENT, (None,), at=now - timedelta(seconds=60), now=now) == (
        (11,),
    )
