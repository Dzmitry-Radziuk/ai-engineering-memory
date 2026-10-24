"""Безопасная регистрация команд Codex hooks в профиле пользователя."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

HOOK_EVENTS = ("UserPromptSubmit", "Stop")
HOOK_TIMEOUT_SECONDS = 5
HOOK_STATUS_MESSAGES = {
    "UserPromptSubmit": "Ищу подходящие утверждённые знания",
    "Stop": "Записываю диагностику завершения хода",
}


def default_config_path() -> Path:
    """Возвращает путь hooks.json для активного профиля Codex."""
    profile = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(profile).expanduser() / "hooks.json"


def _command_for_event(event: str, executable: str | None = None) -> str:
    """Собирает команду hook-а с корректным quoting для текущей ОС."""
    parts = [
        executable or sys.executable,
        "-m",
        "ai_engineering_memory.codex_hooks",
        event,
    ]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def _is_memory_command(value: Any, event: str) -> bool:
    """Определяет команду конкретного обработчика AI Engineering Memory."""
    return (
        isinstance(value, str)
        and f"-m ai_engineering_memory.codex_hooks {event}" in value
    )


def _hook_definition(event: str, command: str) -> dict[str, Any]:
    """Формирует запись Codex hook с коротким сообщением состояния."""
    return {
        "hooks": [{
            "type": "command",
            "command": command,
            "timeout": HOOK_TIMEOUT_SECONDS,
            "statusMessage": HOOK_STATUS_MESSAGES[event],
        }],
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Отклоняет повторяющиеся JSON-ключи вместо молчаливой потери значений."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("hooks.json содержит повторяющийся JSON-ключ")
        result[key] = value
    return result


def merge_hook_config(
    document: dict[str, Any], *, executable: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Добавляет или обновляет наши hooks, сохраняя остальные настройки."""
    if not isinstance(document, dict):
        raise TypeError("Корнем hooks.json должен быть JSON-объект")
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("Поле hooks в hooks.json должно быть объектом")

    changes: dict[str, list[str]] = {}

    def record_change(event: str, field: str) -> None:
        """Запоминает только название поля без значений пользовательского конфига."""
        event_changes = changes.setdefault(event, [])
        if field not in event_changes:
            event_changes.append(field)

    for event in HOOK_EVENTS:
        definitions = hooks.setdefault(event, [])
        if not isinstance(definitions, list):
            raise TypeError(f"Список {event} в hooks.json имеет неверный формат")
        command = _command_for_event(event, executable)
        kept_handler = False
        for definition in definitions:
            if not isinstance(definition, dict):
                raise TypeError(
                    f"Запись {event} в hooks.json должна быть объектом"
                )
            handlers = definition.get("hooks", [])
            if not isinstance(handlers, list):
                raise TypeError(
                    f"Поле hooks у записи {event} должно быть списком"
                )
            retained = []
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise TypeError(
                        f"Обработчик {event} в hooks.json должен быть объектом"
                    )
                if (
                    handler.get("type") == "command"
                    and _is_memory_command(handler.get("command"), event)
                ):
                    if kept_handler:
                        record_change(event, "duplicate_memory_handler_removed")
                        continue
                    if handler.get("command") != command:
                        handler["command"] = command
                        record_change(event, "command")
                    for key, value in (
                        ("timeout", HOOK_TIMEOUT_SECONDS),
                        ("statusMessage", HOOK_STATUS_MESSAGES[event]),
                    ):
                        if handler.get(key) != value:
                            handler[key] = value
                            record_change(event, key)
                    kept_handler = True
                retained.append(handler)
            if len(retained) != len(handlers):
                definition["hooks"] = retained
        if not kept_handler:
            definitions.append(_hook_definition(event, command))
            record_change(event, "registration_added")
    return document, changes


def _backup_path(path: Path) -> Path:
    """Выбирает имя резервной копии без перезаписи предыдущего файла."""
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{timestamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{timestamp}-{suffix}")
        suffix += 1
    return candidate


def install_hooks(
    config_path: str | Path | None = None, *, executable: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Устанавливает hooks атомарно и сохраняет исходную конфигурацию в backup."""
    path = Path(config_path).expanduser() if config_path else default_config_path()
    if path.is_symlink():
        path = path.resolve(strict=True)
    if path.exists() and not path.is_file():
        raise ValueError("Путь hooks.json существует, но не является файлом")
    existed = path.is_file()
    if existed:
        try:
            document = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("Не удалось безопасно прочитать hooks.json") from exc
    else:
        document = {}

    updated, changes = merge_hook_config(document, executable=executable)
    result: dict[str, Any] = {
        "path": str(path),
        "changed": bool(changes),
        "changes": changes,
        "dry_run": dry_run,
        "backup": None,
    }
    if not changes or dry_run:
        return result

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_path(path) if existed else None
    if backup:
        shutil.copy2(path, backup)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(updated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
        raise

    result["backup"] = str(backup) if backup else None
    return result
