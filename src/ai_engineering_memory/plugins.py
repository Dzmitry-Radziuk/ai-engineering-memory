"""Явная загрузка сторонних расширений через Python package entry points."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from importlib.metadata import EntryPoint, entry_points
from typing import Any

from ai_engineering_memory.plugin_api import PLUGIN_API_VERSION

PLUGIN_GROUPS = frozenset(
    {
        "ai_engineering_memory.search",
        "ai_engineering_memory.analyzers",
        "ai_engineering_memory.importers",
        "ai_engineering_memory.exporters",
    }
)
_GROUP_METHODS = {
    "ai_engineering_memory.search": "search",
    "ai_engineering_memory.analyzers": "analyze",
    "ai_engineering_memory.importers": "import_records",
    "ai_engineering_memory.exporters": "export_records",
}


class PluginLoadError(RuntimeError):
    """Сообщает о выбранном расширении, которое нельзя безопасно загрузить."""


class PluginRegistry:
    """Перечисляет расширения и загружает только явно выбранные пользователем."""

    def __init__(
        self,
        discover: Callable[[str], Iterable[EntryPoint]] | None = None,
    ) -> None:
        """Принимает discovery-функцию для изоляции и тестирования registry."""
        self._discover = discover or (lambda group: entry_points(group=group))

    def available(self, group: str) -> tuple[str, ...]:
        """Возвращает имена зарегистрированных расширений без их импорта."""
        self._validate_group(group)
        try:
            return tuple(sorted(point.name for point in self._discover(group)))
        except Exception as exc:
            raise PluginLoadError(
                f"Не удалось прочитать entry points группы {group}: {type(exc).__name__}"
            ) from exc

    def load(self, group: str, name: str) -> Any:
        """Загружает выбранный plugin и проверяет API version и обязательный метод."""
        self._validate_group(group)
        try:
            matches = [point for point in self._discover(group) if point.name == name]
        except Exception as exc:
            raise PluginLoadError(
                f"Не удалось найти plugin {name!r}: {type(exc).__name__}"
            ) from exc
        if not matches:
            raise PluginLoadError(f"Plugin {name!r} не найден в группе {group}")
        if len(matches) != 1:
            raise PluginLoadError(f"Имя plugin {name!r} зарегистрировано несколько раз")

        try:
            factory = matches[0].load()
            plugin = factory()
        except Exception as exc:
            raise PluginLoadError(
                f"Plugin {name!r} завершился ошибкой загрузки: {type(exc).__name__}"
            ) from exc

        if type(getattr(plugin, "plugin_api_version", None)) is not int:
            raise PluginLoadError(f"Plugin {name!r} не объявил целую версию API")
        if plugin.plugin_api_version != PLUGIN_API_VERSION:
            raise PluginLoadError(
                f"Plugin {name!r} использует API {plugin.plugin_api_version}; "
                f"поддерживается {PLUGIN_API_VERSION}"
            )
        method = _GROUP_METHODS[group]
        if not callable(getattr(plugin, method, None)):
            raise PluginLoadError(
                f"Plugin {name!r} не реализует метод {method} для {group}"
            )
        return plugin

    @staticmethod
    def _validate_group(group: str) -> None:
        """Ограничивает discovery четырьмя документированными API группами."""
        if group not in PLUGIN_GROUPS:
            raise PluginLoadError(f"Неподдерживаемая группа plugin: {group}")
