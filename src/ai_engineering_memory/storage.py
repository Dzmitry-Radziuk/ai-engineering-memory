"""Локальное хранилище знаний, событий и предложений Skills."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from ai_engineering_memory import migrations
from ai_engineering_memory.database import (
    Database,
    active_database,
    current_database,
    use_database,
)
from ai_engineering_memory.privacy import (
    contains_sensitive_text as _contains_sensitive_text,
)
from ai_engineering_memory.settings import Settings

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = Settings.from_environment(repository_data_dir=DATA_DIR).database_path
LATEST_SCHEMA_VERSION = migrations.LATEST_SCHEMA_VERSION
_fts_unavailable = migrations.fts_unavailable
MAX_TEXT_LENGTH = 100_000
STATUSES = {"candidate", "approved", "rejected", "deprecated"}
MAX_LEARNING_ATTEMPTS = 3


class StorageError(ValueError):
    """Ошибка валидации или состояния локального хранилища."""


def require_maintainer(maintainer: str) -> str:
    """Проверяет локального владельца и отказывает, если владелец не настроен."""
    configured = os.environ.get("AI_MEMORY_MAINTAINER", "").strip()
    if not configured:
        raise StorageError(
            "AI_MEMORY_MAINTAINER не настроен; решение maintainer запрещено"
        )
    if not maintainer or maintainer != configured:
        raise StorageError(
            "Действие разрешено только настроенному локальному maintainer"
        )
    return configured


def current_database_path() -> Path:
    """Возвращает путь БД текущего вызова с поддержкой legacy default."""
    return current_database(DATABASE_PATH).path


@contextmanager
def _connect(
    *, read_only: bool = False, timeout: float = 10.0
) -> Iterator[sqlite3.Connection]:
    """Открывает изолированное соединение с локальной базой."""
    database = current_database(DATABASE_PATH)
    with database.connect(read_only=read_only, timeout=timeout) as connection:
        yield connection


def _last_insert_id(cursor: sqlite3.Cursor) -> int:
    """Возвращает ID вставленной строки или сообщает о нарушении SQLite-контракта."""
    if cursor.lastrowid is None:
        raise StorageError("SQLite не вернул ID созданной записи")
    return cursor.lastrowid


class Storage:
    """Совместимый storage facade, привязанный к одному Database instance."""

    def __init__(self, database: Database):
        """Создаёт facade без открытия соединения и изменения схемы."""
        self.database = database

    @property
    def database_path(self) -> Path:
        """Возвращает неизменяемый путь базы этого facade."""
        return self.database.path

    @contextmanager
    def activate(self) -> Iterator[None]:
        """Активирует базу для группы операций, сохраняя изоляцию контекста."""
        with use_database(self.database):
            yield

    def __getattr__(self, name: str) -> Any:
        """Вызывает legacy storage API внутри изолированного database context."""
        function = globals().get(name)
        if name.startswith("_") or not inspect.isfunction(function):
            raise AttributeError(name)

        @wraps(function)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            with use_database(self.database):
                return function(*args, **kwargs)

        return invoke


def create_storage(settings: Settings | None = None) -> Storage:
    """Создаёт storage facade с явно заданными или вычисленными настройками."""
    if settings is None and (database := active_database()) is not None:
        return Storage(database)
    resolved = settings or Settings.from_environment(repository_data_dir=DATA_DIR)
    return Storage(Database(resolved.database_path))


def init_database() -> None:
    """????????? ??????????? versioned migrations; latest schema ???????? ??? DDL."""
    current_database_path().parent.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        migrations.MigrationEngine().apply(connection)


def _validate_text(value: str, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    """Проверяет текстовое поле и возвращает очищенное значение."""
    if not isinstance(value, str) or not value.strip():
        raise StorageError(f"Поле {field} должно быть непустой строкой")
    if len(value) > maximum:
        raise StorageError(f"Поле {field} превышает лимит {maximum} символов")
    return value.strip()


def _validate_decision_reason(reason: str) -> str:
    """Ограничивает причину решения и отсекает частые приватные данные."""
    cleaned = _validate_text(reason, "reason", 500)
    if _contains_sensitive_text(cleaned):
        raise StorageError("Причина содержит запрещённые приватные данные")
    return cleaned


def create_knowledge(
    category: str,
    title: str,
    content: str,
    *,
    source_type: str = "manual",
    source_reference: str | None = None,
    technology: str = "",
    confidence: float = 0.5,
    namespace: str = "global",
    evidence: dict[str, Any] | None = None,
    publishable: bool = False,
) -> dict[str, Any]:
    """Создаёт знание в статусе кандидата и при необходимости добавляет доказательство."""
    init_database()
    category = _validate_text(category, "category", 100)
    title = _validate_text(title, "title", 500)
    content = _validate_text(content, "content")
    namespace = _validate_text(namespace, "namespace", 200)
    if not 0 <= confidence <= 1:
        raise StorageError("confidence должен быть от 0 до 1")
    digest = hashlib.sha256(
        (category.lower() + "\0" + title.lower() + "\0" + content.lower()).encode()
    ).hexdigest()
    now = datetime.now(UTC).isoformat()
    with _connect() as connection:
        duplicate = connection.execute(
            "SELECT id FROM knowledge WHERE content_hash = ? AND namespace = ?",
            (digest, namespace),
        ).fetchone()
        if duplicate:
            raise StorageError(
                f"В этом пространстве уже есть точный дубликат: id={duplicate['id']}"
            )
        cursor = connection.execute(
            """INSERT INTO knowledge
            (uuid,category,title,content,status,source_type,source_reference,technology,
             confidence,namespace,updated_at,content_hash,publishable)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                category,
                title,
                content,
                "candidate",
                source_type,
                source_reference,
                technology,
                confidence,
                namespace,
                now,
                digest,
                int(publishable),
            ),
        )
        knowledge_id = _last_insert_id(cursor)
        if evidence:
            connection.execute(
                "INSERT INTO evidence(knowledge_id,kind,result,source,checked_at,limitations,confidence) VALUES(?,?,?,?,?,?,?)",
                (
                    knowledge_id,
                    _validate_text(evidence["kind"], "kind", 100),
                    _validate_text(evidence["result"], "result", 1000),
                    str(evidence.get("source", ""))[:2000],
                    now,
                    str(evidence.get("limitations", ""))[:2000],
                    float(evidence.get("confidence", confidence)),
                ),
            )
        row = connection.execute(
            "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
        ).fetchone()
        return dict(row)


def add_knowledge(category: str, title: str, content: str) -> int:
    """Сохраняет знание через совместимый с прежней версией интерфейс."""
    return int(create_knowledge(category, title, content)["id"])


def get_knowledge(
    knowledge_id: int | None = None,
    *,
    _read_only: bool = False,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    """Возвращает одну запись по ID или все записи по убыванию ID."""
    if _read_only and not current_database_path().is_file():
        return None if knowledge_id is not None else []
    if not _read_only:
        init_database()
    with _connect(read_only=_read_only) as connection:
        if knowledge_id is not None:
            row = connection.execute(
                "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
            ).fetchone()
            return dict(row) if row else None
        return [
            dict(row)
            for row in connection.execute("SELECT * FROM knowledge ORDER BY id DESC")
        ]


def get_knowledge_by_ids(
    knowledge_ids: list[int],
    *,
    _read_only: bool = False,
) -> list[dict[str, Any]]:
    """Читает ограниченный набор строк одним соединением без изменения их порядка."""
    ids = list(dict.fromkeys(knowledge_ids))
    if not ids:
        return []
    if len(ids) > 900 or any(type(knowledge_id) is not int for knowledge_id in ids):
        raise StorageError("Допустимо не более 900 целочисленных ID")
    if _read_only and not current_database_path().is_file():
        return []
    if not _read_only:
        init_database()
    placeholders = ",".join("?" for _ in ids)
    with _connect(read_only=_read_only) as connection:
        rows = connection.execute(
            f"SELECT * FROM knowledge WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    by_id = {row["id"]: dict(row) for row in rows}
    return [by_id[knowledge_id] for knowledge_id in ids if knowledge_id in by_id]


def get_knowledge_evidence(knowledge_id: int) -> list[dict[str, Any]]:
    """Возвращает ограниченные свидетельства, прикреплённые к записи знания."""
    init_database()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT kind,result,source,checked_at,limitations,confidence "
            "FROM evidence WHERE knowledge_id=? ORDER BY id LIMIT 20",
            (knowledge_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_knowledge(
    *, status: str | None = None, namespace: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Выводит знания с ограничением объёма и фильтрами области видимости."""
    init_database()
    if not 1 <= limit <= 500:
        raise StorageError("limit должен быть от 1 до 500")
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in STATUSES:
            raise StorageError("Недопустимый статус знания")
        clauses.append("status=?")
        params.append(status)
    if namespace is not None:
        namespace = _validate_text(namespace, "namespace", 200)
        clauses.append("namespace=?")
        params.append(namespace)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect() as connection:
        return [
            dict(r)
            for r in connection.execute(
                f"SELECT * FROM knowledge{where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            )
        ]


def memory_overview(*, namespace: str | None = None) -> dict[str, Any]:
    """Считает знания по статусам, категориям и пространствам имён без изменения статусов."""
    init_database()
    clauses: list[str] = []
    params: list[Any] = []
    if namespace is not None:
        namespace = _validate_text(namespace, "namespace", 200)
        clauses.append("namespace=?")
        params.append(namespace)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    status_counts = {status: 0 for status in sorted(STATUSES)}
    category_counts: dict[str, int] = {}
    namespace_counts: dict[str, dict[str, Any]] = {}
    total = 0
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT namespace, category, status, COUNT(*) AS count "
            f"FROM knowledge{where} GROUP BY namespace, category, status "
            "ORDER BY namespace, category, status",
            params,
        ).fetchall()

    for row in rows:
        row_namespace = row["namespace"]
        category = row["category"]
        status = row["status"]
        count = int(row["count"])
        total += count
        status_counts[status] = status_counts.get(status, 0) + count
        category_counts[category] = category_counts.get(category, 0) + count
        namespace_summary = namespace_counts.setdefault(
            row_namespace,
            {"total": 0, "status_counts": {key: 0 for key in sorted(STATUSES)}},
        )
        namespace_summary["total"] += count
        namespace_summary["status_counts"][status] = (
            namespace_summary["status_counts"].get(status, 0) + count
        )

    return {
        "namespace_filter": namespace,
        "total": total,
        "status_counts": status_counts,
        "category_counts": category_counts,
        "namespace_counts": namespace_counts,
    }


def search_index_status() -> dict[str, Any]:
    """Проверяет наличие FTS-индекса без создания или миграции базы."""
    database_path = current_database_path()
    if not database_path.is_file():
        return {
            "status": "database_missing",
            "backend": "uninitialized",
            "fts_available": False,
            "triggers_available": False,
        }
    with _connect(read_only=True) as connection:
        objects = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            )
        }
        fts_available = "knowledge_fts" in objects
        triggers_available = {
            "knowledge_ai",
            "knowledge_ad",
            "knowledge_au",
        }.issubset(objects)
        schema_version = _database_schema_version(connection)
    return {
        "status": "ready" if fts_available and triggers_available else "fallback",
        "backend": "fts5" if fts_available and triggers_available else "like",
        "fts_available": fts_available,
        "triggers_available": triggers_available,
        "schema_version": schema_version,
    }


def rebuild_search_index() -> dict[str, Any]:
    """Перестраивает только FTS-копию из канонической таблицы knowledge."""
    if not current_database_path().is_file():
        raise StorageError("База не найдена; сначала выполните ai-memory setup")
    init_database()
    with _connect() as connection:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
            ).fetchone()
            is None
        ):
            raise StorageError("FTS5 недоступен; поиск продолжит работать через LIKE")
        try:
            connection.execute(
                "INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')"
            )
        except sqlite3.OperationalError as exc:
            if not migrations.fts_unavailable(exc):
                raise
            raise StorageError(
                "FTS5 недоступен; поиск продолжит работать через LIKE"
            ) from exc
    result = search_index_status()
    result["status"] = "rebuilt" if result["backend"] == "fts5" else "fallback"
    return result


def update_knowledge(knowledge_id: int, **changes: Any) -> dict[str, Any]:
    """Обновляет разрешённые поля записи в транзакции."""
    init_database()
    allowed = {
        "category",
        "title",
        "content",
        "technology",
        "confidence",
        "source_reference",
        "namespace",
        "publishable",
    }
    if not changes or set(changes) - allowed:
        raise StorageError("Переданы пустые или неподдерживаемые поля")
    if "confidence" in changes and not 0 <= changes["confidence"] <= 1:
        raise StorageError("confidence должен быть от 0 до 1")
    if "publishable" in changes:
        changes["publishable"] = int(bool(changes["publishable"]))
    for key in {
        "category",
        "title",
        "content",
        "technology",
        "namespace",
    } & changes.keys():
        changes[key] = _validate_text(changes[key], key)
    changes["updated_at"] = datetime.now(UTC).isoformat()
    with _connect() as connection:
        if "category" in changes or "title" in changes or "content" in changes:
            row = connection.execute(
                "SELECT category,title,content,namespace FROM knowledge WHERE id=?",
                (knowledge_id,),
            ).fetchone()
            if not row:
                raise StorageError("Знание не найдено")
            values = {**dict(row), **changes}
            changes["content_hash"] = hashlib.sha256(
                (
                    values["category"].lower()
                    + "\0"
                    + values["title"].lower()
                    + "\0"
                    + values["content"].lower()
                ).encode()
            ).hexdigest()
        assignments = ",".join(f"{key}=?" for key in changes)
        cursor = connection.execute(
            f"UPDATE knowledge SET {assignments} WHERE id=?",
            (*changes.values(), knowledge_id),
        )
        if not cursor.rowcount:
            raise StorageError("Знание не найдено")
        return dict(
            connection.execute(
                "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
            ).fetchone()
        )


def search_knowledge(
    query: str,
    *,
    category: str | None = None,
    technology: str | None = None,
    status: str | None = "approved",
    namespace: str | None = None,
    limit: int = 10,
    prompt_context: bool = False,
    _read_only: bool = False,
) -> list[dict[str, Any]]:
    """Ищет знания через FTS5 с безопасным резервным поиском LIKE."""
    if _read_only:
        if not current_database_path().is_file():
            return []
    else:
        init_database()
    query = _validate_text(query, "query", 10_000 if prompt_context else 500)
    if not 1 <= limit <= 100:
        raise StorageError("limit должен быть от 1 до 100")
    if status is not None:
        status = _validate_text(status, "status", 30)
        if status not in STATUSES:
            raise StorageError("Недопустимый статус знания")
    if category is not None:
        category = _validate_text(category, "category", 200)
    if namespace is not None:
        namespace = _validate_text(namespace, "namespace", 200)
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("k.status=?")
        params.append(status)
    if category is not None:
        clauses.append("k.category=?")
        params.append(category)
    if technology:
        clauses.append("k.technology LIKE ?")
        params.append(f"%{technology}%")
    if namespace is not None:
        clauses.append("k.namespace=?")
        params.append(namespace)
    filters = " AND ".join(clauses) or "1=1"
    tokens = re.findall(r"[\w-]+", query)
    if prompt_context:
        stop_words = {
            "about",
            "after",
            "again",
            "also",
            "and",
            "are",
            "can",
            "could",
            "does",
            "find",
            "from",
            "have",
            "help",
            "into",
            "just",
            "more",
            "most",
            "need",
            "please",
            "search",
            "should",
            "show",
            "some",
            "that",
            "their",
            "them",
            "then",
            "there",
            "these",
            "this",
            "those",
            "through",
            "very",
            "want",
            "what",
            "when",
            "where",
            "which",
            "with",
            "would",
            "надо",
            "есть",
            "для",
            "как",
            "мне",
            "нас",
            "нам",
            "наш",
            "наша",
            "наше",
            "наши",
            "это",
            "этот",
            "эта",
            "эти",
            "чтобы",
            "когда",
            "какой",
            "какая",
            "какие",
            "можно",
            "нужно",
            "пожалуйста",
            "покажи",
            "проверь",
            "сделай",
        }
        tokens = list(
            dict.fromkeys(
                token
                for token in tokens
                if len(token) >= 3 and token.casefold() not in stop_words
            )
        )[:8]
        if not tokens:
            return []
    with _connect(
        read_only=_read_only, timeout=1.0 if _read_only else 10.0
    ) as connection:
        if namespace is not None:
            namespace_clauses = ["namespace=?"]
            namespace_params: list[Any] = [namespace]
            if status is not None:
                namespace_clauses.append("status=?")
                namespace_params.append(status)
            exists = connection.execute(
                "SELECT 1 FROM knowledge WHERE "
                + " AND ".join(namespace_clauses)
                + " LIMIT 1",
                namespace_params,
            ).fetchone()
            if exists is None:
                return []
        if tokens:
            try:
                operators = (" AND ", " OR ") if prompt_context else (" AND ",)
                matches: list[dict[str, Any]] = []
                seen_ids: set[int] = set()
                for operator in operators:
                    token_matches = []
                    for token in tokens:
                        quoted_token = '"' + token.replace('"', '""') + '"*'
                        if prompt_context:
                            text_fields = ("title", "content", "category")
                            token_matches.append(
                                "("
                                + " OR ".join(
                                    f"{field} : {quoted_token}" for field in text_fields
                                )
                                + ")"
                            )
                        else:
                            token_matches.append(quoted_token)
                    match = operator.join(token_matches)
                    rows = connection.execute(
                        f"SELECT k.*,bm25(knowledge_fts,4.0,1.0,2.0,3.0) AS rank FROM knowledge_fts JOIN knowledge k ON k.id=knowledge_fts.rowid WHERE knowledge_fts MATCH ? AND {filters} ORDER BY rank LIMIT ?",
                        (match, *params, limit),
                    ).fetchall()
                    for row in rows:
                        if row["id"] not in seen_ids:
                            matches.append(dict(row))
                            seen_ids.add(row["id"])
                    if len(matches) >= limit:
                        break
                return matches[:limit]
            except sqlite3.OperationalError as exc:
                if not _fts_unavailable(exc):
                    raise
        if prompt_context:
            escaped_tokens = [
                token.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                for token in tokens
            ]
            fallback_matches: list[dict[str, Any]] = []
            fallback_seen_ids: set[int] = set()
            text_fields = ("title", "content", "category")
            text_match = (
                "("
                + " OR ".join(f"k.{field} LIKE ? ESCAPE '!'" for field in text_fields)
                + ")"
            )
            score_terms = [text_match for _ in escaped_tokens]
            term_score = " + ".join(
                f"CASE WHEN {term} THEN 1 ELSE 0 END" for term in score_terms
            )
            title_score = " + ".join(
                "CASE WHEN k.title LIKE ? ESCAPE '!' THEN 1 ELSE 0 END"
                for _ in escaped_tokens
            )
            score_params = tuple(
                pattern
                for token in escaped_tokens
                for pattern in (f"%{token}%",) * len(text_fields)
            ) + tuple(f"%{token}%" for token in escaped_tokens)
            for operator in (" AND ", " OR "):
                term_clauses = [text_match for _ in escaped_tokens]
                rows = connection.execute(
                    f"SELECT k.* FROM knowledge k WHERE ({operator.join(term_clauses)}) "
                    f"AND {filters} ORDER BY ({term_score}) DESC, "
                    f"({title_score}) DESC, k.updated_at DESC LIMIT ?",
                    tuple(
                        pattern
                        for token in escaped_tokens
                        for pattern in (f"%{token}%",) * len(text_fields)
                    )
                    + (*params, *score_params, limit),
                ).fetchall()
                for row in rows:
                    if row["id"] not in fallback_seen_ids:
                        fallback_matches.append(dict(row))
                        fallback_seen_ids.add(row["id"])
                if len(matches) >= limit:
                    break
            return fallback_matches[:limit]

        escaped_query = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        pattern = f"%{escaped_query}%"
        rows = connection.execute(
            f"SELECT k.* FROM knowledge k WHERE (k.title LIKE ? ESCAPE '!' OR k.content LIKE ? ESCAPE '!' OR k.category LIKE ? ESCAPE '!' OR k.technology LIKE ? ESCAPE '!') AND {filters} ORDER BY k.updated_at DESC LIMIT ?",
            (pattern, pattern, pattern, pattern, *params, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def _set_status(
    knowledge_id: int,
    status: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Меняет статус знания и атомарно записывает решение в журнал."""
    if status not in STATUSES:
        raise StorageError("Недопустимый статус знания")
    if reason is not None:
        reason = _validate_decision_reason(reason)
    init_database()
    now = datetime.now(UTC).isoformat()
    with _connect() as connection:
        row = connection.execute(
            "SELECT status,source_type,source_reference,namespace "
            "FROM knowledge WHERE id=?",
            (knowledge_id,),
        ).fetchone()
        if not row:
            raise StorageError("Знание не найдено")
        previous_status = str(row["status"])
        if previous_status == status:
            return dict(
                connection.execute(
                    "SELECT * FROM knowledge WHERE id=?",
                    (knowledge_id,),
                ).fetchone()
            )
        connection.execute(
            "UPDATE knowledge SET status=?,updated_at=?,approved_at=? WHERE id=?",
            (status, now, now if status == "approved" else None, knowledge_id),
        )
        event_ids: list[str] = []
        reference = str(row["source_reference"] or "")
        if reference.startswith("event:"):
            event_ids.append(reference.removeprefix("event:"))
        elif row["source_type"] == "cross_project_learning":
            event_ids.extend(
                item["event_id"]
                for item in connection.execute(
                    "SELECT event_id FROM learning_corroborations "
                    "WHERE global_knowledge_id=? ORDER BY created_at",
                    (knowledge_id,),
                ).fetchall()
            )
        if not event_ids:
            event_ids.append(f"knowledge:{knowledge_id}")
        details = json.dumps(
            {
                "source_type": row["source_type"],
                "source_reference": reference,
                "namespace": row["namespace"],
                "reason": reason,
            },
            ensure_ascii=False,
        )
        for event_id in dict.fromkeys(event_ids):
            connection.execute(
                "INSERT INTO learning_event_audit "
                "(event_id,knowledge_id,action,status_from,status_to,occurred_at,details) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    event_id,
                    knowledge_id,
                    "knowledge_status_changed",
                    previous_status,
                    status,
                    now,
                    details,
                ),
            )
        return dict(
            connection.execute(
                "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
            ).fetchone()
        )


def approve_knowledge(knowledge_id: int) -> dict[str, Any]:
    """Подтверждает запись явным действием владельца."""
    return _set_status(knowledge_id, "approved")


def reject_knowledge(
    knowledge_id: int,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Отклоняет запись явным действием владельца."""
    return _set_status(knowledge_id, "rejected", reason=reason)


def deprecate_knowledge(knowledge_id: int) -> dict[str, Any]:
    """Помечает запись устаревшей, сохраняя её историю."""
    return _set_status(knowledge_id, "deprecated")


def add_evidence(
    knowledge_id: int,
    *,
    kind: str,
    result: str,
    source: str = "",
    limitations: str = "",
    confidence: float = 0.5,
) -> int:
    """Прикрепляет проверяемое свидетельство к записи знания."""
    if not 0 <= confidence <= 1:
        raise StorageError("confidence должен быть от 0 до 1")
    init_database()
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO evidence(knowledge_id,kind,result,source,checked_at,limitations,confidence) VALUES(?,?,?,?,?,?,?)",
            (
                knowledge_id,
                _validate_text(kind, "kind", 100),
                _validate_text(result, "result", 1000),
                source[:2000],
                datetime.now(UTC).isoformat(),
                limitations[:2000],
                confidence,
            ),
        )
        return _last_insert_id(cursor)


def ingest_task_event(event: dict[str, Any]) -> dict[str, Any]:
    """Сохраняет обезличенное событие, не принимая его за доказательство проверки."""
    init_database()
    required = {
        "event_id",
        "project_id",
        "task_id",
        "client_name",
        "task_description",
        "outcome",
        "completed_at",
    }
    if missing := required - event.keys():
        raise StorageError("Не заданы обязательные поля: " + ", ".join(sorted(missing)))
    status = event.get("status", "task_completed")
    if not isinstance(status, str) or status not in {
        "session_finished",
        "task_completed",
        "task_verified",
    }:
        raise StorageError("Недопустимый статус события")
    test_results = event.get("test_results", {})
    if not isinstance(test_results, dict):
        raise StorageError("test_results должен быть JSON-объектом")
    if status == "task_verified":
        raise StorageError("task_verified доступен только локальному verify API")
    identifiers = {}
    for field in ("event_id", "project_id", "task_id"):
        value = _validate_text(event[field], field, 200)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
            raise StorageError(f"Поле {field} содержит недопустимые символы")
        identifiers[field] = value
    # Validate sizes and types, but never persist free text from unverified MCP input.
    _validate_text(event["client_name"], "client_name", 100)
    _validate_text(event["task_description"], "task_description", 2_000)
    _validate_text(event["outcome"], "outcome", 1_000)
    completed_at = _validate_text(event["completed_at"], "completed_at", 100)
    try:
        parsed_completed_at = datetime.fromisoformat(completed_at)
    except ValueError as exc:
        raise StorageError("completed_at должен быть датой ISO 8601") from exc
    if parsed_completed_at.tzinfo is None:
        parsed_completed_at = parsed_completed_at.replace(tzinfo=UTC)
    safe_completed_at = parsed_completed_at.astimezone(UTC).isoformat()
    # Caller-supplied descriptions, outcomes, files, and test output are not evidence.
    values = (
        identifiers["event_id"],
        identifiers["project_id"],
        identifiers["task_id"],
        "MCP",
        "Unverified task event",
        "[]",
        "{}",
        "Awaiting local verification",
        safe_completed_at,
    )
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO task_events "
            "(event_id,project_id,task_id,client_name,task_description,changed_files,"
            "test_results,outcome,completed_at,verification_id,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (*values, None, "waiting_for_verification"),
        )
        row = connection.execute(
            "SELECT * FROM task_events WHERE event_id=?", (values[0],)
        ).fetchone()
        return dict(row)


def record_verified_task(
    event: dict[str, Any], verification: dict[str, Any]
) -> dict[str, Any]:
    """Записывает локально выполненную проверку и связанное событие очереди."""
    init_database()
    required = {
        "verification_id",
        "verification_type",
        "exit_code",
        "started_at",
        "completed_at",
        "artifact_digest",
        "command_digest",
        "verifier",
    }
    if missing := required - verification.keys():
        raise StorageError("Не заданы поля проверки: " + ", ".join(sorted(missing)))
    if verification["verification_type"] != "command_exit_code":
        raise StorageError("Недопустимый тип локальной проверки")
    project_id = _validate_text(event.get("project_id", ""), "project_id", 200)
    raw_fingerprint = verification.get("project_fingerprint")
    project_fingerprint = (
        None
        if raw_fingerprint is None
        else _validate_text(str(raw_fingerprint), "project_fingerprint", 80)
    )
    if project_fingerprint is not None and not re.fullmatch(
        r"origin-v3:[a-f0-9]{64}", project_fingerprint
    ):
        raise StorageError("Некорректный отпечаток проекта")
    task_id = _validate_text(event.get("task_id", ""), "task_id", 200)
    if event.get("status") != "task_verified" or int(verification["exit_code"]) != 0:
        raise StorageError("Проверенное событие требует успешный локальный процесс")
    event_id = _validate_text(event.get("event_id", ""), "event_id", 200)
    task_description = _validate_text(
        event.get("task_description", ""), "task_description", 1000
    )
    completed_at = _validate_text(event.get("completed_at", ""), "completed_at", 100)
    verification_result = event.get("verification_result")
    if verification_result is None:
        # Совместимость для вызовов, созданных до нейтрального формата.
        verification_result = event.get("test_results")
        process_succeeded = (
            isinstance(verification_result, dict)
            and verification_result.get("passed") is True
        )
    else:
        process_succeeded = (
            isinstance(verification_result, dict)
            and verification_result.get("process_succeeded") is True
        )
    if not process_succeeded:
        raise StorageError("Проверенное событие требует успешного завершения процесса")
    verification_values = (
        _validate_text(str(verification["verification_id"]), "verification_id", 200),
        project_id,
        task_id,
        "command_exit_code",
        0,
        _validate_text(str(verification["started_at"]), "started_at", 100),
        _validate_text(str(verification["completed_at"]), "completed_at", 100),
        _validate_text(str(verification["artifact_digest"]), "artifact_digest", 128),
        _validate_text(str(verification["command_digest"]), "command_digest", 128),
        "ai-memory-cli",
        "trusted_local_process",
        project_fingerprint,
    )
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO verification_records "
            "(verification_id,project_id,task_id,verification_type,exit_code,started_at,"
            "completed_at,artifact_digest,command_digest,verifier,trust_status,project_fingerprint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            verification_values,
        )
        saved_verification = connection.execute(
            "SELECT * FROM verification_records WHERE verification_id=?",
            (verification_values[0],),
        ).fetchone()
        if (
            not saved_verification
            or saved_verification["exit_code"] != 0
            or saved_verification["trust_status"] != "trusted_local_process"
            or saved_verification["project_id"] != project_id
            or saved_verification["task_id"] != task_id
            or saved_verification["project_fingerprint"] != project_fingerprint
        ):
            raise StorageError("Локальная проверка не прошла валидацию")
        inserted = connection.execute(
            "INSERT OR IGNORE INTO task_events "
            "(event_id,project_id,task_id,client_name,task_description,changed_files,"
            "test_results,outcome,completed_at,status,verification_id,project_fingerprint) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending_analysis',?,?)",
            (
                event_id,
                project_id,
                task_id,
                "Codex",
                task_description,
                json.dumps(event.get("changed_files", []), ensure_ascii=False),
                json.dumps(verification_result, ensure_ascii=False),
                "Locally verified command completed successfully",
                completed_at,
                verification_values[0],
                project_fingerprint,
            ),
        )
        if inserted.rowcount:
            connection.execute(
                "INSERT INTO learning_event_audit "
                "(event_id,action,status_to,occurred_at,details) VALUES(?,?,?,?,?)",
                (
                    event_id,
                    "verified_event_created",
                    "pending_analysis",
                    completed_at,
                    json.dumps(
                        {"verification_id": verification_values[0]}, ensure_ascii=False
                    ),
                ),
            )
        row = connection.execute(
            "SELECT * FROM task_events WHERE event_id=?", (event_id,)
        ).fetchone()
        return dict(row)


def record_failed_verification(
    *, project_id: str, task_id: str, verification: dict[str, Any]
) -> str:
    """Фиксирует только метаданные неуспешного локального запуска."""
    init_database()
    verification_id = _validate_text(
        str(verification.get("verification_id", "")), "verification_id", 200
    )
    verification_type = str(verification.get("verification_type", "command_exit_code"))
    if verification_type not in {"command_exit_code", "command_launch"}:
        raise StorageError("Недопустимый тип неуспешной проверки")
    raw_fingerprint = verification.get("project_fingerprint")
    project_fingerprint = (
        None
        if raw_fingerprint is None
        else _validate_text(str(raw_fingerprint), "project_fingerprint", 80)
    )
    if project_fingerprint is not None and not re.fullmatch(
        r"origin-v3:[a-f0-9]{64}", project_fingerprint
    ):
        raise StorageError("Некорректный отпечаток проекта")
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO verification_records "
            "(verification_id,project_id,task_id,verification_type,exit_code,started_at,"
            "completed_at,artifact_digest,command_digest,verifier,trust_status,project_fingerprint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                verification_id,
                _validate_text(project_id, "project_id", 200),
                _validate_text(task_id, "task_id", 200),
                verification_type,
                int(verification["exit_code"]),
                str(verification["started_at"]),
                str(verification["completed_at"]),
                str(verification["artifact_digest"]),
                str(verification["command_digest"]),
                "ai-memory-cli",
                "trusted_local_launcher"
                if verification_type == "command_launch"
                else "trusted_local_process",
                project_fingerprint,
            ),
        )
    return verification_id


def list_learning_events(*, limit: int = 100) -> list[dict[str, Any]]:
    """Возвращает ограниченный список событий очереди без секретных данных."""
    init_database()
    if not 1 <= limit <= 500:
        raise StorageError("limit должен быть от 1 до 500")
    with _connect() as connection:
        rows = connection.execute(
            "SELECT event_id,project_id,task_id,status,attempt_count,completed_at,"
            "analyzed_at,last_error_type,verification_id FROM task_events "
            "ORDER BY completed_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]


def get_verification(verification_id: str) -> dict[str, Any] | None:
    """Возвращает метаданные доверенной проверки без команды и её вывода."""
    init_database()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM verification_records WHERE verification_id=?",
            (verification_id,),
        ).fetchone()
        return dict(row) if row else None


def list_verification_attempts(
    *, limit: int = 20, failed_only: bool = True
) -> list[dict[str, Any]]:
    """Показывает последние проверки без команд, вывода и абсолютных путей."""
    init_database()
    if not 1 <= limit <= 100:
        raise StorageError("limit должен быть от 1 до 100")
    condition = "WHERE exit_code != 0" if failed_only else ""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT verification_id,project_id,task_id,verification_type,exit_code,"
            "started_at,completed_at,trust_status FROM verification_records "
            f"{condition} ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_learning_event_by_verification(verification_id: str) -> dict[str, Any] | None:
    """Возвращает только событие с точной ссылкой на verifier."""
    init_database()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM task_events WHERE verification_id=? ORDER BY completed_at LIMIT 1",
            (verification_id,),
        ).fetchone()
        return dict(row) if row else None


def get_learning_corroboration(knowledge_id: int) -> dict[str, Any]:
    """Возвращает число независимых подтверждений global-записи без путей проектов."""
    init_database()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT project_namespace,event_id,verification_id,project_knowledge_id,created_at, "
            "project_fingerprint "
            "FROM learning_corroborations WHERE global_knowledge_id=? "
            "ORDER BY created_at,project_namespace",
            (knowledge_id,),
        ).fetchall()
        counts = connection.execute(
            "SELECT COUNT(DISTINCT CASE WHEN project_fingerprint LIKE 'origin-v3:%' "
            "THEN project_fingerprint END) AS independent_count, "
            "COALESCE(SUM(CASE WHEN project_fingerprint IS NULL THEN 1 ELSE 0 END), 0) "
            "AS legacy_count, "
            "COALESCE(SUM(CASE WHEN project_fingerprint IS NOT NULL "
            "AND project_fingerprint NOT LIKE 'origin-v3:%' THEN 1 ELSE 0 END), 0) "
            "AS legacy_identity_count "
            "FROM learning_corroborations WHERE global_knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
    return {
        "knowledge_id": knowledge_id,
        "independent_project_count": int(counts["independent_count"]),
        "legacy_unfingerprinted_support_count": int(counts["legacy_count"]),
        "legacy_identity_support_count": int(counts["legacy_identity_count"]),
        "supports": [dict(row) for row in rows],
    }


def _lesson_tokens(value: str) -> set[str]:
    """Возвращает токены для консервативного сравнения похожих уроков."""
    return set(re.findall(r"[a-z0-9а-яё]{3,}", value.casefold()))


def _verification_evidence_text(rationale: str, verification_id: str) -> str:
    """Разделяет серверный факт exit code и непроверенный комментарий модели."""
    return (
        "Факт verifier-а: локальная команда завершилась с exit code 0. "
        "Её релевантность задаче и полнота проверки не оценивались.\n\n"
        f"Комментарий модели (не подтверждён verifier-ом): {rationale}\n\n"
        f"Ссылка: verification:{verification_id}"
    )


def create_codex_learning_candidate(
    *,
    verification_id: str,
    category: str,
    title: str,
    content: str,
    rationale: str,
    limitations: str,
    confidence: float,
    evidence_refs: list[str],
    analysis_json: str,
) -> dict[str, Any]:
    """Создаёт project-кандидат; global-кандидат требует ссылки на файлы из разных проектов."""
    init_database()
    category = _validate_text(category, "category", 40)
    title = _validate_text(title, "title", 200)
    content = _validate_text(content, "content", 3_500)
    rationale = _validate_text(rationale, "rationale", 1_000)
    limitations = _validate_text(limitations, "limitations", 500)
    verification_id = _validate_text(verification_id, "verification_id", 64)
    if not re.fullmatch(r"[a-f0-9]{64}", verification_id):
        raise StorageError("Некорректный verification_id")
    if (
        not 0 <= confidence <= 1
        or not isinstance(evidence_refs, list)
        or not evidence_refs
    ):
        raise StorageError("Некорректная confidence или evidence_refs")
    if len(analysis_json) > 8_000:
        raise StorageError("Слишком большой структурированный вывод")
    now = datetime.now(UTC).isoformat()
    digest = hashlib.sha256(
        (
            category.casefold() + "\0" + title.casefold() + "\0" + content.casefold()
        ).encode()
    ).hexdigest()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT e.*,v.exit_code,v.trust_status, "
            "v.project_fingerprint AS verification_project_fingerprint "
            "FROM task_events e "
            "JOIN verification_records v ON v.verification_id=e.verification_id "
            "WHERE e.verification_id=? ORDER BY e.completed_at LIMIT 1",
            (verification_id,),
        ).fetchone()
        if (
            not row
            or row["exit_code"] != 0
            or row["trust_status"] != "trusted_local_process"
            or not re.fullmatch(r"project:[a-f0-9]{16}", row["project_id"])
        ):
            raise StorageError("Требуется успешная локальная проверка этого проекта")
        namespace = row["project_id"]
        current_fingerprint = row["verification_project_fingerprint"]
        event_id = row["event_id"]
        existing_for_event = connection.execute(
            "SELECT id,status,namespace FROM knowledge WHERE source_type='learning_engine' "
            "AND source_reference=?",
            (f"event:{event_id}",),
        ).fetchone()
        if existing_for_event:
            return {
                "knowledge_id": existing_for_event["id"],
                "status": existing_for_event["status"],
                "namespace": existing_for_event["namespace"],
                "event_id": event_id,
                "verification_id": verification_id,
                "idempotent": True,
            }
        if row["status"] not in {"pending_analysis", "retryable_error"}:
            raise StorageError("Событие уже обработано или не ожидает анализа")

        project_files = set(json.loads(row["changed_files"] or "[]"))
        has_current_file_evidence = bool(project_files) and any(
            reference.startswith("file:")
            for reference in evidence_refs
            if isinstance(reference, str)
        )
        expected_verification = f"verification:{verification_id}"
        if expected_verification not in evidence_refs:
            raise StorageError("evidence_refs должен содержать точный verification ID")
        for reference in evidence_refs:
            if not isinstance(reference, str) or len(reference) > 300:
                raise StorageError("Некорректная ссылка evidence")
            if reference.startswith("verification:"):
                if reference != expected_verification:
                    raise StorageError("Ссылка на другую проверку запрещена")
            elif reference.startswith("file:"):
                if reference[5:] not in project_files:
                    raise StorageError(
                        "Файл evidence отсутствует среди проверенных файлов"
                    )
            else:
                raise StorageError(
                    "Допустимы только ссылки на verification и changed files"
                )

        proposed_tokens = _lesson_tokens(title + " " + content)
        matches: list[tuple[float, sqlite3.Row]] = []
        rows = connection.execute(
            "SELECT k.id,k.title,k.content,k.namespace,k.status,k.content_hash, "
            "k.source_reference,k.source_type,v.project_fingerprint, "
            "e.changed_files AS project_changed_files, "
            "EXISTS(SELECT 1 FROM evidence ev WHERE ev.knowledge_id=k.id "
            "AND (ev.source LIKE '%; file:%' OR ev.source LIKE 'file:%')) "
            "AS has_file_evidence "
            "FROM knowledge k "
            "LEFT JOIN task_events e ON k.source_type='learning_engine' "
            "AND e.event_id=substr(k.source_reference,7) "
            "LEFT JOIN verification_records v ON v.verification_id=e.verification_id "
            "WHERE k.status IN ('approved','candidate') "
            "ORDER BY k.id DESC LIMIT 10_000",
        ).fetchall()
        for candidate in rows:
            existing_tokens = _lesson_tokens(
                candidate["title"] + " " + candidate["content"]
            )
            if candidate["content_hash"] == digest:
                matches.append((1.0, candidate))
                continue
            union = proposed_tokens | existing_tokens
            score = len(proposed_tokens & existing_tokens) / len(union) if union else 0
            if score >= 0.82:
                matches.append((score, candidate))

        global_matches = [
            (score, item) for score, item in matches if item["namespace"] == "global"
        ]
        local_matches = [
            (score, item) for score, item in matches if item["namespace"] == namespace
        ]
        cross_project_matches = [
            (score, item)
            for score, item in matches
            if item["namespace"].startswith("project:")
            and item["namespace"] != namespace
            and item["source_type"] == "learning_engine"
            and item["has_file_evidence"]
            and bool(json.loads(item["project_changed_files"] or "[]"))
            and isinstance(current_fingerprint, str)
            and current_fingerprint.startswith("origin-v3:")
            and isinstance(item["project_fingerprint"], str)
            and item["project_fingerprint"].startswith("origin-v3:")
            and item["project_fingerprint"] != current_fingerprint
        ]
        same_project_matches = [
            (score, item)
            for score, item in matches
            if item["source_type"] == "learning_engine"
            and item["namespace"] != namespace
            and isinstance(current_fingerprint, str)
            and current_fingerprint.startswith("origin-v3:")
            and item["project_fingerprint"] == current_fingerprint
        ]

        def add_corroboration(
            global_id: int,
            project_knowledge_id: int | None,
            support_namespace: str,
            support_event_id: str,
            support_verification_id: str,
            support_fingerprint: str | None,
        ) -> bool:
            inserted = (
                connection.execute(
                    "INSERT OR IGNORE INTO learning_corroborations "
                    "(global_knowledge_id,project_knowledge_id,project_namespace,event_id,"
                    "verification_id,created_at,project_fingerprint) VALUES(?,?,?,?,?,?,?)",
                    (
                        global_id,
                        project_knowledge_id,
                        support_namespace,
                        support_event_id,
                        support_verification_id,
                        now,
                        support_fingerprint,
                    ),
                ).rowcount
                == 1
            )
            if inserted:
                connection.execute(
                    "INSERT INTO evidence "
                    "(knowledge_id,kind,result,source,checked_at,limitations,confidence) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        global_id,
                        "independent_project_verification",
                        "Lesson independently observed in another verified project context.",
                        f"event:{support_event_id}; verification:{support_verification_id}",
                        now,
                        "Project identity is stored only as one-way namespace and repository hashes.",
                        confidence,
                    ),
                )
                connection.execute(
                    "INSERT INTO learning_event_audit "
                    "(event_id,knowledge_id,action,status_from,status_to,occurred_at,details) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        support_event_id,
                        global_id,
                        "cross_project_corroboration_added",
                        None,
                        None,
                        now,
                        json.dumps(
                            {
                                "global_knowledge_id": global_id,
                                "project_namespace": support_namespace,
                            }
                        ),
                    ),
                )
            return inserted

        # Same-project repeats do not create more candidates. A matching global
        # record can still receive one support row per independent project.
        duplicate_matches = local_matches or global_matches or same_project_matches
        if duplicate_matches:
            _, duplicate = max(duplicate_matches, key=lambda item: item[0])
            if (
                global_matches
                and has_current_file_evidence
                and isinstance(current_fingerprint, str)
                and current_fingerprint.startswith("origin-v3:")
            ):
                global_id = int(max(global_matches, key=lambda item: item[0])[1]["id"])
                existing_local_id = (
                    int(local_matches[0][1]["id"]) if local_matches else None
                )
                add_corroboration(
                    global_id,
                    existing_local_id,
                    namespace,
                    event_id,
                    verification_id,
                    current_fingerprint,
                )
            connection.execute(
                "UPDATE task_events SET status='no_insight',analysis=?,analyzed_at=?,locked_at=NULL "
                "WHERE event_id=?",
                (analysis_json, now, event_id),
            )
            connection.execute(
                "INSERT INTO learning_event_audit "
                "(event_id,action,status_from,status_to,occurred_at,details) VALUES(?,?,?,?,?,?)",
                (
                    event_id,
                    "candidate_duplicate_skipped",
                    row["status"],
                    "no_insight",
                    now,
                    json.dumps(
                        {
                            "matched_knowledge_id": duplicate["id"],
                            "matched_namespace": duplicate["namespace"],
                            "namespace": namespace,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            corroborations = 0
            if global_matches:
                corroborations = int(
                    connection.execute(
                        "SELECT COUNT(DISTINCT project_fingerprint) "
                        "FROM learning_corroborations WHERE global_knowledge_id=? "
                        "AND project_fingerprint LIKE 'origin-v3:%'",
                        (global_id,),
                    ).fetchone()[0]
                )
            return {
                "knowledge_id": duplicate["id"],
                "status": "duplicate_skipped",
                "namespace": namespace,
                "event_id": event_id,
                "verification_id": verification_id,
                "global_candidate_id": global_id if global_matches else None,
                "independent_project_count": corroborations,
            }

        cursor = connection.execute(
            "INSERT INTO knowledge (uuid,category,title,content,status,source_type,"
            "source_reference,technology,confidence,namespace,updated_at,content_hash,publishable) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                str(uuid.uuid4()),
                category,
                title,
                content,
                "candidate",
                "learning_engine",
                f"event:{event_id}",
                "",
                confidence,
                namespace,
                now,
                digest,
            ),
        )
        knowledge_id = _last_insert_id(cursor)
        evidence_result = _verification_evidence_text(rationale, verification_id)
        connection.execute(
            "INSERT INTO evidence (knowledge_id,kind,result,source,checked_at,limitations,confidence) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                knowledge_id,
                "local_process_verification",
                evidence_result[:1_000],
                "; ".join([f"event:{event_id}", *evidence_refs])[:2_000],
                now,
                limitations,
                confidence,
            ),
        )
        connection.execute(
            "UPDATE task_events SET status='candidate_created',analysis=?,analyzed_at=?,locked_at=NULL "
            "WHERE event_id=?",
            (analysis_json, now, event_id),
        )
        connection.execute(
            "INSERT INTO learning_event_audit "
            "(event_id,knowledge_id,action,status_from,status_to,occurred_at,details) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                knowledge_id,
                "candidate_created",
                row["status"],
                "candidate_created",
                now,
                json.dumps(
                    {
                        "namespace": namespace,
                        "verification_id": verification_id,
                        "evidence_refs": evidence_refs,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

        global_candidate_id: int | None = None
        independent_project_count = 0
        if cross_project_matches and has_current_file_evidence:
            project_candidates: dict[str, sqlite3.Row] = {
                current_fingerprint: connection.execute(
                    "SELECT k.id,k.namespace,k.source_reference,v.project_fingerprint "
                    "FROM knowledge k JOIN task_events e "
                    "ON e.event_id=substr(k.source_reference,7) "
                    "JOIN verification_records v ON v.verification_id=e.verification_id "
                    "WHERE k.id=?",
                    (knowledge_id,),
                ).fetchone()
            }
            for _, matched in cross_project_matches:
                project_candidates.setdefault(matched["project_fingerprint"], matched)
            if len(project_candidates) >= 2:
                rollup_key = (
                    "corroborated:"
                    + hashlib.sha256(
                        "|".join(
                            str(item["id"])
                            for item in sorted(
                                project_candidates.values(),
                                key=lambda value: value["id"],
                            )
                        ).encode()
                    ).hexdigest()
                )
                rollup_hash = hashlib.sha256(
                    (
                        category.casefold()
                        + "\0"
                        + title.casefold()
                        + "\0"
                        + content.casefold()
                    ).encode()
                ).hexdigest()
                cursor = connection.execute(
                    "INSERT INTO knowledge "
                    "(uuid,category,title,content,status,source_type,source_reference,technology,"
                    "confidence,namespace,updated_at,content_hash,publishable) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        str(uuid.uuid4()),
                        category,
                        title,
                        content,
                        "candidate",
                        "cross_project_learning",
                        rollup_key,
                        "",
                        confidence,
                        "global",
                        now,
                        rollup_hash,
                    ),
                )
                global_candidate_id = _last_insert_id(cursor)
                for support_fingerprint, source_candidate in project_candidates.items():
                    support_event_id = str(source_candidate["source_reference"])[6:]
                    support_event = connection.execute(
                        "SELECT verification_id FROM task_events WHERE event_id=?",
                        (support_event_id,),
                    ).fetchone()
                    if not support_event or not support_event["verification_id"]:
                        raise StorageError(
                            "Не найдена проверка межпроектного кандидата"
                        )
                    add_corroboration(
                        global_candidate_id,
                        int(source_candidate["id"]),
                        source_candidate["namespace"],
                        support_event_id,
                        support_event["verification_id"],
                        support_fingerprint,
                    )
                independent_project_count = len(project_candidates)
                for source_candidate in project_candidates.values():
                    support_event_id = str(source_candidate["source_reference"])[6:]
                    connection.execute(
                        "INSERT INTO learning_event_audit "
                        "(event_id,knowledge_id,action,status_from,status_to,occurred_at,details) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            support_event_id,
                            global_candidate_id,
                            "cross_project_candidate_created",
                            None,
                            "candidate",
                            now,
                            json.dumps(
                                {"independent_project_count": independent_project_count}
                            ),
                        ),
                    )
        saved = connection.execute(
            "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
        ).fetchone()
        return {
            "knowledge_id": knowledge_id,
            "status": saved["status"],
            "namespace": saved["namespace"],
            "event_id": event_id,
            "verification_id": verification_id,
            "idempotent": False,
            "global_candidate_id": global_candidate_id,
            "independent_project_count": independent_project_count,
        }


def claim_learning_event(*, stale_after_seconds: int = 300) -> dict[str, Any] | None:
    """Атомарно захватывает одно ожидающее или просроченное событие анализа."""
    init_database()
    now = datetime.now(UTC)
    now_text = now.isoformat()
    stale_before = datetime.fromtimestamp(
        now.timestamp() - stale_after_seconds, UTC
    ).isoformat()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        expired = connection.execute(
            "SELECT event_id,attempt_count FROM task_events WHERE status='processing' "
            "AND locked_at<=? AND attempt_count>=?",
            (stale_before, MAX_LEARNING_ATTEMPTS),
        ).fetchall()
        for row in expired:
            connection.execute(
                "INSERT INTO learning_event_audit "
                "(event_id,action,status_from,status_to,occurred_at,details) "
                "VALUES(?,?,?,?,?,?)",
                (
                    row["event_id"],
                    "processing_lease_expired",
                    "processing",
                    "permanent_error",
                    now_text,
                    json.dumps({"attempt_count": row["attempt_count"]}),
                ),
            )
        connection.execute(
            "UPDATE task_events SET status='permanent_error',locked_at=NULL,"
            "last_error_type='ProcessingLeaseExpired' WHERE status='processing' "
            "AND locked_at<=? AND attempt_count>=?",
            (stale_before, MAX_LEARNING_ATTEMPTS),
        )
        row = connection.execute(
            "SELECT * FROM task_events WHERE verification_id IS NOT NULL AND "
            "attempt_count<? AND ((status IN ('pending_analysis','retryable_error') AND "
            "(next_attempt_at IS NULL OR next_attempt_at<=?)) OR "
            "(status='processing' AND locked_at<=?)) "
            "ORDER BY completed_at LIMIT 1",
            (MAX_LEARNING_ATTEMPTS, now_text, stale_before),
        ).fetchone()
        if not row:
            return None
        previous = row["status"]
        connection.execute(
            "UPDATE task_events SET status='processing',locked_at=?,attempt_count=attempt_count+1 "
            "WHERE event_id=?",
            (now_text, row["event_id"]),
        )
        connection.execute(
            "INSERT INTO learning_event_audit "
            "(event_id,action,status_from,status_to,occurred_at) VALUES(?,?,?,?,?)",
            (row["event_id"], "processing_started", previous, "processing", now_text),
        )
        result = dict(
            connection.execute(
                "SELECT * FROM task_events WHERE event_id=?", (row["event_id"],)
            ).fetchone()
        )
        return result


def finish_learning_event(
    event_id: str, *, status: str, analysis: str | None = None
) -> None:
    """Завершает разбор события и сохраняет только допустимый итоговый статус."""
    init_database()
    if status not in {"analysed", "no_insight", "candidate_created"}:
        raise StorageError("Недопустимый итог анализа")
    now = datetime.now(UTC).isoformat()
    with _connect() as connection:
        row = connection.execute(
            "SELECT status FROM task_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row:
            raise StorageError("Событие обучения не найдено")
        connection.execute(
            "UPDATE task_events SET status=?,analysis=?,analyzed_at=?,locked_at=NULL,"
            "next_attempt_at=NULL,last_error_type=NULL WHERE event_id=?",
            (status, analysis, now, event_id),
        )
        connection.execute(
            "INSERT INTO learning_event_audit "
            "(event_id,action,status_from,status_to,occurred_at) VALUES(?,?,?,?,?)",
            (event_id, "analysis_finished", row["status"], status, now),
        )


def fail_learning_event(
    event_id: str, *, error_type: str, max_attempts: int = 3
) -> str:
    """Планирует ограниченный повтор анализа или закрывает постоянную ошибку."""
    init_database()
    now = datetime.now(UTC)
    with _connect() as connection:
        row = connection.execute(
            "SELECT status,attempt_count FROM task_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row:
            raise StorageError("Событие обучения не найдено")
        attempts = int(row["attempt_count"])
        target = "permanent_error" if attempts >= max_attempts else "retryable_error"
        next_attempt = (
            None
            if target == "permanent_error"
            else datetime.fromtimestamp(
                now.timestamp() + min(300, 2**attempts), UTC
            ).isoformat()
        )
        safe_error = re.sub(r"[^A-Za-z0-9_.-]", "", error_type)[:80] or "UnknownError"
        connection.execute(
            "UPDATE task_events SET status=?,locked_at=NULL,next_attempt_at=?,"
            "last_error_type=? WHERE event_id=?",
            (target, next_attempt, safe_error, event_id),
        )
        connection.execute(
            "INSERT INTO learning_event_audit "
            "(event_id,action,status_from,status_to,occurred_at,details) VALUES(?,?,?,?,?,?)",
            (
                event_id,
                "analysis_failed",
                row["status"],
                target,
                now.isoformat(),
                json.dumps({"error_type": safe_error}),
            ),
        )
        return target


def retry_learning_event(event_id: str) -> None:
    """Начинает новый цикл обработки с audit-записью предыдущих попыток."""
    init_database()
    now = datetime.now(UTC).isoformat()
    with _connect() as connection:
        row = connection.execute(
            "SELECT status,attempt_count FROM task_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row or row["status"] not in {"retryable_error", "permanent_error"}:
            raise StorageError("Повтор доступен только для событий с ошибкой")
        connection.execute(
            "UPDATE task_events SET status='pending_analysis',next_attempt_at=NULL,"
            "last_error_type=NULL,attempt_count=0 WHERE event_id=?",
            (event_id,),
        )
        connection.execute(
            "INSERT INTO learning_event_audit "
            "(event_id,action,status_from,status_to,occurred_at,details) "
            "VALUES(?,?,?,?,?,?)",
            (
                event_id,
                "retry_requested",
                row["status"],
                "pending_analysis",
                now,
                json.dumps({"previous_attempt_count": row["attempt_count"]}),
            ),
        )


def create_learning_candidate(
    event_id: str,
    *,
    category: str,
    title: str,
    content: str,
    rationale: str,
    confidence: float,
    namespace: str,
    analysis_json: str,
) -> dict[str, Any] | None:
    """Создаёт только проверенный проектный кандидат и оставляет audit trail."""
    init_database()
    category = _validate_text(category, "category", 100)
    title = _validate_text(title, "title", 200)
    content = _validate_text(content, "content", 2000)
    rationale = _validate_text(rationale, "rationale", 1000)
    namespace = _validate_text(namespace, "namespace", 200)
    if not namespace.startswith("project:") or not 0 <= confidence <= 1:
        raise StorageError("Learning candidate должен быть project-scoped")
    now = datetime.now(UTC).isoformat()
    digest = hashlib.sha256(
        (
            category.casefold() + "\0" + title.casefold() + "\0" + content.casefold()
        ).encode()
    ).hexdigest()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        event = connection.execute(
            "SELECT verification_id,status FROM task_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if not event or not event["verification_id"]:
            raise StorageError("Learning candidate требует проверенное событие")
        verification = connection.execute(
            "SELECT exit_code,trust_status FROM verification_records WHERE verification_id=?",
            (event["verification_id"],),
        ).fetchone()
        if (
            not verification
            or verification["exit_code"] != 0
            or verification["trust_status"] != "trusted_local_process"
        ):
            raise StorageError("Доказательство локальной проверки недоступно")
        duplicate = connection.execute(
            "SELECT id FROM knowledge WHERE namespace=? AND content_hash=?",
            (namespace, digest),
        ).fetchone()
        if duplicate:
            return None
        cursor = connection.execute(
            "INSERT INTO knowledge (uuid,category,title,content,status,source_type,"
            "source_reference,technology,confidence,namespace,updated_at,content_hash,publishable) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                str(uuid.uuid4()),
                category,
                title,
                content,
                "candidate",
                "learning_engine",
                f"event:{event_id}",
                "",
                confidence,
                namespace,
                now,
                digest,
            ),
        )
        knowledge_id = _last_insert_id(cursor)
        connection.execute(
            "INSERT INTO evidence (knowledge_id,kind,result,source,checked_at,limitations,confidence) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                knowledge_id,
                "local_process_verification",
                _verification_evidence_text(rationale, event["verification_id"])[:1000],
                f"event:{event_id}; verification:{event['verification_id']}",
                now,
                "LLM-generated candidate; requires maintainer review",
                confidence,
            ),
        )
        connection.execute(
            "UPDATE task_events SET status='candidate_created',analysis=?,analyzed_at=?,locked_at=NULL "
            "WHERE event_id=?",
            (analysis_json, now, event_id),
        )
        connection.execute(
            "INSERT INTO learning_event_audit "
            "(event_id,knowledge_id,action,status_from,status_to,occurred_at,details) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                knowledge_id,
                "candidate_created",
                event["status"],
                "candidate_created",
                now,
                json.dumps({"namespace": namespace}),
            ),
        )
        return dict(
            connection.execute(
                "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
            ).fetchone()
        )


def withdraw_learning_candidate(knowledge_id: int, *, reason: str) -> dict[str, Any]:
    """Помечает только кандидат Learning Engine отклонённым с сохранением причины."""
    init_database()
    reason = _validate_text(reason, "reason", 500)
    now = datetime.now(UTC).isoformat()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id,status,source_type,source_reference FROM knowledge WHERE id=?",
            (knowledge_id,),
        ).fetchone()
        if (
            not row
            or row["status"] != "candidate"
            or row["source_type"] != "learning_engine"
        ):
            raise StorageError(
                "Можно отменить только candidate, созданный Learning Engine"
            )
        connection.execute(
            "UPDATE knowledge SET status='rejected',updated_at=? WHERE id=?",
            (now, knowledge_id),
        )
        event_id = str(row["source_reference"] or "").removeprefix("event:")
        connection.execute(
            "INSERT INTO learning_event_audit "
            "(event_id,knowledge_id,action,status_from,status_to,occurred_at,details) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                knowledge_id,
                "candidate_withdrawn",
                "candidate",
                "rejected",
                now,
                json.dumps({"reason": reason}, ensure_ascii=False),
            ),
        )
        return dict(
            connection.execute(
                "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
            ).fetchone()
        )


def learning_status_summary() -> dict[str, Any]:
    """Считает состояние очереди анализа без изменения событий."""
    init_database()
    with _connect() as connection:
        counts = {
            row["status"]: row["total"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS total FROM task_events GROUP BY status"
            )
        }
        last_success = connection.execute(
            "SELECT MAX(analyzed_at) FROM task_events WHERE status IN "
            "('analysed','no_insight','candidate_created')"
        ).fetchone()[0]
        verification_counts = connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN exit_code != 0 THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN verification_type='command_launch' THEN 1 ELSE 0 END) "
            "AS launch_failures FROM verification_records"
        ).fetchone()
        return {
            "total": sum(counts.values()),
            "status_counts": counts,
            "verification_attempts": {
                "total": verification_counts["total"],
                "failed": verification_counts["failed"] or 0,
                "launch_failures": verification_counts["launch_failures"] or 0,
            },
            "llm_provider": "ollama-local"
            if os.environ.get("AI_MEMORY_OLLAMA_MODEL")
            else "not configured",
            "automatic_analysis": False,
            "analyzed": counts.get("analysed", 0) + counts.get("no_insight", 0),
            "candidates_created": counts.get("candidate_created", 0),
            "failures": counts.get("retryable_error", 0)
            + counts.get("permanent_error", 0),
            "last_successful_analysis": last_success,
        }


def get_task_status(event_id: str) -> dict[str, Any] | None:
    """Возвращает сохранённое состояние обработки события."""
    init_database()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM task_events WHERE event_id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None


def export_knowledge(knowledge_ids: list[int]) -> dict[str, Any]:
    """Экспортирует только явно выбранные публичные утверждённые знания."""
    if not knowledge_ids or len(knowledge_ids) > 500:
        raise StorageError("Укажите от 1 до 500 ID")
    init_database()
    with _connect() as connection:
        placeholders = ",".join("?" for _ in knowledge_ids)
        rows = connection.execute(
            f"SELECT * FROM knowledge WHERE id IN ({placeholders})", knowledge_ids
        ).fetchall()
    if len(rows) != len(set(knowledge_ids)) or any(
        r["status"] != "approved" or not r["publishable"] for r in rows
    ):
        raise StorageError(
            "Экспорт разрешён только для выбранных approved-записей с publishable=true"
        )
    for row in rows:
        exported_text = " ".join(
            str(row[field] or "")
            for field in (
                "category",
                "title",
                "content",
                "technology",
                "source_reference",
            )
        )
        if _contains_sensitive_text(exported_text):
            raise StorageError(
                "Возможные чувствительные данные обнаружены; запись не экспортирована"
            )
    return {
        "schema_version": 1,
        "knowledge": [
            {
                k: r[k]
                for k in (
                    "uuid",
                    "category",
                    "title",
                    "content",
                    "technology",
                    "confidence",
                    "source_type",
                    "source_reference",
                )
            }
            for r in rows
        ],
    }


def validate_import(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Проверяет версию и поля импортируемого JSON без записи в базу."""
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("knowledge"), list)
    ):
        raise StorageError("Неподдерживаемый формат импорта")
    cleaned: list[dict[str, Any]] = []
    for item in payload["knowledge"]:
        if not isinstance(item, dict):
            raise StorageError("Запись импорта должна быть объектом")
        category = item.get("category")
        title = item.get("title")
        content = item.get("content")
        technology = item.get("technology", "")
        source_reference = item.get("source_reference")
        if not isinstance(category, str):
            raise StorageError("category должен быть строкой")
        if not isinstance(title, str):
            raise StorageError("title должен быть строкой")
        if not isinstance(content, str):
            raise StorageError("content должен быть строкой")
        if not isinstance(technology, str):
            raise StorageError("technology должен быть строкой")
        if source_reference is not None and not isinstance(source_reference, str):
            raise StorageError("source_reference должен быть строкой или null")
        if _contains_sensitive_text(
            " ".join(
                value
                for value in (category, title, content, technology, source_reference)
                if isinstance(value, str)
            )
        ):
            raise StorageError("Импорт содержит чувствительные данные")
        cleaned.append(
            {
                "category": _validate_text(category, "category", 100),
                "title": _validate_text(title, "title", 500),
                "content": _validate_text(content, "content"),
                "technology": technology[:500],
                "source_reference": source_reference,
            }
        )
    return cleaned


def import_knowledge(
    payload: dict[str, Any], *, namespace: str = "global"
) -> list[int]:
    """Импортирует проверенные записи как новые неподтверждённые кандидаты."""
    cleaned = validate_import(payload)
    ids = []
    for item in cleaned:
        try:
            ids.append(
                int(
                    create_knowledge(**item, source_type="import", namespace=namespace)[
                        "id"
                    ]
                )
            )
        except StorageError as exc:
            if "дубликат" not in str(exc):
                raise
    return ids


def backup_database(destination: Path) -> Path:
    """Создаёт проверенную атомарную копию базы через SQLite backup API."""
    destination = destination.resolve()
    source_path = current_database_path().resolve()
    if destination == source_path:
        raise StorageError("Путь резервной копии совпадает с активной базой")
    if not source_path.is_file():
        raise StorageError(f"Активная база не найдена: {source_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with (
            _connect(read_only=True) as source,
            closing(sqlite3.connect(temporary)) as target,
        ):
            source.backup(target)
        _validate_database_file(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _validate_database_file(path: Path, *, require_current: bool = False) -> None:
    """Проверяет целостность SQLite-файла и наличие таблицы знаний без записи."""
    if not path.is_file():
        raise StorageError(f"Файл базы не найден: {path}")
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise StorageError(
                    f"Проверка SQLite не пройдена: {result[0] if result else 'нет результата'}"
                )
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge'"
            ).fetchone()
            if table is None:
                raise StorageError("В файле нет таблицы knowledge")
            if require_current:
                connection.row_factory = sqlite3.Row
                if not migrations.MigrationEngine().is_current(connection):
                    raise StorageError("В файле нет актуальной схемы базы")
    except sqlite3.DatabaseError as exc:
        raise StorageError(f"Некорректный файл базы: {exc}") from exc


def verify_database_backup(source_path: Path) -> dict[str, Any]:
    """Проверяет резервную копию без миграции или изменения её файла."""
    source_path = source_path.expanduser().resolve()
    _validate_database_file(source_path)
    with closing(
        sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        schema_version = _database_schema_version(connection)
        schema_current = migrations.MigrationEngine().is_current(connection)
        knowledge_count = connection.execute(
            "SELECT count(*) FROM knowledge"
        ).fetchone()[0]
    return {
        "valid": True,
        "source": str(source_path),
        "schema_version": schema_version,
        "schema_current": schema_current,
        "knowledge_count": knowledge_count,
    }


def _database_schema_version(connection: sqlite3.Connection) -> int:
    """Возвращает номер схемы либо ноль для базы до migration registry."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if table is None:
        return 0
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
        ).fetchone()[0]
    )


def _knowledge_id_digest(connection: sqlite3.Connection) -> tuple[int, str]:
    """Считает устойчивую контрольную сумму ID без чтения текстов знаний."""
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute("SELECT id FROM knowledge ORDER BY id"):
        digest.update(int(row[0]).to_bytes(8, byteorder="big", signed=False))
        count += 1
    return count, digest.hexdigest()


def dry_run_restore_database(
    source_path: Path, *, temporary_directory: Path | None = None
) -> dict[str, Any]:
    """Проверяет копию и миграцию восстановления, не меняя активную БД."""
    source_path = source_path.expanduser().resolve()
    destination = current_database_path().resolve()
    if source_path == destination:
        raise StorageError("Источник восстановления совпадает с активной базой")
    _validate_database_file(source_path)

    temporary_root = (
        temporary_directory.expanduser().resolve()
        if temporary_directory is not None
        else None
    )
    descriptor, staged_name = tempfile.mkstemp(
        prefix="aem-restore-dry-run-",
        suffix=".sqlite",
        dir=temporary_root,
    )
    os.close(descriptor)
    staged = Path(staged_name)
    try:
        with (
            closing(
                sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(staged)) as target,
        ):
            source_ids = _knowledge_id_digest(source)
            source_version = _database_schema_version(source)
            source.backup(target)
        with closing(sqlite3.connect(staged)) as candidate:
            candidate.row_factory = sqlite3.Row
            migrations.MigrationEngine().apply(candidate)
            candidate.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            candidate.execute("PRAGMA journal_mode=DELETE")
            restored_ids = _knowledge_id_digest(candidate)
            restored_version = _database_schema_version(candidate)
        _validate_database_file(staged, require_current=True)
        if source_ids != restored_ids:
            raise StorageError("Проверка восстановления изменила integer ID знаний")
        return {
            "valid": True,
            "dry_run": True,
            "source": str(source_path),
            "destination": str(destination),
            "knowledge_count": restored_ids[0],
            "knowledge_id_digest": restored_ids[1],
            "source_schema_version": source_version,
            "restored_schema_version": restored_version,
            "ids_preserved": True,
        }
    finally:
        staged.unlink(missing_ok=True)
        Path(str(staged) + "-wal").unlink(missing_ok=True)
        Path(str(staged) + "-shm").unlink(missing_ok=True)
        Path(str(staged) + "-journal").unlink(missing_ok=True)


def restore_database(source_path: Path) -> dict[str, str]:
    """Атомарно восстанавливает активную базу, сохраняя её копию до замены."""
    source_path = source_path.expanduser().resolve()
    destination = current_database_path().resolve()
    if source_path == destination:
        raise StorageError("Источник восстановления совпадает с активной базой")
    _validate_database_file(source_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.restore")
    previous = destination.with_name(
        f"{destination.stem}.pre-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}{destination.suffix}"
    )
    replaced = False
    try:
        with (
            closing(
                sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(staged)) as target,
        ):
            source.backup(target)
        with closing(sqlite3.connect(staged)) as candidate:
            candidate.row_factory = sqlite3.Row
            migrations.MigrationEngine().apply(candidate)
            candidate.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            candidate.execute("PRAGMA journal_mode=DELETE")
        _validate_database_file(staged, require_current=True)
        if destination.exists():
            backup_database(previous)
        if destination.exists():
            with _connect() as active:
                active.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        staged.replace(destination)
        replaced = True
        _validate_database_file(destination, require_current=True)
        return {
            "database": str(destination),
            "source": str(source_path),
            "pre_restore_backup": str(previous) if previous.exists() else "",
        }
    except Exception:
        if replaced:
            if previous.exists():
                previous.replace(destination)
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        staged.unlink(missing_ok=True)
        Path(str(staged) + "-wal").unlink(missing_ok=True)
        Path(str(staged) + "-shm").unlink(missing_ok=True)


def _skills_root() -> Path:
    """Возвращает каталог Skills, только если он находится внутри репозитория."""
    base_dir = BASE_DIR.resolve()
    skills_dir = (base_dir / "skills").resolve()
    if not skills_dir.is_relative_to(base_dir):
        raise StorageError("Каталог Skills выходит за пределы проекта")
    if skills_dir.exists() and not skills_dir.is_dir():
        raise StorageError("Путь Skills должен указывать на каталог")
    return skills_dir


def _skill_file_path(name: str) -> Path:
    """Разрешает путь Skill и запрещает выход через symlink за каталог Skills."""
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        raise StorageError("Недопустимое имя Skill")
    skills_dir = _skills_root()
    path = (skills_dir / name / "SKILL.md").resolve()
    if not path.is_relative_to(skills_dir):
        raise StorageError("Путь Skill выходит за разрешённый каталог")
    return path


def list_skills() -> list[dict[str, Any]]:
    """Находит Skills с валидным front matter внутри разрешённого каталога."""
    skills_dir = _skills_root()
    result: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return result
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        safe_path = _skill_file_path(path.parent.name)
        if not safe_path.is_file():
            continue
        text = safe_path.read_text(encoding="utf-8")
        metadata = _skill_frontmatter(text)
        if _contains_sensitive_text(text):
            raise StorageError("Активный Skill содержит возможные приватные данные")
        result.append(
            {**metadata, "path": f"skills/{path.parent.name}", "status": "active"}
        )
    return result


def _skill_frontmatter(text: str) -> dict[str, str]:
    """Извлекает обязательные поля YAML front matter без небезопасного загрузчика."""
    if not text.startswith("---\n") or "\n---" not in text[4:]:
        raise StorageError("SKILL.md должен начинаться с YAML front matter")
    header = text[4 : text.index("\n---", 4)]
    fields: dict[str, str] = {}
    in_metadata = False
    for line in header.splitlines():
        if line == "metadata:":
            in_metadata = True
            continue
        nested = re.match(r"^\s+([a-zA-Z_-]+):\s*['\"]?(.+?)['\"]?\s*$", line)
        top = re.match(r"^([a-zA-Z_-]+):\s*['\"]?(.+?)['\"]?\s*$", line)
        if nested and in_metadata:
            fields[nested.group(1)] = nested.group(2).strip("'\"")
        elif top:
            key, value = top.groups()
            in_metadata = key == "metadata"
            if not in_metadata:
                fields[key] = value.strip("'\"")
    if not fields.get("name") or not fields.get("description"):
        raise StorageError("В SKILL.md обязательны name и description")
    return fields


def get_skill(name: str) -> dict[str, Any]:
    """Возвращает Skill по имени после проверки его пути и front matter."""
    path = _skill_file_path(name)
    if not path.is_file():
        raise StorageError("Skill не найден")
    content = path.read_text(encoding="utf-8")
    if _contains_sensitive_text(content):
        raise StorageError("Skill содержит возможные приватные данные")
    return {**_skill_frontmatter(content), "content": content}


def validate_skill(content: str) -> dict[str, Any]:
    """Проверяет формат SKILL.md и обязательные поля Agent Skills."""
    metadata = _skill_frontmatter(content)
    return {"valid": True, "metadata": metadata}


def propose_skill_update(
    skill_name: str, proposer: str, description: str, content: str
) -> str:
    """Проверяет метаданные и текст перед сохранением кандидата Skill."""
    if not re.fullmatch(r"[a-z0-9-]{1,64}", skill_name):
        raise StorageError("Недопустимое имя Skill")
    safe_proposer = _validate_text(proposer, "proposer", 100)
    safe_description = _validate_text(description, "description", 1_000)
    if _contains_sensitive_text(safe_proposer) or _contains_sensitive_text(
        safe_description
    ):
        raise StorageError(
            "Метаданные предложения Skill содержат возможные приватные данные"
        )
    _validate_text(content, "content", MAX_TEXT_LENGTH)
    if _contains_sensitive_text(content):
        raise StorageError("Предложение Skill содержит возможные приватные данные")
    metadata = validate_skill(content)["metadata"]
    if metadata["name"] != skill_name:
        raise StorageError("Имя в SKILL.md не совпадает с именем предложения")
    init_database()
    proposal_id = str(uuid.uuid4())
    with _connect() as connection:
        connection.execute(
            "INSERT INTO skill_proposals(id,skill_name,proposer,change_description,proposed_content,created_at) VALUES(?,?,?,?,?,?)",
            (
                proposal_id,
                skill_name,
                safe_proposer,
                safe_description,
                content,
                datetime.now(UTC).isoformat(),
            ),
        )
    return proposal_id


def list_skill_proposals(status: str = "candidate") -> list[dict[str, Any]]:
    """Выводит предложения Skills, скрывая их полный текст по умолчанию."""
    init_database()
    with _connect() as connection:
        proposals = [
            dict(r)
            for r in connection.execute(
                "SELECT id,skill_name,proposer,change_description,status,created_at "
                "FROM skill_proposals WHERE status=? ORDER BY created_at DESC",
                (status,),
            )
        ]
    for proposal in proposals:
        for field in ("proposer", "change_description"):
            if _contains_sensitive_text(proposal[field]):
                proposal[field] = "[redacted]"
    return proposals


def get_skill_proposal(proposal_id: str) -> dict[str, Any]:
    """Возвращает текст кандидата Skill и активную версию для сравнения."""
    if not isinstance(proposal_id, str):
        raise StorageError("Недопустимый ID предложения Skill")
    try:
        if str(uuid.UUID(proposal_id)) != proposal_id:
            raise ValueError
    except ValueError as exc:
        raise StorageError("Недопустимый ID предложения Skill") from exc
    init_database()
    with _connect() as connection:
        row = connection.execute(
            "SELECT id,skill_name,proposer,change_description,status,created_at,"
            "proposed_content FROM skill_proposals WHERE id=? AND status='candidate'",
            (proposal_id,),
        ).fetchone()
    if row is None:
        raise StorageError("Активное предложение Skill не найдено")
    result = dict(row)
    if any(
        _contains_sensitive_text(result[field])
        for field in ("proposer", "change_description", "proposed_content")
    ):
        raise StorageError("Предложение Skill содержит возможные приватные данные")
    current_path = _skill_file_path(result["skill_name"])
    result["current_content"] = (
        current_path.read_text(encoding="utf-8") if current_path.is_file() else None
    )
    if result["current_content"] and _contains_sensitive_text(
        result["current_content"]
    ):
        raise StorageError("Активный Skill содержит возможные приватные данные")
    return result


def decide_skill_proposal(
    proposal_id: str, *, decision: str, maintainer: str, reason: str = ""
) -> dict[str, Any]:
    """Принимает или отклоняет предложение, проверяя локальное имя maintainer."""
    if decision not in {"approved", "rejected"}:
        raise StorageError("Недопустимое решение")
    maintainer = require_maintainer(maintainer)
    if reason:
        reason = _validate_decision_reason(reason)
    init_database()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM skill_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if not row or row["status"] != "candidate":
            raise StorageError("Предложение не найдено или уже обработано")
        now = datetime.now(UTC).isoformat()
        current_path = _skill_file_path(row["skill_name"])
        original = (
            current_path.read_text(encoding="utf-8") if current_path.is_file() else None
        )
        if decision == "approved" and any(
            _contains_sensitive_text(value)
            for value in (
                row["proposer"],
                row["change_description"],
                row["proposed_content"],
            )
        ):
            raise StorageError("Предложение Skill содержит возможные приватные данные")
        version = "0.1.0"
        previous = None
        if decision == "approved":
            if original and _contains_sensitive_text(original):
                raise StorageError("Активный Skill содержит возможные приватные данные")
            proposed = validate_skill(row["proposed_content"])["metadata"]
            version = proposed.get("version", "")
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise StorageError("Для публикации укажите SemVer в metadata.version")
            if original:
                current = _skill_frontmatter(original)
                previous = current.get("version")
                if not previous or not re.fullmatch(r"\d+\.\d+\.\d+", previous):
                    raise StorageError(
                        "Текущий Skill должен иметь SemVer в metadata.version"
                    )
                if tuple(map(int, version.split("."))) <= tuple(
                    map(int, previous.split("."))
                ):
                    raise StorageError("Новая версия должна быть выше текущей")
        try:
            if decision == "approved":
                current_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".SKILL-", suffix=".tmp", dir=current_path.parent
                )
                try:
                    with os.fdopen(
                        descriptor, "w", encoding="utf-8", newline="\n"
                    ) as stream:
                        stream.write(row["proposed_content"])
                        stream.flush()
                        os.fsync(stream.fileno())
                    Path(temporary_name).replace(current_path)
                finally:
                    Path(temporary_name).unlink(missing_ok=True)
            connection.execute(
                "UPDATE skill_proposals SET status=?,decided_at=?,decision_by=?,decision_reason=? WHERE id=?",
                (decision, now, maintainer, reason, proposal_id),
            )
            if decision == "approved":
                if original is not None and previous is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO skill_versions(skill_name,version,content,changelog,validation_result,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            row["skill_name"],
                            previous,
                            original,
                            "Исходная утверждённая версия",
                            "validated",
                            now,
                        ),
                    )
                connection.execute(
                    "INSERT INTO skill_versions(skill_name,version,content,changelog,previous_version,proposal_id,validation_result,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        row["skill_name"],
                        version,
                        row["proposed_content"],
                        row["change_description"],
                        previous,
                        proposal_id,
                        "validated",
                        now,
                    ),
                )
            result = connection.execute(
                "SELECT id,skill_name,status,decided_at,decision_by,decision_reason FROM skill_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
            return dict(result)
        except Exception:
            if decision == "approved":
                if original is None:
                    current_path.unlink(missing_ok=True)
                else:
                    current_path.write_text(original, encoding="utf-8")
            raise


def get_skill_history(skill_name: str) -> list[dict[str, Any]]:
    """Возвращает утверждённые версии Skill в обратном хронологическом порядке."""
    if not re.fullmatch(r"[a-z0-9-]{1,64}", skill_name):
        raise StorageError("Недопустимое имя Skill")
    init_database()
    with _connect() as connection:
        versions = [
            dict(row)
            for row in connection.execute(
                "SELECT id,skill_name,version,changelog,previous_version,proposal_id,validation_result,created_at FROM skill_versions WHERE skill_name=? ORDER BY id DESC",
                (skill_name,),
            )
        ]
    for version in versions:
        if _contains_sensitive_text(version["changelog"]):
            version["changelog"] = "[redacted]"
    return versions


def rollback_skill_version(
    skill_name: str, version: str, *, maintainer: str
) -> dict[str, Any]:
    """Атомарно восстанавливает содержимое выбранной версии без удаления истории."""
    maintainer = require_maintainer(maintainer)
    init_database()
    path = _skill_file_path(skill_name)
    now = datetime.now(UTC).isoformat()
    with _connect() as connection:
        target = connection.execute(
            "SELECT * FROM skill_versions WHERE skill_name=? AND version=?",
            (skill_name, version),
        ).fetchone()
        if not target:
            raise StorageError("Версия Skill не найдена")
        original = path.read_text(encoding="utf-8") if path.is_file() else None
        if _contains_sensitive_text(target["content"]):
            raise StorageError("Версия Skill содержит возможные приватные данные")
        if original and _contains_sensitive_text(original):
            raise StorageError("Активный Skill содержит возможные приватные данные")
        metadata = validate_skill(target["content"])["metadata"]
        if metadata["name"] != skill_name or metadata.get("version") != version:
            raise StorageError("Метаданные версии Skill не совпадают с записью истории")
        current_version = (
            _skill_frontmatter(original).get("version") if original else None
        )
        if current_version:
            connection.execute(
                "INSERT OR IGNORE INTO skill_versions(skill_name,version,content,changelog,previous_version,validation_result,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    skill_name,
                    current_version,
                    original,
                    "Сохранение перед откатом",
                    version,
                    "validated",
                    now,
                ),
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".SKILL-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(target["content"])
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary_name).replace(path)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return {"skill_name": skill_name, "version": version, "restored_at": now}
