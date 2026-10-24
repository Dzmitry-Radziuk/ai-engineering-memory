"""Regression fixtures for additive upgrades from legacy schema versions."""

from __future__ import annotations

import sqlite3

import pytest

from ai_engineering_memory import storage
from ai_engineering_memory.settings import Settings


def _write_legacy_fixture(path, version: int) -> None:
    """Creates an isolated historical schema shape and one real knowledge row."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute(
            "INSERT INTO knowledge(id,category,title,content,status) "
            "VALUES(71,'backend','Migration preserved','Keep the existing integer ID','approved')"
        )
        connection.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES(?, 'legacy-fixture')", (version,),
        )

        if version >= 2:
            connection.execute(
                """CREATE TABLE task_events (
                    event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, client_name TEXT NOT NULL,
                    task_description TEXT NOT NULL, changed_files TEXT NOT NULL,
                    test_results TEXT NOT NULL, outcome TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_analysis', analysis TEXT
                )"""
            )
            connection.execute(
                "INSERT INTO task_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-event", "project:migration", "legacy-task", "Codex",
                    "Existing event", "[]", "{}", "finished",
                    "2026-10-01T00:00:00Z", "pending_analysis", None,
                ),
            )

        if version >= 3:
            connection.execute("ALTER TABLE task_events ADD COLUMN verification_id TEXT")
            connection.execute(
                """CREATE TABLE verification_records (
                    verification_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, verification_type TEXT NOT NULL,
                    exit_code INTEGER NOT NULL, started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL, artifact_digest TEXT NOT NULL,
                    command_digest TEXT NOT NULL, verifier TEXT NOT NULL,
                    trust_status TEXT NOT NULL
                )"""
            )

        if version >= 4:
            connection.execute("ALTER TABLE task_events ADD COLUMN project_fingerprint TEXT")
            connection.execute(
                "ALTER TABLE verification_records ADD COLUMN project_fingerprint TEXT"
            )


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_legacy_schema_migration_retains_ids_and_is_repeatable(tmp_path, version):
    """Каждая старая схема доходит до latest без потери строк и повторных audit."""
    database_path = tmp_path / f"schema-v{version}.sqlite"
    _write_legacy_fixture(database_path, version)
    store = storage.create_storage(Settings(database_path))

    with store.activate():
        store.init_database()
        with storage._connect() as connection:
            first_audit_count = connection.execute(
                "SELECT COUNT(*) FROM learning_event_audit"
            ).fetchone()[0]
        store.init_database()
        with storage._connect() as connection:
            latest = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            migration_count = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        knowledge = store.get_knowledge(71)
        event = store.get_task_status("legacy-event")

    assert latest == storage.LATEST_SCHEMA_VERSION
    assert migration_count == storage.LATEST_SCHEMA_VERSION
    assert integrity == "ok"
    assert knowledge["id"] == 71
    assert knowledge["content"] == "Keep the existing integer ID"
    if version == 1:
        assert event is None
        assert first_audit_count == 0
    else:
        assert event["status"] == "waiting_for_verification"
        assert first_audit_count == 1
        with store.activate(), storage._connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM learning_event_audit"
            ).fetchone()[0] == first_audit_count


def test_failed_migration_step_rolls_back_schema_and_data(tmp_path):
    """Constraint failure cannot leave half-applied DDL or alter legacy rows."""
    database_path = tmp_path / "rollback.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE knowledge (
                id INTEGER PRIMARY KEY,
                uuid TEXT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        connection.executemany(
            "INSERT INTO knowledge VALUES(?,?,?,?,?,?,?)",
            [
                (10, "duplicate", "backend", "One", "First", "candidate", "t1"),
                (11, "duplicate", "backend", "Two", "Second", "candidate", "t2"),
            ],
        )
    store = storage.create_storage(Settings(database_path))

    with store.activate(), pytest.raises(sqlite3.IntegrityError):
        store.init_database()

    with store.activate(), storage._connect() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(knowledge)")
        }
        rows = connection.execute(
            "SELECT id,title FROM knowledge ORDER BY id"
        ).fetchall()
        versions = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    assert "updated_at" not in columns
    assert [(row["id"], row["title"]) for row in rows] == [(10, "One"), (11, "Two")]
    assert versions == 0
