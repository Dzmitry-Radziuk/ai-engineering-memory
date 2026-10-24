""Проверки настроек и изоляции нескольких локальных баз в одном процессе."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_engineering_memory import storage
from ai_engineering_memory.database import Database, use_database
from ai_engineering_memory.settings import Settings
from ai_engineering_memory.storage import Storage, create_storage


def test_settings_resolve_explicit_database_path_at_call_time(tmp_path):
    """Явный путь берётся из текущего env и не зависит от импорта модуля."""
    first = Settings.from_environment(
        environ={"AI_MEMORY_DB": str(tmp_path / "first.sqlite")},
    )
    second = Settings.from_environment(
        environ={"AI_MEMORY_DB": str(tmp_path / "second.sqlite")},
    )

    assert first.database_path != second.database_path


def test_settings_reject_relative_database_path():
    """Относительная база отклоняется до создания файлов или каталогов."""
    with pytest.raises(ValueError, match="абсолютным"):
        Settings.from_environment(environ={"AI_MEMORY_DB": "relative.sqlite"})


def test_two_storage_instances_remain_isolated_in_one_process(tmp_path):
    """Два facade используют только назначенные им базы при чередовании вызовов."""
    first = create_storage(Settings(tmp_path / "first.sqlite"))
    second = create_storage(Settings(tmp_path / "second.sqlite"))
    first.init_database()
    second.init_database()

    first_item = first.create_knowledge("test", "First database", "first only")
    second_item = second.create_knowledge("test", "Second database", "second only")

    assert first.get_knowledge(first_item["id"])["title"] == "First database"
    assert second.get_knowledge(second_item["id"])["title"] == "Second database"
    assert second.search_knowledge("first only", status=None) == []
    assert first.search_knowledge("second only", status=None) == []


def test_database_context_is_restored_after_nested_scope(tmp_path):
    """Вложенная подмена базы восстанавливает внешний context после выхода."""
    first = Database(tmp_path / "outer.sqlite")
    second = Database(tmp_path / "inner.sqlite")

    with use_database(first):
        assert storage.current_database_path() == first.path
        with use_database(second):
            assert storage.current_database_path() == second.path
        assert storage.current_database_path() == first.path


def test_storage_facades_do_not_share_database_between_threads(tmp_path):
    """Параллельные вызовы разных facade не переносят контекст базы между потоками."""
    stores = [
        create_storage(Settings(tmp_path / "left.sqlite")),
        create_storage(Settings(tmp_path / "right.sqlite")),
    ]
    for store in stores:
        store.init_database()

    def write(store_and_title: tuple[Storage, str]) -> str:
        store, title = store_and_title
        store.create_knowledge("test", title, f"content for {title}")
        return store.database_path.name

    with ThreadPoolExecutor(max_workers=2) as executor:
        names = list(executor.map(write, zip(stores, ("Left", "Right"))))

    assert names == ["left.sqlite", "right.sqlite"]
    assert stores[0].search_knowledge("Left", status=None)
    assert stores[0].search_knowledge("Right", status=None) == []
    assert stores[1].search_knowledge("Right", status=None)
    assert stores[1].search_knowledge("Left", status=None) == []
