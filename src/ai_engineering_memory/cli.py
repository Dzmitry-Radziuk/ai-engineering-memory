"""Командный интерфейс локальной платформы памяти."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ai_engineering_memory import storage


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
        help="запустить MCP и проверить initialize/tools/list без вызова инструментов",
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
    skills = commands.add_parser("skills")
    skill_commands = skills.add_subparsers(dest="action", required=True)
    skill_commands.add_parser("list")
    proposals = skill_commands.add_parser("proposals")
    proposals.add_argument("--status", default="candidate")
    learning = commands.add_parser("learning")
    learning_commands = learning.add_subparsers(dest="action", required=True)
    learning_commands.add_parser("status")
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
    project_namespace = learning.project_namespace(cwd)
    candidates = storage.list_knowledge(
        status="candidate", namespace="global", limit=limit,
    )
    project_candidates = storage.list_knowledge(
        status="candidate", namespace=project_namespace, limit=limit,
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
        evidence_rows = storage.get_knowledge_evidence(knowledge_id)
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
            support = storage.get_learning_corroboration(knowledge_id)
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
            saved = storage.approve_knowledge(knowledge_id)
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
            saved = storage.reject_knowledge(knowledge_id, reason=reason)
            print(f"Запись {knowledge_id}: {saved['status']}.")
            continue
        print("Неизвестное действие; кандидат не изменён.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Выполняет команду и печатает результат в JSON или текстовом виде."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            storage.init_database()
            print(f"База готова: {storage.DATABASE_PATH}")
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
                database_path=storage.DATABASE_PATH,
                hooks_config_path=codex_home / "hooks.json",
                hook_log_path=codex_hooks._log_path(),
                mcp_config_path=codex_home / "config.toml",
                project_mcp_config_path=project_mcp_config_paths,
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
                hooks = report["hooks"]
                print(
                    f"Hooks: {labels[hooks['status']]} · "
                    f"UserPromptSubmit={hooks['events'].get('UserPromptSubmit', 'нет')} · "
                    f"Stop={hooks['events'].get('Stop', 'нет')}"
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
                        f"{memory_mcp['protocol_probe_tool_count']}; бизнес-инструменты не вызывались."
                    )
                elif args.probe_mcp:
                    probe_messages = {
                        "unsafe_command_skipped": "команда запуска не распознана; процесс не запускался",
                        "unsupported_environment_skipped": "есть переменные окружения вне AI Engineering Memory; процесс не запускался",
                        "timeout": "сервер не ответил за отведённое время",
                        "incompatible_server": "ответил сервер без ожидаемых инструментов",
                        "launcher_unavailable": "исполняемый файл не найден",
                        "disabled": "сервер выключен в конфигурации",
                        "invalid_config": "формат конфигурации некорректен",
                        "error": "не удалось выполнить MCP handshake",
                        "config_error": "не удалось прочитать конфигурацию",
                        "not_configured": "сервер не зарегистрирован",
                        "missing_config": "файл конфигурации не найден",
                    }
                    probe_status = memory_mcp.get("protocol_probe_status", "unknown")
                    probe_message = probe_messages.get(probe_status, "причина не определена")
                    print(f"  MCP protocol probe: {probe_status} · {probe_message}")
                    if memory_mcp.get("protocol_probe_error_type"):
                        print(f"  Тип ошибки: {memory_mcp['protocol_probe_error_type']}")
                project_scope = {
                    "configured_root": "закреплённый AI_MEMORY_PROJECT_ROOT",
                    "process_cwd": "рабочий каталог процесса MCP",
                    "invalid": "ошибка настройки",
                }.get(memory_mcp.get("project_scope"), "не проверена")
                print(f"Область проекта MCP: {project_scope}")
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
                    f"ошибок в последних строках: {hook_log['error_count']}"
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
                state = {
                    "configured": "настроены",
                    "inherited": "не подтверждены для MCP",
                    "not_configured": "отключены",
                    "unknown": "не удалось проверить",
                }.get(maintainer_actions["status"], "неизвестно")
                print(f"Решения maintainer: {state}")
            return 0 if report["overall"] == "ok" else 1
        elif args.command == "hooks":
            from ai_engineering_memory import hook_install

            result = hook_install.install_hooks(
                args.config, dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "status":
            entries = storage.list_knowledge(limit=500)
            print(json.dumps({"database": str(storage.DATABASE_PATH), "knowledge": len(entries),
                              "skills": len(storage.list_skills())}, ensure_ascii=False, indent=2))
        elif args.command == "backup":
            print(storage.backup_database(args.destination))
        elif args.command == "knowledge":
            if args.action == "list":
                result = storage.list_knowledge(status=args.status, namespace=args.namespace, limit=args.limit)
            elif args.action == "search":
                result = storage.search_knowledge(args.query, status=None if args.status == "all" else args.status,
                                                  namespace=args.namespace, limit=args.limit)
            elif args.action == "review":
                if not sys.stdin.isatty():
                    raise ValueError("Интерактивный review доступен только в терминале")
                return _review_candidates(args.cwd, limit=args.limit)
            elif args.action == "add":
                result = storage.create_knowledge(args.category,args.title,args.content,
                                                  technology=args.technology,namespace=args.namespace)
            else:
                result = getattr(storage, f"{args.action}_knowledge")(args.id)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif args.command == "skills":
            result = storage.list_skills() if args.action == "list" else storage.list_skill_proposals(args.status)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "learning":
            from ai_engineering_memory import learning

            if args.action == "status":
                summary = storage.learning_status_summary()
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
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            elif args.action == "list":
                print(json.dumps(storage.list_learning_events(limit=args.limit),
                                 ensure_ascii=False, indent=2))
            elif args.action == "attempts":
                print(json.dumps(storage.list_verification_attempts(
                    limit=args.limit, failed_only=not args.all,
                ), ensure_ascii=False, indent=2))
            elif args.action == "process":
                print(json.dumps(learning.process_pending(limit=args.limit),
                                 ensure_ascii=False, indent=2))
            elif args.action == "retry":
                storage.retry_learning_event(args.event_id)
                print(json.dumps({"event_id": args.event_id, "status": "pending_analysis"}))
            elif args.action == "verify":
                command = list(args.program_argv)
                if command and command[0] == "--":
                    command.pop(0)
                result = learning.verify_command(
                    args.task_id, args.summary, command, cwd=args.cwd,
                    timeout=args.timeout, changed_files=args.changed_file,
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if not result["process_succeeded"]:
                    return 1
            elif args.action == "withdraw":
                result = storage.withdraw_learning_candidate(
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


if __name__ == "__main__":
    raise SystemExit(main())
