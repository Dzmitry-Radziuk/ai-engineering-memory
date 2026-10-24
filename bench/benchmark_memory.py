"""Измеряет основные локальные сценарии на синтетических временных базах."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import statistics
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ai_engineering_memory import learning, storage
from ai_engineering_memory.codex_hooks import handle_event
from ai_engineering_memory.migrations import MigrationEngine
from ai_engineering_memory.settings import Settings


def _percentile(values: list[float], percentile: int) -> float:
    """Возвращает ближайший ранг процентиля для измеренных значений."""
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return round(ordered[index], 3)


def _measure(operation: Callable[[], Any], iterations: int) -> dict[str, float | int]:
    """Прогревает операцию и возвращает длительности в миллисекундах."""
    operation()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "iterations": iterations,
        "p50_ms": _percentile(samples, 50),
        "p95_ms": _percentile(samples, 95),
        "p99_ms": _percentile(samples, 99),
        "mean_ms": round(statistics.fmean(samples), 3),
    }


def _seed_database(
    store: storage.Storage, count: int, *, namespace: str = "global"
) -> float:
    """Заполняет временную БД одобренными поисковыми записями указанного scope."""
    now = datetime.now(UTC).isoformat()
    records = []
    for index in range(count):
        searchable = index % 100 == 0
        title = f"Synthetic engineering memory record {index}"
        content = (
            (
                "Fix retry bug in Python database repository and run tests. "
                if namespace != "global"
                else ""
            )
            + "Reliable retries preserve durable failure states and recovery evidence. "
            + ("markerneedle search token. " if searchable else "routine record. ")
            + f"Record index {index}."
        )
        digest = hashlib.sha256(
            f"backend\0{title.lower()}\0{content.lower()}".encode()
        ).hexdigest()
        records.append(
            (
                str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"ai-memory-benchmark:{namespace}:{index}",
                    )
                ),
                "backend",
                title,
                content,
                "approved",
                "manual",
                None,
                "python",
                0.8,
                namespace,
                now,
                now,
                0,
                digest,
            )
        )
    started = time.perf_counter()
    with store.database.connect() as connection:
        connection.executemany(
            """INSERT INTO knowledge
            (uuid,category,title,content,status,source_type,source_reference,
             technology,confidence,namespace,created_at,updated_at,publishable,
             content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            records,
        )
    return round(time.perf_counter() - started, 3)


def _initialize_temporary_database(store: storage.Storage) -> None:
    """Повторяет временный SQLite open при краткой блокировке каталога Windows."""
    for attempt in range(10):
        try:
            store.init_database()
            return
        except sqlite3.OperationalError as exc:
            if (
                "unable to open database file" not in str(exc).casefold()
                or attempt == 9
            ):
                raise
            time.sleep(0.05)


async def _mcp_sample(
    database_path: Path, project_root: Path
) -> tuple[float, float, int]:
    """Измеряет handshake MCP v2 и отдельный roundtrip поиска."""
    environment = os.environ.copy()
    environment["AI_MEMORY_DB"] = str(database_path)
    environment["AI_MEMORY_PROJECT_ROOT"] = str(project_root)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_engineering_memory.cli", "mcp", "serve"],
        env=environment,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        started = time.perf_counter()
        await session.initialize()
        listed = await session.list_tools()
        startup_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        result = await session.call_tool(
            "memory_search",
            {"query": "markerneedle", "namespace": "global", "limit": 10},
        )
        search_ms = (time.perf_counter() - started) * 1000
        if result.is_error:
            raise RuntimeError("MCP search benchmark tool call failed")
        return startup_ms, search_ms, len(listed.tools)


def _contains_permission_error(error: BaseException) -> bool:
    """Распознаёт Win32 pipe denial, вложенный в ExceptionGroup AnyIO."""
    if isinstance(error, PermissionError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_permission_error(item) for item in error.exceptions)
    return False


async def _measure_mcp(
    database_path: Path, project_root: Path, iterations: int
) -> dict[str, Any]:
    """Повторяет изолированный MCP launch и получает p50/p95/p99 задержек."""
    startup_samples: list[float] = []
    search_samples: list[float] = []
    tool_count = 0
    for _ in range(iterations):
        try:
            startup_ms, search_ms, tool_count = await _mcp_sample(
                database_path, project_root
            )
        except ExceptionGroup as exc:
            if not _contains_permission_error(exc):
                raise
            break
        except PermissionError:
            break
        startup_samples.append(startup_ms)
        search_samples.append(search_ms)

    if not startup_samples:
        return {
            "status": "unavailable",
            "error_type": "ProcessTransportPermissionError",
        }

    def summarize(samples: list[float]) -> dict[str, float | int]:
        return {
            "iterations": iterations,
            "p50_ms": _percentile(samples, 50),
            "p95_ms": _percentile(samples, 95),
            "p99_ms": _percentile(samples, 99),
            "mean_ms": round(statistics.fmean(samples), 3),
        }

    return {
        "status": "measured" if len(startup_samples) == iterations else "partial",
        "requested_iterations": iterations,
        "completed_iterations": len(startup_samples),
        "tool_count": tool_count,
        "cold_start_initialize_and_list": summarize(startup_samples),
        "search_round_trip": summarize(search_samples),
    }


def _migration_sample(source: Path, destination: Path) -> None:
    """Клонирует legacy v5 fixture и применяет только актуальный runner."""
    with (
        closing(sqlite3.connect(source)) as legacy,
        closing(sqlite3.connect(destination)) as target,
    ):
        legacy.backup(target)
    with closing(sqlite3.connect(destination)) as candidate:
        candidate.row_factory = sqlite3.Row
        MigrationEngine().apply(candidate)
        version = candidate.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        if version != storage.LATEST_SCHEMA_VERSION:
            raise RuntimeError("Migration benchmark did not reach latest schema")


def _measure_migration(source: Path, iterations: int) -> dict[str, float | int]:
    """Измеряет копирование и миграцию изолированной схемы v4."""
    counter = 0

    def operation() -> None:
        nonlocal counter
        counter += 1
        destination = source.with_name(f"migration-{counter}.sqlite")
        try:
            _migration_sample(source, destination)
        finally:
            destination.unlink(missing_ok=True)

    return _measure(operation, iterations)


@contextmanager
def _benchmark_directory():
    """Создаёт и удаляет только собственную временную папку benchmark-запуска."""
    benchmark_root = Path(__file__).resolve().parents[1] / ".tmp" / "benchmarks"
    benchmark_root.mkdir(parents=True, exist_ok=True)
    run_directory = benchmark_root / f"aem-benchmark-{uuid.uuid4().hex}"
    run_directory.mkdir()
    try:
        yield run_directory
    finally:
        resolved_root = benchmark_root.resolve()
        resolved_run = run_directory.resolve()
        if resolved_run.parent != resolved_root or not resolved_run.name.startswith(
            "aem-benchmark-"
        ):
            raise RuntimeError("Временный каталог benchmark вышел за ожидаемый корень")
        for attempt in range(8):
            try:
                shutil.rmtree(resolved_run)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.1)


def _benchmark_size(
    count: int, iterations: int, write_iterations: int
) -> dict[str, Any]:
    """Создаёт отдельную временную базу и измеряет сценарии одного размера."""
    with _benchmark_directory() as root:
        temporary = str(root)
        database_path = root / "memory.db"
        database_path.parent.mkdir(parents=True, exist_ok=True)
        store = storage.create_storage(Settings(database_path))
        with store.activate():
            _initialize_temporary_database(store)
            seed_seconds = _seed_database(store, count)
            previous_hook_log = os.environ.get("AI_MEMORY_HOOK_LOG")
            os.environ["AI_MEMORY_HOOK_LOG"] = str(root / "hooks.jsonl")
            search = _measure(
                lambda: store.search_knowledge(
                    "markerneedle",
                    status="approved",
                    namespace="global",
                    limit=10,
                    _read_only=True,
                ),
                iterations,
            )

            def submit_prompt() -> dict[str, Any]:
                return handle_event(
                    "UserPromptSubmit",
                    {
                        "prompt": "Fix the retry bug in this Python database repository and run tests.",
                        "cwd": temporary,
                    },
                )

            try:
                hook_empty_project = _measure(submit_prompt, iterations)
                project_namespace = learning.project_namespace(temporary)
                project_rows = max(1, count // 100)
                _seed_database(store, project_rows, namespace=project_namespace)
                hook_populated_project = _measure(submit_prompt, iterations)
            finally:
                if previous_hook_log is None:
                    os.environ.pop("AI_MEMORY_HOOK_LOG", None)
                else:
                    os.environ["AI_MEMORY_HOOK_LOG"] = previous_hook_log
            initialization = _measure(store.init_database, iterations)
            dedupe = _measure(
                lambda: learning._similar_existing(
                    "Unrelated benchmark rule",
                    "Avoid duplicate synthetic content for independent consumers.",
                    "global",
                ),
                iterations,
            )
            write_index = 0

            def write_candidate() -> None:
                nonlocal write_index
                write_index += 1
                store.create_knowledge(
                    "benchmark",
                    f"Synthetic candidate write {count}-{write_index}",
                    f"A unique candidate write measurement for dataset {count}, run {write_index}.",
                    namespace="project:benchmark",
                )

            candidate_write = _measure(write_candidate, write_iterations)
            backup_path = root / "backup.sqlite"
            backup = _measure(lambda: store.backup_database(backup_path), 3)
            backup_check = storage.verify_database_backup(backup_path)
            try:
                restore_directory = root / "restore-temp"
                restore_directory.mkdir()
                restore_check = storage.dry_run_restore_database(
                    backup_path, temporary_directory=restore_directory
                )
                restore_preview: dict[str, Any] = {
                    "status": "measured",
                    **_measure(
                        lambda: storage.dry_run_restore_database(
                            backup_path, temporary_directory=restore_directory
                        ),
                        3,
                    ),
                }
            except (PermissionError, sqlite3.OperationalError):
                restore_check = None
                restore_preview = {
                    "status": "unavailable",
                    "error_type": "TemporaryWorkspaceError",
                }
            with store.database.connect(read_only=True) as connection:
                fts_available = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
                    ).fetchone()
                    is not None
                )
                schema_version = int(
                    connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0]
                )
                migration_source = root / "migration-v5.sqlite"
                with (
                    closing(sqlite3.connect(store.database_path)) as current,
                    closing(sqlite3.connect(migration_source)) as migration_target,
                ):
                    current.backup(migration_target)
                with closing(sqlite3.connect(migration_source)) as legacy:
                    legacy.execute("DELETE FROM schema_migrations WHERE version=6")
                    legacy.execute("DROP INDEX IF EXISTS idx_knowledge_namespace_id")
                migration = _measure_migration(migration_source, 3)
                mcp = asyncio.run(_measure_mcp(database_path, root, min(iterations, 2)))
            database_bytes = store.database_path.stat().st_size

        return {
            "knowledge_rows": count,
            "seed_seconds": seed_seconds,
            "database_bytes": database_bytes,
            "schema_version": schema_version,
            "fts_available": fts_available,
            "search": search,
            "project_rows": project_rows,
            "user_prompt_submit_hook_empty_project": hook_empty_project,
            "user_prompt_submit_hook_populated_project": hook_populated_project,
            "latest_schema_init": initialization,
            "migration_v5_to_latest_with_fixture_copy": migration,
            "mcp": mcp,
            "candidate_dedupe": dedupe,
            "candidate_write": candidate_write,
            "backup": backup,
            "backup_verified": backup_check["valid"],
            "restore_dry_run": restore_preview,
            "restore_ids_preserved": (
                restore_check["ids_preserved"] if restore_check else None
            ),
        }


def main() -> int:
    """Запускает выбранные размеры и печатает JSON-отчёт без путей к временным файлам."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[1_000, 10_000, 100_000],
        help="число синтетических знаний для каждого отдельного прогона",
    )
    parser.add_argument("--iterations", type=int, default=15)
    parser.add_argument("--write-iterations", type=int, default=5)
    args = parser.parse_args()
    if (
        any(size < 1 or size > 100_000 for size in args.sizes)
        or args.iterations < 1
        or args.write_iterations < 1
    ):
        parser.error("размеры должны быть 1..100000, число повторов — положительным")

    report = {
        "benchmark_version": 2,
        "synthetic_data": True,
        "reference_host": {
            "system": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "latency_unit": "milliseconds",
        "note": "Measured locally; results depend on host, filesystem, and SQLite build.",
        "datasets": [
            _benchmark_size(size, args.iterations, args.write_iterations)
            for size in args.sizes
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
