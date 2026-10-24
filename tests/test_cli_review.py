"""Проверки безопасного терминального обзора кандидатов памяти."""

from __future__ import annotations

import builtins
import json
import subprocess

import pytest

from ai_engineering_memory import cli, learning, storage


class _Terminal:
    """Представляет терминал для проверки интерактивной команды."""

    def __init__(self, interactive: bool) -> None:
        """Сохраняет признак доступного интерактивного ввода."""
        self.interactive = interactive

    def isatty(self) -> bool:
        """Сообщает CLI, доступен ли интерактивный ввод."""
        return self.interactive


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Перенаправляет CLI в одноразовую SQLite-базу."""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DATABASE_PATH", tmp_path / "review.sqlite")
    monkeypatch.setenv("AI_MEMORY_DB", str(storage.DATABASE_PATH))
    storage.init_database()
    return tmp_path


def test_review_scopes_candidates_and_requires_exact_decisions(
    isolated_db,
    monkeypatch,
    capsys,
):
    """Обзор видит только global и текущий проект и журналирует решения."""
    project = isolated_db / "current"
    foreign_project = isolated_db / "foreign"
    project.mkdir()
    foreign_project.mkdir()
    for repository in (project, foreign_project):
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=repository,
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
    global_candidate = storage.create_knowledge(
        "backend",
        "Global candidate",
        "A shared operational finding.",
        namespace="global",
        evidence={"kind": "test", "result": "verified", "source": "verification:abc"},
    )
    local_candidate = storage.create_knowledge(
        "backend",
        "Project candidate",
        "A project-scoped finding.",
        namespace=learning.project_namespace(project),
    )
    foreign_candidate = storage.create_knowledge(
        "backend",
        "Foreign candidate",
        "Must remain isolated.",
        namespace=learning.project_namespace(foreign_project),
    )
    answers = iter(
        [
            "a",
            f"APPROVE {global_candidate['id']}",
            "r",
            f"REJECT {local_candidate['id']}",
            "Not reproducible in this project",
        ]
    )
    monkeypatch.setattr(cli.sys, "stdin", _Terminal(interactive=True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))

    assert cli.main(["knowledge", "review", "--cwd", str(project)]) == 0

    output = capsys.readouterr().out
    assert "Global candidate" in output
    assert "Project candidate" in output
    assert "Foreign candidate" not in output
    assert storage.get_knowledge(global_candidate["id"])["status"] == "approved"
    assert storage.get_knowledge(local_candidate["id"])["status"] == "rejected"
    assert storage.get_knowledge(foreign_candidate["id"])["status"] == "candidate"
    with storage._connect() as connection:
        decisions = connection.execute(
            "SELECT event_id,status_from,status_to,details FROM learning_event_audit "
            "WHERE knowledge_id IN (?,?) AND action='knowledge_status_changed' "
            "ORDER BY knowledge_id",
            (global_candidate["id"], local_candidate["id"]),
        ).fetchall()
    assert [row["event_id"] for row in decisions] == [
        f"knowledge:{global_candidate['id']}",
        f"knowledge:{local_candidate['id']}",
    ]
    assert (
        json.loads(decisions[1]["details"])["reason"]
        == "Not reproducible in this project"
    )


def test_review_refuses_noninteractive_input_without_changing_candidates(
    isolated_db,
    monkeypatch,
    capsys,
):
    """Пайп или фоновый запуск не могут утвердить candidate без человека."""
    candidate = storage.create_knowledge(
        "ops",
        "Candidate",
        "Await a maintainer decision.",
        namespace="global",
    )
    monkeypatch.setattr(cli.sys, "stdin", _Terminal(interactive=False))

    assert cli.main(["knowledge", "review"]) == 2

    assert storage.get_knowledge(candidate["id"])["status"] == "candidate"
    assert "только в терминале" in capsys.readouterr().err


def test_review_labels_learning_confidence_as_codex_estimate(
    isolated_db,
    monkeypatch,
    capsys,
):
    """Кандидат обучения не выдаёт самооценку модели за серверную уверенность."""
    storage.create_knowledge(
        "backend",
        "A repeated finding",
        "A candidate from cross-project learning.",
        namespace="global",
        source_type="cross_project_learning",
        confidence=0.8,
    )
    monkeypatch.setattr(cli.sys, "stdin", _Terminal(interactive=True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "s")

    assert cli.main(["knowledge", "review"]) == 0

    output = capsys.readouterr().out
    assert "оценка уверенности Codex: 0.8" in output
    assert "уверенность: 0.8" not in output


def test_review_escapes_terminal_controls_in_untrusted_candidate_text(
    isolated_db,
    monkeypatch,
    capsys,
):
    """ANSI и возврат каретки в кандидате не подменяют вывод подтверждения."""
    storage.create_knowledge(
        "backend",
        "Candidate\x1b[2J",
        "Text\rInjected title\x07\nРешение [a] утвердить, [r] отклонить",
        namespace="global",
        evidence={
            "kind": "check\x1b[31m",
            "result": "evidence\x1b[2K",
            "source": "verification:abc\rspoofed",
            "limitations": "none\x07",
        },
    )
    monkeypatch.setattr(cli.sys, "stdin", _Terminal(interactive=True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "s")

    assert cli.main(["knowledge", "review"]) == 0

    output = capsys.readouterr().out
    assert "Candidate\\x1b[2J" in output
    assert "Text\\rInjected title\\x07" in output
    assert "  | Решение [a] утвердить, [r] отклонить" in output
    assert "check\\x1b[31m" in output
    assert "evidence\\x1b[2K" in output
    assert "verification:abc\\rspoofed" in output
    assert "none\\x07" in output
    assert "\x1b" not in output
    assert "\x07" not in output
