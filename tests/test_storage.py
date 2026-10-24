"""Тесты локального хранилища на изолированных временных базах."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

import pytest

from ai_engineering_memory import learning, storage


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Перенаправляет хранилище на одноразовую базу теста."""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "test_memory.db")
    monkeypatch.setenv("AI_MEMORY_DB", str(storage.DATABASE_PATH))
    return storage.DATABASE_PATH


def _disable_fts(monkeypatch):
    """Заменяет FTS-запросы ошибкой, сохраняя обычный доступ к временной SQLite-базе."""
    original_connect = storage._connect

    class FtsUnavailableConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if "knowledge_fts MATCH" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def connect_without_fts(*, read_only=False, timeout=10.0):
        with original_connect(read_only=read_only, timeout=timeout) as connection:
            yield FtsUnavailableConnection(connection)

    monkeypatch.setattr(storage, "_connect", connect_without_fts)


def test_legacy_api_and_safe_migration(isolated_db):
    """Сохраняет старый интерфейс и добавляет новую схему без потери строки."""
    with sqlite3.connect(isolated_db) as connection:
        connection.execute(
            "CREATE TABLE knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO knowledge(category,title,content) VALUES('ops','legacy','preserved')"
        )
    storage.init_database()
    legacy = storage.get_knowledge(1)
    assert legacy["content"] == "preserved"
    assert legacy["uuid"]
    new_id = storage.add_knowledge("ops", "New", "new content")
    assert new_id == 2
    assert len(storage.get_knowledge()) == 2


def test_database_init_does_not_rebuild_existing_fts_index(isolated_db, monkeypatch):
    """Повторный запуск init сохраняет FTS-индекс без полного перестроения."""
    storage.init_database()
    with storage._connect() as connection:
        fts_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='knowledge_fts'"
        ).fetchone()
    if fts_exists is None:
        pytest.skip("SQLite в этой среде собрана без FTS5")

    statements = []
    original_connect = storage._connect

    class TrackingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            statements.append(sql)
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def tracking_connect(*, read_only=False, timeout=10.0):
        with original_connect(read_only=read_only, timeout=timeout) as connection:
            yield TrackingConnection(connection)

    monkeypatch.setattr(storage, "_connect", tracking_connect)
    storage.init_database()

    assert not any("VALUES ('rebuild')" in sql for sql in statements)


def test_database_init_skips_schema_work_after_latest_migration(
    isolated_db, monkeypatch
):
    """Актуальная схема пропускает повторный запуск таблиц, индексов и FTS."""
    storage.init_database()
    statements = []
    original_connect = storage._connect

    class TrackingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            statements.append(sql)
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def tracking_connect(*, read_only=False, timeout=10.0):
        with original_connect(read_only=read_only, timeout=timeout) as connection:
            yield TrackingConnection(connection)

    monkeypatch.setattr(storage, "_connect", tracking_connect)
    storage.init_database()

    assert len(statements) == 6
    assert "sqlite_master" in statements[0]
    assert "MAX(version)" in statements[1]
    assert all(
        statement.startswith("PRAGMA table_info") for statement in statements[2:]
    )


def test_search_index_rebuild_restores_fts_contents(isolated_db):
    """Ручное восстановление FTS строит индекс из канонических данных."""
    storage.init_database()
    with storage._connect() as connection:
        fts_available = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='knowledge_fts'"
        ).fetchone()
    if fts_available is None:
        pytest.skip("SQLite в этой среде собрана без FTS5")

    record = storage.create_knowledge(
        "backend", "Index fixture", "Needle for explicit index rebuild"
    )
    with storage._connect() as connection:
        connection.execute("DELETE FROM knowledge_fts")

    result = storage.rebuild_search_index()

    assert result["status"] == "rebuilt"
    assert storage.get_knowledge(record["id"])["title"] == "Index fixture"
    assert storage.search_knowledge("Needle", status="candidate", namespace="global")


def test_search_index_status_does_not_initialize_database(isolated_db):
    """Статус поискового backend не меняет legacy или отсутствующую базу."""
    result = storage.search_index_status()

    assert result["status"] == "database_missing"
    assert not isolated_db.exists()


def test_schema_migration_six_adds_namespace_index_without_losing_data(isolated_db):
    """Обновляет v5 до v6 без удаления существующих записей."""
    storage.init_database()
    item = storage.create_knowledge(
        "backend",
        "Preserved",
        "Existing content",
        namespace="project:test",
    )
    with storage._connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=6")
        connection.execute("DROP INDEX idx_knowledge_namespace_id")

    storage.init_database()

    with storage._connect() as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        index = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_knowledge_namespace_id'"
        ).fetchone()
    assert version == storage.LATEST_SCHEMA_VERSION == 6
    assert index is not None
    assert storage.get_knowledge(item["id"])["content"] == "Existing content"


def test_namespace_listing_uses_recent_namespace_index(isolated_db):
    """Запрос последних записей namespace использует индекс v6."""
    storage.init_database()
    with storage._connect() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM knowledge "
            "WHERE namespace=? ORDER BY id DESC LIMIT ?",
            ("global", 500),
        ).fetchall()

    assert any("idx_knowledge_namespace_id" in row[3] for row in plan)


def test_project_fingerprint_schema_migration_is_repeatable(isolated_db):
    """Миграция добавляет отпечатки к старой схеме без удаления данных."""
    with sqlite3.connect(isolated_db) as connection:
        connection.execute(
            "CREATE TABLE knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "category TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL "
            "DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO knowledge(category,title,content) "
            "VALUES('backend','Existing note','Preserve this record')"
        )
    storage.init_database()
    storage.init_database()
    with storage._connect() as connection:
        event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(task_events)")
        }
        verification_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(verification_records)")
        }
        support_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(learning_corroborations)")
        }
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version=4"
            ).fetchone()[0]
            == 1
        )
    assert "project_fingerprint" in event_columns
    assert "project_fingerprint" in verification_columns
    assert "project_fingerprint" in support_columns
    assert storage.get_knowledge(1)["content"] == "Preserve this record"


def test_legacy_unverified_queue_events_wait_for_local_verification(isolated_db):
    """Миграция сохраняет старое событие и явно помечает отсутствие доверенной проверки."""
    storage.init_database()
    with storage._connect() as connection:
        connection.execute(
            "INSERT INTO task_events "
            "(event_id,project_id,task_id,client_name,task_description,changed_files,"
            "test_results,outcome,completed_at,status) "
            "VALUES('legacy-event','project:test','task-legacy','Codex','Old report',"
            "'[]','{}','Finished','2026-10-22T00:00:00Z','pending_analysis')"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version>=5")
    storage.init_database()
    event = storage.get_task_status("legacy-event")
    assert event["status"] == "waiting_for_verification"
    with storage._connect() as connection:
        audit = connection.execute(
            "SELECT action,status_from,status_to FROM learning_event_audit "
            "WHERE event_id='legacy-event'"
        ).fetchone()
    assert tuple(audit) == (
        "legacy_event_requires_verification",
        "pending_analysis",
        "waiting_for_verification",
    )


def test_project_scope_search_and_duplicate_guard(isolated_db):
    """Ограничивает поиск namespace и запрещает точный дубль в нём."""
    first = storage.create_knowledge(
        "backend", "Retry", "Retry safely", namespace="project:a"
    )
    second = storage.create_knowledge(
        "backend", "Retry", "Retry safely", namespace="project:b"
    )
    storage.approve_knowledge(first["id"])
    storage.approve_knowledge(second["id"])
    assert len(storage.search_knowledge("Retry", namespace="project:a")) == 1
    assert (
        storage.search_knowledge("Retry", namespace="project:a")[0]["namespace"]
        == "project:a"
    )
    with pytest.raises(storage.StorageError, match="дубликат"):
        storage.create_knowledge(
            "backend", "Retry", "Retry safely", namespace="project:a"
        )


def test_scoped_search_skips_fts_when_namespace_has_no_matching_status(
    isolated_db, monkeypatch
):
    """Не сканирует FTS, если в запрошенном namespace нет подходящего статуса."""
    storage.create_knowledge(
        "backend", "Retry guidance", "Retry safely", namespace="project:empty"
    )
    statements: list[str] = []
    original_connect = storage._connect

    @contextmanager
    def traced_connect(*, read_only=False, timeout=10.0):
        with original_connect(read_only=read_only, timeout=timeout) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(storage, "_connect", traced_connect)

    assert (
        storage.search_knowledge(
            "Retry guidance",
            namespace="project:empty",
            status="approved",
            prompt_context=True,
            _read_only=True,
        )
        == []
    )
    assert any("SELECT 1 FROM knowledge WHERE namespace=" in sql for sql in statements)
    assert not any("knowledge_fts MATCH" in sql for sql in statements)


def test_search_filters_status_category_and_namespace_exactly(isolated_db):
    """Разделяет status, category и namespace; по умолчанию ищет только approved."""
    candidate = storage.create_knowledge(
        "ai_agents", "CRM Integration", "Use exponential backoff", namespace="global"
    )
    approved = storage.create_knowledge(
        "backend", "CRM Integration", "Use retries safely", namespace="project:crm"
    )
    deprecated = storage.create_knowledge(
        "ai_agents", "CRM Integration", "Old retry guidance", namespace="global"
    )
    storage.approve_knowledge(approved["id"])
    storage.deprecate_knowledge(deprecated["id"])

    assert [row["id"] for row in storage.search_knowledge("CRM Integration")] == [
        approved["id"]
    ]
    assert [
        row["id"]
        for row in storage.search_knowledge(
            "CRM Integration",
            status="candidate",
            category="ai_agents",
            namespace="global",
        )
    ] == [candidate["id"]]
    assert [
        row["id"]
        for row in storage.search_knowledge(
            "CRM Integration",
            status="deprecated",
            category="ai_agents",
            namespace="global",
        )
    ] == [deprecated["id"]]
    assert {
        row["id"]
        for row in storage.search_knowledge(
            "CRM Integration", status=None, namespace="global"
        )
    } == {deprecated["id"], candidate["id"]}
    assert (
        storage.search_knowledge(
            "CRM Integration",
            status="candidate",
            category="ai_agents",
            namespace="project:crm",
        )
        == []
    )
    prompt_results = storage.search_knowledge(
        "CRM Integration " + "longprompt " * 100,
        status="approved",
        namespace="project:crm",
        prompt_context=True,
    )
    assert [row["id"] for row in prompt_results] == [approved["id"]]
    with pytest.raises(storage.StorageError, match="query"):
        storage.search_knowledge("x" * 501)
    with pytest.raises(storage.StorageError, match="Недопустимый статус"):
        storage.search_knowledge("CRM Integration", status="all")
    with pytest.raises(storage.StorageError, match="namespace"):
        storage.search_knowledge("CRM Integration", namespace="")


def test_prompt_search_prefers_memories_matching_all_query_terms(isolated_db):
    """Сначала возвращает записи, совпавшие со всеми словами запроса."""
    partial = storage.create_knowledge(
        "backend",
        "Retry policy",
        "Retry only after the first failure.",
        namespace="project:alpha",
    )
    complete = storage.create_knowledge(
        "backend",
        "Retry backoff timeout",
        "Use bounded exponential backoff timeout.",
        namespace="project:alpha",
    )
    foreign = storage.create_knowledge(
        "backend",
        "Retry backoff timeout",
        "Do not use this project rule.",
        namespace="project:beta",
    )
    for item in (partial, complete, foreign):
        storage.approve_knowledge(item["id"])

    results = storage.search_knowledge(
        "retry backoff timeout",
        namespace="project:alpha",
        prompt_context=True,
    )

    assert [item["id"] for item in results] == [complete["id"], partial["id"]]
    assert all(item["namespace"] == "project:alpha" for item in results)


@pytest.mark.parametrize("fts_available", [True, False], ids=["fts5", "like-fallback"])
def test_prompt_context_does_not_match_only_technology_metadata(
    isolated_db, monkeypatch, fts_available
):
    """Не подмешивает запись только из-за совпадения в поле technology."""
    knowledge = storage.create_knowledge(
        "backend",
        "General retry guidance",
        "Use bounded retries after transient failures.",
        technology="python",
        namespace="global",
    )
    storage.approve_knowledge(knowledge["id"])
    if not fts_available:
        _disable_fts(monkeypatch)

    prompt_results = storage.search_knowledge(
        "Python", namespace="global", prompt_context=True
    )
    regular_results = storage.search_knowledge("Python", namespace="global")

    assert prompt_results == []
    assert [row["id"] for row in regular_results] == [knowledge["id"]]


def test_prompt_search_fallback_matches_terms_out_of_order(isolated_db, monkeypatch):
    """LIKE fallback ищет слова запроса независимо от порядка при недоступном FTS5."""
    item = storage.create_knowledge(
        "backend",
        "Retry policy",
        "Retry operations use exponential backoff after transient failures.",
        namespace="project:alpha",
    )
    storage.approve_knowledge(item["id"])
    _disable_fts(monkeypatch)
    results = storage.search_knowledge(
        "transient retry backoff",
        namespace="project:alpha",
        prompt_context=True,
    )

    assert [result["id"] for result in results] == [item["id"]]


def test_prompt_search_does_not_hide_corrupted_fts_errors(isolated_db, monkeypatch):
    """Не маскирует повреждение существующего FTS-индекса под обычный fallback."""
    item = storage.create_knowledge(
        "backend",
        "Retry policy",
        "Use bounded backoff for transient errors.",
    )
    storage.approve_knowledge(item["id"])
    original_connect = storage._connect

    class CorruptedFtsConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if "knowledge_fts MATCH" in sql:
                raise sqlite3.OperationalError("database disk image is malformed")
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def connect_with_corrupted_fts(*, read_only=False, timeout=10.0):
        with original_connect(read_only=read_only, timeout=timeout) as connection:
            yield CorruptedFtsConnection(connection)

    monkeypatch.setattr(storage, "_connect", connect_with_corrupted_fts)
    with pytest.raises(sqlite3.OperationalError, match="malformed"):
        storage.search_knowledge("retry backoff", status="approved")


def test_prompt_search_fallback_ranks_by_matching_query_terms(isolated_db, monkeypatch):
    """Частичный fallback предпочитает записи с большим числом совпавших слов."""
    strong = storage.create_knowledge(
        "backend",
        "Retry behavior",
        "Transient retry uses exponential backoff.",
        namespace="project:alpha",
    )
    medium = storage.create_knowledge(
        "backend",
        "Retry behavior",
        "Retry after transient errors.",
        namespace="project:alpha",
    )
    recent_decoy = storage.create_knowledge(
        "backend",
        "Retry behavior",
        "Retry once before returning.",
        namespace="project:alpha",
    )
    for item in (strong, medium, recent_decoy):
        storage.approve_knowledge(item["id"])
    with storage._connect() as connection:
        connection.execute(
            "UPDATE knowledge SET updated_at='2099-01-01T00:00:00+00:00' WHERE id=?",
            (recent_decoy["id"],),
        )
    _disable_fts(monkeypatch)

    results = storage.search_knowledge(
        "transient retry backoff timeout",
        namespace="project:alpha",
        prompt_context=True,
        limit=3,
    )

    assert [item["id"] for item in results] == [
        strong["id"],
        medium["id"],
        recent_decoy["id"],
    ]


@pytest.mark.parametrize(
    ("query", "matching_content", "decoy_content"),
    [
        (
            "50% complete",
            "Import reports at 50% complete.",
            "Import reports at 5000 complete.",
        ),
        ("build_id value", "Read the build_id value.", "Read the buildXid value."),
    ],
)
def test_exact_search_fallback_treats_like_wildcards_literally(
    isolated_db,
    monkeypatch,
    query,
    matching_content,
    decoy_content,
):
    """Fallback exact search treats percent and underscore as literal query text."""
    matching = storage.create_knowledge("ops", "Literal result", matching_content)
    decoy = storage.create_knowledge("ops", "Wildcard decoy", decoy_content)
    storage.approve_knowledge(matching["id"])
    storage.approve_knowledge(decoy["id"])
    _disable_fts(monkeypatch)

    results = storage.search_knowledge(query)

    assert [result["id"] for result in results] == [matching["id"]]


def test_memory_overview_counts_all_statuses_and_scopes(isolated_db):
    """Обзор учитывает кандидатов и архивные записи без изменения их статусов."""
    candidate = storage.create_knowledge(
        "ai_agents", "Candidate", "Unapproved finding", namespace="global"
    )
    approved = storage.create_knowledge(
        "backend", "Approved", "Verified finding", namespace="global"
    )
    deprecated = storage.create_knowledge(
        "ai_agents", "Outdated", "Old finding", namespace="project:crm"
    )
    rejected = storage.create_knowledge(
        "ops", "Rejected", "Rejected finding", namespace="project:crm"
    )
    storage.approve_knowledge(approved["id"])
    storage.deprecate_knowledge(deprecated["id"])
    storage.reject_knowledge(rejected["id"])

    overview = storage.memory_overview()
    assert overview["total"] == 4
    assert overview["status_counts"] == {
        "approved": 1,
        "candidate": 1,
        "deprecated": 1,
        "rejected": 1,
    }
    assert overview["category_counts"] == {"ai_agents": 2, "backend": 1, "ops": 1}
    assert overview["namespace_counts"]["global"] == {
        "total": 2,
        "status_counts": {
            "approved": 1,
            "candidate": 1,
            "deprecated": 0,
            "rejected": 0,
        },
    }
    assert overview["namespace_counts"]["project:crm"]["total"] == 2

    global_overview = storage.memory_overview(namespace="global")
    assert global_overview["namespace_filter"] == "global"
    assert global_overview["total"] == 2
    assert global_overview["namespace_counts"].keys() == {"global"}
    assert storage.get_knowledge(candidate["id"])["status"] == "candidate"


def test_status_evidence_learning_and_export_controls(isolated_db):
    """Проверяет статусы, свидетельства, идемпотентность и безопасный экспорт."""
    item = storage.create_knowledge(
        "ops",
        "Verified",
        "A safe finding",
        publishable=True,
        evidence={"kind": "unit test", "result": "passed", "confidence": 0.9},
    )
    assert storage.search_knowledge("Verified") == []
    storage.approve_knowledge(item["id"])
    assert storage.search_knowledge("Verified")[0]["status"] == "approved"
    payload = storage.export_knowledge([item["id"]])
    assert storage.validate_import(payload)[0]["title"] == "Verified"


@pytest.mark.parametrize(
    "sensitive_value",
    [
        "token=example-secret",
        r"C:\Users\Owner\private.txt",
        "postgres://user:private-password@db.example.test/app",
    ],
)
def test_import_applies_shared_sensitive_data_policy(isolated_db, sensitive_value):
    """Import rejects common secrets, credential URLs, and absolute paths."""
    payload = {
        "schema_version": 1,
        "knowledge": [
            {
                "category": "backend",
                "title": "Safe title",
                "content": f"Example: {sensitive_value}",
            }
        ],
    }

    with pytest.raises(storage.StorageError, match="чувствительные данные"):
        storage.import_knowledge(payload, namespace="project:import")

    assert not isolated_db.exists()


def test_export_checks_sensitive_metadata_with_shared_policy(isolated_db):
    """Export checks metadata fields as well as the knowledge body."""
    item = storage.create_knowledge(
        "backend",
        "Safe title",
        "Safe content",
        technology="Python",
        source_reference="https://user:private-password@example.test/docs",
        publishable=True,
    )
    storage.approve_knowledge(item["id"])

    with pytest.raises(storage.StorageError, match="чувствительные данные"):
        storage.export_knowledge([item["id"]])
    event = {
        "event_id": "evt-1",
        "project_id": "p1",
        "task_id": "t1",
        "client_name": "test",
        "task_description": "finished task",
        "outcome": "done",
        "completed_at": "2026-10-22T00:00:00Z",
    }
    first = storage.ingest_task_event(event)
    second = storage.ingest_task_event(event)
    assert first["event_id"] == second["event_id"]
    assert first["status"] == "waiting_for_verification"
    assert storage.learning_status_summary() == {
        "total": 1,
        "status_counts": {"waiting_for_verification": 1},
        "verification_attempts": {"total": 0, "failed": 0, "launch_failures": 0},
        "llm_provider": "not configured",
        "automatic_analysis": False,
        "analyzed": 0,
        "candidates_created": 0,
        "failures": 0,
        "last_successful_analysis": None,
    }


def test_status_lists_launch_failures_without_raw_command_or_output(isolated_db):
    """Команда запуска не попадает в очередь и видна только как безопасные метаданные."""
    result = learning.verify_command(
        "missing-command-task",
        "Check missing verifier handling",
        ["ai-memory-command-that-does-not-exist-7e0c7d"],
        cwd=storage.DATA_DIR,
    )

    assert result["process_succeeded"] is False
    assert result["verification_scope"] == "process_launch_failed"
    assert result["verification_type"] == "command_launch"
    assert result["exit_code"] == -2
    assert result["error_type"] == "CommandLaunchError"
    saved = storage.get_verification(result["verification_id"])
    assert saved["verification_type"] == "command_launch"
    assert saved["trust_status"] == "trusted_local_launcher"
    assert storage.get_task_status(result["verification_id"]) is None
    attempts = storage.list_verification_attempts()
    assert len(attempts) == 1
    assert attempts[0]["verification_id"] == result["verification_id"]
    assert attempts[0]["task_id"] == "missing-command-task"
    assert "command_digest" not in attempts[0]
    assert "artifact_digest" not in attempts[0]
    assert storage.learning_status_summary()["verification_attempts"] == {
        "total": 1,
        "failed": 1,
        "launch_failures": 1,
    }


def test_unverified_task_event_does_not_persist_caller_supplied_content(isolated_db):
    """Сохраняет статус и безопасные ID, отбрасывая весь свободный ввод MCP."""
    event = {
        "event_id": "evt-safe-redaction",
        "project_id": "project:0123456789abcdef",
        "task_id": "task-safe-redaction",
        "client_name": "agent owner@example.com",
        "task_description": "Fix token=secret123 in C:\\client\\private.py",
        "outcome": "Bearer private-secret",
        "completed_at": "2026-10-22T00:00:00Z",
        "changed_files": ["C:\\client\\private.py"],
        "test_results": {"passed": True, "output": "password=raw-test-output"},
    }

    saved = storage.ingest_task_event(event)
    serialized = json.dumps(saved, ensure_ascii=False)

    for untrusted_value in (
        "secret123",
        "private-secret",
        "owner@example.com",
        "raw-test-output",
        "C:\\client",
        "Fix token=",
        "Bearer",
        "password=",
    ):
        assert untrusted_value not in serialized
    assert json.loads(saved["changed_files"]) == []
    assert json.loads(saved["test_results"]) == {}
    assert saved["client_name"] == "MCP"
    assert saved["task_description"] == "Unverified task event"
    assert saved["outcome"] == "Awaiting local verification"
    assert saved["status"] == "waiting_for_verification"
    assert saved["completed_at"] == "2026-10-22T00:00:00+00:00"


def test_rejection_reason_rejects_sensitive_values(isolated_db):
    """Причина отклонения не записывает credential-подобные значения в audit."""
    item = storage.create_knowledge("ops", "Candidate", "Pending review")

    with pytest.raises(storage.StorageError, match="приватные данные"):
        storage.reject_knowledge(item["id"], reason="token=example-secret")

    assert storage.get_knowledge(item["id"])["status"] == "candidate"
    with storage._connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM learning_event_audit WHERE knowledge_id=?",
                (item["id"],),
            ).fetchone()[0]
            == 0
        )


def test_mcp_cannot_claim_task_verified(isolated_db):
    """MCP completion reports cannot impersonate the trusted local verifier."""
    event = {
        "event_id": "evt-verified",
        "project_id": "project:test",
        "task_id": "task-1",
        "client_name": "Codex",
        "task_description": "Add a bounded read-only memory retrieval hook",
        "outcome": "Implemented and checked",
        "completed_at": "2026-10-22T00:00:00Z",
        "status": "task_verified",
        "test_results": {"passed": True, "evidence": "pytest: 1 passed"},
    }
    with pytest.raises(storage.StorageError, match="локальному verify API"):
        storage.ingest_task_event(
            {**event, "test_results": {"evidence": "pytest passed"}}
        )
    with pytest.raises(storage.StorageError, match="локальному verify API"):
        storage.ingest_task_event(
            {**event, "test_results": {"passed": True, "evidence": " "}}
        )
    with pytest.raises(storage.StorageError, match="локальному verify API"):
        storage.ingest_task_event(event)
    assert storage.list_knowledge() == []


def test_learning_queue_cli_reports_real_counts(isolated_db, monkeypatch, capsys):
    """CLI status reports the isolated queue, not a constant placeholder."""
    from ai_engineering_memory import cli

    storage.ingest_task_event(
        {
            "event_id": "evt-status",
            "project_id": "project:test",
            "task_id": "task-1",
            "client_name": "Codex",
            "task_description": "Check queue status",
            "outcome": "done",
            "completed_at": "2026-10-22T00:00:00Z",
        }
    )
    assert cli.main(["learning", "status"]) == 0
    result = __import__("json").loads(capsys.readouterr().out)
    assert result["total"] == 1
    assert result["status_counts"] == {"waiting_for_verification": 1}
    assert result["automatic_analysis"] is False
    assert result["background_analysis_configured"] is False
    assert result["codex_native_capture"] == {
        "mode": "best_effort",
        "requires": "UserPromptSubmit hook, Codex model, and learning_add_candidate MCP tool",
    }


def test_learning_status_reports_ollama_as_manual_only(
    isolated_db, monkeypatch, capsys
):
    """Configured legacy Ollama is not reported as automatic analysis."""
    from ai_engineering_memory import cli

    monkeypatch.setenv("AI_MEMORY_OLLAMA_MODEL", "local-model")
    monkeypatch.setenv("AI_MEMORY_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")

    assert storage.learning_status_summary()["automatic_analysis"] is False
    assert cli.main(["learning", "status"]) == 0
    result = __import__("json").loads(capsys.readouterr().out)
    assert result["provider_configured"] is True
    assert result["llm_provider"] == "ollama-local"
    assert result["automatic_analysis"] is False
    assert result["background_analysis_configured"] is False
    assert result["legacy_manual_analysis_available"] is True
    assert cli.main(["learning", "status", "--human"]) == 0
    human_output = capsys.readouterr().out
    assert "Дополнительный анализ: доступен только вручную" in human_output
    assert "автоматический запуск выключен" in human_output


def test_learning_status_human_explains_codex_capture_without_provider(
    isolated_db,
    capsys,
):
    """Поясняет best-effort capture, не смешивая его с legacy-провайдером."""
    from ai_engineering_memory import cli

    assert cli.main(["learning", "status", "--human"]) == 0
    output = capsys.readouterr().out

    assert (
        "Обучение: событий 0 (нет); проверок 0, ошибок 0, ошибок запуска 0." in output
    )
    assert "Сбор через Codex: best-effort" in output
    assert "провайдер не настроен; это не отключает сбор через Codex" in output
    assert "automatic_analysis" not in output


def test_export_rejects_unapproved_and_path_traversal(
    isolated_db, monkeypatch, tmp_path
):
    """Не разрешает экспорт кандидата и выход за разрешённый каталог Skills."""
    item = storage.create_knowledge(
        "private", "Draft", "personal draft", publishable=True
    )
    with pytest.raises(storage.StorageError, match="Экспорт разрешён"):
        storage.export_knowledge([item["id"]])
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    with pytest.raises(storage.StorageError, match="Недопустимое имя"):
        storage.get_skill("../secret")


def test_skill_approval_versions_and_rollback(isolated_db, monkeypatch, tmp_path):
    """Публикует предложение после maintainer approval и сохраняет откатываемую версию."""
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "maintainer")
    skill_dir = tmp_path / "skills" / "sample-skill"
    skill_dir.mkdir(parents=True)
    initial = "---\nname: sample-skill\ndescription: Sample\nmetadata:\n  version: 1.0.0\n  owner: maintainer\n  status: active\n---\nOld body\n"
    updated = initial.replace("1.0.0", "1.1.0").replace("Old body", "Updated body")
    (skill_dir / "SKILL.md").write_text(initial, encoding="utf-8")
    proposal_id = storage.propose_skill_update(
        "sample-skill", "agent", "Clarify behavior", updated
    )
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == initial
    storage.decide_skill_proposal(
        proposal_id, decision="approved", maintainer="maintainer"
    )
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == updated
    assert storage.get_skill_history("sample-skill")[0]["version"] == "1.1.0"
    storage.rollback_skill_version("sample-skill", "1.0.0", maintainer="maintainer")
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == initial


def test_skill_proposal_review_shows_candidate_and_active_content(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    """Даёт maintainer-у прочитать кандидат и активную версию до решения."""
    from ai_engineering_memory import mcp_server

    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    skill_dir = tmp_path / "skills" / "sample-skill"
    skill_dir.mkdir(parents=True)
    current = (
        "---\nname: sample-skill\ndescription: Sample\nmetadata:\n"
        "  version: 1.0.0\n---\nCurrent active instructions\n"
    )
    proposed = current.replace("1.0.0", "1.1.0").replace(
        "Current active instructions",
        "Proposed untrusted instructions",
    )
    (skill_dir / "SKILL.md").write_text(current, encoding="utf-8")
    proposal_id = storage.propose_skill_update(
        "sample-skill",
        "agent",
        "Clarify workflow",
        proposed,
    )

    review = mcp_server.skills_get_proposal(proposal_id)

    assert review["status"] == "candidate"
    assert review["proposed_content"] == proposed
    assert review["current_content"] == current
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == current


def test_skill_proposal_rejects_common_sensitive_content(isolated_db):
    """Не сохраняет очевидные credentials, контакты и абсолютные пути в proposal."""
    unsafe_values = (
        "token=example-secret",
        "Contact owner@example.com",
        r"Local file C:\Users\Owner\private.txt",
        "postgres://user:private-password@db.example.test/app",
        "https://user:private-password@example.test/docs",
        "AKIA1234567890ABCDEF",
        "ghp_123456789012345678901234567890123456",
    )
    for index, unsafe_value in enumerate(unsafe_values):
        content = (
            "---\nname: sample-skill\ndescription: Sample\n---\n"
            f"Unsafe content {index}: {unsafe_value}\n"
        )
        with pytest.raises(storage.StorageError, match="приватные данные"):
            storage.propose_skill_update(
                "sample-skill",
                "agent",
                "Test sensitive content",
                content,
            )

    assert storage.list_skill_proposals() == []


def test_skill_proposal_allows_safe_documentation_urls(isolated_db):
    """Официальные и обычные HTTPS-ссылки не считаются абсолютными путями."""
    content = (
        "---\nname: sample-skill\ndescription: Sample\n---\n"
        "Read https://docs.python.org/3/library/pathlib.html and "
        "https://example.test/api/v1 before implementation.\n"
    )

    proposal_id = storage.propose_skill_update(
        "sample-skill",
        "agent",
        "Add docs links",
        content,
    )

    assert storage.list_skill_proposals()[0]["id"] == proposal_id


def test_skill_proposal_rejects_sensitive_metadata(isolated_db):
    """Не сохраняет контакт или секрет из имени автора и описания изменения."""
    content = "---\nname: sample-skill\ndescription: Sample\n---\nSafe body\n"
    with pytest.raises(storage.StorageError, match="приватные данные"):
        storage.propose_skill_update(
            "sample-skill",
            "owner@example.com",
            "Safe description",
            content,
        )
    with pytest.raises(storage.StorageError, match="приватные данные"):
        storage.propose_skill_update(
            "sample-skill",
            "agent",
            "Set token=secret-value",
            content,
        )
    assert storage.list_skill_proposals() == []


def test_skill_listing_uses_relative_paths_and_blocks_sensitive_active_content(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    """Скрывает абсолютный путь проекта и не возвращает опасное содержимое активного Skill."""
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    skill_dir = tmp_path / "skills" / "sample-skill"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    safe_content = "---\nname: sample-skill\ndescription: Sample\n---\nSafe body\n"
    skill_path.write_text(safe_content, encoding="utf-8")

    listed = storage.list_skills()

    assert listed[0]["path"] == "skills/sample-skill"
    assert str(tmp_path) not in str(listed)
    skill_path.write_text(safe_content + "token=example-secret\n", encoding="utf-8")
    with pytest.raises(storage.StorageError, match="приватные данные"):
        storage.get_skill("sample-skill")
    with pytest.raises(storage.StorageError, match="приватные данные"):
        storage.list_skills()


def test_legacy_sensitive_skill_proposal_cannot_be_read_or_approved(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    """Блокирует чтение и публикацию небезопасного legacy-предложения без его изменения."""
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "maintainer")
    skill_dir = tmp_path / "skills" / "sample-skill"
    skill_dir.mkdir(parents=True)
    current = "---\nname: sample-skill\ndescription: Sample\nmetadata:\n  version: 1.0.0\n---\nSafe\n"
    (skill_dir / "SKILL.md").write_text(current, encoding="utf-8")
    unsafe = current.replace("1.0.0", "1.1.0") + "token=example-secret\n"
    proposal_id = "68fc77c8-882e-49b5-9be6-77fa2772c117"
    storage.init_database()
    with storage._connect() as connection:
        connection.execute(
            "INSERT INTO skill_proposals(id,skill_name,proposer,change_description,proposed_content,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                proposal_id,
                "sample-skill",
                "owner@example.com",
                "token=legacy-secret",
                unsafe,
                "2026-01-01T00:00:00+00:00",
            ),
        )

    with pytest.raises(storage.StorageError, match="приватные данные"):
        storage.get_skill_proposal(proposal_id)
    with pytest.raises(storage.StorageError, match="приватные данные"):
        storage.decide_skill_proposal(
            proposal_id,
            decision="approved",
            maintainer="maintainer",
        )
    listed = storage.list_skill_proposals()
    assert listed[0]["proposer"] == "[redacted]"
    assert listed[0]["change_description"] == "[redacted]"

    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == current
    with storage._connect() as connection:
        saved = connection.execute(
            "SELECT status, proposed_content FROM skill_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
    assert saved["status"] == "candidate"
    assert saved["proposed_content"] == unsafe
    rejected = storage.decide_skill_proposal(
        proposal_id,
        decision="rejected",
        maintainer="maintainer",
    )
    assert rejected["status"] == "rejected"


def test_legacy_sensitive_skill_version_cannot_be_rolled_back(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    """Не восстанавливает секрет из legacy-истории и не меняет активный Skill."""
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "maintainer")
    skill_dir = tmp_path / "skills" / "sample-skill"
    skill_dir.mkdir(parents=True)
    current = "---\nname: sample-skill\ndescription: Sample\nmetadata:\n  version: 2.0.0\n---\nSafe\n"
    (skill_dir / "SKILL.md").write_text(current, encoding="utf-8")
    unsafe = current.replace("2.0.0", "1.0.0") + "token=example-secret\n"
    storage.init_database()
    with storage._connect() as connection:
        connection.execute(
            "INSERT INTO skill_versions(skill_name,version,content,changelog,validation_result,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "sample-skill",
                "1.0.0",
                unsafe,
                "legacy import",
                "validated",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    with pytest.raises(storage.StorageError, match="приватные данные"):
        storage.rollback_skill_version(
            "sample-skill",
            "1.0.0",
            maintainer="maintainer",
        )

    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == current


def test_legacy_sensitive_skill_changelog_is_redacted(isolated_db):
    """Скрывает возможные секреты из legacy-changelog в выдаче истории версий."""
    storage.init_database()
    with storage._connect() as connection:
        connection.execute(
            "INSERT INTO skill_versions(skill_name,version,content,changelog,validation_result,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "sample-skill",
                "1.0.0",
                "---\nname: sample-skill\ndescription: Sample\n---\nSafe\n",
                "token=legacy-secret",
                "validated",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    history = storage.get_skill_history("sample-skill")

    assert history[0]["changelog"] == "[redacted]"


def test_maintainer_decisions_fail_closed_without_configuration(
    isolated_db, monkeypatch
):
    """Запрещает подтверждения при отсутствующей локальной конфигурации владельца."""
    monkeypatch.delenv("AI_MEMORY_MAINTAINER", raising=False)

    with pytest.raises(storage.StorageError, match="не настроен"):
        storage.require_maintainer("local-owner")

    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "configured-owner")
    with pytest.raises(
        storage.StorageError, match="настроенному локальному maintainer"
    ):
        storage.require_maintainer("local-owner")
    assert storage.require_maintainer("configured-owner") == "configured-owner"


@pytest.mark.parametrize("link_location", ["skills_root", "skill_directory"])
def test_skill_operations_reject_symlink_escape(
    isolated_db,
    monkeypatch,
    tmp_path,
    link_location,
):
    """Запрещает читать и публиковать Skill через ссылку за пределами репозитория."""
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    outside_root = tmp_path / "outside"
    skill_dir = outside_root / "sample-skill"
    skill_dir.mkdir(parents=True)
    original = (
        "---\nname: sample-skill\ndescription: Sample\nmetadata:\n"
        "  version: 1.0.0\n  owner: maintainer\n  status: active\n---\n"
        "Outside content\n"
    )
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(original, encoding="utf-8")
    if link_location == "skills_root":
        link_path = base_dir / "skills"
        link_target = outside_root
    else:
        (base_dir / "skills").mkdir()
        link_path = base_dir / "skills" / "sample-skill"
        link_target = skill_dir
    try:
        link_path.symlink_to(link_target, target_is_directory=True)
    except OSError as exc:
        if sys.platform != "win32":
            pytest.skip(f"Символьные ссылки недоступны в этой среде: {exc}")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(link_target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(
                f"Не удалось создать тестовую ссылку: {junction.stderr.strip()}"
            )

    monkeypatch.setattr(storage, "BASE_DIR", base_dir)
    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "maintainer")
    updated = original.replace("1.0.0", "1.1.0").replace(
        "Outside content",
        "Updated content",
    )
    proposal_id = storage.propose_skill_update(
        "sample-skill",
        "agent",
        "Change through symlink",
        updated,
    )

    with pytest.raises(storage.StorageError, match="пределы|разрешённый каталог"):
        storage.get_skill("sample-skill")
    with pytest.raises(storage.StorageError, match="пределы|разрешённый каталог"):
        storage.list_skills()
    with pytest.raises(storage.StorageError, match="пределы|разрешённый каталог"):
        storage.decide_skill_proposal(
            proposal_id,
            decision="approved",
            maintainer="maintainer",
        )
    assert skill_path.read_text(encoding="utf-8") == original
