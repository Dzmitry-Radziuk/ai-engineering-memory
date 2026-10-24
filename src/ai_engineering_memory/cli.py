"""Командный интерфейс локальной платформы памяти."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ai_engineering_memory import storage
from ai_engineering_memory.application import MemoryService
from ai_engineering_memory.bootstrap import (
    create_admin_service,
    create_learning_service,
    create_memory_service,
    create_skills_service,
)


def _memory_service() -> MemoryService:
    """Создаёт сервис для текущего изолированного запуска CLI."""
    return create_memory_service()


def _configure_redirected_output() -> None:
    """Использует UTF-8 для перенаправленного вывода CLI в Windows PowerShell."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").replace("_", "-").lower()
        if stream.isatty() or encoding == "utf-8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _confirm_admin_action(expected: str, *, ask=None) -> bool:
    """Требует интерактивный exact-match перед human-admin изменением."""
    if not sys.stdin.isatty():
        raise ValueError("Привилегированное действие доступно только в интерактивном терминале")
    ask = ask or input
    received = ask(f"Для подтверждения введите {expected}: ").strip()
    if received != expected:
        print("Подтверждение не совпало; изменений нет.")
        return False
    return True


def _run_admin(args: argparse.Namespace) -> int:
    """Выполняет human-admin решение после интерактивного exact confirmation."""
    service = create_admin_service()
    if args.admin_domain == "knowledge":
        action = args.admin_action
        knowledge_id = args.id
        if not _confirm_admin_action(f"{action.upper()} {knowledge_id}"):
            return 1
        if action == "approve":
            result = service.approve_knowledge(knowledge_id)
        elif action == "reject":
            result = service.reject_knowledge(knowledge_id, reason=args.reason)
        else:
            result = service.deprecate_knowledge(knowledge_id)
    else:
        maintainer = os.environ.get("AI_MEMORY_MAINTAINER", "")
        if args.admin_action == "rollback":
            target = f"{args.skill_name}@{args.version}"
            if not _confirm_admin_action(f"ROLLBACK {target}"):
                return 1
            result = service.rollback_skill_version(
                args.skill_name, args.version, maintainer=maintainer,
            )
        else:
            action = args.admin_action
            if not _confirm_admin_action(f"{action.upper()} {args.proposal_id}"):
                return 1
            result = service.decide_skill_proposal(
                args.proposal_id,
                decision="approved" if action == "approve" else "rejected",
                maintainer=maintainer,
                reason=getattr(args, "reason", ""),
            )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _parser() -> argparse.ArgumentParser:
    """Создаёт дерево команд CLI."""
    parser = argparse.ArgumentParser(prog="ai-memory", description="Локальная инженерная память")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="создать или обновить локальную базу")
    commands.add_parser("status", help="показать состояние базы")
    doctor = commands.add_parser("doctor", help="проверить базу, Codex hooks и их журнал")
    doctor.add_argument("--json", action="store_true", help="вывести машинно-читаемый отчёт")
    doctor.add_argument(
        "--probe-mcp", action="store_true",
        help="запустить MCP и вызвать безопасный memory_search на временной БД",
    )
    doctor.add_argument(
        "--cwd", type=Path, default=Path.cwd(),
        help="проектный каталог для read-only проверки .codex/config.toml",
    )
    project_config = commands.add_parser(
        "project-config",
        help="напечатать MCP-конфигурацию для доверенного проекта Codex",
    )
    project_config.add_argument("--cwd", type=Path, default=Path.cwd())
    hooks = commands.add_parser("hooks", help="установить Codex hooks без перезаписи остальных настроек")
    hook_actions = hooks.add_subparsers(dest="action", required=True)
    install_hooks = hook_actions.add_parser("install", help="объединить hooks с профилем Codex")
    install_hooks.add_argument("--config", type=Path, help="путь к hooks.json")
    install_hooks.add_argument("--dry-run", action="store_true", help="показать, потребуется ли изменение")
    backup = commands.add_parser("backup", help="создать резервную копию базы")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="восстановить базу из резервной копии")
    restore.add_argument("source", type=Path)
    admin = commands.add_parser("admin", help="human-only lifecycle actions")
    admin_commands = admin.add_subparsers(dest="admin_domain", required=True)
    admin_knowledge = admin_commands.add_parser("knowledge")
    admin_knowledge_actions = admin_knowledge.add_subparsers(
        dest="admin_action", required=True,
    )
    for action in ("approve", "reject", "deprecate"):
        admin_action = admin_knowledge_actions.add_parser(action)
        admin_action.add_argument("id", type=int)
        if action == "reject":
            admin_action.add_argument("--reason", required=True)
    admin_skills = admin_commands.add_parser("skill")
    admin_skill_actions = admin_skills.add_subparsers(
        dest="admin_action", required=True,
    )
    for action in ("approve", "reject"):
        admin_action = admin_skill_actions.add_parser(action)
        admin_action.add_argument("proposal_id")
        if action == "reject":
            admin_action.add_argument("--reason", required=True)
    rollback = admin_skill_actions.add_parser("rollback")
    rollback.add_argument("skill_name")
    rollback.add_argument("version")
    knowledge = commands.add_parser("knowledge")
    sub = knowledge.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--status", choices=sorted(storage.STATUSES))
    listing.add_argument("--namespace")
    listing.add_argument("--limit", type=int, default=100)
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--status", default="approved", choices=[*sorted(storage.STATUSES), "all"])
    search.add_argument("--namespace")
    search.add_argument("--limit", type=int, default=10)
    review = sub.add_parser("review", help="проверить и решить судьбу candidates")
    review.add_argument("--cwd", type=Path, default=Path.cwd())
    review.add_argument("--limit", type=int, default=100)
    add = sub.add_parser("add")
    add.add_argument("--category", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--content", required=True)
    add.add_argument("--technology", default="")
    add.add_argument("--namespace", default="global")
    for action in ("approve", "reject", "deprecate"):
        item = sub.add_parser(action)
        item.add_argument("id", type=int)
        if action == "reject":
            item.add_argument("--reason")
    skills = commands.add_parser("skills")
    skill_commands = skills.add_subparsers(dest="action", required=True)
    skill_commands.add_parser("list")
    proposals = skill_commands.add_parser("proposals")
    proposals.add_argument("--status", default="candidate")
    learning = commands.add_parser("learning")
    learning_commands = learning.add_subparsers(dest="action", required=True)
    learning_status = learning_commands.add_parser("status")
    learning_status.add_argument(
        "--human", action="store_true",
        help="показать короткую сводку вместо JSON",
    )
    queue = learning_commands.add_parser("list")
    queue.add_argument("--limit", type=int, default=100)
    attempts = learning_commands.add_parser(
        "attempts", help="показать метаданные последних запусков verifier",
    )
    attempts.add_argument("--limit", type=int, default=20)
    attempts.add_argument("--all", action="store_true",
                          help="включить успешные запуски")
    process = learning_commands.add_parser("process")
    process.add_argument("--limit", type=int, default=10)
    retry = learning_commands.add_parser("retry")
    retry.add_argument("event_id")
    verify = learning_commands.add_parser("verify")
    verify.add_argument("--task-id", required=True)
    verify.add_argument("--summary", required=True)
    verify.add_argument("--cwd", type=Path, default=Path.cwd())
    verify.add_argument("--timeout", type=int, default=300)
    verify.add_argument("--changed-file", action="append", default=[])
    verify.add_argument("program_argv", nargs=argparse.REMAINDER)
    withdraw = learning_commands.add_parser("withdraw")
    withdraw.add_argument("knowledge_id", type=int)
    withdraw.add_argument("--reason", required=True)
    mcp = commands.add_parser("mcp")
    mcp.add_subparsers(dest="action", required=True).add_parser("serve")
    return parser


def _safe_terminal_text(value: str) -> str:
    """Экранирует управляющие символы перед показом недоверенного текста в терминале."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\\r")
    rendered: list[str] = []
    for char in normalized:
        if char == "\n" or char.isprintable():
            rendered.append(char)
            continue
        codepoint = ord(char)
        if codepoint <= 0xFF:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(f"\\U{codepoint:08x}")
    return "".join(rendered)


def _print_untrusted_block(label: str, value: str) -> None:
    """Показывает сохранённые данные отдельным явно отделённым блоком."""
    print(f"{label}:")
    for line in _safe_terminal_text(value).split("\n"):
        print(f"  | {line}")


def _review_candidates(cwd: Path, *, limit: int, ask=None) -> int:
    """Показывает кандидатов текущего проекта и global с подтверждением решений."""
    from ai_engineering_memory import learning

    ask = ask or input
    memory_service = _memory_service()
    admin_service = create_admin_service()
    project_namespace = learning.project_namespace(cwd)
    candidates = memory_service.list_candidates(namespace="global", limit=limit)
    project_candidates = memory_service.list_candidates(
        namespace=project_namespace, limit=limit,
    )
    candidates.extend(project_candidates)
    candidates.sort(key=lambda item: (item["namespace"] != "global", -item["id"]))
    if not candidates:
        print("Нет кандидатов в общей памяти и текущем проекте.")
        return 0

    print(f"Кандидаты: {len(candidates)}; project namespace: {project_namespace}")
    for item in candidates:
        knowledge_id = int(item["id"])
        print("\n" + "=" * 72)
        print(
            f"ID {knowledge_id} · {_safe_terminal_text(item['namespace'])} · "
            f"{_safe_terminal_text(item['category'])}"
        )
        confidence_label = (
            "оценка уверенности Codex"
            if item["source_type"] in {"learning_engine", "cross_project_learning"}
            else "уверенность"
        )
        print(
            f"Источник: {_safe_terminal_text(item['source_type'])} · "
            f"{confidence_label}: {item['confidence']}"
        )
        _print_untrusted_block("Название кандидата", item["title"])
        _print_untrusted_block("Содержание кандидата", item["content"])
        evidence_rows = memory_service.get_evidence(knowledge_id)
        for evidence in evidence_rows:
            _print_untrusted_block(
                f"Доказательство ({_safe_terminal_text(evidence['kind'])})",
                evidence["result"],
            )
            if evidence["source"]:
                _print_untrusted_block("Ссылки", evidence["source"])
            if evidence["limitations"]:
                _print_untrusted_block("Ограничения", evidence["limitations"])
        if item["source_type"] == "cross_project_learning":
            support = memory_service.get_corroboration(knowledge_id)
            print(
                "Независимые отпечатки проектов: "
                f"{support['independent_project_count']}; старые подтверждения "
                "без проверенной идентичности: "
                f"{support['legacy_unfingerprinted_support_count'] + support['legacy_identity_support_count']}"
            )
        choice = ask("Решение [a] утвердить, [r] отклонить, [s] пропустить, [q] выход: ")
        choice = choice.strip().casefold()
        if choice in {"q", "quit", "выход"}:
            break
        if choice in {"s", "skip", "пропустить"}:
            continue
        if choice in {"a", "approve", "утвердить"}:
            if ask(f"Для утверждения введи APPROVE {knowledge_id}: ").strip() != f"APPROVE {knowledge_id}":
                print("Подтверждение не совпало; кандидат не изменён.")
                continue
            saved = admin_service.approve_knowledge(knowledge_id)
            print(f"Запись {knowledge_id}: {saved['status']}.")
            continue
        if choice in {"r", "reject", "отклонить"}:
            if ask(f"Для отклонения введи REJECT {knowledge_id}: ").strip() != f"REJECT {knowledge_id}":
                print("Подтверждение не совпало; кандидат не изменён.")
                continue
            reason = ask("Причина отклонения (до 500 символов): ").strip()
            if not reason:
                print("Без причины кандидат не изменён.")
                continue
            saved = admin_service.reject_knowledge(knowledge_id, reason=reason)
            print(f"Запись {knowledge_id}: {saved['status']}.")
            continue
        print("Неизвестное действие; кандидат не изменён.")
    return 0


def _main(argv: Sequence[str] | None = None) -> int:
    """Выполняет команду и печатает результат в JSON или текстовом виде."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            storage.init_database()
            print(f"База готова: {storage.current_database_path()}")
        elif args.command == "project-config":
            from ai_engineering_memory import learning

            project_root = learning.resolve_project_root(args.cwd)
            mcp_command = str(Path(sys.executable).resolve())
            project_path = str(project_root)
            mcp_args = ["-m", "ai_engineering_memory.cli", "mcp", "serve"]
            print("# Добавьте этот блок в <проект>/.codex/config.toml")
            print("# Codex применяет проектную конфигурацию только для доверенных проектов.")
            print("[mcp_servers.ai_engineering_memory]")
            print(
                "command = "
                + json.dumps(mcp_command, ensure_ascii=False)
            )
            print(
                "args = " + json.dumps(mcp_args, ensure_ascii=False)
            )
            print("\n[mcp_servers.ai_engineering_memory.env]")
            print(
                "AI_MEMORY_PROJECT_ROOT = "
                + json.dumps(project_path, ensure_ascii=False)
            )
        elif args.command == "doctor":
            from ai_engineering_memory import codex_hooks, diagnostics

            codex_home = Path(
                os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            ).expanduser()
            from ai_engineering_memory import learning

            requested_cwd = args.cwd.expanduser().resolve(strict=True)
            project_root = learning.resolve_project_root(requested_cwd)
            relative_cwd = requested_cwd.relative_to(project_root)
            project_directories = [project_root]
            for part in relative_cwd.parts:
                project_directories.append(project_directories[-1] / part)
            project_mcp_config_paths = [
                directory / ".codex" / "config.toml"
                for directory in project_directories
            ]
            report = diagnostics.collect_diagnostics(
                database_path=storage.current_database_path(),
                hooks_config_path=codex_home / "hooks.json",
                hook_log_path=codex_hooks._log_path(),
                mcp_config_path=codex_home / "config.toml",
                project_mcp_config_path=project_mcp_config_paths,
                expected_project_root=project_root,
                probe_mcp=args.probe_mcp,
            )
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                labels = {"ok": "OK", "warning": "ВНИМАНИЕ", "error": "ОШИБКА", "missing": "НЕТ"}
                print(f"AI Engineering Memory — диагностика: {labels[report['overall']]}")
                database = report["database"]
                print(
                    f"База: {labels[database['status']]} · {database['path']} · "
                    f"quick_check={database['integrity']} · schema={database['schema_version']}"
                )
                if database.get("recommendation"):
                    print(f"  Рекомендация: {database['recommendation']}")
                knowledge_counts = database.get("knowledge_counts")
                if knowledge_counts is not None:
                    status_counts = knowledge_counts.get("status_counts", {})
                    status_summary = ", ".join(
                        f"{_safe_terminal_text(status)}={count}"
                        for status, count in sorted(status_counts.items())
                    ) or "статусов пока нет"
                    print(
                        f"Знания: всего {knowledge_counts['total']} · "
                        f"{status_summary}"
                    )
                elif database.get("integrity") == "skipped_active_wal":
                    print(
                        "Счётчики знаний пропущены: SQLite WAL активен, "
                        "чтобы сохранить read-only проверку."
                    )
                learning_counts = database.get("learning_counts", {})
                if learning_counts:
                    task_events = learning_counts.get("task_events", {})
                    task_status_counts = task_events.get("status_counts", {})
                    task_status_summary = ", ".join(
                        f"{_safe_terminal_text(status)}={count}"
                        for status, count in sorted(task_status_counts.items())
                    )
                    task_total = task_events.get("total", 0)
                    task_label = f"событий {task_total}"
                    if task_status_summary:
                        task_label += f" ({task_status_summary})"
                    verifications = learning_counts.get(
                        "verification_records", {},
                    ).get("total", 0)
                    corroborations = learning_counts.get(
                        "learning_corroborations", {},
                    ).get("total", 0)
                    print(
                        f"Обучение: {task_label} · проверок {verifications} · "
                        f"межпроектных подтверждений {corroborations}"
                    )
                hooks = report["hooks"]
                print(
                    f"Hooks: {labels[hooks['status']]} · "
                    f"UserPromptSubmit={hooks['events'].get('UserPromptSubmit', 'нет')} · "
                    f"Stop={hooks['events'].get('Stop', 'нет')}"
                )
                observed_events = report["hook_log"].get("events", {})
                for event in ("UserPromptSubmit", "Stop"):
                    details = observed_events.get(event)
                    if not details:
                        print(f"  Последний запуск {event}: нет записи в журнале")
                        continue
                    timestamp = _safe_terminal_text(
                        details.get("timestamp") or "время неизвестно",
                    )
                    result = _safe_terminal_text(
                        details.get("result") or "результат неизвестен",
                    )
                    source = _safe_terminal_text(
                        details.get("source") or "версия неизвестна",
                    )
                    duration = details.get("duration_ms")
                    duration_label = (
                        f" · {duration} мс" if isinstance(duration, int) else ""
                    )
                    error = details.get("error_type")
                    error_label = (
                        f" · ошибка {_safe_terminal_text(error)}" if error else ""
                    )
                    print(
                        f"  Последний запуск {event}: {timestamp} · {result} · "
                        f"{source}{duration_label}{error_label}"
                    )
                unavailable = [
                    event for event, available in hooks.get("launchers", {}).items()
                    if not available
                ]
                if unavailable:
                    print(f"  Не найден исполняемый файл для: {', '.join(unavailable)}")
                metadata_mismatch = hooks.get("metadata_mismatch_events", [])
                if metadata_mismatch:
                    print(
                        "  Параметры обработчиков требуют обновления: "
                        + ", ".join(metadata_mismatch)
                    )
                    print(
                        "  Проверьте `.venv\\Scripts\\ai-memory.exe hooks install --dry-run`, "
                        "затем при необходимости выполните "
                        "`.venv\\Scripts\\ai-memory.exe hooks install`."
                    )
                memory_mcp = report["memory_mcp"]
                memory_mcp_state = {
                    "configured": "настроен, запуск не проверялся",
                    "not_checked": "не проверялся",
                    "missing": "не настроен",
                    "disabled": "отключён",
                    "unavailable": "launcher не найден",
                    "incomplete": "неполная команда запуска",
                    "invalid_project_root": "корень проекта задан неверно",
                    "invalid_project_cwd": "рабочий каталог MCP задан неверно",
                    "project_scope_mismatch": "MCP закреплён за другим проектом",
                    "project_scope_unverified": "область MCP не удалось сверить с проектом",
                    "invalid": "ошибка формата конфигурации",
                    "error": "ошибка чтения конфигурации",
                }.get(memory_mcp["status"], "неизвестно")
                if args.probe_mcp and memory_mcp.get("protocol_probe_status") == "verified":
                    memory_mcp_state = (
                        "проверка запуска из doctor прошла; runtime Codex не подтверждён"
                    )
                print(
                    "MCP AI Engineering Memory: "
                    f"{memory_mcp_state}"
                )
                config_scope = memory_mcp.get("config_scope", "user")
                if config_scope == "project":
                    print(
                        "  Источник: конфигурации пользователя и проекта; "
                        "Codex применяет проектную часть только для доверенного проекта."
                    )
                elif config_scope == "user":
                    print("  Источник: пользовательская конфигурация Codex.")
                if args.probe_mcp and memory_mcp.get("protocol_probe_status") == "verified":
                    print(
                        f"  MCP initialize/tools/list: OK · инструментов: "
                        f"{memory_mcp['protocol_probe_tool_count']}; "
                        "memory_search на временной пустой БД: OK."
                    )
                elif args.probe_mcp:
                    probe_messages = {
                        "unsafe_command_skipped": "команда запуска не распознана; процесс не запускался",
                        "unsupported_environment_skipped": "есть переменные окружения вне AI Engineering Memory; процесс не запускался",
                        "timeout": "сервер не ответил за отведённое время",
                        "tool_call_error": "MCP запустился, но безопасный вызов memory_search завершился ошибкой",
                        "incompatible_server": "ответил сервер без ожидаемых инструментов",
                        "launcher_unavailable": "исполняемый файл не найден",
                        "disabled": "сервер выключен в конфигурации",
                        "invalid_config": "формат конфигурации некорректен",
                        "error": "не удалось выполнить MCP handshake",
                        "config_error": "не удалось прочитать конфигурацию",
                        "not_configured": "сервер не зарегистрирован",
                        "missing_config": "файл конфигурации не найден",
                        "skipped_project_scope_mismatch": "корень MCP относится к другому проекту; процесс не запускался",
                        "skipped_project_scope_unverified": "рабочую область MCP нельзя надёжно сверить; процесс не запускался",
                    }
                    probe_status = memory_mcp.get("protocol_probe_status", "unknown")
                    probe_message = probe_messages.get(probe_status, "причина не определена")
                    print(f"  MCP protocol probe: {probe_status} · {probe_message}")
                    if memory_mcp.get("protocol_probe_error_type"):
                        print(f"  Тип ошибки: {memory_mcp['protocol_probe_error_type']}")
                project_scope = {
                    "configured_root": "закреплённый AI_MEMORY_PROJECT_ROOT",
                    "configured_cwd": "рабочий каталог MCP из конфигурации",
                    "process_cwd": "рабочий каталог процесса MCP",
                    "invalid": "ошибка настройки",
                }.get(memory_mcp.get("project_scope"), "не проверена")
                print(f"Область проекта MCP: {project_scope}")
                if memory_mcp.get("project_scope_match") == "mismatch":
                    print(
                        "  AI_MEMORY_PROJECT_ROOT не совпадает с проверяемым проектом; "
                        "MCP не будет проверяться вызовом инструментов. "
                        "Используйте отдельную доверенную проектную конфигурацию."
                    )
                elif memory_mcp.get("project_scope_match") == "unverified":
                    print(
                        "  Рабочую область MCP нельзя надёжно сверить с проектом; "
                        "используйте абсолютный AI_MEMORY_PROJECT_ROOT."
                    )
                if memory_mcp.get("project_scope") == "process_cwd":
                    print(
                        "  При смене workspace перезапустите MCP или задайте "
                        "AI_MEMORY_PROJECT_ROOT."
                    )
                if not args.probe_mcp and memory_mcp["status"] != "ok":
                    print(
                        "  doctor проверяет только конфигурацию, корень проекта и launcher; "
                        "сам MCP-сервер не запускается"
                    )
                hook_log = report["hook_log"]
                print(
                    f"Журнал hooks: {labels[hook_log['status']]} · "
                    f"ошибок в просмотренном фрагменте: {hook_log['error_count']}"
                )
                latest_error_events = hook_log.get("latest_error_events", [])
                if latest_error_events:
                    print(
                        "  Последний запуск завершился ошибкой для: "
                        + ", ".join(latest_error_events)
                    )
                if hook_log["missing_events"]:
                    print(
                        "  Нет записей для: "
                        + ", ".join(hook_log["missing_events"])
                    )
                if hook_log.get("stale_source_events"):
                    print(
                        "  Версия последней записи отличается от ожидаемой для: "
                        + ", ".join(hook_log["stale_source_events"])
                    )
                    print(
                        "  Это версия последнего наблюдавшегося запуска; "
                        "она сама по себе не доказывает, что текущая настройка устарела."
                    )
                    if hook_log.get("expected_source"):
                        print(
                            "  Ожидаемый источник кода: "
                            + hook_log["expected_source"]
                        )
                for event, record in hook_log["events"].items():
                    print(
                        f"  {event}: {record['timestamp']} · {record['result']} · "
                        f"{record['duration_ms']} ms · error={record['error_type']}"
                    )
                    if event == "Stop":
                        if record["result"] == "no_marker_seen":
                            print(
                                "  Stop отработал без маркера; это не ошибка запуска "
                                "и не подтверждение выполнения проверки."
                            )
                        elif record["result"] == "marker_present_unverified":
                            print(
                                "  Маркер найден, но Stop не проверяет, запускалась ли "
                                "или прошла ли проверка."
                            )
                maintainer_actions = report["maintainer_actions"]
                print(f"Привилегированные операции MCP: {maintainer_actions['status']}")
            return 0 if report["overall"] == "ok" else 1
        elif args.command == "hooks":
            from ai_engineering_memory import hook_install

            result = hook_install.install_hooks(
                args.config, dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "status":
            entries = _memory_service().list_knowledge(limit=500)
            print(json.dumps({"database": str(storage.current_database_path()), "knowledge": len(entries),
                              "skills": len(create_skills_service().list_skills())}, ensure_ascii=False, indent=2))
        elif args.command == "backup":
            print(storage.backup_database(args.destination))
        elif args.command == "restore":
            source = args.source.expanduser().resolve()
            destination = storage.current_database_path().resolve()
            expected = f"RESTORE {source} -> {destination}"
            print("Перед восстановлением остановите Codex/MCP и другие процессы, использующие эту базу.")
            if not _confirm_admin_action(expected):
                return 1
            result = storage.restore_database(source)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "admin":
            return _run_admin(args)
        elif args.command == "knowledge":
            if args.action == "list":
                result = _memory_service().list_knowledge(
                    status=args.status, namespace=args.namespace, limit=args.limit,
                )
            elif args.action == "search":
                result = _memory_service().search(
                    args.query,
                    status=None if args.status == "all" else args.status,
                    namespace=args.namespace,
                    limit=args.limit,
                )
            elif args.action == "review":
                if not sys.stdin.isatty():
                    raise ValueError("Интерактивный review доступен только в терминале")
                return _review_candidates(args.cwd, limit=args.limit)
            elif args.action == "add":
                result = _memory_service().create_candidate(
                    args.category,
                    args.title,
                    args.content,
                    technology=args.technology,
                    namespace=args.namespace,
                )
            elif args.action in {"approve", "reject", "deprecate"}:
                if not _confirm_admin_action(f"{args.action.upper()} {args.id}"):
                    return 1
                admin_service = create_admin_service()
                if args.action == "approve":
                    result = admin_service.approve_knowledge(args.id)
                elif args.action == "reject":
                    reason = args.reason or input("Причина отклонения (обязательна): ").strip()
                    result = admin_service.reject_knowledge(args.id, reason=reason)
                else:
                    result = admin_service.deprecate_knowledge(args.id)
            else:
                raise ValueError("Неизвестное действие с knowledge")
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif args.command == "skills":
            skills_service = create_skills_service()
            result = (
                skills_service.list_skills()
                if args.action == "list"
                else skills_service.list_proposals(args.status)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "learning":
            from ai_engineering_memory import learning

            learning_service = create_learning_service()
            if args.action == "status":
                summary = learning_service.status_summary()
                summary["codex_native_capture"] = {
                    "mode": "best_effort",
                    "requires": "UserPromptSubmit hook, Codex model, and learning_add_candidate MCP tool",
                }
                summary["background_analysis_configured"] = False
                try:
                    provider = learning.configured_provider()
                    summary["provider_configured"] = provider is not None
                    summary["provider_type"] = type(provider).__name__ if provider else None
                    summary["llm_provider"] = "ollama-local" if provider else "not configured"
                    summary["automatic_analysis"] = False
                    summary["background_analysis_configured"] = False
                    summary["legacy_manual_analysis_available"] = provider is not None
                except learning.ProviderError as exc:
                    summary["provider_configured"] = False
                    summary["provider_error"] = str(exc)
                    summary["llm_provider"] = "not configured"
                    summary["automatic_analysis"] = False
                    summary["legacy_manual_analysis_available"] = False
                if args.human:
                    statuses = summary.get("status_counts", {})
                    status_summary = ", ".join(
                        f"{_safe_terminal_text(status)}={count}"
                        for status, count in sorted(statuses.items())
                    ) or "нет"
                    attempts = summary.get("verification_attempts", {})
                    print(
                        f"Обучение: событий {summary.get('total', 0)} "
                        f"({status_summary}); проверок {attempts.get('total', 0)}, "
                        f"ошибок {attempts.get('failed', 0)}, "
                        f"ошибок запуска {attempts.get('launch_failures', 0)}."
                    )
                    print(
                        "Сбор через Codex: best-effort; нужны UserPromptSubmit, "
                        "модель Codex и MCP-инструмент learning_add_candidate."
                    )
                    provider = summary.get("provider_type")
                    if provider:
                        print(
                            "Дополнительный анализ: доступен только вручную "
                            "через learning process; автоматический запуск выключен."
                        )
                    else:
                        print(
                            "Дополнительный анализ: провайдер не настроен; "
                            "это не отключает сбор через Codex."
                        )
                else:
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
            elif args.action == "list":
                print(json.dumps(learning_service.list_events(limit=args.limit),
                                 ensure_ascii=False, indent=2))
            elif args.action == "attempts":
                print(json.dumps(learning_service.list_attempts(
                    limit=args.limit, failed_only=not args.all,
                ), ensure_ascii=False, indent=2))
            elif args.action == "process":
                print(json.dumps(learning_service.process_pending(limit=args.limit),
                                 ensure_ascii=False, indent=2))
            elif args.action == "retry":
                learning_service.retry_event(args.event_id)
                print(json.dumps({"event_id": args.event_id, "status": "pending_analysis"}))
            elif args.action == "verify":
                command = list(args.program_argv)
                if command and command[0] == "--":
                    command.pop(0)
                result = learning_service.verify_command(
                    args.task_id, args.summary, command, cwd=args.cwd,
                    timeout=args.timeout, changed_files=args.changed_file,
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if not result["process_succeeded"]:
                    return 1
            elif args.action == "withdraw":
                result = learning_service.withdraw_candidate(
                    args.knowledge_id, reason=args.reason
                )
                print(json.dumps({"id": result["id"], "status": result["status"]},
                                 ensure_ascii=False))
        elif args.command == "mcp":
            from ai_engineering_memory.mcp_server import serve
            serve()
        return 0
    except (storage.StorageError, OSError, TypeError, ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Создаёт Settings/Database для запуска CLI и изолирует его операции."""
    _configure_redirected_output()
    store = storage.create_storage()
    with store.activate():
        return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
