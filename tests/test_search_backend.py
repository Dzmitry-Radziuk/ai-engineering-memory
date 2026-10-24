"""Регрессионные проверки границ и read-only режима search backend plugins."""

from __future__ import annotations

from ai_engineering_memory import storage
from ai_engineering_memory.application import MemoryService
from ai_engineering_memory.plugin_api import SearchHit, SearchQuery
from ai_engineering_memory.settings import Settings
from ai_engineering_memory.sqlite_repository import SqliteKnowledgeRepository
from ai_engineering_memory.storage import create_storage


class FakeSearchBackend:
    """Возвращает заданные ID и сохраняет запрос для проверки адаптера."""

    plugin_api_version = 1

    def __init__(self, hits: list[SearchHit]) -> None:
        """Запоминает ранжированные результаты без содержимого записей."""
        self.hits = hits
        self.queries: list[SearchQuery] = []

    def search(self, query: SearchQuery) -> list[SearchHit]:
        """Возвращает подготовленный набор ID кандидатов на выдачу."""
        self.queries.append(query)
        return self.hits


def test_plugin_search_returns_only_canonical_rows_with_matching_scope(tmp_path):
    """Ядро отбрасывает чужие namespace и строит ответ только из SQLite."""
    store = create_storage(Settings(tmp_path / "search.sqlite"))
    with store.activate():
        local = storage.create_knowledge(
            "backend",
            "Local row",
            "Stored local content",
            namespace="project:local",
        )
        foreign = storage.create_knowledge(
            "backend",
            "Foreign row",
            "Stored foreign content",
            namespace="project:other",
        )
    backend = FakeSearchBackend(
        [
            SearchHit(foreign["id"], score=100),
            SearchHit(local["id"], score=1),
            SearchHit(local["id"], score=0),
        ]
    )
    service = MemoryService(
        SqliteKnowledgeRepository(store),
        search_backend=backend,
    )

    rows = service.search(
        "local content",
        namespace="project:local",
        status="candidate",
        limit=1,
        read_only=True,
    )

    assert len(rows) == 1
    assert rows[0]["id"] == local["id"]
    assert rows[0]["content"] == "Stored local content"
    assert backend.queries[0].namespace == "project:local"
    assert backend.queries[0].read_only is True


def test_sqlite_batch_lookup_preserves_plugin_order_and_deduplicates(tmp_path):
    """Пакетная проверка ID возвращает канонические строки в ранжированном порядке."""
    store = create_storage(Settings(tmp_path / "batch.sqlite"))
    with store.activate():
        first = storage.create_knowledge("test", "First", "First row")
        second = storage.create_knowledge("test", "Second", "Second row")

    rows = store.get_knowledge_by_ids(
        [second["id"], first["id"], second["id"]],
        _read_only=True,
    )

    assert [row["id"] for row in rows] == [second["id"], first["id"]]


def test_plugin_search_cannot_bypass_status_or_category_filters(tmp_path):
    """Backend не может протащить candidate в approved или чужую категорию."""
    store = create_storage(Settings(tmp_path / "filters.sqlite"))
    with store.activate():
        candidate = storage.create_knowledge(
            "backend",
            "Candidate row",
            "Not approved",
            namespace="project:filters",
        )
    backend = FakeSearchBackend([SearchHit(candidate["id"])])
    service = MemoryService(
        SqliteKnowledgeRepository(store),
        search_backend=backend,
    )

    assert (
        service.search(
            "candidate",
            namespace="project:filters",
            status="approved",
        )
        == []
    )
    assert (
        service.search(
            "candidate",
            namespace="project:filters",
            status="candidate",
            category="security",
        )
        == []
    )


def test_plugin_read_only_lookup_does_not_create_missing_database(tmp_path):
    """Повторная проверка ID остаётся read-only и при отсутствующей базе."""
    database_path = tmp_path / "missing.sqlite"
    backend = FakeSearchBackend([SearchHit(1)])
    service = MemoryService(
        SqliteKnowledgeRepository(create_storage(Settings(database_path))),
        search_backend=backend,
    )

    assert (
        service.search(
            "query",
            namespace="global",
            read_only=True,
        )
        == []
    )
    assert not database_path.exists()


def test_plugin_search_validates_limits_before_calling_backend(tmp_path):
    """Нестандартный backend не обходит лимиты и базовую валидацию запроса."""
    backend = FakeSearchBackend([])
    service = MemoryService(
        SqliteKnowledgeRepository(create_storage(Settings(tmp_path / "limits.sqlite"))),
        search_backend=backend,
    )

    try:
        service.search("query", namespace="global", limit=101)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("Лимит 101 должен отклоняться")
    assert backend.queries == []
