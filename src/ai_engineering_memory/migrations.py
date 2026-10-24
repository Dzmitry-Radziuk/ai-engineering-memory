""Repeatable, additive SQLite schema migrations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

LATEST_SCHEMA_VERSION = 5

_REQUIRED_OBJECTS = {
    "knowledge",
    "evidence",
    "task_events",
    "verification_records",
    "learning_event_audit",
    "learning_corroborations",
    "skill_proposals",
    "skill_versions",
    "schema_migrations",
    "idx_knowledge_uuid",
    "idx_knowledge_scope",
    "idx_knowledge_scope_category",
}
_REQUIRED_COLUMNS = {
    "knowledge": {
        "id", "uuid", "category", "title", "content", "status",
        "source_type", "technology", "confidence", "namespace",
        "updated_at", "approved_at", "publishable", "content_hash",
    },
    "task_events": {
        "event_id", "project_id", "task_id", "status", "verification_id",
        "attempt_count", "locked_at", "next_attempt_at", "last_error_type",
        "analyzed_at", "project_fingerprint",
    },
    "verification_records": {
        "verification_id", "project_id", "task_id", "verification_type",
        "exit_code", "started_at", "completed_at", "artifact_digest",
        "command_digest", "verifier", "trust_status", "project_fingerprint",
    },
    "learning_corroborations": {
        "global_knowledge_id", "project_knowledge_id", "project_namespace",
        "event_id", "verification_id", "created_at", "project_fingerprint",
    },
}


def fts_unavailable(error: sqlite3.OperationalError) -> bool:
    """Return True only for known absence of SQLite FTS5 support or objects."""
    message = str(error).casefold()
    return any(
        item in message
        for item in (
            "no such module: fts5",
            "no such table: knowledge_fts",
            "no such table: main.knowledge_fts",
            "no such function: bm25",
        )
    )


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    additions: dict[str, str],
) -> None:
    existing = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    for name, definition in additions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    _ensure_columns(connection, "knowledge", {
        "uuid": "TEXT",
        "source_type": "TEXT NOT NULL DEFAULT 'manual'",
        "source_reference": "TEXT",
        "technology": "TEXT NOT NULL DEFAULT ''",
        "confidence": "REAL NOT NULL DEFAULT 0.5",
        "namespace": "TEXT NOT NULL DEFAULT 'global'",
        "updated_at": "TEXT",
        "approved_at": "TEXT",
        "publishable": "INTEGER NOT NULL DEFAULT 0",
        "content_hash": "TEXT",
    })
    for row in connection.execute("SELECT id FROM knowledge WHERE uuid IS NULL"):
        connection.execute(
            "UPDATE knowledge SET uuid=? WHERE id=?", (str(uuid.uuid4()), row["id"]),
        )
    connection.execute("UPDATE knowledge SET updated_at=created_at WHERE updated_at IS NULL")
    for row in connection.execute(
        "SELECT id,category,title,content FROM knowledge WHERE content_hash IS NULL"
    ):
        digest = hashlib.sha256(
            (row["category"].lower() + "\0" + row["title"].lower() + "\0"
             + row["content"].lower()).encode()
        ).hexdigest()
        connection.execute(
            "UPDATE knowledge SET content_hash=? WHERE id=?", (digest, row["id"]),
        )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_uuid ON knowledge(uuid)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_scope ON knowledge(namespace,status)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, result TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL, limitations TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL
        )"""
    )


def _migration_2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS task_events (
            event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
            client_name TEXT NOT NULL, task_description TEXT NOT NULL,
            changed_files TEXT NOT NULL, test_results TEXT NOT NULL,
            outcome TEXT NOT NULL, completed_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_analysis', analysis TEXT
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS learning_event_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            knowledge_id INTEGER, action TEXT NOT NULL, status_from TEXT,
            status_to TEXT, occurred_at TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS verification_records (
            verification_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            task_id TEXT NOT NULL, verification_type TEXT NOT NULL,
            exit_code INTEGER NOT NULL, started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL, artifact_digest TEXT NOT NULL,
            command_digest TEXT NOT NULL, verifier TEXT NOT NULL,
            trust_status TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS learning_corroborations (
            global_knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
            project_knowledge_id INTEGER REFERENCES knowledge(id) ON DELETE SET NULL,
            project_namespace TEXT NOT NULL,
            event_id TEXT NOT NULL REFERENCES task_events(event_id) ON DELETE CASCADE,
            verification_id TEXT NOT NULL REFERENCES verification_records(verification_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(global_knowledge_id,project_namespace)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS skill_proposals (
            id TEXT PRIMARY KEY, skill_name TEXT NOT NULL, proposer TEXT NOT NULL,
            change_description TEXT NOT NULL, proposed_content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL,
            decided_at TEXT, decision_by TEXT, decision_reason TEXT
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS skill_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, skill_name TEXT NOT NULL,
            version TEXT NOT NULL, content TEXT NOT NULL, changelog TEXT NOT NULL,
            previous_version TEXT, proposal_id TEXT, validation_result TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(skill_name,version)
        )"""
    )


def _migration_3(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "task_events", {
        "verification_id": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "locked_at": "TEXT",
        "next_attempt_at": "TEXT",
        "last_error_type": "TEXT",
        "analyzed_at": "TEXT",
    })


def _migration_4(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "task_events", {"project_fingerprint": "TEXT"})
    _ensure_columns(connection, "verification_records", {"project_fingerprint": "TEXT"})
    _ensure_columns(connection, "learning_corroborations", {"project_fingerprint": "TEXT"})
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_corroboration_project_fingerprint
        ON learning_corroborations(global_knowledge_id,project_fingerprint)
        WHERE project_fingerprint IS NOT NULL"""
    )
    legacy = connection.execute(
        "SELECT event_id,status FROM task_events WHERE verification_id IS NULL "
        "AND status IN ('pending_analysis','processing','retryable_error','permanent_error')"
    ).fetchall()
    applied_at = datetime.now(UTC).isoformat()
    for row in legacy:
        connection.execute(
            """INSERT INTO learning_event_audit
            (event_id,action,status_from,status_to,occurred_at,details)
            VALUES(?,?,?,?,?,?)""",
            (row["event_id"], "legacy_event_requires_verification", row["status"],
             "waiting_for_verification", applied_at,
             json.dumps({"reason": "no_trusted_verification_record"})),
        )
    connection.execute(
        """UPDATE task_events SET status='waiting_for_verification',locked_at=NULL
        WHERE verification_id IS NULL AND status IN
        ('pending_analysis','processing','retryable_error','permanent_error')"""
    )


def _migration_5(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_scope_category "
        "ON knowledge(namespace,status,category)"
    )
    trigger_names = {"knowledge_ai", "knowledge_ad", "knowledge_au"}
    existing = {
        row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name='knowledge_fts' "
            "OR (type='trigger' AND name IN ('knowledge_ai','knowledge_ad','knowledge_au'))"
        )
    }
    rebuild = not trigger_names.issubset(existing) or "knowledge_fts" not in existing
    try:
        connection.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                title,content,category,technology,
                content='knowledge',content_rowid='id'
            )"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
            INSERT INTO knowledge_fts(rowid,title,content,category,technology)
            VALUES(new.id,new.title,new.content,new.category,new.technology); END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts,rowid,title,content,category,technology)
            VALUES('delete',old.id,old.title,old.content,old.category,old.technology); END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts,rowid,title,content,category,technology)
            VALUES('delete',old.id,old.title,old.content,old.category,old.technology);
            INSERT INTO knowledge_fts(rowid,title,content,category,technology)
            VALUES(new.id,new.title,new.content,new.category,new.technology); END"""
        )
        if rebuild:
            connection.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')")
    except sqlite3.OperationalError as exc:
        if not fts_unavailable(exc):
            raise


_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_1),
    (2, _migration_2),
    (3, _migration_3),
    (4, _migration_4),
    (5, _migration_5),
)


class MigrationEngine:
    """Applies additive schema migrations, each in its own transaction."""

    def is_current(self, connection: sqlite3.Connection) -> bool:
        """Checks both recorded version and the columns required by the current code."""
        objects = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        if "schema_migrations" not in objects:
            return False
        version = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
        ).fetchone()[0]
        if version < LATEST_SCHEMA_VERSION or not _REQUIRED_OBJECTS.issubset(objects):
            return False
        for table, required_columns in _REQUIRED_COLUMNS.items():
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not required_columns.issubset(columns):
                return False
        return True

    def apply(self, connection: sqlite3.Connection) -> bool:
        """Migrates an old/partial schema; returns False for a verified hot-path no-op."""
        if self.is_current(connection):
            return False
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version, migrate in _MIGRATIONS:
            connection.execute("BEGIN IMMEDIATE")
            try:
                migrate(connection)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                    (version, datetime.now(UTC).isoformat()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True
