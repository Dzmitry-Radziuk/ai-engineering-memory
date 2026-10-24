"""Проверяет маршрут CLI через сервис памяти на временной базе."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ai_engineering_memory import cli, storage
from ai_engineering_memory.settings import Settings


def test_cli_help_uses_utf8_for_redirected_windows_output(tmp_path):
    """Перенаправленная русская справка CLI остаётся читаемой в PowerShell."""
    environment = os.environ.copy()
    environment["AI_MEMORY_DB"] = str(tmp_path / "cli-help.sqlite")
    result = subprocess.run(
        [sys.executable, "-m", "ai_engineering_memory.cli", "--help"],
        capture_output=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    assert "Локальная инженерная память".encode() in result.stdout
    assert not (tmp_path / "cli-help.sqlite").exists()


def test_global_json_format_emits_stable_init_result(tmp_path, monkeypatch, capsys):
    """Global JSON-режим возвращает объект для команды инициализации."""
    database_path = tmp_path / "json-init.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))

    assert cli.main(["--format", "json", "init"]) == 0

    assert json.loads(capsys.readouterr().out) == {"status": "ready"}
    assert database_path.exists()


def test_setup_prepares_database_and_reports_next_steps(tmp_path, monkeypatch, capsys):
    """Setup создаёт выбранную локальную базу и ясно называет следующие команды."""
    database_path = tmp_path / "setup.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))

    assert cli.main(["--format", "json", "setup"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "status": "ready",
        "next_steps": ["ai-memory hooks install", "ai-memory doctor"],
    }
    assert database_path.is_file()


def test_search_index_check_is_read_only_and_reports_missing_database(
    tmp_path, monkeypatch, capsys
):
    """Проверка FTS сообщает об отсутствующей базе и не создаёт её."""
    database_path = tmp_path / "missing-index.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))

    assert cli.main(["--format", "json", "search-index", "check"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "status": "database_missing",
        "backend": "uninitialized",
        "fts_available": False,
        "triggers_available": False,
    }
    assert not database_path.exists()


def test_search_index_rebuild_cannot_run_noninteractively_or_in_json_mode(
    tmp_path, monkeypatch, capsys
):
    """JSON не обходит human confirmation для перестроения FTS индекса."""
    database_path = tmp_path / "index-admin.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))

    assert cli.main(["--format", "json", "search-index", "rebuild"]) == 2

    assert json.loads(capsys.readouterr().err)["error"]["type"] == "ValueError"
    assert not database_path.exists()


def test_search_index_rebuild_requires_and_accepts_exact_human_confirmation(
    tmp_path, monkeypatch, capsys
):
    """Только явное подтверждение перестраивает индекс через CLI."""
    database_path = tmp_path / "confirmed-index.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))
    store = storage.create_storage()
    with store.activate():
        storage.init_database()

    class InteractiveInput:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", InteractiveInput())
    monkeypatch.setattr("builtins.input", lambda _prompt: "REBUILD SEARCH INDEX")

    assert cli.main(["search-index", "rebuild"]) == 0

    assert json.loads(capsys.readouterr().out)["status"] in {"rebuilt", "fallback"}


def test_knowledge_export_import_cli_roundtrip_keeps_import_unapproved(
    tmp_path, monkeypatch, capsys
):
    """CLI roundtrip экспортирует approved и импортирует только candidate."""
    database_path = tmp_path / "transfer.sqlite"
    export_path = tmp_path / "approved-export.json"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))
    store = storage.create_storage(Settings(database_path))
    with store.activate():
        knowledge = storage.create_knowledge(
            "backend",
            "Portable retry rule",
            "Use bounded retries and preserve the final error state.",
            publishable=True,
        )
        storage.approve_knowledge(knowledge["id"])

    assert (
        cli.main(
            [
                "--format",
                "json",
                "knowledge",
                "export",
                str(knowledge["id"]),
                "--output",
                str(export_path),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "exported"
    assert (
        cli.main(
            [
                "--format",
                "json",
                "knowledge",
                "import",
                str(export_path),
                "--namespace",
                "project:imported",
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)

    assert imported["status"] == "imported_as_candidates"
    with store.activate():
        record = storage.get_knowledge(imported["ids"][0])
    assert record["status"] == "candidate"
    assert record["namespace"] == "project:imported"


def test_profile_show_reports_selected_profile_without_creating_database(
    tmp_path, monkeypatch, capsys
):
    """Просмотр профиля только сообщает конфигурацию и не создаёт SQLite-файл."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user-data"))
    monkeypatch.setenv("AI_MEMORY_PROFILE", "chekhov")
    monkeypatch.delenv("AI_MEMORY_DB", raising=False)

    assert cli.main(["--format", "json", "profile", "show"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "chekhov"
    assert payload["selection"] == "profile"
    assert payload["database_exists"] is False
    assert not (
        tmp_path
        / "user-data"
        / "AIEngineeringMemory"
        / "profiles"
        / "chekhov"
        / "memory.db"
    ).exists()


def test_profile_list_shows_only_existing_profile_databases(
    tmp_path, monkeypatch, capsys
):
    """Список профилей не включает пустые каталоги и ничего не создаёт."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user-data"))
    monkeypatch.delenv("AI_MEMORY_PROFILE", raising=False)
    monkeypatch.delenv("AI_MEMORY_DB", raising=False)
    profiles = tmp_path / "user-data" / "AIEngineeringMemory" / "profiles"
    (profiles / "active").mkdir(parents=True)
    (profiles / "active" / "memory.db").touch()
    (profiles / "empty").mkdir()

    assert cli.main(["--format", "json", "profile", "list"]) == 0

    assert json.loads(capsys.readouterr().out) == [
        {"name": "active", "database_exists": True}
    ]
    assert not (profiles / "empty" / "memory.db").exists()


def test_global_json_format_emits_structured_errors(tmp_path, monkeypatch, capsys):
    """Ошибки команд в JSON-режиме остаются машиночитаемыми в stderr."""
    monkeypatch.setenv("AI_MEMORY_DB", str(tmp_path / "json-error.sqlite"))

    assert (
        cli.main(
            [
                "--format",
                "json",
                "knowledge",
                "search",
                "retry policy",
                "--limit",
                "0",
            ]
        )
        == 2
    )

    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    assert error["error"]["type"] == "StorageError"
    assert error["error"]["message"]


def test_global_json_format_emits_structured_argument_errors(capsys):
    """Ошибки argparse в JSON-режиме сохраняют ту же схему и exit code 2."""
    with pytest.raises(SystemExit) as captured:
        cli.main(["--format", "json", "unknown-command"])

    assert captured.value.code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    assert error["error"]["type"] == "ArgumentError"


def test_global_json_format_refuses_interactive_approval(tmp_path, monkeypatch, capsys):
    """JSON-режим не обходит интерактивную границу human approval."""
    monkeypatch.setenv("AI_MEMORY_DB", str(tmp_path / "json-admin.sqlite"))
    store = storage.create_storage(Settings(tmp_path / "json-admin.sqlite"))
    with store.activate():
        candidate = storage.create_knowledge(
            "backend",
            "Candidate awaiting review",
            "A project-scoped result that needs human review.",
            namespace="project:json",
        )

    assert (
        cli.main(["--format", "json", "knowledge", "approve", str(candidate["id"])])
        == 2
    )

    error = json.loads(capsys.readouterr().err)
    assert error["error"]["type"] == "ValueError"
    with store.activate():
        assert storage.get_knowledge(candidate["id"])["status"] == "candidate"


def test_cli_add_stays_candidate_and_is_hidden_from_approved_search(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Новая запись остаётся кандидатом при добавлении через общий application layer."""
    monkeypatch.setenv("AI_MEMORY_DB", str(tmp_path / "cli.sqlite"))

    assert (
        cli.main(
            [
                "knowledge",
                "add",
                "--category",
                "backend",
                "--title",
                "Bound retry policy",
                "--content",
                "Limit retries and retain the final error.",
                "--namespace",
                "project:cli",
            ]
        )
        == 0
    )
    added = json.loads(capsys.readouterr().out)
    assert added["status"] == "candidate"

    assert (
        cli.main(
            [
                "knowledge",
                "list",
                "--status",
                "candidate",
                "--namespace",
                "project:cli",
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed] == [added["id"]]

    assert (
        cli.main(
            [
                "knowledge",
                "search",
                "bound retry",
                "--namespace",
                "project:cli",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == []

    assert (
        cli.main(
            [
                "knowledge",
                "search",
                "bound retry",
                "--namespace",
                "project:cli",
                "--status",
                "candidate",
            ]
        )
        == 0
    )
    candidates = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in candidates] == [added["id"]]


def test_cli_lists_skill_proposals_through_application_service(
    tmp_path,
    monkeypatch,
    capsys,
):
    """CLI показывает только metadata proposal, не вмешиваясь в активные Skills."""
    database_path = tmp_path / "skills-cli.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(database_path))
    content = (
        "---\nname: sample-skill\ndescription: Sample\n---\n"
        "Untrusted proposed instructions\n"
    )
    store = storage.create_storage(Settings(database_path))
    with store.activate():
        proposal_id = store.propose_skill_update(
            "sample-skill",
            "agent",
            "Clarify one step",
            content,
        )

    assert cli.main(["skills", "proposals"]) == 0
    proposals = json.loads(capsys.readouterr().out)

    assert [item["id"] for item in proposals] == [proposal_id]
    assert "proposed_content" not in proposals[0]
