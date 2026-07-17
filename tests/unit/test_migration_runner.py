"""Unit tests for the migration runner's parsing, digesting, and history rules.

Everything here runs without a cluster. The statements are parsed rather than
executed, the history is a mapping rather than a table, and the savepoint wrapper
is driven against a recording cursor, so the rules that decide whether a run may
proceed are checked with no instance and no credential.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from molt.models.event import EVENT_CATEGORY_VALUES
from molt.models.session import UNASSIGNED_CLIENT_ID, UNASSIGNED_CLIENT_SLUG
from molt.store.migrate import (
    MIGRATIONS_DIRECTORY,
    AppliedMigration,
    MigrationHistoryError,
    MigrationSourceError,
    discover_migrations,
    file_digest,
    load_migration,
    parse_statements,
    permitted_to_fail,
    verify_history,
)


class RecordingCursor:
    """A cursor that records what it was sent and fails on a chosen statement."""

    def __init__(self, failing: str | None = None) -> None:
        self.sent: list[str] = []
        self.failing = failing

    def execute(self, query: str, params: Sequence[object] | None = None) -> object:
        """Record the statement, raising when it is the one chosen to fail."""
        self.sent.append(query)
        if self.failing is not None and self.failing in query:
            raise RuntimeError("the capability is not available on this tier")
        return params

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return nothing; no test here reads rows."""
        return []

    def close(self) -> None:
        """Release nothing."""


# ---------------------------------------------------------------------------
# Statement parsing
# ---------------------------------------------------------------------------


def test_parsing_splits_on_semicolons_and_drops_comments() -> None:
    statements = parse_statements(
        "-- a leading note\nCREATE TABLE a (id INT PRIMARY KEY);\nSELECT 1;\n"
    )
    assert [statement.text for statement in statements] == [
        "CREATE TABLE a (id INT PRIMARY KEY)",
        "SELECT 1",
    ]
    assert [statement.permitted_label for statement in statements] == [None, None]


def test_parsing_ignores_a_semicolon_inside_quoted_text() -> None:
    statements = parse_statements("INSERT INTO a VALUES ('one; two', 'it''s fine');")
    assert len(statements) == 1
    assert statements[0].text == "INSERT INTO a VALUES ('one; two', 'it''s fine')"


def test_parsing_ignores_a_semicolon_inside_a_block_comment() -> None:
    statements = parse_statements("SELECT 1 /* not a boundary; really */ ;")
    assert [statement.text for statement in statements] == ["SELECT 1"]


def test_parsing_reports_the_line_a_statement_begins_on() -> None:
    statements = parse_statements("SELECT 1;\n\n-- a note\nSELECT 2;\n")
    assert [statement.line for statement in statements] == [1, 4]


def test_parsing_reads_the_permitted_marker_and_its_label() -> None:
    statements = parse_statements(
        "SELECT 1;\n-- molt:permit-failure vector_index\nCREATE INDEX i ON a (b);\n"
    )
    assert statements[0].permitted_label is None
    assert statements[0].permitted is False
    assert statements[1].permitted_label == "vector_index"
    assert statements[1].permitted is True


def test_parsing_refuses_two_markers_on_one_statement() -> None:
    with pytest.raises(MigrationSourceError, match="more than one permitted-failure marker"):
        parse_statements("-- molt:permit-failure one\n-- molt:permit-failure two\nSELECT 1;")


def test_parsing_refuses_a_statement_with_no_terminator() -> None:
    with pytest.raises(MigrationSourceError, match="not terminated by a semicolon"):
        parse_statements("SELECT 1;\nSELECT 2\n")


def test_parsing_refuses_unterminated_quoted_text() -> None:
    with pytest.raises(MigrationSourceError, match="unterminated quoted value"):
        parse_statements("SELECT 'open;")


def test_trailing_comments_produce_no_statement() -> None:
    assert parse_statements("SELECT 1;\n-- nothing follows\n") == parse_statements("SELECT 1;")


# ---------------------------------------------------------------------------
# Discovery and digests
# ---------------------------------------------------------------------------


def test_a_digest_depends_on_every_byte_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "001_probe.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    before = file_digest(path)
    path.write_text("SELECT 1;\n-- a comment added later\n", encoding="utf-8")
    assert file_digest(path) != before


def test_discovery_orders_by_version(tmp_path: Path) -> None:
    (tmp_path / "002_second.sql").write_text("SELECT 2;\n", encoding="utf-8")
    (tmp_path / "001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored\n", encoding="utf-8")
    found = discover_migrations(tmp_path)
    assert [migration.version for migration in found] == [1, 2]
    assert [migration.name for migration in found] == ["001_first", "002_second"]


def test_discovery_refuses_a_duplicated_version(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "001_again.sql").write_text("SELECT 2;\n", encoding="utf-8")
    with pytest.raises(MigrationSourceError, match="is claimed by both"):
        discover_migrations(tmp_path)


def test_discovery_refuses_a_file_name_that_carries_no_version(tmp_path: Path) -> None:
    (tmp_path / "core.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationSourceError, match="three-digit version"):
        discover_migrations(tmp_path)


def test_discovery_refuses_an_absent_directory(tmp_path: Path) -> None:
    with pytest.raises(MigrationSourceError, match="does not exist"):
        discover_migrations(tmp_path / "absent")


# ---------------------------------------------------------------------------
# History verification
# ---------------------------------------------------------------------------


def _migration(tmp_path: Path) -> Path:
    path = tmp_path / "001_first.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    return path


def test_a_matching_history_is_accepted(tmp_path: Path) -> None:
    migration = load_migration(_migration(tmp_path))
    recorded = {1: AppliedMigration(1, migration.name, migration.digest)}
    verify_history([migration], recorded)


def test_an_edited_applied_migration_is_refused_with_both_digests(tmp_path: Path) -> None:
    migration = load_migration(_migration(tmp_path))
    recorded = {1: AppliedMigration(1, migration.name, "0" * 64)}
    with pytest.raises(MigrationHistoryError) as caught:
        verify_history([migration], recorded)
    message = str(caught.value)
    assert "0" * 64 in message
    assert migration.digest in message
    assert "rather than re-applied" in message


def test_a_removed_applied_migration_is_refused(tmp_path: Path) -> None:
    migration = load_migration(_migration(tmp_path))
    recorded = {
        1: AppliedMigration(1, migration.name, migration.digest),
        2: AppliedMigration(2, "002_gone", "1" * 64),
    }
    with pytest.raises(MigrationHistoryError, match="was removed"):
        verify_history([migration], recorded)


# ---------------------------------------------------------------------------
# The savepoint wrapper
# ---------------------------------------------------------------------------


def test_the_wrapper_releases_and_reports_success() -> None:
    cursor = RecordingCursor()
    with permitted_to_fail(cursor) as result:
        cursor.execute("CREATE INDEX i ON a (b)")
    assert result.succeeded is True
    assert result.detail == ""
    assert cursor.sent[0].startswith("SAVEPOINT ")
    assert cursor.sent[-1].startswith("RELEASE SAVEPOINT ")


def test_the_wrapper_unwinds_reports_the_failure_and_raises_nothing() -> None:
    cursor = RecordingCursor(failing="CREATE VECTOR INDEX")
    with permitted_to_fail(cursor) as result:
        cursor.execute("CREATE VECTOR INDEX v ON a (b)")
    assert result.succeeded is False
    assert result.detail == "the capability is not available on this tier"
    assert cursor.sent[-1].startswith("ROLLBACK TO SAVEPOINT ")


# ---------------------------------------------------------------------------
# The shipped first migration
# ---------------------------------------------------------------------------


def test_the_first_migration_is_discovered_and_parses() -> None:
    migrations = discover_migrations()
    assert migrations[0].version == 1
    assert migrations[0].name == "001_core"
    assert migrations[0].path.parent == MIGRATIONS_DIRECTORY
    assert migrations[0].body
    assert migrations[0].permitted == ()


def test_the_first_migration_holds_the_category_set_the_model_declares() -> None:
    text = (MIGRATIONS_DIRECTORY / "001_core.sql").read_text(encoding="utf-8")
    start = text.index("CONSTRAINT ledger_category_known")
    end = text.index("CONSTRAINT ledger_embedding_state_known")
    listed = tuple(
        piece.strip().strip("'")
        for piece in text[start:end].split("(", 2)[2].split(")", 1)[0].split(",")
    )
    # The seventeenth category is added to this constraint by the migration that
    # introduces attribution history, so the first migration holds the first
    # sixteen, in the order the model declares them.
    assert listed == EVENT_CATEGORY_VALUES[:16]


def test_the_first_migration_reserves_the_client_the_model_names() -> None:
    text = (MIGRATIONS_DIRECTORY / "001_core.sql").read_text(encoding="utf-8")
    assert f"'{UNASSIGNED_CLIENT_ID}'" in text
    assert f"'{UNASSIGNED_CLIENT_SLUG}'" in text
