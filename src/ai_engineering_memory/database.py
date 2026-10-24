""Изолированные SQLite-соединения и контекст выбора базы для одного вызова."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Database:
    """Параметры локальной базы; соединения создаются только на время операции."""

    path: Path

    def __post_init__(self) -> None:
        """Проверяет абсолютный путь, не создавая каталогов или файлов."""
        path = self.path.expanduser()
        if not path.is_absolute():
            raise ValueError("Database.path должен быть абсолютным путём")
        object.__setattr__(self, "path", path)

    @contextmanager
    def connect(
        self, *, read_only: bool = False, timeout: float = 10.0,
    ) -> Iterator[sqlite3.Connection]:
        """Открывает отдельное SQLite-соединение с включёнными внешними ключами."""
        if read_only:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=timeout)
        else:
            connection = sqlite3.connect(self.path, timeout=timeout)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
            with connection:
                yield connection
        finally:
            connection.close()


_active_database: ContextVar[Database | None] = ContextVar(
    "ai_engineering_memory_database", default=None,
)


@contextmanager
def use_database(database: Database) -> Iterator[None]:
    """Изолирует выбранную базу в текущем execution context и восстанавливает её."""
    token: Token[Database | None] = _active_database.set(database)
    try:
        yield
    finally:
        _active_database.reset(token)


def current_database(default_path: Path) -> Database:
    """Возвращает контекстную базу или legacy default без общего соединения."""
    return _active_database.get() or Database(default_path)


def active_database() -> Database | None:
    """Возвращает явно внедрённую базу текущего execution context."""
    return _active_database.get()
