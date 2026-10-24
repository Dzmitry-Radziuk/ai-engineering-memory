"""Проверяет границы MCP tools без запуска Codex или пользовательской БД."""

from __future__ import annotations

import json

import pytest

from ai_engineering_memory import mcp_server, storage
from ai_engineering_memory.learning import project_namespace
from ai_engineering_memory.settings import Settings


def _configure_isolated_mcp(monkeypatch, tmp_path):
    """Выбирает временную базу и корень проекта для прямого вызова tools."""
    database_path = tmp_path / "mcp-unit.sqlite"
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))
    monkeypatch.setenv("AI_MEMORY_PROJECT_ROOT", str(project_root))
    monkeypatch.delenv("AI_MEMORY_MAINTAINER", raising=False)
    store = storage.create_storage(Settings(database_path))
    store.init_database()
    return store, project_root


def test_mcp_namespace_policy_rejects_unconfigured_and_relative_roots(
    monkeypatch, tmp_path
):
    """MCP доступ ограничен текущим проектом и общей памятью."""
    _configure_isolated_mcp(monkeypatch, tmp_path)
    allowed = mcp_server._allowed_memory_namespaces()
    assert "global" in allowed
    assert project_namespace(tmp_path / "project") in allowed
    mcp_server._require_allowed_namespace("global")

    with pytest.raises(ValueError, match="Namespace недоступен"):
        mcp_server._require_allowed_namespace("project:foreign")

    monkeypatch.setenv("AI_MEMORY_PROJECT_ROOT", "relative-project")
    with pytest.raises(ValueError, match="абсолютным путём"):
        mcp_server._allowed_memory_namespaces()


def test_memory_tools_keep_read_and_candidate_access_scoped(monkeypatch, tmp_path):
    """MCP search/get/overview/list/add проходят через scoped services."""
    store, project_root = _configure_isolated_mcp(monkeypatch, tmp_path)
    namespace = project_namespace(project_root)
    with store.activate():
        global_record = storage.create_knowledge(
            "backend", "Global retry", "needle bounded retry guidance", publishable=True
        )
        storage.approve_knowledge(global_record["id"])
        foreign_record = storage.create_knowledge(
            "backend",
            "Foreign retry",
            "needle foreign guidance",
            namespace="project:foreign",
        )

    candidate = mcp_server.memory_add_candidate(
        "backend", "Project candidate", "A scoped rule for local tests.", namespace
    )
    assert candidate["status"] == "candidate"
    assert (
        mcp_server.memory_list_candidates(namespace=namespace)[0]["id"]
        == candidate["id"]
    )
    assert mcp_server.memory_get(candidate["id"])["namespace"] == namespace
    assert mcp_server.memory_get(foreign_record["id"]) is None
    assert mcp_server.memory_search("needle", namespace="global")
    assert mcp_server.memory_overview("global")["total"] == 1
    assert mcp_server.memory_overview()["total"] == 2
    with pytest.raises(ValueError, match="Namespace недоступен"):
        mcp_server.memory_search("needle", namespace="project:foreign")
    with pytest.raises(ValueError, match="Запись не найдена"):
        mcp_server._require_visible_knowledge(foreign_record["id"])


def test_learning_task_tool_validates_json_and_project_scope(monkeypatch, tmp_path):
    """Learning MCP tool accepts scoped event metadata and rejects forged scope."""
    store, project_root = _configure_isolated_mcp(monkeypatch, tmp_path)
    namespace = project_namespace(project_root)
    event = {
        "event_id": "mcp-unit-event",
        "project_id": namespace,
        "task_id": "task-1",
        "client_name": "Codex",
        "task_description": "Store only a bounded event summary",
        "outcome": "Completed with tests",
        "completed_at": "2026-10-24T00:00:00Z",
    }

    result = mcp_server.learning_submit_task(json.dumps(event))
    assert result["status"] == "waiting_for_verification"
    assert (
        mcp_server.learning_get_status("mcp-unit-event")["event_id"] == "mcp-unit-event"
    )

    with pytest.raises(json.JSONDecodeError):
        mcp_server.learning_submit_task("{")
    with pytest.raises(TypeError, match="JSON-объектом"):
        mcp_server.learning_submit_task("[]")
    with pytest.raises(ValueError, match="текущему проекту"):
        mcp_server.learning_submit_task(
            json.dumps({**event, "event_id": "global-event", "project_id": "global"})
        )
    with pytest.raises(ValueError, match="Namespace недоступен"):
        mcp_server.learning_submit_task(
            json.dumps(
                {**event, "event_id": "foreign-event", "project_id": "project:foreign"}
            )
        )
    assert store.database_path.exists()


def test_mcp_json_formatter_keeps_only_structured_safe_fields():
    """MCP formatter produces structured metadata and does not echo extras."""
    record = mcp_server._JsonFormatter().format(
        __import__("logging").LogRecord("aem-test", 20, "", 0, "safe event", (), None)
    )

    payload = json.loads(record)
    assert payload["event"] == "safe event"
    assert payload["component"] == "aem-test"
    assert "prompt" not in payload
