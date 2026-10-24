"""Проверки безопасной генерации проектной конфигурации Codex."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from ai_engineering_memory import cli, storage


def test_project_config_prints_valid_scoped_mcp_settings_without_writes(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Печатает валидный MCP-блок для проекта и не создаёт в нём файлы."""
    project_root = tmp_path / "sample project"
    project_root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(project_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    database_path = tmp_path / "must-not-be-created.sqlite"
    monkeypatch.setattr(storage, "DATABASE_PATH", database_path)
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))

    assert cli.main(["project-config", "--cwd", str(project_root)]) == 0

    output = capsys.readouterr().out
    config = tomllib.loads(output)
    server = config["mcp_servers"]["ai_engineering_memory"]
    assert server["command"] == str(Path(sys.executable).resolve())
    assert server["args"] == ["-m", "ai_engineering_memory.cli", "mcp", "serve"]
    assert server["env"]["AI_MEMORY_PROJECT_ROOT"] == str(project_root.resolve())
    assert not database_path.exists()
    assert not (project_root / ".codex").exists()
