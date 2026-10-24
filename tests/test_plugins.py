"""Проверки безопасного discovery и загрузки расширений."""

from __future__ import annotations

from typing import Any

import pytest

from ai_engineering_memory import bootstrap
from ai_engineering_memory.plugin_api import PLUGIN_API_VERSION
from ai_engineering_memory.plugins import PluginLoadError, PluginRegistry


class FakeEntryPoint:
    """Имитирует установленные package entry points без импорта модулей."""

    def __init__(self, name: str, loader: Any) -> None:
        """Сохраняет имя расширения и контролируемую функцию загрузки."""
        self.name = name
        self._loader = loader
        self.load_calls = 0

    def load(self) -> Any:
        """Возвращает factory или поднимает заданную ошибку загрузки."""
        self.load_calls += 1
        return self._loader()


class ValidSearchPlugin:
    """Реализует минимальный контракт backend поиска версии 1."""

    plugin_api_version = PLUGIN_API_VERSION

    def search(self, query: Any) -> list[Any]:
        """Возвращает пустой результат для контрактного smoke test."""
        return []


class ValidAnalyzerPlugin:
    """Реализует analyzer SPI для bootstrap-проверки без внешней модели."""

    plugin_api_version = PLUGIN_API_VERSION

    def analyze(self, request: Any) -> Any:
        """Возвращает отсутствие предложения без изменения базы знаний."""
        return None


def test_available_lists_plugins_without_importing_them():
    """Обнаружение entry points не исполняет код незапрошенных plugin."""
    broken = FakeEntryPoint(
        "broken",
        lambda: (_ for _ in ()).throw(ImportError("private plugin detail")),
    )
    registry = PluginRegistry(lambda group: [broken])

    assert registry.available("ai_engineering_memory.search") == ("broken",)
    assert broken.load_calls == 0


def test_loading_selected_plugin_does_not_load_unselected_broken_plugin():
    """Registry импортирует только выбранный provider из зарегистрированной группы."""
    broken = FakeEntryPoint(
        "broken",
        lambda: (_ for _ in ()).throw(ImportError("private plugin detail")),
    )
    working = FakeEntryPoint("fts5", lambda: ValidSearchPlugin)
    registry = PluginRegistry(lambda group: [broken, working])

    plugin = registry.load("ai_engineering_memory.search", "fts5")

    assert isinstance(plugin, ValidSearchPlugin)
    assert broken.load_calls == 0
    assert working.load_calls == 1


def test_selected_broken_plugin_returns_actionable_redacted_error():
    """Ошибка выбранного plugin называет его и класс ошибки, скрывая её текст."""
    broken = FakeEntryPoint(
        "broken",
        lambda: (_ for _ in ()).throw(ImportError("private plugin detail")),
    )
    registry = PluginRegistry(lambda group: [broken])

    with pytest.raises(PluginLoadError) as captured:
        registry.load("ai_engineering_memory.search", "broken")

    assert "broken" in str(captured.value)
    assert "ImportError" in str(captured.value)
    assert "private plugin detail" not in str(captured.value)


def test_plugin_api_version_and_group_contract_are_required():
    """Registry отклоняет provider другой версии или без метода своей группы."""

    class UnsupportedVersion:
        plugin_api_version = PLUGIN_API_VERSION + 1

        def search(self, query: Any) -> list[Any]:
            """Поддерживает метод, чтобы проверялось именно несовпадение версии."""
            return []

    class WrongContract:
        plugin_api_version = PLUGIN_API_VERSION

    providers = {
        "old": FakeEntryPoint("old", lambda: UnsupportedVersion),
        "wrong": FakeEntryPoint("wrong", lambda: WrongContract),
    }
    registry = PluginRegistry(lambda group: list(providers.values()))

    with pytest.raises(PluginLoadError, match="использует API"):
        registry.load("ai_engineering_memory.search", "old")
    with pytest.raises(PluginLoadError, match="не реализует метод search"):
        registry.load("ai_engineering_memory.search", "wrong")


def test_plugin_registry_rejects_unknown_group_without_discovery():
    """Неизвестные группы не запускают общий поиск installed distributions."""
    discovery_calls = []
    registry = PluginRegistry(lambda group: discovery_calls.append(group) or [])

    with pytest.raises(PluginLoadError, match="Неподдерживаемая группа"):
        registry.available("arbitrary.group")
    assert discovery_calls == []


def test_default_bootstrap_does_not_load_any_plugin(monkeypatch, tmp_path):
    """Обычный startup не выполняет discovery и остаётся независимым от plugins."""
    monkeypatch.delenv("AI_MEMORY_SEARCH_PLUGIN", raising=False)
    monkeypatch.setenv("AI_MEMORY_DB", str(tmp_path / "default.sqlite"))
    monkeypatch.setattr(
        "ai_engineering_memory.bootstrap.PluginRegistry.load",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("plugin не должен загружаться по умолчанию")
        ),
    )

    service = bootstrap.create_memory_service()

    assert service._search_backend is None


def test_explicit_search_plugin_selection_injects_selected_backend(
    monkeypatch,
    tmp_path,
):
    """Переменная выбора подключает только явно названный search provider."""
    backend = ValidSearchPlugin()
    monkeypatch.setenv("AI_MEMORY_SEARCH_PLUGIN", "semantic-local")
    monkeypatch.setenv("AI_MEMORY_DB", str(tmp_path / "plugin.sqlite"))
    loaded = []

    def load_selected(registry, group, name):
        """Фиксирует запрошенную группу и имя без импорта установленного модуля."""
        loaded.append((group, name))
        return backend

    monkeypatch.setattr(
        "ai_engineering_memory.bootstrap.PluginRegistry.load",
        load_selected,
    )

    service = bootstrap.create_memory_service()

    assert loaded == [("ai_engineering_memory.search", "semantic-local")]
    assert service._search_backend is backend


def test_explicit_analyzer_plugin_selection_injects_selected_provider(
    monkeypatch,
    tmp_path,
):
    """Выбранный analyzer загружается только по явной настройке."""
    monkeypatch.setenv("AI_MEMORY_ANALYZER_PLUGIN", "local-analyzer")
    monkeypatch.setenv("AI_MEMORY_DB", str(tmp_path / "analyzer.sqlite"))
    loaded = []

    def load_selected(registry, group, name):
        loaded.append((group, name))
        return ValidAnalyzerPlugin()

    monkeypatch.setattr(
        "ai_engineering_memory.bootstrap.PluginRegistry.load",
        load_selected,
    )

    service = bootstrap.create_learning_service()

    assert loaded == [("ai_engineering_memory.analyzers", "local-analyzer")]
    assert service._analyzer_provider is not None


def test_registry_loads_real_python_package_entry_point(tmp_path, monkeypatch):
    """Обычная metadata entry point обнаруживается и создаёт typed provider."""
    distribution = tmp_path / "sample_search_plugin-0.1.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample-search-plugin\nVersion: 0.1\n",
        encoding="utf-8",
    )
    (distribution / "entry_points.txt").write_text(
        "[ai_engineering_memory.search]\n"
        "sample = sample_search_plugin:create_backend\n",
        encoding="utf-8",
    )
    (tmp_path / "sample_search_plugin.py").write_text(
        "from ai_engineering_memory.plugin_api import PLUGIN_API_VERSION\n"
        "class Backend:\n"
        "    plugin_api_version = PLUGIN_API_VERSION\n"
        "    def search(self, query):\n"
        "        return []\n"
        "def create_backend():\n"
        "    return Backend()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    registry = PluginRegistry()

    assert registry.available("ai_engineering_memory.search") == ("sample",)
    plugin = registry.load("ai_engineering_memory.search", "sample")
    assert plugin.plugin_api_version == PLUGIN_API_VERSION
