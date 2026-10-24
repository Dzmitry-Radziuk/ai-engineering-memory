"""Проверки настроек и изоляции нескольких локальных баз в одном процессе."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_engineering_memory import storage
from ai_engineering_memory.database import Database, use_database
from ai_engineering_memory.settings import Settings
from ai_engineering_memory.storage import Storage, create_storage


def test_pytest_default_database_is_not_the_repository_database():
    """Путь БД по умолчанию для тестов изолирован от данных репозитория."""
    assert storage.DATABASE_PATH != storage.DATA_DIR / "memory.db"
    assert storage.DATABASE_PATH.is_absolute()


def test_settings_resolve_explicit_database_path_at_call_time(tmp_path):
    """Явный путь берётся из текущего env и не зависит от импорта модуля."""
    first = Settings.from_environment(
        environ={"AI_MEMORY_DB": str(tmp_path / "first.sqlite")},
    )
    second = Settings.from_environment(
        environ={"AI_MEMORY_DB": str(tmp_path / "second.sqlite")},
    )

    assert first.database_path != second.database_path


def test_named_profiles_use_separate_paths_and_take_priority_over_legacy_database(
    tmp_path,
):
    """Named profiles select isolated databases before the legacy repository file."""
    legacy_root = tmp_path / "repository-data"
    legacy_root.mkdir()
    (legacy_root / "memory.db").touch()
    data_root = tmp_path / "user-data"

    first = Settings.from_environment(
        environ={
            "LOCALAPPDATA": str(data_root),
            "AI_MEMORY_PROFILE": "project-a",
        },
        repository_data_dir=legacy_root,
    )
    second = Settings.from_environment(
        environ={
            "LOCALAPPDATA": str(data_root),
            "AI_MEMORY_PROFILE": "project-b",
        },
        repository_data_dir=legacy_root,
    )

    assert first.profile == "project-a"
    assert (
        first.database_path
        == data_root / "AIEngineeringMemory" / "profiles" / "project-a" / "memory.db"
    )
    assert first.database_path != second.database_path
    assert not first.database_path.exists()
    assert (legacy_root / "memory.db").is_file()


def test_explicit_database_path_overrides_named_profile(tmp_path):
    """AI_MEMORY_DB remains the highest-priority database selector."""
    explicit = tmp_path / "explicit.sqlite"
    settings = Settings.from_environment(
        environ={
            "LOCALAPPDATA": str(tmp_path / "user-data"),
            "AI_MEMORY_PROFILE": "project-a",
            "AI_MEMORY_DB": str(explicit),
        }
    )

    assert settings.database_path == explicit
    assert settings.profile is None
    assert not explicit.exists()


@pytest.mark.parametrize(
    "profile", ["../other", "CON", "con.log", "trailing.", "bad/name", "has space"]
)
def test_settings_reject_unsafe_profile_names(profile):
    """Имена профилей не допускают пути, пробелы и зарезервированные имена Windows."""
    with pytest.raises(ValueError, match="profile"):
        Settings.from_environment(environ={"AI_MEMORY_PROFILE": profile})


def test_explicit_database_ignores_invalid_lower_priority_profile(tmp_path):
    """Явная AI_MEMORY_DB не затрагивается некорректной неиспользуемой настройкой профиля."""
    settings = Settings.from_environment(
        environ={
            "AI_MEMORY_DB": str(tmp_path / "explicit.sqlite"),
            "AI_MEMORY_PROFILE": "../ignored",
        }
    )

    assert settings.profile is None
    assert settings.database_path == tmp_path / "explicit.sqlite"


def test_settings_select_optional_search_plugin_from_environment(tmp_path):
    """Выбор plugin читается одновременно с настройками пути базы."""
    settings = Settings.from_environment(
        environ={
            "AI_MEMORY_DB": str(tmp_path / "memory.sqlite"),
            "AI_MEMORY_SEARCH_PLUGIN": "  semantic-local  ",
            "AI_MEMORY_ANALYZER_PLUGIN": "  local-analyzer  ",
        }
    )

    assert settings.search_plugin == "semantic-local"
    assert settings.analyzer_plugin == "local-analyzer"


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


def test_named_profile_storages_keep_project_data_isolated(tmp_path):
    """Два выбранных профиля получают отдельные SQLite-хранилища и не пересекают записи."""
    root = tmp_path / "user-data"
    first = create_storage(
        Settings.from_environment(
            environ={
                "LOCALAPPDATA": str(root),
                "AI_MEMORY_PROFILE": "project-a",
            }
        )
    )
    second = create_storage(
        Settings.from_environment(
            environ={
                "LOCALAPPDATA": str(root),
                "AI_MEMORY_PROFILE": "project-b",
            }
        )
    )
    first.init_database()
    second.init_database()

    first_item = first.create_knowledge("test", "Project A", "private A")
    second_item = second.create_knowledge("test", "Project B", "private B")

    assert first.database_path != second.database_path
    assert first.get_knowledge(first_item["id"])["title"] == "Project A"
    assert second.get_knowledge(second_item["id"])["title"] == "Project B"
    assert first.search_knowledge("private B", status=None) == []
    assert second.search_knowledge("private A", status=None) == []
