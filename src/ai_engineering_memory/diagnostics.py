"""Диагностика локальной базы и hooks Codex только для чтения."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ai_engineering_memory.codex_hooks import HOOK_SOURCE
from ai_engineering_memory.hook_install import (
    HOOK_STATUS_MESSAGES,
    HOOK_TIMEOUT_SECONDS,
)
from ai_engineering_memory.storage import LATEST_SCHEMA_VERSION

MAX_LOG_BYTES = 256_000
MAX_LOG_RECORDS = 500
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_SAFE_HOOK_SOURCE = re.compile(r"^ai-engineering-memory:\d+\.\d+\.\d+$")
_HOOK_EVENTS = ("UserPromptSubmit", "Stop")
_COMMAND_HEAD = re.compile(r'^\s*(?:"([^"]+)"|\'([^\']+)\'|(\S+))')


def _command_launcher_exists(command: str) -> bool:
    """Проверяет наличие executable из начала команды, не запуская её."""
    match = _COMMAND_HEAD.match(command)
    if not match:
        return False
    executable = next((part for part in match.groups() if part), "")
    if not executable:
        return False
    path = Path(executable).expanduser()
    if path.is_absolute() or "\\" in executable or "/" in executable:
        return path.is_file()
    return shutil.which(executable) is not None


def _command_matches_current_interpreter(command: str) -> bool:
    """Сверяет launcher команды с Python текущей диагностики без её запуска."""
    match = _COMMAND_HEAD.match(command)
    if not match:
        return False
    executable = next((part for part in match.groups() if part), "")
    if not executable:
        return False
    candidate = Path(executable).expanduser()
    if not candidate.is_absolute() and "\\" not in executable and "/" not in executable:
        located = shutil.which(executable)
        if not located:
            return False
        candidate = Path(located)
    try:
        return os.path.normcase(str(candidate.resolve())) == os.path.normcase(
            str(Path(sys.executable).resolve())
        )
    except OSError:
        return False


def _database_check(path: Path) -> dict[str, Any]:
    """Проверяет существующую SQLite-базу без её создания или изменения."""
    result: dict[str, Any] = {
        "status": "missing",
        "path": str(path),
        "integrity": None,
        "schema_version": None,
        "database_size_bytes": None,
        "wal_size_bytes": None,
        "fts_available": None,
    }
    if not path.is_file():
        return result

    wal_path = Path(f"{path}-wal")
    try:
        result["database_size_bytes"] = path.stat().st_size
        wal_stat = wal_path.stat() if wal_path.exists() else None
        wal_before = (wal_stat.st_size, wal_stat.st_mtime_ns) if wal_stat else None
        result["wal_size_bytes"] = wal_before[0] if wal_before else 0
    except OSError as exc:
        result["status"] = "error"
        result["error_type"] = type(exc).__name__
        return result
    if wal_before and wal_before[0] > 0:
        result.update(
            {
                "status": "warning",
                "integrity": "skipped_active_wal",
                "recommendation": "SQLite WAL is active; integrity check was skipped to keep this command read-only.",
            }
        )
        return result

    try:
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            result["integrity"] = str(integrity)
            has_migrations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='schema_migrations'"
            ).fetchone()
            if has_migrations:
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                result["schema_version"] = version

            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            result["fts_available"] = "knowledge_fts" in tables
            if "knowledge" in tables:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(knowledge)")
                }
                if "status" in columns:
                    counts = connection.execute(
                        "SELECT status, COUNT(*) FROM knowledge "
                        "GROUP BY status ORDER BY status"
                    ).fetchall()
                    status_counts = {str(row[0]): int(row[1]) for row in counts}
                    result["knowledge_counts"] = {
                        "total": sum(status_counts.values()),
                        "status_counts": status_counts,
                    }

            learning_counts: dict[str, Any] = {}
            for table in (
                "task_events",
                "verification_records",
                "learning_corroborations",
            ):
                if table not in tables:
                    continue
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                total = int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                entry: dict[str, Any] = {"total": total}
                if "status" in columns:
                    counts = connection.execute(
                        f"SELECT status, COUNT(*) FROM {table} "
                        "GROUP BY status ORDER BY status"
                    ).fetchall()
                    entry["status_counts"] = {
                        str(row[0]): int(row[1]) for row in counts
                    }
                learning_counts[table] = entry
            if learning_counts:
                result["learning_counts"] = learning_counts

        wal_stat = wal_path.stat() if wal_path.exists() else None
        wal_after = (wal_stat.st_size, wal_stat.st_mtime_ns) if wal_stat else None
        if wal_before != wal_after:
            result.update(
                {
                    "status": "warning",
                    "integrity": "snapshot_changed",
                    "recommendation": "SQLite files changed during diagnosis; rerun when no writer is active.",
                }
            )
            return result

        if integrity != "ok":
            result["status"] = "error"
        elif result["schema_version"] != LATEST_SCHEMA_VERSION:
            result["status"] = "warning"
            result["recommendation"] = (
                "Run `ai-memory init` to apply additive migrations."
            )
        else:
            result["status"] = "ok"
    except (OSError, sqlite3.Error, ValueError) as exc:
        result["status"] = "error"
        result["error_type"] = type(exc).__name__
    return result


def _hook_config_check(path: Path) -> dict[str, Any]:
    """Проверяет наличие команд обоих hooks в Codex-конфиге."""
    result: dict[str, Any] = {
        "status": "missing",
        "path": str(path),
        "events": {},
        "registration_counts": {},
        "launchers": {},
        "launcher_matches_current_interpreter": {},
        "handler_metadata": {},
        "metadata_mismatch_events": [],
        "status_message_mismatch_events": [],
    }
    if not path.is_file():
        return result

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result["status"] = "error"
        result["error_type"] = type(exc).__name__
        return result

    hooks = document.get("hooks") if isinstance(document, dict) else None
    for event in _HOOK_EVENTS:
        definitions = hooks.get(event, []) if isinstance(hooks, dict) else []
        handlers: list[dict[str, Any]] = []
        if isinstance(definitions, list):
            for definition in definitions:
                definition_handlers = (
                    definition.get("hooks", []) if isinstance(definition, dict) else []
                )
                if isinstance(definition_handlers, list):
                    handlers.extend(
                        handler
                        for handler in definition_handlers
                        if isinstance(handler, dict)
                        and handler.get("type") == "command"
                        and isinstance(handler.get("command"), str)
                    )
        expected = f"-m ai_engineering_memory.codex_hooks {event}"
        matching_handlers = [
            handler for handler in handlers if expected in handler["command"]
        ]
        matching_commands = [handler["command"] for handler in matching_handlers]
        result["registration_counts"][event] = len(matching_commands)
        result["events"][event] = (
            "missing"
            if not matching_commands
            else "duplicate"
            if len(matching_commands) > 1
            else "ok"
        )
        result["launchers"][event] = (
            any(_command_launcher_exists(command) for command in matching_commands)
            if matching_commands
            else False
        )
        result["launcher_matches_current_interpreter"][event] = (
            any(
                _command_matches_current_interpreter(command)
                for command in matching_commands
            )
            if matching_commands
            else False
        )
        timeout_matches = bool(matching_handlers) and all(
            type(handler.get("timeout")) is int
            and handler["timeout"] == HOOK_TIMEOUT_SECONDS
            for handler in matching_handlers
        )
        status_matches = bool(matching_handlers) and all(
            handler.get("statusMessage") == HOOK_STATUS_MESSAGES[event]
            for handler in matching_handlers
        )
        result["handler_metadata"][event] = {
            "timeout_matches_expected": timeout_matches,
            "status_message_matches_expected": status_matches,
        }
        if not status_matches:
            result["status_message_mismatch_events"].append(event)
        if not timeout_matches or not status_matches:
            result["metadata_mismatch_events"].append(event)

    result["status"] = (
        "ok"
        if all(value == "ok" for value in result["events"].values())
        and all(result["launchers"].values())
        and all(
            metadata["timeout_matches_expected"]
            for metadata in result["handler_metadata"].values()
        )
        else "warning"
    )
    return result


def _hook_log_check(path: Path) -> dict[str, Any]:
    """Читает ограниченный хвост журнала и оставляет только безопасную метаинформацию."""
    result: dict[str, Any] = {
        "status": "missing",
        "events": {},
        "missing_events": list(_HOOK_EVENTS),
        "expected_source": HOOK_SOURCE,
        "stale_source_events": [],
        "error_count": 0,
        "invalid_line_count": 0,
        "duration_summary_ms": None,
        "search_duration_summary_ms": None,
    }
    if not path.is_file():
        return result

    try:
        with path.open("rb") as stream:
            offset = max(0, path.stat().st_size - MAX_LOG_BYTES)
            stream.seek(offset)
            if offset:
                stream.readline()
            tail = stream.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
    except OSError as exc:
        result["status"] = "error"
        result["error_type"] = type(exc).__name__
        return result

    records = []
    lines = tail.splitlines()
    if len(lines) > MAX_LOG_RECORDS:
        lines = lines[-MAX_LOG_RECORDS:]
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            result["invalid_line_count"] += 1
            continue
        if not isinstance(record, dict) or record.get("event") not in _HOOK_EVENTS:
            result["invalid_line_count"] += 1
            continue
        records.append(record)

    hook_durations: list[int] = []
    search_durations: list[int] = []
    for record in records:
        event = record["event"]
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and len(timestamp) <= 64:
            try:
                datetime.fromisoformat(timestamp)
            except ValueError:
                timestamp = None
        else:
            timestamp = None
        duration_ms = record.get("duration_ms")
        if (
            not isinstance(duration_ms, int)
            or isinstance(duration_ms, bool)
            or not 0 <= duration_ms <= 86_400_000
        ):
            duration_ms = None
        else:
            hook_durations.append(duration_ms)
        search_duration_ms = record.get("search_duration_ms")
        if (
            isinstance(search_duration_ms, int)
            and not isinstance(search_duration_ms, bool)
            and 0 <= search_duration_ms <= 86_400_000
            and event == "UserPromptSubmit"
        ):
            search_durations.append(search_duration_ms)
        else:
            search_duration_ms = None
        source = record.get("source")
        safe_source = (
            source
            if isinstance(source, str) and _SAFE_HOOK_SOURCE.fullmatch(source)
            else None
        )
        result["events"][event] = {
            "timestamp": timestamp,
            "source": safe_source,
            "result": record.get("result")
            if record.get("result")
            in {
                "success",
                "captured",
                "verification_required",
                "no_verified_marker",
                "error",
                "marker_present_unverified",
                "no_marker_seen",
            }
            else None,
            "duration_ms": duration_ms,
            "search_duration_ms": search_duration_ms,
            "learning_instruction_injected": (
                record.get("learning_instruction_injected")
                if isinstance(record.get("learning_instruction_injected"), bool)
                else None
            ),
            "error_type": (
                record.get("error_type")
                if isinstance(record.get("error_type"), str)
                and _SAFE_ERROR_TYPE.fullmatch(record["error_type"])
                else None
            ),
        }
        if record.get("result") == "error":
            result["error_count"] += 1

    def summarize_durations(values: list[int]) -> dict[str, int | float] | None:
        if not values:
            return None
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "min_ms": ordered[0],
            "mean_ms": round(sum(ordered) / len(ordered), 2),
            "p95_ms": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
            "max_ms": ordered[-1],
        }

    result["duration_summary_ms"] = summarize_durations(hook_durations)
    result["search_duration_summary_ms"] = summarize_durations(search_durations)

    result["missing_events"] = [
        event for event in _HOOK_EVENTS if event not in result["events"]
    ]
    latest_error_events = [
        event
        for event, details in result["events"].items()
        if details["result"] == "error"
    ]
    result["latest_error_events"] = latest_error_events
    result["stale_source_events"] = [
        event
        for event, details in result["events"].items()
        if details["source"] is not None and details["source"] != HOOK_SOURCE
    ]
    has_unrecognized_result = any(
        details["result"] is None for details in result["events"].values()
    )
    has_unrecognized_source = any(
        record.get("source") is not None
        and not (
            isinstance(record.get("source"), str)
            and _SAFE_HOOK_SOURCE.fullmatch(record["source"])
        )
        for record in records
    )
    result["status"] = (
        "ok"
        if records
        and not result["missing_events"]
        and not result["invalid_line_count"]
        and not latest_error_events
        and not result["stale_source_events"]
        and not has_unrecognized_result
        and not has_unrecognized_source
        else "warning"
    )
    return result


def _effective_mcp_config(
    mcp_config_path: Path | None,
    project_mcp_config_path: Path | Sequence[Path] | None = None,
) -> tuple[dict[str, Any] | None, str | None, str]:
    """Объединяет user и project MCP-слои без изменения файлов."""
    effective: dict[str, Any] = {}
    loaded = False
    project_has_server = False
    project_paths = (
        (project_mcp_config_path,)
        if isinstance(project_mcp_config_path, Path)
        else tuple(project_mcp_config_path or ())
    )
    config_layers = []
    if mcp_config_path is not None:
        config_layers.append((mcp_config_path, False))
    config_layers.extend((path, True) for path in project_paths)
    for path, project_scoped in config_layers:
        if path is None:
            continue
        try:
            layer = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            return None, type(exc).__name__, "project" if project_scoped else "user"
        loaded = True
        if not isinstance(layer, dict):
            return None, "InvalidConfig", "project" if project_scoped else "user"

        base_servers = effective.get("mcp_servers", {})
        layer_servers = layer.get("mcp_servers", {})
        if isinstance(base_servers, dict) and isinstance(layer_servers, dict):
            merged_servers = dict(base_servers)
            for server_name, layer_server in layer_servers.items():
                base_server = merged_servers.get(server_name)
                if isinstance(base_server, dict) and isinstance(layer_server, dict):
                    merged_server = {**base_server, **layer_server}
                    base_env = base_server.get("env", {})
                    layer_env = layer_server.get("env", {})
                    if isinstance(base_env, dict) and isinstance(layer_env, dict):
                        merged_server["env"] = {**base_env, **layer_env}
                    merged_servers[server_name] = merged_server
                else:
                    merged_servers[server_name] = layer_server
                if project_scoped and server_name == "ai_engineering_memory":
                    project_has_server = True
            effective.update(layer)
            effective["mcp_servers"] = merged_servers
        else:
            effective.update(layer)
            if project_scoped and "mcp_servers" in layer:
                project_has_server = True

    if not loaded:
        return None, None, "user"
    return effective, None, "project" if project_has_server else "user"


def _maintainer_check(
    mcp_config_path: Path | None = None,
    project_mcp_config_path: Path | Sequence[Path] | None = None,
) -> dict[str, str]:
    """Сообщает, что привилегированные операции не опубликованы как MCP-инструменты."""
    return {
        "status": "not_exposed",
        "source": "mcp_tool_surface",
        "message": "Privileged knowledge and Skill mutations are not registered as MCP tools",
    }


def _same_project_namespace(first: Path, second: Path) -> bool | None:
    """Сравнивает пути по тем же Git-границам, что и MCP namespace."""
    try:
        from ai_engineering_memory.learning import project_namespace

        return project_namespace(first) == project_namespace(second)
    except (OSError, RuntimeError, ValueError):
        return None


def _memory_mcp_check(
    mcp_config_path: Path | None,
    project_mcp_config_path: Path | Sequence[Path] | None = None,
    expected_project_root: Path | None = None,
) -> dict[str, Any]:
    """Проверяет регистрацию и форму запуска MCP, не исполняя сервер."""
    result: dict[str, Any] = {
        "status": "not_checked",
        "configured": False,
        "launcher_available": False,
        "entrypoint_valid": False,
        "runtime_verified": False,
        "project_scope": "not_checked",
        "project_scope_match": "not_checked",
        "config_scope": "user",
    }
    if mcp_config_path is None and project_mcp_config_path is None:
        return result
    config, error_type, config_scope = _effective_mcp_config(
        mcp_config_path,
        project_mcp_config_path,
    )
    result["config_scope"] = config_scope
    if error_type:
        result.update({"status": "error", "error_type": error_type})
        return result
    if config is None:
        result["status"] = "missing"
        return result

    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        result["status"] = "missing"
        return result
    server = servers.get("ai_engineering_memory")
    if server is None:
        result["status"] = "missing"
        return result
    if not isinstance(server, dict):
        result["status"] = "invalid"
        return result
    if server.get("enabled") is False:
        result["status"] = "disabled"
        return result

    server_env = server.get("env", {})
    if not isinstance(server_env, dict):
        result["status"] = "invalid"
        return result
    configured_root = server_env.get("AI_MEMORY_PROJECT_ROOT")
    if isinstance(configured_root, str) and not configured_root.strip():
        configured_root = None
    if configured_root is None:
        configured_cwd = server.get("cwd")
        if configured_cwd is None:
            result["project_scope"] = "process_cwd"
        elif not isinstance(configured_cwd, str) or not configured_cwd.strip():
            result.update({"status": "invalid_project_cwd", "project_scope": "invalid"})
            return result
        else:
            project_cwd = Path(configured_cwd.strip()).expanduser()
            result["project_scope"] = "configured_cwd"
            if not project_cwd.is_absolute():
                result["project_scope_match"] = "unverified"
            else:
                try:
                    project_cwd = project_cwd.resolve(strict=True)
                except (OSError, RuntimeError, ValueError):
                    result.update(
                        {"status": "invalid_project_cwd", "project_scope": "invalid"}
                    )
                    return result
                if not project_cwd.is_dir():
                    result.update(
                        {"status": "invalid_project_cwd", "project_scope": "invalid"}
                    )
                    return result
                if expected_project_root is not None:
                    matches_expected = _same_project_namespace(
                        project_cwd,
                        expected_project_root,
                    )
                    if matches_expected is None:
                        result["project_scope_match"] = "unverified"
                    else:
                        result["project_scope_match"] = (
                            "matched" if matches_expected else "mismatch"
                        )
    elif not isinstance(configured_root, str):
        result.update({"status": "invalid_project_root", "project_scope": "invalid"})
        return result
    else:
        project_root = Path(configured_root.strip()).expanduser()
        if not project_root.is_absolute():
            result.update(
                {"status": "invalid_project_root", "project_scope": "invalid"}
            )
            return result
        try:
            project_root = project_root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            result.update(
                {"status": "invalid_project_root", "project_scope": "invalid"}
            )
            return result
        if not project_root.is_dir():
            result.update(
                {"status": "invalid_project_root", "project_scope": "invalid"}
            )
            return result
        result["project_scope"] = "configured_root"
        if expected_project_root is not None:
            matches_expected = _same_project_namespace(
                project_root,
                expected_project_root,
            )
            if matches_expected is None:
                result["project_scope_match"] = "expected_root_unavailable"
            else:
                result["project_scope_match"] = (
                    "matched" if matches_expected else "mismatch"
                )

    command = server.get("command")
    args = server.get("args", [])
    if (
        not isinstance(command, str)
        or not command.strip()
        or not isinstance(args, list)
        or any(not isinstance(argument, str) for argument in args)
    ):
        result["status"] = "invalid"
        return result

    executable = Path(command).expanduser()
    if executable.is_absolute() or "\\" in command or "/" in command:
        launcher_available = executable.is_file()
    else:
        launcher_available = shutil.which(command) is not None
    result["configured"] = True
    result["launcher_available"] = launcher_available
    if not launcher_available:
        result["status"] = "unavailable"
        return result

    normalized_args = [argument.casefold() for argument in args]
    module_entrypoint = any(
        normalized_args[index : index + 4]
        == ["-m", "ai_engineering_memory.cli", "mcp", "serve"]
        for index in range(max(0, len(normalized_args) - 3))
    )
    executable_entrypoint = executable.stem.casefold() in {
        "ai-memory",
        "ai-memory.exe",
    } and normalized_args[:2] == ["mcp", "serve"]
    result["entrypoint_valid"] = module_entrypoint or executable_entrypoint
    if not result["entrypoint_valid"]:
        result["status"] = "incomplete"
    elif result["project_scope_match"] == "mismatch":
        result["status"] = "project_scope_mismatch"
    elif result["project_scope_match"] == "unverified":
        result["status"] = "project_scope_unverified"
    else:
        result["status"] = "configured"
    return result


def _probe_memory_mcp(
    mcp_config_path: Path | None,
    project_mcp_config_path: Path | Sequence[Path] | None = None,
    *,
    timeout_seconds: float = 5,
) -> dict[str, Any]:
    """Проверяет MCP и вызывает только memory_search на временной пустой базе."""
    result: dict[str, Any] = {
        "protocol_probe_status": "not_requested",
        "protocol_probe_tool_count": None,
        "protocol_probe_required_tools_present": False,
        "protocol_probe_call_status": "not_requested",
        "protocol_probe_call_verified": False,
        "protocol_probe_database_scope": "not_used",
    }
    try:
        config, error_type, _ = _effective_mcp_config(
            mcp_config_path,
            project_mcp_config_path,
        )
        if error_type:
            result["protocol_probe_status"] = "config_error"
            result["protocol_probe_error_type"] = error_type
            return result
        if config is None:
            result["protocol_probe_status"] = "missing_config"
            return result
        servers = config.get("mcp_servers")
        if not isinstance(servers, dict):
            result["protocol_probe_status"] = "not_configured"
            return result
        server = servers.get("ai_engineering_memory")
        if not isinstance(server, dict):
            result["protocol_probe_status"] = "not_configured"
            return result
        if server.get("enabled") is False:
            result["protocol_probe_status"] = "disabled"
            return result
        command = server.get("command")
        args = server.get("args", [])
        server_env = server.get("env", {})
        if (
            not isinstance(command, str)
            or not command.strip()
            or not isinstance(args, list)
            or any(not isinstance(arg, str) for arg in args)
            or not isinstance(server_env, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in server_env.items()
            )
        ):
            result["protocol_probe_status"] = "invalid_config"
            return result
        allowed_environment_keys = {"PLUGIN_DATA", "LOCALAPPDATA"}
        unsupported_environment = [
            key
            for key in server_env
            if key not in allowed_environment_keys and not key.startswith("AI_MEMORY_")
        ]
        if unsupported_environment:
            result["protocol_probe_status"] = "unsupported_environment_skipped"
            return result

        executable = Path(command).expanduser()
        if not executable.is_absolute() and "\\" not in command and "/" not in command:
            resolved = shutil.which(command)
            if resolved is None:
                result["protocol_probe_status"] = "launcher_unavailable"
                return result
            executable = Path(resolved)
        stem = executable.stem.casefold()
        normalized_args = [arg.casefold() for arg in args]
        safe_python_entrypoint = stem in {
            "python",
            "python3",
            "python.exe",
            "python3.exe",
        } and normalized_args == ["-m", "ai_engineering_memory.cli", "mcp", "serve"]
        safe_script_entrypoint = stem in {
            "ai-memory",
            "ai-memory.exe",
        } and normalized_args == ["mcp", "serve"]
        if not (safe_python_entrypoint or safe_script_entrypoint):
            result["protocol_probe_status"] = "unsafe_command_skipped"
            return result

        environment = os.environ.copy()
        environment.update(server_env)
        cwd = server.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            result["protocol_probe_status"] = "invalid_config"
            return result
        with tempfile.TemporaryDirectory(prefix="ai-memory-doctor-") as probe_dir:
            environment["AI_MEMORY_DB"] = str(Path(probe_dir) / "probe.sqlite")
            result["protocol_probe_database_scope"] = "isolated_temporary"
            params = StdioServerParameters(
                command=command,
                args=args,
                env=environment,
                cwd=cwd or None,
            )

            async def _handshake_and_smoke() -> tuple[int, bool, bool]:
                async with (
                    stdio_client(params) as (read_stream, write_stream),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    listed = await session.list_tools()
                    names = {tool.name for tool in listed.tools}
                    required = {"memory_search", "learning_add_candidate"}
                    required_present = required <= names
                    if not required_present:
                        return len(names), False, False
                    called = await session.call_tool(
                        "memory_search",
                        {
                            "query": "ai-memory-doctor-smoke",
                            "namespace": "global",
                            "limit": 1,
                        },
                    )
                    return len(names), True, not called.is_error

            try:
                tool_count, required_tools_present, call_verified = asyncio.run(
                    asyncio.wait_for(
                        _handshake_and_smoke(),
                        timeout=timeout_seconds,
                    )
                )
            except TimeoutError:
                result["protocol_probe_status"] = "timeout"
                result["protocol_probe_call_status"] = "timeout"
                return result
            except Exception as exc:  # noqa: BLE001 - only a safe type is exposed.
                result["protocol_probe_status"] = "error"
                result["protocol_probe_error_type"] = type(exc).__name__
                result["protocol_probe_call_status"] = "error"
                return result

        result.update(
            {
                "protocol_probe_status": (
                    "verified" if required_tools_present else "incompatible_server"
                ),
                "protocol_probe_tool_count": tool_count,
                "protocol_probe_required_tools_present": required_tools_present,
                "protocol_probe_call_status": (
                    "verified"
                    if call_verified
                    else "tool_error"
                    if required_tools_present
                    else "not_available"
                ),
                "protocol_probe_call_verified": call_verified,
            }
        )
        if required_tools_present and not call_verified:
            result["protocol_probe_status"] = "tool_call_error"
        if required_tools_present:
            result["status"] = "configured"
        return result
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        result["protocol_probe_status"] = "config_error"
        result["protocol_probe_error_type"] = type(exc).__name__
        return result


def collect_diagnostics(
    *,
    database_path: Path,
    hooks_config_path: Path,
    hook_log_path: Path,
    mcp_config_path: Path | None = None,
    project_mcp_config_path: Path | Sequence[Path] | None = None,
    expected_project_root: Path | None = None,
    probe_mcp: bool = False,
) -> dict[str, Any]:
    """Собирает диагностический отчёт, не меняя базу, конфиг и журнал."""
    database = _database_check(database_path)
    hooks = _hook_config_check(hooks_config_path)
    hook_log = _hook_log_check(hook_log_path)
    memory_mcp = _memory_mcp_check(
        mcp_config_path,
        project_mcp_config_path,
        expected_project_root,
    )
    if probe_mcp:
        if memory_mcp["status"] in {
            "project_scope_mismatch",
            "project_scope_unverified",
        }:
            probe = {
                "protocol_probe_status": (
                    "skipped_project_scope_mismatch"
                    if memory_mcp["status"] == "project_scope_mismatch"
                    else "skipped_project_scope_unverified"
                ),
                "protocol_probe_tool_count": None,
                "protocol_probe_required_tools_present": False,
                "protocol_probe_call_status": "not_run",
                "protocol_probe_call_verified": False,
                "protocol_probe_database_scope": "not_used",
            }
        else:
            probe = _probe_memory_mcp(mcp_config_path, project_mcp_config_path)
        memory_mcp.update(probe)
        if memory_mcp["status"] not in {
            "project_scope_mismatch",
            "project_scope_unverified",
        } and probe["protocol_probe_status"] not in {"verified", "not_configured"}:
            memory_mcp["status"] = "probe_" + str(probe["protocol_probe_status"])
    maintainer_actions = _maintainer_check(
        mcp_config_path,
        project_mcp_config_path,
    )
    checks = (database, hooks, hook_log)
    overall = (
        "error"
        if any(check["status"] == "error" for check in checks)
        or (memory_mcp["status"] == "error")
        else (
            "warning"
            if any(check["status"] != "ok" for check in checks)
            or memory_mcp["status"] != "configured"
            else "ok"
        )
    )
    return {
        "overall": overall,
        "database": database,
        "hooks": hooks,
        "hook_log": hook_log,
        "memory_mcp": memory_mcp,
        "maintainer_actions": maintainer_actions,
    }
