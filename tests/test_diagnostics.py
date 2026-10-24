"""Проверки read-only диагностики локальной установки."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from ai_engineering_memory import cli, codex_hooks, diagnostics, hook_install, storage


def _write_hook_config(path):
    """Создаёт временную конфигурацию с обоими ожидаемыми hooks."""
    hooks = {
        event: [{
            "hooks": [{
                "type": "command",
                "command": f'"{sys.executable}" -m ai_engineering_memory.codex_hooks {event}',
                "timeout": hook_install.HOOK_TIMEOUT_SECONDS,
                "statusMessage": hook_install.HOOK_STATUS_MESSAGES[event],
            }],
        }]
        for event in ("UserPromptSubmit", "Stop")
    }
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def test_diagnostics_checks_database_hooks_and_redacted_log(tmp_path):
    """Диагностика читает актуальное состояние и не возвращает текст из журнала."""
    database = tmp_path / "memory.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (5)")
    before_database = database.read_bytes()

    hook_config = tmp_path / "hooks.json"
    _write_hook_config(hook_config)
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(
        json.dumps({
            "event": "UserPromptSubmit",
            "timestamp": "2026-10-23T10:00:00+00:00",
            "source": "ai-engineering-memory:1.5.3",
            "result": "success",
            "duration_ms": 12,
            "prompt": "PRIVATE-PROMPT-MUST-NOT-LEAK",
        }) + "\n" + json.dumps({
            "event": "Stop",
            "timestamp": "2026-10-23T10:01:00+00:00",
            "result": "error",
            "duration_ms": 4,
            "error_type": "ValueError",
            "response": "PRIVATE-RESPONSE-MUST-NOT-LEAK",
        }) + "\n",
        encoding="utf-8",
    )
    before_log = hook_log.read_bytes()

    report = diagnostics.collect_diagnostics(
        database_path=database,
        hooks_config_path=hook_config,
        hook_log_path=hook_log,
    )

    serialized = json.dumps(report)
    assert report["overall"] == "warning"
    assert report["database"]["integrity"] == "ok"
    assert report["database"]["schema_version"] == 5
    assert report["hooks"]["events"] == {
        "UserPromptSubmit": "ok", "Stop": "ok",
    }
    assert report["hooks"]["launcher_matches_current_interpreter"] == {
        "UserPromptSubmit": True, "Stop": True,
    }
    assert report["hook_log"]["error_count"] == 1
    assert report["hook_log"]["missing_events"] == []
    assert report["hook_log"]["latest_error_events"] == ["Stop"]
    assert report["hook_log"]["status"] == "warning"
    assert report["hook_log"]["events"]["UserPromptSubmit"]["source"] == (
        "ai-engineering-memory:1.5.3"
    )
    assert report["hook_log"]["events"]["Stop"]["error_type"] == "ValueError"
    assert "PRIVATE-PROMPT" not in serialized
    assert "PRIVATE-RESPONSE" not in serialized
    assert database.read_bytes() == before_database
    assert hook_log.read_bytes() == before_log


def test_diagnostics_reports_missing_installation_without_creating_files(tmp_path):
    """Отсутствующие файлы видны как предупреждение, но не создаются проверкой."""
    database = tmp_path / "missing.db"
    hook_config = tmp_path / "missing-hooks.json"
    hook_log = tmp_path / "missing-hooks.jsonl"

    report = diagnostics.collect_diagnostics(
        database_path=database,
        hooks_config_path=hook_config,
        hook_log_path=hook_log,
    )

    assert report["overall"] == "warning"
    assert report["database"]["status"] == "missing"
    assert report["hooks"]["status"] == "missing"
    assert report["hook_log"]["status"] == "missing"
    assert report["hook_log"]["missing_events"] == ["UserPromptSubmit", "Stop"]
    assert not database.exists()
    assert not hook_config.exists()
    assert not hook_log.exists()
    assert report["memory_mcp"]["status"] == "not_checked"


def test_database_check_reports_lifecycle_counts_without_reading_record_content(tmp_path):
    """Read-only diagnosis exposes lifecycle counts, never stored knowledge text."""
    database = tmp_path / "memory-counts.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (5)")
        connection.execute(
            "CREATE TABLE knowledge "
            "(id INTEGER PRIMARY KEY, namespace TEXT, status TEXT, title TEXT)"
        )
        connection.executemany(
            "INSERT INTO knowledge(namespace, status, title) VALUES (?, ?, ?)",
            [
                ("global", "approved", "PRIVATE-APPROVED-TITLE"),
                ("project:test", "candidate", "PRIVATE-CANDIDATE-TITLE"),
            ],
        )
        connection.execute("CREATE TABLE task_events (event_id TEXT, status TEXT)")
        connection.executemany(
            "INSERT INTO task_events VALUES (?, ?)",
            [("private-event-1", "pending_analysis"), ("private-event-2", "failed")],
        )
        connection.execute("CREATE TABLE verification_records (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO verification_records DEFAULT VALUES")
        connection.execute(
            "CREATE TABLE learning_corroborations (id INTEGER PRIMARY KEY)"
        )
    before = database.read_bytes()

    report = diagnostics._database_check(database)

    assert report["knowledge_counts"] == {
        "total": 2,
        "status_counts": {"approved": 1, "candidate": 1},
    }
    assert report["learning_counts"] == {
        "task_events": {
            "total": 2,
            "status_counts": {"failed": 1, "pending_analysis": 1},
        },
        "verification_records": {"total": 1},
        "learning_corroborations": {"total": 0},
    }
    serialized = json.dumps(report)
    assert "PRIVATE-APPROVED-TITLE" not in serialized
    assert "PRIVATE-CANDIDATE-TITLE" not in serialized
    assert "private-event" not in serialized
    assert database.read_bytes() == before


def test_memory_mcp_check_reports_safe_launch_state_without_secrets(tmp_path):
    """Показывает известный MCP launcher, не раскрывая его env или аргументы."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        'AI_MEMORY_DB = "private-database-path"\n'
        f"AI_MEMORY_PROJECT_ROOT = {json.dumps(str(tmp_path))}\n",
        encoding="utf-8",
    )

    result = diagnostics._memory_mcp_check(config, expected_project_root=tmp_path)

    assert result == {
        "status": "configured", "configured": True,
        "launcher_available": True, "entrypoint_valid": True,
        "runtime_verified": False, "project_scope": "configured_root",
        "project_scope_match": "matched",
        "config_scope": "user",
    }
    assert "private-database-path" not in json.dumps(result)
    assert "ai_engineering_memory.cli" not in json.dumps(result)


def test_memory_mcp_check_rejects_invalid_project_root_without_echoing_it(tmp_path):
    """Проверяет путь scope MCP, не раскрывая конфигурированный путь."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        'AI_MEMORY_PROJECT_ROOT = "relative-private-project-path"\n',
        encoding="utf-8",
    )

    result = diagnostics._memory_mcp_check(config)

    assert result["status"] == "invalid_project_root"
    assert result["configured"] is False
    assert result["project_scope"] == "invalid"
    assert "relative-private-project-path" not in json.dumps(result)


def test_memory_mcp_check_detects_configured_root_for_another_project(tmp_path):
    configured_root = tmp_path / "project-a"
    expected_root = tmp_path / "project-b"
    configured_root.mkdir()
    (configured_root / ".git").mkdir()
    expected_root.mkdir()
    (expected_root / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        f"AI_MEMORY_PROJECT_ROOT = {json.dumps(str(configured_root))}\n",
        encoding="utf-8",
    )

    result = diagnostics._memory_mcp_check(
        config, expected_project_root=expected_root,
    )

    assert result["status"] == "project_scope_mismatch"
    assert result["project_scope_match"] == "mismatch"
    assert str(configured_root) not in json.dumps(result)
    assert str(expected_root) not in json.dumps(result)


def test_memory_mcp_check_detects_explicit_cwd_for_another_project(tmp_path):
    configured_cwd = tmp_path / "project-a"
    expected_root = tmp_path / "project-b"
    configured_cwd.mkdir()
    (configured_cwd / ".git").mkdir()
    expected_root.mkdir()
    (expected_root / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        f"cwd = {json.dumps(str(configured_cwd))}\n",
        encoding="utf-8",
    )

    result = diagnostics._memory_mcp_check(
        config, expected_project_root=expected_root,
    )

    assert result["status"] == "project_scope_mismatch"
    assert result["project_scope"] == "configured_cwd"
    assert result["project_scope_match"] == "mismatch"
    assert str(configured_cwd) not in json.dumps(result)
    assert str(expected_root) not in json.dumps(result)


def test_memory_mcp_check_matches_cwd_subdirectory_to_git_project_root(tmp_path):
    project_root = tmp_path / "project"
    configured_cwd = project_root / "src"
    (project_root / ".git").mkdir(parents=True)
    configured_cwd.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        f"cwd = {json.dumps(str(configured_cwd))}\n",
        encoding="utf-8",
    )

    result = diagnostics._memory_mcp_check(
        config, expected_project_root=project_root,
    )

    assert result["status"] == "configured"
    assert result["project_scope"] == "configured_cwd"
    assert result["project_scope_match"] == "matched"


def test_memory_mcp_check_marks_relative_cwd_unverified(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        'cwd = "relative-project-path"\n',
        encoding="utf-8",
    )

    result = diagnostics._memory_mcp_check(
        config, expected_project_root=project_root,
    )

    assert result["status"] == "project_scope_unverified"
    assert result["project_scope_match"] == "unverified"
    assert "relative-project-path" not in json.dumps(result)


def test_doctor_skips_mcp_probe_when_project_scope_mismatches(tmp_path, monkeypatch):
    configured_root = tmp_path / "project-a"
    expected_root = tmp_path / "project-b"
    configured_root.mkdir()
    (configured_root / ".git").mkdir()
    expected_root.mkdir()
    (expected_root / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        f"AI_MEMORY_PROJECT_ROOT = {json.dumps(str(configured_root))}\n",
        encoding="utf-8",
    )
    probe_calls = []
    monkeypatch.setattr(
        diagnostics, "_probe_memory_mcp",
        lambda *args, **kwargs: probe_calls.append((args, kwargs)),
    )

    report = diagnostics.collect_diagnostics(
        database_path=tmp_path / "missing.db",
        hooks_config_path=tmp_path / "missing-hooks.json",
        hook_log_path=tmp_path / "missing-hooks.jsonl",
        mcp_config_path=config,
        expected_project_root=expected_root,
        probe_mcp=True,
    )

    assert report["memory_mcp"]["status"] == "project_scope_mismatch"
    assert report["memory_mcp"]["protocol_probe_status"] == "skipped_project_scope_mismatch"
    assert probe_calls == []


def test_doctor_skips_mcp_probe_when_project_scope_is_unverified(tmp_path, monkeypatch):
    expected_root = tmp_path / "project"
    expected_root.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        'cwd = "relative-project-path"\n',
        encoding="utf-8",
    )
    probe_calls = []
    monkeypatch.setattr(
        diagnostics, "_probe_memory_mcp",
        lambda *args, **kwargs: probe_calls.append((args, kwargs)),
    )

    report = diagnostics.collect_diagnostics(
        database_path=tmp_path / "missing.db",
        hooks_config_path=tmp_path / "missing-hooks.json",
        hook_log_path=tmp_path / "missing-hooks.jsonl",
        mcp_config_path=config,
        expected_project_root=expected_root,
        probe_mcp=True,
    )

    assert report["memory_mcp"]["status"] == "project_scope_unverified"
    assert report["memory_mcp"]["protocol_probe_status"] == "skipped_project_scope_unverified"
    assert probe_calls == []


def test_memory_mcp_check_merges_project_override_and_inherited_maintainer(tmp_path):
    """Проверяет user+project overlay, не раскрывая конфигурационные значения."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    user_config = tmp_path / "user.toml"
    user_config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        'AI_MEMORY_MAINTAINER = "user-maintainer-secret"\n',
        encoding="utf-8",
    )
    project_config = tmp_path / ".codex" / "config.toml"
    project_config.parent.mkdir()
    project_config.write_text(
        "[mcp_servers.ai_engineering_memory.env]\n"
        f"AI_MEMORY_PROJECT_ROOT = {json.dumps(str(project_root))}\n",
        encoding="utf-8",
    )

    result = diagnostics._memory_mcp_check(
        user_config, project_config, expected_project_root=project_root,
    )
    maintainer = diagnostics._maintainer_check(user_config, project_config)

    assert result["status"] == "configured"
    assert result["config_scope"] == "project"
    assert result["project_scope"] == "configured_root"
    assert result["project_scope_match"] == "matched"
    assert result["entrypoint_valid"] is True
    assert maintainer["status"] == "not_exposed"
    assert "user-maintainer-secret" not in json.dumps(result | maintainer)


def test_memory_mcp_check_uses_project_server_when_user_config_is_missing(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    project_config = tmp_path / ".codex" / "config.toml"
    project_config.parent.mkdir()
    project_config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        f"AI_MEMORY_PROJECT_ROOT = {json.dumps(str(project_root))}\n",
        encoding="utf-8",
    )

    result = diagnostics._memory_mcp_check(None, project_config)

    assert result["status"] == "configured"
    assert result["config_scope"] == "project"
    assert result["project_scope"] == "configured_root"


def test_doctor_cli_reads_project_overlay_for_requested_cwd(
    tmp_path, monkeypatch, capsys
):
    """doctor --cwd читает project config, не выдавая его за runtime Codex."""
    database = tmp_path / "memory.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (5)")
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    _write_hook_config(codex_home / "hooks.json")
    (codex_home / "config.toml").write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        'AI_MEMORY_MAINTAINER = "test-maintainer"\n',
        encoding="utf-8",
    )
    project_root = tmp_path / "sample-project"
    project_root.mkdir()
    project_codex = project_root / ".codex"
    project_codex.mkdir()
    (project_codex / "config.toml").write_text(
        "[mcp_servers.ai_engineering_memory.env]\n"
        f"AI_MEMORY_PROJECT_ROOT = {json.dumps(str(project_root))}\n",
        encoding="utf-8",
    )
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(
        "\n".join(json.dumps({
            "event": event,
            "timestamp": "2026-10-23T10:00:00+00:00",
            "source": codex_hooks.HOOK_SOURCE,
            "result": result,
        }) for event, result in (
            ("UserPromptSubmit", "success"), ("Stop", "no_marker_seen"),
        )) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(storage, "DATABASE_PATH", database)
    monkeypatch.setenv("AI_MEMORY_DB", str(database))
    monkeypatch.setattr(codex_hooks, "_log_path", lambda: hook_log)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("AI_MEMORY_MAINTAINER", raising=False)

    assert cli.main(["doctor", "--cwd", str(project_root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["memory_mcp"]["status"] == "configured"
    assert report["memory_mcp"]["config_scope"] == "project"
    assert report["memory_mcp"]["project_scope"] == "configured_root"
    assert report["memory_mcp"]["runtime_verified"] is False
    assert report["maintainer_actions"]["status"] == "not_exposed"

    assert cli.main(["doctor", "--cwd", str(project_root)]) == 0
    human_report = capsys.readouterr().out
    assert "Codex" in human_report and "доверенного проекта" in human_report

    nested_cwd = project_root / "src" / "feature"
    nested_cwd.mkdir(parents=True)
    nested_codex = nested_cwd / ".codex"
    nested_codex.mkdir()
    (nested_codex / "config.toml").write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        "enabled = false\n",
        encoding="utf-8",
    )
    assert cli.main(["doctor", "--cwd", str(nested_cwd), "--json"]) == 1
    nested_report = json.loads(capsys.readouterr().out)
    assert nested_report["memory_mcp"]["status"] == "disabled"
    assert nested_report["memory_mcp"]["config_scope"] == "project"


def test_doctor_mcp_probe_calls_read_only_tool_on_isolated_database(tmp_path):
    config = tmp_path / "config.toml"
    database = tmp_path / "probe.sqlite"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        f"AI_MEMORY_DB = {json.dumps(str(database))}\n"
        f"AI_MEMORY_PROJECT_ROOT = {json.dumps(str(tmp_path))}\n",
        encoding="utf-8",
    )

    result = diagnostics._probe_memory_mcp(config)

    assert result["protocol_probe_status"] == "verified"
    assert result["protocol_probe_required_tools_present"] is True
    assert result["protocol_probe_tool_count"] > 0
    assert result["protocol_probe_call_status"] == "verified"
    assert result["protocol_probe_call_verified"] is True
    assert result["protocol_probe_database_scope"] == "isolated_temporary"
    report = diagnostics.collect_diagnostics(
        database_path=tmp_path / "doctor.sqlite",
        hooks_config_path=tmp_path / "hooks.json",
        hook_log_path=tmp_path / "hooks.jsonl",
        mcp_config_path=config,
        probe_mcp=True,
    )
    assert report["memory_mcp"]["runtime_verified"] is False
    assert report["memory_mcp"]["protocol_probe_status"] == "verified"
    assert not database.exists()


def test_doctor_mcp_probe_skips_unrecognized_command_without_executing_it(tmp_path):
    config = tmp_path / "config.toml"
    sentinel = tmp_path / "should-not-run.txt"
    payload = f"from pathlib import Path; Path({str(sentinel)!r}).touch()"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [\"-c\", {json.dumps(payload)}]\n",
        encoding="utf-8",
    )

    result = diagnostics._probe_memory_mcp(config)

    assert result["protocol_probe_status"] == "unsafe_command_skipped"
    assert not sentinel.exists()


def test_doctor_mcp_probe_skips_environment_that_can_change_python_execution(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n'
        "[mcp_servers.ai_engineering_memory.env]\n"
        f"PYTHONPATH = {json.dumps(str(tmp_path))}\n",
        encoding="utf-8",
    )

    result = diagnostics._probe_memory_mcp(config)

    assert result["protocol_probe_status"] == "unsupported_environment_skipped"


def test_memory_mcp_check_distinguishes_missing_disabled_and_incomplete(tmp_path):
    """Не считает отсутствующий или неполный MCP запуск готовым."""
    config = tmp_path / "config.toml"
    assert diagnostics._memory_mcp_check(config)["status"] == "missing"

    config.write_text(
        "[mcp_servers.ai_engineering_memory]\nenabled = false\n",
        encoding="utf-8",
    )
    assert diagnostics._memory_mcp_check(config)["status"] == "disabled"

    config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\nargs = []\n",
        encoding="utf-8",
    )
    assert diagnostics._memory_mcp_check(config)["status"] == "incomplete"


def test_maintainer_diagnostic_reports_only_configuration_state(tmp_path, monkeypatch):
    """Сообщает, включены ли решения владельца, не раскрывая его имя."""
    monkeypatch.delenv("AI_MEMORY_MAINTAINER", raising=False)
    disabled = diagnostics._maintainer_check()
    assert disabled["status"] == "not_exposed"

    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "private-maintainer-name")
    enabled = diagnostics._maintainer_check()
    assert enabled["status"] == "not_exposed"
    assert enabled["source"] == "mcp_tool_surface"
    assert "private-maintainer-name" not in json.dumps(enabled)

    mcp_config = tmp_path / "config.toml"
    mcp_config.write_text(
        '[mcp_servers.ai_engineering_memory.env]\n'
        'AI_MEMORY_MAINTAINER = "private-maintainer-name"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "inherited-but-not-used")
    configured = diagnostics._maintainer_check(mcp_config)
    assert configured["status"] == "not_exposed"
    assert configured["source"] == "mcp_tool_surface"
    assert "private-maintainer-name" not in json.dumps(configured)
    assert "inherited-but-not-used" not in json.dumps(configured)

    mcp_config.write_text(
        "[mcp_servers.ai_engineering_memory.env]\nAI_MEMORY_MAINTAINER = true\n",
        encoding="utf-8",
    )
    malformed = diagnostics._maintainer_check(mcp_config)
    assert malformed["status"] == "not_exposed"
    assert "AI_MEMORY_MAINTAINER" not in json.dumps(malformed)


def test_diagnostics_does_not_warn_for_unexposed_maintainer_actions(tmp_path, monkeypatch):
    """Не помечает готовой установку, где MCP не может менять статусы знаний и Skills."""
    database = tmp_path / "memory.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (5)")
    hook_config = tmp_path / "hooks.json"
    _write_hook_config(hook_config)
    mcp_config = tmp_path / "config.toml"
    mcp_config.write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        "command = 'python'\n"
        "args = ['-m', 'ai_engineering_memory.cli', 'mcp', 'serve']\n",
        encoding="utf-8",
    )
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(
        "\n".join(json.dumps({
            "event": event, "timestamp": "2026-10-23T10:00:00+00:00",
            "result": result, "duration_ms": 2,
        }) for event, result in (
            ("UserPromptSubmit", "success"), ("Stop", "no_verified_marker"),
        )) + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AI_MEMORY_MAINTAINER", raising=False)

    disabled = diagnostics.collect_diagnostics(
        database_path=database,
        hooks_config_path=hook_config,
        hook_log_path=hook_log,
        mcp_config_path=mcp_config,
    )

    assert disabled["overall"] == "ok"
    assert disabled["database"]["status"] == "ok"
    assert disabled["hooks"]["status"] == "ok"
    assert disabled["hook_log"]["status"] == "ok"
    assert disabled["maintainer_actions"]["status"] == "not_exposed"
    assert disabled["memory_mcp"]["status"] == "configured"

    monkeypatch.setenv("AI_MEMORY_MAINTAINER", "doctor-only-owner")
    inherited = diagnostics.collect_diagnostics(
        database_path=database,
        hooks_config_path=hook_config,
        hook_log_path=hook_log,
        mcp_config_path=mcp_config,
    )
    assert inherited["overall"] == "ok"
    assert inherited["maintainer_actions"]["status"] == "not_exposed"
    assert "doctor-only-owner" not in json.dumps(inherited)

    monkeypatch.delenv("AI_MEMORY_MAINTAINER", raising=False)
    mcp_config.write_text(
        '[mcp_servers.ai_engineering_memory]\n'
        "command = 'python'\n"
        "args = ['-m', 'ai_engineering_memory.cli', 'mcp', 'serve']\n"
        '[mcp_servers.ai_engineering_memory.env]\n'
        'AI_MEMORY_MAINTAINER = "configured-owner"\n',
        encoding="utf-8",
    )
    enabled = diagnostics.collect_diagnostics(
        database_path=database,
        hooks_config_path=hook_config,
        hook_log_path=hook_log,
        mcp_config_path=mcp_config,
    )
    assert enabled["overall"] == "ok"


def test_stale_database_schema_has_explicit_safe_recommendation(tmp_path):
    """Старая схема требует явного init, а read-only doctor её не мигрирует."""
    database = tmp_path / "old-memory.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (3)")

    report = diagnostics._database_check(database)

    assert report["status"] == "warning"
    assert report["schema_version"] == 3
    assert "ai-memory init" in report["recommendation"]
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 3


def test_read_only_database_check_creates_no_wal_sidecars(tmp_path):
    """Проверка immutable не создаёт SQLite coordination-файлы рядом с базой."""
    database = tmp_path / "wal-memory.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (5)")
    before = {path.name for path in tmp_path.iterdir()}

    report = diagnostics._database_check(database)

    after = {path.name for path in tmp_path.iterdir()}
    assert report["status"] == "ok"
    assert after == before


def test_active_wal_is_skipped_without_touching_database(tmp_path):
    """Непустой WAL не читается через immutable-снимок и не инициирует checkpoint."""
    database = tmp_path / "active-memory.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_migrations VALUES (5)")
    connection.commit()
    wal_path = tmp_path / "active-memory.db-wal"
    assert wal_path.stat().st_size > 0

    report = diagnostics._database_check(database)

    assert report["status"] == "warning"
    assert report["integrity"] == "skipped_active_wal"
    assert wal_path.stat().st_size > 0
    connection.close()


def test_diagnostics_does_not_echo_untrusted_log_values(tmp_path):
    """Неожиданные поля в JSONL не попадают в диагностический отчёт."""
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(json.dumps({
        "event": "Stop",
        "timestamp": "PRIVATE-TIMESTAMP",
        "result": "PRIVATE-RESULT",
        "source": "PRIVATE-SOURCE",
        "duration_ms": -1,
        "error_type": "PRIVATE-ERROR-TYPE",
    }), encoding="utf-8")

    report = diagnostics._hook_log_check(hook_log)

    record = report["events"]["Stop"]
    assert record == {
        "timestamp": None,
        "source": None,
        "result": None,
        "duration_ms": None,
        "learning_instruction_injected": None,
        "error_type": None,
    }
    assert report["missing_events"] == ["UserPromptSubmit"]
    assert report["status"] == "warning"
    assert "PRIVATE-SOURCE" not in json.dumps(report)


def test_diagnostics_warns_when_latest_hook_source_is_stale(tmp_path):
    """Последняя запись старой версии должна быть видна как устаревшая."""
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(
        "\n".join(json.dumps(record) for record in (
            {
                "event": "UserPromptSubmit",
                "timestamp": "2026-10-23T10:00:00+00:00",
                "source": "ai-engineering-memory:1.5.2",
                "result": "success",
            },
            {
                "event": "Stop",
                "timestamp": "2026-10-23T10:01:00+00:00",
                "source": codex_hooks.HOOK_SOURCE,
                "result": "no_marker_seen",
            },
        )) + "\n",
        encoding="utf-8",
    )

    report = diagnostics._hook_log_check(hook_log)

    assert report["expected_source"] == codex_hooks.HOOK_SOURCE
    assert report["stale_source_events"] == ["UserPromptSubmit"]
    assert report["status"] == "warning"


def test_stop_marker_results_are_valid_diagnostics(tmp_path):
    """Новые диагностические статусы Stop не выглядят ошибками или повреждением."""
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(
        "\n".join(json.dumps(record) for record in (
            {
                "event": "UserPromptSubmit",
                "timestamp": "2026-10-23T10:00:00+00:00",
                "source": codex_hooks.HOOK_SOURCE,
                "result": "success",
            },
            {
                "event": "Stop",
                "timestamp": "2026-10-23T10:01:00+00:00",
                "result": "marker_present_unverified",
            },
            {
                "event": "Stop",
                "timestamp": "2026-10-23T10:02:00+00:00",
                "result": "no_marker_seen",
            },
        )) + "\n",
        encoding="utf-8",
    )

    report = diagnostics._hook_log_check(hook_log)

    assert report["status"] == "ok"
    assert report["invalid_line_count"] == 0
    assert report["error_count"] == 0
    assert report["missing_events"] == []
    assert report["events"]["Stop"]["result"] == "no_marker_seen"


def test_recovered_hook_event_does_not_keep_current_status_in_warning(tmp_path):
    """Историческая ошибка остаётся видна, но успешный последний запуск снимает warning."""
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(
        "\n".join(json.dumps(record) for record in (
            {
                "event": "Stop",
                "timestamp": "2026-10-23T10:00:00+00:00",
                "result": "error",
                "error_type": "RuntimeError",
            },
            {
                "event": "Stop",
                "timestamp": "2026-10-23T10:01:00+00:00",
                "result": "no_verified_marker",
            },
            {
                "event": "UserPromptSubmit",
                "timestamp": "2026-10-23T10:02:00+00:00",
                "source": codex_hooks.HOOK_SOURCE,
                "result": "success",
            },
        )) + "\n",
        encoding="utf-8",
    )

    report = diagnostics._hook_log_check(hook_log)

    assert report["status"] == "ok"
    assert report["error_count"] == 1
    assert report["missing_events"] == []
    assert report["latest_error_events"] == []
    assert report["events"]["Stop"]["result"] == "no_verified_marker"


def test_hook_config_warns_when_launcher_path_is_missing(tmp_path):
    """Зарегистрированный hook с отсутствующим executable не считается готовым."""
    config = tmp_path / "hooks.json"
    hooks = {
        event: [{
            "hooks": [{
                "type": "command",
                "command": f'"{tmp_path / "removed" / "python.exe"}" '
                f"-m ai_engineering_memory.codex_hooks {event}",
            }],
        }]
        for event in ("UserPromptSubmit", "Stop")
    }
    config.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    result = diagnostics._hook_config_check(config)

    assert result["status"] == "warning"
    assert result["events"] == {"UserPromptSubmit": "ok", "Stop": "ok"}
    assert result["launchers"] == {"UserPromptSubmit": False, "Stop": False}
    assert result["launcher_matches_current_interpreter"] == {
        "UserPromptSubmit": False, "Stop": False,
    }


def test_hook_status_message_mismatch_does_not_mark_launch_config_broken(tmp_path):
    """A stale display label does not mean the hook command cannot run."""
    config = tmp_path / "hooks.json"
    _write_hook_config(config)
    document = json.loads(config.read_text(encoding="utf-8"))
    document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["statusMessage"] = (
        "older display label"
    )
    config.write_text(json.dumps(document), encoding="utf-8")

    result = diagnostics._hook_config_check(config)

    assert result["status"] == "ok"
    assert result["events"] == {"UserPromptSubmit": "ok", "Stop": "ok"}
    assert result["metadata_mismatch_events"] == ["UserPromptSubmit"]
    assert result["status_message_mismatch_events"] == ["UserPromptSubmit"]
    assert result["handler_metadata"]["UserPromptSubmit"] == {
        "timeout_matches_expected": True,
        "status_message_matches_expected": False,
    }


def test_hook_config_warns_when_event_is_registered_more_than_once(tmp_path):
    """РџРѕРєР°Р·С‹РІР°РµС‚ РґСѓР±Р»РёСЂСѓСЋС‰РёРµСЃСЏ Р·Р°РїСѓСЃРєРё hook-Р° РІ РґРёР°РіРЅРѕСЃС‚РёРєРµ."""
    config = tmp_path / "hooks.json"
    _write_hook_config(config)
    document = json.loads(config.read_text(encoding="utf-8"))
    first_registration = document["hooks"]["UserPromptSubmit"][0]
    document["hooks"]["UserPromptSubmit"].append(first_registration)
    config.write_text(json.dumps(document), encoding="utf-8")

    result = diagnostics._hook_config_check(config)

    assert result["status"] == "warning"
    assert result["events"] == {"UserPromptSubmit": "duplicate", "Stop": "ok"}
    assert result["registration_counts"] == {"UserPromptSubmit": 2, "Stop": 1}
    assert result["launchers"] == {"UserPromptSubmit": True, "Stop": True}


def test_doctor_cli_emits_json_report_without_initializing_database(
    tmp_path, monkeypatch, capsys
):
    """CLI показывает JSON-отчёт и возвращает успех только для готовой установки."""
    database = tmp_path / "memory.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (5)")
        connection.execute(
            "CREATE TABLE knowledge "
            "(id INTEGER PRIMARY KEY, namespace TEXT, status TEXT, title TEXT)"
        )
        connection.execute(
            "INSERT INTO knowledge(namespace, status, title) "
            "VALUES ('global', 'candidate', 'PRIVATE-TITLE')"
        )
        connection.execute("CREATE TABLE task_events (event_id TEXT, status TEXT)")
        connection.execute(
            "INSERT INTO task_events VALUES ('PRIVATE-EVENT-ID', 'pending_analysis')"
        )
        connection.execute("CREATE TABLE verification_records (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO verification_records DEFAULT VALUES")
        connection.execute(
            "CREATE TABLE learning_corroborations (id INTEGER PRIMARY KEY)"
        )
    before_database = database.read_bytes()
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    hook_config = codex_home / "hooks.json"
    _write_hook_config(hook_config)
    hook_document = json.loads(hook_config.read_text(encoding="utf-8"))
    hook_document["hooks"]["Stop"][0]["hooks"][0]["statusMessage"] = (
        "legacy queue verification status"
    )
    hook_config.write_text(json.dumps(hook_document), encoding="utf-8")
    (codex_home / "config.toml").write_text(
        "[mcp_servers.ai_engineering_memory]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]\n',
        encoding="utf-8",
    )
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(
        "\n".join(json.dumps(record) for record in (
            {
                "event": "UserPromptSubmit",
                "timestamp": "2026-10-23T10:00:00+00:00",
                "source": codex_hooks.HOOK_SOURCE,
                "result": "error",
                "duration_ms": 7,
                "error_type": "RuntimeError",
            },
            {
                "event": "UserPromptSubmit",
                "timestamp": "2026-10-23T10:00:30+00:00",
                "source": codex_hooks.HOOK_SOURCE,
                "result": "success",
                "duration_ms": 2,
                "learning_instruction_injected": True,
            },
            {
                "event": "Stop",
                "timestamp": "2026-10-23T10:01:00+00:00",
                "source": "ai-engineering-memory:1.5.2",
                "result": "marker_present_unverified",
                "duration_ms": 3,
            },
        )) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(storage, "DATABASE_PATH", database)
    monkeypatch.setenv("AI_MEMORY_DB", str(database))
    monkeypatch.setattr(codex_hooks, "_log_path", lambda: hook_log)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("AI_MEMORY_MAINTAINER", raising=False)

    assert cli.main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["overall"] == "warning"
    assert report["maintainer_actions"]["status"] == "not_exposed"
    assert report["hooks"]["metadata_mismatch_events"] == ["Stop"]
    assert report["hooks"]["handler_metadata"]["Stop"] == {
        "timeout_matches_expected": True,
        "status_message_matches_expected": False,
    }
    assert report["hook_log"]["events"]["Stop"]["result"] == "marker_present_unverified"
    assert report["hook_log"]["events"]["UserPromptSubmit"][
        "learning_instruction_injected"
    ] is True
    assert report["hook_log"]["error_count"] == 1
    assert report["hook_log"]["latest_error_events"] == []
    assert report["hook_log"]["stale_source_events"] == ["Stop"]
    assert report["database"]["knowledge_counts"] == {
        "total": 1,
        "status_counts": {"candidate": 1},
    }
    assert report["database"]["learning_counts"] == {
        "task_events": {
            "total": 1,
            "status_counts": {"pending_analysis": 1},
        },
        "verification_records": {"total": 1},
        "learning_corroborations": {"total": 0},
    }
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "not_exposed" in output
    assert "learning_instruction=enabled" in output
    assert (
        "Последний запуск UserPromptSubmit: 2026-10-23T10:00:30+00:00 · "
        "success · ai-engineering-memory:1.5.10 · 2 мс"
    ) in output
    assert (
        "Последний запуск Stop: 2026-10-23T10:01:00+00:00 · "
        "marker_present_unverified · ai-engineering-memory:1.5.2 · 3 мс"
    ) in output
    assert "ошибок в просмотренном фрагменте: 1" in output
    assert "Последний запуск завершился ошибкой" not in output
    assert "последнего наблюдавшегося запуска" in output
    assert "не доказывает, что текущая настройка устарела" in output
    assert "statusMessage" in output
    assert "не мешает запуску" in output
    assert "hooks install --dry-run" in output
    assert "Область проекта MCP: рабочий каталог процесса MCP" in output
    assert "Знания: всего 1 · candidate=1" in output
    assert "Обучение: событий 1 (pending_analysis=1) · проверок 1" in output
    assert "межпроектных подтверждений 0" in output
    assert "При смене workspace перезапустите MCP" in output
    assert "Stop не проверяет, запускалась ли" in output
    hook_log.write_text(json.dumps({
        "event": "Stop",
        "timestamp": "2026-10-23T10:02:00+00:00",
        "source": "ai-engineering-memory:1.5.8",
        "result": "no_marker_seen",
        "duration_ms": 1,
    }), encoding="utf-8")
    assert cli.main(["doctor"]) == 1
    assert "Stop отработал без маркера" in capsys.readouterr().out
    assert database.read_bytes() == before_database
