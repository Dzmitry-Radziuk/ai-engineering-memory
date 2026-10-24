""Проверяет маршрут CLI через сервис памяти на временной базе."""

from __future__ import annotations

import json
import os
import subprocess
import sys

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


def test_cli_add_stays_candidate_and_is_hidden_from_approved_search(
    tmp_path, monkeypatch, capsys,
):
    """Новая запись остаётся кандидатом при добавлении через общий application layer."""
    monkeypatch.setenv("AI_MEMORY_DB", str(tmp_path / "cli.sqlite"))

    assert cli.main([
        "knowledge", "add",
        "--category", "backend",
        "--title", "Bound retry policy",
        "--content", "Limit retries and retain the final error.",
        "--namespace", "project:cli",
    ]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["status"] == "candidate"

    assert cli.main([
        "knowledge", "list", "--status", "candidate",
        "--namespace", "project:cli",
    ]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed] == [added["id"]]

    assert cli.main([
        "knowledge", "search", "bound retry",
        "--namespace", "project:cli",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert cli.main([
        "knowledge", "search", "bound retry",
        "--namespace", "project:cli", "--status", "candidate",
    ]) == 0
    candidates = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in candidates] == [added["id"]]


def test_cli_lists_skill_proposals_through_application_service(
    tmp_path, monkeypatch, capsys,
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
            "sample-skill", "agent", "Clarify one step", content,
        )

    assert cli.main(["skills", "proposals"]) == 0
    proposals = json.loads(capsys.readouterr().out)

    assert [item["id"] for item in proposals] == [proposal_id]
    assert "proposed_content" not in proposals[0]
