""Проверки резервного копирования и восстановления на временных базах."""

from __future__ import annotations

import builtins
import sqlite3

import pytest

from ai_engineering_memory import cli, storage
from ai_engineering_memory.settings import Settings


def _add(store, title: str) -> int:
    with store.activate():
        return int(store.create_knowledge(
            category="backend", title=title, content=f"Content for {title}",
            namespace="project:restore-test",
        )["id"])


def _titles(store) -> set[str]:
    with store.activate():
        return {item["title"] for item in store.list_knowledge(limit=100)}


def test_backup_and_restore_round_trip_keeps_pre_restore_snapshot(tmp_path):
    """Восстановление возвращает записи из backup и сохраняет текущее состояние отдельно."""
    active_path = tmp_path / "active.sqlite"
    store = storage.create_storage(Settings(active_path))
    original_id = _add(store, "From backup")
    with store.activate():
        backup_path = storage.backup_database(tmp_path / "backup.sqlite")
    _add(store, "Before restore")

    with store.activate():
        result = storage.restore_database(backup_path)

    assert _titles(store) == {"From backup"}
    assert store.get_knowledge(original_id)["title"] == "From backup"
    pre_restore = result["pre_restore_backup"]
    assert pre_restore
    with sqlite3.connect(pre_restore) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT title FROM knowledge WHERE title='Before restore'"
        ).fetchone() is not None


def test_restore_rejects_invalid_source_without_changing_active_database(tmp_path):
    """Повреждённый backup отклоняется до замены активной базы."""
    store = storage.create_storage(Settings(tmp_path / "active.sqlite"))
    _add(store, "Keep me")
    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(storage.StorageError):
        storage.restore_database(invalid)

    assert _titles(store) == {"Keep me"}


def test_backup_does_not_create_or_initialize_a_missing_active_database(tmp_path):
    """Backup остаётся read-only и не создаёт базу по отсутствующему пути."""
    database_path = tmp_path / "missing.sqlite"
    store = storage.create_storage(Settings(database_path))

    with store.activate(), pytest.raises(storage.StorageError):
        storage.backup_database(tmp_path / "backup.sqlite")

    assert not database_path.exists()


def test_restore_cli_requires_exact_interactive_confirmation(tmp_path, monkeypatch, capsys):
    """CLI отклоняет non-interactive и неверное подтверждение до записи."""
    active = tmp_path / "active.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB", str(active))
    store = storage.create_storage(Settings(active))
    _add(store, "Current")
    with store.activate():
        backup = storage.backup_database(tmp_path / "backup.sqlite")
    _add(store, "Current latest")

    class InteractiveInput:
        def isatty(self):
            return True

    class NonInteractiveInput:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", NonInteractiveInput())
    assert cli.main(["restore", str(backup)]) == 2
    assert _titles(store) == {"Current", "Current latest"}
    capsys.readouterr()

    monkeypatch.setattr(cli.sys, "stdin", InteractiveInput())
    monkeypatch.setattr(builtins, "input", lambda _prompt: "RESTORE wrong")
    assert cli.main(["restore", str(backup)]) == 1
    assert _titles(store) == {"Current", "Current latest"}
    capsys.readouterr()

    expected = f"RESTORE {backup.resolve()} -> {active.resolve()}"
    monkeypatch.setattr(builtins, "input", lambda _prompt: expected)
    assert cli.main(["restore", str(backup)]) == 0
    output = capsys.readouterr().out
    assert "pre_restore_backup" in output
    assert _titles(store) == {"Current"}
