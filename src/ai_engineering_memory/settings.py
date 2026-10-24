"""Настройки запуска и выбор локального хранилища без скрытой инициализации."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Неизменяемые настройки одного запуска приложения."""

    database_path: Path
    search_plugin: str | None = None
    analyzer_plugin: str | None = None
    profile: str | None = None
    data_root: Path | None = None

    def __post_init__(self) -> None:
        """Проверяет и нормализует путь локальной SQLite-базы."""
        path = self.database_path.expanduser()
        if not path.is_absolute():
            raise ValueError("database_path должен быть абсолютным путём")
        object.__setattr__(self, "database_path", path)
        if self.search_plugin is not None:
            selected_plugin = self.search_plugin.strip()
            object.__setattr__(self, "search_plugin", selected_plugin or None)
        if self.analyzer_plugin is not None:
            selected_plugin = self.analyzer_plugin.strip()
            object.__setattr__(self, "analyzer_plugin", selected_plugin or None)
        if self.profile is not None:
            selected_profile = self.profile.strip().casefold()
            if not re.fullmatch(
                r"[a-z0-9][a-z0-9._-]{0,63}", selected_profile
            ) or selected_profile.endswith("."):
                raise ValueError("profile должен быть коротким именем без путей")
            reserved_base = selected_profile.split(".", maxsplit=1)[0]
            if reserved_base in {
                "con",
                "prn",
                "aux",
                "nul",
                *(f"com{index}" for index in range(1, 10)),
                *(f"lpt{index}" for index in range(1, 10)),
            }:
                raise ValueError("Недопустимое зарезервированное имя profile")
            object.__setattr__(self, "profile", selected_profile)
        if self.data_root is not None:
            root = self.data_root.expanduser()
            if not root.is_absolute():
                raise ValueError("data_root должен быть абсолютным путём")
            object.__setattr__(self, "data_root", root)

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        repository_data_dir: Path | None = None,
    ) -> Settings:
        """Строит настройки по явному env, legacy-пути и системному каталогу данных."""
        values = os.environ if environ is None else environ
        local_app_data = values.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            data_root = Path(local_app_data) / "AIEngineeringMemory"
        else:
            data_home = values.get("XDG_DATA_HOME", "").strip()
            if data_home:
                root = Path(data_home).expanduser()
            else:
                home = values.get("USERPROFILE") or values.get("HOME")
                root = (
                    (Path(home).expanduser() if home else Path.home())
                    / ".local"
                    / "share"
                )
            data_root = root / "AIEngineeringMemory"

        configured = values.get("AI_MEMORY_DB", "").strip()
        profile = (
            values.get("AI_MEMORY_PROFILE", "").strip() or None
            if not configured
            else None
        )
        if configured:
            database_path = Path(configured)
        elif profile:
            profile_path = data_root / "profiles" / profile
            if not profile_path.resolve().is_relative_to(data_root.resolve()):
                raise ValueError("profile выходит за каталог профилей")
            database_path = profile_path / "memory.db"
        elif (
            repository_data_dir is not None
            and (repository_data_dir.expanduser() / "memory.db").is_file()
        ):
            legacy_path = repository_data_dir.expanduser() / "memory.db"
            database_path = legacy_path
        else:
            database_path = data_root / "memory.db"
        search_plugin = values.get("AI_MEMORY_SEARCH_PLUGIN", "").strip() or None
        analyzer_plugin = values.get("AI_MEMORY_ANALYZER_PLUGIN", "").strip() or None
        return cls(
            database_path,
            search_plugin=search_plugin,
            analyzer_plugin=analyzer_plugin,
            profile=profile,
            data_root=data_root,
        )
