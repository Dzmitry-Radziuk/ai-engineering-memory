"""Настройки запуска и выбор локального хранилища без скрытой инициализации."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Неизменяемые настройки одного запуска приложения."""

    database_path: Path

    def __post_init__(self) -> None:
        """Проверяет и нормализует путь локальной SQLite-базы."""
        path = self.database_path.expanduser()
        if not path.is_absolute():
            raise ValueError("database_path должен быть абсолютным путём")
        object.__setattr__(self, "database_path", path)

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        repository_data_dir: Path | None = None,
    ) -> Settings:
        """Строит настройки по явному env, legacy-пути и системному каталогу данных."""
        values = os.environ if environ is None else environ
        configured = values.get("AI_MEMORY_DB", "").strip()
        if configured:
            return cls(Path(configured))

        if repository_data_dir is not None:
            legacy_path = repository_data_dir.expanduser() / "memory.db"
            if legacy_path.is_file():
                return cls(legacy_path)

        local_app_data = values.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return cls(Path(local_app_data) / "AIEngineeringMemory" / "memory.db")

        data_home = values.get("XDG_DATA_HOME", "").strip()
        if data_home:
            root = Path(data_home).expanduser()
        else:
            home = values.get("USERPROFILE") or values.get("HOME")
            root = (Path(home).expanduser() if home else Path.home()) / ".local" / "share"
        return cls(root / "AIEngineeringMemory" / "memory.db")
