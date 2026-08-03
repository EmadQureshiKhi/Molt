"""Unit tests for lease generation arithmetic over a whole history, refusal hygiene, finalisation.

Nothing here opens a socket. What answers the statements is not a script of canned
rows but a small stateful cluster: it holds the lease rows and the run rows, it
holds a reading of its own clock, it evaluates each statement's predicate against
what it holds, and it snapshots and restores on the transaction boundaries. That
is the difference between this module and the scripted one beside it. A script can
only confirm that the modules asked the right questions in the right order; a
state machine can be driven through a whole sequence of supersessions and then
asked what it holds, which is what the three claims below need.

**A generation is the tenant's historical maximum plus one, and the maximum spans
the tenant's whole history.** Driven over a run of supersessions rather than
asserted on one number: no generation the tenant ever held is repeated, each
successor exceeds every generation the tenant ever held including the closed ones,
and the closed rows chain to their successors. The maximum is also read per tenant,
which is asserted from both sides: a tenant with a long history neither raises nor
lowers the generation a different tenant's first grant takes, and every history
read binds the tenant it was asked about.

**A refusal names the current owner and the current generation, and carries no
secret.** The three environment-only secrets of the configuration surface hold
recognisable synthetic values while the refusal is produced, and the surface is
genuinely read on that path because the acquisition resolves its lease interval
from it. None of those values reaches the failure's text, its arguments, its
notes, or the emitted record, and the record carries exactly the keys the refusal
puts there and no others. The second example refuses from a driver failure whose
own message embeds a synthetic connection string: the refusal is rendered from the
lease facts, so the driver's text is preserved as the cause and restated nowhere.

**A repeated finalisation returns the recorded result unchanged.** The repeat
offers a different outcome, and what comes back is the first outcome field for
field. The state guard is evaluated by the cluster rather than scripted, so the
claim is that the second payload was refused rather than that a canned row was
returned: it appears in neither the returned record, the recorded row, nor the
reading a resuming attempt takes afterwards.

**Validates: Requirements 44.3, 44.4, 44.10, 36.1**
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Final
from uuid import UUID, uuid4

import pytest

from molt.config.resolve import Configuration
from molt.erase.lease import LeaseGrant, acquire, finalisation_for, finalise, register_run
from molt.errors import LeaseRefused, LeaseRefusedError
from molt.models.event import JsonObject
from molt.store import RESET_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, Connection, MemoryStore
from molt.store.erasure_lease import (
    CLOSE_LEASE_STATEMENT,
    CURRENT_LEASE_QUERY,
    FINALISATION_QUERY,
    HIGHEST_GENERATION_QUERY,
    INSERT_LEASE_STATEMENT,
    LEASE_INTERVAL_KEY,
    MARK_FINALISED_STATEMENT,
    NO_GENERATION,
    RECORD_RUN_KEY_STATEMENT,
    UNIQUE_VIOLATION_STATE,
    LeaseInterval,
)
from molt.store.fencing import CURRENT_GENERATION_QUERY, FIRST_GENERATION
from molt.store.retry import (
    BEGIN_STATEMENT,
    COMMIT_STATEMENT,
    ROLLBACK_STATEMENT,
    SERIALIZABLE_STATEMENT,
)
from molt.telemetry import configure, reset

# The two tenants, whose histories must not reach into each other, and the run one
# of them finalises.
CLIENT_ID: Final[UUID] = uuid4()
OTHER_CLIENT_ID: Final[UUID] = uuid4()
RUN_ID: Final[UUID] = uuid4()

# The workers that take ownership in turn, and the keys their attempts carry. Four
# of them, so a history is long enough for a repeat to be visible if one happened.
OWNERS: Final[tuple[str, ...]] = ("worker-alpha", "worker-beta", "worker-gamma", "worker-delta")
KEYS: Final[tuple[str, ...]] = ("attempt-alpha", "attempt-beta", "attempt-gamma", "attempt-delta")

# The window every lease here is granted for, and the instant the fake cluster's
# clock starts at. The window is not the configured default, so no assertion about
# it is satisfied by the configuration surface.
WINDOW: Final[LeaseInterval] = LeaseInterval(seconds=45)
START: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)

# The attempt a history is continued under once its four workers have each held
# ownership, so a fifth grant carries a key of its own rather than repeating one.
LATE_KEY: Final[str] = "attempt-epsilon"

# How far the cluster's clock moves between a finalisation and its repeat. Short
# enough that the window is still open, so the fence admits the repeat, and long
# enough that a restamped instant would be visible.
RESTAMP_DELAY: Final[int] = 7

# The two outcomes a finalisation is offered, which agree in no field, so a repeat
# returning the first cannot be mistaken for a repeat returning the second. The
# refused outcome's status is named on its own, because it is what a leak of the
# second payload would be recognised by.
REFUSED_STATUS: Final[str] = "abandoned-outcome-never-recorded"
FIRST_RESULT: Final[JsonObject] = {"status": "completed", "dispositions": 4}
SECOND_RESULT: Final[JsonObject] = {"status": REFUSED_STATUS, "dispositions": 0}

# The environment-only secrets of the configuration surface, and the obviously
# synthetic values they hold while a refusal is produced. Each value is unmistakable
# in any output it reached, and none of them resembles a credential of any real
# service.
COLLECTOR_ENV: Final[str] = "MOLT_COLLECTOR_TOKEN"
INGRESS_ENV: Final[str] = "MOLT_INGRESS_SECRET"
CONNECTION_ENV: Final[str] = "MOLT_DSN"
SYNTHETIC_BEARER: Final[str] = "synthetic-collector-bearer-never-a-real-value"
SYNTHETIC_KEYING: Final[str] = "synthetic-ingress-keying-material-never-real"

# The connection string is composed from its parts rather than written whole, and
# the part standing for the credential is named for what it is rather than for the
# kind of value it imitates. Both together are why this reads as a synthetic
# fixture to a reader and to the linter alike, while the value still looks like the
# thing whose absence from a refusal is the claim.
SYNTHETIC_ROLE: Final[str] = "synthetic-role"
SYNTHETIC_CREDENTIAL_PART: Final[str] = "synthetic-credential-portion-never-real"
SYNTHETIC_HOST: Final[str] = "synthetic-host"
SYNTHETIC_CONNECTION: Final[str] = (
    f"postgresql://{SYNTHETIC_ROLE}:{SYNTHETIC_CREDENTIAL_PART}@{SYNTHETIC_HOST}/molt"
)

# Everything a refusal is checked against for a leak. The credential portion is
# listed on its own as well as inside the connection string, because a refusal that
# carried only that portion would still be a disclosure.
LEAK_NEEDLES: Final[tuple[str, ...]] = (
    SYNTHETIC_BEARER,
    SYNTHETIC_KEYING,
    SYNTHETIC_CONNECTION,
    SYNTHETIC_CREDENTIAL_PART,
)

# The three environment-only names and the values they hold while a refusal is
# produced, in the order the surface declares them.
SURFACE_SECRETS: Final[tuple[tuple[str, str], ...]] = (
    (COLLECTOR_ENV, SYNTHETIC_BEARER),
    (INGRESS_ENV, SYNTHETIC_KEYING),
    (CONNECTION_ENV, SYNTHETIC_CONNECTION),
)

# The keys the refusal's own record carries. Asserting the set exactly is what
# makes the no-leak claim a closure rather than a spot check: a field added to that
# record has to be accounted for here.
REFUSAL_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "severity",
        "component",
        "message",
        "correlation_id",
        "client_id",
        "current_owner",
        "current_generation",
    }
)


# ---------------------------------------------------------------------------
# The stateful cluster
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredLease:
    """One Erasure_Lease row as the fake cluster holds it."""

    lease_id: UUID
    client_id: UUID
    owner: str
    generation: int
    idempotency_key: str
    acquired_at: datetime
    expires_at: datetime
    superseded_by: UUID | None = None

    @property
    def unclosed(self) -> bool:
        """Whether this row is still the tenant's current lease.

        The closure pair is written together, so the successor identifier standing
        absent is the same fact as the supersession instant standing absent.
        """
        return self.superseded_by is None

    @property
    def columns(self) -> tuple[object, ...]:
        """The seven columns every lease statement selects or returns."""
        return (
            self.lease_id,
            self.client_id,
            self.owner,
            self.generation,
            self.idempotency_key,
            self.acquired_at,
            self.expires_at,
        )


@dataclass(frozen=True, slots=True)
class StoredRun:
    """One Erasure_Run row, holding only the columns this module's statements touch."""

    run_id: UUID
    client_id: UUID
    idempotency_key: str | None = None
    lease_id: UUID | None = None
    generation: int | None = None
    finalised_at: datetime | None = None
    result: str | None = None


@dataclass(frozen=True, slots=True)
class Race:
    """A grant that loses a race, and the grant that won it while the loser wrote.

    The winner's row is committed by another transaction at the instant the loser's
    insert is refused, which is what puts the loser in the state the uniqueness path
    exists for: it read no current lease, and by the time it wrote there was one.
    """

    failure: Exception
    winner: StoredLease


class DriverFailureError(Exception):
    """A driver failure carrying the state a driver reports it under.

    The message is a driver's rather than Molt's, and it embeds a connection string
    on purpose: a refusal built from this failure must restate none of it.
    """

    def __init__(self, sqlstate: str, detail: str) -> None:
        super().__init__(detail)
        self.sqlstate = sqlstate


def _uuid(value: object) -> UUID:
    """One bound identifier parameter."""
    assert isinstance(value, UUID), "the statement binds an identifier here"
    return value


def _text(value: object) -> str:
    """One bound text parameter."""
    assert isinstance(value, str), "the statement binds text here"
    return value


def _whole(value: object) -> int:
    """One bound whole-number parameter."""
    assert isinstance(value, int) and not isinstance(value, bool), (
        "the statement binds a whole number here"
    )
    return value


def _anchor(value: object) -> datetime | None:
    """One bound anchor parameter, which the deployed path leaves absent."""
    if value is None:
        return None
    assert isinstance(value, datetime), "the statement binds a timestamp here"
    return value


@dataclass(slots=True)
class Cluster:
    """The rows a fake cluster holds, the reading of its clock, and its statements.

    Two things make this more than a script. Expiry is evaluated against the
    cluster's own reading rather than answered by a canned verdict, so a takeover is
    admitted by advancing this clock rather than by arming a row. And the write
    predicates are evaluated against what is held, so a repeat that must match no
    row matches no row here for the reason the schema gives rather than because a
    test said so.
    """

    reading: datetime = START
    leases: list[StoredLease] = field(default_factory=list)
    runs: dict[UUID, StoredRun] = field(default_factory=dict)
    race: Race | None = None
    sent: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    armed: tuple[tuple[object, ...], ...] = ()
    saved: tuple[list[StoredLease], dict[UUID, StoredRun]] | None = None

    # -- what a test reads afterwards ------------------------------------

    @property
    def statements(self) -> list[str]:
        """Every statement the cluster was sent, in order."""
        return [query for query, _ in self.sent]

    @property
    def issued(self) -> list[str]:
        """What the modules sent, with the pool's own setup and reset removed.

        The pool resets a returned connection with the same literal a transaction
        is abandoned by, so abandonment is removed here alongside it. Nothing below
        reads this for an abandonment: what a refused transaction did not write is
        asserted from the rows the cluster holds afterwards, which is the stronger
        reading anyway.
        """
        return [
            query
            for query in self.statements
            if query not in (STATEMENT_TIMEOUT_STATEMENT, RESET_STATEMENT)
        ]

    def parameters_for(self, statement: str) -> list[tuple[object, ...]]:
        """The bound parameters of every occurrence of one statement, in order."""
        return [bound for query, bound in self.sent if query == statement and bound is not None]

    def current_lease(self, client_id: UUID) -> StoredLease | None:
        """The tenant's current lease, which the partial uniqueness admits one of."""
        for lease in self.leases:
            if lease.client_id == client_id and lease.unclosed:
                return lease
        return None

    def generations_of(self, client_id: UUID) -> list[int]:
        """Every generation the tenant ever held, closed leases included."""
        return [lease.generation for lease in self.leases if lease.client_id == client_id]

    def advance(self, seconds: int) -> None:
        """Move the cluster's clock forward, which is how a window runs out here."""
        self.reading = self.reading + timedelta(seconds=seconds)

    # -- transaction boundaries -------------------------------------------

    def _open(self) -> None:
        self.saved = (list(self.leases), dict(self.runs))

    def _discard(self) -> None:
        if self.saved is not None:
            self.leases, self.runs = list(self.saved[0]), dict(self.saved[1])
        self.saved = None

    def _settle(self) -> None:
        self.saved = None

    def commit_elsewhere(self, lease: StoredLease) -> None:
        """Record a row another transaction committed, which a rollback cannot undo."""
        self.leases.append(lease)
        if self.saved is not None:
            self.saved[0].append(lease)

    # -- the statements ---------------------------------------------------

    def answer(self, query: str, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        """Evaluate one statement against what is held and return the rows it yields.

        The pool's reset of a returned connection is the same literal as an
        abandonment, so both are answered as an abandonment. That is not an
        approximation: a connection comes back with no transaction open, and
        abandoning nothing restores nothing.
        """
        if query in (STATEMENT_TIMEOUT_STATEMENT, SERIALIZABLE_STATEMENT):
            return ()
        if query == BEGIN_STATEMENT:
            self._open()
            return ()
        if query == COMMIT_STATEMENT:
            self._settle()
            return ()
        if query in (ROLLBACK_STATEMENT, RESET_STATEMENT):
            self._discard()
            return ()
        if query == CURRENT_LEASE_QUERY:
            return self._read_current(bound)
        if query == CURRENT_GENERATION_QUERY:
            return self._read_generation(bound)
        if query == HIGHEST_GENERATION_QUERY:
            return self._read_highest(bound)
        if query == INSERT_LEASE_STATEMENT:
            return self._insert(bound)
        if query == CLOSE_LEASE_STATEMENT:
            return self._close(bound)
        if query == RECORD_RUN_KEY_STATEMENT:
            return self._record_key(bound)
        if query == MARK_FINALISED_STATEMENT:
            return self._mark_finalised(bound)
        if query == FINALISATION_QUERY:
            return self._read_finalisation(bound)
        raise AssertionError("the fake cluster holds no behaviour for a statement it was sent")

    def _read_current(self, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        held = self.current_lease(_uuid(bound[0]))
        if held is None:
            return ()
        return ((*held.columns, held.expires_at < self.reading),)

    def _read_generation(self, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        held = self.current_lease(_uuid(bound[0]))
        if held is None:
            return ()
        return ((held.lease_id, held.owner, held.generation),)

    def _read_highest(self, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        floor = _whole(bound[0])
        recorded = self.generations_of(_uuid(bound[1]))
        return ((max(recorded) if recorded else floor,),)

    def _insert(self, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        if self.race is not None:
            race, self.race = self.race, None
            self.commit_elsewhere(race.winner)
            raise race.failure
        client_id = _uuid(bound[1])
        if self.current_lease(client_id) is not None:
            raise DriverFailureError(
                UNIQUE_VIOLATION_STATE,
                "one current lease per client is held unique and this write repeats one",
            )
        anchor = _anchor(bound[5]) or self.reading
        stored = StoredLease(
            lease_id=_uuid(bound[0]),
            client_id=client_id,
            owner=_text(bound[2]),
            generation=_whole(bound[3]),
            idempotency_key=_text(bound[4]),
            acquired_at=anchor,
            expires_at=anchor + timedelta(seconds=_whole(bound[7])),
        )
        self.leases.append(stored)
        return (stored.columns,)

    def _close(self, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        successor, lease_id = _uuid(bound[1]), _uuid(bound[2])
        for index, lease in enumerate(self.leases):
            if lease.lease_id == lease_id and lease.unclosed:
                self.leases[index] = replace(lease, superseded_by=successor)
                return (lease.columns,)
        return ()

    def _record_key(self, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        key, run_id, client_id = _text(bound[0]), _uuid(bound[3]), _uuid(bound[4])
        run = self.runs.get(run_id)
        if run is None or run.client_id != client_id:
            return ()
        if run.idempotency_key is not None and run.idempotency_key != key:
            return ()
        generation = _whole(bound[2])
        self.runs[run_id] = replace(
            run,
            idempotency_key=key,
            lease_id=_uuid(bound[1]),
            generation=generation,
        )
        return ((run_id, key, generation),)

    def _mark_finalised(self, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        run_id, client_id, key = _uuid(bound[2]), _uuid(bound[3]), _text(bound[4])
        run = self.runs.get(run_id)
        if run is None or run.client_id != client_id or run.idempotency_key != key:
            return ()
        if run.finalised_at is not None:
            return ()
        finalised = replace(
            run,
            finalised_at=_anchor(bound[0]) or self.reading,
            result=_text(bound[1]),
        )
        self.runs[run_id] = finalised
        return (_finalisation_columns(finalised),)

    def _read_finalisation(self, bound: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        key = _text(bound[0])
        for run in self.runs.values():
            if run.idempotency_key == key and run.finalised_at is not None:
                return (_finalisation_columns(run),)
        return ()


def _finalisation_columns(run: StoredRun) -> tuple[object, ...]:
    """The five columns a finalisation marking returns and its read selects."""
    return (run.run_id, run.idempotency_key, run.finalised_at, run.result, run.generation)


class ClusterCursor:
    """A cursor sending statements to the fake cluster and holding its answer."""

    def __init__(self, cluster: Cluster) -> None:
        self._cluster = cluster

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, then evaluate it against what the cluster holds."""
        bound = () if params is None else tuple(params)
        self._cluster.sent.append((query, None if params is None else bound))
        self._cluster.armed = ()
        self._cluster.armed = self._cluster.answer(query, bound)
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        """The first row the last statement yielded, or None when it yielded none."""
        rows = self._cluster.armed
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        """Every row the last statement yielded."""
        return list(self._cluster.armed)

    def close(self) -> None:
        """Release this cursor."""


class ClusterConnection:
    """A connection handing out cursors over one fake cluster."""

    def __init__(self, cluster: Cluster) -> None:
        self.cluster = cluster
        self.closed = False

    def cursor(self) -> ClusterCursor:
        """Open a cursor onto this connection's cluster."""
        return ClusterCursor(self.cluster)

    def close(self) -> None:
        """Mark this connection closed."""
        self.closed = True


def build_store(cluster: Cluster) -> MemoryStore:
    """A store whose only connection reaches the fake cluster, with no waiting."""
    connection = ClusterConnection(cluster)

    def connect_with() -> Connection:
        return connection

    return MemoryStore(connect_with=connect_with, sleep=lambda _: None, jitter=lambda low, _: low)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cluster() -> Cluster:
    """A fake cluster holding no rows, its clock at the starting instant."""
    return Cluster()


@pytest.fixture
def store(cluster: Cluster) -> MemoryStore:
    """A store whose only connection reaches that cluster."""
    return build_store(cluster)


@pytest.fixture
def telemetry_sink() -> Iterator[io.StringIO]:
    """Install a process-wide telemetry instance writing to a sink for one test.

    The level is the lowest one, so a record this module claims is absent is absent
    because nothing wrote it rather than because the threshold dropped it.
    """
    sink = io.StringIO()
    configure(Configuration(environ={"MOLT_LOG_LEVEL": "debug"}, file_values={}), stream=sink)
    try:
        yield sink
    finally:
        reset()


@pytest.fixture
def surface_secrets(monkeypatch: pytest.MonkeyPatch) -> Configuration:
    """Put the three environment-only secrets on the surface for one test.

    The lease interval is removed at the same time, so the acquisition resolves it
    from the surface's own default rather than from whatever a developer's
    environment happens to hold. That matters twice: the resolution is what makes
    the refusal a path that genuinely reads the surface the secrets sit on, and the
    removal is what makes the reading the same on every machine.
    """
    for name, value in SURFACE_SECRETS:
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(LEASE_INTERVAL_KEY, raising=False)
    return Configuration()


@pytest.fixture
def owned_run(cluster: Cluster, store: MemoryStore) -> LeaseGrant:
    """A tenant holding erasure, with one run's ownership recorded under that lease."""
    cluster.runs[RUN_ID] = StoredRun(run_id=RUN_ID, client_id=CLIENT_ID)
    granted = history_of(store, cluster, CLIENT_ID, 1)[0]
    assert register_run(store, granted, RUN_ID) == granted.idempotency_key
    return granted


# ---------------------------------------------------------------------------
# Driving the cluster
# ---------------------------------------------------------------------------


def grant_to(store: MemoryStore, client_id: UUID, index: int) -> LeaseGrant:
    """Acquire ownership for one tenant under the index-th worker and its own key."""
    return acquire(store, client_id, OWNERS[index], KEYS[index], interval=WINDOW)


def history_of(
    store: MemoryStore,
    cluster: Cluster,
    client_id: UUID,
    length: int,
) -> list[LeaseGrant]:
    """Drive one tenant through a run of supersessions and report every grant.

    Each successor is admitted by letting the held window run out on the cluster's
    clock, because that is the only thing that makes a lease takeable. So the run is
    a sequence of real transfers rather than a sequence of first grants, and the
    generations it produces are takeover generations.
    """
    granted: list[LeaseGrant] = []
    for index in range(length):
        if cluster.current_lease(client_id) is not None:
            cluster.advance(WINDOW.seconds + 1)
        granted.append(grant_to(store, client_id, index))
    return granted


def stored_history(cluster: Cluster, client_id: UUID) -> list[StoredLease]:
    """Every lease row the cluster holds for one tenant, in the order it wrote them."""
    return [lease for lease in cluster.leases if lease.client_id == client_id]


def refusal_from(
    store: MemoryStore,
    client_id: UUID,
    index: int,
    *,
    interval: LeaseInterval | None = None,
) -> LeaseRefusedError:
    """The refusal a contending acquisition is met with, for the claims below to read.

    The interval defaults to absent, which is the deployed path: the acquisition
    resolves it from the configuration surface before it opens a transaction.
    """
    with pytest.raises(LeaseRefused) as raised:
        acquire(store, client_id, OWNERS[index], KEYS[index], interval=interval)
    return raised.value


# ---------------------------------------------------------------------------
# Reading a failure and a record
# ---------------------------------------------------------------------------


def notes_of(error: BaseException) -> tuple[str, ...]:
    """Every note a failure carries, or none where it carries none."""
    recorded: object = getattr(error, "__notes__", ())
    if isinstance(recorded, Sequence) and not isinstance(recorded, str):
        return tuple(str(note) for note in recorded)
    return ()


def rendering_of(error: BaseException) -> str:
    """Everything a failure says about itself: its text, its arguments, its notes.

    All three together, because a value absent from the message and present in the
    arguments has still reached every reader that prints a traceback.
    """
    return "\n".join((str(error), repr(error.args), *notes_of(error)))


def records_in(sink: io.StringIO) -> list[JsonObject]:
    """Every record the sink holds, decoded from the single-line form."""
    decoded: list[JsonObject] = []
    for line in sink.getvalue().splitlines():
        entry: object = json.loads(line)
        assert isinstance(entry, dict), "a telemetry record is one JSON object per line"
        decoded.append(entry)
    return decoded


def refusal_record(sink: io.StringIO) -> JsonObject:
    """The one record the refusal wrote, which no other act of the lifecycle writes."""
    matching = [
        record for record in records_in(sink) if "refused" in str(record.get("message", ""))
    ]
    assert len(matching) == 1, f"the refusal is recorded once, not {len(matching)} times"
    return matching[0]


# ---------------------------------------------------------------------------
# The generation over a whole history
# ---------------------------------------------------------------------------


def test_a_run_of_supersessions_repeats_no_generation(cluster: Cluster, store: MemoryStore) -> None:
    """Each successor exceeds every generation the tenant ever held, closed ones included.

    Driven rather than asserted on one number: four workers take ownership in turn,
    each after the held window ran out, and what is checked is the whole sequence.
    """
    granted = history_of(store, cluster, CLIENT_ID, len(OWNERS))
    generations = [grant.generation for grant in granted]

    assert generations[0] == FIRST_GENERATION
    assert len(set(generations)) == len(generations), "no generation is repeated"
    for position, grant in enumerate(granted):
        assert all(grant.generation > earlier for earlier in generations[:position])
    assert cluster.generations_of(CLIENT_ID) == generations
    assert all(grant.took_over for grant in granted[1:])


def test_a_takeover_generation_exceeds_the_maximum_of_the_closed_leases(
    cluster: Cluster,
    store: MemoryStore,
) -> None:
    """The maximum a successor is read against spans rows no longer current."""
    granted = history_of(store, cluster, CLIENT_ID, len(OWNERS))
    closed = [
        lease.generation for lease in stored_history(cluster, CLIENT_ID) if not lease.unclosed
    ]

    assert len(closed) == len(OWNERS) - 1
    assert granted[-1].generation == max(closed) + 1
    assert granted[-1].generation > max(closed)


def test_the_closed_leases_chain_to_the_successors_that_replaced_them(
    cluster: Cluster,
    store: MemoryStore,
) -> None:
    """A history is one ordered walk, so a generation cannot be reached by two rows."""
    granted = history_of(store, cluster, CLIENT_ID, len(OWNERS))
    stored = stored_history(cluster, CLIENT_ID)

    assert granted[0].superseded is None
    for earlier, later in pairwise(granted):
        assert later.superseded == earlier.lease_id
    for closed, successor in pairwise(stored):
        assert closed.superseded_by == successor.lease_id
    assert stored[-1].unclosed
    assert not any(lease.unclosed for lease in stored[:-1])


def test_a_long_history_leaves_another_tenants_first_generation_alone(
    cluster: Cluster,
    store: MemoryStore,
) -> None:
    """The maximum is read per tenant, so one tenant's history cannot raise another's."""
    history_of(store, cluster, CLIENT_ID, len(OWNERS))

    first = grant_to(store, OTHER_CLIENT_ID, 0)

    assert first.generation == FIRST_GENERATION
    assert not first.took_over
    assert cluster.generations_of(OTHER_CLIENT_ID) == [FIRST_GENERATION]


def test_another_tenants_history_lowers_no_successor_generation(
    cluster: Cluster,
    store: MemoryStore,
) -> None:
    """Nor can a second tenant's low maximum pull a first tenant's next grant back down."""
    granted = history_of(store, cluster, CLIENT_ID, len(OWNERS))
    grant_to(store, OTHER_CLIENT_ID, 0)
    cluster.advance(WINDOW.seconds + 1)

    successor = acquire(store, CLIENT_ID, OWNERS[0], LATE_KEY, interval=WINDOW)

    assert successor.generation == granted[-1].generation + 1
    assert successor.superseded == granted[-1].lease_id


def test_every_history_read_binds_the_tenant_it_was_asked_about(
    cluster: Cluster,
    store: MemoryStore,
) -> None:
    """A maximum scoped to no tenant would be one tenant's fence handed to another."""
    history_of(store, cluster, CLIENT_ID, 2)
    grant_to(store, OTHER_CLIENT_ID, 0)

    assert cluster.parameters_for(HIGHEST_GENERATION_QUERY) == [
        (NO_GENERATION, CLIENT_ID),
        (NO_GENERATION, CLIENT_ID),
        (NO_GENERATION, OTHER_CLIENT_ID),
    ]


# ---------------------------------------------------------------------------
# What a refusal names, and what it must not
# ---------------------------------------------------------------------------


def test_a_refusal_names_the_current_owner_and_generation(
    cluster: Cluster,
    store: MemoryStore,
) -> None:
    """The loser learns who holds erasure, at which fence, and until when."""
    held = history_of(store, cluster, CLIENT_ID, 1)[0]

    refusal = refusal_from(store, CLIENT_ID, 1, interval=WINDOW)

    assert refusal.owner == held.owner
    assert refusal.generation == held.generation
    rendered = rendering_of(refusal)
    assert held.owner in rendered
    assert str(held.generation) in rendered
    notes = "\n".join(notes_of(refusal))
    assert str(held.lease_id) in notes
    assert held.expires_at.isoformat() in notes
    assert cluster.generations_of(CLIENT_ID) == [held.generation], "the loser wrote nothing"


def test_the_acquisition_resolves_its_interval_from_the_surface(
    cluster: Cluster,
    store: MemoryStore,
    surface_secrets: Configuration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The surface the secrets sit on is read on this path, before any statement is sent.

    This is what puts a secret within reach of a refusal at all, and therefore what
    makes the absence claimed below a claim about something rather than nothing.
    """
    assert surface_secrets.environment_value(CONNECTION_ENV) == SYNTHETIC_CONNECTION
    history_of(store, cluster, CLIENT_ID, 1)
    monkeypatch.setenv(LEASE_INTERVAL_KEY, "0")
    already_sent = len(cluster.statements)

    with pytest.raises(ValueError, match="positive") as raised:
        acquire(store, CLIENT_ID, OWNERS[1], KEYS[1])

    assert len(cluster.statements) == already_sent
    rendered = rendering_of(raised.value)
    for needle in LEAK_NEEDLES:
        assert needle not in rendered


def test_no_configured_secret_reaches_a_refusal(
    cluster: Cluster,
    store: MemoryStore,
    surface_secrets: Configuration,
    telemetry_sink: io.StringIO,
) -> None:
    """A refusal is rendered from the lease facts, so nothing secret-shaped travels with it."""
    assert surface_secrets.environment_value(COLLECTOR_ENV) == SYNTHETIC_BEARER
    assert surface_secrets.environment_value(INGRESS_ENV) == SYNTHETIC_KEYING
    assert surface_secrets.environment_value(CONNECTION_ENV) == SYNTHETIC_CONNECTION
    held = history_of(store, cluster, CLIENT_ID, 1)[0]

    refusal = refusal_from(store, CLIENT_ID, 1)

    rendered = rendering_of(refusal)
    written = telemetry_sink.getvalue()
    assert held.owner in rendered, "the refusal names the winner, so absence below means absence"
    assert held.owner in written
    for needle in LEAK_NEEDLES:
        assert needle not in rendered
        assert needle not in written


def test_the_refusal_record_carries_the_lease_facts_and_nothing_else(
    cluster: Cluster,
    store: MemoryStore,
    surface_secrets: Configuration,
    telemetry_sink: io.StringIO,
) -> None:
    """Asserting the key set exactly is what closes the leak claim over later fields."""
    assert surface_secrets.environment_value(COLLECTOR_ENV) == SYNTHETIC_BEARER
    held = history_of(store, cluster, CLIENT_ID, 1)[0]

    refusal_from(store, CLIENT_ID, 1)

    record = refusal_record(telemetry_sink)
    assert set(record) == REFUSAL_RECORD_KEYS
    assert record["client_id"] == str(CLIENT_ID)
    assert record["current_owner"] == held.owner
    assert record["current_generation"] == held.generation


def test_a_refusal_built_from_a_driver_failure_restates_none_of_its_text(
    cluster: Cluster,
    store: MemoryStore,
    surface_secrets: Configuration,
    telemetry_sink: io.StringIO,
) -> None:
    """A losing race is refused by reading who won, not by repeating what the driver said.

    The driver's own message embeds a connection string, as a driver's message may.
    It is preserved as the cause, where a caller that asked for it finds it, and it
    is restated in neither the refusal nor the record.
    """
    assert surface_secrets.environment_value(CONNECTION_ENV) == SYNTHETIC_CONNECTION
    winner = StoredLease(
        lease_id=uuid4(),
        client_id=CLIENT_ID,
        owner=OWNERS[0],
        generation=FIRST_GENERATION,
        idempotency_key=KEYS[0],
        acquired_at=START,
        expires_at=START + WINDOW.interval,
    )
    cluster.race = Race(
        failure=DriverFailureError(
            UNIQUE_VIOLATION_STATE,
            f"the write to {SYNTHETIC_CONNECTION} repeats a value held unique",
        ),
        winner=winner,
    )

    refusal = refusal_from(store, CLIENT_ID, 1)

    assert refusal.owner == winner.owner
    assert refusal.generation == winner.generation
    rendered = rendering_of(refusal)
    written = telemetry_sink.getvalue()
    for needle in LEAK_NEEDLES:
        assert needle not in rendered
        assert needle not in written
    cause = refusal.__cause__
    assert isinstance(cause, DriverFailureError)
    assert SYNTHETIC_CONNECTION in str(cause), "the driver's own text is preserved as the cause"
    assert cluster.generations_of(CLIENT_ID) == [FIRST_GENERATION], "only the winner wrote"


# ---------------------------------------------------------------------------
# A repeated finalisation
# ---------------------------------------------------------------------------


def test_a_repeat_offering_another_outcome_returns_the_first_field_for_field(
    cluster: Cluster,
    store: MemoryStore,
    owned_run: LeaseGrant,
) -> None:
    """What comes back is the recorded outcome, not the one this call offered."""
    first = finalise(store, owned_run, RUN_ID, FIRST_RESULT)
    cluster.advance(RESTAMP_DELAY)

    repeated = finalise(store, owned_run, RUN_ID, SECOND_RESULT)

    assert repeated == first
    assert repeated.run_id == first.run_id
    assert repeated.idempotency_key == first.idempotency_key
    assert repeated.finalised_at == first.finalised_at
    assert repeated.result == FIRST_RESULT
    assert repeated.generation == owned_run.generation
    assert repeated.finalised_at < cluster.reading, "the recorded instant was not restamped"


def test_the_outcome_a_repeat_offered_is_recorded_nowhere(
    cluster: Cluster,
    store: MemoryStore,
    owned_run: LeaseGrant,
) -> None:
    """The state guard refused the second payload, so it reaches no row and no reader."""
    finalise(store, owned_run, RUN_ID, FIRST_RESULT)

    repeated = finalise(store, owned_run, RUN_ID, SECOND_RESULT)

    stored = cluster.runs[RUN_ID].result
    assert stored is not None
    resumed = finalisation_for(store, owned_run.idempotency_key)
    assert resumed is not None
    assert REFUSED_STATUS not in stored
    assert REFUSED_STATUS not in json.dumps(repeated.result)
    assert REFUSED_STATUS not in json.dumps(resumed.result)
    assert resumed.result == FIRST_RESULT
    assert resumed.finalised_at == repeated.finalised_at
