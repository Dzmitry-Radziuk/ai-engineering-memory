"""Общая безопасная конфигурация запуска тестов."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ORIGINAL_DATABASE_ENV: str | None = None
_TEMPORARY_ROOT: tempfile.TemporaryDirectory[str] | None = None


def pytest_configure(config) -> None:
    """Перенаправляет импортируемый default SQLite путь в одноразовый каталог."""
    global _ORIGINAL_DATABASE_ENV, _TEMPORARY_ROOT

    _ORIGINAL_DATABASE_ENV = os.environ.get("AI_MEMORY_DB")
    basetemp = getattr(config.option, "basetemp", None)
    if basetemp:
        database_root = Path(basetemp).expanduser().resolve() / "test-database-guard"
        database_root.mkdir(parents=True, exist_ok=True)
    else:
        _TEMPORARY_ROOT = tempfile.TemporaryDirectory(prefix="ai-memory-pytest-")
        database_root = Path(_TEMPORARY_ROOT.name)
    os.environ["AI_MEMORY_DB"] = str(database_root / "memory.sqlite")


def pytest_unconfigure(config) -> None:
    """Восстанавливает окружение и удаляет только свою временную папку."""
    global _ORIGINAL_DATABASE_ENV, _TEMPORARY_ROOT

    if _ORIGINAL_DATABASE_ENV is None:
        os.environ.pop("AI_MEMORY_DB", None)
    else:
        os.environ["AI_MEMORY_DB"] = _ORIGINAL_DATABASE_ENV
    _ORIGINAL_DATABASE_ENV = None
    if _TEMPORARY_ROOT is not None:
        _TEMPORARY_ROOT.cleanup()
        _TEMPORARY_ROOT = None
