"""The Binding_Detector against a live cluster: three kinds, one transaction, one history.

**Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 43.3**

The unit suite drives the detector against a scripted cursor and asserts what it
sent. Five claims cannot be made that way, and each is made here against real
rows.

**The bindings are in the Artifact's transaction.** One transaction writes the
Derived_Artifact and every binding it carries. A failure raised after both leaves
neither the Artifact nor any binding behind, which is what Requirement 12.6 means
in practice: an Artifact nobody can attribute never exists, and an attribution of
an Artifact that was never stored never exists either.

**A differing detection supersedes.** Detecting again with different evidence
leaves the pair holding two versions, one closed naming its successor and one
current, and appends the one Ledger Event a supersession records. Nothing is
edited: the closed version still says what it originally said.

**The maximum-confidence rule operates on the unsuperseded version.** After a
detection has raised a pair to the marker confidence, detecting the same pair
again with weaker but differently-concluded evidence supersedes on the method and
keeps the greater confidence, because the successor's insert takes the greater of
the submitted value and the value the closing statement returned. So the current
version holds the maximum confidence ever submitted for the pair while the history
still records the sequence of methods.

**Inheritance reads the parents' current claims and nothing else.** A parent claim
raised by a supersession is inherited at the raised confidence, and a parent claim
withdrawn by an erasure is not inherited at all, both read inside the child's own
transaction.

**The submission order of the collapsed claims does not change stored state.** The
unit suite asserts that permuting the raw detections collapses to the same claims;
this asserts the other half against the database, that submitting those claims in
either order leaves identical rows. Together they are the whole claim: neither the
order evidence was produced in nor the order claims were written in reaches the
stored state.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from uuid import UUID, uuid4

import pytest

from molt.models.artifact import ArtifactKind, ArtifactRef
from molt.models.binding import BindingMethod
from molt.store import Connection, Cursor, MemoryStore
from molt.store.attribution import (
    AttributionOutcome,
    AttributionSubmission,
    SupersessionContext,
    record_attribution,
    withdraw_attribution,
)
from molt.store.binding_detector import (
    MARKER_CONFIDENCE,
    SCOPE_CONFIDENCE,
    DetectionRequest,
    bindings_for,
    record_bindings,
    write_bindings,
)
from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# The bound parameter form of a search path change, so the schema name is a value
# rather than statement text even in a fixture.
SEARCH_PATH_STATEMENT: Final[str] = "SELECT set_config('search_path', %s, false)"

# The fixture's own writes and reads. The module under test owns no tenant insert,
# no Session insert, and no Artifact insert, and the history is read back column by
# column rather than through the surface that wrote it.
INSERT_CLIENT: Final[str] = (
    "INSERT INTO client (id, slug, display_name, jurisdiction, content_markers) "
    "VALUES (%s, %s, %s, %s, %s)"
)
SET_MARKERS: Final[str] = "UPDATE client SET content_markers = %s WHERE id = %s"
INSERT_SESSION: Final[str] = (
    "INSERT INTO session (id, client_id, agent_cli, machine_id) VALUES (%s, %s, %s, %s)"
)
INSERT_ARTIFACT: Final[str] = (
    "INSERT INTO derived_artifact ("
    "id, kind, owner_client_id, body, content_digest, derivation_method, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
SELECT_VERSIONS: Final[str] = (
    "SELECT client_id, method, confidence, valid_to, superseded_by FROM client_binding "
    "WHERE artifact_id = %s ORDER BY client_id ASC, valid_from ASC"
)
COUNT_ARTIFACTS: Final[str] = "SELECT count(*) FROM derived_artifact WHERE id = %s"
COUNT_VERSIONS: Final[str] = "SELECT count(*) FROM client_binding WHERE artifact_id = %s"
COUNT_SUPERSESSION_EVENTS: Final[str] = "SELECT count(*) FROM ledger WHERE category = %s"

# The Event category a supersession appends, bound as a value rather than written
# into the counting statement.
SUPERSEDED_CATEGORY: Final[str] = "attribution_superseded"

# The command and the machine every Event of this module records.
AGENT_CLI: Final[str] = "molt"
MACHINE_ID: Final[str] = "integration-machine"

# An instant with an offset, derived from the epoch rather than written as a
# literal, so no example depends on when it ran. Every validity instant compared
# below is read back from a stored row.
DETECTED_AT: Final[datetime] = datetime.fromtimestamp(0.0, tz=UTC)
RETENTION: Final[timedelta] = timedelta(days=90)
EXPIRES_AT: Final[datetime] = DETECTED_AT + RETENTION

# A digest-shaped value for the column the schema fixes at sixty-four characters.
DIGEST: Final[str] = "a" * 64

# The derivation and the kind every Artifact of this module carries.
DERIVATION: Final[str] = "summarise"
ARTIFACT_KIND: Final[str] = "summary"
BODY: Final[str] = "the migration for acme-payments failed twice"

# The marker configured for the tenant the text names, and a text in which the
# same marker occurs only inside a longer word.
MARKER: Final[str] = "acme"
JOINED_BODY: Final[str] = "acmecorp released a patch"

# One marker and one text per test that configures markers, because the schema is
# shared across this module and the marker read returns every configured tenant.
# A test whose text could be matched by a marker another test configured would be
# asserting about that test's tenants as well as its own.
RAISED_MARKER: Final[str] = "beacon"
RAISED_BODY: Final[str] = "the beacon rollout stalled halfway"
ORDERED_MARKER: Final[str] = "zephyr"
ORDERED_BODY: Final[str] = "the zephyr ledger reconciled cleanly"

# What a parent's claim carries before and after it is raised. Three distinct
# values, none of them the scope or the marker confidence, so no expectation about
# an inherited value is satisfied by a constant of the module under test.
PARENT_CONFIDENCE: Final[float] = 0.4
RAISED_PARENT_CONFIDENCE: Final[float] = 0.65

# A connection is typed loosely because the driver is reached through a fixture
# rather than imported, which keeps this module collectable with no driver.
DriverConnection = Any


class AbandonTransactionError(Exception):
    """Raised inside a transaction to abandon it after both writes have run."""


@dataclass(frozen=True, slots=True)
class StoredVersion:
    """One stored attribution version, read back column by column."""

    client_id: UUID
    method: str
    confidence: float
    valid_to: datetime | None
    superseded_by: UUID | None

    @property
    def current(self) -> bool:
        """Whether this row is the live claim for its pair."""
        return self.superseded_by is None


def send(connection: DriverConnection, statement: str, params: tuple[object, ...]) -> None:
    """Send one parameterised statement on the fixture's own connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def rows_of(
    connection: DriverConnection,
    statement: str,
    params: tuple[object, ...],
) -> list[tuple[object, ...]]:
    """Read every row of one parameterised statement on the fixture's connection."""
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return list(cursor.fetchall())


def _identifier(value: object) -> UUID:
    """Narrow a stored identifier, refusing anything else."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _moment(value: object) -> datetime:
    """Narrow a stored timestamp, refusing anything else."""
    assert isinstance(value, datetime), "a validity column holds an instant"
    return value


@dataclass(frozen=True, slots=True)
class Cluster:
    """A schema holding every migration, a store over it, and the fixture's writes."""

    store: MemoryStore
    connection: DriverConnection

    def tenant(self, *, markers: tuple[str, ...] = ()) -> UUID:
        """Place one Client with its configured content markers."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_CLIENT,
            (identifier, f"tenant-{identifier.hex[:10]}", "Tenant", "eu", list(markers)),
        )
        return identifier

    def set_markers(self, client_id: UUID, markers: tuple[str, ...]) -> None:
        """Reconfigure one Client's content markers."""
        send(self.connection, SET_MARKERS, (list(markers), client_id))

    def session(self, client_id: UUID) -> UUID:
        """Place one Session, which every Event of a supersession belongs to."""
        identifier = uuid4()
        send(self.connection, INSERT_SESSION, (identifier, client_id, AGENT_CLI, MACHINE_ID))
        return identifier

    def artifact(self, owner: UUID, *, body: str = BODY) -> UUID:
        """Place one Derived_Artifact directly and return its identifier."""
        identifier = uuid4()
        send(
            self.connection,
            INSERT_ARTIFACT,
            (identifier, ARTIFACT_KIND, owner, body, DIGEST, DERIVATION, EXPIRES_AT),
        )
        return identifier

    def context(self, session_id: UUID) -> SupersessionContext:
        """The Session context a supersession Event is recorded within."""
        return SupersessionContext(
            session_id=session_id,
            agent_cli=AGENT_CLI,
            machine_id=MACHINE_ID,
            expires_at=EXPIRES_AT,
        )

    def versions(self, artifact_id: UUID) -> tuple[StoredVersion, ...]:
        """Every stored version of one Artifact, grouped by Client and oldest first."""
        return tuple(
            StoredVersion(
                client_id=_identifier(row[0]),
                method=str(row[1]),
                confidence=float(str(row[2])),
                valid_to=None if row[3] is None else _moment(row[3]),
                superseded_by=None if row[4] is None else _identifier(row[4]),
            )
            for row in rows_of(self.connection, SELECT_VERSIONS, (artifact_id,))
        )

    def current(self, artifact_id: UUID) -> dict[UUID, StoredVersion]:
        """The live claim per Client for one Artifact."""
        return {
            version.client_id: version for version in self.versions(artifact_id) if version.current
        }

    def count(self, statement: str, params: tuple[object, ...]) -> int:
        """Read one count on the fixture's own connection."""
        rows = rows_of(self.connection, statement, params)
        assert rows, "a count statement produced no row"
        return int(str(rows[0][0]))

    def supersession_events(self) -> int:
        """How many supersession Events the whole schema holds."""
        return self.count(COUNT_SUPERSESSION_EVENTS, (SUPERSEDED_CATEGORY,))


@pytest.fixture(scope="module")
def cluster(
    fresh_schema: DriverConnection,
    database_driver: ModuleType,
    local_instance_dsn: str,
) -> Iterator[Cluster]:
    """Apply every migration, then build a store whose connections see that schema."""
    apply_migrations(fresh_schema)

    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        assert row is not None
        schema = str(row[0])

    def connect_with() -> Connection:
        opened = database_driver.connect(local_instance_dsn, autocommit=True)
        with opened.cursor() as cursor:
            cursor.execute(SEARCH_PATH_STATEMENT, (schema,))
        connection: Connection = opened
        return connection

    with MemoryStore(connect_with=connect_with) as store:
        yield Cluster(store=store, connection=fresh_schema)


def reference(identifier: UUID, owner: UUID) -> ArtifactRef:
    """A reference to one Derived_Artifact of this module."""
    return ArtifactRef(id=identifier, kind=ArtifactKind.DERIVED_ARTIFACT, client_id=owner)


def parent_claim(
    cluster: Cluster,
    parent_id: UUID,
    client_id: UUID,
    *,
    confidence: float,
    context: SupersessionContext,
) -> None:
    """Give one parent Artifact a current claim through the shipped write path."""

    def body(cursor: Cursor) -> None:
        record_attribution(
            cursor,
            AttributionSubmission(
                artifact_id=parent_id,
                artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                client_id=client_id,
                method=BindingMethod.SCOPE,
                confidence=confidence,
                detected_at=DETECTED_AT,
            ),
            context=context,
        )

    cluster.store.in_serializable(body)


# ---------------------------------------------------------------------------
# The Artifact's transaction
# ---------------------------------------------------------------------------


def test_the_three_kinds_land_in_the_artifacts_own_transaction(cluster: Cluster) -> None:
    """One transaction writes the Artifact and every binding, or writes neither.

    The evidence is arranged so that all three kinds occur at once: the Session's
    Client owns the Artifact, a second Client has a marker the text names, and a
    third holds a current claim on the parent.
    """
    owner = cluster.tenant()
    marker_owner = cluster.tenant(markers=(MARKER,))
    parent_owner = cluster.tenant()
    context = cluster.context(cluster.session(owner))
    parent_id = cluster.artifact(parent_owner)
    parent_claim(
        cluster,
        parent_id,
        parent_owner,
        confidence=PARENT_CONFIDENCE,
        context=cluster.context(cluster.session(parent_owner)),
    )

    abandoned = uuid4()
    request = DetectionRequest(
        artifact=reference(abandoned, owner),
        scope_client_id=owner,
        text=BODY,
        parents=(reference(parent_id, parent_owner),),
    )

    def failing(cursor: Cursor) -> None:
        cursor.execute(
            INSERT_ARTIFACT,
            (abandoned, ARTIFACT_KIND, owner, BODY, DIGEST, DERIVATION, EXPIRES_AT),
        )
        written = write_bindings(cursor, request, context=context, detected_at=DETECTED_AT)
        assert len(written) == 3
        raise AbandonTransactionError

    with pytest.raises(AbandonTransactionError):
        cluster.store.in_serializable(failing)

    assert cluster.count(COUNT_ARTIFACTS, (abandoned,)) == 0, "the Artifact survived its rollback"
    assert cluster.count(COUNT_VERSIONS, (abandoned,)) == 0, (
        "a binding outlived the Artifact's transaction, so it was not written inside it"
    )

    committed = uuid4()
    kept = DetectionRequest(
        artifact=reference(committed, owner),
        scope_client_id=owner,
        text=BODY,
        parents=(reference(parent_id, parent_owner),),
    )

    def succeeding(cursor: Cursor) -> None:
        cursor.execute(
            INSERT_ARTIFACT,
            (committed, ARTIFACT_KIND, owner, BODY, DIGEST, DERIVATION, EXPIRES_AT),
        )
        write_bindings(cursor, kept, context=context, detected_at=DETECTED_AT)

    cluster.store.in_serializable(succeeding)

    assert cluster.count(COUNT_ARTIFACTS, (committed,)) == 1
    live = cluster.current(committed)
    assert {client: version.method for client, version in live.items()} == {
        owner: BindingMethod.SCOPE.value,
        marker_owner: BindingMethod.MARKER.value,
        parent_owner: BindingMethod.INHERITED.value,
    }
    assert live[owner].confidence == pytest.approx(SCOPE_CONFIDENCE)
    assert live[marker_owner].confidence == pytest.approx(MARKER_CONFIDENCE)
    assert live[parent_owner].confidence == pytest.approx(PARENT_CONFIDENCE)


def test_a_marker_inside_a_longer_word_binds_nothing(cluster: Cluster) -> None:
    """A Client's marker occurring inside another word is not that Client's data.

    Admitting it would let one tenant's recall reach another tenant's content and
    would put somebody else's rows inside this tenant's erasure scope, and nothing
    downstream would notice, because a binding is exactly the evidence both paths
    trust.
    """
    owner = cluster.tenant()
    cluster.tenant(markers=(MARKER,))
    context = cluster.context(cluster.session(owner))
    artifact_id = cluster.artifact(owner, body=JOINED_BODY)

    record_bindings(
        cluster.store,
        DetectionRequest(
            artifact=reference(artifact_id, owner),
            scope_client_id=owner,
            text=JOINED_BODY,
        ),
        context=context,
        detected_at=DETECTED_AT,
    )

    assert set(cluster.current(artifact_id)) == {owner}


# ---------------------------------------------------------------------------
# Supersession rather than overwrite
# ---------------------------------------------------------------------------


def test_a_differing_detection_supersedes_and_keeps_the_greater_confidence(
    cluster: Cluster,
) -> None:
    """The stored claim is closed and a successor written, and the maximum is kept.

    Three detections over one pair. The first inherits the parent's claim. The
    second runs once the Client has configured a marker the text names, so the
    method and the confidence both differ and the pair is superseded. The third
    runs with the marker removed, so the method differs again while the submitted
    confidence is lower: the successor carries the greater of the submitted value
    and the value the closing statement returned, which is what makes the
    maximum-confidence rule a fact about the unsuperseded version rather than about
    what this process last observed.
    """
    owner = cluster.tenant()
    subject = cluster.tenant()
    context = cluster.context(cluster.session(owner))
    parent_id = cluster.artifact(subject)
    parent_claim(
        cluster,
        parent_id,
        subject,
        confidence=PARENT_CONFIDENCE,
        context=cluster.context(cluster.session(subject)),
    )
    artifact_id = cluster.artifact(owner, body=RAISED_BODY)
    request = DetectionRequest(
        artifact=reference(artifact_id, owner),
        scope_client_id=owner,
        text=RAISED_BODY,
        parents=(reference(parent_id, subject),),
    )
    events_before = cluster.supersession_events()

    first = record_bindings(cluster.store, request, context=context, detected_at=DETECTED_AT)
    assert {item.write.outcome for item in first} == {AttributionOutcome.INSERTED}
    assert cluster.current(artifact_id)[subject].method == BindingMethod.INHERITED.value

    cluster.set_markers(subject, (RAISED_MARKER,))
    second = record_bindings(cluster.store, request, context=context, detected_at=DETECTED_AT)
    raised = cluster.current(artifact_id)[subject]
    assert raised.method == BindingMethod.MARKER.value
    assert raised.confidence == pytest.approx(MARKER_CONFIDENCE)
    assert AttributionOutcome.SUPERSEDED in {item.write.outcome for item in second}

    cluster.set_markers(subject, ())
    record_bindings(cluster.store, request, context=context, detected_at=DETECTED_AT)
    held = cluster.current(artifact_id)[subject]
    assert held.method == BindingMethod.INHERITED.value
    assert held.confidence == pytest.approx(MARKER_CONFIDENCE), (
        "the successor did not carry the greater of the submitted confidence and the "
        "confidence the closed version held, so the maximum-confidence rule was not "
        "evaluated against the unsuperseded version"
    )

    history = [version for version in cluster.versions(artifact_id) if version.client_id == subject]
    assert len(history) == 3, "a detection edited a stored version instead of superseding it"
    assert [version.current for version in history] == [False, False, True]
    assert [version.method for version in history] == [
        BindingMethod.INHERITED.value,
        BindingMethod.MARKER.value,
        BindingMethod.INHERITED.value,
    ]
    assert all(version.valid_to is not None for version in history[:-1])
    assert cluster.supersession_events() == events_before + 2, (
        "a supersession was silent, so the attribution history is not in the Ledger"
    )


# ---------------------------------------------------------------------------
# Inheritance reads the current claims
# ---------------------------------------------------------------------------


def test_inheritance_reads_the_parents_current_claims(cluster: Cluster) -> None:
    """A raised parent claim is inherited at the raised value; a withdrawn one is not.

    Both readings come from the current-attribution form inside the child's own
    transaction, so what a child inherits is what its parents hold at the instant
    the child is written rather than what they once held.
    """
    owner = cluster.tenant()
    subject = cluster.tenant()
    context = cluster.context(cluster.session(owner))
    parent_context = cluster.context(cluster.session(subject))
    parent_id = cluster.artifact(subject)
    parent_claim(cluster, parent_id, subject, confidence=PARENT_CONFIDENCE, context=parent_context)

    def detected(child_id: UUID) -> dict[UUID, StoredVersion]:
        record_bindings(
            cluster.store,
            DetectionRequest(
                artifact=reference(child_id, owner),
                scope_client_id=owner,
                parents=(reference(parent_id, subject),),
            ),
            context=context,
            detected_at=DETECTED_AT,
        )
        return cluster.current(child_id)

    first_child = cluster.artifact(owner)
    assert detected(first_child)[subject].confidence == pytest.approx(PARENT_CONFIDENCE)

    parent_claim(
        cluster, parent_id, subject, confidence=RAISED_PARENT_CONFIDENCE, context=parent_context
    )
    second_child = cluster.artifact(owner)
    assert detected(second_child)[subject].confidence == pytest.approx(RAISED_PARENT_CONFIDENCE)

    def withdraw(cursor: Cursor) -> None:
        withdraw_attribution(cursor, parent_id, subject, context=parent_context)

    cluster.store.in_serializable(withdraw)

    third_child = cluster.artifact(owner)
    assert set(detected(third_child)) == {owner}, (
        "a withdrawn parent claim was inherited, so the inheritance read is not the "
        "current-attribution form"
    )


# ---------------------------------------------------------------------------
# Order independence, against stored rows
# ---------------------------------------------------------------------------


def test_the_submission_order_of_the_collapsed_claims_leaves_identical_rows(
    cluster: Cluster,
) -> None:
    """Two Artifacts, the same evidence, the claims submitted in opposite orders.

    The unit suite asserts the first half of this claim, that permuting the raw
    detections collapses to the same claims. This is the second half: the collapsed
    claims are one submission per pair, so writing them in either order leaves the
    same stored method and the same stored confidence for every Client.
    """
    owner = cluster.tenant()
    marker_owner = cluster.tenant(markers=(ORDERED_MARKER,))
    context = cluster.context(cluster.session(owner))
    parent_id = cluster.artifact(marker_owner)
    parent_claim(
        cluster,
        parent_id,
        marker_owner,
        confidence=PARENT_CONFIDENCE,
        context=cluster.context(cluster.session(marker_owner)),
    )

    def stored(child_id: UUID, *, reverse: bool) -> dict[UUID, tuple[str, float]]:
        request = DetectionRequest(
            artifact=reference(child_id, owner),
            scope_client_id=owner,
            text=ORDERED_BODY,
            parents=(reference(parent_id, marker_owner),),
        )

        def body(cursor: Cursor) -> None:
            detections = bindings_for(cursor, request)
            assert len(detections) == 2
            ordered = tuple(reversed(detections)) if reverse else detections
            for detection in ordered:
                record_attribution(
                    cursor,
                    AttributionSubmission(
                        artifact_id=child_id,
                        artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                        client_id=detection.client_id,
                        method=detection.method,
                        confidence=detection.confidence,
                        detected_at=DETECTED_AT,
                    ),
                    context=context,
                )

        cluster.store.in_serializable(body)
        return {
            client: (version.method, version.confidence)
            for client, version in cluster.current(child_id).items()
        }

    forward = stored(cluster.artifact(owner, body=ORDERED_BODY), reverse=False)
    backward = stored(cluster.artifact(owner, body=ORDERED_BODY), reverse=True)

    assert forward == backward
    assert set(forward) == {owner, marker_owner}
